"""Strategy identity: version, configuration hash and the shadow experiments attached to every trade.

The baseline (frozen 2026-10-25, code revision 2cf2731) is the momentum rule set below. While the
stop experiments collect their evaluation sample, none of these parameters may be tuned: a change
changes CONFIG_HASH, so trades under different configurations can never be pooled silently.

Experiments are SHADOW ONLY: they observe the same live positions and books and record what their
policy would have done. They never place, change or cancel a paper order.
"""
from __future__ import annotations
import hashlib,json

STRATEGY_VERSION='momentum-17.8'
BASELINE={'frozenAtUtc':'2026-10-25','codeRevision':'2cf2731','tag':'baseline-2026-10-25'}

def _momentum_config():
    import poly_momentum as pm,poly_server_demo as sd
    return {'entryPriceMin':0.08,'entryPriceMax':pm.MAX_ENTRY_PRICE,'minEstimatedEdge':pm.MIN_EDGE,'minImpulseBps':pm.MIN_IMPULSE_BPS,
            'maxBookMove':pm.MAX_BOOK_MOVE,'maxSpread':0.02,'minLeadBps':0.2,'minFair':0.55,'baseShares':5.0,
            'exits':{'stop':'net loss >= lane stopLossUsd','takeProfit':'net >= max(0.18, stop)','maxHoldSec':30,
                     'oracleReversalBps':-0.2,'closingSec':15,'exitWalkTicks':2},
            'execution':{'latencySec':sd.EXECUTION_LATENCY_SEC,'staleBookSec':3,'feeRate':0.07,'makerExit':sd.MAKER_EXIT},
            'laneStops':'max(0.30, min(0.40, allocation*0.005))'}

# Experiment A: exit earlier on an adverse move. Fires on whichever comes first:
#   net loss at the bid >= stopFraction * stop, or
#   the bid has fallen adverseTicks below the entry-time bid while the oracle lead points against the trade.
# Experiment B: depth-aware entry gate, evaluated on the book at order time. Would reject when
#   executable bid depth within 2 ticks < depthMultiple * shares, or
#   the loss from selling into the current bids shifted adverseTicks lower (fees included) > riskBudgetMultiple * stop.
EXPERIMENTS={
    'A':{'id':'A-earlier-exit-v1','stopFraction':0.6,'adverseTicks':2,'latencySec':0.35,'walkTicks':2},
    'B':{'id':'B-depth-gate-v1','depthMultiple':2.0,'adverseTicks':3,'riskBudgetMultiple':1.0},
}

def _hash(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()[:12]

_cache=None
def identity():
    """{'strategyVersion','configHash','experimentsHash','config','experiments','baseline'}; computed once."""
    global _cache
    if _cache is None:
        cfg=_momentum_config()
        _cache={'strategyVersion':STRATEGY_VERSION,'configHash':_hash(cfg),'experimentsHash':_hash(EXPERIMENTS),
                'config':cfg,'experiments':EXPERIMENTS,'baseline':BASELINE}
    return _cache

def stamp():
    i=identity();return {'strategyVersion':i['strategyVersion'],'configHash':i['configHash'],'experimentsHash':i['experimentsHash']}
