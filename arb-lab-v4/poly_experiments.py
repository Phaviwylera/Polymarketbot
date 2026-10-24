"""Shadow stop experiments (see poly_strategy.EXPERIMENTS). Pure functions over a position dict and
the live book; they record what each policy would have done and never touch orders, cash or the
ledger. Only information available at the moment of each decision is used.
"""
from __future__ import annotations
from poly_features import walk

def fee(p):return round(.07*p*(1-p),5)

def net_at(shares,price,entry_total_cost):
    return shares*(price-fee(price))-entry_total_cost

def threshold_price(shares,entry_total_cost,stop):
    """Bid at which the net loss equals the stop (fees included); bisection on (0, 1)."""
    lo,hi=1e-4,0.9999
    if net_at(shares,hi,entry_total_cost)<-stop:return hi
    for _ in range(60):
        mid=(lo+hi)/2
        if net_at(shares,mid,entry_total_cost)<-stop:lo=mid
        else:hi=mid
    return hi

def gate_b(bid_levels,shares,entry_total_cost,stop,tick,cfg):
    """Experiment B on the book at order time. Returns the decision and the evidence behind it."""
    best=max((float(l['price']) for l in bid_levels if float(l.get('size') or 0)>0),default=None)
    if best is None:return {'wouldReject':True,'reason':'no bids','depthWithin2Ticks':0.,'stressedLossUsd':None}
    depth=sum(float(l['size']) for l in bid_levels if float(l['price'])>=best-2*tick-1e-9)
    shifted=[{'price':round(float(l['price'])-cfg['adverseTicks']*tick,4),'size':float(l['size'])} for l in bid_levels]
    fill=walk([l for l in shifted if l['price']>0],shares,False)
    stressed=None if not fill else entry_total_cost-shares*(fill['vwap']-fill['feePerShare'])
    reasons=[]
    if depth<cfg['depthMultiple']*shares-1e-9:reasons.append(f"bid depth within 2 ticks {depth:.1f} < {cfg['depthMultiple']}x {shares:.0f} shares")
    if stressed is None:reasons.append('bids cannot absorb the position after the adverse shift')
    elif stressed>cfg['riskBudgetMultiple']*stop+1e-9:reasons.append(f"stressed exit loss ${stressed:.2f} > ${cfg['riskBudgetMultiple']*stop:.2f}")
    return {'wouldReject':bool(reasons),'reason':'; '.join(reasons) or 'within budget','depthWithin2Ticks':round(depth,2),
            'stressedLossUsd':round(stressed,4) if stressed is not None else None,'bestBid':best}

def step_a(pos,bid_levels,bid,lead_bps,now,tick,cfg):
    """Experiment A, called on every position-risk tick while the real position is open."""
    ex=pos.setdefault('experiments',{}).setdefault('A',{'id':cfg['id'],'state':'watching'})
    if ex['state']=='filled' or bid is None:return ex
    shares=float(pos['shares']);cost=float(pos['entryTotalCost']);stop=float(pos.get('stopLossUsd') or .30)
    direction=1 if str(pos.get('side')).upper()=='UP' else -1
    if ex['state']=='watching':
        net=net_at(shares,float(bid),cost);entry_bid=pos.get('entryBid')
        drop=(float(entry_bid)-float(bid))/tick if entry_bid is not None else 0.
        why=None
        if net<=-cfg['stopFraction']*stop:why=f"net {net:.3f} <= -{cfg['stopFraction']}x stop"
        elif drop>=cfg['adverseTicks']-1e-9 and lead_bps is not None and lead_bps*direction<0:why=f"bid {drop:.0f} ticks below entry bid and oracle lead against"
        if why:ex.update(state='triggered',triggerAt=now,firstTriggerAt=now,triggerBid=float(bid),triggerNet=round(net,4),why=why,attempts=0)
        return ex
    # triggered: simulated exit after the same latency, walking at most walkTicks below the trigger bid
    if now-ex['triggerAt']<cfg['latencySec']:return ex
    ex['attempts']+=1
    fill=walk(bid_levels,shares,False,ex['triggerBid']-cfg['walkTicks']*tick)
    if not fill:   # like the real exit: retry from the current bid
        ex.update(triggerAt=now,triggerBid=float(bid));return ex
    ex.update(state='filled',fillAt=now,exitPrice=round(fill['vwap'],4),
              netPnlUsd=round(shares*(fill['vwap']-fill['feePerShare'])-cost,5),
              triggerToFillMs=round((now-ex.get('firstTriggerAt',ex['triggerAt']))*1000))
    return ex

def finalize(pos,real_net):
    """At the real close: the shadow result for each experiment, with the real result for comparison."""
    out={}
    a=(pos.get('experiments') or {}).get('A')
    if a is not None:
        shadow=a['netPnlUsd'] if a.get('state')=='filled' else real_net
        out['A']={**{k:a.get(k) for k in ('id','state','why','triggerNet','triggerBid','exitPrice','attempts','triggerToFillMs')},
                  'shadowNetPnlUsd':round(shadow,5),'realNetPnlUsd':round(real_net,5),
                  'exitedEarlier':a.get('state')=='filled','missedRecovery':a.get('state')=='filled' and real_net>shadow+1e-9}
    b=(pos.get('experiments') or {}).get('B')
    if b is not None:out['B']={**b,'realNetPnlUsd':round(real_net,5)}
    return out
