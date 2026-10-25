"""Trade tape: score EVERY Polymarket trade print in real time against our model (shadow, record-only).

For each trade on the Up or Down token of a live 5/15-minute market we record, at that instant:
  * the trade (token, price, size, aggressor side),
  * our model's probability for that token (EdgeLab calibrated P(up), with its standard error),
  * the decision a buyer following our model would take at that print price:
      BUY   the token: conservative chance (p - z*se) beats price + taker fee + buffer
      SELL  the token (= buy the other side): the other token's conservative chance beats 1 - price + fee + buffer
      SKIP  otherwise (reason recorded), and SKIP when the model is stale or absent,
  * what the momentum strategy was doing at that moment.
When the round resolves every row is labelled (token won or lost), and we score:
  * Brier/log-loss of our model vs the traded price itself (the market's probability at that print),
  * the per-$1 result of every BUY/SELL decision held to settlement after the entry fee.
Rows in one round share one outcome, so they are NOT independent: uncertainty is reported by
resampling whole rounds (block bootstrap), and 'rounds' is the real sample size.

Storage: labelled rows go to hourly gzip JSON-lines files under POLY_DATA_DIR/tape and are uploaded to
S3 (polybot/tape/...) when POLY_BACKUP_S3_BUCKET is set; memory keeps only running aggregates and the
last rows for the dashboard. Never places, changes or cancels an order. Off with POLY_TAPE=0.
"""
from __future__ import annotations
import asyncio,gzip,json,math,os,time
from collections import defaultdict,deque

FEE_RATE=0.07;EDGE_BUFFER=0.02;Z=1.0;MODEL_MAX_AGE_SEC=10.0;MAX_PENDING_PER_ROUND=20000
CURRENT=None

def fee(p):return FEE_RATE*p*(1-p)

def decide(p_token,se,price,model_age):
    """(action, conservative edge, reason) for a buyer at `price` of a token whose model chance is p_token."""
    if p_token is None or se is None:return 'SKIP',None,'model absent'
    if model_age is None or model_age>MODEL_MAX_AGE_SEC:return 'SKIP',None,'model stale'
    if not 0.01<price<0.99:return 'SKIP',None,'price outside 1-99c'
    lo=p_token-Z*se;hi=p_token+Z*se
    buy=lo-price-fee(price)-EDGE_BUFFER
    other=1-price;sell=(1-hi)-other-fee(other)-EDGE_BUFFER
    if buy>0 and buy>=sell:return 'BUY',round(buy,5),'model chance above price after fee'
    if sell>0:return 'SELL',round(sell,5),'model chance below price after fee (buy the other side)'
    return 'SKIP',round(max(buy,sell),5),'no edge after fee and uncertainty'

class Agg:
    def __init__(self):
        self.n=0;self.rounds=set();self.brier_m=0.;self.brier_p=0.;self.ll_m=0.;self.ll_p=0.
        self.act=defaultdict(lambda:{'n':0,'won':0,'ret':0.});self.round_ret=defaultdict(float);self.round_n=defaultdict(int)
    def add(self,row):
        y=1. if row['won'] else 0.;p=row['pToken'];q=row['price'];self.n+=1;self.rounds.add(row['slug'])
        if p is not None:
            self.brier_m+=(p-y)**2;self.ll_m+=-math.log(max(1e-6,p if y else 1-p))
        self.brier_p+=(q-y)**2;self.ll_p+=-math.log(max(1e-6,q if y else 1-q))
        a=row['action'];r=row.get('returnPerDollar')
        st=self.act[a];st['n']+=1
        if r is not None:
            st['ret']+=r;st['won']+=int(r>0);self.round_ret[row['slug']]+=r;self.round_n[row['slug']]+=1
    def summary(self,with_model):
        out={'labelledPrints':self.n,'rounds':len(self.rounds),
             'brierModel':round(self.brier_m/with_model,5) if with_model else None,'brierPrice':round(self.brier_p/self.n,5) if self.n else None,
             'logLossModel':round(self.ll_m/with_model,5) if with_model else None,'logLossPrice':round(self.ll_p/self.n,5) if self.n else None,
             'actions':{k:{'prints':v['n'],'winPct':round(100*v['won']/v['n'],1) if v['n'] and k!='SKIP' else None,
                           'avgReturnPerDollar':round(v['ret']/v['n'],4) if v['n'] and k!='SKIP' else None} for k,v in self.act.items()}}
        if with_model and self.n:out['modelBeatsPrice']=out['brierModel']<out['brierPrice']
        return out

class Tape:
    def __init__(self,root=None,s3=None,now=time.time):
        self.root=root or os.path.join(os.getenv('POLY_DATA_DIR','/tmp'),'tape');self.s3=s3;self.now=now
        self.pending=defaultdict(list)          # slug -> rows awaiting the round's outcome
        self.agg=Agg();self.with_model=0;self.recent=deque(maxlen=400);self.lanes=defaultdict(Agg);self.lane_model=defaultdict(int)
        self.seen=0;self.dropped=0;self.files_uploaded=0;self.last_error=None;self.started=now()
    # ---- recording ------------------------------------------------------------------------------
    def on_print(self,lane,pr,market,signal,momentum=None):
        """pr: lane trade print {'t','side':'up'|'down','price','size','aggressor','marketSlug'}; signal: EdgeLab.signals[lane]."""
        slug=pr.get('marketSlug') or (market or {}).get('slug')
        if not slug:return None
        if len(self.pending[slug])>=MAX_PENDING_PER_ROUND:self.dropped+=1;return None
        t=float(pr.get('t') or pr.get('receivedAt') or self.now());price=float(pr['price'])
        pu=(signal or {}).get('pUp');se=(signal or {}).get('se')
        age=(t-float(signal['at'])) if signal and signal.get('at') else None
        p_tok=None if pu is None else (pu if pr['side']=='up' else 1-pu)
        action,edge,why=decide(p_tok,se,price,age)
        row={'t':round(t,3),'lane':lane,'slug':slug,'roundEnd':(market or {}).get('endTs'),'token':pr['side'].upper(),'price':price,
             'size':float(pr.get('size') or 0),'aggressor':pr.get('aggressor'),'pToken':None if p_tok is None else round(p_tok,5),
             'se':None if se is None else round(float(se),5),'modelAgeSec':None if age is None else round(age,2),
             'action':action,'edge':edge,'reason':why,'momentum':(momentum or '')[:60]}
        self.pending[slug].append(row);self.seen+=1
        return row
    def settle(self,slug,winner):
        rows=self.pending.pop(slug,[])
        if not rows or winner not in ('up','down'):return 0
        for r in rows:
            r['winner']=winner.upper();r['won']=r['token']==r['winner']
            if r['action']=='BUY':c=r['price']+fee(r['price']);r['returnPerDollar']=round(((1. if r['won'] else 0.)-c)/c,5)
            elif r['action']=='SELL':
                o=1-r['price'];c=o+fee(o);r['returnPerDollar']=round(((0. if r['won'] else 1.)-c)/c,5)
            else:r['returnPerDollar']=None
            self.agg.add(r);self.lanes[r['lane']].add(r)
            if r['pToken'] is not None:self.with_model+=1;self.lane_model[r['lane']]+=1
            self.recent.append(r)
        try:self._write(rows)
        except Exception as exc:self.last_error=f'write: {type(exc).__name__}'
        return len(rows)
    def expire(self,max_age=3*3600):
        """Rounds whose outcome never arrived are dropped (not labelled) after max_age."""
        now=self.now()
        for slug in [s for s,rows in self.pending.items() if rows and now-rows[-1]['t']>max_age]:
            self.dropped+=len(self.pending.pop(slug))
    # ---- files ----------------------------------------------------------------------------------
    def _path(self,t):
        return os.path.join(self.root,time.strftime('%Y-%m-%d',time.gmtime(t)),time.strftime('%H',time.gmtime(t))+'.jsonl.gz')
    def _write(self,rows):
        by={}
        for r in rows:by.setdefault(self._path(r['t']),[]).append(r)
        for path,rs in by.items():
            os.makedirs(os.path.dirname(path),exist_ok=True)
            with gzip.open(path,'at') as f:
                for r in rs:f.write(json.dumps(r,separators=(',',':'))+'\n')
    async def upload_closed_hours(self):
        """Upload hour files older than 2 h (rounds labelled late append to their hour) and delete local copies."""
        if self.s3 is None or not os.path.isdir(self.root):return 0
        cutoff=self.now()-2*3600;n=0
        for day in sorted(os.listdir(self.root)):
            d=os.path.join(self.root,day)
            if not os.path.isdir(d):continue
            for name in sorted(os.listdir(d)):
                path=os.path.join(d,name)
                try:hour_t=time.mktime(time.strptime(day+' '+name[:2],'%Y-%m-%d %H'))-time.timezone
                except ValueError:continue
                if hour_t+3600>cutoff:continue
                import hashlib
                sha=hashlib.sha256(open(path,'rb').read()).hexdigest()
                await self.s3.put(path,f'polybot/tape/{day}/{name}',sha);os.remove(path);n+=1
            try:os.rmdir(d)
            except OSError:pass
        self.files_uploaded+=n;return n
    # ---- report ---------------------------------------------------------------------------------
    def report(self):
        import poly_audit
        rows=[r for r in self.recent if r.get('returnPerDollar') is not None]
        blk=poly_audit.block_bootstrap_mean([{'entryAt':r['roundEnd'] or r['t'],'pnl':r['returnPerDollar']} for r in rows],block_sec=300) if rows else None
        return {'mode':'shadow, record-only: every market trade scored by the model; no orders',
                'seenPrints':self.seen,'pendingRounds':len(self.pending),'pendingPrints':sum(len(v) for v in self.pending.values()),
                'droppedPrints':self.dropped,'filesUploaded':self.files_uploaded,'error':self.last_error,
                'overall':self.agg.summary(self.with_model),'byLane':{k:a.summary(self.lane_model[k]) for k,a in self.lanes.items()},
                'recentDecisionsBlockBootstrap':blk,
                'rules':{'z':Z,'edgeBuffer':EDGE_BUFFER,'feeRate':FEE_RATE,'modelMaxAgeSec':MODEL_MAX_AGE_SEC},
                'note':'prints in one round share one outcome: rounds, not prints, are the independent sample size',
                'recent':[{k:r.get(k) for k in ('t','lane','token','price','pToken','action','edge','won','returnPerDollar')} for r in list(self.recent)[-12:]]}
    def compact(self):
        o=self.agg.summary(self.with_model);a=o['actions']
        return {'prints':o['labelledPrints'],'rounds':o['rounds'],'brierModel':o['brierModel'],'brierPrice':o['brierPrice'],
                'buy':[(a.get('BUY') or {}).get('prints'),(a.get('BUY') or {}).get('avgReturnPerDollar')],
                'sell':[(a.get('SELL') or {}).get('prints'),(a.get('SELL') or {}).get('avgReturnPerDollar')],
                'skip':(a.get('SKIP') or {}).get('prints'),'pending':sum(len(v) for v in self.pending.values()),'error':self.last_error}
    async def loop(self):
        global CURRENT;CURRENT=self
        while True:
            await asyncio.sleep(600)
            try:self.expire();await self.upload_closed_hours()
            except Exception as exc:self.last_error=f'upload: {type(exc).__name__}'
