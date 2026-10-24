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
import copy,math,random,time

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
               'minShareOfFoldsNonNegative':0.6,'maxDrawdownUsdBelow':10.0,'stressOneTickMeanAbove':0.0}

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
ERROR_FLAGS=('feeMismatch','latencyTooFast','accountingError','holdTooLong','priceOutOfRange')
RISK_FLAGS=('stopOvershoot','adverseSlippage','heldToSettlement')

def _settled(t):
    return 'settlement' in str(t.get('exitReason') or '').lower()

def execution_audit(trades):
    """trades: attribution rows (poly_attribution.closed_trade).
    Errors break the simulator's contract; market-risk flags are realistic frictions worth knowing
    (a stop that fills worse than its level after the 350 ms latency, a scalp that could not exit and
    was held to settlement). Settled trades close at exactly 0 or 1 with no exit fee."""
    flags={k:[] for k in ERROR_FLAGS+RISK_FLAGS}
    fees=slip=over=gross=0.;lat=[]
    for t in trades:
        try:
            q=float(t['quantity']);e=float(t['entryPrice']);x=float(t['exitPrice']);tid=t.get('id')
        except (KeyError,TypeError,ValueError):continue
        settled=_settled(t)
        expected=q*(fee(e)+(0. if settled else fee(x)));paid=float(t.get('feesUsd') or 0);fees+=paid;gross+=float(t.get('grossPnlUsd') or 0)
        # VWAP exits pay the fee per filled level, so allow one tick of price difference plus rounding.
        if abs(paid-expected)>q*FEE_RATE*TICK+1e-4:flags['feeMismatch'].append(tid)
        ms=float(t.get('entryLatencyMs') or 0)
        if ms:lat.append(ms)
        if 0<ms<SIM_LATENCY_MS-1:flags['latencyTooFast'].append(tid)
        sl=float(t.get('entrySlippageUsd') or 0);slip+=sl
        if sl>q*TICK+1e-9:flags['adverseSlippage'].append(tid)
        o=float(t.get('stopOvershootUsd') or 0);over+=o
        if o>0.05:flags['stopOvershoot'].append(tid)
        if abs(float(t.get('accountingErrorUsd') or 0))>1e-6:flags['accountingError'].append(tid)
        scalp=str(t.get('strategy'))=='MOMENTUM_SCALP'
        if scalp and settled:flags['heldToSettlement'].append(tid)
        elif scalp and float(t.get('holdSec') or 0)>MAX_HOLD_SEC+5:flags['holdTooLong'].append(tid)
        if not (0<e<1 and (0<=x<=1 if settled else 0<x<1)):flags['priceOutOfRange'].append(tid)
    n=len(trades)
    return {'trades':n,'flagCounts':{k:len(v) for k,v in flags.items()},'flaggedIds':{k:v[:10] for k,v in flags.items() if v},
            'errors':{k:len(flags[k]) for k in ERROR_FLAGS},'marketRisk':{k:len(flags[k]) for k in RISK_FLAGS},
            'totals':{'feesUsd':round(fees,4),'grossPnlUsd':round(gross,4),'feesShareOfGrossPct':round(100*fees/abs(gross),1) if gross else None,
                      'entrySlippageUsd':round(slip,4),'stopOvershootUsd':round(over,4)},
            'latencyMs':{'median':round(sorted(lat)[len(lat)//2],1) if lat else None,'max':round(max(lat),1) if lat else None},
            'clean':n>0 and not any(flags[k] for k in ERROR_FLAGS),
            'checks':'errors: fee = C*0.07*p*(1-p) per leg (no exit fee at settlement), entry latency >= 350 ms, net = gross - fees, '
                     'momentum hold <= 35 s unless settled, prices in range; market risk: stop overshoot > $0.05, slippage > 1 tick, scalp held to settlement'}

# ---------------------------------------------------------------- 1b. ledger reconciliation
def ledger_reconcile(sessions,epoch,rules_live_at):
    """sessions: [{'lane','live','paper':{...}}]. Invariants per session: closed-trade P&L from the event log
    equals realizedPnl and the close count equals trades (only checkable when the 500-event log is complete);
    for a running session without inventory: startingBalance + realizedPnl == cash + open position cost."""
    out=[];bad=0
    for s in sessions:
        p=s.get('paper') or {};ev=p.get('events') or []
        closes=[e for e in ev if e.get('action') in ('SELL','SETTLE') and e.get('pnl') is not None]
        complete=len(ev)<500
        row={'lane':s['lane'],'sessionId':p.get('sessionId'),'live':bool(s.get('live')),'startedAt':p.get('startedAt'),
             'trades':int(p.get('trades') or 0),'realizedPnl':round(float(p.get('realizedPnl') or 0),5),'eventLogComplete':complete,'problems':[]}
        if complete:
            if len(closes)!=row['trades']:row['problems'].append(f"{len(closes)} close events vs {row['trades']} trades")
            if abs(math.fsum(float(e['pnl']) for e in closes)-row['realizedPnl'])>1e-4:row['problems'].append('event P&L sum differs from realizedPnl')
        inv=p.get('inventory')
        if s.get('live') and not (isinstance(inv,dict) and (inv.get('upLots') or inv.get('downLots'))) and p.get('cash') is not None:
            pos=p.get('position') or {}
            lhs=float(p.get('startingBalance') or 0)+float(p.get('realizedPnl') or 0)
            rhs=float(p['cash'])+float(pos.get('entryTotalCost') or 0)
            if abs(lhs-rhs)>1e-4:row['problems'].append(f'start+realized {lhs:.4f} != cash+open cost {rhs:.4f}')
        bad+=bool(row['problems']);out.append(row)
    inep=[r for r in out if float(r['startedAt'] or 0)>=epoch]
    return {'sessions':len(out),'sessionsWithProblems':bad,'problems':[r for r in out if r['problems']][:10],
            'sinceFreshStart':{'sessions':len(inep),'trades':sum(r['trades'] for r in inep),'realizedPnl':round(math.fsum(r['realizedPnl'] for r in inep),4)},
            'windows':{'freshStartUtc':time.strftime('%Y-%m-%d %H:%M',time.gmtime(epoch)) if epoch else None,
                       'rulesLiveUtc':time.strftime('%Y-%m-%d %H:%M',time.gmtime(rules_live_at)),
                       'note':'"since fresh start" counts sessions started after the reset; "since rules live" counts trades entered after the rule change. '
                              'They are different windows; their difference is the trades between the two times.'}}

def window_counts(rows,epoch,rules_live_at):
    t=lambda r:float(r.get('entryAt') or 0)
    a=[r for r in rows if t(r)>=epoch] if epoch else [];b=[r for r in rows if t(r)>=rules_live_at]
    lo,hi=sorted((epoch or rules_live_at,rules_live_at))
    gap=[r for r in rows if lo<=t(r)<hi]
    return {'sinceFreshStart':len(a),'sinceRulesLive':len(b),'betweenTheTwoTimes':len(gap),
            'explained':abs(abs(len(a)-len(b))-len(gap))==0}

# ---------------------------------------------------------------- 3b. block bootstrap, stress, breakdowns
def block_bootstrap_mean(rows,samples=BOOTSTRAP_SAMPLES,seed=SEED,block_sec=300):
    """Resample whole 5-minute market windows: trades in the same round share one price path."""
    blocks={}
    for r in rows:blocks.setdefault(int(float(r['entryAt'])//block_sec),[]).append(float(r['pnl']))
    bl=list(blocks.values());n=sum(len(b) for b in bl)
    if len(bl)<5:return None
    rng=random.Random(seed);means=[]
    for _ in range(samples):
        pick=[rng.choice(bl) for _ in bl];means.append(math.fsum(x for b in pick for x in b)/max(1,sum(len(b) for b in pick)))
    means.sort()
    return {'lower95':round(means[int(.025*samples)],4),'upper95':round(means[int(.975*samples)-1],4),'blocks':len(bl),'trades':n,
            'probMeanAbove0':round(sum(m>0 for m in means)/samples,3),'blockSec':block_sec,'seed':seed}

def stress(rows,shares=5.0):
    """Pessimistic frictions: each exit 1 or 2 ticks worse (fees recomputed), as if depth were thinner."""
    out={}
    for ticks in (0,1,2):
        pn=[]
        for r in rows:
            x=r.get('exit');pnl=float(r['pnl'])
            if x is None or ticks==0 or float(x) in (0.,1.):pn.append(pnl);continue
            x=float(x);w=max(.01,x-ticks*TICK);pn.append(pnl-shares*((x-w)-(fee(x)-fee(w))))
        m=metrics(pn);out[f'exitWorseBy{ticks}Tick']={'trades':m['trades'],'avgPnlUsd':m['avgPnlUsd'],'netPnlUsd':m['netPnlUsd']}
    return out

def breakdowns(rows):
    by_asset={};by_hour={};pn=[float(r['pnl']) for r in rows]
    for r in rows:
        by_asset.setdefault(r.get('asset'),[]).append(float(r['pnl']))
        h=int(time.gmtime(float(r['entryAt'])).tm_hour)//6*6;by_hour.setdefault(f'{h:02d}-{h+6:02d} UTC',[]).append(float(r['pnl']))
    streak=cur=0
    for x in pn:cur=cur+1 if x<=0 else 0;streak=max(streak,cur)
    srt=sorted(pn)
    return {'byAsset':{k:metrics(v) for k,v in by_asset.items()},'byTimeOfDay':{k:metrics(v) for k,v in sorted(by_hour.items())},
            'medianTradeUsd':round(srt[len(srt)//2],4) if srt else None,'longestLosingStreak':streak}

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
    folds=[];oos=[];base=[];fixed=[];oos_rows=[]
    start=MIN_TRAIN
    while start<len(rows):
        train=rows[:start];test=rows[start:start+FOLD_SIZE]
        me,mk=_select(train)
        chosen=[float(r['pnl']) for r in test if _passes(r,me,mk)]
        allp=[float(r['pnl']) for r in test];cur=[float(r['pnl']) for r in test if _passes(r,*current)]
        oos+=chosen;base+=allp;fixed+=cur;oos_rows+=[r for r in test if _passes(r,me,mk)]
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
            'foldsTraded':len(traded),'foldsNonNegative':len(nonneg),'outOfSampleBootstrap':bootstrap_mean(oos),'_oos':oos,'_oosRows':oos_rows}

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
def verdict(wf,execution=None,ledger=None):
    """Go/no-go. Every criterion is a minimum; passing is necessary, not proof."""
    o=wf['outOfSample'];b=wf.get('outOfSampleBootstrap') or {};bb=wf.get('outOfSampleBlockBootstrap');c=PASS_CRITERIA;reasons=[]
    if o['trades']<c['minOutOfSampleTrades']:reasons.append(f"only {o['trades']} out-of-sample trades (need {c['minOutOfSampleTrades']})")
    if (o['avgPnlUsd'] or 0)<=c['meanPnlAbove']:reasons.append(f"out-of-sample mean {o['avgPnlUsd']} is not above 0")
    # Block bootstrap (whole 5-minute rounds) when available: trades in one round are not independent.
    lower=(bb or b).get('lower95') if (bb or b) else None
    if lower is None or lower<=c['bootstrapLower95Above']:reasons.append(f"{'block ' if bb else ''}bootstrap 95% lower bound {lower} is not above 0")
    share=wf['foldsNonNegative']/wf['foldsTraded'] if wf['foldsTraded'] else 0
    if share<c['minShareOfFoldsNonNegative']:reasons.append(f"{wf['foldsNonNegative']}/{wf['foldsTraded']} folds non-negative (need {int(c['minShareOfFoldsNonNegative']*100)}%)")
    if o['maxDrawdownUsd']>=c['maxDrawdownUsdBelow']:reasons.append(f"max drawdown ${o['maxDrawdownUsd']} (limit ${c['maxDrawdownUsdBelow']})")
    st=(wf.get('stressOutOfSample') or {}).get('exitWorseBy1Tick')
    if st and (st['avgPnlUsd'] or 0)<=c['stressOneTickMeanAbove']:reasons.append(f"edge does not survive exits 1 tick worse (mean {st['avgPnlUsd']})")
    if execution and not execution.get('clean'):reasons.append(f"execution audit errors: {', '.join(k for k,v in (execution.get('errors') or {}).items() if v) or 'no trades audited'}")
    if ledger and ledger.get('sessionsWithProblems'):reasons.append(f"{ledger['sessionsWithProblems']} ledger session(s) do not reconcile")
    return {'status':'PASS' if not reasons else 'FAIL','reasons':reasons,'criteria':c}

def gather(engine):
    """Copy the inputs on the event-loop thread (cheap) so the heavy part can run in a worker thread."""
    import poly_devlog,poly_momentum as pm
    edge=getattr(engine,'edge',None)
    bets={n:[dict(b) for b in rec.bets] for n,rec in (getattr(edge,'strategies',{}) or {}).items()}
    sessions=[{'lane':a,'live':False,'paper':copy.deepcopy(p)} for a,h in (getattr(engine,'ledger_history',{}) or {}).items() for p in h.values()]
    sessions+=[{'lane':a,'live':True,'paper':copy.deepcopy(e.paper)} for a,e in engine.engines.items() if e.paper]
    return {'sessions':sessions,'epoch':float(getattr(engine,'epoch',0) or 0),'rows':poly_devlog.trade_rows(engine),'attribution':[dict(r) for r in list(getattr(getattr(engine,'attribution',None),'rows',[]) or [])],
            'bets':bets,'rules':(pm.MAX_ENTRY_PRICE,pm.MIN_EDGE),'rulesLiveAt':pm.RULES_LIVE_AT}

class _Bets:
    def __init__(self,bets):self.bets=bets
class _Edge:
    def __init__(self,bets):self.strategies={n:_Bets(b) for n,b in bets.items()}

def analyse(inp):
    rows=[r for r in inp['rows'] if r.get('mode') in (None,'MOMENTUM_SCALP')]
    live=[float(r['pnl']) for r in rows if r.get('pnl') is not None and float(r.get('entryAt') or 0)>=inp['rulesLiveAt']]
    wf=walk_forward(rows,inp['rules']);oos_rows=wf.pop('_oosRows',[]);wf.pop('_oos',None)
    wf['outOfSampleBlockBootstrap']=block_bootstrap_mean(oos_rows)
    wf['stressOutOfSample']=stress(oos_rows)
    live_rows=[r for r in rows if r.get('pnl') is not None and float(r.get('entryAt') or 0)>=inp['rulesLiveAt']]
    rep={'generatedAt':time.time(),'execution':execution_audit(inp['attribution']),'walkForward':wf,
            'ledger':{**ledger_reconcile(inp.get('sessions',[]),inp.get('epoch',0.),inp['rulesLiveAt']),
                      'tradeWindows':window_counts([r for r in inp['rows'] if r.get('pnl') is not None],inp.get('epoch',0.),inp['rulesLiveAt'])},
            'breakdowns':{'outOfSample':breakdowns(oos_rows),'liveRules':breakdowns(live_rows)},
            'liveRules':{'metrics':metrics(live),'bootstrap':bootstrap_mean(live)},
            'modelVsMarket':calibration(_Edge(inp['bets'])),'about':__doc__.strip().split('\n\n')[0]}
    rep['verdict']=verdict(wf,rep['execution'],rep['ledger']);return rep

def audit_report(engine):
    return analyse(gather(engine))

def compact(rep):
    wf=rep['walkForward'];o=wf['outOfSample'];b=wf.get('outOfSampleBootstrap') or {};ex=rep['execution'];mm=rep['modelVsMarket']
    return {'verdict':rep['verdict']['status'],'why':rep['verdict']['reasons'][:3],
            'oos':[o['trades'],o['avgPnlUsd'],o['netPnlUsd'],b.get('lower95'),b.get('upper95')],
            'oosBlock':[(wf.get('outOfSampleBlockBootstrap') or {}).get(k) for k in ('lower95','upper95','blocks')],
            'stress1tick':(wf.get('stressOutOfSample') or {}).get('exitWorseBy1Tick',{}).get('avgPnlUsd'),
            'ledger':[rep['ledger']['sessions'],rep['ledger']['sessionsWithProblems'],rep['ledger']['tradeWindows']],
            'takeAll':[wf['baselineTakeAll']['trades'],wf['baselineTakeAll']['avgPnlUsd']],
            'folds':f"{wf['foldsNonNegative']}/{wf['foldsTraded']}",
            'live':[rep['liveRules']['metrics']['trades'],rep['liveRules']['metrics']['avgPnlUsd'],(rep['liveRules']['bootstrap'] or {}).get('lower95')],
            'exec':{'trades':ex['trades'],'clean':ex['clean'],'errors':{k:v for k,v in ex['errors'].items() if v},'risk':{k:v for k,v in ex['marketRisk'].items() if v}},
            'model':[mm.get('bets'),(mm.get('model') or {}).get('brier'),(mm.get('marketPrice') or {}).get('brier'),mm.get('brierSkillVsMarket')]}
