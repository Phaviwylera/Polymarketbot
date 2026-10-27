"""Cross-market lane X1 (record-only; places no orders): do related markets disagree in ways that pay?

Uses Polymarket's daily "Bitcoin/Ethereum above $X on <date>" ladders (discovery and order books shared with the
threshold lane) every SCAN_SEC:
 A) Strike-ladder no-arbitrage. For strikes K1 < K2 on the same date, YES(K1) + NO(K2) pays at least $1 whatever
    happens. If ask_YES(K1) + ask_NO(K2) + taker fees + 1c stress per leg < 1, that is a riskless gap. Every gap is
    logged with the size available; the first gap per pair is recorded as a shadow trade and settled at resolution.
 B) Deribit-implied probability. The same event priced by the options market: P(S_T > K) = N(d2) - vega * dsigma/dK
    (digital call from the smile, undiscounted), with sigma(K) interpolated in log-strike on the two Deribit expiries
    around T and in total variance between them. Recorded at fixed horizons before each market closes (24h, 6h,
    1h; one snapshot each, so the score is unbiased) next to Polymarket's mid, and scored at resolution.
    Shadow trade rule X1-B: buy the side whose ask + fee + 1c is >= 5 points below the Deribit chance; one per
    market; hold to resolution. Pre-registered at REGISTERED_AT; pass = >= 30 events (days x asset) with trades and
    the per-event mean return - 1.645 SE > 0.
Off with POLY_XMARKET=0.
"""
from __future__ import annotations
import asyncio,bisect,datetime as dt,gzip,json,math,os,re,time
from collections import deque

DERIBIT='https://www.deribit.com/api/v2/public/get_book_summary_by_currency'
SCAN_SEC=300;EDGE=0.05;STRESS=0.01;STAKE=5.0;MIN_EVENTS=30
HORIZONS=((24*3600,'24h'),(6*3600,'6h'),(3600,'1h'))
MONTHS={m:i for i,m in enumerate(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'],1)}
INSTR=re.compile(r'^(BTC|ETH)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:d\d+)?)-([CP])$')
REGISTERED_AT=1793071725   # 2026-10-27 03:28 UTC: forward results count only after this
CURRENT=None
YEAR=365*86400
def ncdf(x):return 0.5*math.erfc(-x/math.sqrt(2))
def npdf(x):return math.exp(-x*x/2)/math.sqrt(2*math.pi)

# ---- Deribit smile -------------------------------------------------------------------------------------
def parse_deribit(rows):
    """{expiryTs: {'F': underlying, 'smile': [(strike, iv)...] sorted}} from book summaries (calls and puts, OTM preferred)."""
    by={}
    for r in rows or []:
        m=INSTR.match(str(r.get('instrument_name') or ''))
        iv=r.get('mark_iv');F=r.get('underlying_price')
        if not m or iv in (None,0) or not F:continue
        _,d,mon,yy,k,cp=m.groups()
        if mon not in MONTHS:continue
        exp=dt.datetime(2000+int(yy),MONTHS[mon],int(d),8,0,tzinfo=dt.timezone.utc).timestamp()
        k=float(k.replace('d','.'));iv=float(iv);iv=iv/100 if iv>3 else iv;F=float(F)
        otm=(cp=='C' and k>=F) or (cp=='P' and k<F)
        e=by.setdefault(exp,{'F':F,'pts':{}})
        if k not in e['pts'] or otm:e['pts'][k]=iv
    return {exp:{'F':e['F'],'smile':sorted(e['pts'].items())} for exp,e in by.items() if len(e['pts'])>=5}

def smile_iv(smile,K):
    """Linear in log-strike, flat beyond the wings; returns (iv, d iv / dK)."""
    ks=[math.log(k) for k,_ in smile];vs=[v for _,v in smile];x=math.log(K)
    if x<=ks[0]:return vs[0],0.
    if x>=ks[-1]:return vs[-1],0.
    i=bisect.bisect_right(ks,x)-1;w=(x-ks[i])/(ks[i+1]-ks[i]);iv=vs[i]+w*(vs[i+1]-vs[i])
    slope=(vs[i+1]-vs[i])/(ks[i+1]-ks[i])/K          # d iv / d logK * d logK / dK
    return iv,slope

def deribit_prob(chain,K,T,now):
    """P(S_T > K) from the options smile around T. None if T is not bracketed sensibly."""
    exps=sorted(chain)
    if not exps or T<=now:return None
    after=[e for e in exps if e>=T];before=[e for e in exps if now<e<T]
    if not after:return None
    e2=after[0];e1=before[-1] if before else None
    t=(T-now)/YEAR
    iv2,s2=smile_iv(chain[e2]['smile'],K);F=chain[e2]['F']
    if e1 is not None:   # total variance linear in time between the two expiries
        iv1,s1=smile_iv(chain[e1]['smile'],K);t1=(e1-now)/YEAR;t2=(e2-now)/YEAR
        w1,w2=iv1*iv1*t1,iv2*iv2*t2;a=(t-t1)/(t2-t1);wt=w1+a*(w2-w1)
        if wt<=0:return None
        iv=math.sqrt(wt/t);slope=s1+a*(s2-s1)
    else:iv,slope=iv2,s2
    if iv<=0:return None
    sd=iv*math.sqrt(t);d1=(math.log(F/K)+0.5*sd*sd)/sd;d2=d1-sd
    vega=F*npdf(d1)*math.sqrt(t)                  # undiscounted, per unit of vol
    p=ncdf(d2)-vega*slope
    return max(0.,min(1.,p))

# ---- order books ---------------------------------------------------------------------------------------
def top(book,side):
    lv=[(float(a['price']),float(a['size'])) for a in (book or {}).get(side) or [] if float(a.get('size') or 0)>0]
    if not lv:return None
    return min(lv) if side=='asks' else max(lv)

class XMarket:
    def __init__(self,http=None,root=None,now=time.time):
        import poly_threshold
        self.tl=poly_threshold.ThresholdLane(http=http);self.http=http;self.now=now
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_XMARKET','1')!='0'
        self.snaps={};self.trades={};self.arbs={};self.scored=deque(maxlen=20000);self.closed=deque(maxlen=5000)
        self.gaps=deque(maxlen=500)
        self.state={'scans':0,'markets':0,'chains':{},'gapsSeen':0,'errors':0,'lastError':None,'lastScanAt':None}
    def path(self):return os.path.join(self.root,'xmarket_state.json.gz')
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    async def chain(self,cur):
        d=await self.tl.get(DERIBIT,{'currency':cur,'kind':'option'})
        return parse_deribit((d or {}).get('result') if isinstance(d,dict) else d)
    async def scan(self):
        import poly_threshold as pt
        now=self.now();self.state['scans']+=1;self.state['lastScanAt']=now
        for asset in ('BTC','ETH'):
            try:ms=await self.tl.discover(asset,now)
            except Exception as exc:self._err('discover',exc);continue
            if not ms:continue
            try:chain=await self.chain(asset);self.state['chains'][asset]=len(chain)
            except Exception as exc:self._err('deribit',exc);chain={}
            toks=[t for m in ms for t in (m['yesToken'],m['noToken'])]
            try:books=await self.tl.books(toks)
            except Exception as exc:self._err('books',exc);continue
            self.state['markets']=len(ms)
            q={}
            for m in ms:
                yb,ya=top(books.get(m['yesToken']),'bids'),top(books.get(m['yesToken']),'asks')
                nb,na=top(books.get(m['noToken']),'bids'),top(books.get(m['noToken']),'asks')
                q[m['slug']]=(m,ya,na,yb,nb)
                self._deribit(m,ya,na,yb,nb,chain,now)
            self._ladder(q,now)
    def _deribit(self,m,ya,na,yb,nb,chain,now):
        import poly_threshold as pt
        tau=m['endAt']-now
        if tau<=0 or not chain:return
        p=deribit_prob(chain,m['strike'],m['endAt'],now)
        if p is None:return
        mid=(yb[0]+ya[0])/2 if (yb and ya) else None
        for h,name in HORIZONS:                       # one snapshot per market per horizon, the first scan inside it
            key=(m['slug'],name)
            if tau<=h and tau>h-2*SCAN_SEC and key not in self.snaps:
                self.snaps[key]={'slug':m['slug'],'asset':m['asset'],'event':m['event'],'strike':m['strike'],'endAt':m['endAt'],'horizon':name,
                                 't':now,'pDeribit':round(p,4),'mid':None if mid is None else round(mid,4),'yesAsk':ya and ya[0],'noAsk':na and na[0]}
        if m['slug'] in self.trades or tau<pt.MIN_TAU_SEC:return
        best=None
        for side,ask,chance in (('YES',ya,p),('NO',na,1-p)):
            if not ask or not 0.03<=ask[0]<=0.97:continue
            cost=min(.99,ask[0]+STRESS);edge=chance-cost-pt.fee(cost)
            if edge>=EDGE and (best is None or edge>best[3]):best=(side,ask[0],cost,edge,chance)
        if best:
            side,ask,cost,edge,chance=best
            self.trades[m['slug']]={'slug':m['slug'],'asset':m['asset'],'event':m['event'],'strike':m['strike'],'endAt':m['endAt'],'t':now,
                                    'side':side,'ask':ask,'cost':round(cost,4),'pDeribit':round(p,4),'edge':round(edge,4),'shares':round(STAKE/cost,4)}
    def _ladder(self,q,now):
        import poly_threshold as pt
        ev={}
        for slug,(m,ya,na,yb,nb) in q.items():ev.setdefault((m['asset'],m['endAt']),[]).append((m['strike'],slug,ya,na))
        for k,rows in ev.items():
            rows.sort()
            for i in range(len(rows)):
                for j in range(i+1,len(rows)):
                    k1,s1,ya1,_=rows[i];k2,s2,_,na2=rows[j]
                    if not ya1 or not na2:continue
                    c1=min(.99,ya1[0]+STRESS);c2=min(.99,na2[0]+STRESS)
                    cost=c1+pt.fee(c1)+c2+pt.fee(c2)
                    if cost<1:
                        size=min(ya1[1],na2[1]);self.state['gapsSeen']+=1
                        g={'t':now,'asset':k[0],'endAt':k[1],'low':k1,'high':k2,'yesAskLow':ya1[0],'noAskHigh':na2[0],'costPerPair':round(cost,4),
                           'gainPerPair':round(1-cost,4),'pairsAvailable':round(size,2)}
                        self.gaps.append(g)
                        key=f'{s1}|{s2}'
                        if key not in self.arbs:self.arbs[key]={**g,'slugLow':s1,'slugHigh':s2,'pairs':round(min(size,STAKE/cost),4)}
    async def resolve(self):
        now=self.now()
        slugs={s['slug'] for s in self.snaps.values() if now>s['endAt']+600}|{t['slug'] for t in self.trades.values() if now>t['endAt']+600}
        slugs|={a['slugLow'] for a in self.arbs.values() if now>a['endAt']+600}|{a['slugHigh'] for a in self.arbs.values() if now>a['endAt']+600}
        won={}
        for slug in list(slugs)[:60]:
            try:
                d=await self.tl.get(self.tl_gamma(),{'slug':slug,'closed':'true'})
                m=d[0] if isinstance(d,list) and d else None
                pr=[float(x) for x in (json.loads(m['outcomePrices']) if isinstance(m.get('outcomePrices'),str) else m.get('outcomePrices') or [])] if m else []
                if m and m.get('closed') and len(pr)==2 and max(pr)>=.99:won[slug]=pr[0]>pr[1]     # True = YES won
            except Exception as exc:self._err('resolve',exc)
        for key in [k for k,s in self.snaps.items() if s['slug'] in won]:
            s=self.snaps.pop(key);s['yesWon']=won[s['slug']];self.scored.append(s)
        for slug in [s for s in self.trades if s in won]:
            t=self.trades.pop(slug);w=won[slug] if t['side']=='YES' else not won[slug]
            t['won']=w;t['pnl']=round(t['shares']*(1. if w else 0.)-STAKE,4);t['ret']=round(t['pnl']/STAKE,4);self.closed.append({**t,'kind':'deribit'})
        for key in [k for k,a in self.arbs.items() if a['slugLow'] in won and a['slugHigh'] in won]:
            a=self.arbs.pop(key);pay=(1. if won[a['slugLow']] else 0.)+(0. if won[a['slugHigh']] else 1.)
            a['payPerPair']=pay;a['pnl']=round(a['pairs']*(pay-a['costPerPair']),4);self.closed.append({**a,'kind':'ladder'})
    def tl_gamma(self):
        import poly_threshold as pt;return pt.GAMMA+'/markets'
    # ---- report --------------------------------------------------------------------------------------
    def report(self):
        sc=list(self.scored)
        def brier(rs,k):
            v=[(r[k]-(1. if r['yesWon'] else 0.))**2 for r in rs if r.get(k) is not None];return round(sum(v)/len(v),5) if v else None
        hz={name:{'n':len([r for r in sc if r['horizon']==name and r.get('mid') is not None]),
                  'brierDeribit':brier([r for r in sc if r['horizon']==name and r.get('mid') is not None],'pDeribit'),
                  'brierPolymarket':brier([r for r in sc if r['horizon']==name],'mid')} for _,name in HORIZONS}
        cl=[c for c in self.closed if c['kind']=='deribit' and c['t']>=REGISTERED_AT]
        ev={}
        for c in cl:ev.setdefault((c['asset'],c['event']),[]).append(c['ret'])
        means=[sum(v)/len(v) for v in ev.values()];n=len(means);m=sum(means)/n if n else None
        sd=math.sqrt(sum((x-m)**2 for x in means)/(n-1)) if n>1 else None
        lo=round(m-1.645*sd/math.sqrt(n),4) if sd else None
        lad=[c for c in self.closed if c['kind']=='ladder']
        return {'enabled':self.enabled,**self.state,'openSnapshots':len(self.snaps),'openTrades':len(self.trades),'openLadderPairs':len(self.arbs),
                'calibration':hz,
                'X1':{'rule':'buy the side priced >= 5 points below the Deribit-implied chance (ask + fee + 1c); hold to resolution',
                      'pass':f'>= {MIN_EVENTS} events with trades and per-event mean return - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'trades':len(cl),'events':n,'avgReturn':None if m is None else round(m,4),'lower95':lo,
                      'pnlUsd':round(sum(c['pnl'] for c in cl),3),
                      'verdict':'collecting' if n<MIN_EVENTS else ('CONFIRMED' if lo is not None and lo>0 else 'not confirmed')},
                'ladder':{'gapsSeen':self.state['gapsSeen'],'pairsSettled':len(lad),'pnlUsd':round(sum(c['pnl'] for c in lad),4),
                          'recentGaps':list(self.gaps)[-6:][::-1]}}
    def compact(self):
        r=self.report();r['ladder'].pop('recentGaps',None);return r
    # ---- persistence and loop ------------------------------------------------------------------------
    def save(self):
        st={'snaps':[[list(k),v] for k,v in self.snaps.items()],'trades':self.trades,'arbs':self.arbs,'scored':list(self.scored),
            'closed':list(self.closed),'gaps':list(self.gaps),'gapsSeen':self.state['gapsSeen']}
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump(st,f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:st=json.load(f)
        self.snaps={tuple(k):v for k,v in st.get('snaps') or []};self.trades.update(st.get('trades') or {});self.arbs.update(st.get('arbs') or {})
        self.scored.extend(st.get('scored') or []);self.closed.extend(st.get('closed') or []);self.gaps.extend(st.get('gaps') or [])
        self.state['gapsSeen']=int(st.get('gapsSeen') or 0);return True
    async def loop(self,first_delay=150):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err('load',exc)
        await asyncio.sleep(first_delay);tick=0
        while True:
            try:await self.scan()
            except Exception as exc:self._err('scan',exc)
            tick+=1
            if tick%3==0:
                try:await self.resolve()
                except Exception as exc:self._err('resolve loop',exc)
            try:self.save()
            except Exception as exc:self._err('save',exc)
            await asyncio.sleep(SCAN_SEC)
