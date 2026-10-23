"""Asynchronous PostgreSQL checkpoints and evidence, with one paper-engine owner.

Local SQLite remains the raw feed spool. Only research outcomes, execution evidence,
sampled decisions and checkpoints go to PostgreSQL. No credentials enter snapshots.
"""
import asyncio,copy,json,os,time,base64,gzip
from collections import deque

LOCK_ID=161000702
HEALTH_WINDOW_SEC=20.0   # entries pause when the last durable commit is older than this
SCHEMA="""
CREATE TABLE IF NOT EXISTS poly_v161_state (
  name text PRIMARY KEY, updated_at double precision NOT NULL, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS poly_v161_events (
  id text PRIMARY KEY, t double precision NOT NULL, asset text NOT NULL,
  kind text NOT NULL, payload jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS poly_v161_events_kind_time ON poly_v161_events(kind,t DESC);
"""
KINDS={'SHADOW_OUTCOME','MODEL_EVALUATION','TRADE_ATTRIBUTION','EXECUTION_REJECT',
       'EXIT_ATTEMPT','EXIT_WAIT','MOMENTUM_ORDER','ENGINE_EVENT','DECISION_SAMPLE',
       'PAIR_QUOTE','PAIR_CANCEL','PAIR_FIRST_FILL','PAIR_HEDGE_FILL','PAIR_EXIT_REQUEST','PAIR_OUTCOME','PAIR_ERROR'}
def checkpoint(snapshot):
    keys={'asset','revision','serverTime','paper','market','bbo','openRef','openTrusted',
          'spot','twap','spotAgeSec','twapAgeSec','model','candidate','decision','mode','reasonCounts'}
    out={k:copy.deepcopy(snapshot[k]) for k in ('revision','serverTime','portfolio','ledgerHistory','strategyConfig') if k in snapshot}
    out['assets']={a:{k:copy.deepcopy(v) for k,v in s.items() if k in keys} for a,s in snapshot.get('assets',{}).items()}
    return out

class DurableStore:
    def __init__(self,engine,url=None):
        self.engine=engine;self.url=url if url is not None else os.getenv('POLY_DATABASE_URL',os.getenv('DATABASE_URL',''))
        self.conn=None;self.task=None;self.owner=False;self.active=False;self.ready=False;self.last_commit=0.
        self.queue=deque();self.errors=0;self.last_error=None;self.dropped=0;self.saved=0;self.standby=None
        self.commits=0;self.closed=False;self.restored_rows=[];self.restored_model=None
        self.engine.storage=self
        for lane in engine.engines.values():lane.operational_guard=self.entry_allowed
    def status(self):
        age=time.time()-self.last_commit if self.last_commit else None
        return {'configured':bool(self.url),'backend':'postgresql' if self.url else 'local_only',
                'state':('local_only' if not self.url else 'healthy' if self.entry_allowed() else 'standby' if self.ready and not self.owner else 'recovering'),
                'writer':self.owner,'active':self.active,'checkpointAt':self.last_commit or None,'checkpointAgeSec':age,
                'queuedEvents':len(self.queue),'lastFlushSec':round(getattr(self,'last_flush_sec',0.) or 0.,3),'eventsSavedThisBoot':self.saved,'errors':self.errors,
                'lastError':self.last_error,'droppedEvents':self.dropped,
                'databaseExpiresAt':os.getenv('POLY_DATABASE_EXPIRES_AT') or None,
                'coverage':'Paper checkpoints, research labels, model artifact, attribution, sampled decisions and EdgeLab learning (calibration, strategy record, wallet study); raw feed spool stays local.',
                'checkpointTargetSec':2,'rawFeedDurable':False}
    def entry_allowed(self):
        return not self.url or bool(self.owner and self.active and self.ready and not self.dropped and time.time()-self.last_commit<HEALTH_WINDOW_SEC)
    def enqueue(self,event):
        if not self.url or event.get('kind') not in KINDS:return
        if len(self.queue)>=20000:self.dropped+=1;return
        # Serialized immediately: later engine mutations cannot change evidence.
        self.queue.append((event['bootId']+':'+str(event['journalSeq']),event['t'],event['asset'],event['kind'],json.dumps(event,separators=(',',':'),allow_nan=False)))
    def attach(self):
        for lane in self.engine.engines.values():lane.diagnostics.on_event=self.enqueue
        if self.engine.research:self.engine.research.journal.on_event=self.enqueue
    def snapshot(self,asset='BTC',include_assets=False):
        if self.active or not self.standby:return self.engine.snapshot(asset,include_assets=include_assets)
        result=copy.deepcopy(self.standby.get('assets',{}).get(asset,{}))
        result.update(portfolio=copy.deepcopy(self.standby.get('portfolio',{})),infrastructure=self.status(),status='handover',revision='16.1-pair-shadow')
        if include_assets:result['assets']=copy.deepcopy(self.standby.get('assets',{}))
        return result
    async def start(self):
        if not self.url:
            await self.engine.start();self.engine.start_research();self.active=True;return
        self.task=asyncio.create_task(self.worker(),name='v161-durable-owner')
    async def connect(self):
        import asyncpg
        self.step='connect';self.conn=await asyncpg.connect(self.url,timeout=15,command_timeout=8)
        await self.conn.execute(SCHEMA,timeout=30)
        self.ready=True
    async def load(self):
        self.step='standby-load';raw=await self.conn.fetchval("SELECT payload FROM poly_v161_state WHERE name='paper'",timeout=30)
        return json.loads(raw) if isinstance(raw,str) else raw
    async def promote(self):
        """Read everything durable first (generous timeouts), then start trading once.
        Starting engines before the heavy reads meant a slow read could time out,
        suspend freshly started engines and repeat the whole start-up."""
        T=60;self.step='promote'
        row=await self.conn.fetchval("SELECT payload FROM poly_v161_state WHERE name='paper'",timeout=T)
        recovery=json.loads(row) if isinstance(row,str) else row
        edge_raw=await self.conn.fetchval("SELECT payload FROM poly_v161_state WHERE name='edge'",timeout=T)
        labels=await self.conn.fetch("SELECT payload FROM poly_v161_events WHERE kind='SHADOW_OUTCOME' ORDER BY t DESC LIMIT 12000",timeout=T)
        model_raw=await self.conn.fetchval("SELECT payload FROM poly_v161_state WHERE name='model'",timeout=T)
        records=await self.conn.fetch("SELECT payload FROM poly_v161_events WHERE kind='TRADE_ATTRIBUTION' ORDER BY t DESC LIMIT 5000",timeout=T)
        self.engine.durable_recovery=recovery
        edge=getattr(self.engine,'edge',None)
        if edge is not None and edge_raw:edge.restore(json.loads(edge_raw) if isinstance(edge_raw,str) else edge_raw)
        if hasattr(self.engine,'import_edge_archive'):self.engine.import_edge_archive()  # DB row may predate the archive
        if getattr(self.engine,'started',False) and hasattr(self.engine,'_seed_trade_record'):self.engine._seed_trade_record()  # a retried promote must not leave a stale scorecard
        bootstrap=[]
        encoded=os.getenv('POLY_RESEARCH_BOOTSTRAP_B64','') or ''.join(os.getenv('POLY_RESEARCH_BOOTSTRAP_'+str(i),'') for i in range(1,9))
        if not self.engine.durable_recovery and encoded:
            imported=json.loads(gzip.decompress(base64.b64decode(encoded,validate=True)))
            bootstrap=imported.get('rows',[])
        # The DB is the release-independent authority; bootstrap from V16 only once.
        await self.engine.start()
        self.engine.start_research()
        self.attach()  # Recovery cancellations and bounds also need durable evidence.
        self.engine.research.restore_pairs((self.engine.durable_recovery or {}).get('pairResearchCheckpoint'))
        rows=[(json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload'])['row'] for r in reversed(labels)]
        known={r['id'] for r in self.engine.research.rows}
        self.engine.research.rows.extend(r for r in rows if r['id'] not in known)
        if model_raw:
            from poly_features import SCHEMA as fs,POLICY,NAMES
            model=json.loads(model_raw) if isinstance(model_raw,str) else model_raw
            if model.get('report',{}).get('schema')==fs and model.get('report',{}).get('policy')==POLICY and model.get('featureNames')==list(NAMES):
                self.engine.research.artifact=model;self.engine.research.training=model['report']
        for r in reversed(records):self.engine.attribution.add((json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload'])['trade'])
        self.active=True;self.attach();await self.flush()
        for r in bootstrap:
            self.enqueue({'bootId':'v16-import','journalSeq':r['id'],'t':r['labelAt'],'asset':r['asset'],'kind':'SHADOW_OUTCOME','row':r})
        known={r['id'] for r in self.engine.research.rows}
        self.engine.research.rows.extend(r for r in bootstrap if r['id'] not in known)
        if bootstrap:await self.flush()
    async def flush(self):
        if not self.owner or not self.conn:return
        self.step='flush';batch=list(self.queue);now=time.time()
        snap=checkpoint(self.engine.snapshot(include_assets=True))
        research=self.engine.research
        if research and hasattr(research,'pair_checkpoint'):snap['pairResearchCheckpoint']=research.pair_checkpoint()
        async with self.conn.transaction():
            if batch:await self.conn.executemany('INSERT INTO poly_v161_events(id,t,asset,kind,payload) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(id) DO NOTHING',batch,timeout=30)
            await self.conn.execute("INSERT INTO poly_v161_state VALUES('paper',$1,$2::jsonb) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",now,json.dumps(snap,separators=(',',':'),allow_nan=False),timeout=30)
            artifact=self.engine.research.artifact if self.engine.research else None
            # The model artifact changes only when the research model retrains; rewriting it every
            # 2s checkpoint made commits slow enough to trip the 10s health window and pause entries.
            if artifact is not None and artifact is getattr(self,'_saved_artifact',None):artifact=None
            if artifact:await self.conn.execute("INSERT INTO poly_v161_state VALUES('model',$1,$2::jsonb) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",now,json.dumps(artifact,separators=(',',':'),allow_nan=False),timeout=30)
            edge=getattr(self.engine,'edge',None)
            if edge is not None and self.commits%15==0:
                await self.conn.execute("INSERT INTO poly_v161_state VALUES('edge',$1,$2::jsonb) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",now,json.dumps(edge.export(),separators=(',',':'),allow_nan=False),timeout=30)
            if self.commits and self.commits%300==0:
                for kind,limit in (('SHADOW_OUTCOME',60000),('DECISION_SAMPLE',10000),('PAIR_OUTCOME',20000),('PAIR_QUOTE',20000),('PAIR_CANCEL',20000),('PAIR_FIRST_FILL',20000),('PAIR_HEDGE_FILL',20000),('PAIR_EXIT_REQUEST',20000)):
                    await self.conn.execute('DELETE FROM poly_v161_events WHERE kind=$1 AND t < (SELECT t FROM poly_v161_events WHERE kind=$1 ORDER BY t DESC OFFSET $2 LIMIT 1)',kind,limit,timeout=60)
        for _ in batch:self.queue.popleft()
        if artifact:self._saved_artifact=artifact
        self.saved+=len(batch);self.last_commit=now;self.last_error=None;self.commits+=1
        self.last_flush_sec=time.time()-now
    async def worker(self):
        while not self.closed:
            try:
                if self.conn is None or self.conn.is_closed():
                    if self.active:
                        # A lost owner connection releases its advisory lock. Stop all
                        # old workers before another process is allowed to resume.
                        await self.engine.suspend();self.active=False
                    self.owner=False;self.ready=False;await self.connect()
                if not self.owner:
                    self.step='lock';self.owner=await self.conn.fetchval('SELECT pg_try_advisory_lock($1)',LOCK_ID,timeout=15)
                    if self.owner:await self.promote()
                    else:self.standby=await self.load()
                elif self.active:await self.flush()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.errors+=1;self.ready=False;self.last_error=type(exc).__name__
                # Never print a connection exception containing the database URL.
                print('POLY_STORAGE_ERROR',self.last_error,'step='+str(getattr(self,'step','?')),flush=True)
                if self.active:
                    await self.engine.suspend();self.active=False
                self.owner=False
                if self.conn:
                    try:await self.conn.close(timeout=2)
                    except Exception:pass
                self.conn=None
            await asyncio.sleep(2)
    async def close(self):
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.active:await self.engine.shutdown();self.active=False
        if self.owner and self.conn and not self.conn.is_closed():
            try:await self.flush()
            except Exception:pass
        if self.conn:await self.conn.close(timeout=3)
        self.owner=False
