from __future__ import annotations
import asyncio
import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from wallet_strategy_research import WalletResearch, TARGET_WALLET

app=FastAPI(title="POLY Wallet Research",docs_url=None,redoc_url=None)
app.add_middleware(CORSMiddleware,allow_origins=["https://arb-lab-v4-unified.onrender.com"],allow_methods=["GET","POST","OPTIONS"],allow_headers=["*"])
STATE={"runner":None,"task":None,"wallet":TARGET_WALLET}

async def launch(wallet=TARGET_WALLET,copy=True):
    if STATE.get("task") and not STATE["task"].done():return
    r=WalletResearch(wallet);STATE["runner"]=r;STATE["wallet"]=wallet
    async def go():
        async def heartbeat():
            last=None
            while True:
                await asyncio.sleep(10)
                p=dict(r.progress)
                if p!=last:
                    print("WALLET_RESEARCH_PROGRESS",p,flush=True)
                    last=p
        hb=asyncio.create_task(heartbeat())
        try:
            rep=await r.run(copy=copy)
            print("WALLET_RESEARCH_COMPLETE",{
                "wallet":wallet,
                "coverage":rep.get("coverage"),
                "feeModel":(rep.get("feeReconciliation") or {}).get("selected"),
                "pnl":(rep.get("reconstructed") or {}).get("pnl"),
                "pre2026":(rep.get("persistence") or {}).get("pre2026"),
                "y2026":(rep.get("persistence") or {}).get("2026"),
                "strategy":rep.get("strategy"),
                "copyTest":rep.get("copyTest"),
                "wash":rep.get("washLowerBound")
            },flush=True)
        except Exception as exc:
            print("WALLET_RESEARCH_ERROR",type(exc).__name__,str(exc),flush=True)
        finally:
            hb.cancel()
    STATE["task"]=asyncio.create_task(go())

async def keepalive():
    # Render free web services sleep when they receive no HTTP traffic even if an
    # in-process research task is active. A lightweight self-health request keeps
    # this dedicated analysis job alive until its finite full-history run ends.
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            await asyncio.sleep(45)
            try:
                await c.get("https://poly-wallet-research.onrender.com/health")
            except Exception:
                pass

@app.on_event("startup")
async def startup():
    asyncio.create_task(keepalive())
    await launch(TARGET_WALLET,True)

@app.get("/health")
async def health():
    r=STATE.get("runner")
    return {"status":"online","wallet":STATE["wallet"],"progress":r.progress if r else {"stage":"idle"}}

@app.get("/api/research/progress")
async def progress():
    r=STATE.get("runner")
    return {"wallet":STATE["wallet"],"progress":r.progress if r else {"stage":"idle"}}

@app.get("/api/research/report")
async def report():
    r=STATE.get("runner")
    return {"wallet":STATE["wallet"],"progress":r.progress if r else {"stage":"idle"},"report":r.report if r else None}

@app.post("/api/research/run")
async def run(wallet:str=Query(default=TARGET_WALLET),copy:bool=Query(default=True)):
    await launch(wallet.lower(),copy)
    return {"started":True,"wallet":wallet.lower(),"copy":copy}
