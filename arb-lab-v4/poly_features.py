"""Causal V16 microstructure observations. No future label is a feature."""
import math
SCHEMA='poly-features-v16.0'
POLICY='scalp30-fak-stop-target-v1'
NAMES=('side_up','btc','eth','sol','xrp','elapsed','remaining','ask','bid','spread','entry_vwap','friction',
       'imbalance','bid_depth','ask_depth','book_age','oracle_age','model_present','fair','lead_bps','impulse_bps',
       'token_flow5','stop_per_share','target_per_share','spot_present','spot_return5','spot_spread',
       'perp_present','perp_return5','perp_flow5','funding_rate')
def number(v,default=0.):
    try:
        v=float(v);return v if math.isfinite(v) else default
    except (TypeError,ValueError):return default

def fee(p):return round(.07*p*(1-p),5)

def walk(levels,quantity,buy,limit=None):
    if not math.isfinite(quantity) or quantity<=0:return None
    rows=[(number(r.get('price'),-1),number(r.get('size'))) for r in levels]
    left=quantity;cost=fees=0.
    for p,q in sorted(rows,reverse=not buy):
        if not 0<p<1 or q<=0:continue
        if limit is not None and ((buy and p>limit+1e-9) or (not buy and p<limit-1e-9)):continue
        take=min(left,q);cost+=take*p;fees+=take*fee(p);left-=take
        if left<=1e-9:return {'vwap':cost/quantity,'feePerShare':fees/quantity,'quantity':quantity}
    return None

def observe(asset,c,external,now):
    market=c.get('market') or {};result={}
    if not market.get('acceptingOrders') or not market.get('slug'):return {},'market unavailable'
    start=number(market.get('startTs'));end=number(market.get('endTs'))
    if not start<=now<end:return {},'outside market lifetime'
    if any(c.get('feed',{}).get(k)!='live' for k in ('market','clob','rtds')):return {},'feed unavailable'
    reference_times=[number(c.get(k)) for k in ('spotTs','twapTs')]
    if any(t>now+1 or now-t>=5 for t in reference_times):return {},'oracle stale or future'
    oracle_age=max(max(0,now-t) for t in reference_times)
    for side in ('up','down'):
        b=c['bbo'][side];depth=c['depth'][side];ask=number(b.get('ask'),-1);bid=number(b.get('bid'),-1)
        received=number(c.get('bookObservedAt',{}).get(side));lag=number(c.get('bookEventLagSec',{}).get(side),1e9)
        age=max(0,now-received)+max(0,lag)
        if not (0<bid<ask<1 and age<=3 and received<=now+1 and lag>=-1):continue
        qty=max(5.,number(market.get('minOrderSize')));tick=max(.001,number(market.get('minTickSize'),.01))
        buy=walk(depth.get('asks',[]),qty,True,ask+tick);sell=walk(depth.get('bids',[]),qty,False)
        if not buy or not sell:continue
        bd=sum(number(r.get('size')) for r in depth.get('bids',[])[:3]);ad=sum(number(r.get('size')) for r in depth.get('asks',[])[:3])
        trades=[r for r in c.get('tradePrints',[]) if r.get('side')==side and r.get('marketSlug')==market['slug'] and 0<=now-number(r.get('t'),-1e9)<=5 and number(r.get('receivedAt'),now+1)<=now]
        flow=sum(number(r.get('size'))*(1 if str(r.get('aggressor')).upper()=='BUY' else -1) for r in trades)/max(sum(number(r.get('size')) for r in trades),1e-9)
        m=c.get('model') or {};direction=1 if side=='up' else -1;stop=number(c.get('stopLossUsd'),.3);target=max(.18,stop)
        spot=external.get('coinbase') or {};perp=external.get('binance') or {}
        def fresh(r):return bool(r and 0<=now-number(r.get('eventAt'),-1e9)<=5 and number(r.get('receivedAt'),now+1)<=now)
        sf=fresh(spot);pf=fresh(perp)
        values=(side=='up',asset=='BTC',asset=='ETH',asset=='SOL',asset=='XRP',(now-start)/300,end-now,
            ask,bid,ask-bid,buy['vwap'],buy['vwap']+buy['feePerShare']-sell['vwap']+sell['feePerShare'],
            (bd-ad)/max(bd+ad,1e-9),bd,ad,age,oracle_age,bool(m),number(m.get('fairUp' if side=='up' else 'fairDown'),.5),
            direction*number(m.get('leadBps')),direction*number(m.get('impulse5Bps')),flow,stop/qty,target/qty,
            sf,direction*number(spot.get('return5Bps')) if sf else 0,number(spot.get('spreadBps')) if sf else 0,
            pf,direction*number(perp.get('return5Bps')) if pf else 0,direction*number(perp.get('flow5')) if pf else 0,
            number(perp.get('fundingRate')) if pf and 0<=now-number(perp.get('fundingAt'),-1e9)<10 else 0)
        features={k:max(-1e9,min(1e9,number(v))) for k,v in zip(NAMES,values)}
        result[side]={'schema':SCHEMA,'policy':POLICY,'asset':asset,'side':side,'marketSlug':market['slug'],
            'roundStart':start,'marketEnd':end,'featuresAt':now,'availableAt':now,'receivedAt':received,
            'sourceEventAt':received-max(0,lag),'features':features,'quantity':qty,'ask':ask,'bid':bid,'tick':tick,
            'stopLossUsd':stop,'targetUsd':target,'entry':buy,'exitNow':sell,'depth':depth}
    return result,'collecting' if result else 'waiting for current executable depth'
