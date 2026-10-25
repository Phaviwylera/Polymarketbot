"""Strategy identity: version, configuration hash and the shadow experiments attached to every trade.

The baseline (frozen 2026-10-25, code revision 2cf2731) is the momentum rule set below. While the
stop experiments collect their evaluation sample, none of these parameters may be tuned: a change
changes CONFIG_HASH, so trades under different configurations can never be pooled silently.

Experiments are SHADOW ONLY: they observe the same live positions and books and record what their
policy would have done. They never place, change or cancel a paper order.
"""
from __future__ import annotations
import hashlib,json

STRATEGY_VERSION='momentum-17.9'
BASELINE={'frozenAtUtc':'2026-10-25','codeRevision':'2cf2731','tag':'baseline-2026-10-25'}

def _momentum_config():
    import poly_momentum as pm,poly_server_demo as sd
    return {'entryPriceMin':0.08,'entryPriceMax':pm.MAX_ENTRY_PRICE,'minEstimatedEdge':pm.MIN_EDGE,'minImpulseBps':pm.MIN_IMPULSE_BPS,
            'maxBookMove':pm.MAX_BOOK_MOVE,'maxSpread':0.02,'minLeadBps':0.2,'minFair':0.55,'baseShares':5.0,
            'exits':{'stop':'net loss >= lane stopLossUsd','takeProfit':'net >= max(0.18, stop)','maxHoldSec':30,
                     'oracleReversalBps':-0.2,'closingSec':15,'exitWalkTicks':2},
            'execution':{'latencySec':sd.EXECUTION_LATENCY_SEC,'staleBookSec':3,'feeRate':0.07,'makerExit':sd.MAKER_EXIT},
            'laneStops':'max(0.30, min(0.40, allocation*0.005))',
            # v17.9 (owner decision 2026-10-25, after 19-20 frozen trades): experiment A adopted as a live rule; B stays shadow.
            'earlyExit':{k:v for k,v in EXPERIMENTS['A'].items() if k!='id'} if LIVE_RULES['earlyExit'] else None,
            'depthGate':{k:v for k,v in EXPERIMENTS['B'].items() if k!='id'} if LIVE_RULES['depthGate'] else None}

# Experiment A: exit earlier on an adverse move. Fires on whichever comes first:
#   net loss at the bid >= stopFraction * stop, or
#   the bid has fallen adverseTicks below the entry-time bid while the oracle lead points against the trade.
# Experiment C: see EXPERIMENTS['C'] (forward-only edge gate).
# Experiment B: depth-aware entry gate, evaluated on the book at order time. Would reject when
#   executable bid depth within 2 ticks < depthMultiple * shares, or
#   the loss from selling into the current bids shifted adverseTicks lower (fees included) > riskBudgetMultiple * stop.
EXPERIMENTS={
    'A':{'id':'A-earlier-exit-v1','stopFraction':0.6,'adverseTicks':2,'latencySec':0.35,'walkTicks':2},
    'B':{'id':'B-depth-gate-v1','depthMultiple':2.0,'adverseTicks':3,'riskBudgetMultiple':1.0},
    # Experiment C: entry gate on the model's estimated edge. Hypothesis from the 2026-10-25 audit bands (IN-SAMPLE:
    # 12-20c edge 28 trades -$6.98, >=20c 31 trades +$8.95). Scored ONLY on trades entered after registeredAt, so
    # the split that suggested it is never part of its evidence. Report-only: no live order is affected.
    # D (priority 1, fees): post a zero-fee resting BID at the best bid when the entry signal fires, valid
    # windowSec; it fills only when a market trade prints strictly BELOW it (trade-through, so queue position
    # cannot flatter it). Shadow P&L = the real exit proceeds minus the cheaper zero-fee entry; unfilled = no trade.
    # Answers: does a maker entry save more in fees + price than it loses by missing (adverse selection)?
    'D':{'id':'D-maker-entry-v1','windowSec':2.0,'latencySec':0.35,'registeredAt':1792933930,'registeredUtc':'2026-10-25 13:12 UTC'},
    # E (priority 3, calibration): the learner's market-anchored probability for the entry token at entry time;
    # trades split by learned edge (p - ask - fee) > 0. Report-only.
    'E':{'id':'E-learner-gate-v1','minLearnedEdge':0.0,'registeredAt':1792933930,'registeredUtc':'2026-10-25 13:12 UTC'},
    'C':{'id':'C-min-edge-0.20-v1','minEdge':0.20,'registeredAt':1792900230,'registeredUtc':'2026-10-25 03:50 UTC'},
}

# Live rules adopted from the shadow experiments (A: earlier exit on an adverse move). B stays shadow-only.
# Their shadow scoring keeps running as a consistency check; a new evaluation sample starts with this config hash.
LIVE_RULES={'earlyExit':True,'depthGate':False,'adoptedUtc':'2026-10-25',
            'evidence':'A: shadow -$3.52 vs real -$4.73 on 20 versioned trades (cfa5d71516e3). B NOT adopted: with a $0.30 stop its '
                        '3-tick stress loss (~$0.31 on 5 shares) rejects every entry on BTC, BTC15 and ETH15, i.e. it acts as a lane switch'}
PREVIOUS_CONFIG_HASH='cfa5d71516e3'

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
