"""Research audit of the paper strategy: is the paper P&L real, and is the rule-selection procedure
predictive? Read-only. It never trades and never changes a rule; it reports evidence.

1. Execution audit   - every closed trade's fill is checked against the simulator's own contract:
                       Polymarket taker fee C*0.07*p*(1-p) on both legs, latency >= the simulated 350 ms,
                       entry slippage vs the quoted ask, stop overshoot, P&L accounting, hold limits.
2. Walk-forward test - rules (entry-price ceiling, estimated-edge floor) are chosen on older trades only
                       and scored on the next block; repeated forward in time. Compared with taking every
                       trade and with today's fixed rules on the same blocks.
3. Uncertainty       - seeded bootstrap 95% interval of the mean P&L per trade.
4. Model vs market   - shadow hold-to-resolution bets: Brier, log loss and a reliability table for the
                       model probability and for the market price (the baseline it must beat).
5. Pass/fail         - criteria fixed in advance (PASS_CRITERIA); the verdict is computed, never tuned.

Limitation: trades exist only where an earlier rule set chose to enter, so the walk-forward measures
rule *selection among taken trades*, not every market moment. Entry/exit fills are paper fills from
live books, not exchange fills.
"""
from __future__ import annotations
import math,random,time

FEE_RATE=0.07
SIM_LATENCY_MS=350
TICK=0.01
MAX_HOLD_SEC=30
GRID_MAX_ENTRY=(0.35,0.40,0.45,0.50,0.60,1.01)
GRID_MIN_EDGE=(0.0,0.06,0.09,0.12,0.15)
MIN_TRAIN=30
FOLD_SIZE=15
MIN_TRAIN_SELECTED=10
BOOTSTRAP_SAMPLES=1000
SEED=20261025
# Fixed in advance (owner research note, 2026-10-25). A rule set may be called proven only if all hold.
PASS_CRITERIA={'minOutOfSampleTrades':60,'meanPnlAbove':0.0,'bootstrapLower95Above':0.0,
               'minShareOfFoldsNonNegative':0.6,'maxDrawdownUsdBelow':10.0}

def fee(p):return FEE_RATE*p*(1-p)

def metrics(pnls):
    n=len(pnls)
    if not n:return {'trades':0,'winRatePct':None,'netPnlUsd':0.,'avgPnlUsd':None,'avgWinUsd':None,'avgLossUsd':None,
                     'profitFactor':None,'expectancyUsd':None,'sharpePerTrade':None,'maxDrawdownUsd':0.}
    w=[x for x in pnls if x>0];l=[x for x in pnls if x<=0];mean=math.fsum(pnls)/n
    sd=math.sqrt(math.fsum((x-mean)**2 for x in pnls)/(n-1)) if n>1 else None
    eq=peak=dd=0.
    for x in pnls:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    return {'trades':n,'winRatePct':round(100*len(w)/n,1),'netPnlUsd':round(math.fsum(pnls),4),'avgPnlUsd':round(mean,4),
            'avgWinUsd':round(math.fsum(w)/len(w),4) if w else None,'avgLossUsd':round(math.fsum(l)/len(l),4) if l else None,
            'profitFactor':round(math.fsum(w)/-math.fsum(l),3) if l and math.fsum(l)<0 else None,
            'expectancyUsd':round(mean,4),'sharpePerTrade':round(mean/sd,3) if sd else None,'maxDrawdownUsd':round(dd,4)}

def bootstrap_mean(pnls,samples=BOOTSTRAP_SAMPLES,seed=SEED):
    n=len(pnls)
    if n<5:return None
    rng=random.Random(seed);means=sorted(math.fsum(rng.choice(pnls) for _ in range(n))/n for _ in range(samples))
    return {'lower95':round(means[int(.025*samples)],4),'median':round(means[samples//2],4),'upper95':round(means[int(.975*samples)-1],4),
            'probMeanAbove0':round(sum(m>0 for m in means)/samples,3),'samples':samples,'seed':seed}

# ---------------------------------------------------------------- 1. execution audit
def execution_audit(trades):
    """trades: attribution rows (poly_attribution.closed_trade)."""
    flags={'feeMismatch':[],'latencyTooFast':[],'adverseSlippage':[],'stopOvershoot':[],'accountingError':[],'holdTooLong':[],'priceOutOfRange':[]}
    fees=slip=over=gross=0.;lat=[]
    for t in trades:
        try:
            q=float(t['quantity']);e=float(t['entryPrice']);x=float(t['exitPrice']);tid=t.get('id')
        except (KeyError,TypeError,ValueError):continue
        expected=q*(fee(e)+fee(x));paid=float(t.get('feesUsd') or 0);fees+=paid;gross+=float(t.get('grossPnlUsd') or 0)
        # VWAP exits pay the fee per filled level, so allow one tick of price difference plus rounding.
        if abs(paid-expected)>q*FEE_RATE*TICK+1e-4:flags['feeMismatch'].append(tid)
        ms=float(t.get('entryLatencyMs') or 0)
        if ms:lat.append(ms)
        if 0<ms<SIM_LATENCY_MS-1:flags['latencyTooFast'].append(tid)
        s=float(t.get('entrySlippageUsd') or 0);slip+=s
        if s>q*TICK+1e-9:flags['adverseSlippage'].append(tid)
        o=float(t.get('stopOvershootUsd') or 0);over+=o
        if o>0.05:flags['stopOvershoot'].append(tid)
        if abs(float(t.get('accountingErrorUsd') or 0))>1e-6:flags['accountingError'].append(tid)
        if str(t.get('strategy'))=='MOMENTUM_SCALP' and float(t.get('holdSec') or 0)>MAX_HOLD_SEC+5:flags['holdTooLong'].append(tid)
        if not (0<e<1 and 0<x<1):flags['priceOutOfRange'].append(tid)
    n=len(trades)
    return {'trades':n,'flagCounts':{k:len(v) for k,v in flags.items()},'flaggedIds':{k:v[:10] for k,v in flags.items() if v},
            'totals':{'feesUsd':round(fees,4),'grossPnlUsd':round(gross,4),'feesShareOfGrossPct':round(100*fees/abs(gross),1) if gross else None,
                      'entrySlippageUsd':round(slip,4),'stopOvershootUsd':round(over,4)},
            'latencyMs':{'median':round(sorted(lat)[len(lat)//2],1) if lat else None,'max':round(max(lat),1) if lat else None},
            'clean':n>0 and not any(flags.values()),
            'checks':'fee = C*0.07*p*(1-p) both legs (Polymarket Crypto rate); entry latency >= 350 ms simulated; slippage <= 1 tick; '
                     'stop overshoot <= $0.05; net = gross - fees; momentum hold <= 35 s; prices inside (0,1)'}

# ---------------------------------------------------------------- 2. walk-forward
def _passes(r,max_entry,min_edge):
    e=r.get('entry');k=r.get('estEdge')
    return e is not None and float(e)<max_entry and (min_edge<=0 or (k is not None and float(k)>=min_edge-1e-9))

def _select(train):
    best=None
    for me in GRID_MAX_ENTRY:
        for mk in GRID_MIN_EDGE:
            sel=[float(r['pnl']) for r in train if _passes(r,me,mk)]
            if len(sel)<MIN_TRAIN_SELECTED:continue
            score=math.fsum(sel)/len(sel)
            # Ties go to the looser rule (more trades, less fitted).
            key=(round(score,6),len(sel))
            if best is None or key>best[0]:best=(key,me,mk)
    return (best[1],best[2]) if best else (1.01,0.0)

def walk_forward(rows,current=(0.45,0.12)):
    rows=sorted((r for r in rows if r.get('pnl') is not None and r.get('entryAt')),key=lambda r:float(r['entryAt']))
    folds=[];oos=[];base=[];fixed=[]
    start=MIN_TRAIN
    while start<len(rows):
        train=rows[:start];test=rows[start:start+FOLD_SIZE]
        me,mk=_select(train)
        chosen=[float(r['pnl']) for r in test if _passes(r,me,mk)]
        allp=[float(r['pnl']) for r in test];cur=[float(r['pnl']) for r in test if _passes(r,*current)]
        oos+=chosen;base+=allp;fixed+=cur
        folds.append({'fold':len(folds)+1,'trainTrades':len(train),'testFrom':time.strftime('%m-%d %H:%M',time.gmtime(float(test[0]['entryAt']))),
                      'testTo':time.strftime('%m-%d %H:%M',time.gmtime(float(test[-1]['entryAt']))),
                      'chosenRule':{'maxEntryPrice':me if me<1 else None,'minEstimatedEdge':mk},
                      'selected':metrics(chosen),'takeAll':metrics(allp),'currentRules':metrics(cur)})
        start+=FOLD_SIZE
    nonneg=[f for f in folds if f['selected']['trades'] and f['selected']['netPnlUsd']>=0]
    traded=[f for f in folds if f['selected']['trades']]
    return {'method':f'expanding window: train on all earlier trades (>= {MIN_TRAIN}), choose entry-price ceiling and edge floor '
                     f'from a fixed grid by best mean P&L, test on the next {FOLD_SIZE} trades, roll forward',
            'grid':{'maxEntryPrice':[x if x<1 else None for x in GRID_MAX_ENTRY],'minEstimatedEdge':list(GRID_MIN_EDGE)},
            'folds':folds,'outOfSample':metrics(oos),'baselineTakeAll':metrics(base),'currentRulesSameBlocks':metrics(fixed),
            'foldsTraded':len(traded),'foldsNonNegative':len(nonneg),'outOfSampleBootstrap':bootstrap_mean(oos),'_oos':oos}

# ---------------------------------------------------------------- 4. model vs market
def _unique_bets(edge):
    seen=set();out=[]
    for name in ('edge','edge_smart','edge_cal'):
        rec=(getattr(edge,'strategies',{}) or {}).get(name)
        for b in (list(rec.bets) if rec else []):
            try:k=(b.get('marketSlug'),b.get('side'),round(float(b.get('t') or 0)));p=float(b['p']);a=float(b['ask']);w=bool(b['won'])
            except (KeyError,TypeError,ValueError):continue
            if k in seen or not (0<p<1 and 0<a<1):continue
            seen.add(k);out.append((p,a,w,float(b.get('returnPerDollar') or 0)))
    return out

def calibration(edge):
    bets=_unique_bets(edge)
    if not bets:return {'bets':0}
    def score(idx):
        brier=math.fsum((b[idx]-(1. if b[2] else 0.))**2 for b in bets)/len(bets)
        ll=-math.fsum(math.log(min(1-1e-6,max(1e-6,b[idx] if b[2] else 1-b[idx]))) for b in bets)/len(bets)
        bins=[]
        for lo in [i/10 for i in range(10)]:
            cell=[b for b in bets if lo<=b[idx]<lo+.1]
            if cell:bins.append({'range':f'{lo:.1f}-{lo+.1:.1f}','bets':len(cell),'predictedPct':round(100*math.fsum(b[idx] for b in cell)/len(cell),1),
                                 'actualWinPct':round(100*sum(b[2] for b in cell)/len(cell),1)})
        return {'brier':round(brier,4),'logLoss':round(ll,4),'reliability':bins}
    model=score(0);market=score(1)
    fav=[b[3] for b in bets if b[1]>=.5];dog=[b[3] for b in bets if b[1]<.5]
    return {'bets':len(bets),'model':model,'marketPrice':market,
            'brierSkillVsMarket':round(1-model['brier']/market['brier'],4) if market['brier'] else None,
            'modelBeatsMarket':model['brier']<market['brier'],
            'baselines':{'betsOnFavourite(ask>=50c)':metrics(fav),'betsOnUnderdog(ask<50c)':metrics(dog)},
            'note':'Brier/log loss: lower is better. Skill > 0 means the model forecasts outcomes better than the market price.'}

# ---------------------------------------------------------------- 5. verdict + report
def verdict(wf):
    o=wf['outOfSample'];b=wf.get('outOfSampleBootstrap') or {};c=PASS_CRITERIA;reasons=[]
    if o['trades']<c['minOutOfSampleTrades']:reasons.append(f"only {o['trades']} out-of-sample trades (need {c['minOutOfSampleTrades']})")
    if (o['avgPnlUsd'] or 0)<=c['meanPnlAbove']:reasons.append(f"out-of-sample mean {o['avgPnlUsd']} is not above 0")
    if b.get('lower95') is None or b['lower95']<=c['bootstrapLower95Above']:reasons.append(f"bootstrap 95% lower bound {b.get('lower95')} is not above 0")
    share=wf['foldsNonNegative']/wf['foldsTraded'] if wf['foldsTraded'] else 0
    if share<c['minShareOfFoldsNonNegative']:reasons.append(f"{wf['foldsNonNegative']}/{wf['foldsTraded']} folds non-negative (need {int(c['minShareOfFoldsNonNegative']*100)}%)")
    if o['maxDrawdownUsd']>=c['maxDrawdownUsdBelow']:reasons.append(f"max drawdown ${o['maxDrawdownUsd']} (limit ${c['maxDrawdownUsdBelow']})")
    return {'status':'PASS' if not reasons else 'FAIL','reasons':reasons,'criteria':c}

def gather(engine):
    """Copy the inputs on the event-loop thread (cheap) so the heavy part can run in a worker thread."""
    import poly_devlog,poly_momentum as pm
    edge=getattr(engine,'edge',None)
    bets={n:[dict(b) for b in rec.bets] for n,rec in (getattr(edge,'strategies',{}) or {}).items()}
    return {'rows':poly_devlog.trade_rows(engine),'attribution':[dict(r) for r in list(getattr(getattr(engine,'attribution',None),'rows',[]) or [])],
            'bets':bets,'rules':(pm.MAX_ENTRY_PRICE,pm.MIN_EDGE),'rulesLiveAt':pm.RULES_LIVE_AT}

class _Bets:
    def __init__(self,bets):self.bets=bets
class _Edge:
    def __init__(self,bets):self.strategies={n:_Bets(b) for n,b in bets.items()}

def analyse(inp):
    rows=[r for r in inp['rows'] if r.get('mode') in (None,'MOMENTUM_SCALP')]
    live=[float(r['pnl']) for r in rows if r.get('pnl') is not None and float(r.get('entryAt') or 0)>=inp['rulesLiveAt']]
    wf=walk_forward(rows,inp['rules']);wf.pop('_oos',None)
    return {'generatedAt':time.time(),'execution':execution_audit(inp['attribution']),'walkForward':wf,
            'liveRules':{'metrics':metrics(live),'bootstrap':bootstrap_mean(live)},
            'modelVsMarket':calibration(_Edge(inp['bets'])),'verdict':verdict(wf),'about':__doc__.strip().split('\n\n')[0]}

def audit_report(engine):
    return analyse(gather(engine))

def compact(rep):
    wf=rep['walkForward'];o=wf['outOfSample'];b=wf.get('outOfSampleBootstrap') or {};ex=rep['execution'];mm=rep['modelVsMarket']
    return {'verdict':rep['verdict']['status'],'why':rep['verdict']['reasons'][:3],
            'oos':[o['trades'],o['avgPnlUsd'],o['netPnlUsd'],b.get('lower95'),b.get('upper95')],
            'takeAll':[wf['baselineTakeAll']['trades'],wf['baselineTakeAll']['avgPnlUsd']],
            'folds':f"{wf['foldsNonNegative']}/{wf['foldsTraded']}",
            'live':[rep['liveRules']['metrics']['trades'],rep['liveRules']['metrics']['avgPnlUsd'],(rep['liveRules']['bootstrap'] or {}).get('lower95')],
            'exec':{'trades':ex['trades'],'clean':ex['clean'],'flags':{k:v for k,v in ex['flagCounts'].items() if v}},
            'model':[mm.get('bets'),(mm.get('model') or {}).get('brier'),(mm.get('marketPrice') or {}).get('brier'),mm.get('brierSkillVsMarket')]}
