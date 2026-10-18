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
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from poly_portfolio import engine as poly_server_engine

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
async def poly_home():return FileResponse(ROOT/'poly.html',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@app.get('/poly.js')
async def poly_js():return FileResponse(ROOT/'poly.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

@app.get('/poly-orb.js')
async def poly_orb_js():return FileResponse(ROOT/'poly-orb.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})

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
    await poly_server_engine.start()
    print(f'POLY_SERVER_DEMO_STARTED role={POLY_ENGINE_ROLE}',flush=True)

@app.get('/health')
async def health():return {'status':'online','chain':'solana-mainnet','wallet_private_keys_on_server':False,'jupiter_key_configured':bool(os.environ.get('JUPITER_API_KEY')),'jupiter_provider_selftest':jupiter_provider_selftest,'market_scan_selftest':market_scan_selftest,'market_live_status':market_live_status,'market_seq':market_seq,'jito_selftest':jito_selftest,'poly_selftest':poly_selftest,'poly_server_demo':poly_server_engine.feed,'poly_engine_role':POLY_ENGINE_ROLE,'atomic_bundle_execution':True}

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

@app.get('/api/poly/demo/server')
async def poly_demo_server(asset:Literal['BTC','ETH','SOL','XRP']='BTC',all_assets:bool=False):
    return poly_server_engine.snapshot(asset,include_assets=all_assets)

@app.post('/api/poly/demo/server/reset')
async def poly_demo_server_reset():
    return await poly_server_engine.reset()

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
    asyncio.create_task(continuous_market_worker())

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
@app.get("/api/poly/demo/diagnostics")
async def poly_demo_diagnostics():
    from poly_audit_export import diagnostic_response
    return diagnostic_response(poly_server_engine)
