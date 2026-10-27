"""Event-market lane E1 (record-only; places no orders): are slower Polymarket markets (politics, sports, news,
culture...) mispriced in a way that pays, e.g. the favourite-longshot bias?

History (backfill, about BACKFILL_DAYS of closed binary Yes/No markets with volume >= MIN_VOLUME, excluding the
crypto up/down and above-$X markets that other lanes study):
  * the YES price at fixed horizons before each market's SCHEDULED end (7d, 3d, 1d, 6h; the last price at or before
    that moment, within 6 h), only while the market was still open: a trader then could not know when it would close,
    so horizons never use the actual close time (no look-ahead),
  * calibration by price decile per horizon: does a token priced p win p of the time?
  * pre-registered rule E1: at 3 days before the scheduled end, buy the side priced 80-95c (the favourite);
    cost = price + 2c (spread and slippage stress; most event markets charge no trading fee);
    result per $ = (won ? 1 : 0) / cost - 1. History is the discovery set; by week shows stability.
Forward (live, from REGISTERED_AT): every SCAN_SEC, active markets ending within 8 days are recorded when they cross
each horizon, at the real best ask for the side E1 would buy, and settled when they close.
Pass (forward): >= 100 markets and mean return - 1.645 SE > 0.  Off with POLY_EVENTS=0.
"""
from __future__ import annotations
import asyncio,datetime as dt,gzip,json,math,os,time
from collections import deque

GAMMA='https://gamma-api.polymarket.com';CLOB='https://clob.polymarket.com'
HORIZONS=((7*86400,'7d'),(3*86400,'3d'),(86400,'1d'),(6*3600,'6h'))
BACKFILL_DAYS=int(os.getenv('POLY_EVENTS_DAYS','60'));MIN_VOLUME=float(os.getenv('POLY_EVENTS_MIN_VOLUME','50000'))
FAV_LO,FAV_HI=0.80,0.95;STRESS=0.02;RULE_H='3d';MIN_FWD=100;SCAN_SEC=1800;TOL=6*3600
REGISTERED_AT=1793071725   # 2026-10-27 03:28 UTC: forward results count only after this
CURRENT=None

def _jl(v):
    if isinstance(v,str):
        try:return json.loads(v)
        except ValueError:return None
    return v
def _ts(iso):
    try:return dt.datetime.fromisoformat(str(iso).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None
def eligible(m):
    slug=str(m.get('slug') or '')
    if 'updown' in slug or '-above-' in slug or 'up-or-down' in slug:return False
    outs=[str(o).lower() for o in (_jl(m.get('outcomes')) or [])]
    toks=_jl(m.get('clobTokenIds')) or []
    try:vol=float(m.get('volumeNum') or m.get('volume') or 0)
    except (TypeError,ValueError):vol=0.
    return outs==['yes','no'] and len(toks)==2 and vol>=MIN_VOLUME and _ts(m.get('endDate')) is not None
def winner_yes(m):
    pr=[float(x) for x in (_jl(m.get('outcomePrices')) or [])]
    if len(pr)!=2 or max(pr)<.99:return None
    return pr[0]>pr[1]
def price_at(history,t):
    """Last price at or before t, if within TOL."""
    best=None
    for h in history:
        ht=float(h['t']);ht=ht/1000 if ht>1e12 else ht
        if ht<=t and (best is None or ht>best[0]):best=(ht,float(h['p']))
    return best[1] if best and t-best[0]<=TOL else None
def rule(p_yes,won_yes,cost_add=STRESS):
    """E1 on one snapshot: buy the side priced FAV_LO..FAV_HI. Returns (side, return per $) or None."""
    if p_yes is None:return None
    for side,p,w in (('YES',p_yes,won_yes),('NO',1-p_yes,not won_yes)):
        if FAV_LO<=p<=FAV_HI:
            c=min(.999,p+cost_add);return side,round((1. if w else 0.)/c-1,5)
    return None
def stats(rets,z=1.645):
    n=len(rets)
    if not n:return {'n':0}
    m=sum(rets)/n;sd=math.sqrt(sum((x-m)**2 for x in rets)/(n-1)) if n>1 else None
    return {'n':n,'mean':round(m,4),'lower95':round(m-z*sd/math.sqrt(n),4) if sd else None,'winPct':round(100*sum(1 for x in rets if x>0)/n,1)}

class Events:
    def __init__(self,http=None,root=None,now=time.time):
        self.http=http;self.now=now;self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_EVENTS','1')!='0'
        self.hist={};self.done=set();self.fwd={};self.fwd_closed=deque(maxlen=20000)
        self.state={'phase':'starting','listed':0,'studied':0,'skipped':0,'errors':0,'lastError':None,'scans':0,'dayCursor':None}
    def path(self):return os.path.join(self.root,'events_state.json.gz')
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=25,headers={'User-Agent':'poly-events-study'})
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    # ---- history -------------------------------------------------------------------------------------
    async def closed_day(self,day0):
        """Closed eligible markets whose scheduled end falls in [day0, day0 + 1 day)."""
        out=[];off=0
        while off<5000:
            d=await self.get(GAMMA+'/markets',{'closed':'true','limit':500,'offset':off,
                  'end_date_min':dt.datetime.fromtimestamp(day0,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'end_date_max':dt.datetime.fromtimestamp(day0+86400,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
            rows=d if isinstance(d,list) else (d or {}).get('data') or []
            out+=[m for m in rows if eligible(m)];off+=len(rows)
            if len(rows)<500:break
            await asyncio.sleep(.3)
        return out
    async def history(self,token,end):
        for fid in (60,720):
            d=await self.get(CLOB+'/prices-history',{'market':token,'startTs':int(end-8*86400),'endTs':int(end),'fidelity':fid})
            h=(d or {}).get('history') or []
            if h:return h
        return []
    async def study_market(self,m):
        slug=m['slug']
        if slug in self.done:return None
        w=winner_yes(m);end=_ts(m.get('endDate'))
        if w is None:self.done.add(slug);self.state['skipped']+=1;return None
        closed_at=_ts(m.get('closedTime')) or end
        h=await self.history(str((_jl(m.get('clobTokenIds')) or [None])[0]),max(end,closed_at))
        snaps={}
        for sec,name in HORIZONS:
            t=end-sec
            if closed_at is not None and closed_at<=t:continue              # already closed then: not tradeable
            p=price_at(h,t)
            if p is not None and 0<p<1:snaps[name]=round(p,4)
        rec={'slug':slug,'q':str(m.get('question') or '')[:140],'endAt':end,'closedAt':closed_at,'yesWon':w,
             'volume':round(float(m.get('volumeNum') or 0)),'p':snaps,'cat':m.get('category') or None}
        self.done.add(slug)
        if snaps:self.hist[slug]=rec;self.state['studied']+=1
        else:self.state['skipped']+=1
        return rec
    async def backfill(self):
        now=self.now();start=int((now-BACKFILL_DAYS*86400)//86400)*86400
        day=self.state.get('dayCursor') or start
        self.state['phase']='backfilling'
        while day<now-86400:
            try:ms=await self.closed_day(day);self.state['listed']+=len(ms)
            except Exception as exc:self._err('list',exc);await asyncio.sleep(10);day+=86400;continue
            for m in ms:
                if m['slug'] in self.done:continue
                try:await self.study_market(m)
                except Exception as exc:self._err('history',exc);await asyncio.sleep(2)
                await asyncio.sleep(.4)
            day+=86400;self.state['dayCursor']=day
            try:self.save()
            except Exception as exc:self._err('save',exc)
        self.state['phase']='history done; recording forward'
    # ---- forward -------------------------------------------------------------------------------------
    async def scan_forward(self):
        now=self.now();self.state['scans']+=1;off=0;rows=[]
        while off<3000:
            d=await self.get(GAMMA+'/markets',{'closed':'false','active':'true','limit':500,'offset':off,
                  'end_date_min':dt.datetime.fromtimestamp(now+3600,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'end_date_max':dt.datetime.fromtimestamp(now+8*86400,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
            page=d if isinstance(d,list) else (d or {}).get('data') or []
            rows+=page;off+=len(page)
            if len(page)<500:break
            await asyncio.sleep(.3)
        for m in rows:
            if not eligible(m):continue
            end=_ts(m.get('endDate'));tau=end-now
            try:bb=float(m.get('bestBid'));ba=float(m.get('bestAsk'))
            except (TypeError,ValueError):continue
            if not 0<bb<ba<1:continue
            for sec,name in HORIZONS:
                key=f"{m['slug']}|{name}"
                if tau<=sec and tau>sec-2*SCAN_SEC and key not in self.fwd:
                    self.fwd[key]={'slug':m['slug'],'q':str(m.get('question') or '')[:140],'endAt':end,'h':name,'t':now,
                                   'mid':round((bb+ba)/2,4),'yesAsk':ba,'noAsk':round(1-bb,4)}
    async def resolve_forward(self):
        now=self.now();slugs=sorted({v['slug'] for v in self.fwd.values() if now>v['endAt']+3600})[:80]
        for slug in slugs:
            try:
                d=await self.get(GAMMA+'/markets',{'slug':slug,'closed':'true'});m=d[0] if isinstance(d,list) and d else None
                w=winner_yes(m) if m and m.get('closed') else None
            except Exception as exc:self._err('resolve',exc);continue
            if w is None:
                for k in [k for k,v in self.fwd.items() if v['slug']==slug and now>v['endAt']+14*86400]:self.fwd.pop(k)
                continue
            for k in [k for k,v in self.fwd.items() if v['slug']==slug]:
                v=self.fwd.pop(k);v['yesWon']=w;self.fwd_closed.append(v)
            await asyncio.sleep(.2)
    # ---- report --------------------------------------------------------------------------------------
    def report(self):
        H=list(self.hist.values());calib={}
        for _,name in HORIZONS:
            rows=[(r['p'][name],r['yesWon']) for r in H if name in r['p']];dec=[]
            for b in range(10):
                v=[(1. if w else 0.)-p for p,w in rows if min(9,int(p*10))==b]
                s=stats(v,3.0) if v else {'n':0}
                dec.append({'price':f'{b*10}-{b*10+10}c','n':s['n'],'wonMinusPrice':s.get('mean'),'lower':s.get('lower95'),
                            'upper':None if s.get('lower95') is None else round(2*s['mean']-s['lower95'],4)})
            calib[name]={'markets':len(rows),'deciles':dec}
        hr=[(r['endAt'],rule(r['p'].get(RULE_H),r['yesWon'])) for r in H]
        hr=[(t,x[1]) for t,x in hr if x]
        weeks={}
        for t,x in hr:weeks.setdefault(time.strftime('%G-W%V',time.gmtime(t)),[]).append(x)
        at0=[rule(r['p'].get(RULE_H),r['yesWon'],0.)[1] for r in H if rule(r['p'].get(RULE_H),r['yesWon'],0.)]
        fw=[]
        for v in self.fwd_closed:
            if v['h']!=RULE_H or v['t']<REGISTERED_AT:continue
            fav=('YES',v['yesAsk'],v['yesWon']) if FAV_LO<=v['yesAsk']<=FAV_HI else (('NO',v['noAsk'],not v['yesWon']) if FAV_LO<=v['noAsk']<=FAV_HI else None)
            if fav:fw.append((1. if fav[2] else 0.)/min(.999,fav[1]+0.01)-1)     # real ask + 1c
        f=stats(fw)
        return {'enabled':self.enabled,**self.state,'historyMarkets':len(H),'forwardOpen':len(self.fwd),'forwardSettled':len(self.fwd_closed),
                'calibration':calib,
                'E1':{'rule':f'at {RULE_H} before the scheduled end, buy the side priced {int(FAV_LO*100)}-{int(FAV_HI*100)}c; history cost +2c, forward at the real ask +1c',
                      'pass':f'forward: >= {MIN_FWD} markets and mean - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'history':stats([x for _,x in hr]),'historyNoStress':stats(at0),
                      'historyByWeek':[{'week':w,**stats(v)} for w,v in sorted(weeks.items())],'forward':f,
                      'verdict':'collecting' if f['n']<MIN_FWD else ('CONFIRMED' if (f.get('lower95') or -1)>0 else 'not confirmed')}}
    def compact(self):
        r=self.report()
        r['calibration']={k:[[d['price'],d['n'],d['wonMinusPrice']] for d in v['deciles']] for k,v in r['calibration'].items()}
        r['E1']['historyByWeek']=r['E1']['historyByWeek'][-6:];return r
    # ---- persistence and loop ------------------------------------------------------------------------
    def save(self):
        st={'hist':list(self.hist.values()),'done':sorted(self.done),'fwd':self.fwd,'fwdClosed':list(self.fwd_closed),
            'cursor':self.state.get('dayCursor')}
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump(st,f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:st=json.load(f)
        self.hist={r['slug']:r for r in st.get('hist') or []};self.done=set(st.get('done') or []);self.fwd.update(st.get('fwd') or {})
        self.fwd_closed.extend(st.get('fwdClosed') or []);self.state['dayCursor']=st.get('cursor');self.state['studied']=len(self.hist)
        return True
    async def loop(self,first_delay=240):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err('load',exc)
        await asyncio.sleep(first_delay)
        async def fwd():
            while True:
                try:await self.scan_forward();await self.resolve_forward();self.save()
                except Exception as exc:self._err('forward',exc)
                await asyncio.sleep(SCAN_SEC)
        asyncio.create_task(fwd())
        try:await self.backfill()
        except Exception as exc:self._err('backfill',exc)
        try:self.save()
        except Exception as exc:self._err('save',exc)
