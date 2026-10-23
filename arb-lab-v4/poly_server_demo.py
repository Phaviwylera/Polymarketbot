from __future__ import annotations
import asyncio, json, math, time, uuid
from poly_diagnostics import DiagnosticLog
from poly_momentum import signal as momentum_signal
from poly_features import walk as _walk_book
from collections import deque, defaultdict
from pathlib import Path
import httpx
import websockets

POLY_GAMMA='https://gamma-api.polymarket.com'
POLY_CLOB='https://clob.polymarket.com'
COINBASE_REST='https://api.exchange.coinbase.com'
CLOB_WS='wss://ws-subscriptions-clob.polymarket.com/ws/market'
RTDS_WS='wss://ws-live-data.polymarket.com'
STATE_FILE=Path('/tmp/poly_server_demo_v1.json')
DEMO_BALANCE=20.0
MAX_TRADE_NOTIONAL=3.25
MAX_TRADE_LOSS_USD=0.50
SESSION_LOSS_LIMIT_USD=1.50
LOSS_COOLDOWN_SEC=90.0
FEED_FRESH_SEC=5.0
OPEN_STABILIZE_SEC=15.0
LAG_MIN_REMAINING_SEC=18.0
RESOLUTION_WINDOW_SEC=75.0
LAG_LEAD_MIN_BPS=0.50
LAG_IMPULSE_MIN_BPS=0.15
LAG_CONFIRM_SEC=1.00
LAG_ROUNDTRIP_EDGE_MIN=0.04
MAX_ENTRY_CHASE=0.05
EXECUTION_LATENCY_SEC=0.35
COMPLETE_SET_MAX_COST=0.975
COMPLETE_SET_MAX_NOTIONAL=5.25
MAKER_PAIRING_ENABLED=False  # legacy V10/V11 one-sided maker path remains disabled
INVENTORY_V12_ENABLED=True
INVENTORY_PAIR_TARGET=0.985
INVENTORY_MAX_FIRST_PRICE=0.20
INVENTORY_MAX_RESIDUAL_USD=0.50
INVENTORY_MAX_HOLD_SEC=15.0
INVENTORY_STOP_LOSS_USD=0.15
ENGINE_REVISION="16.4-lanes"
SCALP_MODES=("MOMENTUM_SCALP","MODEL_SCALP")
SELF_MANAGED_MODES=("MOMENTUM_SCALP","MODEL_SCALP","EDGE_HOLD")
MODEL_MIN_CONSERVATIVE_RETURN=0.005
INVENTORY_MAX_INITIAL_GAP=0.0
INVENTORY_OPEN_AFTER_SEC=20.0
INVENTORY_STOP_NEW_REMAINING_SEC=75.0
INVENTORY_QUOTE_TTL_SEC=20.0
INVENTORY_MAX_CYCLES_PER_MARKET=3
INVENTORY_MIN_REVERSAL_LEAD_BPS=0.10
INVENTORY_MIN_REVERSAL_IMPULSE_BPS=-0.03
INVENTORY_RECOVERY_PROFIT_USD=0.02
MAKER_PAIR_MAX_SUM=0.99
MAKER_PAIR_TARGET_SUM=0.985
MAKER_SINGLE_LEG_MAX=3.25
MAKER_QUOTE_MIN_REST_SEC=0.45
MAKER_RESIDUAL_MAX_SEC=18.0
MAKER_TAKER_HEDGE_MAX_COST=0.9975

def _jsonish(v):
    if isinstance(v,list): return v
    if not isinstance(v,str): return []
    try:
        x=json.loads(v)
        return x if isinstance(x,list) else []
    except Exception:
        return []

def _best(book, side):
    rows=book.get(side) or []
    vals=[]
    for row in rows:
        try: vals.append((float(row.get('price')),float(row.get('size') or 0)))
        except Exception: pass
    if not vals: return {'price':None,'size':0.0}
    p=max(x[0] for x in vals) if side=='bids' else min(x[0] for x in vals)
    return {'price':p,'size':sum(x[1] for x in vals if x[0]==p)}

def _cdf(x):
    return .5*(1+math.erf(x/math.sqrt(2)))

def _fee_per_share(price):
    return round(0.07*price*(1-price),5)

def _floor_tick(price,tick):
    tick=max(0.001,float(tick or 0.01))
    return round(math.floor((float(price)+1e-12)/tick)*tick,6)

class PolyServerDemo:
    def __init__(self,asset='BTC',balance=DEMO_BALANCE,state_file=None,portfolio_guard=None,risk_profile=None,interval=300,lane=None):
        if asset not in {'BTC','ETH','SOL','XRP'}:raise ValueError('unsupported crypto asset')
        if int(interval) not in (300,900):raise ValueError('supported rounds: 5m and 15m')
        self.interval=int(interval);self.tag={300:'5m',900:'15m'}[self.interval];self.lane=lane or asset
        self.asset=asset;self.symbol=asset.lower()+'/usd';self.balance=float(balance)
        if self.balance<=0:raise ValueError('positive paper allocation required')
        self.state_file=Path(state_file) if state_file is not None else STATE_FILE
        self.session_loss_limit=min(SESSION_LOSS_LIMIT_USD,self.balance*.10) if balance!=DEMO_BALANCE else SESSION_LOSS_LIMIT_USD
        # Separate profiles; wallet averages are not executable expected returns.
        self.pair_target={'BTC':.980,'ETH':.970,'SOL':.970,'XRP':.970}[asset]
        profile=risk_profile or {}
        self.wallet_quotes=bool(profile.get('walletQuotes',False))
        self.residual_cap=float(profile.get('residualCap',INVENTORY_MAX_RESIDUAL_USD))
        self.first_price_cap=float(profile.get('firstPriceCap',INVENTORY_MAX_FIRST_PRICE))
        self.inventory_stop=float(profile.get('stopLoss',INVENTORY_STOP_LOSS_USD))
        self.session_loss_limit=float(profile.get('sessionLossLimit',self.session_loss_limit))
        self.diagnostics=DiagnosticLog(str(self.state_file)+".diagnostics.sqlite3",self.lane)
        self.last_diagnostic_state=0.0
        self.print_keys=deque(maxlen=2000)
        self.entry_checks=[]
        self.entry_audit='warming'
        self.lag_trading_enabled=False
        self.momentum_enabled=bool(profile.get('momentumEnabled',False))
        self.momentum_decision='warming momentum data'
        self.momentum_candidate=None
        self.edge=None;self.edge_decision='waiting for market data';self.model_decision='waiting for research model'
        self.loss_budget=None  # portfolio-provided callable: remaining portfolio loss budget
        self.momentum_sizing={'earned':False,'reason':'no record yet'}
        self.book_history=deque(maxlen=160)
        self.book_observed={'up':0.,'down':0.}
        self.book_event_lag={'up':None,'down':None}
        self.trade_event_lag=None
        self.risk_loop_lag=0.
        self.stale_book_messages=0;self._stale_book_run=0;self._last_stale_log=0.;self._clob_resync_needed=False
        self._book_levels={side:{'bids':{},'asks':{}} for side in ('up','down')}
        self._fetched_books={}
        self._last_book_refresh=0.
        self._last_saved_at=0.
        self._depth_publish_at={'up':0.,'down':0.}
        self.portfolio_guard=portfolio_guard
        self.market=None
        self.bbo={'up':{'bid':None,'ask':None,'bidSize':0.0,'askSize':0.0},
                  'down':{'bid':None,'ask':None,'bidSize':0.0,'askSize':0.0}}
        self.depth={'up':{'bids':[],'asks':[]},'down':{'bids':[],'asks':[]}}
        self.spot=None; self.spot_ts=0.0
        self.twap=None; self.twap_ts=0.0
        self.open_ref=None; self.open_ref_ts=0.0; self.open_trusted=False
        self.spot_sec={}
        self.twap_hist=deque(maxlen=300)
        self.visual_history=deque(maxlen=900)
        self.trade_prints=deque(maxlen=1000)
        self.trade_seq=0
        self.fallback_sigma=0.00008
        self.feed={'market':'warming','clob':'warming','rtds':'warming'}
        self.latest_model=None
        self.latest_candidate=None
        self.latest_reason='warming up'
        self.latest_mode='WARMING'
        self.latest_core=None
        self.latest_pair=None
        self.last_core_state=None
        self.alignment_side=None
        self.alignment_since=0.0
        self.reason_counts=defaultdict(int)
        self.last_reason_count_at=0.0
        self.paper=None
        self.pending=False
        self.market_version=0
        self._diagnostic('ENGINE_START',profile={'allocation':self.balance,'pairTarget':self.pair_target,'residualCap':self.residual_cap,'sessionLossLimit':self.session_loss_limit})
        self.tasks=[]
        self.started=False

    def close(self):
        if hasattr(self,'diagnostics') and self.diagnostics:
            self.diagnostics.close()

    def __del__(self):
        self.close()

    async def _get(self,url,params=None,headers=None):
        async with httpx.AsyncClient(timeout=12,follow_redirects=False) as c:
            r=await c.get(url,params=params,headers=headers)
        self._diagnostic('REST_RESPONSE',url=url,params=params,status=r.status_code,body=r.text)
        r.raise_for_status()
        return r.json()

    async def start(self):
        if self.started: return
        self.started=True
        await self._seed_sigma()
        if not self._restore_saved():
            self._new_session()
        self._diagnostic('ENGINE_START',profile={'allocation':self.balance,'pairTarget':self.pair_target,'residualCap':self.residual_cap,'sessionLossLimit':self.session_loss_limit})
        self.tasks=[
            asyncio.create_task(self.market_worker(),name='poly-market'),
            asyncio.create_task(self.clob_worker(),name='poly-clob'),
            asyncio.create_task(self.rtds_worker(),name='poly-rtds'),
            asyncio.create_task(self.engine_worker(),name='poly-paper'),
            asyncio.create_task(self.position_worker(),name='poly-position-risk'),
        ]

        for task in self.tasks:task.add_done_callback(self._worker_done)

    def _worker_done(self,task):
        if not task.cancelled() and task.exception() is not None:
            import traceback
            exc=task.exception()
            self._diagnostic('WORKER_CRASH',worker=task.get_name(),error=''.join(traceback.format_exception(exc)))

    def _restore_saved(self):
        try:
            if not self.state_file.exists(): return False
            data=json.loads(self.state_file.read_text(encoding='utf-8'))
            p=data.get('paper')
            now=time.time()
            if not isinstance(p,dict) or int(p.get('version') or 0)!=14 or data.get('asset')!=self.asset or data.get('interval',self.tag)!=self.tag or float(p.get('startingBalance') or 0)!=self.balance:
                return False
            # Strip calculated presentation fields before resuming mutable state.
            p=dict(p)
            p.pop('equity',None);p.pop('roiPct',None);p.pop('winRatePct',None)
            if float(p.get('endsAt') or 0)<=now and not p.get('position'):p['status']='complete'
            self.paper=p
            self.reason_counts=defaultdict(int,data.get('reasonCounts') or {})
            self.visual_history.clear()
            for row in (data.get('history') or [])[-900:]:
                if isinstance(row,dict): self.visual_history.append(row)
            m=data.get('market')
            if isinstance(m,dict) and (float(m.get('startTs') or 0)<=now<float(m.get('endTs') or 0) or (p.get('position') or {}).get('marketSlug')==m.get('slug') or ((p.get('inventory') or {}).get('marketSlug')==m.get('slug') and ((p.get('inventory') or {}).get('upLots') or (p.get('inventory') or {}).get('downLots')))):
                self.market=m
                b=data.get('bbo')
                if isinstance(b,dict): self.bbo=b
                self.open_ref=data.get('openRef')
                self.open_trusted=bool(data.get('openTrusted') and self.open_ref)
                self.latest_model=data.get('model')
                self.latest_candidate=data.get('candidate')
                self.latest_reason=data.get('decision') or 'restored'
                self.latest_mode=data.get('mode') or 'RESTORED'
                self.spot=data.get('spot')
                self.twap=data.get('twap')
                sa=data.get('spotAgeSec');ta=data.get('twapAgeSec')
                if self.spot is not None:self.spot_ts=float(data.get('serverTime') or now)-max(0,float(sa or 0))
                if self.twap is not None:self.twap_ts=float(data.get('serverTime') or now)-max(0,float(ta or 0))
            print(f"POLY_DEMO_RESTORED session={self.paper.get('sessionId')} market={self.market.get('slug') if self.market else None} openTrusted={self.open_trusted}",flush=True)
            return True
        except Exception as exc:
            print(f"POLY_DEMO_RESTORE_FAILED {type(exc).__name__}",flush=True)
            return False

    def _new_session(self):
        now=time.time()
        self.paper={
            'version':14,'revision':ENGINE_REVISION,'sessionId':'srv_'+hex(int(now*1_000_000))[2:],
            'startedAt':now,'endsAt':now+10800,'startingBalance':self.balance,'cash':self.balance,
            'realizedPnl':0.0,'fees':0.0,'trades':0,'wins':0,'losses':0,
            'blocked':0,'nofills':0,'peakEquity':self.balance,'maxDrawdown':0.0,
            'position':None,'events':[],'status':'running','lastOpportunityAt':0.0,
            'signals':0,'candidatesSeen':0,'executionAttempts':0,'postLatencyRejects':0,'tradedMarkets':[],'cooldownUntil':0.0,
            'pairMarkets':[],'pairAttempts':0,'matchedSets':0.0,'lockedPairPnl':0.0,'pairCostSum':0.0,'pairSharesSum':0.0,
            'maker':None,'makerQuotes':0,'makerFills':0,'makerMerges':0,'makerResidualSettles':0,'makerEnabled':MAKER_PAIRING_ENABLED,
            'inventory':None,'inventoryEnabled':INVENTORY_V12_ENABLED,'inventoryQuotes':0,'inventoryFills':0,'inventoryCancels':0,
            'inventoryTakerHedges':0,'inventoryMerges':0,'inventoryResidualSettles':0,'inventoryRecoveryExits':0,
            'consecutiveLosses':0,'riskLockReason':None
        }
        self.reason_counts.clear()
        self._last_saved_at=0.
        self._save()

    async def reset(self):
        self._new_session()
        return self.snapshot()

    async def _seed_sigma(self):
        try:
            data=await self._get(COINBASE_REST+f'/products/{self.asset}-USD/candles',
                                 params={'granularity':'60'},headers={'User-Agent':'POLY-EDGE/1.0'})
            rows=sorted(data,key=lambda x:x[0])[-120:]
            closes=[float(x[4]) for x in rows]
            rs=[math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]
            if rs:
                rms=math.sqrt(sum(r*r for r in rs)/len(rs))
                self.fallback_sigma=max(1e-6,rms/math.sqrt(60))
        except Exception as e:
            print('POLY_DEMO_SIGMA_FALLBACK',type(e).__name__,flush=True)

    async def fetch_current_market(self):
        now=int(time.time());iv=self.interval;bucket=(now//iv)*iv
        selected=None
        for ts in [bucket,bucket+iv,bucket-iv]:
            try: data=await self._get(POLY_GAMMA+'/markets',{'slug':f'{self.asset.lower()}-updown-{self.tag}-{ts}'})
            except Exception: continue
            if isinstance(data,list) and data:
                selected=(ts,data[0])
                if ts==bucket: break
        if not selected: raise RuntimeError('current market unavailable')
        ts,m=selected
        outs=_jsonish(m.get('outcomes')); tids=_jsonish(m.get('clobTokenIds'))
        mp={str(o).strip().lower():str(t) for o,t in zip(outs,tids)}
        up=mp.get('up') or mp.get('yes');down=mp.get('down') or mp.get('no')
        if not up or not down: raise RuntimeError('token mapping unavailable')
        ub,db=await asyncio.gather(
            self._get(POLY_CLOB+'/book',{'token_id':up}),
            self._get(POLY_CLOB+'/book',{'token_id':down})
        )
        self._fetched_books={str(up):ub,str(down):db}
        return {
            'slug':m.get('slug') or f'{self.asset.lower()}-updown-{self.tag}-{ts}','question':m.get('question'),
            'asset':self.asset,'lane':self.lane,'interval':self.tag,'startTs':ts,'endTs':ts+self.interval,'active':bool(m.get('active')),
            'closed':bool(m.get('closed')),'acceptingOrders':bool(m.get('acceptingOrders')),
            'minOrderSize':float(m.get('orderMinSize') or 0),
            'minTickSize':float(m.get('orderPriceMinTickSize') or .01),
            'volume':float(m.get('volumeNum') or m.get('volume') or 0),
            'liquidity':float(m.get('liquidityNum') or m.get('liquidity') or 0),
            'upToken':up,'downToken':down,'conditionId':m.get('conditionId'),
            'up':{'bid':_best(ub,'bids'),'ask':_best(ub,'asks')},
            'down':{'bid':_best(db,'bids'),'ask':_best(db,'asks')}
        }

    def _publish_book(self,side,best_bid=None,best_ask=None):
        book=self._book_levels[side];bids=book['bids'];asks=book['asks']
        bid=best_bid if best_bid is not None else (max(bids) if bids else None)
        ask=best_ask if best_ask is not None else (min(asks) if asks else None)
        self.bbo[side]={'bid':bid,'ask':ask,'bidSize':float(bids.get(bid,0)), 'askSize':float(asks.get(ask,0))}
        now=time.time()
        if now-self._depth_publish_at.get(side,0)>=.2:
            self._depth_publish_at[side]=now
            self.depth[side]={key:[{'price':px,'size':qty} for px,qty in sorted(levels.items(),reverse=key=='bids')[:3]] for key,levels in book.items()}

    def _ingest_book(self,side,book):
        self._book_levels[side]={key:{float(row['price']):float(row.get('size') or 0) for row in (book.get(key) or []) if float(row.get('size') or 0)>0} for key in ('bids','asks')}
        self.book_observed[side]=time.time();self.book_event_lag[side]=0.
        self._publish_book(side)

    def _apply_rest_book(self,m):
        for side in ('up','down'):
            book=self._fetched_books.get(str(m.get(side+'Token')))
            if book:self._ingest_book(side,book)
            else:
                self.bbo[side]={'bid':m[side]['bid']['price'],'ask':m[side]['ask']['price'],'bidSize':m[side]['bid']['size'],'askSize':m[side]['ask']['size']}
                self.book_observed[side]=time.time()

    async def market_worker(self):
        while True:
            try:
                now=time.time()
                if self.market and self.market.get('slug')==f'{self.asset.lower()}-updown-{self.tag}-{int(now)//self.interval*self.interval}' and float(self.market['endTs'])>now:
                    if min(self.book_observed.values())<now-3 and now-self._last_book_refresh>=3:
                        self._last_book_refresh=now
                        books=await asyncio.gather(*(self._get(POLY_CLOB+'/book',{'token_id':self.market[side+'Token']}) for side in ('up','down')))
                        for side,book in zip(('up','down'),books):self._ingest_book(side,book)
                    self.feed['market']='live'
                    await asyncio.sleep(.75);continue
                m=await self.fetch_current_market()
                old=self.market
                changed=not old or old.get('slug')!=m.get('slug')
                if changed:
                    if old and self.paper and self.paper.get('position') and self.paper['position'].get('marketSlug')==old.get('slug'):
                        await self._settle_position(old.get('slug'))
                    if old and self.paper and self.paper.get('maker') and self.paper['maker'].get('marketSlug')==old.get('slug'):
                        await self._settle_maker(old.get('slug'))
                    if old and self.paper and isinstance(self.paper.get('inventory'),dict) and self.paper['inventory'].get('marketSlug')==old.get('slug'):
                        await self._settle_inventory(old.get('slug'))
                    if self.paper and (self.paper.get('position') or self._inventory_has_residual()):
                        self.feed['market']='awaiting_settlement'
                        await asyncio.sleep(3)
                        continue
                    self.open_ref=None;self.open_ref_ts=0;self.open_trusted=False
                    self.alignment_side=None;self.alignment_since=0.0
                    self.market_version+=1
                    self.book_history.clear()
                    self.book_observed={'up':0.,'down':0.}
                    print('POLY_DEMO_MARKET',m['slug'],flush=True)
                self.market=m;self._apply_rest_book(m);self.feed['market']='live'
                self._recover_open_reference()
            except Exception as e:
                self._diagnostic('FEED_ERROR',feed='market',error=repr(e))
                self.feed['market']='error:'+type(e).__name__
            await asyncio.sleep(1.2)

    async def clob_worker(self):
        while True:
            if not self.market:
                await asyncio.sleep(.5);continue
            version=self.market_version
            up=self.market['upToken'];down=self.market['downToken']
            try:
                async with websockets.connect(CLOB_WS,ping_interval=None,close_timeout=2,max_size=2_000_000) as ws:
                    await ws.send(json.dumps({'type':'market','assets_ids':[up,down],'initial_dump':True,'custom_feature_enabled':True}))
                    self.feed['clob']='live';self._clob_resync_needed=False;self._stale_book_run=0
                    print('POLY_DEMO_CLOB_CONNECTED',self.market.get('slug'),flush=True)
                    async def pinger():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send('PING')
                    pt=asyncio.create_task(pinger())
                    try:
                        while version==self.market_version:
                            raw=await asyncio.wait_for(ws.recv(),timeout=15)
                            if raw=='PONG': continue
                            try: data=json.loads(raw)
                            except Exception: continue
                            msgs=data if isinstance(data,list) else [data]
                            for msg in msgs:self._handle_clob(msg)
                            if self._clob_resync_needed:raise RuntimeError('stale order-book backlog; reconnect for current snapshot')
                            await asyncio.sleep(0)
                    finally:
                        pt.cancel()
            except Exception as e:
                self._diagnostic('FEED_ERROR',feed='clob',error=repr(e))
                self.feed['clob']='error:'+type(e).__name__
                await asyncio.sleep(1)

    def _handle_clob(self,m):
        if not self.market or not isinstance(m,dict): return
        def side_for(t):
            if str(t)==str(self.market.get('upToken')): return 'up'
            if str(t)==str(self.market.get('downToken')): return 'down'
            return None
        et=m.get('event_type')
        if et in {'book','price_change','best_bid_ask'}:
            received=time.time()
            try:
                ts=float(m.get('timestamp') or received*1000);ts=ts/1000 if ts>1e12 else ts
                lag=received-ts
            except Exception:lag=0.
            if lag>5 or lag< -1:
                self.stale_book_messages+=1;self._stale_book_run+=1
                if received-self._last_stale_log>=1:
                    self._last_stale_log=received
                    self._diagnostic('STALE_BOOK_MESSAGE',lagSeconds=lag,eventType=et,totalRejected=self.stale_book_messages,sample=m)
                if self._stale_book_run>=50 or lag>15:self._clob_resync_needed=True
                return
            self._stale_book_run=0
            self._diagnostic('CLOB_MESSAGE',message=m)
            if et=='book':
                side=side_for(m.get('asset_id'))
                if side:self._ingest_book(side,m);self.book_event_lag[side]=lag
            elif et=='price_change':
                touched={}
                for pc in (m.get('price_changes') or m.get('changes') or []):
                    side=side_for(pc.get('asset_id'))
                    if not side:continue
                    key='bids' if pc.get('side')=='BUY' else 'asks'
                    if pc.get('price') is not None and pc.get('size') is not None:
                        price=float(pc['price']);qty=float(pc['size'])
                        if qty>0:self._book_levels[side][key][price]=qty
                        else:self._book_levels[side][key].pop(price,None)
                    touched[side]=(float(pc['best_bid']) if pc.get('best_bid') is not None else None,float(pc['best_ask']) if pc.get('best_ask') is not None else None)
                for side,(bid,ask) in touched.items():
                    self._publish_book(side,bid,ask);self.book_observed[side]=received;self.book_event_lag[side]=lag
            else:
                side=side_for(m.get('asset_id'))
                if side:
                    self._publish_book(side,float(m['best_bid']) if m.get('best_bid') is not None else None,float(m['best_ask']) if m.get('best_ask') is not None else None)
                    self.book_observed[side]=received;self.book_event_lag[side]=lag
        elif et=='last_trade_price':
            self._diagnostic('CLOB_MESSAGE',message=m)
            side=side_for(m.get('asset_id'))
            if side:
                try:
                    price=float(m.get('price'));size=float(m.get('size') or 0)
                    ts=float(m.get('timestamp') or time.time()*1000)
                    tsec=(ts/1000 if ts>1e12 else ts)
                    self.trade_event_lag=time.time()-tsec
                    if not all(math.isfinite(v) for v in (price,size,tsec)) or not (0<price<1 and size>0):
                        self._diagnostic('TRADE_INVALID',message=m);return
                    inv=(self.paper or {}).get('inventory')
                    if inv and inv.get('quote'):self._inventory_process_quote(inv,self.latest_model or {})
                    # Exact retransmissions cannot consume simulated queue twice.
                    key=(str(m.get('asset_id')),m.get('timestamp'),price,size,m.get('side'),m.get('transaction_hash'))
                    if m.get('timestamp') is not None and key in self.print_keys:
                        self._diagnostic('TRADE_DUPLICATE',message=m);return
                    self.print_keys.append(key)
                    self.trade_seq+=1
                    self.trade_prints.append({'seq':self.trade_seq,'t':tsec if m.get('timestamp') is not None else None,'receivedAt':time.time(),'side':side,'price':price,'size':size,'aggressor':m.get('side'),'marketSlug':self.market.get('slug'),'feedReady':bool(self._inventory_feed_ready())})
                    # Apply this print before a subsequently received book/risk update.
                    inv=(self.paper or {}).get('inventory')
                    if inv and inv.get('marketSlug')==self.market.get('slug'):
                        self._inventory_apply_prints(inv)
                except Exception as exc:
                    self._diagnostic('TRADE_PARSE_ERROR',error=repr(exc),message=m)

        if et in {'book','price_change','best_bid_ask'}:
            inv=(self.paper or {}).get('inventory')
            if inv and inv.get('quote'):self._inventory_process_quote(inv,self.latest_model or {})

    async def rtds_worker(self):
        while True:
            try:
                async with websockets.connect(RTDS_WS,ping_interval=None,close_timeout=2,max_size=2_000_000) as ws:
                    flt=json.dumps({'symbol':self.symbol},separators=(',',':'))
                    await ws.send(json.dumps({'action':'subscribe','subscriptions':[
                        {'topic':'crypto_prices_chainlink','type':'update','filters':flt},
                        {'topic':'crypto_prices_twap_sixty','type':'update','filters':flt}
                    ]}))
                    self.feed['rtds']='live'
                    print('POLY_DEMO_RTDS_CONNECTED',flush=True)
                    async def pinger():
                        while True:
                            await asyncio.sleep(5);await ws.send('PING')
                    pt=asyncio.create_task(pinger())
                    try:
                        while True:
                            raw=await asyncio.wait_for(ws.recv(),timeout=12)
                            if raw=='PONG':continue
                            try:data=json.loads(raw)
                            except Exception:continue
                            for msg in (data if isinstance(data,list) else [data]):self._handle_rtds(msg)
                    finally:pt.cancel()
            except Exception as e:
                self._diagnostic('FEED_ERROR',feed='rtds',error=repr(e))
                self.feed['rtds']='error:'+type(e).__name__
                print('POLY_DEMO_RTDS_ERROR',type(e).__name__,str(e)[:180],flush=True)
                await asyncio.sleep(1)

    def _recover_open_reference(self):
        if self.open_ref is not None or not self.market:return
        opens=[r for r in self.twap_hist if abs(r['t']-float(self.market['startTs']))<=8]
        if opens:
            opening=min(opens,key=lambda r:abs(r['t']-float(self.market['startTs'])))
            self.open_ref=opening['p'];self.open_ref_ts=opening['t'];self.open_trusted=True
            self._diagnostic('OPEN_REFERENCE_RECOVERED',sample=opening)

    def _handle_rtds(self,m):
        if not isinstance(m,dict) or m.get('message'):return
        topic=str(m.get('topic') or '')
        p=m.get('payload') or m.get('data') or {}
        if not isinstance(p,dict):return
        if p.get('symbol') and str(p['symbol']).lower()!=self.symbol:return
        try:value=float(p.get('value',p.get('full_accuracy_value',m.get('value'))))
        except Exception:return
        try:ts=float(p.get('timestamp',m.get('timestamp',time.time()*1000)))
        except Exception:ts=time.time()*1000
        tms=ts*1000 if ts<1e12 else ts
        if value<=0:return
        self._diagnostic('RTDS_MESSAGE',message=m)
        if topic in {'crypto_prices','crypto_prices_chainlink'}:
            if tms/1000<self.spot_ts or tms/1000>time.time()+1:
                self._diagnostic('REFERENCE_TIMESTAMP_REJECT',topic=topic,eventTime=tms/1000);return
            self.spot=value;self.spot_ts=tms/1000
            sec=int(tms/1000);self.spot_sec[sec]=value
            cutoff=int(time.time())-300
            for k in list(self.spot_sec):
                if k<cutoff:self.spot_sec.pop(k,None)
        elif topic=='crypto_prices_twap_sixty':
            if tms/1000<self.twap_ts or tms/1000>time.time()+1:
                self._diagnostic('REFERENCE_TIMESTAMP_REJECT',topic=topic,eventTime=tms/1000);return
            self.twap=value;self.twap_ts=tms/1000
            self.twap_hist.append({'t':tms/1000,'p':value})
            if self.market and self.open_ref is None:
                start=float(self.market['startTs'])
                delta=abs(tms/1000-start)
                if delta<=8:
                    self.open_ref=value;self.open_ref_ts=tms/1000;self.open_trusted=True
                    print('POLY_DEMO_OPEN_TRUSTED',self.market['slug'],value,flush=True)
                elif start+8<tms/1000<start+22:
                    self.open_ref=value;self.open_ref_ts=tms/1000;self.open_trusted=False

    def _series(self):
        return [{'t':k,'p':v} for k,v in sorted(self.spot_sec.items())]

    def _avg_spot(self,a,b):
        xs=[x['p'] for x in self._series() if a<=x['t']<=b]
        return sum(xs)/len(xs) if xs else None

    def _sigma(self):
        xs=self._series()[-100:]
        if len(xs)<8:return self.fallback_sigma
        rs=[math.log(b['p']/a['p']) for a,b in zip(xs,xs[1:]) if a['p']>0 and b['p']>0]
        if len(rs)<5:return self.fallback_sigma
        mean=sum(rs)/len(rs)
        return math.sqrt(sum((r-mean)**2 for r in rs)/len(rs)) or self.fallback_sigma

    def _momentum(self):
        xs=self._series()[-7:]
        if len(xs)<4:return 0.0
        dt=max(1,xs[-1]['t']-xs[0]['t'])
        x=math.log(xs[-1]['p']/xs[0]['p'])/dt
        return max(-.0002,min(.0002,x))

    def _model(self):
        if not all([self.market,self.spot,self.twap,self.open_ref]):return None
        now=time.time();start=float(self.market['startTs']);end=float(self.market['endTs'])
        remaining=max(0,end-now);elapsed=max(0,now-start)
        final_start=end-60
        known=self._avg_spot(final_start,now)
        known_secs=max(0,min(60,now-final_start))
        future_secs=max(0,min(60,remaining))
        drift=self._momentum()
        current60=self._avg_spot(now-60,now)
        basis=self.twap-current60 if current60 else 0.0
        sig=self._sigma()

        # Early/mid-window uncertainty must remain wide. The old model incorrectly
        # used a <=60s horizon everywhere and saturated at 99% far too early.
        if remaining<=60:
            future_mean=self.spot*math.exp(drift*future_secs*.18)
            projected=((known or self.spot)*known_secs+future_mean*future_secs)/max(1,known_secs+future_secs)+basis
            T=max(1,remaining)
            std=max(.000015,sig*(T**1.5)/(math.sqrt(3)*60))
        else:
            horizon=min(remaining,self.interval-60)
            projected=self.spot*math.exp(drift*min(horizon,90)*.10)+basis
            std=max(.00008,sig*math.sqrt(max(30,horizon))*1.15)

        margin=(projected-self.open_ref)/self.open_ref
        total=math.sqrt(std*std+.00006**2)
        z=max(-5,min(5,margin/total))
        raw_up=max(.01,min(.99,_cdf(z)))

        # Shrink early-window resolution probability toward 50%; confidence grows
        # naturally as the five-minute market approaches resolution.
        progress=max(0,min(1,elapsed/self.interval))
        reliability=.30+.70*(progress**.72)
        fair_up=max(.02,min(.98,.5+(raw_up-.5)*reliability))

        series=self._series()
        impulse5=0.0
        if len(series)>=2:
            recent=[x for x in series if x['t']>=now-5]
            if len(recent)>=2 and recent[0]['p']>0:
                impulse5=math.log(recent[-1]['p']/recent[0]['p'])*10_000
        lead_bps=(self.spot/self.twap-1)*10_000 if self.twap else 0.0
        lag_score=abs(lead_bps)+.65*abs(impulse5)

        return {'remaining':remaining,'elapsed':elapsed,'projected':projected,'sigma':sig,'margin':margin,'z':z,
                'rawFairUp':raw_up,'fairUp':fair_up,'fairDown':1-fair_up,'basis':basis,'drift':drift,
                'leadBps':lead_bps,'impulse5Bps':impulse5,'lagScore':lag_score,'reliability':reliability}

    def _candidate(self,m):
        # LAG mode must evaluate the side the fast oracle lead actually points to.
        # This prevents a value-model candidate from fighting the observed lag direction.
        side='UP' if float(m.get('leadBps') or 0)>=0 else 'DOWN'
        key='up' if side=='UP' else 'down'
        ask=self.bbo[key].get('ask');bid=self.bbo[key].get('bid')
        if ask is None:return None
        ask=float(ask);bid=float(bid) if bid is not None else max(.001,ask-.01)
        fair=m['fairUp'] if side=='UP' else m['fairDown']
        entry_fee=_fee_per_share(ask)
        est_exit=max(.03,min(.97,fair))
        exit_fee=_fee_per_share(est_exit)
        spread=max(0.0,ask-bid)
        raw=fair-ask
        # Conservative round trip: buy fee + estimated sell fee + observed spread
        # plus a 0.5c safety buffer.
        roundtrip=raw-entry_fee-exit_fee-spread-.005
        return {
            'side':side,'ask':ask,'bid':bid,'fair':fair,'fee':entry_fee,
            'exitFeeEst':exit_fee,'spread':spread,'raw':raw,'entryNet':raw-entry_fee-.005,
            'net':roundtrip,'roundTripNet':roundtrip,
            'size':float(self.bbo[key].get('askSize') or 0)
        }

    def _reason(self,m,best,confidence):
        now=time.time();p=self.paper or {}
        fresh=max(now-self.spot_ts if self.spot_ts else 1e9,now-self.twap_ts if self.twap_ts else 1e9)<FEED_FRESH_SEC
        min_sh=float(self.market.get('minOrderSize') or 0)
        liquid=best and (best['size']<=0 or best['size']>=max(.01,min_sh))
        price_ok=best and .03<=best['ask']<=.97

        if p.get('status')=='risk_locked':
            self.latest_mode='RISK_LOCK';return p.get('riskLockReason') or 'risk lock active'
        if float(p.get('realizedPnl') or 0)<=-self.session_loss_limit:
            p['status']='risk_locked';p['riskLockReason']='session loss limit reached'
            self.latest_mode='RISK_LOCK';return 'session loss limit reached'
        if now<float(p.get('cooldownUntil') or 0):
            self.latest_mode='COOLDOWN';return 'cooldown after loss'
        if not self.open_trusted:
            self.latest_mode='WAIT_OPEN';return 'opening reference not trusted'
        if not fresh:
            self.latest_mode='WAIT_FEED';return 'reference feed stale'
        if not best:
            self.latest_mode='WAIT_BOOK';return 'order book unavailable'
        if not liquid:
            self.latest_mode='WAIT_LIQUIDITY';return 'insufficient best-ask liquidity'
        if not price_ok:
            self.latest_mode='WAIT_PRICE';return 'price outside safety band'
        if m['elapsed']<OPEN_STABILIZE_SEC:
            self.latest_mode='OPEN_STABILIZE';return 'opening stabilization period'
        if m['remaining']<4:
            self.latest_mode='CLOSE_LOCK';return 'too close to resolution'

        if not self.lag_trading_enabled:
            self.latest_mode='INVENTORY_PAIRING'
            inv=p.get('inventory') or {}
            if inv.get('quote'):return 'inventory quote awaiting conservative queue fill; directional entries disabled'
            if self._inventory_has_residual():return 'managing bounded residual; directional entries disabled'
            return self.entry_audit+'; directional entries disabled'

        slug=self.market.get('slug') if self.market else None
        if slug and slug in (p.get('tradedMarkets') or []):
            self.latest_mode='MARKET_LOCK';return 'already traded this 5-minute market'

        # Resolution model stays visible, but auto execution is disabled until it
        # proves itself in a longer shadow test. The uploaded run showed severe
        # whipsaw close to expiry.
        if m['remaining']<=RESOLUTION_WINDOW_SEC:
            self.latest_mode='RESOLUTION_WATCH'
            return 'resolution analysis only - auto trade disabled'

        self.latest_mode='LAG_SCALP'
        direction=1 if best['side']=='UP' else -1
        lead=float(m.get('leadBps') or 0)*direction
        impulse=float(m.get('impulse5Bps') or 0)*direction
        stable=lead>=LAG_LEAD_MIN_BPS and impulse>=LAG_IMPULSE_MIN_BPS

        if not stable:
            self.alignment_side=None;self.alignment_since=0.0
            return f'waiting for aligned {self.asset}/TWAP direction'

        if self.alignment_side!=best['side']:
            self.alignment_side=best['side'];self.alignment_since=now
            return 'confirming lag direction'
        if now-self.alignment_since<LAG_CONFIRM_SEC:
            return 'confirming lag direction'

        if m['lagScore']<.75:return 'lag strength below 0.75'
        if confidence<72:return 'lag confidence below 72%'
        if best['fair']<.62:return 'lag fair probability below 62%'
        if best['roundTripNet']<LAG_ROUNDTRIP_EDGE_MIN:return 'round-trip edge below 4c'
        return 'OPPORTUNITY_LAG'

    def _fast_core(self,m,best,confidence,reason):
        p=self.paper or {}
        now=time.time()
        fresh=max(now-self.spot_ts if self.spot_ts else 1e9,now-self.twap_ts if self.twap_ts else 1e9)<FEED_FRESH_SEC
        market_ok=bool(self.market and self.market.get('acceptingOrders'))
        feed_live=all(self.feed.get(k)=='live' for k in ('market','clob','rtds'))
        liquidity=float(self.market.get('liquidity') or 0) if self.market else 0.0

        lead=float(m.get('leadBps') or 0) if m else 0.0
        impulse=float(m.get('impulse5Bps') or 0) if m else 0.0
        side=(best.get('side') if best else ('UP' if lead>=0 else 'DOWN'))
        direction=1 if side=='UP' else -1
        aligned=(lead*direction>=LAG_LEAD_MIN_BPS and impulse*direction>=LAG_IMPULSE_MIN_BPS)

        vega={
            'name':'VEGA','job':'Lag signal',
            'verdict':'CONFIRM' if aligned else 'WAIT',
            'side':side,
            'summary':f"{side} · lead {lead:+.2f} bps · 5s impulse {impulse:+.2f} bps · confirm {LAG_CONFIRM_SEC:.1f}s",
            'confidence':confidence,
            'roundTripEdge':float(best.get('roundTripNet') or 0) if best else None
        }

        hard_fail=[]
        if p.get('status')=='risk_locked': hard_fail.append(p.get('riskLockReason') or 'risk lock active')
        if not feed_live or not fresh: hard_fail.append('live feed stale/incomplete')
        if not market_ok: hard_fail.append('market not accepting orders')
        if liquidity<=500: hard_fail.append('market liquidity below floor')
        if not best: hard_fail.append('no executable candidate')

        if hard_fail:
            verdict='REJECT';summary='; '.join(dict.fromkeys(hard_fail))
        elif reason=='OPPORTUNITY_LAG':
            verdict='APPROVE';summary='signal and hard risk checks pass'
        else:
            verdict='WAIT';summary=reason

        titan={
            'name':'TITAN','job':'Risk veto','verdict':verdict,'summary':summary,'veto':True,
            'checks':{
                'feedFresh':fresh,'feedLive':feed_live,'marketOpen':market_ok,'liquidityOk':liquidity>500,
                'maxTradeNotional':MAX_TRADE_NOTIONAL,'maxTradeLossUsd':MAX_TRADE_LOSS_USD,
                'sessionLossLimitUsd':self.session_loss_limit,
                'roundTripEdgeOk':bool(best and float(best.get('roundTripNet') or -99)>=LAG_ROUNDTRIP_EDGE_MIN)
            }
        }
        return {'VEGA':vega,'TITAN':titan,'final':verdict}


    def _inventory_new_market(self):
        if not self.paper or not self.market:return None
        inv={
            'marketSlug':self.market.get('slug'),'createdAt':time.time(),'cycles':0,
            'upLots':[],'downLots':[],'quote':None,'lastTradeSeq':self.trade_seq,
            'status':'IDLE','lastAction':'waiting for cheap first leg'
        }
        self.paper['inventory']=inv
        return inv

    def _inventory_get(self):
        if not self.paper or not self.market:return None
        inv=self.paper.get('inventory')
        if isinstance(inv,dict) and inv.get('marketSlug')!=self.market.get('slug') and self._inventory_totals(inv).get('residualSide'):
            self.paper['status']='risk_locked';self.paper['riskLockReason']='unsettled previous-market inventory requires reconciliation'
            return None
        if not isinstance(inv,dict) or inv.get('marketSlug')!=self.market.get('slug'):
            inv=self._inventory_new_market()
        return inv

    def _inventory_totals(self,inv):
        out={}
        for side in ('up','down'):
            lots=inv.get(side+'Lots') or []
            sh=sum(float(x.get('shares') or 0) for x in lots)
            cost=sum(float(x.get('cost') or 0) for x in lots)
            out[side]={'shares':sh,'cost':cost,'unitCost':(cost/sh if sh>0 else None)}
        matched=min(out['up']['shares'],out['down']['shares'])
        if out['up']['shares']>out['down']['shares']+1e-9:
            residual='up'
        elif out['down']['shares']>out['up']['shares']+1e-9:
            residual='down'
        else:
            residual=None
        return {'up':out['up'],'down':out['down'],'matched':matched,'residualSide':residual}

    def _inventory_has_residual(self):
        p=self.paper
        if not p:return False
        inv=p.get('inventory')
        if not isinstance(inv,dict):return False
        return bool(self._inventory_totals(inv).get('residualSide'))

    def _inventory_cancel_quote(self,inv,reason):
        q=inv.get('quote') if inv else None
        if not q:return
        self.paper['inventoryCancels']=int(self.paper.get('inventoryCancels') or 0)+1
        self._record('INV_QUOTE_CANCEL',side=str(q.get('side') or '').upper(),price=q.get('price'),detail=reason)
        self._diagnostic('QUOTE_CANCEL',quote=dict(q),reason=reason,context=self._diagnostic_context())
        inv['quote']=None;inv['status']='IDLE';inv['lastAction']=reason

    def _inventory_add_lot(self,inv,side,shares,price,fee,role_name):
        cost=float(shares)*float(price)+float(fee)
        inv[side+'Lots'].append({'shares':float(shares),'price':float(price),'fee':float(fee),'cost':cost,'t':time.time(),'role':role_name})
        self.paper['cash']-=cost
        if fee>0:self.paper['fees']+=fee
        self.paper['inventoryFills']=int(self.paper.get('inventoryFills') or 0)+1
        inv['status']='RESIDUAL';inv['lastAction']=f'{role_name} {side.upper()} fill'
        self._record('INV_FILL',side=side.upper(),price=price,detail=f'{shares:.2f}sh {role_name}; cost USD {cost:.4f}')

    def _inventory_consume(self,lots,qty):
        remain=float(qty);cost=0.0
        while remain>1e-9 and lots:
            lot=lots[0];take=min(remain,float(lot.get('shares') or 0))
            unit=float(lot.get('cost') or 0)/max(1e-12,float(lot.get('shares') or 0))
            cost+=take*unit
            lot['shares']=float(lot.get('shares') or 0)-take
            lot['cost']=float(lot.get('cost') or 0)-take*unit
            remain-=take
            if lot['shares']<=1e-9:lots.pop(0)
        return cost

    def _inventory_merge_available(self,inv):
        totals=self._inventory_totals(inv);qty=float(totals['matched'] or 0)
        if qty<=1e-9:return False
        self._inventory_cancel_quote(inv,'complete set closes outstanding quote')
        up_cost=self._inventory_consume(inv['upLots'],qty)
        dn_cost=self._inventory_consume(inv['downLots'],qty)
        cost=up_cost+dn_cost;payout=qty;pnl=payout-cost;pair_cost=cost/qty
        self.paper['cash']+=payout;self.paper['realizedPnl']+=pnl;self.paper['trades']+=1
        if pnl>=0:self.paper['wins']+=1;self.paper['consecutiveLosses']=0
        else:self.paper['losses']+=1;self.paper['consecutiveLosses']=int(self.paper.get('consecutiveLosses') or 0)+1
        self.paper['inventoryMerges']=int(self.paper.get('inventoryMerges') or 0)+1
        inv['cycles']=int(inv.get('cycles') or 0)+1;inv['status']='MERGED'
        self._pair_stats_update(qty,pair_cost,pnl)
        self._record('INV_MERGE',pnl=pnl,detail=f'{qty:.2f} complete sets; all-in {pair_cost:.4f}; locked edge {(1-pair_cost)*100:.2f}c/share')
        inv['lastAction']=f'merged {qty:.2f} pairs at {pair_cost:.4f}'
        return True

    def _inventory_feed_ready(self):
        now=time.time()
        return (all(self.feed.get(k)=='live' for k in ('market','clob','rtds'))
                and max(now-self.spot_ts,now-self.twap_ts)<FEED_FRESH_SEC
                and self.market and self.market.get('acceptingOrders',False))

    def _update_risk(self):
        p=self.paper
        if not p:return
        if self.portfolio_guard and not self.portfolio_guard():
            p['status']='risk_locked';p['riskLockReason']='portfolio loss limit reached'
        eq=self._equity();peak=max(float(p.get('peakEquity') or p['startingBalance']),eq)
        p['peakEquity']=peak
        p['maxDrawdownUsd']=max(float(p.get('maxDrawdownUsd') or 0),peak-eq)
        p['maxDrawdown']=max(float(p.get('maxDrawdown') or 0),(peak-eq)/peak if peak>0 else 0)
        if min(float(p.get('realizedPnl') or 0),eq-float(p['startingBalance']))<=-self.session_loss_limit:
            p['status']='risk_locked';p['riskLockReason']='session realized or marked loss limit reached'
        if int(p.get('consecutiveLosses') or 0)>=2:
            p['status']='risk_locked';p['riskLockReason']='two consecutive losses'

    def _inventory_first_leg_candidate(self,inv,m):
        self.entry_checks=[]
        def block(reason):
            self.entry_audit=reason;self.entry_checks.append({'scope':'entry','reason':reason});return None
        if not self._operational_entry_allowed():return block('storage recovery or engine handover')
        if not INVENTORY_V12_ENABLED or not self.open_trusted or not self.market:return block('strategy, opening reference or market unavailable')
        if self.paper.get('status')!='running' or self.paper.get('position') or self.pending:return block('entry status, exposure or pending execution gate')
        if self._inventory_has_residual():return block('existing residual exposure')
        self._update_risk()
        if self.paper.get('status')!='running' or not self._inventory_feed_ready():return block('risk locked or feed stale')
        if time.time()<float(self.paper.get('cooldownUntil') or 0) or time.time()>=self.paper['endsAt']:return block('cooldown or paper session ended')
        if int(inv.get('cycles') or 0)>=INVENTORY_MAX_CYCLES_PER_MARKET:return block('market cycle limit')
        now=time.time();elapsed=now-float(self.market.get('startTs') or now);remaining=float(self.market.get('endTs') or 0)-now
        if elapsed<INVENTORY_OPEN_AFTER_SEC or remaining<=INVENTORY_STOP_NEW_REMAINING_SEC:return block('outside entry time window')
        tick=max(.001,float(self.market.get('minTickSize') or .01));shares=max(5.0,float(self.market.get('minOrderSize') or 0))
        choices=[];self.entry_audit='no funded fee-aware quote within residual cap'
        for side in ('up','down'):
            row={'side':side,'eligible':False};self.entry_checks.append(row)
            opp='down' if side=='up' else 'up'
            bid=self.bbo[side].get('bid');ask=self.bbo[side].get('ask');oppask=self.bbo[opp].get('ask')
            if None in (bid,ask,oppask):row['reason']='missing book';continue
            bid=float(bid);ask=float(ask);oppask=float(oppask)
            if self.wallet_quotes:
                # Work a passive price that already funds the opposite taker leg and fees.
                ceiling=min(self.first_price_cap,self.residual_cap/shares,self.pair_target-oppask-_fee_per_share(oppask))
                maker_px=_floor_tick(min(bid,ask-tick,ceiling),tick)
                if bid-maker_px>6*tick+1e-9:row.update(reason='quote exceeds six-tick distance',price=maker_px,ceiling=ceiling);continue
            else:
                if bid>self.first_price_cap+1e-9:row['reason']='first price cap';continue
                maker_px=min(self.first_price_cap,_floor_tick(max(bid,min(ask-tick,self.first_price_cap)),tick))
            row.update(price=maker_px,shares=shares,projected=maker_px+oppask+_fee_per_share(oppask))
            if maker_px<.03 or maker_px>=ask-1e-9:row['reason']='price outside passive safety band';continue
            if shares*maker_px>min(self.residual_cap,float(self.paper.get('cash') or 0))+1e-9:row['reason']='first cost cap or cash';continue
            hedge_cost=shares*(oppask+_fee_per_share(oppask))
            if float(self.bbo[opp].get('askSize') or 0)<shares:row['reason']='hedge depth insufficient';continue
            if shares*maker_px+hedge_cost>float(self.paper['cash']):row['reason']='both legs not funded';continue
            loss_budget=self.session_loss_limit+min(float(self.paper['realizedPnl']),self._equity()-float(self.paper['startingBalance']))
            row['lossBudget']=loss_budget
            if shares*maker_px>loss_budget:row['reason']='remaining lane loss budget';continue
            projected=maker_px+oppask+_fee_per_share(oppask)
            if projected>self.pair_target+INVENTORY_MAX_INITIAL_GAP:row['reason']='fee-adjusted set exceeds target';continue
            direction=1 if side=='up' else -1
            lead=float(m.get('leadBps') or 0)*direction;imp=float(m.get('impulse5Bps') or 0)*direction
            if not self.wallet_quotes and (lead<INVENTORY_MIN_REVERSAL_LEAD_BPS or imp<INVENTORY_MIN_REVERSAL_IMPULSE_BPS):row['reason']='legacy directional gate';continue
            row.update(eligible=True,reason='funded passive candidate')
            choices.append((maker_px,projected,lead,imp,side,shares,bid,ask))
        if not choices:return None
        maker_px,projected,lead,imp,side,shares,bid,ask=min(choices,key=lambda x:(x[1],x[0]))
        self.entry_audit=f'funded passive {side} quote; projected fee-adjusted set {projected:.4f}'
        return {'side':side,'price':maker_px,'shares':shares,'projected':projected,'lead':lead,'impulse':imp,'bid':bid,'ask':ask}

    def _inventory_place_quote(self,inv,side,price,shares,kind):
        tick=max(.001,float(self.market.get('minTickSize') or .01));price=_floor_tick(price,tick)
        bid=self.bbo[side].get('bid');ask=self.bbo[side].get('ask')
        if bid is None or ask is None or price<=0 or price>=float(ask)-1e-9:return False
        bid=float(bid);ask=float(ask)
        at_best=abs(price-bid)<=tick/2
        improved=price>bid+tick/2
        queue_need=float(shares)+(float(self.bbo[side].get('bidSize') or 0) if at_best else (0.0 if improved else 1e12))
        inv['quote']={'id':uuid.uuid4().hex,'marketSlug':self.market.get('slug'),'side':side,'price':price,'shares':float(shares),'kind':kind,'createdAt':time.time(),
                      'queueNeed':queue_need,'seen':0.0,'lastTradeSeq':self.trade_seq}
        inv['status']='QUOTING';inv['lastAction']=f'{kind} {side.upper()} @ {price:.3f}'
        self.paper['inventoryQuotes']=int(self.paper.get('inventoryQuotes') or 0)+1
        self._record('INV_QUOTE',side=side.upper(),price=price,detail=f'{kind}; {shares:.2f}sh; '+(f'queue requirement {queue_need:.2f}sh' if queue_need<1e11 else 'depth ahead unknown; trade-through evidence required'))
        self._diagnostic('QUOTE_PLACE',quote=dict(inv['quote']),context=self._diagnostic_context())
        return True

    def _inventory_resting_reason(self,inv,m,q):
        """Validate the existing order at its own price, never rank fresh candidates."""
        now=time.time();side=q['side'];opp='down' if side=='up' else 'up'
        self._update_risk()
        if q.get('marketSlug',inv.get('marketSlug'))!=self.market.get('slug'):return 'quote market changed'
        if self.paper.get('status')!='running':return 'entry risk locked'
        if not self._inventory_feed_ready():return 'entry feed stale'
        if now<float(self.paper.get('cooldownUntil') or 0):return 'entry cooldown'
        if self.paper.get('position') or self.pending or self._inventory_has_residual():return 'entry exposure or execution pending'
        if not self.open_trusted:return 'opening reference untrusted'
        if now>=self.paper['endsAt']:return 'paper session ended'
        if now>=float(q['createdAt'])+INVENTORY_QUOTE_TTL_SEC:return 'first-leg quote TTL expired'
        if now>=float(self.market['endTs'])-INVENTORY_STOP_NEW_REMAINING_SEC:return 'new entry window closed'
        if int(inv.get('cycles') or 0)>=INVENTORY_MAX_CYCLES_PER_MARKET:return 'market cycle limit'
        ask=self.bbo[opp].get('ask');ownask=self.bbo[side].get('ask')
        if ask is None or ownask is None:return 'hedge book unavailable'
        if float(q['price'])>=float(ownask):return 'resting bid now crosses ask; no assumed maker fill'
        shares=float(q['shares']);cost=shares*float(q['price'])
        if cost>self.residual_cap+1e-9:return 'residual cost cap'
        if float(q['price'])>self.first_price_cap+1e-9:return 'first price cap'
        if float(q['price'])+float(ask)+_fee_per_share(float(ask))>self.pair_target+1e-9:return 'hedge all-in cost exceeds target'
        if float(self.bbo[opp].get('askSize') or 0)<shares:return 'hedge depth insufficient'
        if cost+shares*(float(ask)+_fee_per_share(float(ask)))>float(self.paper['cash'])+1e-9:return 'hedge cash insufficient'
        loss_budget=self.session_loss_limit+min(float(self.paper['realizedPnl']),self._equity()-float(self.paper['startingBalance']))
        if cost>loss_budget+1e-9:return 'remaining lane loss budget insufficient'
        if not self.wallet_quotes and float(m.get('leadBps') or 0)*(1 if side=='up' else -1)<-.05:return 'cheap-leg reversal signal failed'
        return None

    def _inventory_apply_prints(self,inv):
        q=inv.get('quote')
        if not q:return False
        if not self.market or q.get('marketSlug',inv.get('marketSlug'))!=self.market.get('slug'):return False
        side=q['side'];tick=max(.001,float(self.market.get('minTickSize') or .01))
        deadline=min(float(self.paper['endsAt']),float(self.market['endTs']))
        if q.get('kind')=='FIRST':deadline=min(deadline,float(q['createdAt'])+INVENTORY_QUOTE_TTL_SEC,float(self.market['endTs'])-INVENTORY_STOP_NEW_REMAINING_SEC)
        last=int(q.get('lastTradeSeq') or 0)
        for z in (z for z in self.trade_prints if int(z.get('seq') or 0)>last):
            q['lastTradeSeq']=int(z['seq']);reason=None
            event_time=z.get('t');received=z.get('receivedAt')
            if z.get('side')!=side:reason='opposite token'
            elif z.get('marketSlug')!=self.market.get('slug'):reason='wrong market'
            elif not z.get('feedReady',False):reason='feed stale at receipt'
            elif event_time is None or received is None:reason='missing event/receipt timestamp'
            elif not (float(q['createdAt'])<=float(event_time)<=float(received)+1 and float(received)<deadline and float(event_time)<deadline):reason='outside active quote lifetime or future timestamp'
            elif str(z.get('aggressor') or '').upper()!='SELL':reason='not a confirmed sell aggressor'
            elif float(z.get('size') or 0)<=0:reason='nonpositive volume'
            if reason:
                self._diagnostic('QUOTE_PRINT',quoteId=q.get('id'),trade=z,accepted=False,reason=reason);continue
            zp=float(z['price']);qp=float(q['price'])
            if zp<qp-tick/2:q['throughSeen']=float(q.get('throughSeen') or 0)+float(z['size'])
            elif abs(zp-qp)<=tick/2:q['seen']=float(q.get('seen') or 0)+float(z['size'])
            else:reason='print above bid'
            self._diagnostic('QUOTE_PRINT',quoteId=q.get('id'),trade=z,accepted=reason is None,reason=reason,seen=q.get('seen',0),throughSeen=q.get('throughSeen',0),queueNeed=q['queueNeed'])
            if float(q.get('seen') or 0)<float(q['queueNeed']) and float(q.get('throughSeen') or 0)<float(q['shares']):continue
            # Orphan complements must never manufacture exposure.
            if q.get('kind')=='COMPLEMENT':
                totals=self._inventory_totals(inv);res=totals['residualSide']
                if not res or res==side or abs(totals[res]['shares']-float(q['shares']))>1e-9:
                    self._inventory_cancel_quote(inv,'orphan complement cancelled');return False
            cost=float(q['shares'])*qp
            if cost>float(self.paper['cash'])+1e-9:
                self._inventory_cancel_quote(inv,'cash unavailable at simulated fill');return False
            self._diagnostic('QUOTE_FILL_EVIDENCE',quote=dict(q),triggerTrade=z,context=self._diagnostic_context())
            self._inventory_add_lot(inv,side,float(q['shares']),qp,0.,'MAKER')
            inv['quote']=None
            self._inventory_merge_available(inv)
            return True
        return False

    def _inventory_process_quote(self,inv,m):
        q=inv.get('quote')
        if not q:return
        if self._inventory_apply_prints(inv):return
        q=inv.get('quote')
        if not q:return
        if q.get('kind')=='COMPLEMENT':
            totals=self._inventory_totals(inv);res=totals['residualSide']
            if not res or res==q['side'] or abs(totals[res]['shares']-float(q['shares']))>1e-9:
                self._inventory_cancel_quote(inv,'orphan complement cancelled')
        else:
            reason=self._inventory_resting_reason(inv,m,q)
            if reason:self._inventory_cancel_quote(inv,reason)

    async def _inventory_taker_complement(self,inv,res_side,res_unit):
        def reject(reason):
            self._diagnostic('EXECUTION_REJECT',path='inventory',reason=reason,context=self._diagnostic_context());return False
        if self.pending:return reject('execution already pending')
        opp='down' if res_side=='up' else 'up';ask0=self.bbo[opp].get('ask');avail0=float(self.bbo[opp].get('askSize') or 0)
        totals=self._inventory_totals(inv);shares=float(totals[res_side]['shares'] or 0)
        if ask0 is None or shares<=0 or avail0<shares:return reject('initial hedge book or depth unavailable')
        ask0=float(ask0);allin=res_unit+ask0+_fee_per_share(ask0)
        if allin>self.pair_target+1e-9:return reject('initial hedge exceeds pair target')
        total0=shares*(ask0+_fee_per_share(ask0))
        if total0>float(self.paper.get('cash') or 0)+1e-9:return reject('initial hedge cash insufficient')
        self._inventory_cancel_quote(inv,'cancel maker complement before taker hedge')
        version=self.market_version;session=self.paper
        self.pending=True
        self._record('INV_HEDGE_ORDER',side=opp.upper(),price=ask0,detail=f'{shares:.2f}sh; projected pair {allin:.4f}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not session or self.market_version!=version or not self._inventory_feed_ready():return reject('session/market changed or feed stale after latency')
            ask=self.bbo[opp].get('ask');avail=float(self.bbo[opp].get('askSize') or 0)
            if ask is None or avail<shares:return reject('hedge depth unavailable after latency')
            ask=float(ask);fee=shares*_fee_per_share(ask);fresh=res_unit+ask+_fee_per_share(ask)
            if fresh>self.pair_target+1e-9 or shares*(ask+_fee_per_share(ask))>float(self.paper.get('cash') or 0)+1e-9:
                self._record('INV_HEDGE_NOFILL',side=opp.upper(),price=ask,detail=f'post-latency pair {fresh:.4f}');return False
            self.paper['inventoryTakerHedges']=int(self.paper.get('inventoryTakerHedges') or 0)+1
            self._inventory_add_lot(inv,opp,shares,ask,fee,'TAKER')
            self._inventory_merge_available(inv)
            return True
        finally:self.pending=False

    async def _inventory_recovery_exit(self,inv,res_side,force=False):
        def reject(reason):
            self._diagnostic('EXECUTION_REJECT',path='inventory',reason=reason,context=self._diagnostic_context());return False
        if self.pending:return reject('execution already pending')
        totals=self._inventory_totals(inv);shares=float(totals[res_side]['shares'] or 0);cost=float(totals[res_side]['cost'] or 0)
        bid=self.bbo[res_side].get('bid');avail=float(self.bbo[res_side].get('bidSize') or 0)
        if bid is None or shares<=0 or avail<shares:return reject('exit book or depth unavailable')
        bid=float(bid);fee=shares*_fee_per_share(bid);net=shares*bid-fee;pnl=net-cost
        if not force and pnl<INVENTORY_RECOVERY_PROFIT_USD:return reject('non-forced exit below profit threshold')
        self._inventory_cancel_quote(inv,'cancel complement before residual exit')
        version=self.market_version;session=self.paper
        self.pending=True
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not session or self.market_version!=version or not self._inventory_feed_ready():return reject('session/market changed or feed stale after latency')
            bid2=self.bbo[res_side].get('bid');avail2=float(self.bbo[res_side].get('bidSize') or 0)
            if bid2 is None or avail2<shares:return reject('exit depth unavailable after latency')
            bid2=float(bid2);fee2=shares*_fee_per_share(bid2);net2=shares*bid2-fee2;pnl2=net2-cost
            if not force and pnl2<0:return reject('exit price deteriorated after latency')
            self._inventory_consume(inv[res_side+'Lots'],shares)
            self.paper['cash']+=net2;self.paper['fees']+=fee2;self.paper['realizedPnl']+=pnl2;self.paper['trades']+=1
            if pnl2>=0:
                self.paper['wins']+=1;self.paper['consecutiveLosses']=0
            else:
                self.paper['losses']+=1;self.paper['consecutiveLosses']+=1
                self.paper['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
            self.paper['inventoryRecoveryExits']=int(self.paper.get('inventoryRecoveryExits') or 0)+1
            inv['status']='EXITED';inv['lastAction']='risk exit' if force else 'profitable recovery'
            self._update_risk()
            self._record('INV_RISK_EXIT' if force else 'INV_RECOVERY_EXIT',side=res_side.upper(),price=bid2,pnl=pnl2,detail=inv['lastAction'])
            return True
        finally:self.pending=False

    async def _inventory_step(self,m):
        if not INVENTORY_V12_ENABLED or not self.paper or not self.market:return
        inv=self._inventory_get()
        if inv is None:return
        self._update_risk()
        if self.momentum_enabled and not self._inventory_has_residual():
            self._inventory_cancel_quote(inv,'marketable momentum strategy selected');return
        self._inventory_process_quote(inv,m)
        if not self._inventory_feed_ready():
            self._inventory_cancel_quote(inv,'feed stale; fills paused');return
        self._inventory_merge_available(inv)
        totals=self._inventory_totals(inv);res=totals.get('residualSide')
        if res:
            if inv.get('quote') and inv['quote'].get('side')==res:
                self._inventory_cancel_quote(inv,'same-side quote forbidden while residual exists')
            unit=float(totals[res]['unitCost'] or 0)
            if await self._inventory_taker_complement(inv,res,unit):return
            age=time.time()-min(float(lot['t']) for lot in inv[res+'Lots'])
            bid=self.bbo[res].get('bid')
            pnl=(float(bid)-_fee_per_share(float(bid)))*totals[res]['shares']-totals[res]['cost'] if bid is not None else 0
            force=(age>=INVENTORY_MAX_HOLD_SEC or pnl<=-self.inventory_stop
                   or self.paper.get('status')!='running' or time.time()>=self.paper['endsAt']
                   or float(self.market['endTs'])-time.time()<=15)
            if await self._inventory_recovery_exit(inv,res,force=force):return
            if force:
                self._inventory_cancel_quote(inv,'risk exit pending executable liquidity')
                inv['status']='EXIT_PENDING';inv['lastAction']='risk exit waiting for liquidity';return
            opp='down' if res=='up' else 'up'
            max_opp=_floor_tick(self.pair_target-unit,float(self.market.get('minTickSize') or .01))
            if max_opp>=.03 and not inv.get('quote'):
                bid=self.bbo[opp].get('bid');ask=self.bbo[opp].get('ask')
                if bid is not None and ask is not None:
                    tick=max(.001,float(self.market.get('minTickSize') or .01))
                    px=min(max_opp,_floor_tick(float(ask)-tick,tick))
                    if px>=.03:self._inventory_place_quote(inv,opp,px,float(totals[res]['shares']),'COMPLEMENT')
            inv['status']='RESIDUAL';inv['lastAction']=f'waiting to pair {totals[res]["shares"]:.2f} {res.upper()} shares'
            return
        if inv.get('quote') and inv['quote'].get('kind')=='COMPLEMENT':
            self._inventory_cancel_quote(inv,'orphan complement cancelled')
        if inv.get('quote'):return
        cand=self._inventory_first_leg_candidate(inv,m)
        if cand:self._inventory_place_quote(inv,cand['side'],cand['price'],cand['shares'],'FIRST')

    async def _resolved_winner(self,slug):
        try:
            data=await self._get(POLY_GAMMA+'/markets',{'slug':slug})
            row=data[0] if isinstance(data,list) and data else None
            if not row or not row.get('closed') or row.get('umaResolutionStatus') not in ('resolved','finalized'):return None
            outs=_jsonish(row.get('outcomes'));prices=_jsonish(row.get('outcomePrices'))
            winners=[str(o).lower() for o,p in zip(outs,prices) if float(p)>=.999999]
            return winners[0] if len(winners)==1 and winners[0] in ('up','down') else None
        except Exception:return None

    async def _settle_inventory(self,slug):
        p=self.paper
        if not p:return
        inv=p.get('inventory')
        if not isinstance(inv,dict) or inv.get('marketSlug')!=slug:return
        if inv.get('quote'):self._inventory_cancel_quote(inv,'market rollover')
        self._inventory_merge_available(inv)
        totals=self._inventory_totals(inv);res=totals.get('residualSide')
        if not res:return
        winner=await self._resolved_winner(slug)
        if winner is None:
            inv['status']='AWAITING_SETTLEMENT';return
        shares=float(totals[res]['shares'] or 0);cost=float(totals[res]['cost'] or 0);payout=shares if res==winner else 0.0;pnl=payout-cost
        self._inventory_consume(inv[res+'Lots'],shares)
        p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1;p['inventoryResidualSettles']=int(p.get('inventoryResidualSettles') or 0)+1
        if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
        else:p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
        self._record('INV_RESIDUAL_SETTLE',side=res.upper(),price=1.0 if res==winner else 0.0,pnl=pnl,detail=f'bounded residual; winner {winner.upper()}; no forced loss sale')
        inv['status']='SETTLED';inv['lastAction']=f'residual settled {pnl:+.4f}'
        if pnl<0:p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
        self._update_risk()
        if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
            p['status']='risk_locked';p['riskLockReason']='V12 bounded residual settlement risk lock'

    def _maker_quote_candidate(self):
        if not MAKER_PAIRING_ENABLED:return None
        if not self.market or not self.paper or not self.open_trusted:return None
        now=time.time();remaining=float(self.market.get('endTs') or 0)-now
        if remaining<=20:return None
        ub=self.bbo['up'].get('bid');db=self.bbo['down'].get('bid')
        ua=self.bbo['up'].get('ask');da=self.bbo['down'].get('ask')
        if None in (ub,db,ua,da):return None
        ub=float(ub);db=float(db);ua=float(ua);da=float(da)
        shares=max(5.0,float(self.market.get('minOrderSize') or 0))
        if not (.03<=ub<=.97 and .03<=db<=.97):return None
        if ub>=ua or db>=da:return None
        if ub+db>MAKER_PAIR_MAX_SUM+1e-9:return None
        if shares*ub>MAKER_SINGLE_LEG_MAX+1e-9 or shares*db>MAKER_SINGLE_LEG_MAX+1e-9:return None
        if shares*(ub+db)>min(float(self.paper.get('cash') or 0),COMPLETE_SET_MAX_NOTIONAL)+1e-9:return None
        return {'shares':shares,'upQuote':ub,'downQuote':db,'sum':ub+db}

    def _maker_place(self,cand):
        p=self.paper
        if not p or not cand:return
        shares=float(cand['shares']);now=time.time()
        tick=max(.001,float(self.market.get('minTickSize') or .01))
        uq=_floor_tick(cand['upQuote'],tick);dq=_floor_tick(cand['downQuote'],tick)
        up_ahead=float(self.bbo['up'].get('bidSize') or 0)
        dn_ahead=float(self.bbo['down'].get('bidSize') or 0)
        p['maker']={
            'marketSlug':self.market.get('slug'),'createdAt':now,'shares':shares,
            'upQuote':uq,'downQuote':dq,
            'upQueueNeed':shares+max(0.0,up_ahead),
            'downQueueNeed':shares+max(0.0,dn_ahead),
            'upSeen':0.0,'downSeen':0.0,'upTradeThrough':False,'downTradeThrough':False,
            'upFilledPrice':None,'downFilledPrice':None,'firstFillAt':None,
            'upCost':0.0,'downCost':0.0,'lastTradeSeq':self.trade_seq,'status':'QUOTING'
        }
        p['makerQuotes']=int(p.get('makerQuotes') or 0)+1
        self._record('MAKER_QUOTE',detail=f"{shares:.2f}sh UP {uq:.3f} + DOWN {dq:.3f} = {uq+dq:.3f}; full displayed queue + real prints required")

    def _maker_cancel(self,reason):
        p=self.paper
        if not p or not p.get('maker'):return
        mk=p['maker']
        if mk.get('upFilledPrice') is None and mk.get('downFilledPrice') is None:
            self._record('MAKER_CANCEL',detail=reason)
            p['maker']=None

    def _maker_fill_side(self,mk,side):
        p=self.paper
        if not p:return False
        shares=float(mk['shares']);key='up' if side=='up' else 'down'
        quote=float(mk[key+'Quote'])
        cost=shares*quote
        if cost>float(p.get('cash') or 0)+1e-9:return False
        p['cash']-=cost;p['makerFills']=int(p.get('makerFills') or 0)+1
        mk[key+'FilledPrice']=quote;mk[key+'Cost']=cost;mk['status']='RESIDUAL'
        if not mk.get('firstFillAt'):mk['firstFillAt']=time.time()
        self._record('MAKER_FILL',side=side.upper(),price=quote,detail=f"{shares:.2f} shares maker fill after conservative queue/trade-through test; no maker fee")
        other='down' if side=='up' else 'up'
        if mk.get(other+'FilledPrice') is None:
            best=self.bbo[other].get('bid')
            if best is not None:
                tick=max(.001,float(self.market.get('minTickSize') or .01))
                max_other=_floor_tick(MAKER_PAIR_TARGET_SUM-quote,tick)
                bestq=_floor_tick(float(best),tick)
                newq=min(bestq,max_other)
                if newq>=.03 and newq<1 and shares*newq<=MAKER_SINGLE_LEG_MAX+1e-9:
                    mk[other+'Quote']=newq
                    # If we reprice below current best we do not know queue depth at
                    # that level; require an actual trade-through instead of inventing queue.
                    at_best=abs(bestq-newq)<=tick/2
                    ahead=float(self.bbo[other].get('bidSize') or 0) if at_best else 1e12
                    mk[other+'QueueNeed']=shares+max(0.0,ahead)
                    mk[other+'Seen']=0.0;mk[other+'TradeThrough']=False
                    mk['lastTradeSeq']=self.trade_seq
                    self._record('MAKER_REQUOTE',side=other.upper(),price=newq,detail=f"tick-valid opposite quote; complete-set target <= {MAKER_PAIR_TARGET_SUM:.3f}")
        return True

    async def _maker_taker_hedge(self,mk,filled_side):
        if self.pending or not self.paper:return False
        p=self.paper;other='down' if filled_side=='up' else 'up'
        if mk.get(other+'FilledPrice') is not None:return False
        ask0=self.bbo[other].get('ask');avail0=float(self.bbo[other].get('askSize') or 0)
        if ask0 is None:return False
        ask0=float(ask0);shares=float(mk['shares']);entry=float(mk[filled_side+'FilledPrice'])
        allin=entry+ask0+_fee_per_share(ask0)
        if allin>MAKER_TAKER_HEDGE_MAX_COST+1e-9 or avail0<shares:return False
        self.pending=True
        self._record('MAKER_HEDGE_ORDER',side=other.upper(),price=ask0,detail=f"marketable hedge {shares:.2f}sh; projected complete-set all-in {allin:.4f}")
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            ask=self.bbo[other].get('ask');avail=float(self.bbo[other].get('askSize') or 0)
            if ask is None:return False
            ask=float(ask);fee=_fee_per_share(ask);fresh_allin=entry+ask+fee
            total=shares*(ask+fee)
            if fresh_allin>MAKER_TAKER_HEDGE_MAX_COST+1e-9 or avail<shares or total>float(p.get('cash') or 0)+1e-9:
                self._record('MAKER_HEDGE_NOFILL',side=other.upper(),price=ask,detail=f"post-latency all-in {fresh_allin:.4f}; avail {avail:.2f}")
                return False
            p['cash']-=total;p['fees']+=shares*fee
            p['makerHedgeFills']=int(p.get('makerHedgeFills') or 0)+1
            mk[other+'FilledPrice']=ask;mk[other+'Cost']=total;mk['status']='HEDGED'
            self._record('MAKER_HEDGE_FILL',side=other.upper(),price=ask,detail=f"{shares:.2f}sh taker hedge; complete-set all-in {fresh_allin:.4f}")
            return True
        finally:
            self.pending=False

    async def _maker_residual_exit(self,mk,filled_side,reason):
        if self.pending or not self.paper:return False
        p=self.paper;bid0=self.bbo[filled_side].get('bid')
        if bid0 is None:return False
        shares=float(mk['shares']);self.pending=True
        self._record('MAKER_EXIT_ORDER',side=filled_side.upper(),price=float(bid0),detail=reason)
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            bid=self.bbo[filled_side].get('bid');avail=float(self.bbo[filled_side].get('bidSize') or 0)
            if bid is None or (avail>0 and avail<shares):
                self._record('MAKER_EXIT_NOFILL',side=filled_side.upper(),detail='post-latency residual exit unavailable')
                return False
            bid=float(bid);fee=shares*_fee_per_share(bid);net=shares*bid-fee
            cost=float(mk.get(filled_side+'Cost') or 0);pnl=net-cost
            p['cash']+=net;p['fees']+=fee;p['realizedPnl']+=pnl;p['trades']+=1
            if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
            else:p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
            p['makerResidualExits']=int(p.get('makerResidualExits') or 0)+1
            self._record('MAKER_RESIDUAL_EXIT',side=filled_side.upper(),price=bid,pnl=pnl,detail=reason+f'; exit fee USD {fee:.5f}')
            p['maker']=None
            if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
                p['status']='risk_locked';p['riskLockReason']='maker residual risk lock'
            return True
        finally:
            self.pending=False

    async def _manage_maker_residual(self,mk):
        up=mk.get('upFilledPrice');dn=mk.get('downFilledPrice')
        if (up is None)==(dn is None):return
        side='up' if up is not None else 'down'
        if await self._maker_taker_hedge(mk,side):
            return
        bid=self.bbo[side].get('bid')
        if bid is None:return
        shares=float(mk['shares']);bid=float(bid)
        net=shares*bid-shares*_fee_per_share(bid)
        cost=float(mk.get(side+'Cost') or 0);pnl=net-cost
        age=max(0.0,time.time()-float(mk.get('firstFillAt') or time.time()))
        remaining=float(self.market.get('endTs') or 0)-time.time()
        if pnl<=-MAX_TRADE_LOSS_USD:
            await self._maker_residual_exit(mk,side,'maker residual hard risk stop')
        elif age>=MAKER_RESIDUAL_MAX_SEC:
            await self._maker_residual_exit(mk,side,'maker residual time stop')
        elif remaining<=15:
            await self._maker_residual_exit(mk,side,'maker residual close protection')

    async def _maker_step(self):
        p=self.paper
        if not p or not self.market:return
        mk=p.get('maker')
        slug=self.market.get('slug')
        if mk and mk.get('marketSlug')!=slug:return
        if not mk:
            if p.get('position') or self.pending:return
            cand=self._maker_quote_candidate()
            if cand:self._maker_place(cand)
            return
        if time.time()-float(mk.get('createdAt') or 0)<MAKER_QUOTE_MIN_REST_SEC:return

        last=int(mk.get('lastTradeSeq') or 0)
        newprints=[q for q in self.trade_prints if int(q.get('seq') or 0)>last]
        if newprints:mk['lastTradeSeq']=max(int(q['seq']) for q in newprints)
        tick=max(.001,float(self.market.get('minTickSize') or .01))
        for q in newprints:
            side=q.get('side')
            if side not in ('up','down') or mk.get(side+'FilledPrice') is not None:continue
            quote=float(mk.get(side+'Quote') or 0);qp=float(q.get('price') or 99)
            if qp<quote-tick/2:
                # A public print strictly through our resting bid means all better
                # bids and our price level were crossed: count as filled.
                mk[side+'TradeThrough']=True
                mk[side+'Seen']=max(float(mk.get(side+'Seen') or 0),float(mk.get(side+'QueueNeed') or 0))
            elif abs(qp-quote)<=tick/2:
                mk[side+'Seen']=float(mk.get(side+'Seen') or 0)+max(0.0,float(q.get('size') or 0))

        for side in ('up','down'):
            if mk.get(side+'FilledPrice') is None and float(mk.get(side+'Seen') or 0)>=float(mk.get(side+'QueueNeed') or 1e18):
                self._maker_fill_side(mk,side)

        await self._manage_maker_residual(mk)
        if p.get('maker') is None:return
        mk=p['maker'];up=mk.get('upFilledPrice');dn=mk.get('downFilledPrice')
        if up is not None and dn is not None:
            shares=float(mk['shares']);cost=float(mk['upCost'])+float(mk['downCost'])
            pair_cost=cost/shares;payout=shares;pnl=payout-cost
            p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1;p['wins']+=1
            p['makerMerges']=int(p.get('makerMerges') or 0)+1
            self._pair_stats_update(shares,pair_cost,pnl)
            p.setdefault('pairMarkets',[])
            if slug not in p['pairMarkets']:p['pairMarkets'].append(slug)
            self._record('MAKER_MERGE',pnl=pnl,detail=f"{shares:.2f} maker complete sets merged; cost {pair_cost:.4f}; locked edge {(1-pair_cost)*100:.2f}c/share")
            p['maker']=None

    async def _settle_maker(self,slug):
        p=self.paper
        if not p:return
        mk=p.get('maker')
        if not mk or mk.get('marketSlug')!=slug:return
        up=mk.get('upFilledPrice');dn=mk.get('downFilledPrice')
        if up is None and dn is None:
            self._record('MAKER_CANCEL',detail='market rolled with no maker fill');p['maker']=None;return
        if up is not None and dn is not None:return
        if not self.open_ref or self.twap is None:
            self._record('MAKER_CANCEL',detail='residual could not settle: missing trusted reference');p['maker']=None;return
        side='UP' if up is not None else 'DOWN';shares=float(mk['shares'])
        cost=float(mk['upCost'] if up is not None else mk['downCost'])
        winner='UP' if float(self.twap)>=float(self.open_ref) else 'DOWN'
        payout=shares if side==winner else 0.0;pnl=payout-cost
        p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1
        p['makerResidualSettles']=int(p.get('makerResidualSettles') or 0)+1
        if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
        else:p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
        if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
            p['status']='risk_locked';p['riskLockReason']='maker residual loss risk lock'
        self._record('MAKER_RESIDUAL_SETTLE',side=side,price=1.0 if side==winner else 0.0,pnl=pnl,detail=f"winner {winner}; one maker leg remained unmatched")
        p['maker']=None

    def _pair_state(self):
        upa=self.bbo['up'].get('ask');dna=self.bbo['down'].get('ask')
        upb=self.bbo['up'].get('bid');dnb=self.bbo['down'].get('bid')
        if upa is None or dna is None:return {'ready':False}
        upa=float(upa);dna=float(dna)
        raw=upa+dna
        fees=_fee_per_share(upa)+_fee_per_share(dna)
        taker_cost=raw+fees
        edge=1.0-taker_cost
        maker_sum=(float(upb)+float(dnb)) if upb is not None and dnb is not None else None
        pos=(self.paper or {}).get('position')
        hedge=None
        if pos:
            opp='DOWN' if pos['side']=='UP' else 'UP'
            opp_key='down' if opp=='DOWN' else 'up'
            opp_ask=self.bbo[opp_key].get('ask')
            if opp_ask is not None and float(pos.get('shares') or 0)>0:
                opp_ask=float(opp_ask)
                entry_unit=float(pos['entryTotalCost'])/float(pos['shares'])
                hedge_cost=entry_unit+opp_ask+_fee_per_share(opp_ask)
                hedge={'oppositeSide':opp,'oppositeAsk':opp_ask,'pairCost':hedge_cost,'lockedEdgePerShare':1-hedge_cost,'eligible':hedge_cost<=COMPLETE_SET_MAX_COST}
        return {'ready':True,'upAsk':upa,'downAsk':dna,'rawPairCost':raw,'takerFeesPerPair':fees,'takerPairCost':taker_cost,
                'maker':(self.paper or {}).get('maker'),
                'lockedEdgePerShare':edge,'directEligible':taker_cost<=COMPLETE_SET_MAX_COST,'makerQuoteSum':maker_sum,
                'makerPotentialEdge':(1-maker_sum) if maker_sum is not None else None,
                'makerNote':('V12 inventory pairing active; legacy one-sided maker path suspended' if INVENTORY_V12_ENABLED else 'inventory disabled'),'makerEnabled':MAKER_PAIRING_ENABLED,'inventoryEnabled':INVENTORY_V12_ENABLED,'inventory':(self.paper or {}).get('inventory'),'hedge':hedge}

    def _pair_stats_update(self,shares,cost_per_share,pnl):
        p=self.paper
        p['matchedSets']=float(p.get('matchedSets') or 0)+shares
        p['lockedPairPnl']=float(p.get('lockedPairPnl') or 0)+pnl
        p['pairCostSum']=float(p.get('pairCostSum') or 0)+shares*cost_per_share
        p['pairSharesSum']=float(p.get('pairSharesSum') or 0)+shares

    async def _try_direct_pair(self,pair):
        if not self._operational_entry_allowed():return False
        if self.pending or not self.paper or not self.market or not pair.get('directEligible'):return False
        p=self.paper;slug=self.market.get('slug')
        self._update_risk()
        if p.get('status')!='running' or time.time()<float(p.get('cooldownUntil') or 0) or time.time()>=p['endsAt'] or not self._inventory_feed_ready():return False
        if slug in (p.get('pairMarkets') or []):return False
        shares=max(5.0,float(self.market.get('minOrderSize') or 0))
        if float(self.bbo['up'].get('askSize') or 0)<shares or float(self.bbo['down'].get('askSize') or 0)<shares:return False
        total=shares*float(pair['takerPairCost'])
        if total>min(float(p['cash']),COMPLETE_SET_MAX_NOTIONAL):return False
        version=self.market_version
        self.pending=True;p['pairAttempts']=int(p.get('pairAttempts') or 0)+1
        self._record('PAIR_ORDER',detail=f'direct complete-set paper attempt {shares:.2f} pairs; initial all-in {pair["takerPairCost"]:.4f}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            self._update_risk()
            if self.paper is not p or self.market_version!=version or p.get('status')!='running' or not self._inventory_feed_ready():return False
            fresh=self._pair_state()
            if not fresh.get('ready') or not fresh.get('directEligible'):
                p['nofills']+=1;self._record('PAIR_NOFILL',detail='pair edge disappeared during 350ms latency');return False
            if float(self.bbo['up'].get('askSize') or 0)<shares or float(self.bbo['down'].get('askSize') or 0)<shares:
                p['nofills']+=1;self._record('PAIR_NOFILL',detail='insufficient live size on one leg');return False
            up=float(fresh['upAsk']);dn=float(fresh['downAsk'])
            fees=shares*(_fee_per_share(up)+_fee_per_share(dn));spend=shares*(up+dn)+fees;cost=spend/shares
            if cost>COMPLETE_SET_MAX_COST or spend>min(float(p['cash']),COMPLETE_SET_MAX_NOTIONAL):
                p['nofills']+=1;self._record('PAIR_NOFILL',detail=f'post-latency pair cost {cost:.4f} failed cap');return False
            p['cash']-=spend;p['cash']+=shares
            pnl=shares-spend;p['realizedPnl']+=pnl;p['fees']+=fees;p['trades']+=1
            if pnl>=0:p['wins']+=1
            else:p['losses']+=1
            p.setdefault('pairMarkets',[]);p['pairMarkets'].append(slug);p['pairMarkets']=p['pairMarkets'][-24:]
            self._pair_stats_update(shares,cost,pnl)
            self._record('PAIR_MERGE',pnl=pnl,detail=f'{shares:.2f} complete sets merged; all-in {cost:.4f}; locked edge {(1-cost)*100:.2f}c/share')
            return True
        finally:self.pending=False

    async def _try_hedge_pair(self,pair):
        if self.pending or not self.paper or not self.paper.get('position'):return False
        hedge=pair.get('hedge') if pair else None
        if not hedge or not hedge.get('eligible'):return False
        p=self.paper;pos=p['position'];shares=float(pos['shares'])
        opp=hedge['oppositeSide'];key='down' if opp=='DOWN' else 'up'
        if float(self.bbo[key].get('askSize') or 0)<shares:return False
        opp0=float(hedge['oppositeAsk']);opp_total0=shares*(opp0+_fee_per_share(opp0))
        if opp_total0>float(p['cash']):return False
        self.pending=True;p['pairAttempts']=int(p.get('pairAttempts') or 0)+1
        self._record('HEDGE_ORDER',side=opp,price=opp0,detail=f'complete-set hedge attempt; projected all-in {hedge["pairCost"]:.4f}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            fresh=self._pair_state();h=fresh.get('hedge') if fresh else None
            ask=self.bbo[key].get('ask')
            if not h or not h.get('eligible') or ask is None or float(self.bbo[key].get('askSize') or 0)<shares:
                p['nofills']+=1;self._record('PAIR_NOFILL',side=opp,detail='hedge pair edge/size disappeared during latency');return False
            ask=float(ask);fee=shares*_fee_per_share(ask);opp_total=shares*ask+fee
            cost=(float(pos['entryTotalCost'])+opp_total)/shares
            if cost>COMPLETE_SET_MAX_COST or opp_total>float(p['cash']):
                p['nofills']+=1;self._record('PAIR_NOFILL',side=opp,price=ask,detail=f'hedge all-in {cost:.4f} failed cap');return False
            p['cash']-=opp_total;p['cash']+=shares
            pnl=shares-float(pos['entryTotalCost'])-opp_total
            p['realizedPnl']+=pnl;p['fees']+=fee;p['trades']+=1
            if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
            else:p['losses']+=1
            self._pair_stats_update(shares,cost,pnl)
            self._record('PAIR_HEDGE_MERGE',side=opp,price=ask,pnl=pnl,detail=f'residual hedged into {shares:.2f} complete sets; all-in {cost:.4f}; locked edge {(1-cost)*100:.2f}c/share')
            p['position']=None
            return True
        finally:self.pending=False

    def _equity(self):
        p=self.paper
        if not p:return 0.0
        e=float(p['cash'])
        pos=p.get('position')
        if pos:
            key='up' if pos['side']=='UP' else 'down'
            bid=self.bbo[key].get('bid')
            if bid is not None:
                bid=float(bid);shares=float(pos['shares'])
                e+=max(0.0,shares*bid-shares*_fee_per_share(bid))
            else:
                e+=float(pos.get('entryTotalCost') or 0)

        # A one-sided maker fill is real inventory, not vanished cash. Mark it at
        # executable bid less estimated taker exit fee so equity/drawdown are honest.
        mk=p.get('maker')
        if mk:
            shares=float(mk.get('shares') or 0)
            for side in ('up','down'):
                if mk.get(side+'FilledPrice') is None:continue
                bid=self.bbo[side].get('bid')
                if bid is not None:
                    bid=float(bid)
                    e+=max(0.0,shares*bid-shares*_fee_per_share(bid))
                else:
                    e+=float(mk.get(side+'Cost') or 0)
        inv=p.get('inventory')
        if isinstance(inv,dict):
            for side in ('up','down'):
                for lot in (inv.get(side+'Lots') or []):
                    shares=float(lot.get('shares') or 0);bid=self.bbo[side].get('bid')
                    if bid is not None:
                        bid=float(bid);e+=max(0.0,shares*bid-shares*_fee_per_share(bid))
                    else:e+=float(lot.get('cost') or 0)
        return e

    def _diagnostic_context(self):
        p=self.paper or {};inv=p.get('inventory') or {}
        return {'bbo':self.bbo,'feed':self.feed,'spot':self.spot,'spotTs':self.spot_ts,'twap':self.twap,'twapTs':self.twap_ts,
                'openRef':self.open_ref,'openTrusted':self.open_trusted,'market':self.market,'model':self.latest_model,
                'decision':self.latest_reason,'entryAudit':self.entry_audit,'entryChecks':self.entry_checks,
                'momentumEnabled':self.momentum_enabled,'momentumDecision':self.momentum_decision,'momentumSignal':self.momentum_candidate,
                'bookObservedAt':self.book_observed,'bookEventLagSec':self.book_event_lag,'staleBookMessagesRejected':self.stale_book_messages,'riskLoopLagSec':self.risk_loop_lag,'lastTradeEventLagSec':self.trade_event_lag,'cash':p.get('cash'),'status':p.get('status'),
                'pending':self.pending,'position':p.get('position'),'inventory':inv,'realizedPnl':p.get('realizedPnl'),'riskLockReason':p.get('riskLockReason'),
                'pairTarget':self.pair_target,'residualCap':self.residual_cap,'stopLoss':self.inventory_stop,'laneLossLimit':self.session_loss_limit,
                'equity':self._equity() if self.paper else self.balance,'fees':p.get('fees'),'trades':p.get('trades'),
                'sessionEndsAt':p.get('endsAt'),'tradeSeq':self.trade_seq,'executionLatencySec':EXECUTION_LATENCY_SEC,
                'quoteTtlSec':INVENTORY_QUOTE_TTL_SEC,'stopNewRemainingSec':INVENTORY_STOP_NEW_REMAINING_SEC,
                'residualHoldSec':INVENTORY_MAX_HOLD_SEC,'feeModel':'0.07*p*(1-p) taker; 0 simulated maker'}

    def _diagnostic(self,kind,**data):
        self.diagnostics.append(kind,revision=ENGINE_REVISION,sessionId=(self.paper or {}).get('sessionId'),marketSlug=(self.market or {}).get('slug'),**data)

    def _record(self,action,**kw):
        if not self.paper:return
        ev={'t':time.time(),'action':action,**kw}
        self._diagnostic('ENGINE_EVENT',event=ev,context=self._diagnostic_context())
        self.paper['events'].insert(0,ev);self.paper['events']=self.paper['events'][:500]
        print('POLY_DEMO_EVENT',action,kw.get('side',''),str(kw.get('detail',''))[:120],flush=True)
        self._last_saved_at=0.
        self._save()

    def _momentum_context(self,m):
        return {'t':time.time(),'model':m,'market':self.market,'bbo':self.bbo,'feed':self.feed,
                'openTrusted':self.open_trusted,'spotTs':self.spot_ts,'twapTs':self.twap_ts,
                'bookAges':{k:time.time()-v for k,v in self.book_observed.items()},
                'cash':(self.paper or {}).get('cash',0),'startingBalance':self.balance,'stopLossUsd':self.inventory_stop}

    def _momentum_signal(self,m,allow_weak=False):
        return momentum_signal(self._momentum_context(m),list(self.book_history),allow_weak)

    def _operational_entry_allowed(self):
        guard=getattr(self,'operational_guard',None)
        return guard is None or guard()

    def _attribute_close(self,pos,price,fee,reason,latency_ms=0):
        from poly_attribution import closed_trade
        trade=closed_trade(self.lane,(self.paper or {}).get('sessionId'),pos,price,fee,reason,latency_ms=latency_ms)
        observer=getattr(self,'trade_observer',None)
        if observer:observer(trade)
        self._diagnostic('TRADE_ATTRIBUTION',trade=trade)

    async def _momentum_enter(self,candidate):
        if not self._operational_entry_allowed():return False
        p=self.paper;version=self.market_version;market=self.market
        if not p or self.pending or p.get('position') or self._inventory_has_residual():return False
        self._update_risk()
        if p['status']!='running' or time.time()<p.get('cooldownUntil',0) or time.time()>=p['endsAt']:return False
        if market['slug'] in p.get('tradedMarkets',[]):return False
        inv=p.get('inventory')
        if inv and inv.get('quote'):self._inventory_cancel_quote(inv,'confirmed momentum entry takes priority')
        side=candidate['side'];key=side.lower();shares=candidate['shares'];tick=float(market.get('minTickSize') or .01)
        max_price=candidate['ask']+tick
        submitted=time.time()
        self.pending=True;p['executionAttempts']+=1;p['momentumOrders']=int(p.get('momentumOrders',0))+1
        self._record('ORDER',side=side,price=candidate['ask'],detail=f'MOMENTUM_SCALP marketable paper order; {shares:.2f} shares; max {max_price:.3f}')
        self._diagnostic('MOMENTUM_ORDER',signal=candidate,context=self._diagnostic_context())
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not p or self.market_version!=version or self.market.get('slug')!=market['slug']:return False
            if not self._operational_entry_allowed():return False
            self._update_risk()
            if time.time()-submitted>1.0:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail='entry execution deadline exceeded 1 second')
                return False
            if p['status']!='running' or time.time()>=p['endsAt']:return False
            fresh,reason=self._momentum_signal(self._model(),True)
            if not fresh or fresh['side']!=side or fresh['ask']>max_price+1e-9:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail='momentum post-latency rejection: '+(reason if not fresh else 'direction or one-tick price limit changed'))
                return False
            ask=fresh['ask'];fee=shares*_fee_per_share(ask);cost=shares*ask+fee
            if candidate.get('baseShares',shares)!=fresh['shares'] or cost>min(float(p['cash']),candidate.get('notionalCap',fresh['notionalCap']))+1e-9:return False
            if shares>fresh['shares'] and min(float(self.bbo[key].get('askSize') or 0),float(self.bbo[key].get('bidSize') or 0))<shares:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail='sized momentum entry: depth fell below order size');return False
            p['cash']-=cost;p['fees']+=fee;p['momentumFills']=int(p.get('momentumFills',0))+1
            p.setdefault('tradedMarkets',[]).append(market['slug']);p['tradedMarkets']=p['tradedMarkets'][-32:]
            p['position']={'marketSlug':market['slug'],'side':side,'entryPrice':ask,'shares':shares,'entryFee':fee,
                           'entryNotional':shares*ask,'entryTotalCost':cost,'entryAt':time.time(),'mode':'MOMENTUM_SCALP',
                           'requestedAsk':candidate['ask'],'entryLatencyMs':(time.time()-submitted)*1000,
                           'entryFeatures':{k:fresh[k] for k in ('leadBps','impulse5Bps','bookMove','modelEdgeEstimate','entryFrictionUsd') if k in fresh},
                           'modelFair':fresh['fair'],'entryNetEdge':fresh['modelEdgeEstimate'],
                           'entryFrictionUsd':fresh['entryFrictionUsd'],'adverseMoveBudgetUsd':fresh['adverseMoveBudgetUsd'],
                           'stopLossUsd':candidate.get('stopLossUsd',fresh['stopLossUsd']),'takeProfitUsd':candidate.get('takeProfitUsd',fresh['takeProfitUsd']),'maxHoldSec':fresh['maxHoldSec'],
                           'sizeMultiplier':candidate.get('sizeMultiplier',1.)}
            self._record('BUY',side=side,price=ask,detail=f'MOMENTUM_SCALP; {shares:.2f} shares; confirmed depth; fee {fee:.5f}; estimated edge {fresh["modelEdgeEstimate"]:.4f}')
            return True
        finally:self.pending=False

    async def _momentum_manage(self,m):
        p=self.paper;pos=p.get('position') if p else None
        if pos and pos.get('mode')=='EDGE_HOLD' and not self.pending:
            await self._edge_manage(pos);return
        if not pos or pos.get('mode') not in SCALP_MODES or self.pending:return
        if not self.market or pos['marketSlug']!=self.market['slug']:return
        side=pos['side'].lower();bid=self.bbo[side].get('bid');now=time.time();age=now-pos['entryAt']
        reason=pos.get('exitRequestedReason')
        if p.get('status')!='running' or now>=p['endsAt']:reason='momentum risk/session exit'
        elif age>=pos['maxHoldSec']:reason='momentum maximum hold'
        elif self.market['endTs']-now<=15:reason='momentum market closing'
        if bid is not None:
            net=pos['shares']*(bid-_fee_per_share(bid))-pos['entryTotalCost']
            if net<=-pos['stopLossUsd']:reason='momentum net loss stop'
            elif net>=pos['takeProfitUsd']:reason='momentum net profit target'
            elif m and max(now-self.spot_ts,now-self.twap_ts)<5 and age>=5 and m.get('leadBps',0)*(1 if side=='up' else -1)<-.2:reason='momentum oracle direction reversed'
        if reason:
            pos.setdefault('exitRequestedAt',now);pos.setdefault('exitRequestedReason',reason)
            reason=pos['exitRequestedReason']
            if now-self.book_observed.get(side,0)>3:
                self.momentum_decision='risk exit waiting for a current executable book';return
            await self._try_sell(reason)

    async def _momentum_step(self,m):
        if not self.momentum_enabled or not self.paper or not self.market:return
        now=time.time()
        if not self.book_history or now-self.book_history[-1]['t']>=.2:
            row={'t':now,'marketSlug':self.market['slug']}
            for side in ('up','down'):
                b=self.bbo[side];row[side+'Bid']=b['bid']
                row[side]=(b['bid']+b['ask'])/2 if b['bid'] is not None and b['ask'] is not None and now-self.book_observed.get(side,0)<=3 else None
            self.book_history.append(row)
        edge_candidate=self.edge.evaluate(self.lane,self,m) if self.edge else None
        await self._momentum_manage(m)
        p=self.paper;self._update_risk()
        if p.get('position'):self.momentum_decision='managing live paper position';return
        if not self._operational_entry_allowed():self.momentum_decision='entries paused: storage recovery or handover';return
        if self._inventory_has_residual():self.momentum_decision='managing older paired-inventory exposure';return
        if p['status']!='running':self.momentum_decision=p.get('riskLockReason') or p['status'];return
        if now<p.get('cooldownUntil',0):self.momentum_decision='cooldown after loss';return
        if now>=p['endsAt']:self.momentum_decision='paper session ended';return
        if self.market['slug'] in p.get('tradedMarkets',[]):self.momentum_decision='one momentum entry per asset per 5-minute round';return
        if await self._strategy_step(m,edge_candidate):return
        candidate,reason=self._momentum_signal(m);self.momentum_candidate=candidate;self.momentum_decision=reason
        if candidate:
            p['signals']+=1
            candidate=self._momentum_size(candidate);self.momentum_candidate=candidate
            await self._momentum_enter(candidate)

    # ---------------- promoted strategies (EdgeLab mispricing, research model) ----------------
    def _loss_budget(self):
        p=self.paper or {}
        lane=self.session_loss_limit+min(0.,float(p.get('realizedPnl') or 0))
        portfolio=self.loss_budget() if callable(self.loss_budget) else lane
        return max(0.,min(lane,portfolio))

    def _ask_levels(self,side):
        best=self.bbo[side].get('ask');levels=dict(self._book_levels[side]['asks'])
        if best is not None:
            levels={px:q for px,q in levels.items() if px>=float(best)-1e-9};levels.setdefault(float(best),float(self.bbo[side].get('askSize') or 0))
        return [{'price':px,'size':q} for px,q in sorted(levels.items())]

    def _bid_levels(self,side):
        best=self.bbo[side].get('bid');levels=dict(self._book_levels[side]['bids'])
        if best is not None:
            levels={px:q for px,q in levels.items() if px<=float(best)+1e-9};levels.setdefault(float(best),float(self.bbo[side].get('bidSize') or 0))
        return [{'price':px,'size':q} for px,q in sorted(levels.items(),reverse=True)]

    def _momentum_record(self):
        rows=[t['pnl'] for t in (getattr(self.edge,'trades',None) or []) if t.get('strategy')=='MOMENTUM_SCALP'][-30:]
        n=len(rows)
        if n<20:return {'earned':False,'trades':n,'reason':'needs 20 closed momentum trades'}
        mean=sum(rows)/n;se=(sum((x-mean)**2 for x in rows)/(n-1))**.5/n**.5
        return {'earned':mean-se>0,'trades':n,'meanPnl':round(mean,4),'meanMinusSE':round(mean-se,4)}

    def _momentum_size(self,c):
        """Earned confidence sizing: 1x-2x the minimum, linear in model edge (2x at 10c), only while the
        last 30 momentum trades have mean - 1 SE > 0. Capped by 15% of allocation, the remaining loss
        budget and visible two-sided depth; stop and target scale with size."""
        rec=self._momentum_record();self.momentum_sizing=rec;base=float(c['shares'])
        mult=max(1.,min(2.,float(c.get('modelEdgeEstimate') or 0)/.10)) if rec['earned'] else 1.
        cost=c['ask']+_fee_per_share(c['ask']);stop_ps=float(c['stopLossUsd'])/base
        cap=min(self.balance*.15,float((self.paper or {}).get('cash') or 0))
        budget=self._loss_budget()
        shares=math.floor(min(base*mult,cap/cost,budget/max(stop_ps,1e-9) if budget>0 else base,
                              float(self.bbo[c['side'].lower()].get('askSize') or 0),float(self.bbo[c['side'].lower()].get('bidSize') or 0))*100)/100
        shares=max(base,shares) if mult>1 else base
        if shares<=base+1e-9:return c
        scale=shares/base
        return {**c,'baseShares':base,'shares':shares,'sizeMultiplier':round(scale,3),'entryTotalCost':shares*cost,
                'stopLossUsd':round(c['stopLossUsd']*scale,4),'takeProfitUsd':round(c['takeProfitUsd']*scale,4),
                'notionalCap':max(float(c.get('notionalCap') or 0),shares*cost+1e-6)}

    def _edge_candidate(self,c):
        from poly_edge import size_stake
        variant=self.edge.tradable(c) if self.edge else None
        if not variant:return None,'mispricing strategy not promoted (shadow test running)'
        key=c['side'].lower();min_sh=max(5.,float(self.market.get('minOrderSize') or 0))
        equity=self._equity()
        shares=size_stake(kelly_full=c['kellyFull'],equity=equity,allocation=self.balance,loss_budget=self._loss_budget(),
                          unit_cost=c['cost'],min_shares=min_sh,max_loss_per_dollar=1.)
        tick=max(.001,float(self.market.get('minTickSize') or .01))
        avail=sum(q for px,q in self._book_levels[key]['asks'].items() if px<=c['ask']+tick+1e-9)
        shares=min(shares,math.floor(avail*100)/100)
        if shares<min_sh:return None,'Kelly stake or visible depth below exchange minimum'
        return {**c,'shares':shares,'strategy':variant,'stopLossUsd':None,'takeProfitUsd':None,'maxHoldSec':None},'OPPORTUNITY_EDGE'

    def _model_candidate(self):
        from poly_edge import size_stake
        rs=getattr(self,'research_state',None) or {};now=time.time();best=None
        for side,d in (rs.get('sides') or {}).items():
            pr=(d or {}).get('prediction') or {};c=pr.get('conservativeNetReturnPerShare')
            if not pr.get('evidenceGatePassed') or c is None or now-float(d.get('at') or 0)>1.0:continue
            if c>MODEL_MIN_CONSERVATIVE_RETURN and (best is None or c>best[1]):best=(side,c,pr)
        if not best:return None,'research model has not passed its evidence gate'
        side,mu,pr=best;b=self.bbo[side];ask=b.get('ask');bid=b.get('bid')
        if ask is None or bid is None:return None,'model side has no book'
        ask=float(ask);bid=float(bid);tick=max(.001,float(self.market.get('minTickSize') or .01))
        min_sh=max(5.,float(self.market.get('minOrderSize') or 0))
        if now-self.book_observed.get(side,0)>3:return None,'model side book older than 3s'
        if not .08<=ask<=.92 or ask-bid>.020001:return None,'model side price or spread outside band'
        cost=ask+_fee_per_share(ask);stop_ps=self.inventory_stop/min_sh
        friction=ask-bid+_fee_per_share(ask)+_fee_per_share(bid)
        if friction+max(tick,stop_ps*.25)>stop_ps+1e-9:return None,'fees and spread leave no room inside the loss stop'
        sigma=max(.02,float(pr.get('empiricalErrorHaircut') or .05)/1.2816)
        kelly=min(1.,mu*cost/(sigma*sigma))
        shares=size_stake(kelly_full=kelly,equity=self._equity(),allocation=self.balance,loss_budget=self._loss_budget(),
                          unit_cost=cost,min_shares=min_sh,max_loss_per_dollar=stop_ps/cost)
        bid_depth=sum(q for px,q in self._book_levels[side]['bids'].items() if px>=bid-tick-1e-9)
        shares=min(shares,math.floor(min(bid_depth,sum(q for px,q in self._book_levels[side]['asks'].items() if px<=ask+tick+1e-9))*100)/100)
        if shares<min_sh:return None,'Kelly stake or two-sided depth below exchange minimum'
        scale=shares/min_sh
        return {'side':side.upper(),'ask':ask,'bid':bid,'shares':shares,'cost':cost,'expectedNetReturnPerShare':pr.get('expectedNetReturnPerShare'),
                'conservativeNetReturnPerShare':mu,'kellyFull':kelly,'model':pr.get('model'),'modelVersion':pr.get('modelVersion'),
                'stopLossUsd':round(self.inventory_stop*scale,4),'takeProfitUsd':round(max(.18,self.inventory_stop)*scale,4),'maxHoldSec':30,
                'marketSlug':self.market['slug'],'strategy':'model'},'OPPORTUNITY_MODEL'

    async def _strategy_step(self,m,edge_candidate):
        """Promoted strategies trade before the baseline momentum rule. Returns True when it acted."""
        if edge_candidate:
            c,reason=self._edge_candidate(edge_candidate);self.edge_decision=reason
            if c:
                self.momentum_candidate=c;self.momentum_decision='EDGE: '+reason;p=self.paper;p['signals']+=1
                def again():
                    fresh=self.edge.evaluate(self.lane,self,self._model())
                    return fresh if fresh and fresh['side']==c['side'] and self.edge.tradable(fresh) else None
                await self._strategy_buy(c,'EDGE_HOLD',again);return True
        else:self.edge_decision=(self.edge.signals.get(self.lane) or {}).get('decision','no signal') if self.edge else 'edge lab not attached'
        c,reason=self._model_candidate();self.model_decision=reason
        if c:
            self.momentum_candidate=c;self.momentum_decision='MODEL: '+reason;self.paper['signals']+=1
            def again():
                fresh,_=self._model_candidate()
                return fresh if fresh and fresh['side']==c['side'] else None
            await self._strategy_buy(c,'MODEL_SCALP',again);return True
        return False

    async def _strategy_buy(self,candidate,mode,revalidate):
        if not self._operational_entry_allowed():return False
        p=self.paper;version=self.market_version;market=self.market
        if not p or self.pending or p.get('position') or self._inventory_has_residual():return False
        self._update_risk()
        if p['status']!='running' or time.time()<p.get('cooldownUntil',0) or time.time()>=p['endsAt']:return False
        if market['slug'] in p.get('tradedMarkets',[]):return False
        inv=p.get('inventory')
        if inv and inv.get('quote'):self._inventory_cancel_quote(inv,mode+' entry takes priority')
        side=candidate['side'];key=side.lower();shares=float(candidate['shares']);tick=float(market.get('minTickSize') or .01)
        max_price=candidate['ask']+tick;submitted=time.time()
        self.pending=True;p['executionAttempts']+=1
        counts=p.setdefault('strategyOrders',{});counts[mode]=int(counts.get(mode,0))+1
        self._record('ORDER',side=side,price=candidate['ask'],detail=f'{mode} marketable paper order; {shares:.2f} shares; max {max_price:.3f}')
        self._diagnostic('MOMENTUM_ORDER',signal={**{k:v for k,v in candidate.items() if not isinstance(v,(dict,list))},'strategyMode':mode},context=self._diagnostic_context())
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not p or self.market_version!=version or not self.market or self.market.get('slug')!=market['slug']:return False
            if not self._operational_entry_allowed():return False
            self._update_risk()
            if time.time()-submitted>1.0:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail='entry execution deadline exceeded 1 second');return False
            if p['status']!='running' or time.time()>=p['endsAt']:return False
            fresh=revalidate()
            if not fresh:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail=mode+' post-latency rejection: signal no longer valid');return False
            fill=_walk_book(self._ask_levels(key),shares,True,max_price)
            if not fill:
                p['nofills']+=1;p['postLatencyRejects']+=1
                self._record('NOFILL',side=side,detail=mode+' post-latency rejection: depth within one tick moved');return False
            fee=shares*fill['feePerShare'];cost=shares*fill['vwap']+fee
            if cost>float(p['cash'])+1e-9:return False
            p['cash']-=cost;p['fees']+=fee
            fills=p.setdefault('strategyFills',{});fills[mode]=int(fills.get(mode,0))+1
            p.setdefault('tradedMarkets',[]).append(market['slug']);p['tradedMarkets']=p['tradedMarkets'][-32:]
            p['position']={'marketSlug':market['slug'],'side':side,'entryPrice':fill['vwap'],'shares':shares,'entryFee':fee,
                'entryNotional':shares*fill['vwap'],'entryTotalCost':cost,'entryAt':time.time(),'mode':mode,'strategy':candidate.get('strategy'),
                'requestedAsk':candidate['ask'],'entryLatencyMs':(time.time()-submitted)*1000,
                'entryFeatures':{k:candidate[k] for k in ('p','pConservative','edge','conservativeEdge','smartFlow','cellRounds','kellyFull',
                                 'expectedNetReturnPerShare','conservativeNetReturnPerShare','modelVersion') if k in candidate},
                'modelFair':candidate.get('p'),'stopLossUsd':candidate.get('stopLossUsd'),'takeProfitUsd':candidate.get('takeProfitUsd'),
                'maxHoldSec':candidate.get('maxHoldSec')}
            self._record('BUY',side=side,price=fill['vwap'],detail=f'{mode} ({candidate.get("strategy")}); {shares:.2f} shares; VWAP {fill["vwap"]:.3f}; fee {fee:.5f}')
            return True
        finally:self.pending=False

    async def _edge_manage(self,pos):
        if not self.market or pos['marketSlug']!=self.market['slug']:return  # settles at verified resolution
        side=pos['side'];key=side.lower();bid=self.bbo[key].get('bid')
        if self.market['endTs']-time.time()<=4:return
        reason=self.edge.hold_exit(self.lane,side,bid) if self.edge else None
        if reason and time.time()-self.book_observed.get(key,0)<=3:await self._try_sell(reason)

    async def _sell_walk(self,reason):
        p=self.paper;pos=p['position'];version=self.market_version;key='up' if pos['side']=='UP' else 'down'
        bid0=self.bbo[key].get('bid')
        if bid0 is None:return False
        tick=max(.001,float(self.market.get('minTickSize') or .01));min_price=max(.01,float(bid0)-2*tick)
        if not _walk_book(self._bid_levels(key),pos['shares'],False,min_price):
            if time.time()-pos.get('exitWaitLoggedAt',0)>=3:
                pos['exitWaitLoggedAt']=time.time()
                self._diagnostic('EXIT_WAIT',reason='insufficient bid depth within two ticks',exitReason=reason,context=self._diagnostic_context())
            return False
        submitted=time.time();self.pending=True
        self._record('ORDER',side=pos['side'],price=float(bid0),detail=f'paper FAK sell (depth walk); {reason}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not p or self.market_version!=version or p.get('position') is not pos:return False
            if not self.market or self.market.get('slug')!=pos.get('marketSlug'):return False
            if time.time()-self.book_observed.get(key,0)>3:
                p['nofills']+=1;self._record('NOFILL',side=pos['side'],detail='exit rejected: book older than 3 seconds after latency');return False
            fill=_walk_book(self._bid_levels(key),pos['shares'],False,min_price)
            if not fill:
                p['nofills']+=1;self._record('NOFILL',side=pos['side'],detail='exit depth moved during latency');return False
            self._diagnostic('EXIT_ATTEMPT',filled=True,reason=reason,elapsedSec=time.time()-submitted,requestedAt=pos.get('exitRequestedAt'),context=self._diagnostic_context())
            fee=pos['shares']*fill['feePerShare'];proceeds=pos['shares']*fill['vwap']-fee;pnl=proceeds-pos['entryTotalCost']
            p['cash']+=proceeds;p['fees']+=fee;p['realizedPnl']+=pnl;p['trades']+=1
            if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
            else:
                p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
                p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
            if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
                p['status']='risk_locked';p['riskLockReason']='session risk lock after losses'
            p['position']=None
            self._record('SELL',side=pos['side'],price=fill['vwap'],pnl=pnl,detail=reason+f'; VWAP {fill["vwap"]:.3f}; fee USD {fee:.5f}')
            self._attribute_close(pos,fill['vwap'],fee,reason,(time.time()-submitted)*1000)
            if pos.get('mode')=='MOMENTUM_SCALP':p['momentumCloses']=int(p.get('momentumCloses',0))+1
            return True
        finally:self.pending=False

    async def _enter(self,best,m,confidence):
        if not self._operational_entry_allowed():return False
        if not self.lag_trading_enabled:return
        if self.pending or not self.paper or self.paper.get('position'):return
        p=self.paper;market=self.market
        if time.time()>=p['endsAt']:return
        min_sh=max(5.0,float(market.get('minOrderSize') or 0))
        ask0=float(best['ask']);key='up' if best['side']=='UP' else 'down'
        size0=float(self.bbo[key].get('askSize') or 0)
        risk_cash=min(float(p['cash']),MAX_TRADE_NOTIONAL)

        # Use the exchange minimum quantity instead of spending the whole risk
        # budget at the first quote. This leaves room for a fast marketable-limit
        # reprice while still respecting the same max-notional cap.
        shares=round(min_sh,2)
        if size0>0 and size0<shares:
            p['blocked']+=1
            self._record('BLOCK',side=best['side'],price=ask0,detail=f'best ask has only {size0:.2f} shares; need {shares:.2f}')
            return

        tick=max(.001,float(market.get('minTickSize') or .01))
        edge_budget=max(0.0,float(best.get('roundTripNet') or 0)-LAG_ROUNDTRIP_EDGE_MIN)
        chase=min(MAX_ENTRY_CHASE,max(tick,edge_budget*.35))
        max_price=min(.97,ask0+chase)

        # Reduce max price until the minimum order still fits the risk budget
        # including the taker fee.
        while max_price>ask0 and shares*(max_price+_fee_per_share(max_price))>risk_cash+1e-9:
            max_price=round(max(ask0,max_price-tick),6)
        initial_total=shares*(ask0+_fee_per_share(ask0))
        if initial_total>risk_cash+1e-9:
            p['blocked']+=1
            self._record('BLOCK',side=best['side'],price=ask0,detail=f'{shares:.2f}-share minimum costs USD {initial_total:.4f}; risk cap USD {risk_cash:.2f}')
            return

        self.pending=True
        p['executionAttempts']=int(p.get('executionAttempts') or 0)+1
        self._record(
            'ORDER',side=best['side'],price=ask0,
            detail=f'paper adaptive FAK {shares:.2f} shares; start {ask0:.3f}; max {max_price:.3f}; RT edge {best["roundTripNet"]*100:.2f}c'
        )
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            ask=self.bbo[key].get('ask');avail=float(self.bbo[key].get('askSize') or 0)
            if ask is None:
                p['nofills']+=1;p['postLatencyRejects']=int(p.get('postLatencyRejects') or 0)+1
                self._record('NOFILL',side=best['side'],detail='post-latency ask disappeared')
                return
            ask=float(ask)

            # Recompute the actual lag/model state at the moment the simulated
            # order reaches the book. Never fill merely because the old signal
            # was strong.
            fresh_m=self._model()
            fresh_best=self._candidate(fresh_m) if fresh_m else None
            direction=1 if best['side']=='UP' else -1
            fresh_lead=(float(fresh_m.get('leadBps') or 0)*direction) if fresh_m else -999
            fresh_impulse=(float(fresh_m.get('impulse5Bps') or 0)*direction) if fresh_m else -999
            fresh_edge=float(fresh_best.get('roundTripNet') or -999) if fresh_best and fresh_best.get('side')==best['side'] else -999
            total=shares*(ask+_fee_per_share(ask))

            reject=None
            if ask>max_price+1e-9:
                reject=f'ask moved {ask0:.3f}->{ask:.3f}, above adaptive max {max_price:.3f}'
            elif avail<shares:
                reject=f'only {avail:.2f} shares remain at live ask; need {shares:.2f}'
            elif total>risk_cash+1e-9:
                reject=f'live cost USD {total:.4f} exceeds risk cap USD {risk_cash:.2f}'
            elif not fresh_best or fresh_best.get('side')!=best['side']:
                reject='post-latency lag direction changed'
            elif fresh_lead<LAG_LEAD_MIN_BPS*.75:
                reject=f'post-latency lead weakened to {fresh_lead:.2f} bps'
            elif fresh_impulse<-0.05:
                reject=f'post-latency impulse reversed to {fresh_impulse:.2f} bps'
            elif fresh_edge<LAG_ROUNDTRIP_EDGE_MIN:
                reject=f'post-latency round-trip edge fell to {fresh_edge*100:.2f}c'

            if reject:
                p['nofills']+=1;p['postLatencyRejects']=int(p.get('postLatencyRejects') or 0)+1
                self._record('NOFILL',side=best['side'],price=ask,detail=reject)
                return

            fee=shares*_fee_per_share(ask);notional=shares*ask;total=notional+fee
            p['cash']-=total;p['fees']+=fee
            p.setdefault('tradedMarkets',[])
            if market['slug'] not in p['tradedMarkets']:
                p['tradedMarkets'].append(market['slug'])
                p['tradedMarkets']=p['tradedMarkets'][-24:]
            p['position']={
                'marketSlug':market['slug'],'side':best['side'],'entryPrice':ask,'shares':shares,
                'entryNotional':notional,'entryFee':fee,'entryTotalCost':total,'entryAt':time.time(),
                'openRef':self.open_ref,'modelFair':fresh_best['fair'],'confidence':confidence,
                'mode':self.latest_mode,'entryNetEdge':fresh_edge,'entryLeadBps':fresh_m.get('leadBps',0),
                'entryImpulse5Bps':fresh_m.get('impulse5Bps',0),'entryLagScore':fresh_m.get('lagScore',0),
                'initialAsk':ask0,'adaptiveMaxPrice':max_price
            }
            self._record(
                'BUY',side=best['side'],price=ask,
                detail=f'{self.latest_mode}; adaptive fill after {int(EXECUTION_LATENCY_SEC*1000)}ms; {shares:.2f} shares; '
                       f'ask {ask0:.3f}->{ask:.3f}; post-latency RT edge {fresh_edge*100:.2f}c; fee USD {fee:.5f}'
            )
        finally:self.pending=False

    async def _try_sell(self,reason):
        if self.pending or not self.paper or not self.paper.get('position'):return False
        p=self.paper;pos=p['position'];version=self.market_version;key='up' if pos['side']=='UP' else 'down'
        min_sh=float(self.market.get('minOrderSize') or 0) if self.market else 0
        if pos['shares']<min_sh:return False
        bid0=self.bbo[key].get('bid');size0=float(self.bbo[key].get('bidSize') or 0)
        if pos.get('mode') in SELF_MANAGED_MODES:
            return await self._sell_walk(reason)
        if bid0 is None or size0<pos['shares']:
            if time.time()-pos.get('exitWaitLoggedAt',0)>=3:
                pos['exitWaitLoggedAt']=time.time()
                self._diagnostic('EXIT_WAIT',reason='insufficient confirmed bid depth',exitReason=reason,context=self._diagnostic_context())
            return False
        bid0=float(bid0);tick=max(.001,float(self.market.get('minTickSize') or .01));min_price=max(.01,bid0-tick)
        submitted=time.time()
        self.pending=True;self._record('ORDER',side=pos['side'],price=bid0,detail=f'paper FAK sell; {reason}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not p or self.market_version!=version or p.get('position') is not pos:return False
            if not self.market or self.market.get('slug')!=pos.get('marketSlug'):return False
            if pos.get('mode') in SELF_MANAGED_MODES and time.time()-self.book_observed.get(key,0)>3:
                p['nofills']+=1
                self._record('NOFILL',side=pos['side'],detail='exit rejected: book older than 3 seconds after latency')
                self._diagnostic('EXIT_ATTEMPT',filled=False,reason='stale post-latency book',elapsedSec=time.time()-submitted,context=self._diagnostic_context())
                return False
            bid=self.bbo[key].get('bid');avail=float(self.bbo[key].get('bidSize') or 0)
            if bid is None or float(bid)<min_price or avail<pos['shares']:
                p['nofills']+=1;self._record('NOFILL',side=pos['side'],price=float(bid) if bid is not None else None,detail=f'exit book moved or insufficient depth during {time.time()-submitted:.3f}s latency');return False
            self._diagnostic('EXIT_ATTEMPT',filled=True,reason=reason,elapsedSec=time.time()-submitted,requestedAt=pos.get('exitRequestedAt'),context=self._diagnostic_context())
            bid=float(bid);fee=pos['shares']*_fee_per_share(bid);proceeds=pos['shares']*bid-fee
            pnl=proceeds-pos['entryTotalCost']
            p['cash']+=proceeds;p['fees']+=fee;p['realizedPnl']+=pnl;p['trades']+=1
            if pnl>=0:
                p['wins']+=1;p['consecutiveLosses']=0
            else:
                p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
                p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
            if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
                p['status']='risk_locked'
                p['riskLockReason']='session risk lock after losses'
            p['position']=None
            self._record('SELL',side=pos['side'],price=bid,pnl=pnl,detail=reason+f'; fee USD {fee:.5f}')
            self._attribute_close(pos,bid,fee,reason,(time.time()-submitted)*1000)
            if pos.get('mode')=='MOMENTUM_SCALP':p['momentumCloses']=int(p.get('momentumCloses',0))+1
            return True
        finally:self.pending=False

    async def _settle_position(self,slug):
        p=self.paper
        if not p or not p.get('position') or p['position'].get('marketSlug')!=slug:return
        pos=p['position'];resolved=await self._resolved_winner(slug)
        if resolved is None:return
        winner=resolved.upper()
        payout=pos['shares'] if pos['side']==winner else 0.0
        pnl=payout-pos['entryTotalCost']
        p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1
        if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
        else:
            p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
            p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
        if p['realizedPnl']<=-self.session_loss_limit or int(p.get('consecutiveLosses') or 0)>=2:
            p['status']='risk_locked';p['riskLockReason']='session risk lock after losses'
        p['position']=None
        self._record('SETTLE',side=pos['side'],price=1.0 if pos['side']==winner else 0.0,pnl=pnl,
                     detail=f'verified market resolution winner {winner}')
        self._attribute_close(pos,1.0 if pos['side']==winner else 0.0,0.,'verified settlement')

    def _save(self):
        now=time.time()
        if now-self._last_saved_at<1:return
        self._last_saved_at=now
        try:self.state_file.write_text(json.dumps(self.snapshot(),separators=(',',':')),encoding='utf-8')
        except Exception as exc:self._diagnostic('STATE_SAVE_ERROR',error=repr(exc))

    async def position_worker(self):
        while True:
            expected=time.monotonic()+.1
            await asyncio.sleep(.1)
            self.risk_loop_lag=max(0.,time.monotonic()-expected)
            if self.momentum_enabled:await self._momentum_manage(self.latest_model)

    async def engine_worker(self):
        last_audit=0.0
        while True:
            await asyncio.sleep(.25)
            p=self.paper
            if not p:continue
            now=time.time()
            if now-self.last_diagnostic_state>=1:
                self.last_diagnostic_state=now
                self._diagnostic('STATE',context=self._diagnostic_context())
            if now-getattr(self,'last_decision_sample',0)>=15:
                self.last_decision_sample=now
                self._diagnostic('DECISION_SAMPLE',decision=self.momentum_decision,signal=self.momentum_candidate,
                                 feed=dict(self.feed),bookAgeSec={k:now-v for k,v in self.book_observed.items()},
                                 spotAgeSec=now-self.spot_ts,twapAgeSec=now-self.twap_ts,riskLoopLagSec=self.risk_loop_lag)
            self._update_risk()
            if now>=p['endsAt'] and not p.get('position') and not self._inventory_has_residual():
                p['status']='complete';self._save();continue
            await self._maker_step()
            pair=self._pair_state();self.latest_pair=pair
            if pair.get('directEligible') and not p.get('position') and not self._inventory_has_residual() and not self.pending and self.market and (float(self.market.get('endTs') or 0)-now)>12:
                inv=p.get('inventory')
                if isinstance(inv,dict) and inv.get('quote'):
                    self._inventory_cancel_quote(inv,'direct executable complete set takes priority')
                await self._try_direct_pair(pair)
            m=self._model();self.latest_model=m
            if self.momentum_enabled:await self._momentum_step(m)
            if self.market:await self._inventory_step(m or {})
            if not m:
                self.latest_candidate=None;self.latest_reason=self.momentum_decision if self.momentum_enabled else 'waiting for live spot/TWAP/open reference'
                self._save();continue
            best=self._candidate(m);self.latest_candidate=best
            fresh_age=max(now-self.spot_ts if self.spot_ts else 1e9,now-self.twap_ts if self.twap_ts else 1e9)
            data_score=min(18,len(self.spot_sec)/5)
            lead_score=min(18,m.get('lagScore',0)*10)
            time_score=14 if m['remaining']<=RESOLUTION_WINDOW_SEC else 8
            confidence=max(0,min(100,(32 if self.open_trusted else 5)+(18 if fresh_age<FEED_FRESH_SEC else 4)+data_score+lead_score+time_score+min(8,abs(m['z'])*1.5)))
            reason=self._reason(m,best,confidence)
            if self.momentum_enabled:
                reason=self.momentum_decision;self.latest_mode='MOMENTUM_SCALP';self.latest_candidate=self.momentum_candidate
            self.latest_reason=reason
            self.latest_core=self._fast_core(m,best,confidence,reason)
            if self.momentum_enabled:
                verdict='REJECT' if p.get('status')=='risk_locked' else 'MANAGE' if p.get('position') else 'APPROVE' if self.momentum_candidate else 'WAIT'
                self.latest_core={'VEGA':{'name':'VEGA','job':'Momentum confirmation','verdict':verdict,'summary':self.momentum_decision},'TITAN':{'name':'TITAN','job':'Funded execution and risk','verdict':verdict,'summary':self.momentum_decision},'final':verdict}
            core_state=self.latest_core['final']
            if core_state!=self.last_core_state:
                self.last_core_state=core_state
                self._record('CORE',detail=f"VEGA {self.latest_core['VEGA']['verdict']}; TITAN {core_state}; {self.latest_core['TITAN']['summary']}")
            if now-last_audit>=1:
                self.reason_counts[reason]+=1;last_audit=now;p['candidatesSeen']+=1
                self.visual_history.append({
                    't':now,'spot':self.spot,'twap':self.twap,'open':self.open_ref,
                    'fairUp':m.get('fairUp'),'upAsk':self.bbo['up'].get('ask'),
                    'equity':self._equity(),'mode':self.latest_mode,'decision':reason
                })
            if self.lag_trading_enabled and reason=='OPPORTUNITY_LAG' and best and self.latest_core['TITAN']['verdict']=='APPROVE':
                p['signals']+=1
                mk=p.get('maker')
                maker_residual=bool(mk and (mk.get('upFilledPrice') is not None or mk.get('downFilledPrice') is not None))
                inv=p.get('inventory');inventory_residual=self._inventory_has_residual()
                if mk and not maker_residual:self._maker_cancel('qualified lag trade takes priority')
                if isinstance(inv,dict) and inv.get('quote') and not inventory_residual:self._inventory_cancel_quote(inv,'qualified lag trade takes priority')
                if not maker_residual and not inventory_residual and not p.get('position') and not self.pending and now-p.get('lastOpportunityAt',0)>3:
                    p['lastOpportunityAt']=now
                    await self._enter(best,m,confidence)
            pos=p.get('position')
            if pos and pos.get('mode') not in SELF_MANAGED_MODES and self.market and pos.get('marketSlug')==self.market.get('slug') and not self.pending:
                fresh_pair=self._pair_state();self.latest_pair=fresh_pair
                if fresh_pair.get('hedge',{}).get('eligible'):
                    if await self._try_hedge_pair(fresh_pair):
                        pos=None
                if pos:
                    key='up' if pos['side']=='UP' else 'down';bid=self.bbo[key].get('bid')
                else:
                    bid=None
                if bid is not None:
                    bid=float(bid);fee=pos['shares']*_fee_per_share(bid)
                    net=pos['shares']*bid-fee;pnl=net-pos['entryTotalCost']
                    fair=m['fairUp'] if pos['side']=='UP' else m['fairDown']
                    age=now-float(pos.get('entryAt') or now)
                    direction=1 if pos['side']=='UP' else -1
                    lead_reversed=m.get('leadBps',0)*direction<-.15
                    if pnl<=-MAX_TRADE_LOSS_USD:await self._try_sell('hard risk stop')
                    elif pnl>=max(.12,pos['entryTotalCost']*.025):await self._try_sell('take-profit on live bid')
                    elif pos.get('mode')=='LAG_SCALP' and fair-bid<=.010 and pnl>0:await self._try_sell('lag pricing gap converged')
                    elif pos.get('mode')=='LAG_SCALP' and age>=2.0 and lead_reversed:await self._try_sell('fast-price lead reversed')
                    elif pos.get('mode')=='LAG_SCALP' and age>=12:await self._try_sell('lag scalp time stop')
                    elif m['remaining']<=4:await self._try_sell('market near close')
            eq=self._equity();p['peakEquity']=max(p['peakEquity'],eq)
            if p['peakEquity']>0:p['maxDrawdown']=max(p['maxDrawdown'],(p['peakEquity']-eq)/p['peakEquity'])
            self._save()

    def snapshot(self):
        self._update_risk()
        p=self.paper or {}
        eq=self._equity() if self.paper else 0
        return {
            'status':'ok','asset':self.asset,'lane':self.lane,'interval':self.tag,'revision':ENGINE_REVISION,'serverTime':time.time(),'feed':dict(self.feed),'market':self.market,
            'bbo':self.bbo,'depth':self.depth,'spot':self.spot,'spotAgeSec':time.time()-self.spot_ts if self.spot_ts else None,
            'twap':self.twap,'twapAgeSec':time.time()-self.twap_ts if self.twap_ts else None,
            'openRef':self.open_ref,'openTrusted':self.open_trusted,
            'model':self.latest_model,'candidate':self.latest_candidate,'decision':self.latest_reason,'mode':self.latest_mode,'core':self.latest_core,'pair':self.latest_pair,
            'risk':{'momentumEntriesEnabled':self.momentum_enabled,'directionalLagEntriesEnabled':self.lag_trading_enabled,'maxTradeNotional':MAX_TRADE_NOTIONAL,'maxTradeLossUsd':MAX_TRADE_LOSS_USD,
                    'sessionLossLimitUsd':self.session_loss_limit,'lagConfirmSec':LAG_CONFIRM_SEC,
                    'alignmentSide':self.alignment_side,'alignmentAgeSec':max(0,time.time()-self.alignment_since) if self.alignment_since else 0,
                    'executionLatencyMs':int(EXECUTION_LATENCY_SEC*1000),'maxEntryChase':MAX_ENTRY_CHASE,
                    'inventoryV12Enabled':INVENTORY_V12_ENABLED,'inventoryPairTarget':self.pair_target,
                    'inventoryMaxHoldSec':INVENTORY_MAX_HOLD_SEC,'inventoryStopLossUsd':self.inventory_stop,'inventoryMaxInitialGap':INVENTORY_MAX_INITIAL_GAP,'inventoryMaxResidualUsd':self.residual_cap,'inventoryMaxFirstPrice':self.first_price_cap,'entryAudit':self.entry_audit,'walletQuotes':self.wallet_quotes},
            'strategies':{'edge':self.edge_decision,'model':self.model_decision,'momentumSizing':self.momentum_sizing,'edgeSignal':(self.edge.signals.get(self.lane) if self.edge else None)},
            'momentum':{'enabled':self.momentum_enabled,'decision':self.momentum_decision,'candidate':self.momentum_candidate,'entryNotionalCap':min(3.25,self.balance*.1),'stopLossUsd':self.inventory_stop,'takeProfitUsd':max(.18,self.inventory_stop),'maxHoldSec':30,'modelCalibrated':False},
            'bookHealth':{'observedAt':dict(self.book_observed),'ageSec':{k:time.time()-v if v else None for k,v in self.book_observed.items()},'eventLagSec':self.book_event_lag,'staleBookMessagesRejected':self.stale_book_messages,'riskLoopLagSec':self.risk_loop_lag,'lastTradeEventLagSec':self.trade_event_lag},
            'research':getattr(self,'research_state',{'mode':'SHADOW_ONLY','executionEnabled':False,'reason':'research starting'}),
            'diagnostics':self.diagnostics.status(),
            'history':list(self.visual_history)[-240:],
            'reasonCounts':dict(self.reason_counts),
            'paper':{**p,'revision':ENGINE_REVISION,'equity':eq,'maxDrawdownPct':100*float(p.get('maxDrawdown') or 0),
                     'totalPnl':(eq-float(p.get('startingBalance',20))) if p else 0,
                     'unrealizedPnl':((eq-float(p.get('startingBalance',20)))-float(p.get('realizedPnl') or 0)) if p else 0,
                     'roiPct':((eq/p.get('startingBalance',20))-1)*100 if p else 0,
                     'avgMatchedSetCost':(p.get('pairCostSum',0)/p.get('pairSharesSum',1) if p.get('pairSharesSum',0) else None),
                     'winRatePct':(p.get('wins',0)/p.get('trades',1)*100 if p.get('trades',0) else 0)}
        }

engine=PolyServerDemo()

