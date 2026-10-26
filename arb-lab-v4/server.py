"""ARB/LAB One Site: public read-only scanner and non-custodial Solana RPC/Jupiter gateway.

No wallet private keys, passphrases or encrypted vaults are sent to this service.
Live transaction signing, when explicitly enabled, happens ONLY in the unlocked browser tab.
This is experimental, non-atomic and cannot ensure realized profitability.
"""
from __future__ import annotations
from typing import Literal
import os
import re
import time
import asyncio
import json
import uuid
from collections import defaultdict, deque
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, PlainTextResponse
from pydantic import BaseModel, Field
from poly_portfolio import engine as poly_server_engine
from poly_storage import DurableStore
poly_storage=DurableStore(poly_server_engine)

ROOT = Path(__file__).resolve().parent
JUPITER = 'https://api.jup.ag/swap/v2'
RPC = os.environ.get('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
SOL='So11111111111111111111111111111111111111112'
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
app=FastAPI(title='ARB/LAB — Unified Solana Terminal',docs_url=None,redoc_url=None)
app.add_middleware(CORSMiddleware,allow_origins=['https://arb-lab-v4-unified.onrender.com'],allow_credentials=False,allow_methods=['GET','POST','OPTIONS'],allow_headers=['*'])
POLY_ENGINE_ROLE=os.environ.get('POLY_ENGINE_ROLE','combined').strip().lower()
requests_by_ip=defaultdict(deque)
jupiter_order_lock=asyncio.Lock()
last_jupiter_order=0.0
jupiter_provider_selftest='pending'
market_scan_selftest='pending'
market_latest=None
market_history_live=deque(maxlen=720)
market_live_status='warming-up'
market_seq=0
jito_selftest='pending'
poly_selftest='pending'
poly_demo_sessions={}
poly_demo_latest=None
POLY_DEMO_FILE=Path('/tmp/poly_demo_sessions.json')
JITO='https://singapore.mainnet.block-engine.jito.wtf'
POLY_GAMMA='https://gamma-api.polymarket.com'
POLY_CLOB='https://clob.polymarket.com'
COINBASE_REST='https://api.exchange.coinbase.com'

@app.middleware('http')
async def guard(request:Request,call_next):
    if request.url.path.startswith('/api/'):
        ip=(request.client.host if request.client else 'unknown')
        path=request.url.path
        # Keep the read-only POLY/EDGE snapshot stream in its own bucket.
        # One dashboard poller can never consume the trading/API rate budget.
        bucket='poly-snapshot' if path=='/api/poly/demo/server' else 'api'
        limit=600 if bucket=='poly-snapshot' else 240
        key=(ip,bucket)
        now=time.monotonic(); hits=requests_by_ip[key]
        while hits and hits[0]<now-60: hits.popleft()
        if len(hits)>=limit:
            return JSONResponse({'detail':'Rate limit reached. Automatic backoff active.','retryAfterSeconds':2},status_code=429,headers={'Retry-After':'2'})
        hits.append(now)
    return await call_next(request)

@app.head('/')
async def head_home():return Response(status_code=200,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@app.get('/')
async def home():return FileResponse(ROOT/'index.html', headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})

@app.get('/app.js')
async def app_js():return FileResponse(ROOT/'app.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/market.js')
async def market_js():return FileResponse(ROOT/'market.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/style.css')
async def style():return FileResponse(ROOT/'style.css',media_type='text/css',headers={'Cache-Control':'no-store'})

@app.get('/poly')
async def poly_home():return FileResponse(ROOT/'poly-terminal.html',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@app.get('/poly/classic')
async def poly_classic():   # classic view retired; old bookmarks land on the terminal dashboard
    from fastapi.responses import RedirectResponse
    return RedirectResponse('/poly',status_code=301)

@app.get('/poly-terminal.js')
async def poly_terminal_js():return FileResponse(ROOT/'poly-terminal.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/poly-demo.js')
async def poly_demo_js():return FileResponse(ROOT/'poly-demo.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/poly-log.js')
async def poly_log_js():return FileResponse(ROOT/'poly-log.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/poly.css')
async def poly_css():return FileResponse(ROOT/'poly.css',media_type='text/css',headers={'Cache-Control':'no-store'})


def _save_demo_sessions():
    try:
        payload={'latest':poly_demo_latest,'sessions':poly_demo_sessions}
        POLY_DEMO_FILE.write_text(json.dumps(payload,separators=(',',':')),encoding='utf-8')
    except Exception as exc:
        print(f'POLY_DEMO_SAVE_FAILED {type(exc).__name__}',flush=True)

def _load_demo_sessions():
    global poly_demo_sessions,poly_demo_latest
    try:
        if POLY_DEMO_FILE.exists():
            payload=json.loads(POLY_DEMO_FILE.read_text(encoding='utf-8'))
            sessions=payload.get('sessions') or {}
            if isinstance(sessions,dict):
                poly_demo_sessions=sessions
                poly_demo_latest=payload.get('latest')
    except Exception as exc:
        print(f'POLY_DEMO_LOAD_FAILED {type(exc).__name__}',flush=True)

@app.on_event('startup')
async def load_poly_demo_state():
    _load_demo_sessions()

@app.on_event('startup')
async def start_poly_server_demo():
    if POLY_ENGINE_ROLE=='frontend':
        print('POLY_SERVER_DEMO_DISABLED role=frontend',flush=True)
        return
    # Runs inside startup, before uvicorn binds the port, so the URL still routes
    # to the previous instance during a zero-downtime deploy.
    await poly_server_engine.prefetch_handover()
    await poly_storage.start()
    print(f'POLY_SERVER_DEMO_STARTED role={POLY_ENGINE_ROLE}',flush=True)

async def _keepalive(url):
    # Free Render instances sleep after ~15 idle minutes, which stops the paper
    # engine. A periodic external request keeps it awake while enabled.
    async with httpx.AsyncClient(timeout=20) as client:
        await asyncio.sleep(30)
        while True:
            # Every 4 min: the host sleeps after ~15 idle minutes, and a 10-min ping left too
            # little margin after an unplanned restart (the service slept at 00:02 UTC).
            try:await client.get(url+'/health')
            except Exception as exc:print('POLY_KEEPALIVE_FAILED',type(exc).__name__,flush=True)
            await asyncio.sleep(240)

async def _successor_watch(url):
    # During a zero-downtime deploy the old instance keeps running for a while. Once the public URL
    # answers with a newer engine, this instance stops trading (without a DB lock to arbitrate).
    async with httpx.AsyncClient(timeout=8) as client:
        while not poly_server_engine.yielded:
            await asyncio.sleep(5)
            if poly_storage.url:return   # the Postgres advisory lock already arbitrates ownership
            try:
                h=(await client.get(url+'/health',headers={'Cache-Control':'no-store'})).json()
                other,at=h.get('paperBootId'),float(h.get('paperBootAt') or 0)
                if other and other!=poly_server_engine.boot_id and at>poly_server_engine.boot_at:
                    await poly_server_engine.yield_to(other);return
            except Exception:pass

@app.on_event('startup')
async def start_successor_watch():
    url=(os.environ.get('POLY_KEEPALIVE_URL') or os.environ.get('POLY_HANDOVER_URL') or '').strip().rstrip('/')
    if url and POLY_ENGINE_ROLE!='frontend':asyncio.create_task(_successor_watch(url))

@app.get('/api/poly/research/rows')
async def research_rows(limit:int=4000):
    # Paper research labels for deploy handover; no credentials or account data.
    r=poly_server_engine.research
    rows=list(r.rows)[-max(1,min(12000,limit)):] if r else []
    return JSONResponse({'rows':rows,'count':len(rows)},headers={'Cache-Control':'no-store'})

@app.on_event('startup')
async def start_keepalive():
    url=os.environ.get('POLY_KEEPALIVE_URL','').strip().rstrip('/')
    if url and POLY_ENGINE_ROLE!='frontend':
        asyncio.create_task(_keepalive(url));print('POLY_KEEPALIVE_ENABLED',flush=True)

@app.get('/health')
async def health():return {'status':'online','chain':'solana-mainnet','wallet_private_keys_on_server':False,'jupiter_key_configured':bool(os.environ.get('JUPITER_API_KEY')),'jupiter_provider_selftest':jupiter_provider_selftest,'market_scan_selftest':market_scan_selftest,'market_live_status':market_live_status,'market_seq':market_seq,'jito_selftest':jito_selftest,'poly_selftest':poly_selftest,'poly_server_demo':poly_server_engine.feed,'poly_engine_role':POLY_ENGINE_ROLE,'atomic_bundle_execution':True,'paperBootId':poly_server_engine.boot_id,'paperBootAt':poly_server_engine.boot_at,'paperYielded':poly_server_engine.yielded}

async def external_call(method:str,url:str,*,headers=None,json_data=None,params=None):
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            response=await client.request(method,url,headers=headers,json=json_data,params=params)
        try: body=response.json()
        except ValueError: raise HTTPException(502,'External provider gave a non-JSON response')
        if response.status_code>=400:
            msg=(body.get('errorMessage') or body.get('error') or body.get('message') or 'Provider error') if isinstance(body,dict) else 'Provider error'
            raise HTTPException(502,str(msg)[:300])
        return body
    except httpx.RequestError as exc:
        raise HTTPException(503, f'Provider unavailable: {type(exc).__name__}')


async def external_raw(method:str,url:str,*,headers=None,json_data=None,params=None):
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            response=await client.request(method,url,headers=headers,json=json_data,params=params)
        try: body=response.json()
        except ValueError: raise HTTPException(502,'External provider gave a non-JSON response')
        if response.status_code>=400:
            msg=(body.get('errorMessage') or body.get('error') or body.get('message') or 'Provider error') if isinstance(body,dict) else 'Provider error'
            raise HTTPException(502,str(msg)[:300])
        return body,dict(response.headers)
    except httpx.RequestError as exc:
        raise HTTPException(503, f'Provider unavailable: {type(exc).__name__}')

def jup_key(request:Request):
    key=os.environ.get('JUPITER_API_KEY') or request.headers.get('x-jupiter-api-key','')
    if not key or len(key)>350 or any(ch.isspace() for ch in key):
        raise HTTPException(400,'Set your free Jupiter API key in the dashboard to access mainnet quotes.')
    return {'x-api-key':key,'Accept':'application/json'}

async def jupiter_order(headers, params):
    global last_jupiter_order
    async with jupiter_order_lock:
        delay = 1.05 - (time.monotonic() - last_jupiter_order)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await external_call('GET',JUPITER+'/order',headers=headers,params=params)
        finally:
            last_jupiter_order=time.monotonic()

@app.on_event('startup')
async def verify_jupiter_provider():
    global jupiter_provider_selftest
    key=os.environ.get('JUPITER_API_KEY')
    if not key:
        jupiter_provider_selftest='missing-key'
        print('JUPITER_SELFTEST_MISSING_KEY', flush=True)
        return
    params={'inputMint':SOL,'outputMint':USDC,'amount':'1000000','taker':'BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV'}
    try:
        result=await jupiter_order({'x-api-key':key,'Accept':'application/json'},params)
        if not isinstance(result,dict) or not result.get('outAmount'):
            raise RuntimeError('unexpected order response')
        jupiter_provider_selftest='ok'
        print('JUPITER_SELFTEST_OK', flush=True)

        global market_scan_selftest
        usd_raw=2_000_000
        scan_taker='BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV'
        seed=await jupiter_order({'x-api-key':key,'Accept':'application/json'},{'inputMint':USDC,'outputMint':SOL,'amount':str(usd_raw),'taker':scan_taker})
        sol_start=int(seed.get('outAmount') or 0)
        leg1=await jupiter_order({'x-api-key':key,'Accept':'application/json'},{'inputMint':SOL,'outputMint':USDC,'amount':str(sol_start),'taker':scan_taker})
        usdc_mid=int(leg1.get('outAmount') or 0)
        leg2=await jupiter_order({'x-api-key':key,'Accept':'application/json'},{'inputMint':USDC,'outputMint':SOL,'amount':str(usdc_mid),'taker':scan_taker})
        sol_return=int(leg2.get('outAmount') or 0)
        if min(sol_start,usdc_mid,sol_return)<=0:
            raise RuntimeError('invalid hypothetical scan amounts')
        market_scan_selftest='ok'
        print(f'MARKET_SCAN_SELFTEST_OK start={sol_start} mid={usdc_mid} return={sol_return}', flush=True)
    except Exception as exc:
        code=getattr(exc,'status_code',type(exc).__name__)
        if jupiter_provider_selftest!='ok':
            jupiter_provider_selftest=f'error:{code}'
            print(f'JUPITER_SELFTEST_FAILED {code}', flush=True)
        else:
            market_scan_selftest=f'error:{code}'
            print(f'MARKET_SCAN_SELFTEST_FAILED {code}', flush=True)


def _poly_jsonish(value):
    if isinstance(value,list): return value
    if not isinstance(value,str): return []
    try:
        parsed=json.loads(value)
        return parsed if isinstance(parsed,list) else []
    except Exception:
        return []

def _poly_best(book,side):
    rows=book.get(side) or []
    vals=[]
    for row in rows:
        try:
            vals.append((float(row.get('price')),float(row.get('size') or 0)))
        except Exception:
            pass
    if not vals:return {'price':None,'size':0}
    price=max(x[0] for x in vals) if side=='bids' else min(x[0] for x in vals)
    size=sum(x[1] for x in vals if x[0]==price)
    return {'price':round(price,6),'size':round(size,4)}

@app.on_event('startup')
async def verify_polymarket_feeds():
    global poly_selftest
    step='gamma'
    try:
        now=int(time.time());bucket=(now//300)*300;market=None
        for ts in [bucket,bucket+300,bucket-300]:
            data=await external_call('GET',POLY_GAMMA+'/markets',params={'slug':f'btc-updown-5m-{ts}'})
            if isinstance(data,list) and data:
                market=data[0]
                if ts==bucket: break
        if not market: raise RuntimeError('current market not found')
        outcomes=_poly_jsonish(market.get('outcomes'));tokens=_poly_jsonish(market.get('clobTokenIds'))
        if len(tokens)<2 or len(tokens)!=len(outcomes): raise RuntimeError('token mapping unavailable')
        step='clob'
        book=await external_call('GET',POLY_CLOB+'/book',params={'token_id':str(tokens[0])})
        if not isinstance(book,dict): raise RuntimeError('book unavailable')
        step='coinbase'
        hist=await external_call('GET',COINBASE_REST+'/products/BTC-USD/candles',headers={'User-Agent':'POLY-EDGE/1.0'},params={'granularity':'60'})
        if not isinstance(hist,list) or not hist: raise RuntimeError('BTC history unavailable')
        poly_selftest='ok'
        print(f"POLY_SELFTEST_OK slug={market.get('slug')} outcomes={outcomes}",flush=True)
    except Exception as exc:
        poly_selftest=f'error:{step}:{type(exc).__name__}'
        print(f"POLY_SELFTEST_FAILED step={step} type={type(exc).__name__} detail={getattr(exc,'detail','')}",flush=True)

@app.get('/api/poly/current')
async def poly_current():
    now=int(time.time())
    bucket=(now//300)*300
    candidates=[bucket,bucket+300,bucket-300]
    selected=None
    for ts in candidates:
        slug=f'btc-updown-5m-{ts}'
        try:
            data=await external_call('GET',POLY_GAMMA+'/markets',params={'slug':slug})
        except Exception:
            continue
        if not isinstance(data,list) or not data: continue
        m=data[0]
        if not isinstance(m,dict): continue
        if ts==bucket or (m.get('active') and not m.get('closed')):
            selected=(ts,m)
            if ts==bucket: break
    if not selected:
        raise HTTPException(404,'Current BTC 5-minute Polymarket market not found')
    start_ts,m=selected
    outcomes=_poly_jsonish(m.get('outcomes'))
    token_ids=_poly_jsonish(m.get('clobTokenIds'))
    if len(outcomes)!=len(token_ids) or len(token_ids)<2:
        raise HTTPException(502,'Polymarket market token mapping unavailable')
    mapping={str(o).strip().lower():str(t) for o,t in zip(outcomes,token_ids)}
    up=mapping.get('up') or mapping.get('yes')
    down=mapping.get('down') or mapping.get('no')
    if not up or not down:
        raise HTTPException(502,'Unable to map Up/Down Polymarket tokens')
    try:
        up_book,down_book=await asyncio.gather(
            external_call('GET',POLY_CLOB+'/book',params={'token_id':up}),
            external_call('GET',POLY_CLOB+'/book',params={'token_id':down})
        )
    except Exception:
        up_book={};down_book={}
    return {
        'status':'ok','slug':m.get('slug') or f'btc-updown-5m-{start_ts}',
        'question':m.get('question'),'conditionId':m.get('conditionId'),
        'startTs':start_ts,'endTs':start_ts+300,
        'active':bool(m.get('active')),'closed':bool(m.get('closed')),
        'acceptingOrders':bool(m.get('acceptingOrders')),
        'volume':float(m.get('volumeNum') or m.get('volume') or 0),
        'liquidity':float(m.get('liquidityNum') or m.get('liquidity') or 0),
        'minOrderSize':float(m.get('orderMinSize') or 0),
        'minTickSize':float(m.get('orderPriceMinTickSize') or 0.01),
        'upToken':up,'downToken':down,
        'up':{'bid':_poly_best(up_book,'bids'),'ask':_poly_best(up_book,'asks')},
        'down':{'bid':_poly_best(down_book,'bids'),'ask':_poly_best(down_book,'asks')},
        'resolutionSource':'Chainlink BTC/USD 60s TWAP'
    }

@app.get('/api/poly/btc-history')
async def poly_btc_history():
    data=await external_call('GET',COINBASE_REST+'/products/BTC-USD/candles',headers={'User-Agent':'POLY-EDGE/1.0'},params={'granularity':'60'})
    if not isinstance(data,list):
        raise HTTPException(502,'BTC history unavailable')
    out=[]
    for row in sorted(data,key=lambda x:x[0])[-120:]:
        try:
            out.append({'t':int(row[0])*1000,'o':float(row[3]),'h':float(row[2]),'l':float(row[1]),'c':float(row[4]),'v':float(row[5])})
        except Exception:
            pass
    closes=[x['c'] for x in out]
    rets=[]
    for a,b in zip(closes,closes[1:]):
        if a>0 and b>0: rets.append((b/a)-1)
    vol=(sum(r*r for r in rets)/len(rets))**0.5 if rets else 0
    return {'status':'ok','candles':out,'oneMinuteRmsPct':round(vol*100,5)}

class PolyDemoStart(BaseModel):
    startingBalance:float=Field(default=20.0,ge=0.5,le=10000)
    durationSeconds:int=Field(default=10800,ge=300,le=86400)

class PolyDemoEvent(BaseModel):
    sessionId:str=Field(min_length=8,max_length=64)
    eventType:str=Field(min_length=2,max_length=32)
    payload:dict=Field(default_factory=dict)

@app.post('/api/poly/demo/start')
async def poly_demo_start(body:PolyDemoStart):
    global poly_demo_latest
    now=time.time()
    sid='demo_'+uuid.uuid4().hex[:16]
    session={
        'sessionId':sid,'startedAt':now,'endsAt':now+body.durationSeconds,
        'startingBalance':round(body.startingBalance,6),
        'lastSeenAt':now,'events':[],'latestSnapshot':None,'status':'running'
    }
    poly_demo_sessions[sid]=session
    poly_demo_latest=sid
    # Keep memory bounded.
    if len(poly_demo_sessions)>20:
        oldest=sorted(poly_demo_sessions.values(),key=lambda x:x.get('startedAt',0))[:-20]
        for item in oldest: poly_demo_sessions.pop(item.get('sessionId',''),None)
    _save_demo_sessions()
    return {'status':'ok','sessionId':sid,'startedAt':now,'endsAt':session['endsAt'],'startingBalance':session['startingBalance']}

@app.post('/api/poly/demo/event')
async def poly_demo_event(body:PolyDemoEvent):
    session=poly_demo_sessions.get(body.sessionId)
    if not session: raise HTTPException(404,'Demo session not found')
    now=time.time()
    event={'t':now,'type':body.eventType,'payload':body.payload}
    session['events'].append(event)
    session['events']=session['events'][-5000:]
    session['lastSeenAt']=now
    if body.eventType=='SNAPSHOT':
        session['latestSnapshot']=body.payload
    if body.eventType in {'COMPLETE','STOP'}:
        session['status']='complete'
        session['completedAt']=now
    _save_demo_sessions()
    return {'status':'ok','events':len(session['events'])}

_snapshot_cache={}
@app.get('/api/poly/demo/server')
async def poly_demo_server(asset:Literal['BTC','ETH','BTC15','ETH15']='BTC',all_assets:bool=False):
    # Several open dashboard tabs each polling every couple of seconds rebuilt the full snapshot every
    # time; on the 0.1-CPU host that competed with the order-book feeds. Serve one build per second.
    key=(asset,all_assets);now=time.monotonic();hit=_snapshot_cache.get(key)
    if hit and now-hit[0]<1.0:return hit[1]
    snap=poly_storage.snapshot(asset,include_assets=all_assets);_snapshot_cache[key]=(now,snap)
    return snap

@app.post('/api/poly/demo/server/reset')
async def poly_demo_server_reset():
    if POLY_ENGINE_ROLE=='frontend':
        return JSONResponse({'status':'blocked','detail':'This service runs with POLY_ENGINE_ROLE=frontend; set it to combined to run the paper engine here.'},status_code=409)
    if poly_storage.url and not poly_storage.entry_allowed():
        return JSONResponse({'status':'blocked','detail':'Engine is in storage recovery or handover.'},status_code=409)
    result=await poly_server_engine.reset()
    if result.get('status')=='blocked':return JSONResponse(result,status_code=409)
    return poly_storage.snapshot(include_assets=True)

@app.get('/api/poly/demo/latest')
async def poly_demo_latest_report():
    if not poly_demo_latest or poly_demo_latest not in poly_demo_sessions:
        raise HTTPException(404,'No demo session yet')
    return poly_demo_sessions[poly_demo_latest]

@app.get('/api/poly/demo/{session_id}')
async def poly_demo_report(session_id:str):
    session=poly_demo_sessions.get(session_id)
    if not session: raise HTTPException(404,'Demo session not found')
    return session

class Order(BaseModel):
    inputMint:str
    outputMint:str
    amount:str
    taker:str|None=None
    slippageBps:int|None=Field(default=None,ge=0,le=500)

class HypotheticalScan(BaseModel):
    investmentUsd:float=Field(default=2.0,ge=0.5,le=25.0)

SCAN_TAKER='BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV'
SCAN_FEE_LAMPORTS=180_000
SCAN_MOVE_BUFFER_BPS=35

def _route_labels(order_response):
    labels=[]
    for step in (order_response.get('routePlan') or []):
        info=step.get('swapInfo') or step
        label=info.get('label') or info.get('ammLabel') or info.get('dex') or ''
        if label and label not in labels: labels.append(label)
    return labels

def _rolling_edge_stats(current_edge_bps:float):
    vals=[float(x.get('conservativeEdgeBps') or 0) for x in list(market_history_live)[:180]]
    if not vals:
        return {'edgeMeanBps':0.0,'edgeStdBps':0.0,'edgeZ':0.0,'positiveRatePct':0.0,'persistence':0}
    mean=sum(vals)/len(vals)
    variance=sum((v-mean)**2 for v in vals)/len(vals)
    std=variance**0.5
    z=(current_edge_bps-mean)/std if std>1e-9 else 0.0
    pos=sum(1 for v in vals if v>0)/len(vals)*100
    persistence=0
    for v in vals:
        if v>0:persistence+=1
        else:break
    return {'edgeMeanBps':round(mean,3),'edgeStdBps':round(std,3),'edgeZ':round(z,3),'positiveRatePct':round(pos,1),'persistence':persistence}

def _history_context(current_price:float,leg1_labels:list[str],leg2_labels:list[str]):
    prior=list(market_history_live)
    prices=[float(x.get('solPriceUsd') or 0) for x in prior if float(x.get('solPriceUsd') or 0)>0][:180]
    window=prices[:60]
    if window:
        mean=sum(window)/len(window)
        variance=sum((p-mean)**2 for p in window)/len(window)
        std=variance**0.5
        price_z=(current_price-mean)/std if std>1e-9 else 0.0
    else:
        price_z=0.0
    ref=prices[min(11,len(prices)-1)] if prices else current_price
    short_momentum_bps=((current_price/ref)-1)*10_000 if ref else 0.0

    current_route=tuple(leg1_labels+['→']+leg2_labels)
    prev_route=()
    if prior:
        prev_route=tuple((prior[0].get('leg1Venues') or [])+['→']+(prior[0].get('leg2Venues') or []))
    recent_labels=set(leg1_labels+leg2_labels)
    for item in prior[:30]:
        recent_labels.update(item.get('leg1Venues') or [])
        recent_labels.update(item.get('leg2Venues') or [])
    return {
        'priceZ':round(price_z,3),
        'shortMomentumBps':round(short_momentum_bps,3),
        'routeChanged':bool(prev_route and current_route!=prev_route),
        'routeVenueCount':len(recent_labels),
        'routeVenues':sorted(recent_labels),
        'historySamples':len(prior)
    }

async def calculate_hypothetical(investment_usd:float=2.0):
    key=os.environ.get('JUPITER_API_KEY')
    if not key:
        raise HTTPException(503,'Jupiter API key is not configured')
    headers={'x-api-key':key,'Accept':'application/json'}
    usd_raw=max(500_000,min(25_000_000,round(investment_usd*1_000_000)))

    seed=await jupiter_order(headers,{
        'inputMint':USDC,'outputMint':SOL,'amount':str(usd_raw),'taker':SCAN_TAKER
    })
    sol_start=int(seed.get('outAmount') or 0)
    if sol_start<=0: raise HTTPException(502,'No usable USDC to SOL reference quote')

    leg1=await jupiter_order(headers,{
        'inputMint':SOL,'outputMint':USDC,'amount':str(sol_start),'taker':SCAN_TAKER
    })
    usdc_mid=int(leg1.get('outAmount') or 0)
    if usdc_mid<=0: raise HTTPException(502,'No usable SOL to USDC quote')

    leg2=await jupiter_order(headers,{
        'inputMint':USDC,'outputMint':SOL,'amount':str(usdc_mid),'taker':SCAN_TAKER
    })
    sol_return=int(leg2.get('outAmount') or 0)
    if sol_return<=0: raise HTTPException(502,'No usable USDC to SOL return quote')

    investment=float(usd_raw)/1_000_000
    sol_start_ui=sol_start/1_000_000_000
    sol_return_ui=sol_return/1_000_000_000
    sol_price=(investment/sol_start_ui) if sol_start_ui>0 else 0.0
    raw_profit_sol=sol_return_ui-sol_start_ui
    raw_profit_usd=raw_profit_sol*sol_price
    raw_edge_bps=(raw_profit_usd/investment*10_000) if investment else 0.0
    fee_allowance_usd=(SCAN_FEE_LAMPORTS/1_000_000_000)*sol_price
    move_buffer_usd=investment*(SCAN_MOVE_BUFFER_BPS/10_000)
    conservative_profit_usd=raw_profit_usd-fee_allowance_usd-move_buffer_usd
    conservative_edge_bps=(conservative_profit_usd/investment*10_000) if investment else 0.0
    leg1_labels=_route_labels(leg1)
    leg2_labels=_route_labels(leg2)
    hist=_rolling_edge_stats(conservative_edge_bps)
    ctx=_history_context(sol_price,leg1_labels,leg2_labels)
    dislocation=max(0.0,min(100.0,
        max(0.0,hist['edgeZ'])*22.0 +
        min(abs(ctx['priceZ']),4.0)*8.0 +
        (8.0 if ctx['routeChanged'] else 0.0) +
        min(ctx['routeVenueCount'],5)*2.0
    ))
    confidence=max(5.0,min(95.0,50.0+conservative_edge_bps*0.45 + dislocation*0.06))
    verdict='EXECUTE-CANDIDATE' if conservative_profit_usd>0.001 else ('WATCH' if raw_profit_usd>0 else 'REJECT')

    return {
        'status':'ok','timestamp':time.time(),'investmentUsd':round(investment,6),
        'solPriceUsd':round(sol_price,6),'startSol':sol_start,'startSolUi':round(sol_start_ui,9),
        'midUsdc':usdc_mid,'midUsdcUi':round(usdc_mid/1_000_000,6),
        'returnSol':sol_return,'returnSolUi':round(sol_return_ui,9),
        'rawProfitUsd':round(raw_profit_usd,8),'rawEdgeBps':round(raw_edge_bps,3),
        'feeAllowanceUsd':round(fee_allowance_usd,8),'moveBufferUsd':round(move_buffer_usd,8),
        'conservativeProfitUsd':round(conservative_profit_usd,8),
        'conservativeEdgeBps':round(conservative_edge_bps,3),
        'projectedReturnUsd':round(investment+conservative_profit_usd,8),
        'confidence':round(confidence,1),'confidenceType':'heuristic-not-statistical',
        'verdict':verdict,'route':'SOL → USDC → SOL','router':'Jupiter Swap V2',
        'leg1Venues':leg1_labels,'leg2Venues':leg2_labels,
        'edgeMeanBps':hist['edgeMeanBps'],'edgeStdBps':hist['edgeStdBps'],'edgeZ':hist['edgeZ'],
        'positiveRatePct':hist['positiveRatePct'],'persistence':hist['persistence'],
        'dislocationScore':round(dislocation,1),'priceZ':ctx['priceZ'],
        'shortMomentumBps':ctx['shortMomentumBps'],'routeChanged':ctx['routeChanged'],
        'routeVenueCount':ctx['routeVenueCount'],'routeVenues':ctx['routeVenues'],
        'historySamples':ctx['historySamples']
    }

async def continuous_market_worker():
    global market_latest,market_live_status,market_seq
    while True:
        try:
            market_live_status='scanning'
            item=await calculate_hypothetical(2.0)
            market_seq+=1
            item['seq']=market_seq
            item['serverMode']='continuous'
            market_latest=item
            market_history_live.appendleft(item)
            market_live_status='live'
            print(f"LIVE_SCAN seq={market_seq} verdict={item['verdict']} pnl={item['conservativeProfitUsd']}",flush=True)
        except Exception as exc:
            market_live_status='error'
            print(f"LIVE_SCAN_ERROR {type(exc).__name__}",flush=True)
        await asyncio.sleep(0.15)

@app.on_event('startup')
async def start_market_worker():
    # Legacy Solana route scanner (home page). Three Jupiter quotes per ~10 s scan, every scan a
    # REJECT, sharing the 0.1-CPU host with the Polymarket feeds. Opt-in: ARB_LIVE_SCAN=1.
    global market_live_status
    if os.getenv('ARB_LIVE_SCAN','0')=='1':asyncio.create_task(continuous_market_worker())
    else:market_live_status='disabled (set ARB_LIVE_SCAN=1 to run the Solana scanner)';print('ARB_LIVE_SCAN_DISABLED',flush=True)

_gc_last={'gen2Sec':0.,'at':0.}
def _gc_timer(phase,info):
    if info.get('generation')!=2:return
    if phase=='start':_gc_last['t0']=time.perf_counter()
    else:_gc_last['gen2Sec']=time.perf_counter()-_gc_last.get('t0',time.perf_counter());_gc_last['at']=time.time()

async def _loop_watchdog():
    """Logs event-loop freezes. Lanes resyncing their order-book sockets at the same instant pointed
    to whole-process stalls; this names how long they are and the last full-GC time."""
    import gc
    gc.callbacks.append(_gc_timer)
    await asyncio.sleep(90)
    gc.collect();gc.freeze()   # long-lived restored state stops being rescanned by every full GC
    print('POLY_GC_FROZEN',gc.get_freeze_count(),flush=True)
    last_log=0.
    while True:
        t=time.monotonic();await asyncio.sleep(.5);lag=time.monotonic()-t-.5
        if lag>1.0 and time.monotonic()-last_log>=30:
            last_log=time.monotonic()
            print(f"POLY_LOOP_STALL {lag:.2f}s lastGen2Gc={_gc_last['gen2Sec']:.2f}s gen2Age={time.time()-_gc_last['at'] if _gc_last['at'] else -1:.0f}s",flush=True)

@app.on_event('startup')
async def start_loop_watchdog():
    asyncio.create_task(_loop_watchdog())
    if os.getenv('POLY_SAMPLER','1')!='0':
        import poly_sampler;poly_sampler.Sampler().start();print('POLY_SAMPLER_ON',flush=True)

@app.on_event('startup')
async def verify_jito_block_engine():
    global jito_selftest
    try:
        body=await external_call('POST',JITO+'/api/v1/getTipAccounts',json_data={'jsonrpc':'2.0','id':1,'method':'getTipAccounts','params':[]})
        accounts=body.get('result') or []
        if not accounts: raise RuntimeError('no tip accounts')
        jito_selftest='ok'
        print(f'JITO_SELFTEST_OK accounts={len(accounts)}',flush=True)
    except Exception as exc:
        jito_selftest=f'error:{type(exc).__name__}'
        print(f'JITO_SELFTEST_FAILED {type(exc).__name__}',flush=True)

@app.get('/api/live-market')
async def live_market():
    return {
        'status':market_live_status,
        'latest':market_latest,
        'history':list(market_history_live)[:24],
        'scanInterval':'continuous-provider-limited',
        'providerLimit':'Jupiter requests serialized to ~1 request/second',
        'signalSource':'executable-route-history'
    }

@app.post('/api/hypothetical')
async def hypothetical(payload:HypotheticalScan,request:Request):
    return await calculate_hypothetical(payload.investmentUsd)

@app.post('/api/order')
async def order(input:Order, request:Request):
    if {input.inputMint,input.outputMint}!={SOL,USDC} or input.inputMint==input.outputMint:
        raise HTTPException(400,'Only SOL / USDC swap quotes are permitted')
    if not re.fullmatch(r'[0-9]{1,14}',input.amount) or not 1000<=int(input.amount)<=1_000_000_000:
        raise HTTPException(400,'Swap amount outside supported bounds')
    if input.taker and not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',input.taker):
        raise HTTPException(400,'Invalid Solana public wallet address')
    params={'inputMint':input.inputMint,'outputMint':input.outputMint,'amount':input.amount}
    if input.taker: params['taker']=input.taker
    if input.slippageBps is not None: params['slippageBps']=str(input.slippageBps)
    return await jupiter_order(jup_key(request),params)

class Execute(BaseModel):
    signedTransaction:str=Field(min_length=80,max_length=16000)
    requestId:str=Field(min_length=1,max_length=256)

@app.post('/api/execute')
async def execute(payload:Execute,request:Request):
    # Signed transactions have already been authorized by a key that NEVER leaves the browser.
    if not re.fullmatch(r'[A-Za-z0-9+/=]+',payload.signedTransaction):
        raise HTTPException(400,'Invalid base64 signed transaction')
    return await external_call('POST',JUPITER+'/execute',headers={**jup_key(request),'Content-Type':'application/json'},json_data=payload.model_dump())

class JitoBundle(BaseModel):
    transactions:list[str]=Field(min_length=2,max_length=5)

class JitoStatus(BaseModel):
    bundleId:str=Field(min_length=16,max_length=128)

@app.get('/api/jito/tip-accounts')
async def jito_tip_accounts():
    body=await external_call('POST',JITO+'/api/v1/getTipAccounts',json_data={'jsonrpc':'2.0','id':1,'method':'getTipAccounts','params':[]})
    return {'accounts':body.get('result') or []}

@app.post('/api/jito/bundle')
async def jito_bundle(payload:JitoBundle):
    for tx in payload.transactions:
        if not 80<=len(tx)<=16000 or not re.fullmatch(r'[A-Za-z0-9+/=]+',tx):
            raise HTTPException(400,'Invalid signed base64 transaction in bundle')
    body,headers=await external_raw('POST',JITO+'/api/v1/bundles',json_data={
        'jsonrpc':'2.0','id':1,'method':'sendBundle','params':[payload.transactions,{'encoding':'base64'}]
    })
    result=body.get('result')
    if not result: raise HTTPException(502,'Jito did not return a bundle id')
    return {'bundleId':result,'headerBundleId':headers.get('x-bundle-id',''),'region':'singapore'}

@app.post('/api/jito/status')
async def jito_status(payload:JitoStatus):
    body=await external_call('POST',JITO+'/api/v1/getBundleStatuses',json_data={
        'jsonrpc':'2.0','id':1,'method':'getBundleStatuses','params':[[payload.bundleId]]
    })
    return body.get('result') or {'value':[None]}

class Rpc(BaseModel):
    method:str
    params:list

ALLOWED={'getBalance','getTokenAccountsByOwner','getLatestBlockhash','getSignatureStatuses','getAccountInfo','sendTransaction'}
@app.post('/api/rpc')
async def rpc(body:Rpc):
    if body.method not in ALLOWED:raise HTTPException(400,'RPC method not permitted')
    if len(str(body.params))>18000:raise HTTPException(400,'RPC parameters too large')
    data=await external_call('POST',RPC,json_data={'jsonrpc':'2.0','id':1,'method':body.method,'params':body.params})
    if isinstance(data,dict) and 'error' in data:
        raise HTTPException(502,str(data['error'])[:250])
    return data.get('result')
@app.get("/api/poly/demo/diagnostics/status")
async def poly_demo_diagnostic_status(prepare:bool=False,job:str|None=None):
    from poly_audit_export import downloads
    return JSONResponse(downloads.status(poly_server_engine,prepare,job),headers={'Cache-Control':'no-store'})

@app.get("/api/poly/demo/diagnostics")
async def poly_demo_diagnostics(job:str|None=None):
    from poly_audit_export import downloads
    return downloads.response(poly_server_engine,job)

@app.get("/api/poly/strategy/config")
async def get_strategy_config():
    return poly_server_engine.get_config()

@app.get("/api/poly/performance")
async def get_performance_summary():
    return poly_server_engine.get_performance()

@app.post("/api/poly/strategy/config")
async def update_strategy_config(request:Request):
    try:body=await request.json()
    except Exception:raise HTTPException(400,'Invalid JSON body')
    return poly_server_engine.update_config(body if isinstance(body,dict) else {})

@app.get("/api/poly/research/summary")
async def get_research_summary():
    from poly_pair_shadow import wallet_research_summary
    return wallet_research_summary()


@app.on_event("shutdown")
async def close_poly_research():
    if _tape is not None:   # keep the trade tape's open rounds and totals across a restart
        try:_tape.save_state()
        except Exception as exc:print('POLY_TAPE_SAVE_ERROR',type(exc).__name__,flush=True)
    await poly_storage.close()

@app.get('/api/poly/operations')
async def poly_operations():
    return {'storage':poly_storage.status(),'attribution':poly_server_engine.attribution.summary(),'executionMode':'PAPER_ONLY'}

@app.get("/api/poly/research/v16")
async def poly_v16_research():
    return poly_server_engine.research.snapshot() if poly_server_engine.research else {"mode":"SHADOW_ONLY","executionEnabled":False,"state":"starting"}


# ---- developer logs: plain text, no ZIP, viewable in any browser ----
@app.get('/robots.txt')
async def robots():
    return PlainTextResponse('User-agent: *\nAllow: /\n')

@app.get('/api/poly/dev/report')
async def dev_report(format:str='text'):
    import poly_devlog
    rep=poly_devlog.report(poly_server_engine)
    if format=='json':return JSONResponse(rep,headers={'Cache-Control':'no-store'})
    return PlainTextResponse(poly_devlog.text(rep),headers={'Cache-Control':'no-store'})

@app.get('/api/poly/scout')
async def poly_scout():
    return JSONResponse(poly_server_engine.scout_snapshot(),headers={'Cache-Control':'no-store'})

@app.get('/api/poly/dev/report.txt')
async def dev_report_download():
    import poly_devlog
    body=poly_devlog.text(poly_devlog.report(poly_server_engine))
    name='poly-edge-report-'+time.strftime('%Y-%m-%dT%H-%M-%SZ',time.gmtime())+'.txt'
    return PlainTextResponse(body,headers={'Cache-Control':'no-store','Content-Disposition':f'attachment; filename="{name}"'})

@app.get('/api/poly/dev/events')
async def dev_events(asset:str|None=None,limit:int=300,action:str|None=None):
    import poly_devlog
    rows=poly_devlog.events(poly_server_engine,asset,limit,action)
    return PlainTextResponse(''.join(json.dumps(r,separators=(',',':'),default=str)+'\n' for r in rows),headers={'Cache-Control':'no-store'})

@app.get('/api/poly/dev/edgebets.csv')
async def dev_edge_bets_csv():
    import poly_devlog
    return PlainTextResponse(poly_devlog.edge_bets_csv(poly_server_engine.edge),headers={'Cache-Control':'no-store'})

@app.get('/api/poly/dev/trades.csv')
async def dev_trades_csv():
    import poly_devlog
    return PlainTextResponse(poly_devlog.trades_csv(poly_devlog.trade_rows(poly_server_engine)),headers={'Cache-Control':'no-store'})

async def _print_trade_history():
    import poly_devlog
    await asyncio.sleep(20)
    try:
        rows=poly_devlog.trade_rows(poly_server_engine)
        for line in poly_devlog.trades_csv(rows).splitlines():print('POLY_TRADE_CSV',line,flush=True)
        for line in poly_devlog.edge_bets_csv(poly_server_engine.edge).splitlines():print('POLY_EDGEBET_CSV',line,flush=True)
    except Exception as exc:print('POLY_TRADE_CSV_ERROR',type(exc).__name__,flush=True)

async def _dev_report_printer():
    import poly_devlog
    while True:
        await asyncio.sleep(300)
        try:
            for line in poly_devlog.compact_lines(poly_devlog.report(poly_server_engine)):print(line,flush=True)
        except Exception as exc:print('POLY_DEV_REPORT_ERROR',type(exc).__name__,str(exc)[:160],flush=True)

@app.on_event('startup')
async def start_dev_report_printer():
    if POLY_ENGINE_ROLE!='frontend':asyncio.create_task(_dev_report_printer());asyncio.create_task(_print_trade_history())

_audit_cache={'at':0.,'report':None}
async def _audit(force=False):
    """Research audit (poly_audit): computed in a worker thread so the bootstrap never blocks the feeds."""
    import poly_audit
    if force or _audit_cache['report'] is None or time.time()-_audit_cache['at']>600:
        _audit_cache['report']=await asyncio.to_thread(poly_audit.analyse,poly_audit.gather(poly_server_engine));_audit_cache['at']=time.time()
    return _audit_cache['report']

@app.get("/api/poly/audit")
async def poly_audit_endpoint():
    rep=dict(await _audit())
    rep['threshold']=_threshold.compact() if _threshold else None
    rep['tape']=_tape.compact() if _tape else None
    try:rep['skillVsLuck']={k:v for k,v in poly_server_engine.edge.intel.skill_vs_luck().items() if k!='per'}
    except Exception:rep['skillVsLuck']=None
    return rep

@app.get("/api/poly/baseline")
async def poly_baseline_endpoint():
    """The frozen 2026-10-25 baseline (stored once in the database) and the strategy identity running now."""
    import poly_strategy
    st=getattr(poly_server_engine,'storage',None);b=getattr(st,'baseline',None)
    return {'running':poly_strategy.identity(),'baseline':b,'stored':b is not None}

def _baseline_payload(rep):
    """Frozen baseline: identity, code revision, the full trade history and the audit at freeze time."""
    import poly_strategy,poly_devlog
    return {'identity':poly_strategy.identity(),'renderCommit':os.getenv('RENDER_GIT_COMMIT'),'capturedAt':time.time(),
            'trades':poly_devlog.trade_rows(poly_server_engine),'attribution':list(poly_server_engine.attribution.rows),
            'audit':{k:rep.get(k) for k in ('verdict','walkForward','liveRules','execution','ledger','modelVsMarket','stopForensics')}}

async def _audit_printer():
    import poly_audit
    await asyncio.sleep(240)
    while True:
        try:
            rep=await _audit(force=True)
            st=getattr(poly_server_engine,'storage',None)
            if st is not None and st.baseline is None and st.pending_baseline is None and st.active:st.pending_baseline=_baseline_payload(rep)
            print('POLY_AUDIT '+json.dumps(poly_audit.compact(rep),separators=(',',':'),default=str),flush=True)
        except Exception as exc:print('POLY_AUDIT_ERROR',type(exc).__name__,str(exc)[:120],flush=True)
        await asyncio.sleep(3600)

@app.on_event('startup')
async def start_audit_printer():
    if POLY_ENGINE_ROLE!='frontend':asyncio.create_task(_audit_printer())

_status_tee=None
@app.on_event('startup')
async def start_status_report():
    """POLY_STATUS_GH_TOKEN set (e.g. on EC2): publish health/audit snapshots to a GitHub branch."""
    global _status_tee
    token=os.getenv('POLY_STATUS_GH_TOKEN','')
    if not token or POLY_ENGINE_ROLE=='frontend':return
    import poly_status_report as sr,poly_audit
    _status_tee=sr.install_tee()
    pub=sr.GitHubPublisher(token,os.getenv('POLY_STATUS_REPO','Phaviwylera/polymarketbot'),os.getenv('POLY_STATUS_BRANCH','bot-status'))
    async def compact():return poly_audit.compact(await _audit())
    asyncio.create_task(sr.run(poly_server_engine,compact,_status_tee,time.time(),pub))
    print('POLY_STATUS_REPORT_ON',flush=True)

_bullpen=None
@app.on_event('startup')
async def start_bullpen():
    """Read-only Bullpen wallet analytics, recorded for research only (POLY_BULLPEN=0 disables)."""
    global _bullpen
    if os.getenv('POLY_BULLPEN','1')=='0' or POLY_ENGINE_ROLE=='frontend':return
    import poly_bullpen
    _bullpen=poly_bullpen.Bullpen();asyncio.create_task(_bullpen.loop(poly_server_engine));print('POLY_BULLPEN_ON',flush=True)

_threshold=None
@app.on_event('startup')
async def start_threshold_shadow():
    """Paper-only 'above $X' probability-model shadow lane (POLY_THRESHOLD=0 disables)."""
    global _threshold
    if os.getenv('POLY_THRESHOLD','1')=='0' or POLY_ENGINE_ROLE=='frontend':return
    import poly_threshold
    _threshold=poly_threshold.ThresholdLane();asyncio.create_task(_threshold.loop(poly_server_engine));print('POLY_THRESHOLD_ON',flush=True)

@app.get("/api/poly/threshold")
async def poly_threshold_report():
    return _threshold.report() if _threshold else {'enabled':False}

_tape=None;_history=None
@app.on_event('startup')
async def start_trade_tape():
    """Score every Polymarket trade print with the model, labelled at resolution (POLY_TAPE=0 disables)."""
    global _tape
    if os.getenv('POLY_TAPE','1')=='0' or POLY_ENGINE_ROLE=='frontend':return
    import poly_tape
    s3=None
    if os.getenv('POLY_BACKUP_S3_BUCKET'):
        import poly_s3backup;s3=poly_s3backup.S3(os.getenv('POLY_BACKUP_S3_BUCKET'))
    _tape=poly_tape.Tape(s3=s3,learner=poly_server_engine.edge.learner)
    import poly_study;_tape.study=poly_study.Study();poly_server_engine.edge.study=_tape.study
    global _history
    if os.getenv('POLY_HISTORY','1')!='0':   # weeks of past rounds from Polymarket, studied separately from live
        import poly_history;_history=poly_history.History(poly_study.Study('history'),s3=s3)
        asyncio.create_task(_history.loop(poly_server_engine));print('POLY_HISTORY_ON',flush=True)
    for lane in poly_server_engine.engines.values():lane.tape=_tape
    edge=poly_server_engine.edge
    if not hasattr(edge,'settle_listeners'):edge.settle_listeners=[]
    edge.settle_listeners.append(_tape.settle)
    asyncio.create_task(_tape.loop());print('POLY_TAPE_ON',flush=True)
    import poly_jev;jev=poly_jev.Jev()            # record-only; on only when OPENROUTER_API_KEY is set on the server
    if jev.enabled:
        _tape.jev=jev;edge.settle_listeners.append(jev.settle);asyncio.create_task(jev.loop());print('POLY_JEV_ON',flush=True)
    else:poly_jev.CURRENT=jev

@app.get("/api/poly/jev")
async def poly_jev_report():
    import poly_jev
    return poly_jev.CURRENT.report() if poly_jev.CURRENT else {'enabled':False}

@app.get("/api/poly/tape")
async def poly_tape_report():
    return _tape.report() if _tape else {'enabled':False}

@app.get("/api/poly/study")
async def poly_study_snapshot(source:str='live'):
    if source=='history':
        if not _history:return {'enabled':False}
        snap=_history.study.snapshot(None,None);snap['history']=_history.snapshot();return snap
    if not _tape or _tape.study is None:return {'enabled':False}
    snap=_tape.study.snapshot(_tape,_tape.learner)
    if _history:snap['history']=_history.snapshot()
    return snap

@app.get('/poly-skin.css')
async def poly_skin_css():return FileResponse(ROOT/'poly-skin.css',media_type='text/css',headers={'Cache-Control':'no-store'})

@app.get('/study')
async def study_page():return FileResponse(ROOT/'poly-study.html',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@app.get("/api/poly/bullpen")
async def poly_bullpen_status():
    return _bullpen.state if _bullpen else {'enabled':False}

@app.get("/api/poly/edge")
async def poly_edge():
    return poly_server_engine.edge.snapshot(200.,since=getattr(poly_server_engine,'epoch',None) or None)

@app.get("/api/poly/edge/state")
async def poly_edge_state():
    # Paper research state used for live handover between deploys; contains no credentials.
    return JSONResponse(poly_server_engine.edge.export(),headers={'Cache-Control':'no-store'})

@app.get("/api/poly/readiness")
async def poly_readiness():
    return poly_server_engine.edge.readiness(200.)

@app.get("/api/poly/research/v16/model")
async def poly_v16_model():
    return poly_server_engine.research.artifact if poly_server_engine.research and poly_server_engine.research.artifact else {"model":None,"executionEnabled":False,"state":"collecting_data"}


@app.get("/api/poly/research/pairs")
async def pair_research():
    return poly_server_engine.research.pair_snapshot() if poly_server_engine.research else {"mode":"SHADOW_ONLY","executionEnabled":False,"state":"starting"}
