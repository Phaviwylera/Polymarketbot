from __future__ import annotations
from typing import Literal
import time
from collections import defaultdict, deque
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from poly_portfolio import engine
from poly_storage import DurableStore
storage=DurableStore(engine)

app=FastAPI(title="POLY/EDGE Paper Engine",docs_url=None,redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

requests_by_ip=defaultdict(deque)

@app.middleware("http")
async def guard(request:Request,call_next):
    if request.url.path.startswith("/api/"):
        ip=request.client.host if request.client else "unknown"
        now=time.monotonic();hits=requests_by_ip[ip]
        while hits and hits[0]<now-60:hits.popleft()
        if len(hits)>=900:
            return JSONResponse(
                {"detail":"Rate limit reached. Automatic backoff active.","retryAfterSeconds":2},
                status_code=429,headers={"Retry-After":"2"}
            )
        hits.append(now)
    return await call_next(request)

@app.on_event("startup")
async def start_engine():
    await storage.start()
    print("POLY_DEDICATED_ENGINE_STARTED",flush=True)

@app.get("/health")
async def health():
    snap=storage.snapshot()
    return {
        "status":"online",
        "role":"dedicated-paper-engine",
        "revision":snap.get("revision"),
        "portfolio":snap.get("portfolio"),
        "feed":snap.get("feed"),
        "market":(snap.get("market") or {}).get("slug"),
        "openTrusted":snap.get("openTrusted"),
        "sessionId":(snap.get("paper") or {}).get("sessionId"),
    }

@app.get("/api/poly/demo/server")
async def snapshot(asset:Literal['BTC','ETH','SOL','XRP']='BTC',all_assets:bool=False):
    return storage.snapshot(asset,include_assets=all_assets)

@app.get("/api/poly/demo/diagnostics/status")
async def diagnostic_status(prepare:bool=False,job:str|None=None):
    from poly_audit_export import downloads
    return JSONResponse(downloads.status(engine,prepare,job),headers={'Cache-Control':'no-store'})

@app.get("/api/poly/demo/diagnostics")
async def diagnostics(job:str|None=None):
    from poly_audit_export import downloads
    return downloads.response(engine,job)

@app.get("/api/poly/strategy/config")
async def get_strategy_config():
    return engine.get_config()

@app.get("/api/poly/performance")
async def get_performance_summary():
    return engine.get_performance()

@app.post("/api/poly/strategy/config")
async def update_strategy_config(request:Request):
    if storage.url and not storage.entry_allowed():return JSONResponse({'error':'Engine is in storage recovery or handover'},status_code=409)
    try:body=await request.json()
    except Exception:return JSONResponse({"error":"Invalid JSON"},status_code=400)
    return engine.update_config(body if isinstance(body,dict) else {})

@app.get("/api/poly/research/summary")
async def get_research_summary():
    from poly_pair_shadow import wallet_research_summary
    return wallet_research_summary()


@app.on_event("shutdown")
async def stop_engine():
    await storage.close()

@app.get('/api/poly/operations')
async def operations():
    return {'storage':storage.status(),'attribution':engine.attribution.summary(),'executionMode':'PAPER_ONLY'}

@app.get("/api/poly/research/v16")
async def v16_research():
    return engine.research.snapshot() if engine.research else {"mode":"SHADOW_ONLY","executionEnabled":False,"state":"starting"}

@app.get("/api/poly/research/v16/model")
async def v16_model():
    return engine.research.artifact if engine.research and engine.research.artifact else {"model":None,"executionEnabled":False,"state":"collecting_data"}


@app.get("/api/poly/research/pairs")
async def pair_research():
    return engine.research.pair_snapshot() if engine.research else {"mode":"SHADOW_ONLY","executionEnabled":False,"state":"starting"}
