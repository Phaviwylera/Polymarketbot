"""Stop forensics, shadow-experiment comparison, latency distributions, strategy-version breakdown and
operational alerts. Read-only analysis over copies of the ledger, attribution rows and engine state.

Stop reconstruction uses the session event log, which records, in order: BUY (fill price, shares),
the exit ORDER (price = best bid when the stop decided to exit), any NOFILL retries, and the SELL
(VWAP actually received). The trigger-time book depth was not recorded before 2026-10-25; trades
closed since then carry it (exitTriggerBids) and are reconstructed in full.
"""
from __future__ import annotations
import math,re,time
from poly_experiments import fee,net_at,threshold_price
import poly_audit

TICK=0.01
_SHARES=re.compile(r'([0-9.]+) shares')

def _pct(xs,q):
    if not xs:return None
    s=sorted(xs);i=min(len(s)-1,max(0,int(math.ceil(q*len(s))-1)));return round(s[i],4)

def dist(xs):
    xs=[float(x) for x in xs if x is not None]
    if not xs:return {'n':0}
    return {'n':len(xs),'mean':round(math.fsum(xs)/len(xs),4),'median':_pct(xs,.5),'p90':_pct(xs,.9),'p95':_pct(xs,.95),'max':round(max(xs),4),'min':round(min(xs),4)}

def _match(attrib,asset,t):
    best=None
    for a in attrib:
        if a.get('asset')==asset and abs(float(a.get('entryAt') or 0)-t)<1.5:
            if best is None or abs(float(a['entryAt'])-t)<abs(float(best['entryAt'])-t):best=a
    return best

def reconstruct_stops(sessions,attribution,default_stop=None):
    """One row per momentum stop exit: trigger bid, threshold price, retries, delay and the split of
    the overshoot into (a) loss already beyond the stop when it fired and (b) movement after it fired."""
    rows=[]
    for s in sessions:
        lane=s['lane'];ev=list(reversed((s.get('paper') or {}).get('events') or []))
        buy=None;orders=[];nofill=0
        for e in ev:
            a=e.get('action')
            if a=='BUY':buy=e;orders=[];nofill=0
            elif a=='ORDER' and buy and str(e.get('detail','')).startswith('paper FAK sell'):orders.append(e)
            elif a=='NOFILL' and buy and orders:nofill+=1
            elif a=='SELL' and buy and e.get('pnl') is not None:
                if 'loss stop' in str(e.get('detail','')) and str(buy.get('detail','')).startswith('MOMENTUM_SCALP'):
                    rows.append(_stop_row(lane,buy,orders,nofill,e,_match(attribution,lane,float(buy['t'])),default_stop))
                buy=None;orders=[];nofill=0
    rows.sort(key=lambda r:r['entryAt'])
    return rows

def _stop_row(lane,buy,orders,nofill,sell,att,default_stop):
    m=_SHARES.search(str(buy.get('detail','')));shares=float(m.group(1)) if m else float((att or {}).get('quantity') or 5)
    entry=float(buy['price']);cost=shares*entry+shares*fee(entry);net=float(sell['pnl'])
    stop=(att or {}).get('stopLossUsd')
    if stop is None and att and float(att.get('stopOvershootUsd') or 0)>0:stop=-net-float(att['stopOvershootUsd'])
    if stop is None:stop=default_stop(lane) if default_stop else .30
    stop=float(stop);thr=threshold_price(shares,cost,stop)
    trig=orders[0] if orders else None;tb=float(trig['price']) if trig and trig.get('price') is not None else None
    exitp=float(sell['price']) if sell.get('price') is not None else None
    over=max(0.,-net-stop)
    gap=max(0.,shares*(thr-tb)) if tb is not None else None          # already past the stop when it fired
    after=shares*(tb-exitp) if (tb is not None and exitp is not None) else None   # moved while exiting
    feat=(att or {}).get('entryFeatures') or {}
    delay=float(sell['t'])-float(trig['t']) if trig else None
    causes=[]
    if float(feat.get('bookMove') or 0)>=0.05:causes.append('entered after the token had already moved 5c+ (blocked since V17.6)')
    if gap is not None and gap>=0.05 and gap>=(after or 0):causes.append('price gapped past the stop between book updates')
    if nofill:causes.append(f'{nofill} exit retr{"y" if nofill==1 else "ies"}: bids moved during the 350 ms latency')
    if after is not None and after>=0.05 and not nofill:causes.append('bids fell during the 350 ms exit latency / 2-tick walk')
    if delay is not None and delay>2:causes.append(f'exit took {delay:.1f}s from decision to fill')
    if entry<0.30:causes.append('low-priced token: one tick is a larger share of the position')
    if not causes and over<=0.05:causes.append('within $0.05 of the stop: spread, fees and one tick')
    if att and att.get('exitTriggerBookAgeSec') is not None and float(att['exitTriggerBookAgeSec'])>1:causes.append(f"book {att['exitTriggerBookAgeSec']}s old at the trigger")
    return {'lane':lane,'entryAt':round(float(buy['t']),1),'entryUtc':time.strftime('%m-%d %H:%M:%S',time.gmtime(float(buy['t']))),
            'side':sell.get('side'),'shares':shares,'entryPrice':entry,'stopUsd':round(stop,4),'thresholdBid':round(thr,4),
            'triggerBid':tb,'exitPrice':exitp,'netPnlUsd':round(net,4),'overshootUsd':round(over,4),
            'lossPastStopAtTriggerUsd':round(gap,4) if gap is not None else None,'lossAfterTriggerUsd':round(after,4) if after is not None else None,
            'retries':nofill,'decisionToFillSec':round(delay,3) if delay is not None else None,
            'holdSec':round(float(sell['t'])-float(buy['t']),1),'bookMoveAtEntry':feat.get('bookMove'),'impulseAtEntry':feat.get('impulse5Bps'),
            'configHash':(att or {}).get('configHash'),'triggerBids':(att or {}).get('exitTriggerBids'),
            'causes':causes or ['unexplained with the recorded evidence']}

def stop_summary(rows):
    over=[r for r in rows if r['overshootUsd']>0.05]
    def share(key):
        tot=math.fsum(r['overshootUsd'] for r in over) or 1.
        return round(math.fsum(min(r['overshootUsd'],max(0.,r[key] or 0)) for r in over)/tot*100,1)
    cause_counts={}
    for r in over:
        for c in r['causes']:k=c.split(':')[0].split(' (')[0];cause_counts[k]=cause_counts.get(k,0)+1
    return {'stops':len(rows),'overshootsAbove5c':len(over),'exitLoss':dist([-r['netPnlUsd'] for r in rows]),
            'overshoot':dist([r['overshootUsd'] for r in rows]),
            'overshootShareFromGapBeforeTriggerPct':share('lossPastStopAtTriggerUsd') if over else None,
            'overshootShareAfterTriggerPct':share('lossAfterTriggerUsd') if over else None,
            'causeCounts':dict(sorted(cause_counts.items(),key=lambda kv:-kv[1])),
            'withEntryChase':sum(1 for r in over if float(r['bookMoveAtEntry'] or 0)>=0.05),
            'dataGaps':'trigger-time bid depth recorded only for trades closed after the 2026-10-25 deploy; earlier stops use the trigger bid from the exit ORDER event'}

def experiments_report(attribution):
    """Compare each shadow experiment with the real (baseline) result on the same trades."""
    rows=[a for a in attribution if isinstance(a.get('experiments'),dict) and a.get('strategy')=='MOMENTUM_SCALP']
    out={'trades':len(rows)}
    a=[r for r in rows if 'A' in r['experiments']]
    if a:
        real=[float(r['experiments']['A']['realNetPnlUsd']) for r in a];shadow=[float(r['experiments']['A']['shadowNetPnlUsd']) for r in a]
        diff=[s-r for s,r in zip(shadow,real)]
        stops=[r for r in a if 'loss stop' in str(r.get('exitReason'))]
        out['A']={'id':a[0]['experiments']['A'].get('id'),'trades':len(a),'baseline':poly_audit.metrics(real),'experiment':poly_audit.metrics(shadow),
                  'exitedEarlier':sum(1 for r in a if r['experiments']['A'].get('exitedEarlier')),
                  'missedRecoveries':sum(1 for r in a if r['experiments']['A'].get('missedRecovery')),
                  'savedOnStops':round(math.fsum(float(r['experiments']['A']['shadowNetPnlUsd'])-float(r['experiments']['A']['realNetPnlUsd']) for r in stops),4),
                  'worstLoss':{'baseline':round(min(real),4),'experiment':round(min(shadow),4)},
                  'p95Loss':{'baseline':_pct([-x for x in real],.95),'experiment':_pct([-x for x in shadow],.95)},
                  'meanDifferencePerTrade':round(math.fsum(diff)/len(diff),4),
                  'blockBootstrapDifference':poly_audit.block_bootstrap_mean([{'entryAt':r['entryAt'],'pnl':d} for r,d in zip(a,diff)])}
    b=[r for r in rows if 'B' in r['experiments'] and 'wouldReject' in r['experiments']['B']]
    if b:
        acc=[float(r['netPnlUsd']) for r in b if not r['experiments']['B']['wouldReject']]
        rej=[float(r['netPnlUsd']) for r in b if r['experiments']['B']['wouldReject']]
        out['B']={'id':b[0]['experiments']['B'].get('id'),'trades':len(b),'baseline':poly_audit.metrics([float(r['netPnlUsd']) for r in b]),
                  'experimentAccepted':poly_audit.metrics(acc),'wouldReject':poly_audit.metrics(rej),
                  'rejectRatePct':round(100*len(rej)/len(b),1),
                  'rejectedStops':sum(1 for r in b if r['experiments']['B']['wouldReject'] and 'loss stop' in str(r.get('exitReason')))}
    import poly_strategy
    c=poly_strategy.EXPERIMENTS.get('C')
    if c:
        fwd=[r for r in rows+[a for a in attribution if a.get('strategy')=='MOMENTUM_SCALP' and not isinstance(a.get('experiments'),dict)]
             if float(r.get('entryAt') or 0)>=c['registeredAt'] and (r.get('entryFeatures') or {}).get('modelEdgeEstimate') is not None]
        keep=[r for r in fwd if float(r['entryFeatures']['modelEdgeEstimate'])>=c['minEdge']]
        drop=[r for r in fwd if float(r['entryFeatures']['modelEdgeEstimate'])<c['minEdge']]
        out['C']={'id':c['id'],'registeredUtc':c['registeredUtc'],'forwardTrades':len(fwd),'baseline':poly_audit.metrics([float(r['netPnlUsd']) for r in fwd]),
                  'experimentAccepted':poly_audit.metrics([float(r['netPnlUsd']) for r in keep]),'wouldReject':poly_audit.metrics([float(r['netPnlUsd']) for r in drop]),
                  'acceptedBlockBootstrap':poly_audit.block_bootstrap_mean([{'entryAt':r['entryAt'],'pnl':float(r['netPnlUsd'])} for r in keep]),
                  'decisionRule':'adopt only if, on forward trades, accepted trades pass the same gates as the baseline (60 trades, mean > 0, block-bootstrap lower > 0) and beat the rejected ones'}
    out['note']=('Shadow results on the same entries and books. A policy is not adopted because it lowers the worst loss in a small '
                 'sample: it must pass the same predeclared gates as the baseline on its own evaluation window.')
    return out

def latency_report(attribution):
    rows=[a for a in attribution if a.get('strategy')=='MOMENTUM_SCALP']
    return {'signalToOrderMs':dist([a.get('signalToOrderMs') for a in rows]),'orderToFillMs':dist([a.get('entryLatencyMs') for a in rows]),
            'exitDecisionToFillMs':dist([a.get('exitTriggerToFillMs') for a in rows]),'exitOrderToFillMs':dist([a.get('exitLatencyMs') for a in rows]),
            'exitRetries':dist([a.get('exitRetries') for a in rows if a.get('exitRetries') is not None]),
            'assumedLatencyMs':350,'note':'signal-to-order and decision-to-fill are recorded only for trades closed after the 2026-10-25 deploy'}

def version_report(attribution,identity):
    groups={}
    for a in attribution:
        if a.get('strategy')!='MOMENTUM_SCALP':continue
        groups.setdefault(a.get('configHash') or 'before-versioning',[]).append(float(a.get('netPnlUsd') or 0))
    return {'current':{'strategyVersion':identity['strategyVersion'],'configHash':identity['configHash'],'experimentsHash':identity['experimentsHash']},
            'byConfigHash':{k:poly_audit.metrics(v) for k,v in groups.items()},
            'evaluationSample':poly_audit.metrics(groups.get(identity['configHash'],[])),
            'freezeRule':'no tuning of entry, sizing, exits, filters or market selection while the stop experiments collect their sample'}

def bands(attribution):
    rows=[a for a in attribution if a.get('strategy')=='MOMENTUM_SCALP']
    def band(x,edges):
        for lo,hi in zip(edges,edges[1:]):
            if lo<=x<hi:return f'{lo:g}-{hi:g}'
        return f'>={edges[-1]:g}'
    out={'byEntryEdge':{},'byExitReason':{},'byHoldSec':{},'byLane':{},'byEntryPrice':{}}
    for a in rows:
        e=(a.get('entryFeatures') or {}).get('modelEdgeEstimate')
        if e is not None:out['byEntryEdge'].setdefault(band(float(e),[0,.12,.2,.3]),[]).append(float(a['netPnlUsd']))
        out['byExitReason'].setdefault(str(a.get('exitReason'))[:28],[]).append(float(a['netPnlUsd']))
        out['byHoldSec'].setdefault(band(float(a.get('holdSec') or 0),[0,5,10,20,30]),[]).append(float(a['netPnlUsd']))
        out['byLane'].setdefault(str(a.get('lane') or a.get('asset') or '?'),[]).append(float(a['netPnlUsd']))
        if a.get('entryPrice') is not None:out['byEntryPrice'].setdefault(band(float(a['entryPrice']),[0,.2,.3,.4,.45]),[]).append(float(a['netPnlUsd']))
    return {k:{b:poly_audit.metrics(v) for b,v in sorted(d.items())} for k,d in out.items()}

def alerts(state,ledger,execution,now=None):
    """Operational alerts from the gathered engine state; each is a short, actionable string."""
    now=time.time() if now is None else now;out=[]
    for lane in state.get('lanes',[]):
        if lane.get('paused'):continue
        for k,v in (lane.get('feed') or {}).items():
            if v not in ('live','warming','paused'):out.append(f"{lane['lane']}: {k} feed {v}")
        age=lane.get('bookAgeSec')
        if age is not None and age>10:out.append(f"{lane['lane']}: order book {age:.0f}s old")
        if lane.get('clobReconnectsLastHour',0)>=6:out.append(f"{lane['lane']}: {lane['clobReconnectsLastHour']} order-book reconnects in the last hour")
        pos=lane.get('position')
        if pos and pos.get('mode')=='MOMENTUM_SCALP' and now-float(pos.get('entryAt') or now)>90:out.append(f"{lane['lane']}: scalp open {now-float(pos['entryAt']):.0f}s (max hold 30s)")
        if pos and pos.get('exitRequestedAt') and now-float(pos['exitRequestedAt'])>5:out.append(f"{lane['lane']}: exit pending {now-float(pos['exitRequestedAt']):.0f}s")
    st=state.get('storage') or {}
    if st.get('configured'):
        if st.get('lastError'):out.append(f"database: last write error {st['lastError']}")
        if st.get('checkpointAgeSec') is not None and st['checkpointAgeSec']>30:out.append(f"database: last checkpoint {st['checkpointAgeSec']:.0f}s ago")
        if st.get('droppedEvents'):out.append(f"database: {st['droppedEvents']} events dropped")
        exp=st.get('databaseExpiresAt')
        if exp:
            try:
                days=(time.mktime(time.strptime(exp[:19],'%Y-%m-%dT%H:%M:%S'))-time.timezone-now)/86400
                if days<=30:out.append(f"database expires in {days:.0f} days ({exp[:10]}); migrate and test a restore first")
            except ValueError:pass
        if not st.get('lastBackupAt'):out.append('database: no verified backup recorded yet')
        elif st.get('lastBackupOk') is False:out.append('database: last backup FAILED verification')
        elif now-float(st['lastBackupAt'])>36*3600:out.append(f"database: last verified backup is {(now-float(st['lastBackupAt']))/3600:.0f} h old")
    if ledger.get('sessionsWithProblems'):out.append(f"ledger: {ledger['sessionsWithProblems']} session(s) do not reconcile")
    if not execution.get('clean') and execution.get('trades'):out.append('fill audit: execution errors present')
    rk=execution.get('marketRisk') or {}
    if rk.get('heldToSettlement'):out.append(f"{rk['heldToSettlement']} scalp(s) held to settlement")
    return out
