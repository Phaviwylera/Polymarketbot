"""Asset scout: a read-only agent that looks for Polymarket crypto up/down markets the bot could trade
profitably, and reports them. It NEVER places orders and never touches the trading lanes: it only reads
public market data (Gamma market list + CLOB order books) and the lanes' own closed-trade results.

What it measures for each coin x interval (BTC/ETH/SOL/XRP/DOGE/BNB/HYPE x 5m/15m):
  * listed / accepting orders, best-level depth, spread
  * round-trip friction for a 5-share momentum scalp (taker fee in and out + spread)
  * how often the Up price moves far enough in 30 s to pay that friction AND the profit target
    ("opportunity rate") - the raw material the momentum strategy feeds on
It compares every candidate with the benchmark lanes the bot already trades (BTC/ETH 5m) and flags a
CANDIDATE only after enough rounds were observed. Adding a lane stays a deliberate code change that the
hourly check makes from this report; the scout itself changes nothing.
"""
from __future__ import annotations
import asyncio,json,math,statistics,time
from collections import deque

GAMMA='https://gamma-api.polymarket.com'
CLOB='https://clob.polymarket.com'
COINS=('btc','eth','sol','xrp','doge','bnb','hype')
INTERVALS=((300,'5m'),(900,'15m'))
SAMPLE_SEC=20.0
WINDOW_SEC=30.0
SHARES=5.0
TARGET_PER_SHARE=0.06          # ~$0.30 on 5 shares, the momentum profit target
MIN_ROUNDS=6                    # rounds observed before any verdict
BENCHMARK=('btc-5m','eth-5m')
MAX_SPREAD=0.02
MIN_DEPTH=SHARES

def fee(p):return .07*p*(1-p)

def _jsonish(v):
    if isinstance(v,list):return v
    try:x=json.loads(v);return x if isinstance(x,list) else []
    except Exception:return []

def _best(book,side):
    rows=[]
    for r in (book or {}).get(side) or []:
        try:rows.append((float(r['price']),float(r.get('size') or 0)))
        except (KeyError,TypeError,ValueError):pass
    if not rows:return None,0.
    p,s=(max(rows) if side=='bids' else min(rows));return p,s

class MarketTrack:
    def __init__(self,coin,interval,tag):
        self.coin=coin;self.interval=interval;self.tag=tag;self.key=f'{coin}-{tag}'
        self.samples=deque(maxlen=2000)      # (t, roundStart, mid, spread, depth)
        self.rounds=set();self.market=None;self.listed=None;self.last_lookup=0.;self.errors=0
    def add(self,t,start,bid,ask,bsz,asz):
        mid=(bid+ask)/2;self.samples.append((t,start,mid,ask-bid,min(bsz,asz)));self.rounds.add(start)
    def metrics(self):
        s=list(self.samples)
        if len(s)<4:return {'samples':len(s),'rounds':len(self.rounds)}
        spreads=[x[3] for x in s];depth=[x[4] for x in s];mids=[x[2] for x in s]
        moves=[]
        for i,(t,start,mid,_,_) in enumerate(s):
            for k in range(i+1,len(s)):
                tk,sk,mk=s[k][0],s[k][1],s[k][2]
                if sk!=start or tk-t>WINDOW_SEC*2:break
                if tk-t>=WINDOW_SEC:moves.append(abs(mk-mid));break
        spread=statistics.median(spreads);p=min(.95,max(.05,statistics.median(mids)))
        friction=2*fee(p)+spread                       # per share, in and out as taker plus the spread
        need=friction+TARGET_PER_SHARE
        opp=sum(m>=need for m in moves)/len(moves) if moves else None
        return {'samples':len(s),'rounds':len(self.rounds),'medianSpread':round(spread,4),'medianDepth':round(statistics.median(depth),1),
                'frictionPerShare':round(friction,4),'moveNeeded':round(need,4),'median30sMove':round(statistics.median(moves),4) if moves else None,
                'opportunityRate':round(opp,4) if opp is not None else None,'windows':len(moves)}

class AssetScout:
    def __init__(self,engines,ledger=None):
        self.engines=engines;self.ledger=ledger     # ledger(): {lane: {'trades','netPnlUsd'}} since fresh start
        self.tracks={f'{c}-{tag}':MarketTrack(c,iv,tag) for c in COINS for iv,tag in INTERVALS}
        self.task=None;self.client=None;self.errors=0;self.last_error=None;self.started_at=None;self.last_report=0.
    def active_keys(self):
        return {f'{e.asset.lower()}-{e.tag}' for e in self.engines.values()}
    async def _get(self,url,params):
        import httpx
        if self.client is None:self.client=httpx.AsyncClient(timeout=8,headers={'User-Agent':'POLY-EDGE-SCOUT/1'})
        r=await self.client.get(url,params=params);r.raise_for_status();return r.json()
    async def _market(self,tr,now):
        start=int(now)//tr.interval*tr.interval
        if tr.market and tr.market['startTs']==start:return tr.market
        if now-tr.last_lookup<60 and tr.listed is False:return None
        tr.last_lookup=now
        data=await self._get(GAMMA+'/markets',{'slug':f'{tr.coin}-updown-{tr.tag}-{start}'})
        if not (isinstance(data,list) and data):tr.listed=False;tr.market=None;return None
        m=data[0];mp={str(o).strip().lower():str(t) for o,t in zip(_jsonish(m.get('outcomes')),_jsonish(m.get('clobTokenIds')))}
        up=mp.get('up') or mp.get('yes')
        if not up:tr.listed=False;return None
        tr.listed=True;tr.market={'startTs':start,'up':up,'accepting':bool(m.get('acceptingOrders')),'slug':m.get('slug')};return tr.market
    def _sample_engine(self,tr,now):
        """Benchmark lanes the bot already trades: read the engine's live book instead of polling."""
        for e in self.engines.values():
            if f'{e.asset.lower()}-{e.tag}'!=tr.key or not e.market:continue
            b=e.bbo.get('up') or {}
            if b.get('bid') is None or b.get('ask') is None or now-e.book_observed.get('up',0)>15:return
            tr.listed=True;tr.add(now,float(e.market['startTs']),float(b['bid']),float(b['ask']),float(b.get('bidSize') or 0),float(b.get('askSize') or 0));return
    async def step(self,now=None):
        now=time.time() if now is None else now;active=self.active_keys()
        for key,tr in self.tracks.items():
            try:
                if key in active:self._sample_engine(tr,now);continue
                m=await self._market(tr,now)
                if not m or not m['accepting']:continue
                elapsed=now-m['startTs']
                if elapsed<15 or elapsed>tr.interval-15:continue
                book=await self._get(CLOB+'/book',{'token_id':m['up']})
                (bid,bsz),(ask,asz)=_best(book,'bids'),_best(book,'asks')
                if bid is None or ask is None or bid>=ask:continue
                tr.add(now,float(m['startTs']),bid,ask,bsz,asz)
                await asyncio.sleep(.15)
            except asyncio.CancelledError:raise
            except Exception as exc:tr.errors+=1;self.errors+=1;self.last_error=type(exc).__name__
    def snapshot(self):
        active=self.active_keys();rows={k:{'coin':t.coin,'interval':t.tag,'listed':t.listed,'tradedNow':k in active,**t.metrics(),'errors':t.errors} for k,t in self.tracks.items()}
        bench=[rows[k]['opportunityRate'] for k in BENCHMARK if rows.get(k,{}).get('opportunityRate') is not None]
        ref=statistics.median(bench) if bench else None
        lanes=self.ledger() if self.ledger else {}
        for k,r in rows.items():
            r['vsBenchmark']=round(r['opportunityRate']/ref,2) if ref and r.get('opportunityRate') is not None else None
            if r['tradedNow']:
                lane=lanes.get(k) or {}
                r['verdict']='ACTIVE';r['tradesSinceFreshStart']=lane.get('trades');r['netSinceFreshStart']=lane.get('netPnlUsd')
            elif r['listed'] is False:r['verdict']='NOT_LISTED'
            elif (r.get('rounds') or 0)<MIN_ROUNDS or ref is None:r['verdict']='OBSERVING'
            elif r['medianSpread']>MAX_SPREAD:r['verdict']='TOO_WIDE'
            elif r['medianDepth']<MIN_DEPTH:r['verdict']='TOO_THIN'
            elif (r['vsBenchmark'] or 0)>=0.8:r['verdict']='CANDIDATE'
            else:r['verdict']='WEAK'
        cands=sorted((k for k,r in rows.items() if r['verdict']=='CANDIDATE'),key=lambda k:-(rows[k]['vsBenchmark'] or 0))
        return {'mode':'READ_ONLY','benchmarkOpportunityRate':ref,'candidates':cands,'markets':rows,'errors':self.errors,'lastError':self.last_error,
                'startedAt':self.started_at,
                'rule':f'CANDIDATE = listed, spread <= {MAX_SPREAD*100:.0f}c, depth >= {MIN_DEPTH:.0f} shares, >= {MIN_ROUNDS} rounds observed, '
                       f'30s opportunity rate >= 0.8x the BTC/ETH 5m lanes the bot already trades',
                'note':'Read-only. The scout never trades; a lane is added only by a code change after review.'}
    def compact(self):
        s=self.snapshot()
        return {'candidates':s['candidates'],'bench':s['benchmarkOpportunityRate'],
                'm':{k:[r.get('verdict'),r.get('opportunityRate'),r.get('vsBenchmark'),r.get('medianSpread'),r.get('rounds')] for k,r in s['markets'].items()}}
    async def run(self):
        self.started_at=time.time()
        while True:
            try:
                await self.step()
                if time.time()-self.last_report>=600:
                    self.last_report=time.time();print('POLY_SCOUT '+json.dumps(self.compact(),separators=(',',':')),flush=True)
            except asyncio.CancelledError:raise
            except Exception as exc:self.errors+=1;self.last_error=type(exc).__name__
            await asyncio.sleep(SAMPLE_SEC)
    def start(self):
        if self.task is None:self.task=asyncio.create_task(self.run(),name='asset-scout')
    async def close(self):
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None
        if self.client:await self.client.aclose();self.client=None
