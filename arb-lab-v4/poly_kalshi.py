"""Cross-exchange lane K1 (record-only; places no orders): Polymarket vs Kalshi on the same 15-minute BTC/ETH rounds.

Kalshi KXBTC15M / KXETH15M ("price up in next 15 mins?") and Polymarket btc/eth-updown-15m cover the same quarter
hour. Every SCAN_SEC, for the live Polymarket 15m round, the Kalshi market closing at the same time is found and
both books are read:
   pair A = Polymarket UP ask   + Kalshi NO ask  (= 1 - Kalshi best YES bid)
   pair B = Polymarket DOWN ask + Kalshi YES ask (= 1 - Kalshi best NO bid)
Each pair pays exactly $1 when both venues agree on the direction. Cost = both asks + both taker fees + 1c stress
per leg. A pair under $1 is a gap; the first gap per round is recorded as a shadow trade (up to MAX_PAIRS, limited
by the size shown on both books) and settled with BOTH venues' real results.
Basis risk is measured, not assumed away: Polymarket settles on Chainlink (end vs start price), Kalshi on the
60-second CF Benchmarks BRTI average (end vs start), so the two can disagree when the move is tiny: then the pair
pays 0 or 2. The disagreement rate is reported.
Fees: Polymarket 0.07 p(1-p) per share; Kalshi ceil(0.07 n p(1-p)) cents per order of n contracts.
Pre-registered K1 (REGISTERED_AT): pass = >= 100 rounds with a gap trade and mean P&L per pair - 1.645 SE > 0.
Off with POLY_KALSHI=0.
"""
from __future__ import annotations
import asyncio,datetime as dt,gzip,json,math,os,time
from collections import deque

BASES=('https://external-api.kalshi.com/trade-api/v2','https://api.elections.kalshi.com/trade-api/v2')
SERIES={'BTC15':'KXBTC15M','ETH15':'KXETH15M'}
SCAN_SEC=5;LIST_SEC=60;STRESS=0.01;MAX_PAIRS=5;MIN_ROUNDS=100
REGISTERED_AT=1793085345   # 2026-10-27 07:15 UTC: forward results count only after this
CURRENT=None
def poly_fee(p):return 0.07*p*(1-p)
def kalshi_fee(p,n=MAX_PAIRS):return math.ceil(0.07*n*p*(1-p)*100-1e-9)/100/n
def _ts(iso):
    try:return dt.datetime.fromisoformat(str(iso).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None
def kalshi_top(ob):
    """(best YES bid, size), (best NO bid, size) in dollars from a Kalshi orderbook response."""
    o=(ob or {}).get('orderbook_fp') or (ob or {}).get('orderbook') or {}
    def best(key_d,key_c):
        rows=o.get(key_d);scale=1.
        if rows is None:rows=o.get(key_c);scale=100.
        lv=[(float(p)/scale,float(q)) for p,q in (rows or []) if float(q)>0]
        return max(lv) if lv else None
    return best('yes_dollars','yes'),best('no_dollars','no')
def gap_pairs(pu,pd,ky,kn):
    """pu/pd: Polymarket (ask, size) for UP/DOWN; ky/kn: Kalshi best (bid, size) for YES/NO.
    Returns [(name, cost per pair, pairs available, poly ask, kalshi ask)] with cost < 1."""
    out=[]
    for name,pa,kb in (('A: Poly UP + Kalshi NO',pu,ky),('B: Poly DOWN + Kalshi YES',pd,kn)):
        if not pa or not kb or pa[0] is None:continue
        ka=round(1-kb[0],4)                                  # the opposite side's ask on Kalshi
        if not 0.01<=pa[0]<=0.99 or not 0.01<=ka<=0.99:continue
        c=pa[0]+poly_fee(pa[0])+ka+kalshi_fee(ka)+2*STRESS
        if c<1:out.append((name,round(c,4),min(float(pa[1] or 0),kb[1],MAX_PAIRS),pa[0],ka))
    return out

class Kalshi:
    def __init__(self,engines=None,http=None,root=None,now=time.time):
        self.engines=engines or {};self.http=http;self.now=now;self.root=root or os.getenv('POLY_DATA_DIR','/tmp')
        self.enabled=os.getenv('POLY_KALSHI','1')!='0';self.base=None
        self.markets={};self.listed_at={};self.rounds={};self.wait={};self.done=deque(maxlen=20000);self.gaps=deque(maxlen=300)
        self.state={'scans':0,'matched':0,'gapsSeen':0,'errors':0,'lastError':None,'books':0}
    def path(self):return os.path.join(self.root,'kalshi_state.json.gz')
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=10,headers={'User-Agent':'poly-kalshi-study'})
        return self.http
    async def get(self,path,params=None):
        c=await self._c();last=None
        for b in ([self.base] if self.base else list(BASES)):
            try:
                r=await c.get(b+path,params=params);r.raise_for_status();self.base=b;return r.json()
            except Exception as exc:last=exc
        raise last
    async def list_open(self,lane):
        now=self.now()
        if now-self.listed_at.get(lane,0)<LIST_SEC and self.markets.get(lane):return
        d=await self.get('/markets',{'series_ticker':SERIES[lane],'status':'open','limit':200})
        self.markets[lane]={round(_ts(m.get('close_time'))):m.get('ticker') for m in (d or {}).get('markets') or [] if _ts(m.get('close_time'))}
        self.listed_at[lane]=now
    async def scan(self):
        self.state['scans']+=1;now=self.now()
        for lane in SERIES:
            eng=self.engines.get(lane);m=getattr(eng,'market',None) if eng else None
            if not m or not m.get('endTs') or not m.get('slug'):continue
            end=float(m['endTs']);left=(end-now)/900
            if left<0.02 or left>1:continue
            try:await self.list_open(lane)
            except Exception as exc:self._err('list',exc);continue
            tk=next((t for ce,t in (self.markets.get(lane) or {}).items() if abs(ce-end)<=5),None)
            if not tk:continue
            self.state['matched']+=1
            try:ob=await self.get(f'/markets/{tk}/orderbook');self.state['books']+=1
            except Exception as exc:self._err('orderbook',exc);continue
            ky,kn=kalshi_top(ob);bbo=getattr(eng,'bbo',{}) or {}
            pu=((bbo.get('up') or {}).get('ask'),(bbo.get('up') or {}).get('askSize'))
            pd=((bbo.get('down') or {}).get('ask'),(bbo.get('down') or {}).get('askSize'))
            r=self.rounds.setdefault(m['slug'],{'slug':m['slug'],'lane':lane,'ticker':tk,'end':end,'scans':0,'gapScans':0,'bestCost':None,'trade':None})
            r['scans']+=1
            for name,cost,pairs,pa,ka in gap_pairs(pu,pd,ky,kn):
                if pairs<1:continue
                self.state['gapsSeen']+=1;r['gapScans']+=1
                r['bestCost']=cost if r['bestCost'] is None else min(r['bestCost'],cost)
                self.gaps.append({'t':now,'lane':lane,'pair':name,'cost':cost,'pairs':pairs,'polyAsk':pa,'kalshiAsk':ka,'left':round(left,3)})
                if r['trade'] is None:r['trade']={'pair':name,'cost':cost,'pairs':pairs,'t':now,'left':round(left,3),'polyAsk':pa,'kalshiAsk':ka}
            if len(self.rounds)>200:
                for s in list(self.rounds)[:50]:
                    if s not in self.wait:self.rounds.pop(s,None)
    def settle(self,slug,winner):
        """Polymarket result (EdgeLab settle listener); Kalshi result is fetched in the loop."""
        r=self.rounds.get(slug)
        if r is None or winner not in ('up','down'):return 0
        r['polyUp']=winner=='up';self.wait[slug]=r;return 1
    async def resolve(self):
        now=self.now()
        for slug,r in list(self.wait.items()):
            if now<r['end']+120:continue
            try:d=await self.get(f"/markets/{r['ticker']}")
            except Exception as exc:self._err('result',exc);continue
            res=str(((d or {}).get('market') or {}).get('result') or '').lower()
            if res not in ('yes','no'):
                if now>r['end']+6*3600:self.wait.pop(slug,None)
                continue
            self.wait.pop(slug,None);self.rounds.pop(slug,None)
            r['kalshiUp']=res=='yes';r['agree']=r['kalshiUp']==r['polyUp']
            t=r['trade']
            if t:
                pay=(1. if r['polyUp'] else 0.)+(0. if r['kalshiUp'] else 1.) if t['pair'].startswith('A') else (0. if r['polyUp'] else 1.)+(1. if r['kalshiUp'] else 0.)
                t['payPerPair']=pay;t['pnlPerPair']=round(pay-t['cost'],4);t['pnlUsd']=round(t['pairs']*(pay-t['cost']),4)
            self.done.append(r)
    def report(self):
        rows=list(self.done);fwd=[r for r in rows if r.get('trade') and r['trade']['t']>=REGISTERED_AT]
        per=[r['trade']['pnlPerPair'] for r in fwd];n=len(per);m=sum(per)/n if n else None
        sd=math.sqrt(sum((x-m)**2 for x in per)/(n-1)) if n>1 else None;lo=round(m-1.645*sd/math.sqrt(n),4) if sd else None
        agree=[r['agree'] for r in rows if 'agree' in r]
        return {'enabled':self.enabled,'base':self.base,**self.state,'liveRounds':len(self.rounds),'awaitingResult':len(self.wait),
                'roundsSettled':len(rows),'venuesDisagreePct':round(100*(1-sum(agree)/len(agree)),2) if agree else None,
                'roundsWithGap':sum(1 for r in rows if r.get('trade')),
                'K1':{'rule':'Polymarket ask + opposite Kalshi ask + both taker fees + 1c per leg < $1; first gap per round, up to 5 pairs',
                      'pass':f'>= {MIN_ROUNDS} rounds with a gap trade and mean P&L per pair - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'trades':n,'avgPnlPerPair':None if m is None else round(m,4),'lower95':lo,'pnlUsd':round(sum(r['trade']['pnlUsd'] for r in fwd),3),
                      'verdict':'collecting' if n<MIN_ROUNDS else ('CONFIRMED' if lo is not None and lo>0 else 'not confirmed')},
                'recentGaps':list(self.gaps)[-6:][::-1]}
    def compact(self):
        r=self.report();r.pop('recentGaps',None);return r
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'done':list(self.done),'wait':self.wait,'gapsSeen':self.state['gapsSeen']},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.done.extend(d.get('done') or []);self.wait.update(d.get('wait') or {});self.state['gapsSeen']=int(d.get('gapsSeen') or 0);return True
    async def loop(self,first_delay=90):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err('load',exc)
        await asyncio.sleep(first_delay);tick=0
        while True:
            try:await self.scan()
            except Exception as exc:self._err('scan',exc)
            tick+=1
            if tick%12==0:
                try:await self.resolve();self.save()
                except Exception as exc:self._err('resolve',exc)
            await asyncio.sleep(SCAN_SEC)
