"""Parallel, isolated crypto paper lanes. No signing keys or real order submission."""
from __future__ import annotations
import asyncio,time,json,copy,os,base64,gzip
from pathlib import Path
import httpx
from poly_server_demo import PolyServerDemo,ENGINE_REVISION

ASSETS=('BTC','ETH','SOL','XRP')
ALLOCATIONS={'BTC':60.0,'ETH':80.0,'SOL':30.0,'XRP':30.0}
TOTAL_BALANCE=sum(ALLOCATIONS.values())
PORTFOLIO_LOSS_LIMIT=6.0

def merge_release_recovery(checkpoint,live):
    """A later wall clock cannot roll a known session's ledger backwards."""
    if not str(live.get('revision','')).startswith(('14.','15.','16.')) or not live.get('assets'):return checkpoint
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
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.;self.started=False;self.research=None;self.ledger_history={a:{} for a in ASSETS}
        from poly_attribution import Attribution
        self.attribution=Attribution();self.storage=None;self.durable_recovery=None
        self.data_root=Path(os.environ.get('POLY_DATA_DIR','/tmp'));self.data_root.mkdir(parents=True,exist_ok=True)
        self.engines={asset:PolyServerDemo(asset=asset,balance=ALLOCATIONS[asset],
            state_file=self.data_root/f'poly_v14_{asset.lower()}_paper.json',portfolio_guard=self.entry_allowed,
            risk_profile={'momentumEnabled':True,'walletQuotes':True,'residualCap':min(2.0,ALLOCATIONS[asset]*.025),'firstPriceCap':.60,
                'stopLoss':min(.40,ALLOCATIONS[asset]*.005),'sessionLossLimit':ALLOCATIONS[asset]*.05}) for asset in ASSETS}
        for lane in self.engines.values():lane.trade_observer=self.attribution.add
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
        parents={e.state_file.parent for e in self.engines.values()}
        if len(parents)==1:self.data_root=next(iter(parents))
        try:
            saved_history=json.loads((self.data_root/'poly_recorded_history.json').read_text())
            for asset,history in saved_history.items():
                if asset in self.ledger_history:self.ledger_history[asset].update(history)
        except (OSError,ValueError,TypeError):pass
        # Preserve the paper ledger across this release on ephemeral hosting.
        # Existing local state always wins; a prior live instance is preferred to the checkpoint.
        recovery={}
        try:recovery=json.loads(Path(__file__).with_name('paper_recovery.json').read_text())
        except Exception:pass
        # Private release recovery belongs in the owner's service configuration,
        # never in a newly published source-code checkpoint.
        encoded=os.environ.get('POLY_PAPER_CHECKPOINT_B64','')
        if encoded:
            try:
                private=json.loads(gzip.decompress(base64.b64decode(encoded,validate=True)))
                recovery=merge_release_recovery(recovery,private)
            except Exception:
                raise ValueError('Private paper recovery checkpoint is invalid; refusing to reset the ledger') from None
        if self.durable_recovery:recovery=merge_release_recovery(recovery,self.durable_recovery)
        checkpoint=copy.deepcopy(recovery)
        try:
            if self.durable_recovery:raise RuntimeError('durable recovery is authoritative')
            async with httpx.AsyncClient(timeout=5) as client:
                # Optional live handover from a previous engine instance (e.g. during a migration).
                handover=os.environ.get('POLY_HANDOVER_URL','').strip().rstrip('/')
                if not handover:raise RuntimeError('no handover source configured')
                response=await client.get(handover+'/api/poly/demo/server',params={'all_assets':'true'})
                response.raise_for_status();live=response.json()
                recovery=merge_release_recovery(recovery,live)
        except Exception:pass
        self.locked=bool((recovery.get('portfolio') or {}).get('riskLocked',False))
        self.max_drawdown=max(self.max_drawdown,float((recovery.get('portfolio') or {}).get('maxDrawdownUsd') or 0))
        self.peak=max(self.peak,float((recovery.get('portfolio') or {}).get('peakEquity') or (recovery.get('portfolio') or {}).get('equity') or TOTAL_BALANCE))
        if recovery.get('strategyConfig'):self.update_config(recovery['strategyConfig'])
        for asset,e in self.engines.items():
            saved=(recovery.get('assets') or {}).get(asset);p=(saved or {}).get('paper') or {}
            use_saved=not e.state_file.exists()
            if self.durable_recovery and e.state_file.exists():
                try:use_saved=float(json.loads(e.state_file.read_text()).get('serverTime',0))<float(saved.get('serverTime',recovery.get('serverTime',0)))
                except (ValueError,OSError):use_saved=True
            if use_saved and p.get('version')==14 and p.get('startingBalance')==e.balance:
                e.state_file.write_text(json.dumps(saved))
        await asyncio.gather(*(e.start() for e in self.engines.values()))
        for source in (checkpoint,recovery):
            for asset,history in (source.get('ledgerHistory') or {}).items():
                if asset in self.ledger_history:self.ledger_history[asset].update(copy.deepcopy(history))
            for asset,saved in (source.get('assets') or {}).items():
                if asset in self.engines:
                    prior=saved.get('paper') or {};current=self.engines[asset].paper or {}
                    if prior.get('sessionId')!=current.get('sessionId'):self._archive(asset,prior)
        for asset,e in self.engines.items():self.ledger_history[asset].pop((e.paper or {}).get('sessionId'),None)
        self._save_history()

    def _save_history(self):
        if not self.started:return
        target=self.data_root/'poly_recorded_history.json';temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.ledger_history,separators=(',',':')));temporary.replace(target)

    def _archive(self,asset,paper):
        if not paper.get('sessionId') or not (paper.get('trades') or paper.get('fees') or paper.get('realizedPnl')):return
        self.ledger_history[asset][paper['sessionId']]={k:copy.deepcopy(paper.get(k)) for k in ('sessionId','startedAt','endsAt','startingBalance','realizedPnl','fees','trades','wins','losses','events')}

    def start_research(self):
        if self.research is None:
            from poly_shadow import ResearchHub
            self.research=ResearchHub(self.engines,str(self.data_root/'poly_v16_research.sqlite3'));self.research.start()

    async def suspend(self):
        if self.research is not None:await self.research.close();self.research=None
        for e in self.engines.values():
            for task in e.tasks:task.cancel()
            await asyncio.gather(*e.tasks,return_exceptions=True)
            e.pending=False;e._last_saved_at=0;e._save();e.started=False;e.tasks=[]
        self.started=False

    async def shutdown(self):
        if self.research is not None:await self.research.close();self.research=None
        for e in self.engines.values():
            for task in e.tasks:task.cancel()
            await asyncio.gather(*e.tasks,return_exceptions=True);e.close()

    async def reset(self):
        # Do not erase unresolved or pending simulated exposure.
        if any(e.pending or (e.paper and (e.paper.get('position') or e._inventory_has_residual())) for e in self.engines.values()):
            return {'status':'blocked','detail':'Resolve open paper exposure before resetting.'}
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.
        for asset,e in self.engines.items():self._archive(asset,e.paper or {});e._new_session()
        self._save_history()
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
        selected=dict(snaps[asset])
        prior_pnl=sum(float(p.get('realizedPnl') or 0) for h in self.ledger_history.values() for p in h.values())
        prior_trades=sum(int(p.get('trades') or 0) for h in self.ledger_history.values() for p in h.values())
        selected['ledgerHistory']=copy.deepcopy(self.ledger_history)
        selected['strategyConfig']=self.get_config()
        selected['infrastructure']=self.storage.status() if self.storage else {'backend':'local_only','configured':False}
        selected['attribution']=self.attribution.summary()
        selected['research']=self.research.snapshot() if self.research else {'mode':'SHADOW_ONLY','executionEnabled':False,'training':{'state':'starting'}}
        selected['portfolio']={'revision':ENGINE_REVISION,'paperOnly':True,'selectedAsset':asset,
            'startingBalance':TOTAL_BALANCE,'equity':equity,'totalPnl':equity-TOTAL_BALANCE,
            'realizedPnl':sum(l['realizedPnl'] for l in lanes),'trades':sum(l['trades'] for l in lanes),
            'priorSessionRealizedPnl':prior_pnl,'lifetimeRealizedPnl':prior_pnl+sum(l['realizedPnl'] for l in lanes),'lifetimeTrades':prior_trades+sum(l['trades'] for l in lanes),
            'sessionLossLimitUsd':PORTFOLIO_LOSS_LIMIT,'riskLocked':self.locked,
            'maxDrawdownUsd':self.max_drawdown,'peakEquity':self.peak,'allocations':dict(ALLOCATIONS),
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

