from __future__ import annotations
import asyncio, json, math, time
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
ENGINE_REVISION="12.1-risk-fix"
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
    def __init__(self):
        self.market=None
        self.bbo={'up':{'bid':None,'ask':None,'bidSize':0.0,'askSize':0.0},
                  'down':{'bid':None,'ask':None,'bidSize':0.0,'askSize':0.0}}
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
        self.tasks=[]
        self.started=False

    async def _get(self,url,params=None,headers=None):
        async with httpx.AsyncClient(timeout=12,follow_redirects=False) as c:
            r=await c.get(url,params=params,headers=headers)
        r.raise_for_status()
        return r.json()

    async def start(self):
        if self.started: return
        self.started=True
        await self._seed_sigma()
        if not self._restore_saved():
            self._new_session()
        self.tasks=[
            asyncio.create_task(self.market_worker(),name='poly-market'),
            asyncio.create_task(self.clob_worker(),name='poly-clob'),
            asyncio.create_task(self.rtds_worker(),name='poly-rtds'),
            asyncio.create_task(self.engine_worker(),name='poly-paper'),
        ]

    def _restore_saved(self):
        try:
            if not STATE_FILE.exists(): return False
            data=json.loads(STATE_FILE.read_text(encoding='utf-8'))
            p=data.get('paper')
            now=time.time()
            if not isinstance(p,dict) or int(p.get('version') or 0)!=12 or float(p.get('endsAt') or 0)<=now:
                return False
            # Strip calculated presentation fields before resuming mutable state.
            p=dict(p)
            p.pop('equity',None);p.pop('roiPct',None);p.pop('winRatePct',None)
            self.paper=p
            self.reason_counts=defaultdict(int,data.get('reasonCounts') or {})
            self.visual_history.clear()
            for row in (data.get('history') or [])[-900:]:
                if isinstance(row,dict): self.visual_history.append(row)
            m=data.get('market')
            if isinstance(m,dict) and float(m.get('startTs') or 0)<=now<float(m.get('endTs') or 0):
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
                if self.spot is not None:self.spot_ts=now-max(0,float(sa or 0))
                if self.twap is not None:self.twap_ts=now-max(0,float(ta or 0))
            print(f"POLY_DEMO_RESTORED session={self.paper.get('sessionId')} market={self.market.get('slug') if self.market else None} openTrusted={self.open_trusted}",flush=True)
            return True
        except Exception as exc:
            print(f"POLY_DEMO_RESTORE_FAILED {type(exc).__name__}",flush=True)
            return False

    def _new_session(self):
        now=time.time()
        self.paper={
            'version':12,'revision':ENGINE_REVISION,'sessionId':'srv_'+hex(int(now*1_000_000))[2:],
            'startedAt':now,'endsAt':now+10800,'startingBalance':20.0,'cash':20.0,
            'realizedPnl':0.0,'fees':0.0,'trades':0,'wins':0,'losses':0,
            'blocked':0,'nofills':0,'peakEquity':20.0,'maxDrawdown':0.0,
            'position':None,'events':[],'status':'running','lastOpportunityAt':0.0,
            'signals':0,'candidatesSeen':0,'executionAttempts':0,'postLatencyRejects':0,'tradedMarkets':[],'cooldownUntil':0.0,
            'pairMarkets':[],'pairAttempts':0,'matchedSets':0.0,'lockedPairPnl':0.0,'pairCostSum':0.0,'pairSharesSum':0.0,
            'maker':None,'makerQuotes':0,'makerFills':0,'makerMerges':0,'makerResidualSettles':0,'makerEnabled':MAKER_PAIRING_ENABLED,
            'inventory':None,'inventoryEnabled':INVENTORY_V12_ENABLED,'inventoryQuotes':0,'inventoryFills':0,'inventoryCancels':0,
            'inventoryTakerHedges':0,'inventoryMerges':0,'inventoryResidualSettles':0,'inventoryRecoveryExits':0,
            'consecutiveLosses':0,'riskLockReason':None
        }
        self.reason_counts.clear()
        self._save()

    async def reset(self):
        self._new_session()
        return self.snapshot()

    async def _seed_sigma(self):
        try:
            data=await self._get(COINBASE_REST+'/products/BTC-USD/candles',
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
        now=int(time.time());bucket=(now//300)*300
        selected=None
        for ts in [bucket,bucket+300,bucket-300]:
            try: data=await self._get(POLY_GAMMA+'/markets',{'slug':f'btc-updown-5m-{ts}'})
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
        return {
            'slug':m.get('slug') or f'btc-updown-5m-{ts}','question':m.get('question'),
            'startTs':ts,'endTs':ts+300,'active':bool(m.get('active')),
            'closed':bool(m.get('closed')),'acceptingOrders':bool(m.get('acceptingOrders')),
            'minOrderSize':float(m.get('orderMinSize') or 0),
            'minTickSize':float(m.get('orderPriceMinTickSize') or .01),
            'volume':float(m.get('volumeNum') or m.get('volume') or 0),
            'liquidity':float(m.get('liquidityNum') or m.get('liquidity') or 0),
            'upToken':up,'downToken':down,
            'up':{'bid':_best(ub,'bids'),'ask':_best(ub,'asks')},
            'down':{'bid':_best(db,'bids'),'ask':_best(db,'asks')}
        }

    def _apply_rest_book(self,m):
        for side in ('up','down'):
            self.bbo[side]['bid']=m[side]['bid']['price']
            self.bbo[side]['bidSize']=m[side]['bid']['size']
            self.bbo[side]['ask']=m[side]['ask']['price']
            self.bbo[side]['askSize']=m[side]['ask']['size']

    async def market_worker(self):
        while True:
            try:
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
                    self.open_ref=None;self.open_ref_ts=0;self.open_trusted=False
                    self.alignment_side=None;self.alignment_since=0.0
                    self.market_version+=1
                    print('POLY_DEMO_MARKET',m['slug'],flush=True)
                self.market=m;self._apply_rest_book(m);self.feed['market']='live'
            except Exception as e:
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
                    self.feed['clob']='live'
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
                    finally:
                        pt.cancel()
            except Exception as e:
                self.feed['clob']='error:'+type(e).__name__
                await asyncio.sleep(1)

    def _handle_clob(self,m):
        if not self.market or not isinstance(m,dict): return
        def side_for(t):
            if str(t)==str(self.market.get('upToken')): return 'up'
            if str(t)==str(self.market.get('downToken')): return 'down'
            return None
        et=m.get('event_type')
        if et=='book':
            side=side_for(m.get('asset_id'))
            if not side:return
            bids=[(float(x['price']),float(x.get('size') or 0)) for x in (m.get('bids') or []) if 'price'in x]
            asks=[(float(x['price']),float(x.get('size') or 0)) for x in (m.get('asks') or []) if 'price'in x]
            if bids:
                p=max(x[0] for x in bids);self.bbo[side]['bid']=p;self.bbo[side]['bidSize']=sum(x[1] for x in bids if x[0]==p)
            if asks:
                p=min(x[0] for x in asks);self.bbo[side]['ask']=p;self.bbo[side]['askSize']=sum(x[1] for x in asks if x[0]==p)
        elif et=='price_change':
            for pc in (m.get('price_changes') or m.get('changes') or []):
                side=side_for(pc.get('asset_id'))
                if not side:continue
                if pc.get('best_bid') is not None:self.bbo[side]['bid']=float(pc['best_bid'])
                if pc.get('best_ask') is not None:self.bbo[side]['ask']=float(pc['best_ask'])
        elif et=='best_bid_ask':
            side=side_for(m.get('asset_id'))
            if side:
                if m.get('best_bid') is not None:self.bbo[side]['bid']=float(m['best_bid'])
                if m.get('best_ask') is not None:self.bbo[side]['ask']=float(m['best_ask'])
        elif et=='last_trade_price':
            side=side_for(m.get('asset_id'))
            if side:
                try:
                    price=float(m.get('price'));size=float(m.get('size') or 0)
                    ts=float(m.get('timestamp') or time.time()*1000)
                    tsec=(ts/1000 if ts>1e12 else ts)
                    self.trade_seq+=1
                    self.trade_prints.append({'seq':self.trade_seq,'t':tsec,'side':side,'price':price,'size':size,'aggressor':m.get('side')})
                except Exception:
                    pass

    async def rtds_worker(self):
        while True:
            try:
                async with websockets.connect(RTDS_WS,ping_interval=None,close_timeout=2,max_size=2_000_000) as ws:
                    flt=json.dumps({'symbol':'btc/usd'},separators=(',',':'))
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
                self.feed['rtds']='error:'+type(e).__name__
                print('POLY_DEMO_RTDS_ERROR',type(e).__name__,str(e)[:180],flush=True)
                await asyncio.sleep(1)

    def _handle_rtds(self,m):
        if not isinstance(m,dict) or m.get('message'):return
        topic=str(m.get('topic') or '')
        p=m.get('payload') or m.get('data') or {}
        try:value=float(p.get('value',p.get('full_accuracy_value',m.get('value'))))
        except Exception:return
        try:ts=float(p.get('timestamp',m.get('timestamp',time.time()*1000)))
        except Exception:ts=time.time()*1000
        tms=ts*1000 if ts<1e12 else ts
        if value<=0:return
        if topic in {'crypto_prices','crypto_prices_chainlink'}:
            self.spot=value;self.spot_ts=tms/1000
            sec=int(tms/1000);self.spot_sec[sec]=value
            cutoff=int(time.time())-300
            for k in list(self.spot_sec):
                if k<cutoff:self.spot_sec.pop(k,None)
        elif topic=='crypto_prices_twap_sixty':
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
            horizon=min(remaining,240)
            projected=self.spot*math.exp(drift*min(horizon,90)*.10)+basis
            std=max(.00008,sig*math.sqrt(max(30,horizon))*1.15)

        margin=(projected-self.open_ref)/self.open_ref
        total=math.sqrt(std*std+.00006**2)
        z=max(-5,min(5,margin/total))
        raw_up=max(.01,min(.99,_cdf(z)))

        # Shrink early-window resolution probability toward 50%; confidence grows
        # naturally as the five-minute market approaches resolution.
        progress=max(0,min(1,elapsed/300))
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
        if float(p.get('realizedPnl') or 0)<=-SESSION_LOSS_LIMIT_USD:
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
            return 'waiting for aligned BTC/TWAP direction'

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
                'sessionLossLimitUsd':SESSION_LOSS_LIMIT_USD,
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
        eq=self._equity();peak=max(float(p.get('peakEquity') or p['startingBalance']),eq)
        p['peakEquity']=peak
        p['maxDrawdownUsd']=max(float(p.get('maxDrawdownUsd') or 0),peak-eq)
        p['maxDrawdown']=max(float(p.get('maxDrawdown') or 0),(peak-eq)/peak if peak>0 else 0)
        if min(float(p.get('realizedPnl') or 0),eq-float(p['startingBalance']))<=-SESSION_LOSS_LIMIT_USD:
            p['status']='risk_locked';p['riskLockReason']='session realized or marked loss limit reached'
        if int(p.get('consecutiveLosses') or 0)>=2:
            p['status']='risk_locked';p['riskLockReason']='two consecutive losses'

    def _inventory_first_leg_candidate(self,inv,m):
        if not INVENTORY_V12_ENABLED or not self.open_trusted or not self.market:return None
        if self.paper.get('status')!='running' or self.paper.get('position') or self.pending:return None
        if self._inventory_has_residual():return None
        self._update_risk()
        if self.paper.get('status')!='running' or not self._inventory_feed_ready():return None
        if time.time()<float(self.paper.get('cooldownUntil') or 0) or time.time()>=self.paper['endsAt']:return None
        if int(inv.get('cycles') or 0)>=INVENTORY_MAX_CYCLES_PER_MARKET:return None
        now=time.time();elapsed=now-float(self.market.get('startTs') or now);remaining=float(self.market.get('endTs') or 0)-now
        if elapsed<INVENTORY_OPEN_AFTER_SEC or remaining<=INVENTORY_STOP_NEW_REMAINING_SEC:return None
        tick=max(.001,float(self.market.get('minTickSize') or .01));shares=max(5.0,float(self.market.get('minOrderSize') or 0))
        choices=[]
        for side in ('up','down'):
            opp='down' if side=='up' else 'up'
            bid=self.bbo[side].get('bid');ask=self.bbo[side].get('ask');oppask=self.bbo[opp].get('ask')
            if None in (bid,ask,oppask):continue
            bid=float(bid);ask=float(ask);oppask=float(oppask)
            if bid>INVENTORY_MAX_FIRST_PRICE+1e-9:continue
            maker_px=min(INVENTORY_MAX_FIRST_PRICE,_floor_tick(max(bid,min(ask-tick,INVENTORY_MAX_FIRST_PRICE)),tick))
            if maker_px<.03 or maker_px>=ask-1e-9:continue
            if shares*maker_px>min(INVENTORY_MAX_RESIDUAL_USD,float(self.paper.get('cash') or 0))+1e-9:continue
            hedge_cost=shares*(oppask+_fee_per_share(oppask))
            if float(self.bbo[opp].get('askSize') or 0)<shares:continue
            if shares*maker_px+hedge_cost>float(self.paper['cash']):continue
            loss_budget=SESSION_LOSS_LIMIT_USD+min(float(self.paper['realizedPnl']),self._equity()-float(self.paper['startingBalance']))
            if shares*maker_px>loss_budget:continue
            projected=maker_px+oppask+_fee_per_share(oppask)
            if projected>INVENTORY_PAIR_TARGET+INVENTORY_MAX_INITIAL_GAP:continue
            direction=1 if side=='up' else -1
            lead=float(m.get('leadBps') or 0)*direction;imp=float(m.get('impulse5Bps') or 0)*direction
            if lead<INVENTORY_MIN_REVERSAL_LEAD_BPS or imp<INVENTORY_MIN_REVERSAL_IMPULSE_BPS:continue
            choices.append((maker_px,projected,lead,imp,side,shares,bid,ask))
        if not choices:return None
        maker_px,projected,lead,imp,side,shares,bid,ask=min(choices,key=lambda x:(x[1],x[0]))
        return {'side':side,'price':maker_px,'shares':shares,'projected':projected,'lead':lead,'impulse':imp,'bid':bid,'ask':ask}

    def _inventory_place_quote(self,inv,side,price,shares,kind):
        tick=max(.001,float(self.market.get('minTickSize') or .01));price=_floor_tick(price,tick)
        bid=self.bbo[side].get('bid');ask=self.bbo[side].get('ask')
        if bid is None or ask is None or price<=0 or price>=float(ask)-1e-9:return False
        bid=float(bid);ask=float(ask)
        at_best=abs(price-bid)<=tick/2
        improved=price>bid+tick/2
        queue_need=float(shares)+(float(self.bbo[side].get('bidSize') or 0) if at_best else (0.0 if improved else 1e12))
        inv['quote']={'side':side,'price':price,'shares':float(shares),'kind':kind,'createdAt':time.time(),
                      'queueNeed':queue_need,'seen':0.0,'lastTradeSeq':self.trade_seq}
        inv['status']='QUOTING';inv['lastAction']=f'{kind} {side.upper()} @ {price:.3f}'
        self.paper['inventoryQuotes']=int(self.paper.get('inventoryQuotes') or 0)+1
        self._record('INV_QUOTE',side=side.upper(),price=price,detail=f'{kind}; {shares:.2f}sh; queue need {queue_need if queue_need<1e11 else -1:.2f}')
        return True

    def _inventory_process_quote(self,inv,m):
        q=inv.get('quote')
        if not q:return
        totals=self._inventory_totals(inv)
        if q.get('kind')=='COMPLEMENT':
            res=totals.get('residualSide')
            if not res or res==q['side'] or abs(totals[res]['shares']-float(q['shares']))>1e-9:
                self._inventory_cancel_quote(inv,'orphan complement cancelled');return
        elif self.paper.get('status')!='running' or not self._inventory_feed_ready() or time.time()<float(self.paper.get('cooldownUntil') or 0):
            self._inventory_cancel_quote(inv,'entry risk/feed gate');return
        side=q['side'];tick=max(.001,float(self.market.get('minTickSize') or .01))
        if time.time()-float(q.get('createdAt') or 0)>INVENTORY_QUOTE_TTL_SEC and q.get('kind')=='FIRST':
            self._inventory_cancel_quote(inv,'first-leg quote TTL expired');return
        if q.get('kind')=='FIRST':
            candidate=self._inventory_first_leg_candidate(inv,m)
            opp='down' if side=='up' else 'up';ask=self.bbo[opp].get('ask')
            if not candidate or candidate['side']!=side or ask is None or float(q['price'])+float(ask)+_fee_per_share(float(ask))>INVENTORY_PAIR_TARGET or float(q['shares'])*float(q['price'])>INVENTORY_MAX_RESIDUAL_USD:
                self._inventory_cancel_quote(inv,'entry no longer hedgeable within risk cap');return
            direction=1 if side=='up' else -1
            if float(m.get('leadBps') or 0)*direction<-0.05:
                self._inventory_cancel_quote(inv,'cheap-leg reversal signal failed');return
        last=int(q.get('lastTradeSeq') or 0)
        newprints=[z for z in self.trade_prints if int(z.get('seq') or 0)>last]
        if newprints:q['lastTradeSeq']=max(int(z['seq']) for z in newprints)
        for z in newprints:
            if z.get('side')!=side:continue
            zp=float(z.get('price') or 99);qp=float(q['price'])
            if zp<qp-tick/2:
                q['seen']=max(float(q.get('seen') or 0),float(q.get('queueNeed') or 0))
            elif abs(zp-qp)<=tick/2:
                q['seen']=float(q.get('seen') or 0)+max(0.0,float(z.get('size') or 0))
        if float(q.get('seen') or 0)<float(q.get('queueNeed') or 1e18):return
        cost=float(q['shares'])*float(q['price'])
        if cost>float(self.paper.get('cash') or 0)+1e-9:
            self._inventory_cancel_quote(inv,'cash unavailable at simulated fill');return
        self._inventory_add_lot(inv,side,float(q['shares']),float(q['price']),0.0,'MAKER')
        inv['quote']=None
        self._inventory_merge_available(inv)

    async def _inventory_taker_complement(self,inv,res_side,res_unit):
        if self.pending:return False
        opp='down' if res_side=='up' else 'up';ask0=self.bbo[opp].get('ask');avail0=float(self.bbo[opp].get('askSize') or 0)
        totals=self._inventory_totals(inv);shares=float(totals[res_side]['shares'] or 0)
        if ask0 is None or shares<=0 or avail0<shares:return False
        ask0=float(ask0);allin=res_unit+ask0+_fee_per_share(ask0)
        if allin>INVENTORY_PAIR_TARGET+1e-9:return False
        total0=shares*(ask0+_fee_per_share(ask0))
        if total0>float(self.paper.get('cash') or 0)+1e-9:return False
        self._inventory_cancel_quote(inv,'cancel maker complement before taker hedge')
        version=self.market_version;session=self.paper
        self.pending=True
        self._record('INV_HEDGE_ORDER',side=opp.upper(),price=ask0,detail=f'{shares:.2f}sh; projected pair {allin:.4f}')
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not session or self.market_version!=version or not self._inventory_feed_ready():return False
            ask=self.bbo[opp].get('ask');avail=float(self.bbo[opp].get('askSize') or 0)
            if ask is None or avail<shares:return False
            ask=float(ask);fee=shares*_fee_per_share(ask);fresh=res_unit+ask+_fee_per_share(ask)
            if fresh>INVENTORY_PAIR_TARGET+1e-9 or shares*(ask+_fee_per_share(ask))>float(self.paper.get('cash') or 0)+1e-9:
                self._record('INV_HEDGE_NOFILL',side=opp.upper(),price=ask,detail=f'post-latency pair {fresh:.4f}');return False
            self.paper['inventoryTakerHedges']=int(self.paper.get('inventoryTakerHedges') or 0)+1
            self._inventory_add_lot(inv,opp,shares,ask,fee,'TAKER')
            self._inventory_merge_available(inv)
            return True
        finally:self.pending=False

    async def _inventory_recovery_exit(self,inv,res_side,force=False):
        if self.pending:return False
        totals=self._inventory_totals(inv);shares=float(totals[res_side]['shares'] or 0);cost=float(totals[res_side]['cost'] or 0)
        bid=self.bbo[res_side].get('bid');avail=float(self.bbo[res_side].get('bidSize') or 0)
        if bid is None or shares<=0 or avail<shares:return False
        bid=float(bid);fee=shares*_fee_per_share(bid);net=shares*bid-fee;pnl=net-cost
        if not force and pnl<INVENTORY_RECOVERY_PROFIT_USD:return False
        self._inventory_cancel_quote(inv,'cancel complement before residual exit')
        version=self.market_version;session=self.paper
        self.pending=True
        await asyncio.sleep(EXECUTION_LATENCY_SEC)
        try:
            if self.paper is not session or self.market_version!=version or not self._inventory_feed_ready():return False
            bid2=self.bbo[res_side].get('bid');avail2=float(self.bbo[res_side].get('bidSize') or 0)
            if bid2 is None or avail2<shares:return False
            bid2=float(bid2);fee2=shares*_fee_per_share(bid2);net2=shares*bid2-fee2;pnl2=net2-cost
            if not force and pnl2<0:return False
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
        if not self._inventory_feed_ready():
            self._inventory_cancel_quote(inv,'feed stale; fills paused');return
        self._inventory_process_quote(inv,m)
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
            force=(age>=INVENTORY_MAX_HOLD_SEC or pnl<=-INVENTORY_STOP_LOSS_USD
                   or self.paper.get('status')!='running' or time.time()>=self.paper['endsAt']
                   or float(self.market['endTs'])-time.time()<=15)
            if await self._inventory_recovery_exit(inv,res,force=force):return
            if force:
                self._inventory_cancel_quote(inv,'risk exit pending executable liquidity')
                inv['status']='EXIT_PENDING';inv['lastAction']='risk exit waiting for liquidity';return
            opp='down' if res=='up' else 'up'
            max_opp=_floor_tick(INVENTORY_PAIR_TARGET-unit,float(self.market.get('minTickSize') or .01))
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

    async def _settle_inventory(self,slug):
        p=self.paper
        if not p:return
        inv=p.get('inventory')
        if not isinstance(inv,dict) or inv.get('marketSlug')!=slug:return
        if inv.get('quote'):self._inventory_cancel_quote(inv,'market rollover')
        self._inventory_merge_available(inv)
        totals=self._inventory_totals(inv);res=totals.get('residualSide')
        if not res:return
        if self.twap is None or self.open_ref is None:
            self._record('INV_RESIDUAL_UNSETTLED',side=res.upper(),detail='missing trusted final/open reference');return
        winner='up' if float(self.twap)>=float(self.open_ref) else 'down'
        shares=float(totals[res]['shares'] or 0);cost=float(totals[res]['cost'] or 0);payout=shares if res==winner else 0.0;pnl=payout-cost
        self._inventory_consume(inv[res+'Lots'],shares)
        p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1;p['inventoryResidualSettles']=int(p.get('inventoryResidualSettles') or 0)+1
        if pnl>=0:p['wins']+=1;p['consecutiveLosses']=0
        else:p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
        self._record('INV_RESIDUAL_SETTLE',side=res.upper(),price=1.0 if res==winner else 0.0,pnl=pnl,detail=f'bounded residual; winner {winner.upper()}; no forced loss sale')
        inv['status']='SETTLED';inv['lastAction']=f'residual settled {pnl:+.4f}'
        if pnl<0:p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
        self._update_risk()
        if p['realizedPnl']<=-SESSION_LOSS_LIMIT_USD or int(p.get('consecutiveLosses') or 0)>=2:
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
            if p['realizedPnl']<=-SESSION_LOSS_LIMIT_USD or int(p.get('consecutiveLosses') or 0)>=2:
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
        if p['realizedPnl']<=-SESSION_LOSS_LIMIT_USD or int(p.get('consecutiveLosses') or 0)>=2:
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

    def _record(self,action,**kw):
        if not self.paper:return
        ev={'t':time.time(),'action':action,**kw}
        self.paper['events'].insert(0,ev);self.paper['events']=self.paper['events'][:500]
        print('POLY_DEMO_EVENT',action,kw.get('side',''),str(kw.get('detail',''))[:120],flush=True)
        self._save()

    async def _enter(self,best,m,confidence):
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
        p=self.paper;pos=p['position'];key='up' if pos['side']=='UP' else 'down'
        min_sh=float(self.market.get('minOrderSize') or 0) if self.market else 0
        if pos['shares']<min_sh:return False
        bid0=self.bbo[key].get('bid');size0=float(self.bbo[key].get('bidSize') or 0)
        if bid0 is None or size0<pos['shares']:return False
        bid0=float(bid0);tick=max(.001,float(self.market.get('minTickSize') or .01));min_price=max(.01,bid0-tick)
        self.pending=True;self._record('ORDER',side=pos['side'],price=bid0,detail=f'paper FAK sell; {reason}')
        await asyncio.sleep(.35)
        try:
            bid=self.bbo[key].get('bid');avail=float(self.bbo[key].get('bidSize') or 0)
            if bid is None or float(bid)<min_price or avail<pos['shares']:
                p['nofills']+=1;self._record('NOFILL',side=pos['side'],price=float(bid) if bid is not None else None,detail='exit book moved during 350ms latency');return False
            bid=float(bid);fee=pos['shares']*_fee_per_share(bid);proceeds=pos['shares']*bid-fee
            pnl=proceeds-pos['entryTotalCost']
            p['cash']+=proceeds;p['fees']+=fee;p['realizedPnl']+=pnl;p['trades']+=1
            if pnl>=0:
                p['wins']+=1;p['consecutiveLosses']=0
            else:
                p['losses']+=1;p['consecutiveLosses']=int(p.get('consecutiveLosses') or 0)+1
                p['cooldownUntil']=time.time()+LOSS_COOLDOWN_SEC
            if p['realizedPnl']<=-SESSION_LOSS_LIMIT_USD or int(p.get('consecutiveLosses') or 0)>=2:
                p['status']='risk_locked'
                p['riskLockReason']='session risk lock after losses'
            self._record('SELL',side=pos['side'],price=bid,pnl=pnl,detail=reason+f'; fee USD {fee:.5f}')
            p['position']=None
            return True
        finally:self.pending=False

    async def _settle_position(self,slug):
        p=self.paper
        if not p or not p.get('position') or p['position'].get('marketSlug')!=slug:return
        pos=p['position'];final=self.twap
        if final is None or pos.get('openRef') is None:return
        winner='UP' if final>=pos['openRef'] else 'DOWN'
        payout=pos['shares'] if pos['side']==winner else 0.0
        pnl=payout-pos['entryTotalCost']
        p['cash']+=payout;p['realizedPnl']+=pnl;p['trades']+=1
        if pnl>=0:p['wins']+=1
        else:p['losses']+=1
        self._record('SETTLE',side=pos['side'],price=1.0 if pos['side']==winner else 0.0,pnl=pnl,
                     detail=f'TWAP resolution winner {winner}; final {final:.2f}')
        p['position']=None

    def _save(self):
        try:STATE_FILE.write_text(json.dumps(self.snapshot(),separators=(',',':')),encoding='utf-8')
        except Exception:pass

    async def engine_worker(self):
        last_audit=0.0
        while True:
            await asyncio.sleep(.25)
            p=self.paper
            if not p:continue
            now=time.time()
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
            if self.market:await self._inventory_step(m or {})
            if not m:
                self.latest_candidate=None;self.latest_reason='waiting for live spot/TWAP/open reference'
                self._save();continue
            best=self._candidate(m);self.latest_candidate=best
            fresh_age=max(now-self.spot_ts if self.spot_ts else 1e9,now-self.twap_ts if self.twap_ts else 1e9)
            data_score=min(18,len(self.spot_sec)/5)
            lead_score=min(18,m.get('lagScore',0)*10)
            time_score=14 if m['remaining']<=RESOLUTION_WINDOW_SEC else 8
            confidence=max(0,min(100,(32 if self.open_trusted else 5)+(18 if fresh_age<FEED_FRESH_SEC else 4)+data_score+lead_score+time_score+min(8,abs(m['z'])*1.5)))
            reason=self._reason(m,best,confidence);self.latest_reason=reason
            self.latest_core=self._fast_core(m,best,confidence,reason)
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
            if reason=='OPPORTUNITY_LAG' and best and self.latest_core['TITAN']['verdict']=='APPROVE':
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
            if pos and self.market and pos.get('marketSlug')==self.market.get('slug') and not self.pending:
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
            'status':'ok','revision':ENGINE_REVISION,'serverTime':time.time(),'feed':dict(self.feed),'market':self.market,
            'bbo':self.bbo,'spot':self.spot,'spotAgeSec':time.time()-self.spot_ts if self.spot_ts else None,
            'twap':self.twap,'twapAgeSec':time.time()-self.twap_ts if self.twap_ts else None,
            'openRef':self.open_ref,'openTrusted':self.open_trusted,
            'model':self.latest_model,'candidate':self.latest_candidate,'decision':self.latest_reason,'mode':self.latest_mode,'core':self.latest_core,'pair':self.latest_pair,
            'risk':{'maxTradeNotional':MAX_TRADE_NOTIONAL,'maxTradeLossUsd':MAX_TRADE_LOSS_USD,
                    'sessionLossLimitUsd':SESSION_LOSS_LIMIT_USD,'lagConfirmSec':LAG_CONFIRM_SEC,
                    'alignmentSide':self.alignment_side,'alignmentAgeSec':max(0,time.time()-self.alignment_since) if self.alignment_since else 0,
                    'executionLatencyMs':int(EXECUTION_LATENCY_SEC*1000),'maxEntryChase':MAX_ENTRY_CHASE,
                    'inventoryV12Enabled':INVENTORY_V12_ENABLED,'inventoryPairTarget':INVENTORY_PAIR_TARGET,
                    'inventoryMaxHoldSec':INVENTORY_MAX_HOLD_SEC,'inventoryStopLossUsd':INVENTORY_STOP_LOSS_USD,'inventoryMaxInitialGap':INVENTORY_MAX_INITIAL_GAP,'inventoryMaxResidualUsd':INVENTORY_MAX_RESIDUAL_USD,'inventoryMaxFirstPrice':INVENTORY_MAX_FIRST_PRICE},
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
