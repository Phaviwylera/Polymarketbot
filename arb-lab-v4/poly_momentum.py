"""Executable paper momentum experiment. Model edge is an estimate, not a calibrated return."""
import math

def fee(price):return round(.07*price*(1-price),5)

def signal(context,history,allow_weak_impulse=False):
    c=context;m=c.get('model');market=c.get('market') or {};now=c['t']
    def reject(reason):return None,reason
    if not m or not c.get('openTrusted'):return reject('waiting for trusted market opening and model')
    if not market.get('acceptingOrders'):return reject('market not accepting orders')
    if any(c.get('feed',{}).get(k)!='live' for k in ('market','clob','rtds')):return reject('reference or order-book connection unavailable')
    if max(now-c.get('spotTs',0),now-c.get('twapTs',0))>=5:return reject('oracle data older than 5 seconds')
    if now-market['startTs']<20 or market['endTs']-now<=45:return reject('outside momentum entry window')
    side='up' if m.get('leadBps',0)>=0 else 'down';direction=1 if side=='up' else -1
    if m.get('leadBps',0)*direction<.2:return reject('oracle lead below 0.20 bps')
    if m.get('impulse5Bps',0)*direction<(-.05 if allow_weak_impulse else .05):return reject('oracle impulse does not confirm direction')
    if c.get('bookAges',{}).get(side,1e9)>3:return reject('executable book older than 3 seconds')
    b=c['bbo'][side];ask=b.get('ask');bid=b.get('bid');tick=max(.001,float(market.get('minTickSize') or .01))
    if ask is None or bid is None:return reject('bid or ask missing')
    ask=float(ask);bid=float(bid);shares=max(5.,float(market.get('minOrderSize') or 0))
    if not all(math.isfinite(x) for x in (ask,bid,shares)):return reject('nonfinite book value')
    if not .08<=ask<=.65 or bid<=0 or bid>=ask:return reject('entry price outside liquid price band')
    if ask-bid>.020001:return reject('spread exceeds 2 cents')
    if min(float(b.get('askSize') or 0),float(b.get('bidSize') or 0))<shares:return reject('insufficient confirmed entry or exit depth')
    cap=min(3.25,float(c['startingBalance'])*.10,float(c['cash']))
    total=shares*(ask+fee(ask))
    if total>cap+1e-9:return reject('minimum order exceeds funded notional cap')
    prior=[p for p in history if p['marketSlug']==market['slug'] and 2<=now-p['t']<=6 and p.get(side) is not None]
    if not prior:return reject('collecting two-second book confirmation')
    move=(ask+bid)/2-float(prior[-1][side])
    if move<tick/2-1e-9:return reject('token book does not confirm oracle momentum')
    fair=float(m['fairUp'] if side=='up' else m['fairDown'])
    if fair<.55:return reject('estimated round direction does not confirm scalp direction')
    prior_bid=prior[-1].get(side+'Bid',float(prior[-1][side])-tick/2)
    if prior_bid is None or bid-float(prior_bid)<tick-1e-9:return reject('best bid has not risen a full tick')
    edge=fair-ask-fee(ask)-fee(fair)-.01
    if not math.isfinite(edge) or edge<.02:return reject('model advantage below fees plus 2-cent buffer')
    return {'side':side.upper(),'ask':ask,'bid':bid,'shares':shares,'fair':fair,'roundTripNet':edge,
            'modelEdgeEstimate':edge,'entryTotalCost':total,'notionalCap':cap,'bookMove':move,
            'leadBps':m['leadBps'],'impulse5Bps':m['impulse5Bps'],'marketSlug':market['slug']},'OPPORTUNITY_MOMENTUM'
