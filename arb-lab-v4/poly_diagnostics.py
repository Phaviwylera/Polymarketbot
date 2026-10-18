"""Bounded server-side audit journal. No keys, credentials, request headers or user data."""
from __future__ import annotations
import json,sqlite3,time,uuid

class DiagnosticLog:
    MAX_ROWS=100000
    MAX_EVENT_BYTES=262144
    def __init__(self,path,asset):
        self.path=path;self.asset=asset;self.boot=uuid.uuid4().hex;self.db=None
        self.written=0;self.pruned=0;self.errors=0;self.last_error=None;self.pending=0
        try:
            self.db=sqlite3.connect(path,timeout=.1,isolation_level=None)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, payload TEXT NOT NULL)')
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
            self.db.execute('INSERT INTO events(t,payload) VALUES (?,?)',(now,payload));self.written+=1;self.pending+=1
            if self.written%1000==0:
                cur=self.db.execute('DELETE FROM events WHERE seq <= (SELECT coalesce(max(seq),0)-? FROM events)',(self.MAX_ROWS,))
                self.pruned+=cur.rowcount
            if self.pending>=50 or kind in {'QUOTE_FILL_EVIDENCE','QUOTE_CANCEL','ENGINE_EVENT'}:
                self.db.commit();self.pending=0
        except Exception as exc:self._error(exc)
    def status(self):
        return {'enabled':self.db is not None,'bootId':self.boot,'eventsWrittenThisBoot':self.written,
                'prunedThisBoot':self.pruned,'errors':self.errors,'lastError':self.last_error,
                'retentionRowsPerAsset':self.MAX_ROWS,'retention':'rolling tail; ephemeral server disk; download before restart/deploy'}
    def export(self,output):
        if self.db is None:return {**self.status(),'retained':0,'complete':False}
        self.db.commit();self.pending=0
        count=0;first=last=None;seq_first=seq_last=None
        for seq,t,payload in self.db.execute('SELECT seq,t,payload FROM events ORDER BY seq'):
            output.write(('{"journalSeq":'+str(seq)+','+payload[1:]+'\n').encode())
            count+=1
            if first is None:first=t;seq_first=seq
            last=t;seq_last=seq
        return {**self.status(),'retained':count,'firstEventAt':first,'lastEventAt':last,
                'firstJournalSeq':seq_first,'lastJournalSeq':seq_last,'earlierRowsPruned':bool(seq_first and seq_first>1),
                'complete':self.errors==0 and (not seq_first or seq_first==1)}
