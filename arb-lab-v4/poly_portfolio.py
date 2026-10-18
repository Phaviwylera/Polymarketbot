"""Parallel, isolated crypto paper lanes. No signing keys or real order submission."""
from __future__ import annotations
import asyncio,time
from poly_server_demo import PolyServerDemo,ENGINE_REVISION

ASSETS=('BTC','ETH','SOL','XRP')
ALLOCATIONS={'BTC':60.0,'ETH':80.0,'SOL':30.0,'XRP':30.0}
TOTAL_BALANCE=sum(ALLOCATIONS.values())
PORTFOLIO_LOSS_LIMIT=6.0

class PaperPortfolio:
    def __init__(self):
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.;self.started=False
        self.engines={asset:PolyServerDemo(asset=asset,balance=ALLOCATIONS[asset],
            state_file=f'/tmp/poly_v14_{asset.lower()}_paper.json',portfolio_guard=self.entry_allowed,
            risk_profile={'walletQuotes':True,'residualCap':min(2.0,ALLOCATIONS[asset]*.025),'firstPriceCap':.60,
                'stopLoss':min(.40,ALLOCATIONS[asset]*.005),'sessionLossLimit':ALLOCATIONS[asset]*.05}) for asset in ASSETS}
    def entry_allowed(self):
        equity=sum(e._equity() if e.paper else e.balance for e in self.engines.values())
        realized=sum(float((e.paper or {}).get('realizedPnl') or 0) for e in self.engines.values())
        self.peak=max(self.peak,equity);self.max_drawdown=max(self.max_drawdown,self.peak-equity)
        if min(realized,equity-TOTAL_BALANCE)<=-PORTFOLIO_LOSS_LIMIT:self.locked=True
        return not self.locked
    @property
    def feed(self):return {asset:dict(e.feed) for asset,e in self.engines.items()}
    async def start(self):
        if self.started:return
        self.started=True
        await asyncio.gather(*(e.start() for e in self.engines.values()))
    async def reset(self):
        # Do not erase unresolved or pending simulated exposure.
        if any(e.pending or (e.paper and (e.paper.get('position') or e._inventory_has_residual())) for e in self.engines.values()):
            return {'status':'blocked','detail':'Resolve open paper exposure before resetting.'}
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.
        for e in self.engines.values():e._new_session()
        return self.snapshot()
    def snapshot(self,asset='BTC',include_assets=False):
        if asset not in self.engines:raise ValueError('unsupported asset')
        self.entry_allowed()
        snaps={a:e.snapshot() for a,e in self.engines.items()}
        lanes=[]
        for a,s in snaps.items():
            p=s['paper'];m=s.get('market') or {};pair=s.get('pair') or {}
            lanes.append({'asset':a,'market':m.get('slug'),'question':m.get('question'),'interval':'5m',
                'feed':s['feed'],'spot':s.get('spot'),'spotAgeSec':s.get('spotAgeSec'),'twapAgeSec':s.get('twapAgeSec'),
                'decision':s.get('decision'),'mode':s.get('mode'),'allocation':self.engines[a].balance,
                'equity':p.get('equity',self.engines[a].balance) if p else self.engines[a].balance,
                'realizedPnl':p.get('realizedPnl',0),'totalPnl':p.get('totalPnl',0),'trades':p.get('trades',0),
                'status':p.get('status','warming'),'pairCost':pair.get('takerPairCost'),
                'inventoryFills':p.get('inventoryFills',0),'matchedSets':p.get('matchedSets',0),
                'pairTarget':self.engines[a].pair_target,'residual':self.engines[a]._inventory_has_residual()})
        equity=sum(l['equity'] for l in lanes)
        selected=snaps[asset]
        selected['portfolio']={'revision':ENGINE_REVISION,'paperOnly':True,'selectedAsset':asset,
            'startingBalance':TOTAL_BALANCE,'equity':equity,'totalPnl':equity-TOTAL_BALANCE,
            'realizedPnl':sum(l['realizedPnl'] for l in lanes),'trades':sum(l['trades'] for l in lanes),
            'sessionLossLimitUsd':PORTFOLIO_LOSS_LIMIT,'riskLocked':self.locked,
            'maxDrawdownUsd':self.max_drawdown,'allocations':dict(ALLOCATIONS),
            'assets':lanes,'scope':'crypto-updown-5m','strategy':'fee-aware staged inventory pairing and executable direct sets; directional lag entries disabled'}
        if include_assets:return {**selected,'assets':{a:{k:v for k,v in s.items() if k!='portfolio'} for a,s in snaps.items()}}
        return selected

engine=PaperPortfolio()
