"""Parallel, isolated crypto paper lanes. No signing keys or real order submission."""
from __future__ import annotations
import asyncio,time,json,copy
from pathlib import Path
import httpx
from poly_server_demo import PolyServerDemo,ENGINE_REVISION

ASSETS=('BTC','ETH','SOL','XRP')
ALLOCATIONS={'BTC':60.0,'ETH':80.0,'SOL':30.0,'XRP':30.0}
TOTAL_BALANCE=sum(ALLOCATIONS.values())
PORTFOLIO_LOSS_LIMIT=6.0

def merge_release_recovery(checkpoint,live):
    """A later wall clock cannot roll a known session's ledger backwards."""
    if not str(live.get('revision','')).startswith(('14.','15.')) or not live.get('assets'):return checkpoint
    if float(live.get('serverTime') or 0)<float(checkpoint.get('serverTime') or 0):return checkpoint
    merged=copy.deepcopy(live)
    for asset,saved in (checkpoint.get('assets') or {}).items():
        prior=saved.get('paper') or {};incoming=((live.get('assets') or {}).get(asset) or {}).get('paper') or {}
        same_session=prior.get('sessionId') and prior.get('sessionId')==incoming.get('sessionId')
        regressed=same_session and any(float(incoming.get(k) or 0)+1e-8<float(prior.get(k) or 0) for k in ('trades','momentumFills','inventoryFills','fees'))
        if not incoming or regressed:
            merged['assets'][asset]=copy.deepcopy(saved)
            print('POLY_RECOVERY_LEDGER_REGRESSION',asset,flush=True)
    cp=checkpoint.get('portfolio') or {};mp=merged.setdefault('portfolio',{})
    mp['riskLocked']=bool(mp.get('riskLocked') or cp.get('riskLocked'))
    mp['maxDrawdownUsd']=max(float(mp.get('maxDrawdownUsd') or 0),float(cp.get('maxDrawdownUsd') or 0))
    return merged

class PaperPortfolio:
    def __init__(self):
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.;self.started=False
        self.engines={asset:PolyServerDemo(asset=asset,balance=ALLOCATIONS[asset],
            state_file=f'/tmp/poly_v14_{asset.lower()}_paper.json',portfolio_guard=self.entry_allowed,
            risk_profile={'momentumEnabled':True,'walletQuotes':True,'residualCap':min(2.0,ALLOCATIONS[asset]*.025),'firstPriceCap':.60,
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
        # Preserve the paper ledger across this release on ephemeral hosting.
        # Existing local state always wins; a prior live instance is preferred to the checkpoint.
        recovery={}
        try:recovery=json.loads(Path(__file__).with_name('paper_recovery.json').read_text())
        except Exception:pass
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response=await client.get('https://poly-edge-paper-engine.onrender.com/api/poly/demo/server',params={'all_assets':'true'})
                response.raise_for_status();live=response.json()
                recovery=merge_release_recovery(recovery,live)
        except Exception:pass
        self.locked=bool((recovery.get('portfolio') or {}).get('riskLocked',False))
        self.max_drawdown=max(self.max_drawdown,float((recovery.get('portfolio') or {}).get('maxDrawdownUsd') or 0))
        for asset,e in self.engines.items():
            saved=(recovery.get('assets') or {}).get(asset);p=(saved or {}).get('paper') or {}
            if not e.state_file.exists() and p.get('version')==14 and p.get('startingBalance')==e.balance and float(p.get('endsAt') or 0)>time.time():
                e.state_file.write_text(json.dumps(saved))
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
            'assets':lanes,'scope':'crypto-updown-5m','strategy':'marketable fee-aware momentum paper experiment and executable complete sets; one entry per asset per round'}
        if include_assets:return {**selected,'assets':{a:{k:v for k,v in s.items() if k!='portfolio'} for a,s in snaps.items()}}
        return selected

    def get_config(self):
        return {
            'totalCapital': TOTAL_BALANCE,
            'portfolioLossLimit': PORTFOLIO_LOSS_LIMIT,
            'locked': self.locked,
            'allocations': dict(ALLOCATIONS),
            'assets': {
                asset: {
                    'allocation': self.engines[asset].balance,
                    'pairTarget': self.engines[asset].pair_target,
                    'residualCap': self.engines[asset].residual_cap,
                    'stopLoss': self.engines[asset].inventory_stop,
                    'sessionLossLimit': self.engines[asset].session_loss_limit,
                    'firstPriceCap': self.engines[asset].first_price_cap,
                    'momentumEnabled':self.engines[asset].momentum_enabled,
                    'momentumNotionalCap':min(3.25,self.engines[asset].balance*.1),
                    'momentumLossStopUsd':self.engines[asset].inventory_stop,'momentumTakeProfitUsd':max(.18,self.engines[asset].inventory_stop),'momentumMaxHoldSec':30,
                }
                for asset in ASSETS
            }
        }

    def update_config(self, updates: dict):
        for asset, cfg in (updates.get('assets') or {}).items():
            if asset in self.engines and isinstance(cfg, dict):
                eng = self.engines[asset]
                if 'pairTarget' in cfg and 0.80 <= float(cfg['pairTarget']) <= 1.05:
                    eng.pair_target = round(float(cfg['pairTarget']), 4)
                if 'residualCap' in cfg and 0.1 <= float(cfg['residualCap']) <= 25.0:
                    eng.residual_cap = round(float(cfg['residualCap']), 3)
                if 'stopLoss' in cfg and 0.05 <= float(cfg['stopLoss']) <= 10.0:
                    eng.inventory_stop = round(float(cfg['stopLoss']), 3)
                if 'firstPriceCap' in cfg and 0.10 <= float(cfg['firstPriceCap']) <= 0.90:
                    eng.first_price_cap = round(float(cfg['firstPriceCap']), 3)
        return self.get_config()

    def get_performance(self):
        lanes = []
        for a in ASSETS:
            eng = self.engines[a]
            p = eng.paper or {}
            trades = int(p.get('trades') or 0)
            wins = int(p.get('wins') or 0)
            losses = int(p.get('losses') or 0)
            realized = float(p.get('realizedPnl') or 0.0)
            equity = eng._equity() if eng.paper else eng.balance
            fees = float(p.get('fees') or 0.0)
            matched = float(p.get('matchedSets') or 0.0)
            lanes.append({
                'asset': a,
                'allocation': eng.balance,
                'equity': round(equity, 2),
                'totalPnl': round(equity - eng.balance, 2),
                'realizedPnl': round(realized, 2),
                'unrealizedPnl': round(equity - eng.balance - realized, 2),
                'trades': trades,
                'wins': wins,
                'losses': losses,
                'winRatePct': round((wins / trades * 100) if trades > 0 else 0.0, 1),
                'fees': round(fees, 4),
                'matchedSets': round(matched, 2),
                'maxDrawdownPct': round(float(p.get('maxDrawdown') or 0.0) * 100, 2),
                'pairTarget': eng.pair_target,
                'status': p.get('status', 'warming')
            })
        total_trades = sum(l['trades'] for l in lanes)
        total_wins = sum(l['wins'] for l in lanes)
        total_losses = sum(l['losses'] for l in lanes)
        total_equity = sum(l['equity'] for l in lanes)
        total_realized = sum(l['realizedPnl'] for l in lanes)
        total_fees = sum(l['fees'] for l in lanes)
        total_matched = sum(l['matchedSets'] for l in lanes)
        return {
            'portfolio': {
                'startingCapital': TOTAL_BALANCE,
                'currentEquity': round(total_equity, 2),
                'totalPnl': round(total_equity - TOTAL_BALANCE, 2),
                'realizedPnl': round(total_realized, 2),
                'unrealizedPnl': round(total_equity - TOTAL_BALANCE - total_realized, 2),
                'totalTrades': total_trades,
                'totalWins': total_wins,
                'totalLosses': total_losses,
                'winRatePct': round((total_wins / total_trades * 100) if total_trades > 0 else 0.0, 1),
                'totalFeesPaid': round(total_fees, 4),
                'totalMatchedSets': round(total_matched, 2),
                'maxDrawdownUsd': round(self.max_drawdown, 2),
                'sessionLossLimitUsd': PORTFOLIO_LOSS_LIMIT,
                'riskLocked': self.locked
            },
            'assets': lanes
        }

    def close(self):
        for e in self.engines.values():
            e.close()

engine=PaperPortfolio()

