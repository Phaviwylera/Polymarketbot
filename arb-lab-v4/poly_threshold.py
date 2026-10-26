"""Shadow lane: probability-model trades on Polymarket's daily "Bitcoin/Ethereum above $X on <date>" markets.

PAPER, RECORD-ONLY. Places no orders, uses its own virtual $200 bankroll, never touches the main paper
account, its risk limits or the frozen momentum config. Rules (the "pricing loop" described for the
wallet coinman2, with one added filter):
  1. Model chance that spot ends above the strike: lognormal, P = N(d2), d2 = (ln(S/K) - s^2 t/2)/(s sqrt t),
     s = realized volatility from Coinbase candles (mean of 24h of 5-minute and 7d of hourly returns).
  2. Buy the side (Yes or No) whose ask, plus a taker fee, is at least MIN_EDGE below the model chance.
  3. Only sides priced 20c-95c.  4. Hold to resolution; never sell.
  5. Skip the whole event when model volatility and the market-implied volatility (backed out of the
     strike priced nearest 50c) differ by more than 1.5x.
Fee assumption: the same taker fee as the 5/15-minute markets (0.07 x p x (1-p) per share) on entry; no
exit fee because positions settle. Conservative if these markets charge less.
Every entry and every resolution is a THRESHOLD_SHADOW event; open positions are rebuilt from the
database after a restart. Off with POLY_THRESHOLD=0.
"""
from __future__ import annotations
import asyncio,datetime as dt,json,math,os,re,time

GAMMA='https://gamma-api.polymarket.com';CLOB='https://clob.polymarket.com';COINBASE='https://api.exchange.coinbase.com'
ASSETS={'BTC':('bitcoin','BTC-USD'),'ETH':('ethereum','ETH-USD')}
FEE_RATE=0.07;MIN_PRICE=0.20;MAX_PRICE=0.95;MIN_EDGE=0.03;VOL_RATIO_MAX=1.5
STAKE_USD=5.0;MAX_OPEN_PER_EVENT=4;MAX_OPEN_USD=100.0;BANKROLL=200.0
MIN_TAU_SEC=1800;MAX_TAU_SEC=3*86400;SCAN_SEC=300;YEAR=365*86400
PASS={'minTrades':60,'meanAbove':0.,'blockLowerAbove':0.,'stress1cAbove':0.}
STRIKE=re.compile(r'\$\s?([\d,]+(?:\.\d+)?)\s*([kK])?')
CURRENT=None

def fee(p):return FEE_RATE*p*(1-p)
def ncdf(x):return 0.5*math.erfc(-x/math.sqrt(2))

def prob_above(spot,strike,sigma,tau_sec):
    if spot<=0 or strike<=0 or sigma<=0 or tau_sec<=0:return None
    t=tau_sec/YEAR;s=sigma*math.sqrt(t)
    return ncdf((math.log(spot/strike)-0.5*sigma*sigma*t)/s)

def implied_vol(spot,strike,tau_sec,price):
    """Volatility at which the model chance equals `price` (bisection; None if no solution)."""
    if not 0.02<price<0.98:return None
    f=lambda v:prob_above(spot,strike,v,tau_sec)-price
    lo,hi=0.01,5.0;flo,fhi=f(lo),f(hi)
    if flo is None or fhi is None or flo*fhi>0:return None
    for _ in range(80):
        mid=(lo+hi)/2;fm=f(mid)
        if fm*flo>0:lo,flo=mid,fm
        else:hi=mid
    return (lo+hi)/2

def realized_vol(closes,period_sec):
    r=[math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]
    if len(r)<10:return None
    m=sum(r)/len(r);var=sum((x-m)**2 for x in r)/(len(r)-1)
    return math.sqrt(var*YEAR/period_sec)

def parse_strike(market):
    for txt in (market.get('groupItemTitle'),market.get('question')):
        m=STRIKE.search(str(txt or ''))
        if m:return float(m.group(1).replace(',',''))*(1000 if m.group(2) else 1)
        if txt and re.fullmatch(r'[\d,]+(\.\d+)?',str(txt).strip()):return float(str(txt).replace(',',''))
    return None

def _jl(v):
    if isinstance(v,str):
        try:return json.loads(v)
        except ValueError:return None
    return v

def _ts(iso):
    try:return dt.datetime.fromisoformat(str(iso).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None

def is_above_event(ev,name):
    title=str(ev.get('title') or '').lower();slug=str(ev.get('slug') or '').lower()
    return (f'{name} above' in title or slug.startswith(f'{name}-above')) and not any(x in title for x in ('between','range','hit','dip','reach'))

def parse_event(ev,asset):
    """[market dict] for every open strike market in an 'above $X on <date>' event."""
    out=[]
    for m in ev.get('markets') or []:
        if m.get('closed') or m.get('active') is False:continue
        toks=_jl(m.get('clobTokenIds')) or [];outs=[str(o).lower() for o in (_jl(m.get('outcomes')) or [])]
        k=parse_strike(m);end=_ts(m.get('endDate') or ev.get('endDate'))
        if len(toks)!=2 or outs!=['yes','no'] or not k or not end:continue
        out.append({'asset':asset,'event':ev.get('slug'),'slug':m.get('slug'),'conditionId':m.get('conditionId'),'strike':k,'endAt':end,
                    'yesToken':str(toks[0]),'noToken':str(toks[1]),'question':m.get('question')})
    return out

def best_ask(book):
    asks=[(float(a['price']),float(a['size'])) for a in (book or {}).get('asks') or [] if float(a.get('size') or 0)>0]
    return min(asks) if asks else None

def decide(m,spot,sigma,yes_ask,no_ask,now,vol_ratio):
    """Pure rule check; returns (side, price, prob, edge, reason)."""
    tau=m['endAt']-now
    if tau<MIN_TAU_SEC:return None,None,None,None,'too close to resolution'
    if tau>MAX_TAU_SEC:return None,None,None,None,'too far from resolution'
    if vol_ratio is None:return None,None,None,None,'no implied volatility'
    if max(vol_ratio,1/vol_ratio)>VOL_RATIO_MAX:return None,None,None,None,'model and market volatility disagree > 1.5x'
    p=prob_above(spot,m['strike'],sigma,tau)
    if p is None:return None,None,None,None,'model unavailable'
    best=None
    for side,ask,chance in (('YES',yes_ask,p),('NO',no_ask,1-p)):
        if ask is None:continue
        price=ask[0]
        if not MIN_PRICE<=price<=MAX_PRICE:continue
        edge=chance-price-fee(price)
        if edge>=MIN_EDGE and ask[1]*price>=STAKE_USD and (best is None or edge>best[3]):best=(side,price,chance,edge)
    if best is None:return None,None,p,None,'no side priced below the model chance'
    return (*best,'enter')

class ThresholdLane:
    def __init__(self,http=None):
        self.http=http;self.open={};self.closed=[];self.seq=0;self.boot=f'threshold-{int(time.time())}'
        self.state={'enabled':True,'lastScanAt':None,'events':0,'markets':0,'skipped':{},'vol':{},'errors':0,'lastError':None,
                    'restored':False,'mode':'paper, record-only, own $200 bankroll, hold to resolution'}
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=20,headers={'User-Agent':'poly-threshold-shadow'})
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    async def books(self,tokens):
        out={}
        try:
            r=await (await self._c()).post(CLOB+'/books',json=[{'token_id':t} for t in tokens]);r.raise_for_status()
            for b in r.json() or []:out[str(b.get('asset_id'))]=b
        except Exception:
            for t in tokens:
                try:out[t]=await self.get(CLOB+'/book',{'token_id':t})
                except Exception:pass
        return out
    async def vol(self,product):
        a=await self.get(f'{COINBASE}/products/{product}/candles',{'granularity':300})
        b=await self.get(f'{COINBASE}/products/{product}/candles',{'granularity':3600})
        c5=[float(x[4]) for x in sorted(a or [])][-288:];c60=[float(x[4]) for x in sorted(b or [])][-168:]
        v=[x for x in (realized_vol(c5,300),realized_vol(c60,3600)) if x];return (sum(v)/len(v) if v else None),(c5[-1] if c5 else None)
    async def discover(self,asset,now):
        name=ASSETS[asset][0];evs=[]
        try:
            data=await self.get(GAMMA+'/events',{'closed':'false','active':'true','limit':200,'order':'endDate','ascending':'true',
                                                  'end_date_min':dt.datetime.fromtimestamp(now,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                                  'end_date_max':dt.datetime.fromtimestamp(now+MAX_TAU_SEC,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                                  'tag_slug':'crypto'})
            evs=[e for e in data or [] if is_above_event(e,name)]
        except Exception as exc:self._err('discover',exc)
        have={e.get('slug') for e in evs}
        # always add the daily ladders by slug: the tag listing is capped and sorted by end time
        for d in range(0,4):
            day=dt.datetime.fromtimestamp(now+d*86400,dt.timezone.utc)
            base=f"{name}-above-on-{day.strftime('%B').lower()}-{day.day}"
            for slug in (f'{base}-{day.year}',base):          # live slugs carry the year: bitcoin-above-on-october-25-2026
                if slug in have:continue
                try:
                    data=await self.get(GAMMA+'/events',{'slug':slug})
                    evs+=[e for e in data or [] if is_above_event(e,name) and e.get('slug') not in have];have|={e.get('slug') for e in evs}
                except Exception:pass
        return [m for e in evs for m in parse_event(e,asset)]
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}'
    def _skip(self,reason):self.state['skipped'][reason]=self.state['skipped'].get(reason,0)+1
    def _event(self,store,action,row,now):
        self.seq+=1
        if store is not None:store.enqueue({'bootId':self.boot,'journalSeq':self.seq,'t':now,'asset':row.get('asset','ALL'),
                                            'kind':'THRESHOLD_SHADOW','action':action,'row':row})
    def open_usd(self):return sum(p['costUsd'] for p in self.open.values())

    async def scan(self,store,now=None):
        if os.environ.get('POLY_TRADING_PAUSED','1')=='1':   # study mode: no new positions; open ones still resolve
            self.state['skipped']={'all trading paused by owner (study mode)':1};self.state['lastScanAt']=time.time() if now is None else now
            await self.resolve(store,now);return
        now=time.time() if now is None else now;self.state['skipped']={};seen=0;events=set()
        for asset,(name,product) in ASSETS.items():
            try:sigma,spot=await self.vol(product)
            except Exception as exc:self._err(f'vol {asset}',exc);continue
            if not sigma or not spot:self._skip('no volatility');continue
            markets=await self.discover(asset,now);seen+=len(markets)
            if not markets:continue
            bk=await self.books([t for m in markets for t in (m['yesToken'],m['noToken'])])
            by_event={}
            for m in markets:by_event.setdefault(m['event'],[]).append(m);events.add(m['event'])
            for ev,ms in by_event.items():
                asks={m['slug']:(best_ask(bk.get(m['yesToken'])),best_ask(bk.get(m['noToken']))) for m in ms}
                atm=min((m for m in ms if asks[m['slug']][0] and asks[m['slug']][1]),default=None,
                        key=lambda m:abs((asks[m['slug']][0][0]+1-asks[m['slug']][1][0])/2-.5))
                iv=None
                if atm:mid=(asks[atm['slug']][0][0]+1-asks[atm['slug']][1][0])/2;iv=implied_vol(spot,atm['strike'],atm['endAt']-now,mid)
                ratio=(sigma/iv) if iv else None
                self.state['vol'][f'{asset} {ev}']={'model':round(sigma,4),'implied':round(iv,4) if iv else None,'ratio':round(ratio,3) if ratio else None,'spot':spot}
                for m in ms:
                    if m['slug'] in self.open:continue
                    if sum(1 for p in self.open.values() if p['event']==ev)>=MAX_OPEN_PER_EVENT:self._skip('event position cap');continue
                    if self.open_usd()+STAKE_USD>MAX_OPEN_USD:self._skip('open exposure cap');continue
                    side,price,p,edge,why=decide(m,spot,sigma,*asks[m['slug']],now,ratio)
                    if side is None:self._skip(why);continue
                    shares=round(STAKE_USD/price,2);f=round(fee(price)*shares,5)
                    pos={**m,'side':side,'entryPrice':price,'shares':shares,'feeUsd':f,'costUsd':round(price*shares+f,5),'modelProb':round(p,5),
                         'sideChance':round(p if side=='YES' else 1-p,5),'edge':round(edge,5),'sigmaModel':round(sigma,5),'sigmaImplied':round(iv,5),
                         'volRatio':round(ratio,4),'spot':spot,'tauSec':round(m['endAt']-now),'enteredAt':now,
                         'rules':{'minPrice':MIN_PRICE,'maxPrice':MAX_PRICE,'minEdge':MIN_EDGE,'volRatioMax':VOL_RATIO_MAX,'feeRate':FEE_RATE}}
                    self.open[m['slug']]=pos;self._event(store,'ENTRY',pos,now)
        self.state.update(lastScanAt=now,events=len(events),markets=seen)
        await self.resolve(store,now)

    async def resolve(self,store,now=None):
        now=time.time() if now is None else now
        for slug,pos in list(self.open.items()):
            if now<pos['endAt']+600:continue
            try:   # Gamma leaves closed markets out unless asked; resolved ones are closed (same lookup as the main bot)
                data=await self.get(GAMMA+'/markets',{'slug':slug,'closed':'true'})
                if not (isinstance(data,list) and data):data=await self.get(GAMMA+'/markets',{'slug':slug})
            except Exception as exc:self._err('resolve',exc);continue
            m=(data or [None])[0] if isinstance(data,list) else data
            prices=[float(x) for x in (_jl((m or {}).get('outcomePrices')) or [])]
            if not m or not m.get('closed') or len(prices)!=2 or max(prices)<0.99:
                if now>pos['endAt']+7*86400:self._close(store,pos,None,now,'unresolved after 7 days')
                continue
            self._close(store,pos,'YES' if prices[0]>prices[1] else 'NO',now)
    def _close(self,store,pos,winner,now,note=None):
        won=winner==pos['side'];pnl=None if winner is None else round((pos['shares'] if won else 0.)-pos['costUsd'],5)
        row={**pos,'winner':winner,'won':won if winner else None,'pnl':pnl,'resolvedAt':now,'note':note}
        self.open.pop(pos['slug'],None)
        if pnl is not None:self.closed.append(row)
        self._event(store,'OUTCOME',row,now)

    def restore(self,events):
        """Rebuild open/closed positions from THRESHOLD_SHADOW events (oldest first)."""
        for e in events:
            row=e.get('row') or {};slug=row.get('slug')
            if not slug:continue
            if e.get('action')=='ENTRY':self.open[slug]=row
            elif e.get('action')=='OUTCOME':
                self.open.pop(slug,None)
                if row.get('pnl') is not None and not any(c['slug']==slug for c in self.closed):self.closed.append(row)
        self.state['restored']=True

    def report(self):
        import poly_audit
        rows=self.closed;pnls=[r['pnl'] for r in rows];n=len(rows)
        mean=sum(pnls)/n if n else None
        blk=poly_audit.block_bootstrap_mean([{'entryAt':r['endAt'],'pnl':r['pnl']} for r in rows],block_sec=86400) if n else None
        worse=[(r['shares'] if r['won'] else 0.)-r['shares']*(r['entryPrice']+.01)-fee(min(.99,r['entryPrice']+.01))*r['shares'] for r in rows]
        s1=sum(worse)/n if n else None
        def band(lo,hi):
            b=[r['pnl'] for r in rows if lo<=r['entryPrice']<hi];return {'trades':len(b),'pnl':round(sum(b),4),'avg':round(sum(b)/len(b),4) if b else None}
        checks={'trades':n>=PASS['minTrades'],'mean':mean is not None and mean>PASS['meanAbove'],
                'blockLower':bool(blk) and blk['lower95']>PASS['blockLowerAbove'],'stress1c':s1 is not None and s1>PASS['stress1cAbove']}
        status='PASS' if all(checks.values()) else ('COLLECTING' if n<PASS['minTrades'] else 'FAIL')
        return {'status':status,'checks':checks,'pass':PASS,'resolved':n,'open':len(self.open),'openUsd':round(self.open_usd(),2),
                'wins':sum(1 for r in rows if r['won']),'pnlUsd':round(sum(pnls),4),'avgPnlUsd':round(mean,4) if mean is not None else None,
                'equity':round(BANKROLL+sum(pnls),4),'dayBlockBootstrap':blk,'stressEntry1cWorseAvg':round(s1,4) if s1 is not None else None,
                'bands':{'20-50c':band(.2,.5),'50-80c':band(.5,.8),'80-95c':band(.8,.951)},
                'byAsset':{a:round(sum(r['pnl'] for r in rows if r['asset']==a),4) for a in ASSETS},
                'state':{k:v for k,v in self.state.items() if k!='vol'},'vol':dict(list(self.state['vol'].items())[-6:]),
                'openPositions':[{k:p.get(k) for k in ('asset','question','side','entryPrice','sideChance','edge','volRatio','endAt')} for p in list(self.open.values())[:12]]}
    def compact(self):
        r=self.report();return {k:r[k] for k in ('status','resolved','open','pnlUsd','avgPnlUsd','stressEntry1cWorseAvg')}|{'blockLower':(r['dayBlockBootstrap'] or {}).get('lower95'),'skips':dict(sorted(self.state['skipped'].items(),key=lambda kv:-kv[1])[:4]),'error':self.state['lastError']}

    async def load(self,store):
        if store is None or not getattr(store,'url',None):return
        import asyncpg
        c=await asyncpg.connect(store.url,timeout=20,command_timeout=60)
        try:rows=await c.fetch("SELECT payload::text p FROM poly_v161_events WHERE kind='THRESHOLD_SHADOW' ORDER BY t,id")
        finally:await c.close()
        self.restore([json.loads(r['p']) for r in rows])
    async def loop(self,engine,first_delay=120):
        global CURRENT;CURRENT=self
        await asyncio.sleep(first_delay);st=getattr(engine,'storage',None);loaded=False
        while True:
            try:
                if st is None or st.active:          # owner only; standby never records
                    if not loaded:
                        await self.load(st);loaded=True   # a failed load retries next scan: never re-enter blind
                    await self.scan(st)
                    print('POLY_THRESHOLD '+json.dumps(self.compact(),separators=(',',':'),default=str),flush=True)
            except Exception as exc:self._err('scan',exc);print('POLY_THRESHOLD_ERROR',self.state['lastError'],flush=True)
            await asyncio.sleep(SCAN_SEC)
