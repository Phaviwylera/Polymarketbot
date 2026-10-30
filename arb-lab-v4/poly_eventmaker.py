"""Event-market maker M3 (record-only; places no orders): does market making work on SLOWER markets?

Our crypto 5m/15m maker tests (M1, M2) were picked off by faster informed flow. The owner's research suggests
liquid sports and politics markets instead, where information arrives more slowly. This lane quotes, on paper,
the most active binary sports/politics markets (24 h volume >= MIN_VOL24, ending >= 2 days out, spread 1-6c):
  * every POLL_SEC: read the YES order book and the taker trades since the last poll (data API),
  * quotes (in YES terms): a bid at the best bid and an ask at the best ask, QUOTE shares each, joining the BACK of
    the queue (behind the size shown at that price); quotes move to the new best price each poll (back of queue),
  * inventory skew (Avellaneda-Stoikov flavour): while net YES inventory > SKEW, no new bids (only the ask); while
    < -SKEW, no new asks. Hard cap QMAX shares net,
  * fills from real trades after the quote was posted: a taker SELL of YES at <= our bid (or a taker BUY of NO at
    >= 1 - bid) eats the queue ahead first, then fills us; prices through our level fill us outright. Mirror for asks.
    Selling YES we do not hold = buying NO at 1 - ask; matched YES + NO merge into $1 cash at once,
  * maker fee 0; maker rebate estimated as category share x taker fee-equivalent (sports 15%, politics 25%),
  * P&L = cash + inventory marked at the mid; inventory settles at resolution.
Pre-registered M3 (REGISTERED_AT): pass = >= 50 market-days with fills and mean P&L per market-day - 1.645 SE > 0.
Off with POLY_EVENTMAKER=0.
"""
from __future__ import annotations
import asyncio,datetime as dt,gzip,json,math,os,time
from collections import defaultdict,deque

GAMMA='https://gamma-api.polymarket.com';CLOB='https://clob.polymarket.com';DATA='https://data-api.polymarket.com'
POLL_SEC=30;PICK_SEC=7200;N_MARKETS=20;MIN_VOL24=20000.;QUOTE=10.;SKEW=20.;QMAX=40.;MIN_DAYS=2.;MIN_DAYS_PASS=50
RATE={'sports':0.05,'politics':0.04};REBATE_SHARE={'sports':0.15,'politics':0.25}
REGISTERED_AT=1793092815   # 2026-10-27 09:20 UTC: forward results count only after this
CURRENT=None
def pick_slug(d,slug):
    """Gamma can answer a slug query with other markets; accept only the market whose slug matches exactly."""
    for m in (d if isinstance(d,list) else []):
        if isinstance(m,dict) and m.get('slug')==slug:return m
    return None
def _jl(v):
    if isinstance(v,str):
        try:return json.loads(v)
        except ValueError:return None
    return v
def _ts(iso):
    try:return dt.datetime.fromisoformat(str(iso).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None
def category(ev):
    tags=[str((t or {}).get('slug') or '').lower() for t in (ev or {}).get('tags') or []]
    for c in ('sports','politics'):
        if c in tags or str((ev or {}).get('category') or '').lower()==c:return c
    return None
def best(book,side):
    lv=[(float(a['price']),float(a['size'])) for a in (book or {}).get(side) or [] if float(a.get('size') or 0)>0]
    if not lv:return None
    return max(lv) if side=='bids' else min(lv)
def yes_terms(tr,yes_token):
    """A taker trade in YES terms: (price, taker side as BUY/SELL of YES, size, time)."""
    p=float(tr['price']);sz=float(tr['size']);ts=float(tr['timestamp']);ts=ts/1000 if ts>1e12 else ts
    side=str(tr.get('side') or '').upper()
    is_yes=str(tr.get('asset') or '')==yes_token if tr.get('asset') else str(tr.get('outcome') or '').lower() in ('yes','')
    if is_yes:return p,side,sz,ts
    return 1-p,('SELL' if side=='BUY' else 'BUY'),sz,ts

class Mkt:
    def __init__(self,slug,cid,yes,no,cat,end):
        self.slug=slug;self.cid=cid;self.yes=yes;self.no=no;self.cat=cat;self.end=end
        self.bid=None;self.ask=None;self.yes_sh=0.;self.no_sh=0.;self.cash=0.;self.rebate=0.;self.fills=0;self.vol=0.;self.seen=set();self.last_t=0.
        self.mid=None;self.days=defaultdict(lambda:{'fills':0,'pnlStart':None,'pnlEnd':None})
    def net(self):return self.yes_sh-self.no_sh
    def mtm(self):return self.cash+self.rebate+(self.yes_sh*self.mid+self.no_sh*(1-self.mid) if self.mid is not None else 0.)
    def merge(self):
        m=min(self.yes_sh,self.no_sh)
        if m>0:self.yes_sh-=m;self.no_sh-=m;self.cash+=m
    def fill(self,kind,price,n,rate,share):
        if kind=='bid':self.yes_sh+=n;self.cash-=n*price
        else:self.no_sh+=n;self.cash-=n*(1-price)                       # selling YES = buying NO at 1 - price
        self.rebate+=share*rate*price*(1-price)*n;self.fills+=1;self.vol+=n*(price if kind=='bid' else 1-price);self.merge()

class EventMaker:
    def __init__(self,http=None,root=None,now=time.time):
        self.http=http;self.now=now;self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_EVENTMAKER','1')!='0'
        self.mk={};self.closed=deque(maxlen=5000);self.picked_at=0.
        self.state={'polls':0,'markets':0,'fills':0,'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'eventmaker_state.json.gz')
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=20,headers={'User-Agent':'poly-eventmaker-study'})
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    async def pick(self):
        now=self.now();d=await self.get(GAMMA+'/events',{'closed':'false','active':'true','limit':200,'order':'volume24hr','ascending':'false'})
        out=[]
        for ev in d or []:
            cat=category(ev)
            if not cat:continue
            for m in ev.get('markets') or []:
                tk=_jl(m.get('clobTokenIds')) or [];outs=[str(o).lower() for o in (_jl(m.get('outcomes')) or [])]
                end=_ts(m.get('endDate') or ev.get('endDate'))
                try:v=float(m.get('volume24hr') or 0);bb=float(m.get('bestBid'));ba=float(m.get('bestAsk'))
                except (TypeError,ValueError):continue
                if len(tk)!=2 or outs!=['yes','no'] or not end or end<now+MIN_DAYS*86400 or v<MIN_VOL24 or m.get('closed'):continue
                if not 0.01<=ba-bb<=0.06 or not 0.05<bb<0.95:continue
                out.append((v,Mkt(m['slug'],m.get('conditionId'),str(tk[0]),str(tk[1]),cat,end)))
        out.sort(key=lambda x:-x[0])
        keep={s:m for s,m in self.mk.items() if m.net()!=0}                 # never drop a market holding inventory
        for _,m in out:
            if len(keep)>=N_MARKETS:break
            keep.setdefault(m.slug,self.mk.get(m.slug) or m)
        for s,m in self.mk.items():
            if s not in keep and m.fills:self.closed.append(self._final(m,'rotated out (flat)'))
        self.mk=keep;self.state['markets']=len(keep);self.picked_at=now
    def _final(self,m,why):return {'slug':m.slug,'cat':m.cat,'fills':m.fills,'volumeUsd':round(m.vol,2),'pnlUsd':round(m.mtm(),4),'rebateUsd':round(m.rebate,4),'why':why,'t':self.now()}
    async def poll(self,m):
        now=self.now()
        try:book=await self.get(CLOB+'/book',{'token_id':m.yes})
        except Exception as exc:
            if '404' in str(exc):                         # book gone: the market has closed; stop quoting, settle it
                m.end=min(m.end,now);m.bid=m.ask=None;self.state['booksGone']=self.state.get('booksGone',0)+1;return
            raise
        b,a=best(book,'bids'),best(book,'asks')
        if b and a:m.mid=(b[0]+a[0])/2
        trs=await self.get(DATA+'/trades',{'market':m.cid,'limit':100,'takerOnly':'true'})
        rows=trs if isinstance(trs,list) else (trs or {}).get('data') or []
        rate=RATE.get(m.cat,0.05);share=REBATE_SHARE.get(m.cat,0.2)
        for tr in sorted(rows,key=lambda x:float(x.get('timestamp') or 0)):
            key=(tr.get('transactionHash'),tr.get('asset'),tr.get('price'),tr.get('size'),tr.get('timestamp'))
            if key in m.seen:continue
            m.seen.add(key)
            try:p,side,sz,ts=yes_terms(tr,m.yes)
            except (KeyError,TypeError,ValueError):continue
            for kind,q in (('bid',m.bid),('ask',m.ask)):
                if not q or ts<=q['postedAt']:continue
                hit=(kind=='bid' and side=='SELL' and p<=q['price']+1e-9) or (kind=='ask' and side=='BUY' and p>=q['price']-1e-9)
                if not hit:continue
                if abs(p-q['price'])<1e-9:eat=min(q['ahead'],sz);q['ahead']-=eat;n=min(q['left'],sz-eat)
                else:n=q['left']
                if n>0:
                    m.fill(kind,q['price'],n,rate,share);q['left']-=n;self.state['fills']+=1
                    m.days[time.strftime('%Y-%m-%d',time.gmtime(ts))]['fills']+=1
        if len(m.seen)>3000:m.seen=set(list(m.seen)[-1500:])
        # requote: join the best levels, back of the queue, with inventory skew and caps
        net=m.net()
        if b and net<min(SKEW,QMAX) and (not m.bid or abs(m.bid['price']-b[0])>1e-9 or m.bid['left']<=0):
            m.bid={'price':b[0],'ahead':b[1],'left':QUOTE,'postedAt':now}
        elif net>=SKEW:m.bid=None
        if a and net>-min(SKEW,QMAX) and (not m.ask or abs(m.ask['price']-a[0])>1e-9 or m.ask['left']<=0):
            m.ask={'price':a[0],'ahead':a[1],'left':QUOTE,'postedAt':now}
        elif net<=-SKEW:m.ask=None
        day=m.days[time.strftime('%Y-%m-%d',time.gmtime(now))];v=m.mtm()
        if day['pnlStart'] is None:day['pnlStart']=v
        day['pnlEnd']=v
    async def settle_closed(self):
        """Markets past their end: when resolved, inventory pays out (YES at p_yes, NO at p_no) and the market closes."""
        now=self.now()
        for s,m in list(self.mk.items()):
            if m.end>now:continue
            try:
                d=await self.get(GAMMA+'/markets',{'slug':s,'closed':'true'});mm=pick_slug(d,s)
                pr=[float(x) for x in (_jl((mm or {}).get('outcomePrices')) or [])]
            except Exception as exc:self._err('settle',exc);continue
            if not mm or not mm.get('closed') or len(pr)!=2:continue
            m.cash+=m.yes_sh*pr[0]+m.no_sh*pr[1];m.yes_sh=0.;m.no_sh=0.;m.mid=None
            self.closed.append(self._final(m,'resolved'));self.mk.pop(s,None)
    def report(self):
        md=[]
        for m in self.mk.values():
            for d,x in m.days.items():
                if x['fills'] and x['pnlStart'] is not None and time.mktime(time.strptime(d,'%Y-%m-%d'))>=REGISTERED_AT-86400:md.append(x['pnlEnd']-x['pnlStart'])
        n=len(md);mean=sum(md)/n if n else None;sd=math.sqrt(sum((x-mean)**2 for x in md)/(n-1)) if n>1 else None
        lo=round(mean-1.645*sd/math.sqrt(n),4) if sd else None
        tot=sum(m.mtm() for m in self.mk.values())+sum(c['pnlUsd'] for c in self.closed)
        return {'enabled':self.enabled,**self.state,
                'M3':{'rule':'join best bid and ask on ~20 liquid sports/politics markets, back of the queue, inventory skew; fills from real trades; maker rebate; mark to mid',
                      'pass':f'>= {MIN_DAYS_PASS} market-days with fills and mean P&L per market-day - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'marketDays':n,'avgPerMarketDay':None if mean is None else round(mean,4),'lower95':lo,
                      'verdict':'collecting' if n<MIN_DAYS_PASS else ('CONFIRMED' if lo is not None and lo>0 else 'not confirmed')},
                'pnlMtmUsd':round(tot,3),'rebateUsd':round(sum(m.rebate for m in self.mk.values()),4),'volumeUsd':round(sum(m.vol for m in self.mk.values()),2),
                'markets':[{'slug':m.slug,'cat':m.cat,'fills':m.fills,'net':round(m.net(),1),'mid':m.mid,'pnlUsd':round(m.mtm(),3)} for m in sorted(self.mk.values(),key=lambda x:-x.fills)][:20]}
    def compact(self):
        r=self.report();r['markets']=r['markets'][:5];return r
    def save(self):
        st={'closed':list(self.closed),'mk':{s:{k:getattr(m,k) for k in ('slug','cid','yes','no','cat','end','yes_sh','no_sh','cash','rebate','fills','vol','mid')}|{'days':dict(m.days)} for s,m in self.mk.items()}}
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump(st,f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:st=json.load(f)
        self.closed.extend(st.get('closed') or [])
        for s,d in (st.get('mk') or {}).items():
            m=Mkt(d['slug'],d['cid'],d['yes'],d['no'],d['cat'],d['end'])
            for k in ('yes_sh','no_sh','cash','rebate','fills','vol','mid'):setattr(m,k,d.get(k))
            m.days.update(d.get('days') or {});self.mk[s]=m
        return True
    async def loop(self,first_delay=240):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err('load',exc)
        await asyncio.sleep(first_delay);tick=0
        while True:
            try:
                if self.now()-self.picked_at>PICK_SEC:await self.pick()
                for m in list(self.mk.values()):
                    try:await self.poll(m)
                    except Exception as exc:self._err('poll',exc)
                    await asyncio.sleep(.5)
                self.state['polls']+=1;tick+=1
                if tick%10==0:
                    await self.settle_closed();self.save()
            except Exception as exc:self._err('loop',exc)
            await asyncio.sleep(POLL_SEC)
