"""EdgeLab: calibrated mispricing detection, skilled-wallet intelligence and
strategy promotion for the paper engine.

Everything here is causal: a probability used for a decision is computed only
from rounds that resolved *before* that decision. Strategies start in shadow
mode and are promoted to funded paper execution only while their own
out-of-sample record passes an evidence gate. No real orders are ever placed.
"""
from __future__ import annotations
import asyncio,math,statistics,time
from collections import deque,defaultdict

DATA_API='https://data-api.polymarket.com'
VERSION='edge-1.0'

# Remaining-seconds buckets (one calibration sample per bucket per round keeps
# a single round from dominating a cell) and raw-probability bins.
TIME_EDGES=(30,60,120,180,240,301)
PROB_BINS=10
PRIOR_WEIGHT=10.0          # pseudo-rounds given to the heuristic model in each cell
EDGE_BUFFER=0.02           # extra margin above fees before calling a price a mistake
UNCERTAINTY_Z=1.0          # conservative probability = mean - z * standard error
MIN_REMAINING=20.0
MAX_ELAPSED_FOR_ENTRY=280.0
MIN_ELAPSED=15.0
MAX_SPREAD=0.03
SMART_WINDOW_SEC=120.0
KELLY_FRACTION=0.25
MAX_ALLOCATION_FRACTION=0.15
GATE_MIN_BETS=40
GATE_MIN_ROUNDS=20
GATE_WINDOW=200
SKILL_MIN_ROUNDS=6
VARIANTS=('edge','edge_smart')

def fee(p):return round(.07*p*(1-p),5)

def time_bucket(remaining):
    for i,edge in enumerate(TIME_EDGES):
        if remaining<edge:return i
    return len(TIME_EDGES)-1

def prob_bin(p):return max(0,min(PROB_BINS-1,int(float(p)*PROB_BINS)))

def number(v,default=0.):
    try:
        v=float(v);return v if math.isfinite(v) else default
    except (TypeError,ValueError):return default

class Calibrator:
    """Beta-smoothed empirical P(Up wins | raw model probability, time left)."""
    def __init__(self):
        self.cells=defaultdict(lambda:[0.,0.])   # (tb,pb) -> [rounds, up wins]
        self.brier={'heuristic':[0.,0],'calibrated':[0.,0]}
    def estimate(self,raw_up,fair_up,remaining):
        n,w=self.cells[(time_bucket(remaining),prob_bin(raw_up))]
        prior=max(.02,min(.98,number(fair_up,.5)))
        p=(w+PRIOR_WEIGHT*prior)/(n+PRIOR_WEIGHT)
        se=math.sqrt(max(p*(1-p),1e-4)/(n+PRIOR_WEIGHT))
        return {'pUp':p,'se':se,'cellRounds':int(n),'prior':prior}
    def update(self,samples,up_won):
        """samples: per time bucket, the observation recorded during the round."""
        y=1. if up_won else 0.
        for s in samples:
            # Score the estimate that was live when the sample was taken (causal).
            self.brier['heuristic'][0]+=(s['fairUp']-y)**2;self.brier['heuristic'][1]+=1
            self.brier['calibrated'][0]+=(s['pUp']-y)**2;self.brier['calibrated'][1]+=1
            cell=self.cells[(s['tb'],s['pb'])];cell[0]+=1;cell[1]+=y
    def summary(self):
        b={k:(v[0]/v[1] if v[1] else None) for k,v in self.brier.items()}
        return {'cells':len(self.cells),'samples':int(sum(v[0] for v in self.cells.values())),
                'brierHeuristic':b['heuristic'],'brierCalibrated':b['calibrated'],
                'improvement':(b['heuristic']-b['calibrated']) if None not in b.values() else None,
                'note':'Brier score (lower is better), each scored before its round was learned from.'}
    def export(self):return {'cells':[[k[0],k[1],v[0],v[1]] for k,v in self.cells.items()],'brier':self.brier}
    def restore(self,state):
        for tb,pb,n,w in (state or {}).get('cells',[]):self.cells[(int(tb),int(pb))]=[float(n),float(w)]
        for k,v in ((state or {}).get('brier') or {}).items():
            if k in self.brier and isinstance(v,list) and len(v)==2:self.brier[k]=[float(v[0]),int(v[1])]

class StrategyRecord:
    """Shadow (virtual) hold-to-resolution bets: one per asset per round."""
    def __init__(self,name):self.name=name;self.bets=deque(maxlen=2000)
    def add(self,bet):self.bets.append(bet)
    def stats(self,window=GATE_WINDOW):
        rows=list(self.bets)[-window:];r=[b['returnPerDollar'] for b in rows]
        n=len(r);mean=statistics.mean(r) if r else 0.;se=statistics.stdev(r)/math.sqrt(n) if n>1 else 1.
        rounds=len({b['roundStart'] for b in rows});wins=sum(b['won'] for b in rows)
        passed=bool(n>=GATE_MIN_BETS and rounds>=GATE_MIN_ROUNDS and mean-2*se>0)
        return {'strategy':self.name,'bets':n,'lifetimeBets':len(self.bets),'rounds':rounds,'wins':wins,
                'winRatePct':100*wins/n if n else None,'meanReturnPerDollar':mean,'meanMinus2SE':mean-2*se,
                'virtualPnlPer1Dollar':sum(r),'gatePassed':passed,
                'gate':f'>= {GATE_MIN_BETS} bets over >= {GATE_MIN_ROUNDS} rounds and mean - 2 SE > 0 (last {window} bets)'}
    def export(self):return list(self.bets)[-600:]
    def restore(self,rows):
        for b in rows or []:
            if isinstance(b,dict) and 'returnPerDollar' in b:self.bets.append(b)

def kelly_binary(p,cost):
    """Kelly fraction for buying a $1 binary at all-in cost `cost` with win probability p."""
    if not 0<cost<1:return 0.
    return max(0.,(p-cost)/(1-cost))

def size_stake(*,kelly_full,equity,allocation,loss_budget,unit_cost,min_shares,max_loss_per_dollar=1.):
    """Quarter-Kelly stake, capped at 15% of allocation and the remaining loss budget.
    Returns shares (0 means skip). A minimum-size order is allowed only if it does
    not exceed the *full* Kelly stake, so the exchange minimum never forces over-betting."""
    if unit_cost<=0 or kelly_full<=0 or equity<=0:return 0.
    full=kelly_full*equity;stake=min(KELLY_FRACTION*full,MAX_ALLOCATION_FRACTION*allocation)
    if max_loss_per_dollar>0:stake=min(stake,loss_budget/max_loss_per_dollar)
    min_cost=min_shares*unit_cost
    if stake<min_cost:
        if min_cost<=min(full,MAX_ALLOCATION_FRACTION*allocation) and min_cost*max_loss_per_dollar<=loss_budget:return float(min_shares)
        return 0.
    return math.floor(stake/unit_cost*100)/100

class WalletIntel:
    """Ranks wallets by realized results in resolved 5-minute crypto rounds and
    measures live skilled-wallet order flow in the current round."""
    def __init__(self):
        self.wallets={}                 # wallet -> aggregate
        self.studied=deque(maxlen=3000);self.studied_set=set()
        self.live={}                    # asset -> {'up':flow,'down':flow,'at':t,'trades':n}
        self.errors=0;self.last_error=None;self.last_study=0.
    @staticmethod
    def parse(rows):
        out=[]
        for t in (rows.get('data') if isinstance(rows,dict) else rows) or []:
            wallet=str(t.get('proxyWallet') or t.get('proxy_wallet') or t.get('user') or '').lower()
            outcome=str(t.get('outcome') or '').strip().lower();side=str(t.get('side') or '').upper()
            price=number(t.get('price'),-1);size=number(t.get('size'));ts=number(t.get('timestamp'))
            if ts>1e12:ts/=1000
            if wallet and outcome in ('up','down') and side in ('BUY','SELL') and 0<price<1 and size>0:
                out.append({'wallet':wallet,'outcome':outcome,'side':side,'price':price,'size':size,'t':ts,
                            'tx':str(t.get('transactionHash') or t.get('transaction_hash') or '')})
        return out
    def learn_round(self,trades,winner,round_start):
        per=defaultdict(lambda:{'pnl':0.,'buys':0,'buyUsd':0.,'entrySec':[],'prices':[],'outcomes':set(),'winnerBuys':0})
        for t in trades:
            w=per[t['wallet']];win=1. if t['outcome']==winner else 0.
            if t['side']=='BUY':
                w['pnl']+=t['size']*(win-t['price']);w['buys']+=1;w['buyUsd']+=t['size']*t['price']
                w['entrySec'].append(max(0.,t['t']-round_start));w['prices'].append(t['price']);w['outcomes'].add(t['outcome'])
                w['winnerBuys']+=int(win==1.)
            else:w['pnl']+=t['size']*(t['price']-win)
        for wallet,r in per.items():
            a=self.wallets.setdefault(wallet,{'rounds':0,'pnl':0.,'recent':[],'buys':0,'buyUsd':0.,'entrySec':[],'prices':[],'bothSides':0,'winnerBuys':0})
            a['rounds']+=1;a['pnl']+=r['pnl'];a['recent']=(a['recent']+[round(r['pnl'],4)])[-60:]
            a['buys']+=r['buys'];a['buyUsd']+=r['buyUsd'];a['winnerBuys']+=r['winnerBuys']
            a['entrySec']=(a['entrySec']+r['entrySec'])[-200:];a['prices']=(a['prices']+r['prices'])[-200:]
            a['bothSides']+=int(len(r['outcomes'])>1)
        if len(self.wallets)>30000:
            # Keep wallets with repeat history; one-off participants carry no skill evidence.
            self.wallets={k:v for k,v in sorted(self.wallets.items(),key=lambda kv:-kv[1]['rounds'])[:15000]}
    @staticmethod
    def score(a):
        x=a['recent'];n=len(x)
        if n<SKILL_MIN_ROUNDS:return None
        mean=statistics.mean(x);se=statistics.stdev(x)/math.sqrt(n) if n>1 else abs(mean) or 1.
        return mean-se if a['pnl']>0 else None
    def skilled(self,limit=150):
        scored=[(s,w) for w,a in self.wallets.items() if (s:=self.score(a)) is not None and s>0]
        return dict((w,s) for s,w in sorted(scored,reverse=True)[:limit])
    def live_flow(self,asset,trades,now,skilled):
        flow={'up':0.,'down':0.};count=0
        for t in trades:
            if t['wallet'] in skilled and now-t['t']<=SMART_WINDOW_SEC:
                flow[t['outcome']]+=t['size']*(1 if t['side']=='BUY' else -1);count+=1
        self.live[asset]={**flow,'at':now,'skilledTrades':count}
    def profile(self):
        skilled=self.skilled()
        rows=[self.wallets[w] for w in skilled]
        def med(key):
            xs=[x for a in rows for x in a[key]]
            return statistics.median(xs) if xs else None
        buys=sum(a['buys'] for a in rows)
        top=[{'wallet':w[:6]+'…'+w[-4:],'score':round(s,4),'rounds':self.wallets[w]['rounds'],'pnl':round(self.wallets[w]['pnl'],2)}
             for w,s in list(skilled.items())[:10]]
        return {'walletsTracked':len(self.wallets),'roundsStudied':len(self.studied),'skilledWallets':len(skilled),
                'skilledEntryProfile':{'medianSecondsIntoRound':med('entrySec'),'medianEntryPrice':med('prices'),
                    'buysOnEventualWinnerPct':100*sum(a['winnerBuys'] for a in rows)/buys if buys else None,
                    'roundsBuyingBothSidesPct':100*sum(a['bothSides'] for a in rows)/max(1,sum(a['rounds'] for a in rows))},
                'topWallets':top,'live':self.live,'errors':self.errors,'lastError':self.last_error,
                'rule':f'skilled = >= {SKILL_MIN_ROUNDS} studied rounds, positive total P&L and mean round P&L minus 1 SE > 0'}
    def export(self):
        keep=dict(sorted(self.wallets.items(),key=lambda kv:-kv[1]['rounds'])[:4000])
        return {'wallets':keep,'studied':list(self.studied)[-2000:]}
    def restore(self,state):
        for w,a in ((state or {}).get('wallets') or {}).items():
            if isinstance(a,dict) and 'recent' in a:self.wallets[w]=a
        for cid in (state or {}).get('studied') or []:
            if cid not in self.studied_set:self.studied.append(cid);self.studied_set.add(cid)

class EdgeLab:
    def __init__(self,engines):
        self.engines=engines;self.cal=Calibrator();self.intel=WalletIntel()
        self.strategies={v:StrategyRecord(v) for v in VARIANTS}
        self.rounds={}            # slug -> round record awaiting resolution
        self.signals={}           # asset -> latest decision for UI/orb
        self.trades=deque(maxlen=5000)   # funded paper closes for the readiness scorecard
        self.tasks=[];self.client=None;self.errors=0;self.restored=False
        self._pending_study=deque(maxlen=400)   # (conditionId, winner, roundStart, asset)
    # ---------- live decision -------------------------------------------------
    def _round(self,asset,market):
        slug=market['slug']
        r=self.rounds.get(slug)
        if r is None:
            r=self.rounds[slug]={'asset':asset,'slug':slug,'start':number(market.get('startTs')),'end':number(market.get('endTs')),
                'conditionId':market.get('conditionId'),'samples':{},'bets':{}}
        return r
    def evaluate(self,asset,e,m,now=None):
        """Called by the engine every loop. Returns the best executable mispricing or None."""
        now=time.time() if now is None else now
        market=e.market or {}
        def out(reason,**kw):
            self.signals[asset]={'decision':reason,'at':now,**kw};return None
        if not m or not market.get('slug') or not e.open_trusted:return out('waiting for trusted opening reference')
        if not market.get('acceptingOrders'):return out('market not accepting orders')
        if any(e.feed.get(k)!='live' for k in ('market','clob','rtds')):return out('feeds not live')
        if max(now-e.spot_ts,now-e.twap_ts)>=5:return out('oracle stale')
        remaining=number(market.get('endTs'))-now;elapsed=now-number(market.get('startTs'))
        r=self._round(asset,market)
        est=self.cal.estimate(m.get('rawFairUp',m.get('fairUp',.5)),m.get('fairUp',.5),remaining)
        tb=time_bucket(remaining)
        if elapsed>=MIN_ELAPSED and tb not in r['samples']:
            r['samples'][tb]={'tb':tb,'pb':prob_bin(m.get('rawFairUp',.5)),'fairUp':number(m.get('fairUp'),.5),'pUp':est['pUp'],'t':now}
        smart=self.intel.live.get(asset) or {}
        smart_fresh=now-number(smart.get('at'))<30
        sides=[]
        for side in ('up','down'):
            b=e.bbo[side];ask=b.get('ask');bid=b.get('bid')
            p=est['pUp'] if side=='up' else 1-est['pUp'];pc=p-UNCERTAINTY_Z*est['se']
            info={'side':side.upper(),'p':p,'pConservative':pc}
            if ask is None or bid is None:sides.append({**info,'reason':'no book'});continue
            ask=float(ask);bid=float(bid);cost=ask+fee(ask)
            edge=p-cost-EDGE_BUFFER;cedge=pc-cost-EDGE_BUFFER
            flow=number(smart.get(side))-number(smart.get('down' if side=='up' else 'up')) if smart_fresh else 0.
            info.update(ask=ask,bid=bid,cost=cost,edge=edge,conservativeEdge=cedge,smartFlow=flow)
            min_sh=max(5.,number(market.get('minOrderSize')))
            if now-e.book_observed.get(side,0)>3:info['reason']='book older than 3s'
            elif not .05<=ask<=.95 or bid>=ask:info['reason']='price outside band'
            elif ask-bid>MAX_SPREAD+1e-9:info['reason']='spread above 3c'
            elif number(b.get('askSize'))<min_sh:info['reason']='insufficient ask depth'
            elif cedge<=0:info['reason']='no conservative edge after fees'
            else:info['reason']='MISPRICED'
            sides.append(info)
        best=max(sides,key=lambda s:s.get('conservativeEdge',-9))
        window=elapsed>=MIN_ELAPSED and remaining>=MIN_REMAINING and elapsed<=MAX_ELAPSED_FOR_ENTRY
        candidate=None
        if window and best.get('reason')=='MISPRICED':
            candidate={**best,'remaining':remaining,'cellRounds':est['cellRounds'],'se':est['se'],'marketSlug':market['slug'],
                       'smartAligned':best['smartFlow']>0,'kellyFull':kelly_binary(best['pConservative'],best['cost'])}
            for v in VARIANTS:
                if v not in r['bets'] and (v=='edge' or candidate['smartAligned']):
                    r['bets'][v]={'side':best['side'],'ask':best['ask'],'cost':best['cost'],'p':best['p'],'pConservative':best['pConservative'],
                                  'edge':best['edge'],'smartFlow':best['smartFlow'],'t':now,'remaining':remaining}
        reason='MISPRICED' if candidate else (best.get('reason') if window else 'outside entry window')
        self.signals[asset]={'decision':reason,'at':now,'pUp':est['pUp'],'se':est['se'],'cellRounds':est['cellRounds'],
            'heuristicFairUp':number(m.get('fairUp'),.5),'best':best,'sides':sides,'smart':smart if smart_fresh else None,
            'promoted':self.promoted()}
        return candidate
    def promoted(self):
        s={v:self.strategies[v].stats() for v in VARIANTS}
        passing=[v for v in VARIANTS if s[v]['gatePassed']]
        return max(passing,key=lambda v:s[v]['meanMinus2SE']) if passing else None
    def tradable(self,candidate):
        v=self.promoted()
        if not v or not candidate:return None
        if v=='edge_smart' and not candidate.get('smartAligned'):return None
        return v
    def hold_exit(self,asset,side,bid):
        """EV-consistent exit for a held binary: sell only when the market pays more than our fair value."""
        sig=self.signals.get(asset) or {}
        if bid is None or 'pUp' not in sig or time.time()-number(sig.get('at'))>3:return None
        p=sig['pUp'] if side=='UP' else 1-sig['pUp']
        return 'market price exceeded calibrated fair value' if float(bid)-fee(float(bid))>=p+.01 else None
    # ---------- resolution & learning -----------------------------------------
    def settle(self,slug,winner):
        r=self.rounds.pop(slug,None)
        if not r or winner not in ('up','down'):return
        up=winner=='up'
        self.cal.update(list(r['samples'].values()),up)
        for v,b in r['bets'].items():
            won=b['side'].lower()==winner
            self.strategies[v].add({**b,'asset':r['asset'],'marketSlug':slug,'roundStart':r['start'],'won':won,
                'returnPerDollar':((1. if won else 0.)-b['cost'])/b['cost'],'settledAt':time.time()})
        if r.get('conditionId'):self._pending_study.append((r['conditionId'],winner,r['start'],r['asset']))
    async def _get(self,url,params=None):
        import httpx
        if self.client is None:self.client=httpx.AsyncClient(timeout=12,headers={'User-Agent':'POLY-EDGE-RESEARCH/edge'})
        r=await self.client.get(url,params=params);r.raise_for_status();return r.json()
    async def resolver(self):
        while True:
            try:
                now=time.time()
                due=[r for r in list(self.rounds.values()) if r['end']<now-15][:12]
                for r in due:
                    if r['end']<now-1800:self.rounds.pop(r['slug'],None);continue
                    winner=await self.engines[r['asset']]._resolved_winner(r['slug'])
                    if winner:self.settle(r['slug'],winner)
            except asyncio.CancelledError:raise
            except Exception:self.errors+=1
            await asyncio.sleep(20)
    async def studier(self):
        while True:
            try:
                batch=[]
                while self._pending_study and len(batch)<16:
                    item=self._pending_study.popleft()
                    if item[0] not in self.intel.studied_set:batch.append(item)
                for cid,winner,start,asset in batch:
                    rows=await self._get(DATA_API+'/trades',{'market':cid,'limit':1000,'takerOnly':'false'})
                    self.intel.learn_round(self.intel.parse(rows),winner,start)
                    self.intel.studied.append(cid);self.intel.studied_set.add(cid)
                    await asyncio.sleep(.5)
                self.intel.last_study=time.time()
            except asyncio.CancelledError:raise
            except Exception as exc:self.intel.errors+=1;self.intel.last_error=type(exc).__name__
            await asyncio.sleep(30)
    async def watcher(self):
        while True:
            try:
                skilled=self.intel.skilled()
                if skilled:
                    now=time.time()
                    for asset,e in self.engines.items():
                        cid=(e.market or {}).get('conditionId')
                        if not cid:continue
                        rows=await self._get(DATA_API+'/trades',{'market':cid,'limit':300,'takerOnly':'false'})
                        self.intel.live_flow(asset,self.intel.parse(rows),now,skilled)
            except asyncio.CancelledError:raise
            except Exception as exc:self.intel.errors+=1;self.intel.last_error=type(exc).__name__
            await asyncio.sleep(10)
    def start(self):
        if self.tasks:return
        self.tasks=[asyncio.create_task(self.resolver(),name='edge-resolver'),
                    asyncio.create_task(self.studier(),name='edge-studier'),
                    asyncio.create_task(self.watcher(),name='edge-watcher')]
    async def close(self):
        for t in self.tasks:t.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True);self.tasks=[]
        if self.client:await self.client.aclose();self.client=None
    # ---------- scorecard -----------------------------------------------------
    def on_trade(self,row):
        if isinstance(row,dict) and 'netPnlUsd' in row:
            self.trades.append({'pnl':float(row['netPnlUsd']),'at':float(row.get('closedAt') or time.time()),'strategy':row.get('strategy'),'asset':row.get('asset')})
    def readiness(self,capital):
        rows=list(self.trades);pnl=[r['pnl'] for r in rows];n=len(pnl)
        mean=statistics.mean(pnl) if pnl else 0.;se=statistics.stdev(pnl)/math.sqrt(n) if n>1 else 0.
        eq=peak=dd=0.
        for x in pnl:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
        days=(rows[-1]['at']-rows[0]['at'])/86400 if n>1 else 0.
        checks=[{'check':'At least 300 funded paper trades','value':n,'pass':n>=300},
                {'check':'Net paper P&L after fees is positive','value':round(sum(pnl),2),'pass':sum(pnl)>0},
                {'check':'Average trade minus 2 standard errors > 0','value':round(mean-2*se,4) if n>1 else None,'pass':n>1 and mean-2*se>0},
                {'check':'Max drawdown under 10% of paper capital','value':round(dd,2),'pass':dd<.10*capital},
                {'check':'Track record spans at least 14 days','value':round(days,1),'pass':days>=14},
                {'check':'A strategy currently passes its promotion gate','value':self.promoted(),'pass':self.promoted() is not None}]
        return {'readyForRealMoney':all(c['pass'] for c in checks),'checks':checks,'liveTradingEnabled':False,
                'note':'Paper-only build. A live order adapter is not installed; switching to real money stays a deliberate owner decision.'}
    def snapshot(self,capital=200.):
        return {'version':VERSION,'mode':'PROMOTED' if self.promoted() else 'SHADOW','promotedStrategy':self.promoted(),
                'strategies':{v:self.strategies[v].stats() for v in VARIANTS},'calibration':self.cal.summary(),
                'pendingRounds':len(self.rounds),'signals':self.signals,'wallets':self.intel.profile(),
                'readiness':self.readiness(capital),'errors':self.errors,
                'sizing':{'kellyFraction':KELLY_FRACTION,'maxAllocationFraction':MAX_ALLOCATION_FRACTION,
                          'rule':'quarter-Kelly on conservative probability, capped at 15% of the asset budget and the remaining loss budget'}}
    def export(self):
        return {'version':VERSION,'savedAt':time.time(),'calibration':self.cal.export(),
                'strategies':{v:s.export() for v,s in self.strategies.items()},'wallets':self.intel.export(),
                'trades':list(self.trades)[-3000:]}
    def restore(self,state):
        if not isinstance(state,dict) or state.get('version')!=VERSION:return False
        self.cal=Calibrator();self.cal.restore(state.get('calibration'))
        self.strategies={v:StrategyRecord(v) for v in VARIANTS}
        for v,rows in (state.get('strategies') or {}).items():
            if v in self.strategies:self.strategies[v].restore(rows)
        self.intel.restore(state.get('wallets'))
        self.trades.clear();self.trades.extend(r for r in state.get('trades') or [] if isinstance(r,dict) and 'pnl' in r)
        self.restored=True;return True
