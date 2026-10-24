"""Parallel, isolated crypto paper lanes. No signing keys or real order submission."""
from __future__ import annotations
import asyncio,time,json,copy,os,base64,gzip
from pathlib import Path
import httpx
from poly_server_demo import PolyServerDemo,ENGINE_REVISION

# Lanes: key -> (underlying, round seconds, paper allocation). SOL/XRP 5m were retired in V16.4:
# their books routinely quoted spreads above 3c and a $30 allocation implied a $0.15 stop that
# fees plus spread exceeded, so momentum could almost never enter (4 lifetime trades, all losses).
# Their capital funds BTC/ETH 15-minute lanes. Retired history stays in the lifetime ledger.
LANES={'BTC':('BTC',300,60.0),'ETH':('ETH',300,80.0),'BTC15':('BTC',900,30.0),'ETH15':('ETH',900,30.0)}
RETIRED=('SOL','XRP')
ASSETS=tuple(LANES)
ALLOCATIONS={k:v[2] for k,v in LANES.items()}
LANE_STOP_FLOOR=.30   # spread + both taker fees on 5 shares near 50c is ~$0.22; a smaller stop blocks entries
TOTAL_BALANCE=sum(ALLOCATIONS.values())
PORTFOLIO_LOSS_LIMIT=6.0
# Overnight guards measured from the fresh start (realized P&L across every session since it):
# - stop new entries for good if it falls to -$8 (owner decides what happens next);
# - once it has reached +$4, pause entries for an hour whenever half the peak gain is given back.
EPOCH_LOSS_LIMIT=8.0
GIVEBACK_ARM_USD=4.0
GIVEBACK_FRACTION=0.5
GIVEBACK_PAUSE_SEC=3600.
AUTO_CYCLE=os.environ.get('POLY_AUTO_CYCLE','1').strip()!='0'
LANE_LOCK_COOLDOWN_SEC=1800.
PORTFOLIO_LOCK_COOLDOWN_SEC=3600.

def compatible_revision(rev):
    """Ledger format is stable from V14 on; accept every later major (17, 18, ...)."""
    try:return int(str(rev).split('.')[0])>=14
    except (TypeError,ValueError):return False

def merge_release_recovery(checkpoint,live):
    """A later wall clock cannot roll a known session's ledger backwards."""
    if not compatible_revision(live.get('revision','')) or not live.get('assets'):return checkpoint
    if float(live.get('serverTime') or 0)<float(checkpoint.get('serverTime') or 0):return checkpoint
    merged=copy.deepcopy(live)
    for asset,saved in (checkpoint.get('assets') or {}).items():
        prior=saved.get('paper') or {};incoming=((live.get('assets') or {}).get(asset) or {}).get('paper') or {}
        same_session=prior.get('sessionId') and prior.get('sessionId')==incoming.get('sessionId')
        regressed=same_session and any(float(incoming.get(k) or 0)+1e-8<float(prior.get(k) or 0) for k in ('trades','momentumFills','inventoryFills','fees'))
        if not incoming or regressed:
            merged['assets'][asset]=copy.deepcopy(saved)
            # A lane missing from the live snapshot (retired SOL/XRP) is carried over silently;
            # only a same-session ledger going backwards is a real regression.
            if regressed:print('POLY_RECOVERY_LEDGER_REGRESSION',asset,flush=True)
    cp=checkpoint.get('portfolio') or {};mp=merged.setdefault('portfolio',{})
    mp['riskLocked']=bool(mp.get('riskLocked') or cp.get('riskLocked'))
    if float(cp.get('epochStart') or 0)==float(mp.get('epochStart') or 0):
        mp['maxDrawdownUsd']=max(float(mp.get('maxDrawdownUsd') or 0),float(cp.get('maxDrawdownUsd') or 0))
    return merged

class PaperPortfolio:
    def __init__(self):
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.;self.epoch=0.;self.epoch_peak=0.;self.guard_until=0.;self.guard_reason=None;self.started=False;self.research=None;self.ledger_history={a:{} for a in ASSETS+RETIRED}
        from poly_attribution import Attribution
        self.attribution=Attribution();self.storage=None;self.durable_recovery=None
        self.data_root=Path(os.environ.get('POLY_DATA_DIR','/tmp'));self.data_root.mkdir(parents=True,exist_ok=True)
        self.engines={lane:PolyServerDemo(asset=coin,balance=alloc,interval=iv,lane=lane,
            state_file=self.data_root/f'poly_v14_{lane.lower()}_paper.json',portfolio_guard=self.entry_allowed,
            risk_profile={'momentumEnabled':True,'walletQuotes':True,'residualCap':min(2.0,alloc*.025),'firstPriceCap':.60,
                'stopLoss':max(LANE_STOP_FLOOR,min(.40,alloc*.005)),'sessionLossLimit':max(1.5,alloc*.05)}) for lane,(coin,iv,alloc) in LANES.items()}
        from poly_edge import EdgeLab
        import uuid
        self.edge=EdgeLab(self.engines);self.handover=None;self.handover_edge=None;self.handover_rows=None
        self.boot_id=uuid.uuid4().hex;self.boot_at=time.time();self.yielded=False
        self.lock_seen={};self.cycle_task=None;self.cycles=[]
        for lane in self.engines.values():
            lane.trade_observer=self._observe_trade;lane.edge=self.edge;lane.loss_budget=self.loss_budget;lane.entry_pause=self.pause_reason
        self._restore_edge_file()
        from poly_scout import AssetScout
        self.scout=AssetScout(self.engines,ledger=self._lane_results)   # read-only market-finding agent
    def _observe_trade(self,row):
        self.attribution.add(row);self.edge.on_trade(row)
        try:
            keep=('asset','strategy','side','entryAt','closedAt','quantity','entryPrice','exitPrice','netPnlUsd','feesUsd','holdSec','exitReason','stopOvershootUsd','entryLatencyMs','exitLatencyMs','entrySlippageUsd','entryFeatures')
            print('POLY_TRADE',json.dumps({k:row.get(k) for k in keep},separators=(',',':'),default=str),flush=True)
        except Exception:pass
    def loss_budget(self):
        equity=sum(e._equity() if e.paper else e.balance for e in self.engines.values())
        realized=sum(float((e.paper or {}).get('realizedPnl') or 0) for e in self.engines.values())
        return max(0.,PORTFOLIO_LOSS_LIMIT+min(0.,realized,equity-TOTAL_BALANCE))
    # ---- EdgeLab persistence without a database (local file + live handover) ----
    def _edge_file(self):return self.data_root/'poly_edge_state.json'
    def _restore_edge_file(self):
        try:self.edge.restore(json.loads(self._edge_file().read_text()))
        except (OSError,ValueError,TypeError):pass
    def save_edge_file(self):
        try:
            target=self._edge_file();tmp=target.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.edge.export(),separators=(',',':')));tmp.replace(target)
        except (OSError,ValueError,TypeError):pass
    async def prefetch_handover(self):
        """Read the previous instance's ledger before this instance accepts traffic."""
        handover=os.environ.get('POLY_HANDOVER_URL','').strip().rstrip('/')
        if not handover:return
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                r=await client.get(handover+'/api/poly/demo/server',params={'all_assets':'true'});r.raise_for_status();self.handover=r.json()
                try:
                    r=await client.get(handover+'/api/poly/edge/state');r.raise_for_status();self.handover_edge=r.json()
                except Exception:pass
                try:
                    r=await client.get(handover+'/api/poly/research/rows',timeout=25);r.raise_for_status();self.handover_rows=r.json().get('rows')
                except Exception:pass
            print('POLY_HANDOVER_PREFETCHED',bool(self.handover),bool(self.handover_edge),len(self.handover_rows or []),flush=True)
        except Exception as exc:print('POLY_HANDOVER_UNAVAILABLE',type(exc).__name__,flush=True)
    def _seed_trade_record(self):
        """The session ledgers are the complete closed-trade history; rebuild the scorecard record from them."""
        try:
            import poly_devlog
            rows=poly_devlog.trade_rows(self)
            if len(rows)>=len(self.edge.trades):
                self.edge.trades.clear()
                self.edge.trades.extend({'pnl':float(r['pnl']),'at':float(r['exitAt'] or 0),'strategy':r['mode'],'asset':r['asset']} for r in rows if r['pnl'] is not None)
        except Exception as exc:print('POLY_TRADE_SEED_ERROR',type(exc).__name__,flush=True)
    async def yield_to(self,successor):
        """A newer instance now serves this URL: stop all trading and research here so the two never
        manage the same handed-over positions. Without a database lock this is the only ownership signal."""
        if self.yielded:return
        self.yielded=True
        print('POLY_YIELDED_TO_SUCCESSOR',successor,flush=True)
        await self.suspend()
    # ---- autonomous session rollover: losses stay in the lifetime ledger ----
    def _exposed(self,e):
        return bool(e.pending or (e.paper and (e.paper.get('position') or e._inventory_has_residual())))
    def auto_cycle(self,now=None):
        now=time.time() if now is None else now;actions=[]
        if not AUTO_CYCLE:return actions
        if self.locked:
            self.lock_seen.setdefault('portfolio',now)
            if now-self.lock_seen['portfolio']>=PORTFOLIO_LOCK_COOLDOWN_SEC and not any(self._exposed(e) for e in self.engines.values()):
                self.locked=False;self.peak=sum(e._equity() if e.paper else e.balance for e in self.engines.values());self.max_drawdown=0.
                for asset,e in self.engines.items():self._archive(asset,e.paper or {});e._new_session()
                self.lock_seen.clear();actions.append('portfolio');self._save_history()
            return actions
        self.lock_seen.pop('portfolio',None)
        for asset,e in self.engines.items():
            p=e.paper or {};status=p.get('status')
            if status=='risk_locked':self.lock_seen.setdefault(asset,now)
            else:self.lock_seen.pop(asset,None)
            due=status=='complete' or (status=='risk_locked' and now-self.lock_seen.get(asset,now)>=LANE_LOCK_COOLDOWN_SEC)
            if due and not self._exposed(e):
                self._archive(asset,p);e._new_session();self.lock_seen.pop(asset,None);actions.append(asset)
        if actions:self._save_history()
        return actions
    def fresh_start_due(self):
        """Owner-requested fresh start (POLY_FRESH_START_AT, unix seconds): the paper account restarts at
        $200 and the headline P&L counts only trades after it. Earlier sessions stay in the ledger history
        for analysis (trade CSV, backtests, readiness evidence). Applied once, only when flat."""
        try:at=float(os.environ.get('POLY_FRESH_START_AT') or 0)
        except ValueError:return 0.
        return at if at>self.epoch+1 else 0.
    def apply_fresh_start(self,now=None):
        at=self.fresh_start_due()
        if not at or any(self._exposed(e) for e in self.engines.values()):return False
        self.locked=False;self.peak=TOTAL_BALANCE;self.max_drawdown=0.;self.lock_seen.clear()
        for asset,e in self.engines.items():self._archive(asset,e.paper or {});e._new_session()
        self.epoch=at;self.epoch_peak=0.;self.guard_until=0.;self.guard_reason=None;self._save_history()
        print('POLY_FRESH_START',at,flush=True)
        return True
    def _in_epoch(self,session):
        return float(session.get('startedAt') or 0)>=self.epoch
    async def cycle_worker(self):
        while True:
            await asyncio.sleep(30)
            try:
                if self.fresh_start_due() and self.apply_fresh_start():
                    self.cycles=(self.cycles+[{'at':time.time(),'lanes':['fresh-start']}])[-50:]
                done=self.auto_cycle()
                if done:
                    self.cycles=(self.cycles+[{'at':time.time(),'lanes':done}])[-50:]
                    print('POLY_AUTO_CYCLE',done,flush=True)
                if not (self.storage and self.storage.url):self.save_edge_file()
            except asyncio.CancelledError:raise
            except Exception as exc:print('POLY_AUTO_CYCLE_ERROR',type(exc).__name__,flush=True)
    def entry_allowed(self):
        if self.yielded:return False
        equity=sum(e._equity() if e.paper else e.balance for e in self.engines.values())
        realized=sum(float((e.paper or {}).get('realizedPnl') or 0) for e in self.engines.values())
        self.peak=max(self.peak,equity);self.max_drawdown=max(self.max_drawdown,self.peak-equity)
        if min(realized,equity-TOTAL_BALANCE)<=-PORTFOLIO_LOSS_LIMIT:self.locked=True
        return not self.locked
    def pause_reason(self):
        """Overnight guard: blocks new entries without locking lanes; open positions keep their exits."""
        realized=sum(float((e.paper or {}).get('realizedPnl') or 0) for e in self.engines.values())
        return None if self._epoch_guard(realized) else self.guard_reason
    def epoch_pnl(self,realized=None):
        if realized is None:realized=sum(float((e.paper or {}).get('realizedPnl') or 0) for e in self.engines.values())
        current={(e.paper or {}).get('sessionId') for e in self.engines.values()}
        return realized+sum(float(s.get('realizedPnl') or 0) for h in self.ledger_history.values() for sid,s in h.items() if sid not in current and self._in_epoch(s))
    def _epoch_guard(self,realized,now=None):
        if not self.epoch:return True
        now=time.time() if now is None else now;pnl=self.epoch_pnl(realized);self.epoch_peak=max(self.epoch_peak,pnl)
        if pnl<=-EPOCH_LOSS_LIMIT:
            self.guard_reason=f'overnight loss limit reached ({pnl:+.2f} since fresh start); entries stopped';return False
        if self.epoch_peak>=GIVEBACK_ARM_USD and pnl<=self.epoch_peak*GIVEBACK_FRACTION and now>=self.guard_until:
            self.guard_until=now+GIVEBACK_PAUSE_SEC;self.epoch_peak=pnl   # re-arm from the new level after the pause
            self.guard_reason=f'gave back half of the peak gain; entries paused 60 min at {pnl:+.2f}'
            print('POLY_GIVEBACK_PAUSE',round(pnl,4),flush=True)
        if now<self.guard_until:return False
        self.guard_reason=None;return True
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
        # Owner-supplied archive of closed sessions (service env, never the public repo). Adds sessions
        # missing after an ephemeral-disk loss; never overwrites a session already present.
        encoded=os.environ.get('POLY_LEDGER_ARCHIVE_B64','')
        if encoded:
            try:
                archive=json.loads(gzip.decompress(base64.b64decode(encoded,validate=True)))
                added=0
                for lane,sessions in archive.items():
                    if lane in self.ledger_history and isinstance(sessions,dict):
                        for sid,session in sessions.items():
                            if sid not in self.ledger_history[lane]:self.ledger_history[lane][sid]=session;added+=1
                print('POLY_LEDGER_ARCHIVE_RESTORED',added,flush=True)
            except Exception as exc:print('POLY_LEDGER_ARCHIVE_INVALID',type(exc).__name__,flush=True)
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
            # Prefetched before this instance opened its port (see prefetch_handover).
            if not self.handover:raise RuntimeError('no handover snapshot')
            recovery=merge_release_recovery(recovery,self.handover)
        except Exception:pass
        self.locked=bool((recovery.get('portfolio') or {}).get('riskLocked',False))
        self.epoch=max(self.epoch,float((recovery.get('portfolio') or {}).get('epochStart') or 0))
        # Drawdown and peak carry over only from a checkpoint of the same fresh-start period; an older
        # snapshot (e.g. the committed release checkpoint) must not leak a pre-reset drawdown.
        if float((recovery.get('portfolio') or {}).get('epochStart') or 0)==self.epoch:
            self.max_drawdown=max(self.max_drawdown,float((recovery.get('portfolio') or {}).get('maxDrawdownUsd') or 0))
            self.peak=max(self.peak,float((recovery.get('portfolio') or {}).get('peakEquity') or (recovery.get('portfolio') or {}).get('equity') or TOTAL_BALANCE))
        pf0=recovery.get('portfolio') or {}
        if self.epoch and float(pf0.get('epochStart') or 0)==self.epoch:
            self.epoch_peak=max(self.epoch_peak,float(pf0.get('epochPeakPnl') or 0));self.guard_until=float((pf0.get('guard') or {}).get('pausedUntil') or 0)
        if recovery.get('strategyConfig'):self.update_config(recovery['strategyConfig'])
        for asset in RETIRED:   # keep the final session of a retired lane in the lifetime ledger
            self._archive(asset,((recovery.get('assets') or {}).get(asset) or {}).get('paper') or {})
        for asset,e in self.engines.items():
            saved=(recovery.get('assets') or {}).get(asset);p=(saved or {}).get('paper') or {}
            use_saved=not e.state_file.exists()
            if self.durable_recovery and e.state_file.exists():
                try:use_saved=float(json.loads(e.state_file.read_text()).get('serverTime',0))<float(saved.get('serverTime',recovery.get('serverTime',0)))
                except (ValueError,OSError):use_saved=True
            if use_saved and p.get('version')==14 and p.get('startingBalance')==e.balance:
                e.state_file.write_text(json.dumps(saved))
        if self.handover_edge and not self.durable_recovery and not self.edge.restored:self.edge.restore(self.handover_edge)
        self.import_edge_archive()
        await asyncio.gather(*(e.start() for e in self.engines.values()))
        self.edge.start()
        try:self.scout.start()          # isolated: a scout failure can never stop trading
        except Exception as exc:print('POLY_SCOUT_START_ERROR',type(exc).__name__,flush=True)
        if self.cycle_task is None:self.cycle_task=asyncio.create_task(self.cycle_worker(),name='auto-cycle')
        for source in (checkpoint,recovery):
            for asset,history in (source.get('ledgerHistory') or {}).items():
                if asset in self.ledger_history:self.ledger_history[asset].update(copy.deepcopy(history))
            for asset,saved in (source.get('assets') or {}).items():
                if asset in self.engines:
                    prior=saved.get('paper') or {};current=self.engines[asset].paper or {}
                    if prior.get('sessionId')!=current.get('sessionId'):self._archive(asset,prior)
        for asset,e in self.engines.items():self.ledger_history[asset].pop((e.paper or {}).get('sessionId'),None)
        self._save_history()
        self._seed_trade_record()  # after archived ledgers are merged: they hold the full history

    def import_edge_archive(self):
        """Owner archive of settled shadow bets; idempotent, so it is re-applied after any later state restore."""
        encoded=os.environ.get('POLY_EDGE_BETS_B64','')
        if not encoded:return
        try:print('POLY_EDGE_BETS_RESTORED',self.edge.import_bets(json.loads(gzip.decompress(base64.b64decode(encoded,validate=True)))),flush=True)
        except Exception as exc:print('POLY_EDGE_BETS_INVALID',type(exc).__name__,flush=True)

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
            # The V16 research schema and labels are defined on 5-minute rounds only.
            five={k:e for k,e in self.engines.items() if e.interval==300}
            self.research=ResearchHub(five,str(self.data_root/'poly_v16_research.sqlite3'))
            self.research.on_pair_outcome=self.edge.on_pair_outcome
            if self.handover_rows:
                known={r.get('id') for r in self.research.rows}
                for r in self.handover_rows:
                    if isinstance(r,dict) and r.get('id') and r['id'] not in known:known.add(r['id']);self.research.rows.append(r)
            # POLY_RESEARCH=0 keeps the research data (labels, model, maker record) but runs none of its
            # background work: it never trades, and on the 0.1-CPU host it competed with the trading loop.
            if os.environ.get('POLY_RESEARCH','1')!='0':self.research.start()
            else:print('POLY_RESEARCH_PAUSED',flush=True)

    def _lane_results(self):
        """Closed-trade results per traded market since the fresh start, keyed like the scout ('btc-5m')."""
        import poly_devlog
        out={}
        for r in poly_devlog.trade_rows(self):
            if float(r.get('entryAt') or 0)<self.epoch or r.get('pnl') is None:continue
            e=self.engines.get(r['asset']);k=f'{e.asset.lower()}-{e.tag}' if e else r['asset']
            d=out.setdefault(k,{'trades':0,'netPnlUsd':0.});d['trades']+=1;d['netPnlUsd']=round(d['netPnlUsd']+float(r['pnl']),4)
        return out
    def scout_snapshot(self):
        now=time.time();c=getattr(self,'_scout_cache',None)
        if c is None or now-c[0]>30:
            try:c=self._scout_cache=(now,self.scout.snapshot())
            except Exception as exc:c=(now,{'mode':'READ_ONLY','error':type(exc).__name__})
        return c[1]
    async def _stop_aux(self):
        try:await self.scout.close()
        except Exception:pass
        if self.cycle_task:self.cycle_task.cancel();await asyncio.gather(self.cycle_task,return_exceptions=True);self.cycle_task=None
        await self.edge.close()
    async def suspend(self):
        await self._stop_aux()
        if self.research is not None:await self.research.close();self.research=None
        for e in self.engines.values():
            for task in e.tasks:task.cancel()
            await asyncio.gather(*e.tasks,return_exceptions=True)
            e.pending=False;e._last_saved_at=0;e._save();e.started=False;e.tasks=[]
        self.started=False

    async def shutdown(self):
        await self._stop_aux()
        if not (self.storage and self.storage.url):self.save_edge_file()
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
            lanes.append({'asset':a,'coin':self.engines[a].asset,'market':m.get('slug'),'question':m.get('question'),'interval':self.engines[a].tag,
                'feed':s['feed'],'spot':s.get('spot'),'spotAgeSec':s.get('spotAgeSec'),'twapAgeSec':s.get('twapAgeSec'),
                'decision':s.get('decision'),'mode':s.get('mode'),'allocation':self.engines[a].balance,
                'equity':p.get('equity',self.engines[a].balance) if p else self.engines[a].balance,
                'realizedPnl':p.get('realizedPnl',0),'totalPnl':p.get('totalPnl',0),'trades':p.get('trades',0),
                'status':p.get('status','warming'),'pairCost':pair.get('takerPairCost'),
                'inventoryFills':p.get('inventoryFills',0),'matchedSets':p.get('matchedSets',0),
                'pairTarget':self.engines[a].pair_target,'residual':self.engines[a]._inventory_has_residual()})
        equity=sum(l['equity'] for l in lanes)
        selected=dict(snaps[asset])
        prior_pnl=sum(float(p.get('realizedPnl') or 0) for h in self.ledger_history.values() for p in h.values() if self._in_epoch(p))
        prior_trades=sum(int(p.get('trades') or 0) for h in self.ledger_history.values() for p in h.values() if self._in_epoch(p))
        arch_pnl=sum(float(p.get('realizedPnl') or 0) for h in self.ledger_history.values() for p in h.values() if not self._in_epoch(p))
        arch_trades=sum(int(p.get('trades') or 0) for h in self.ledger_history.values() for p in h.values() if not self._in_epoch(p))
        selected['ledgerHistory']=copy.deepcopy(self.ledger_history)
        selected['strategyConfig']=self.get_config()
        selected['infrastructure']=self.storage.status() if self.storage else {'backend':'local_only','configured':False}
        selected['attribution']=self.attribution.summary()
        now=time.time();cache=getattr(self,'_rules_cache',None)
        if cache is None or now-cache[0]>5:
            import poly_devlog
            try:cache=self._rules_cache=(now,poly_devlog.rules_scorecard(self))
            except Exception:cache=(now,None)
        selected['rulesScorecard']=cache[1]
        selected['scout']=self.scout_snapshot()
        selected['edge']={**self.edge.snapshot(TOTAL_BALANCE),'autoCycle':{'enabled':AUTO_CYCLE,'laneLockCooldownSec':LANE_LOCK_COOLDOWN_SEC,'portfolioLockCooldownSec':PORTFOLIO_LOCK_COOLDOWN_SEC,'recent':self.cycles[-10:]}}
        selected['research']=self.research.snapshot() if self.research else {'mode':'SHADOW_ONLY','executionEnabled':False,'training':{'state':'starting'}}
        selected['portfolio']={'revision':ENGINE_REVISION,'paperOnly':True,'selectedAsset':asset,
            'startingBalance':TOTAL_BALANCE,'equity':equity,'totalPnl':equity-TOTAL_BALANCE,
            'realizedPnl':sum(l['realizedPnl'] for l in lanes),'trades':sum(l['trades'] for l in lanes),
            'priorSessionRealizedPnl':prior_pnl,'lifetimeRealizedPnl':prior_pnl+sum(l['realizedPnl'] for l in lanes),'lifetimeTrades':prior_trades+sum(l['trades'] for l in lanes),
            'epochStart':self.epoch or None,'epochPnl':self.epoch_pnl() if self.epoch else None,'epochPeakPnl':self.epoch_peak if self.epoch else None,
            'guard':{'reason':self.guard_reason,'pausedUntil':self.guard_until or None,'lossLimitUsd':EPOCH_LOSS_LIMIT,'givebackArmUsd':GIVEBACK_ARM_USD,'givebackFraction':GIVEBACK_FRACTION},'archivedRealizedPnl':arch_pnl,'archivedTrades':arch_trades,
            'sessionLossLimitUsd':PORTFOLIO_LOSS_LIMIT,'riskLocked':self.locked,
            'maxDrawdownUsd':self.max_drawdown,'peakEquity':self.peak,'allocations':dict(ALLOCATIONS),
            'assets':lanes,'retiredLanes':list(RETIRED),'scope':'crypto-updown BTC/ETH 5m and 15m','strategy':'promoted calibrated-mispricing and research-model strategies first, then fee-aware momentum and executable complete sets; one entry per asset per round'}
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
                    'strategyMaxStakeUsd':round(self.engines[asset].balance*.15,2),'strategySizing':'quarter-Kelly, max 15% of allocation',
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

