"""Forward-only research probes. Never mutate paper balances or submit orders."""
import asyncio,copy,json,sys,time,uuid,sqlite3
from collections import Counter,deque
from pathlib import Path
from poly_diagnostics import DiagnosticLog
from poly_external import ExchangeFeeds
from poly_features import observe,walk,SCHEMA,POLICY,NAMES
from poly_research_model import infer
from poly_semantic import SemanticResearch
from poly_pair_shadow import PairShadow,POLICY as PAIR_POLICY

import os as _os
# The research model is shadow-only (never trades); retraining every 3 min in a full-priority child
# process was the main CPU load on the 0.1-CPU instance. Retrain every 30 min, at low priority.
TRAIN_INTERVAL_SEC=float(_os.getenv('POLY_TRAIN_INTERVAL_SEC','1800'))
RESEARCH_STEP_SEC=0.5
def _low_priority():
    try:_os.nice(15)
    except Exception:pass

class ShadowLane:
    def __init__(self,asset,emit,artifact=lambda:None):
        self.asset=asset;self.emit=emit;self.artifact=artifact;self.pending=[];self.last_seed={};self.counts=Counter();self.last={};self.reason='warming'
    def finish(self,s,now,status,net,reason,fill=None):
        row={k:copy.deepcopy(s['row'][k]) for k in ('schema','policy','asset','side','marketSlug','roundStart','featuresAt','availableAt','receivedAt','sourceEventAt','features','quantity')}
        row.update(id=s['id'],labelAt=now,labelStatus=status,entryFilled='entryAt' in s,entryAt=s.get('entryAt'),entryCostPerShare=s.get('entryCost'),
            exitFill=fill,netReturnPerShare=net,exitReason=reason,maxObservedGapSec=s.get('maxGap',0),horizonSec=30,executionEnabled=False)
        self.counts[status]+=1;self.counts['completed']+=1;self.emit('SHADOW_OUTCOME',row)
        return row
    def step(self,observations,now):
        completed=[];keep=[]
        for s in self.pending:
            o=observations.get(s['row']['side'])
            if o and o['marketSlug']!=s['row']['marketSlug']:o=None
            gap=now-s['lastSeenAt'];s['maxGap']=max(s['maxGap'],gap)
            if o:s['lastSeenAt']=now
            if now>=s['row']['marketEnd'] or gap>2:
                if 'entryAt' in s:completed.append(self.finish(s,now,'censored_loss',-s['entryCost'],'data gap or rollover; full entry-cost loss'))
                else:
                    self.counts['invalidBeforeEntry']+=1;self.emit('SHADOW_REJECT',{'id':s['id'],'asset':self.asset,'at':now,'reason':'gap before entry'})
                continue
            if not o:keep.append(s);continue
            if s['phase']=='entry':
                age=now-s['seedAt']
                if age<.35:keep.append(s);continue
                if age>1:
                    self.counts['invalidBeforeEntry']+=1;self.emit('SHADOW_REJECT',{'id':s['id'],'asset':self.asset,'at':now,'reason':'entry observation deadline'});continue
                fill=walk(o['depth'].get('asks',[]),o['quantity'],True,s['row']['ask']+s['row']['tick'])
                if not fill:completed.append(self.finish(s,now,'no_fill',0.,'one-tick entry limit or full depth unavailable'));continue
                s.update(phase='open',entryAt=now,entryCost=fill['vwap']+fill['feePerShare']);self.counts['entered']+=1
                self.emit('SHADOW_ENTRY',{'id':s['id'],'asset':self.asset,'at':now,'fill':fill,'featureAvailableAt':s['row']['availableAt']})
            elif s['phase']=='open':
                fill=walk(o['depth'].get('bids',[]),o['quantity'],False)
                net=(fill['vwap']-fill['feePerShare']-s['entryCost'])*o['quantity'] if fill else None
                age=now-s['entryAt'];reason=None
                if age>=30:reason='maximum hold'
                if o['marketEnd']-now<=15:reason='market closing'
                if net is not None:
                    if net<=-s['row']['stopLossUsd']:reason='net loss stop'
                    elif net>=s['row']['targetUsd']:reason='net profit target'
                    elif age>=5 and o['features']['model_present'] and o['features']['lead_bps']<-.2:reason='oracle reversal'
                if reason:s.update(phase='exit',exitRequestedAt=now,attemptAt=now,minBid=max(.001,o['bid']-o['tick']),exitReason=reason)
            elif s['phase']=='exit':
                if now-s['exitRequestedAt']>3:completed.append(self.finish(s,now,'censored_loss',-s['entryCost'],'exit not executable within three seconds'));continue
                if now-s['attemptAt']<.35:keep.append(s);continue
                fill=walk(o['depth'].get('bids',[]),o['quantity'],False,s['minBid'])
                if fill:completed.append(self.finish(s,now,'filled',fill['vwap']-fill['feePerShare']-s['entryCost'],s['exitReason'],fill));continue
                self.counts['exitRetries']+=1;s.update(attemptAt=now,minBid=max(.001,o['bid']-o['tick']))
            keep.append(s)
        self.pending=keep;self.last={}
        for side,o in observations.items():
            prediction=infer(self.artifact(),o,now)
            self.last[side]={'at':now,'marketSlug':o['marketSlug'],'sourceEventAt':o['sourceEventAt'],'receivedAt':o['receivedAt'],'features':o['features'],'prediction':prediction}
            key=(o['marketSlug'],side)
            if o['marketEnd']-now<40 or now-self.last_seed.get(key,-1e9)<10:continue
            self.last_seed[key]=now;s={'id':uuid.uuid4().hex,'row':copy.deepcopy(o),'phase':'entry','seedAt':now,'lastSeenAt':now,'maxGap':0.}
            self.pending.append(s);self.counts['seeded']+=1;self.emit('SHADOW_SAMPLE',{'id':s['id'],'observation':o,'prediction':prediction})
        active={o['marketSlug'] for o in observations.values()}
        if active:self.last_seed={k:v for k,v in self.last_seed.items() if k[0] in active}
        return completed
    def snapshot(self):
        return {'mode':'SHADOW_ONLY','executionEnabled':False,'horizonSec':30,'policy':POLICY,'semanticWeight':0.,'reason':self.reason,
            'counts':dict(self.counts),'pendingProbes':len(self.pending),'sides':self.last,'note':'Overlapping hypothetical samples, not funded portfolio P&L.'}

class ResearchHub:
    def __init__(self,engines,path='/tmp/poly_v16_research.sqlite3'):
        self.engines=engines;self.journal=DiagnosticLog(path,'RESEARCH');self.journal.MAX_CRITICAL_ROWS=8000;self.external=ExchangeFeeds();self.semantic=SemanticResearch(engines,self.journal)
        self.rows=deque(maxlen=12000);self.artifact=None;self.training={'state':'collecting_data','executionEnabled':False};self.errors=0;self.last_step=0.;self.tasks=[]
        self.lanes={a:ShadowLane(a,self.emit,lambda:self.artifact) for a in engines};self.child=None;self.directory=Path(path).parent
        self.pairs={a:PairShadow(a,e.balance,self.emit) for a,e in engines.items()};self.pair_errors=Counter()
    on_pair_outcome=None
    def emit(self,kind,row):
        self.journal.append(kind,row=row)
        if kind=='SHADOW_OUTCOME':self.rows.append(row)
        if kind=='PAIR_OUTCOME' and self.on_pair_outcome:
            try:self.on_pair_outcome(row)
            except Exception:self.errors+=1
    @staticmethod
    def context(e):
        depth={s:{key:[{'price':p,'size':q} for p,q in sorted(levels.items(),reverse=key=='bids')[:8]] for key,levels in book.items()} for s,book in e._book_levels.items()}
        return {'market':e.market,'bbo':e.bbo,'depth':depth,'bookObservedAt':e.book_observed,'bookEventLagSec':e.book_event_lag,
            'spotTs':e.spot_ts,'twapTs':e.twap_ts,'feed':e.feed,'model':e._model() if e.open_trusted else None,'tradePrints':list(e.trade_prints),'stopLossUsd':e.inventory_stop}
    def step(self,now):
        for asset,e in self.engines.items():
            context=self.context(e)
            ext=self.external.data.get(asset,{})
            context['fast']={p:(r.get('return5Bps') if r and 0<=now-float(r.get('eventAt') or -1e9)<=5 else None) for p,r in ext.items()}
            obs,reason=observe(asset,context,ext,now)
            lane=self.lanes[asset];lane.reason=reason;lane.step(obs,now)
            if not self.pair_errors[asset]:
                try:self.pairs[asset].step(context,now)
                except Exception as exc:
                    self.pair_errors[asset]+=1;self.emit('PAIR_ERROR',{'asset':asset,'at':now,'error':type(exc).__name__,'executionEnabled':False})
            e.research_state={**lane.snapshot(),'pairShadow':self.pairs[asset].snapshot()}
        self.last_step=now
    def _load_history(self):
        with sqlite3.connect(self.journal.path,timeout=1) as db:
            payloads=db.execute("SELECT payload FROM events WHERE payload LIKE '%\"kind\":\"SHADOW_OUTCOME\"%' ORDER BY seq DESC LIMIT 12000").fetchall()
        rows=[json.loads(p[0])['row'] for p in reversed(payloads)]
        path=self.directory/'poly_v16_shadow_model.json';artifact=None
        if path.exists() and path.stat().st_size<2_000_000:
            candidate=json.loads(path.read_text())
            if candidate.get('model') and candidate.get('report',{}).get('schema')==SCHEMA and candidate.get('report',{}).get('policy')==POLICY and candidate.get('featureNames')==list(NAMES):artifact=candidate
        return rows,artifact
    async def worker(self):
        try:
            rows,artifact=await asyncio.to_thread(self._load_history)
            known={r['id'] for r in self.rows};self.rows.extend(r for r in rows if r['id'] not in known)
            if artifact and not self.artifact:self.artifact=artifact
            if self.artifact:self.training=self.artifact['report']
        except Exception as exc:self.errors+=1;self.journal.append('RESEARCH_RESTORE_ERROR',error=type(exc).__name__)
        while True:
            try:self.step(time.time())
            except asyncio.CancelledError:raise
            except Exception as exc:self.errors+=1;self.journal.append('RESEARCH_ERROR',error=type(exc).__name__)
            await asyncio.sleep(RESEARCH_STEP_SEC)
    async def training_cycle(self):
        rows=list(self.rows)
        if len(rows)<120:
            self.training={'state':'collecting_data','samples':len(rows),'executionEnabled':False,'reason':'Need 120 labelled samples across at least five complete UTC rounds.'};return
        source=self.directory/'poly_v16_train_input.json';target=self.directory/'poly_v16_shadow_model.json'
        await asyncio.to_thread(source.write_text,json.dumps(rows,separators=(',',':')))
        # Low priority: on the 0.1-CPU host a full-priority trainer starved the trading loop, so order-book
        # messages were handled late, judged stale, and the feeds reconnected in a loop.
        self.child=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name('poly_research_train.py')),str(source),'--output',str(target),
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,preexec_fn=_low_priority)
        try:
            await asyncio.wait_for(self.child.wait(),60)
            if self.child.returncode:raise RuntimeError('trainer failed')
            artifact=json.loads(await asyncio.to_thread(target.read_text));self.training=artifact['report']
            if artifact.get('model'):self.artifact=artifact
            self.journal.append('MODEL_EVALUATION',report=self.training)
        finally:
            if self.child and self.child.returncode is None:self.child.kill();await self.child.wait()
            self.child=None
    async def trainer(self):
        while True:
            try:await self.training_cycle()
            except asyncio.CancelledError:raise
            except Exception as exc:self.training={'state':'training_error','reason':type(exc).__name__,'executionEnabled':False}
            await asyncio.sleep(TRAIN_INTERVAL_SEC)
    def start(self):
        if self.tasks:return
        self.external.start();self.tasks=[asyncio.create_task(self.worker(),name='v16-shadow'),asyncio.create_task(self.semantic.worker(),name='v16-semantic'),asyncio.create_task(self.trainer(),name='v16-trainer')]
    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True);self.tasks=[];await self.external.close();self.journal.close()
    def pair_checkpoint(self):
        return {a:p.export_state() for a,p in self.pairs.items()}
    def restore_pairs(self,state):
        for a,p in self.pairs.items():p.restore((state or {}).get(a),time.time())
    def pair_snapshot(self):
        assets={a:p.snapshot() for a,p in self.pairs.items()}
        for a in assets:
            if self.pair_errors[a]:assets[a].update(reason='shadow lane quarantined after error',state='error')
        return {'policy':PAIR_POLICY,'mode':'SHADOW_ONLY','executionEnabled':False,'assets':assets,
            'errors':dict(self.pair_errors),'conservativePnlUsd':sum(p.realized for p in self.pairs.values()),
            'observedProxyPnlUsd':sum(p.observed for p in self.pairs.values()),'censoredLossBoundUsd':sum(p.censored for p in self.pairs.values()),
            'unhedgedCostUsd':sum(p.active.get('entryCost',0.) for p in self.pairs.values() if p.active),
            'comparison':'Non-overlapping virtual inventory strategy; no contribution to funded paper equity.'}
    def quality(self):
        counts=Counter(r.get('labelStatus','unknown') for r in self.rows)
        return {'retainedLabelStatusCounts':dict(counts),'censoredFraction':counts['censored_loss']/max(1,len(self.rows)),
            'note':'Censored labels use full entry-cost loss bounds; they are not observed exit losses. They remain in conservative training targets.'}
    def snapshot(self):
        return {'version':'16.1','schema':SCHEMA,'policy':POLICY,'mode':'SHADOW_ONLY','executionEnabled':False,'lastStepAt':self.last_step,
            'sampleAgeSec':time.time()-self.last_step if self.last_step else None,'errors':self.errors,'labelledSamples':len(self.rows),
            'independentRounds':len({r['roundStart'] for r in self.rows}),'training':self.training,'externalFeeds':self.external.snapshot(),
            'semantic':self.semantic.snapshot(),'labelQuality':self.quality(),'pairShadow':self.pair_snapshot(),'assets':{a:lane.snapshot() for a,lane in self.lanes.items()},'journal':self.journal.status()}
