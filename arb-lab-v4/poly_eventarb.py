"""Event arbitrage lanes (record-only; place no orders). From the owner's research review (27 Oct):

N1 - NegRisk baskets. In a multi-outcome negRisk event exactly one outcome resolves YES, so
   * basket:     buying 1 YES of EVERY outcome pays exactly $1. Gap if sum(YES ask + taker fee + 0.5c) < 1.
   * conversion: 1 NO of outcome k converts (NegRiskAdapter.convertPositions) into 1 YES of every other outcome.
                 Gap if sum over j != k of (YES bid_j - fee) - (NO ask_k + fee) - 0.5c per leg > 0.
   Augmented events (outcomes can be added later) and events with any inactive/closed outcome are skipped,
   because the "exactly one" guarantee does not hold for the listed outcomes. Size = the depth shown on every leg,
   capped at MAX_BASKETS. The first basket gap per event is a shadow trade, settled when the event resolves.
S1 - resolution window. After the real outcome is known, Polymarket resolves through the UMA oracle (a proposal,
   then at least a 2 h challenge window). Markets with a live proposal (umaResolutionStatus 'proposed') or past their
   scheduled end, whose leading side's ask is 90-99.5c: shadow-buy the leader at the ask + taker fee + 0.5c, $5,
   capped by depth; settle at 1, 0, or 0.5 if resolved invalid (N/A). Disputes and N/A are the risk; they are counted.
Taker fee rates by category (Polymarket maker-rebate docs): crypto 0.07, sports 0.05, politics/finance/tech/mentions
0.04, economics/culture/weather/other 0.05, geopolitics 0; fee = shares x rate x p (1 - p).
Pre-registered (REGISTERED_AT): N1 pass = >= 30 basket trades and mean return - 1.645 SE > 0;
S1 pass = >= 100 trades and mean return - 1.645 SE > 0.  Off with POLY_EVENTARB=0.
"""
from __future__ import annotations
import asyncio,datetime as dt,gzip,json,math,os,time
from collections import deque

GAMMA='https://gamma-api.polymarket.com';CLOB='https://clob.polymarket.com'
SCAN_SEC=600;STRESS=0.005;MAX_BASKETS=20.;STAKE=5.0;MIN_EVENT_VOLUME=10000.;MAX_EVENTS=60
S1_LO,S1_HI=0.90,0.995;MIN_N1=30;MIN_S1=100
RATES={'crypto':0.07,'sports':0.05,'politics':0.04,'finance':0.04,'tech':0.04,'mentions':0.04,'economics':0.05,
       'culture':0.05,'weather':0.05,'geopolitics':0.0}
REGISTERED_AT=1793092815   # 2026-10-27 09:20 UTC: forward results count only after this
CURRENT=None

def _jl(v):
    if isinstance(v,str):
        try:return json.loads(v)
        except ValueError:return None
    return v
def _ts(iso):
    try:return dt.datetime.fromisoformat(str(iso).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None
def fee_rate(ev):
    tags=[str((t or {}).get('slug') or (t or {}).get('label') or '').lower() for t in (ev or {}).get('tags') or []]
    cat=str((ev or {}).get('category') or '').lower()
    for k in ('geopolitics','crypto','sports','politics','finance','tech','mentions','economics','culture','weather'):
        if k==cat or k in tags:return RATES[k]
    return 0.05                                                          # conservative default
def fee(rate,p):return rate*p*(1-p)
def pick_slug(d,slug):
    """Gamma can answer a slug query with other markets; accept only the market whose slug matches exactly."""
    for m in (d if isinstance(d,list) else []):
        if isinstance(m,dict) and m.get('slug')==slug:return m
    return None
def top(book,side):
    lv=[(float(a['price']),float(a['size'])) for a in (book or {}).get(side) or [] if float(a.get('size') or 0)>0]
    if not lv:return None
    return min(lv) if side=='asks' else max(lv)
def stats(xs,z=1.645):
    n=len(xs)
    if not n:return {'n':0}
    m=sum(xs)/n;sd=math.sqrt(sum((x-m)**2 for x in xs)/(n-1)) if n>1 else None
    return {'n':n,'mean':round(m,5),'lower95':round(m-z*sd/math.sqrt(n),5) if sd else None,'winPct':round(100*sum(1 for x in xs if x>0)/n,1)}

def basket_gap(legs,rate):
    """legs: [(yesAsk, size)] for every outcome. Returns (cost per basket incl. fees+stress, baskets available) or None."""
    if not legs or any(l is None for l in legs):return None
    c=sum(a+fee(rate,a)+STRESS for a,_ in legs);n=min(s for _,s in legs)
    return (round(c,4),round(min(n,MAX_BASKETS),2)) if c<1 else None
def conversion_gaps(no_asks,yes_bids,rate):
    """no_asks[k]=(ask,size) of NO_k; yes_bids[j]=(bid,size). Returns [(k, profit per unit, units)]."""
    out=[]
    for k,na in enumerate(no_asks):
        if not na:continue
        others=[b for j,b in enumerate(yes_bids) if j!=k]
        if not others or any(b is None for b in others):continue
        proceeds=sum(b-fee(rate,b)-STRESS for b,_ in others);cost=na[0]+fee(rate,na[0])+STRESS
        if proceeds-cost>0:out.append((k,round(proceeds-cost,4),round(min([na[1]]+[s for _,s in others]+[MAX_BASKETS]),2)))
    return out

class EventArb:
    def __init__(self,http=None,root=None,now=time.time):
        self.http=http;self.now=now;self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_EVENTARB','1')!='0'
        self.n1_open={};self.s1_open={};self.closed=deque(maxlen=20000);self.gaps=deque(maxlen=300)
        self.state={'scans':0,'events':0,'eventsEligible':0,'skippedAugmented':0,'skippedInactive':0,'basketGaps':0,'conversionGaps':0,
                    's1Candidates':0,'fieldSeen':{'negRisk':0,'negRiskAugmented':0,'umaResolutionStatus':0},'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'eventarb_state.json.gz')
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=25,headers={'User-Agent':'poly-eventarb-study'})
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    async def books(self,tokens):
        out={}
        for i in range(0,len(tokens),100):
            chunk=tokens[i:i+100]
            try:
                r=await (await self._c()).post(CLOB+'/books',json=[{'token_id':t} for t in chunk]);r.raise_for_status()
                for b in r.json() or []:out[str(b.get('asset_id'))]=b
            except Exception as exc:self._err('books',exc)
            await asyncio.sleep(.2)
        return out
    async def events(self):
        out=[];off=0
        while off<2000:
            d=await self.get(GAMMA+'/events',{'closed':'false','active':'true','limit':200,'offset':off,'order':'volume','ascending':'false'})
            rows=d if isinstance(d,list) else []
            out+=rows;off+=len(rows)
            if len(rows)<200:break
            await asyncio.sleep(.3)
        return out
    # ---- N1 -----------------------------------------------------------------------------------------
    async def scan_n1(self,evs):
        now=self.now();cands=[]
        for ev in evs:
            for k in ('negRisk','negRiskAugmented'):
                if k in ev:self.state['fieldSeen'][k]+=1
            if not ev.get('negRisk'):continue
            ms=ev.get('markets') or []
            if len(ms)<3:continue
            try:vol=float(ev.get('volume') or 0)
            except (TypeError,ValueError):vol=0.
            if vol<MIN_EVENT_VOLUME:continue
            if ev.get('negRiskAugmented'):self.state['skippedAugmented']+=1;continue
            if any(m.get('closed') or m.get('active') is False for m in ms):self.state['skippedInactive']+=1;continue
            legs=[]
            for m in ms:
                tk=_jl(m.get('clobTokenIds')) or []
                if len(tk)!=2:legs=None;break
                legs.append((m.get('slug'),str(tk[0]),str(tk[1])))
            if legs:cands.append((ev,legs))
        cands=cands[:MAX_EVENTS];self.state['eventsEligible']=len(cands)
        toks=[t for _,legs in cands for _,y,n in legs for t in (y,n)]
        bk=await self.books(toks)
        for ev,legs in cands:
            rate=fee_rate(ev)
            ya=[top(bk.get(y),'asks') for _,y,_ in legs];yb=[top(bk.get(y),'bids') for _,y,_ in legs];na=[top(bk.get(n),'asks') for _,_,n in legs]
            g=basket_gap(ya,rate)
            if g:
                self.state['basketGaps']+=1;self.gaps.append({'t':now,'kind':'basket','event':ev.get('slug'),'outcomes':len(legs),'costPerBasket':g[0],'baskets':g[1],'feeRate':rate})
                if ev.get('slug') not in self.n1_open and g[1]>=1:
                    self.n1_open[ev['slug']]={'event':ev['slug'],'t':now,'markets':[s for s,_,_ in legs],'cost':g[0],'baskets':g[1],'feeRate':rate}
            for k,profit,units in conversion_gaps(na,yb,rate):
                self.state['conversionGaps']+=1;self.gaps.append({'t':now,'kind':'conversion','event':ev.get('slug'),'outcome':legs[k][0],'profitPerUnit':profit,'units':units,'feeRate':rate})
    # ---- S1 -----------------------------------------------------------------------------------------
    async def scan_s1(self,evs):
        now=self.now();cands=[]
        for ev in evs:
            for m in ev.get('markets') or []:
                if 'umaResolutionStatus' in m:self.state['fieldSeen']['umaResolutionStatus']+=1
                if m.get('closed') or m.get('slug') in self.s1_open:continue
                st=str(m.get('umaResolutionStatus') or '').lower();end=_ts(m.get('endDate') or ev.get('endDate'))
                past=end is not None and end<now
                if st not in ('proposed',) and not past:continue
                tk=_jl(m.get('clobTokenIds')) or [];outs=[str(o) for o in (_jl(m.get('outcomes')) or [])]
                if len(tk)!=2 or len(outs)!=2:continue
                cands.append((ev,m,st or 'past end',[str(t) for t in tk],outs))
        self.state['s1Candidates']=len(cands)
        bk=await self.books([t for _,_,_,tk,_ in cands[:300] for t in tk])
        for ev,m,st,tk,outs in cands[:300]:
            rate=fee_rate(ev)
            for i in (0,1):
                a=top(bk.get(tk[i]),'asks')
                if not a or not S1_LO<=a[0]<=S1_HI:continue
                cost=min(.999,a[0]+fee(rate,a[0])+STRESS);sh=min(STAKE/cost,a[1])
                if sh*a[0]<1:continue
                self.s1_open[m['slug']]={'slug':m['slug'],'event':ev.get('slug'),'t':now,'status':st,'side':i,'outcome':outs[i],'ask':a[0],
                                         'cost':round(cost,5),'shares':round(sh,3),'feeRate':rate,'endAt':_ts(m.get('endDate'))}
                break
    # ---- settle -------------------------------------------------------------------------------------
    async def market_result(self,slug):
        d=await self.get(GAMMA+'/markets',{'slug':slug,'closed':'true'})
        m=pick_slug(d,slug)
        if m is None and isinstance(d,list) and d:self.state['slugMismatch']=self.state.get('slugMismatch',0)+1
        if not m or not m.get('closed'):return None
        pr=[float(x) for x in (_jl(m.get('outcomePrices')) or [])]
        return pr if len(pr)==2 else None
    async def settle(self):
        for slug,t in list(self.s1_open.items())[:80]:
            try:pr=await self.market_result(slug)
            except Exception as exc:self._err('s1 result',exc);continue
            if pr is None:continue
            pay=pr[t['side']];self.s1_open.pop(slug)
            r=pay/t['cost']-1
            self.closed.append({**t,'lane':'S1','pay':pay,'ret':round(r,5),'pnl':round(t['shares']*(pay-t['cost']),4),'invalid':abs(pay-0.5)<1e-6,'checked':True})
            await asyncio.sleep(.15)
        for ev,t in list(self.n1_open.items())[:20]:
            pays=[]
            try:
                for s in t['markets']:
                    pr=await self.market_result(s)
                    if pr is None:pays=None;break
                    pays.append(pr[0]);await asyncio.sleep(.1)
            except Exception as exc:self._err('n1 result',exc);continue
            if pays is None:continue
            pay=sum(pays);self.n1_open.pop(ev)
            self.closed.append({**t,'lane':'N1','pay':round(pay,4),'ret':round(pay/t['cost']-1,5),'pnl':round(t['baskets']*(pay-t['cost']),4)})
    # ---- report -------------------------------------------------------------------------------------
    def report(self):
        n1=[c for c in self.closed if c['lane']=='N1' and c['t']>=REGISTERED_AT];s1=[c for c in self.closed if c['lane']=='S1' and c['t']>=REGISTERED_AT and c.get('checked')]
        s1_legacy=sum(1 for c in self.closed if c['lane']=='S1' and not c.get('checked'))
        a,b=stats([c['ret'] for c in n1]),stats([c['ret'] for c in s1])
        v=lambda s,mn:'collecting' if s['n']<mn else ('CONFIRMED' if (s.get('lower95') or -1)>0 else 'not confirmed')
        return {'enabled':self.enabled,**self.state,'openBaskets':len(self.n1_open),'openS1':len(self.s1_open),
                'N1':{'rule':'buy one YES of every outcome of a negRisk event when the basket costs < $1 after fees + 0.5c per leg; settle at resolution',
                      'pass':f'>= {MIN_N1} trades and mean - 1.645 SE > 0','registeredAt':REGISTERED_AT,**a,'pnlUsd':round(sum(c['pnl'] for c in n1),3),'verdict':v(a,MIN_N1)},
                'S1':{'rule':'buy the side priced 90-99.5c while a resolution proposal is live or after the scheduled end; settle at 1/0/0.5',
                      'pass':f'>= {MIN_S1} trades and mean - 1.645 SE > 0','registeredAt':REGISTERED_AT,**b,'pnlUsd':round(sum(c['pnl'] for c in s1),3),
                      'invalidResolutions':sum(1 for c in s1 if c.get('invalid')),'quarantined':s1_legacy,
                      'quarantineWhy':'settled before 2026-10-30 by a lookup that did not check the returned market was the one traded; excluded','losses':sum(1 for c in s1 if c['pay']<0.5),'verdict':v(b,MIN_S1)},
                'recentGaps':list(self.gaps)[-8:][::-1],'recentClosed':list(self.closed)[-8:][::-1]}
    def compact(self):
        r=self.report();r.pop('recentGaps',None);r.pop('recentClosed',None);return r
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'n1':self.n1_open,'s1':self.s1_open,'closed':list(self.closed),'gaps':list(self.gaps),
                                                  'counts':{k:self.state[k] for k in ('basketGaps','conversionGaps')}},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.n1_open.update(d.get('n1') or {});self.s1_open.update(d.get('s1') or {});self.closed.extend(d.get('closed') or []);self.gaps.extend(d.get('gaps') or [])
        self.state.update(d.get('counts') or {});return True
    async def loop(self,first_delay=200):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err('load',exc)
        await asyncio.sleep(first_delay)
        while True:
            self.state['scans']+=1
            try:
                evs=await self.events();self.state['events']=len(evs)
                await self.scan_n1(evs);await self.scan_s1(evs);await self.settle();self.save()
            except Exception as exc:self._err('scan',exc)
            await asyncio.sleep(SCAN_SEC)
