from __future__ import annotations
from typing import Literal
import time
from collections import defaultdict, deque
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from poly_portfolio import engine

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
    await engine.start()
    print("POLY_DEDICATED_ENGINE_STARTED",flush=True)

@app.get("/health")
async def health():
    snap=engine.snapshot()
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
    return engine.snapshot(asset,include_assets=all_assets)

@app.get("/api/poly/demo/diagnostics")
async def diagnostics():
    from poly_audit_export import diagnostic_response
    return await diagnostic_response(engine)

@app.get("/api/poly/strategy/config")
async def get_strategy_config():
    return engine.get_config()

@app.post("/api/poly/strategy/config")
async def update_strategy_config(request:Request):
    try:body=await request.json()
    except Exception:return JSONResponse({"error":"Invalid JSON"},status_code=400)
    return engine.update_config(body if isinstance(body,dict) else {})

@app.get("/api/poly/research/summary")
async def get_research_summary():
    return {
        "status":"active",
        "targetWallet":"0x31393e2f8333d4a6e2e31ca6424defe8b6cb9e2e",
        "reconciliation":{"feeModel":"dynamic-price-decay","cryptoTakerFeePct":7.0,"makerFeePct":0.0,"latencyDecayHalfLifeSec":1.2},
        "findings":[
            "Taker-only copying in 5-minute markets suffers negative edge from ~7% dynamic price decay fees.",
            "Passive first-leg maker quoting preserves edge, but requires strict queue consumption before filling.",
            "Immediate complement hedging is vital: unhedged residual exposure past 15s triggers aggressive exit stop.",
            "Hard session loss gate ($6.00 / 3%) prevents catastrophic drawdown during market shifts."
        ],
        "workstationMode":"PAPER_MULTI_ASSET_STAGE_PAIRING"
    }
