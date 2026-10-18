"""Bounded server-side audit journal. No keys, credentials, request headers or user data."""
from __future__ import annotations
import json,sqlite3,time,uuid

class DiagnosticLog:
    MAX_ROWS=100000
    MAX_STATE_ROWS=20000
    MAX_CRITICAL_ROWS=20000
    MAX_EVENT_BYTES=262144
    def __init__(self,path,asset):
        self.path=path;self.asset=asset;self.boot=uuid.uuid4().hex;self.db=None
        self._captures={};self.last_commit=time.monotonic();self.pruned_by_priority={0:0,1:0,2:0}
        self.written=0;self.pruned=0;self.errors=0;self.last_error=None;self.pending=0
        try:
            self.db=sqlite3.connect(path,timeout=.1)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, payload TEXT NOT NULL)')
            columns={r[1] for r in self.db.execute('PRAGMA table_info(events)')}
            if 'priority' not in columns:self.db.execute('ALTER TABLE events ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            self.db.execute('CREATE INDEX IF NOT EXISTS events_priority_seq ON events(priority,seq)')
            self.db.commit()
        except Exception as exc:self._error(exc)
    def _error(self,exc):
        self.errors+=1;self.last_error=type(exc).__name__+': '+str(exc)[:200]
        print('POLY_DIAGNOSTIC_ERROR',self.asset,self.last_error,flush=True)
    def append(self,kind,**data):
        if self.db is None:return
        try:
            now=time.time();event={'t':now,'kind':kind,'asset':self.asset,'bootId':self.boot,**data}
            payload=json.dumps(event,separators=(',',':'),allow_nan=False)
            if len(payload.encode())>self.MAX_EVENT_BYTES:
                event={'t':now,'kind':'OVERSIZE_EVENT','asset':self.asset,'bootId':self.boot,'originalKind':kind,'bytes':len(payload.encode())}
                payload=json.dumps(event);self.errors+=1
            priority=0 if kind in {'CLOB_MESSAGE','RTDS_MESSAGE','REST_RESPONSE'} else 1 if kind=='STATE' else 2
            self.db.execute('INSERT INTO events(t,payload,priority) VALUES (?,?,?)',(now,payload,priority));self.written+=1;self.pending+=1
            if self.written%1000==0:
                for category,limit in ((0,self.MAX_ROWS),(1,self.MAX_STATE_ROWS),(2,self.MAX_CRITICAL_ROWS)):
                    cur=self.db.execute('DELETE FROM events WHERE priority=? AND seq <= (SELECT seq FROM events WHERE priority=? ORDER BY seq DESC LIMIT 1 OFFSET ?)',(category,category,limit))
                    self.pruned+=cur.rowcount;self.pruned_by_priority[category]+=cur.rowcount
            # Batching raw messages avoids one synchronous disk commit per tick.
            # Critical evidence is always committed immediately.
            if self.pending>=50 or priority==2 or time.monotonic()-self.last_commit>=.25:
                self.db.commit();self.pending=0;self.last_commit=time.monotonic()
        except Exception as exc:self._error(exc)
    def status(self):
        return {'enabled':self.db is not None,'bootId':self.boot,'eventsWrittenThisBoot':self.written,
                'prunedThisBoot':self.pruned,'errors':self.errors,'lastError':self.last_error,
                'retentionRowsPerAsset':self.MAX_ROWS,'stateRetentionRows':self.MAX_STATE_ROWS,'criticalRetentionRows':self.MAX_CRITICAL_ROWS,
                'prunedByPriorityThisBoot':dict(self.pruned_by_priority),'retention':'separate raw, state and critical budgets; ephemeral server disk; download before restart/deploy'}
    def capture(self):
        if self.db is not None:self.db.commit();self.pending=0
        meta=self.status();meta['captureAt']=time.time();meta['cutoffJournalSeq']=0
        if self.db is not None:
            # Pin the read snapshot now, before compression or another asset's
            # export can allow pruning to change this capture's contents.
            reader=sqlite3.connect(self.path,timeout=.5,check_same_thread=False)
            reader.execute('BEGIN')
            meta['cutoffJournalSeq']=reader.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0]
            meta['captureId']=uuid.uuid4().hex;self._captures[meta['captureId']]=reader
        return meta
    def export(self,output,capture=None):
        meta=capture if capture is not None else self.capture()
        if not meta['enabled']:return {**meta,'retained':0,'complete':False}
        count=0;first=last=None;seq_first=seq_last=None;categories={0:0,1:0,2:0}
        # Separate reader connection supports background ZIP generation while the engine writes.
        reader=self._captures.pop(meta.get('captureId'),None)
        if reader is None:reader=sqlite3.connect(self.path,timeout=.5)
        try:
            buffer=bytearray()
            for seq,t,payload,priority in reader.execute('SELECT seq,t,payload,priority FROM events WHERE seq<=? ORDER BY seq',(meta['cutoffJournalSeq'],)):
                buffer.extend(('{"journalSeq":'+str(seq)+','+payload[1:]+'\n').encode())
                if len(buffer)>=256*1024:
                    output.write(buffer);buffer.clear()
                count+=1;categories[priority]+=1
                if first is None:first=t;seq_first=seq
                last=t;seq_last=seq
            if buffer:output.write(buffer)
        finally:
            reader.close()
        return {**meta,'retained':count,'firstEventAt':first,'lastEventAt':last,
                'retainedRawRows':categories[0],'retainedStateRows':categories[1],'retainedCriticalRows':categories[2],
                'firstJournalSeq':seq_first,'lastJournalSeq':seq_last,'earlierRowsPruned':count<meta['cutoffJournalSeq'],
                'complete':meta['errors']==0 and count==meta['cutoffJournalSeq']}
    def release_capture(self,capture):
        reader=self._captures.pop(capture.get('captureId'),None)
        if reader is not None:reader.close()
    def close(self):
        if self.db is not None:
            try:self.db.commit();self.db.close()
            except Exception:pass
            self.db=None
    def __del__(self):
        self.close()

