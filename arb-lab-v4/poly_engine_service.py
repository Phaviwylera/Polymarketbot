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
    allow_origins=["https://arb-lab-v4-unified.onrender.com"],
    allow_credentials=False,
    allow_methods=["GET","OPTIONS"],
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
async def snapshot(asset:Literal['BTC','ETH','SOL','XRP']='BTC'):
    return engine.snapshot(asset)
