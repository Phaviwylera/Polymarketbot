from __future__ import annotations
import asyncio, json, math, os, random, re, sqlite3, statistics, time
from collections import defaultdict
from datetime import datetime
import httpx

DATA="https://data-api.polymarket.com"
GAMMA="https://gamma-api.polymarket.com"
TARGET_WALLET="0x31393e2f8333d4a6e2e31ca6424defe8b6cb9e2e"
DB_PATH=os.environ.get("WALLET_RESEARCH_DB","/tmp/poly_wallet_research.sqlite3")
FEE_RATE={"CRYPTO":.07,"SPORTS":.05,"FINANCE":.04,"POLITICS":.04,"ECONOMICS":.05,
          "CULTURE":.05,"WEATHER":.05,"OTHER":.05,"GENERAL":.05,"MENTIONS":.04,"TECH":.04,
          "GEOPOLITICS":0.0}

def f(v,d=0.0):
    try:return float(v)
    except:return d
def i(v,d=0):
    try:return int(v)
    except:return d
def mean(xs): return sum(xs)/len(xs) if xs else None
def median(xs): return statistics.median(xs) if xs else None
def quantile(xs,q):
    if not xs:return None
    xs=sorted(xs);k=(len(xs)-1)*q;a=math.floor(k);b=math.ceil(k)
    return xs[a] if a==b else xs[a]*(b-k)+xs[b]*(k-a)
def role(t):
    x=str(t.get("trader_side") or t.get("type") or t.get("role") or "").upper()
    return "MAKER" if "MAKER" in x else ("TAKER" if "TAKER" in x else "UNKNOWN")
def asset(title):
    t=(title or "").lower()
    for a,ks in [("BTC",("bitcoin","btc")),("ETH",("ethereum","eth")),("SOL",("solana","sol")),
                 ("XRP",("xrp",)),("DOGE",("doge","dogecoin")),("OIL",("oil","crude","wti","brent"))]:
        if any(k in t for k in ks):return a
    return "OTHER"
def timeframe(title):
    t=(title or "").lower()
    if "5m" in t or re.search(r"5\s*(minute|min)",t):return "5m"
    if "15m" in t or re.search(r"15\s*(minute|min)",t):return "15m"
    return "other"
def parse_list(v):
    if isinstance(v,list):return v
    try:
        x=json.loads(v);return x if isinstance(x,list) else []
    except:return []
def dt_epoch(v):
    if v is None:return None
    if isinstance(v,(int,float)):return float(v)
    try:return datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp()
    except:return None

class API:
    def __init__(self):
        self.c=httpx.AsyncClient(timeout=35,follow_redirects=True,headers={"User-Agent":"POLY-EDGE-RESEARCH/2.0"})
        self.sem=asyncio.Semaphore(6)
    async def close(self):await self.c.aclose()
    async def get(self,url,params=None,retries=8):
        delay=.5
        for n in range(retries):
            try:
                async with self.sem:r=await self.c.get(url,params=params)
                if r.status_code in (429,503):
                    await asyncio.sleep(f(r.headers.get("Retry-After"),delay));delay=min(8,delay*1.7);continue
                r.raise_for_status();return r.json()
            except Exception:
                if n==retries-1:raise
                await asyncio.sleep(delay);delay=min(8,delay*1.7)
    async def pages(self,path,params,limit=1000):
        cursor=None
        while True:
            q=dict(params);q["limit"]=limit
            if cursor:q["cursor"]=cursor
            body=await self.get(DATA+path,q)
            rows=(body.get("data") if isinstance(body,dict) else body) or []
            yield rows
            pg=(body.get("pagination") or {}) if isinstance(body,dict) else {}
            cursor=pg.get("next_cursor")
            if not pg.get("has_more") or not cursor:break
            await asyncio.sleep(.04)

class WalletResearch:
    def __init__(self,wallet=TARGET_WALLET):
        self.wallet=wallet.lower();self.api=API();self.progress={"stage":"idle"};self.report=None
        try:os.remove(DB_PATH)
        except FileNotFoundError:pass
        self.db=sqlite3.connect(DB_PATH)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
        CREATE TABLE trades(id INTEGER PRIMARY KEY,condition TEXT,event_id TEXT,event_slug TEXT,title TEXT,outcome TEXT,outcome_index INTEGER,side TEXT,role TEXT,price REAL,size REAL,ts INTEGER,tx TEXT,token TEXT);
        CREATE INDEX tr_cond ON trades(condition);CREATE INDEX tr_ts ON trades(ts);CREATE INDEX tr_tx ON trades(tx);
        CREATE TABLE closed(condition TEXT,realized REAL,title TEXT,event_slug TEXT,first_entry INTEGER,last_event INTEGER,entry_cost REAL,total_size REAL);
        CREATE INDEX cp_cond ON closed(condition);
        """)
    def log(self,*a):print("WALLET_RESEARCH",*a,flush=True)
    async def ingest_trades(self):
        total=0;page=0
        async for rows in self.api.pages("/v2/trades",{"user":self.wallet,"taker_only":"false","start":1,"sort_by":"TIMESTAMP","sort_direction":"ASC"},1000):
            page+=1
            vals=[]
            for t in rows:
                vals.append((
                    str(t.get("condition_id") or t.get("conditionId") or ""),
                    str(t.get("event_id") or t.get("eventId") or ""),str(t.get("event_slug") or t.get("eventSlug") or ""),
                    str(t.get("title") or ""),str(t.get("outcome") or ""),i(t.get("outcome_index"),999),
                    str(t.get("side") or "").upper(),role(t),f(t.get("price")),f(t.get("size")),i(t.get("timestamp")),
                    str(t.get("transaction_hash") or t.get("transactionHash") or ""),str(t.get("token_id") or t.get("asset") or "")
                ))
            self.db.executemany("INSERT INTO trades(condition,event_id,event_slug,title,outcome,outcome_index,side,role,price,size,ts,tx,token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",vals);self.db.commit()
            total+=len(vals);self.progress={"stage":"wallet_trades","page":page,"rows":total}
            if page%20==0:self.log("PROGRESS",self.progress)
        return total
    async def ingest_closed(self):
        total=0;page=0
        async for rows in self.api.pages("/v2/positions",{"user":self.wallet,"status":"CLOSED","sort_by":"TIMESTAMP","sort_direction":"ASC"},1000):
            page+=1;vals=[]
            for p in rows:
                vals.append((str(p.get("condition_id") or ""),f(p.get("realized_pnl")),str(p.get("title") or ""),str(p.get("event_slug") or ""),i(p.get("first_entry_at")),i(p.get("last_event_at")),f(p.get("entry_cost_usdc")),f(p.get("total_size"))))
            self.db.executemany("INSERT INTO closed(condition,realized,title,event_slug,first_entry,last_event,entry_cost,total_size) VALUES(?,?,?,?,?,?,?,?)",vals);self.db.commit()
            total+=len(vals);self.progress={"stage":"closed_positions","page":page,"rows":total}
        self.log("CLOSED",total);return total
    def market_rows(self):
        sql="""
        SELECT t.condition,MAX(t.event_id),MAX(t.event_slug),MAX(t.title),MIN(t.ts),MAX(t.ts),COUNT(*),
               SUM(t.role='MAKER'),SUM(t.role='TAKER'),
               SUM(CASE WHEN t.side='BUY' THEN t.size*t.price ELSE 0 END),
               SUM(CASE WHEN t.side='SELL' THEN t.size*t.price ELSE 0 END),
               COUNT(DISTINCT CASE WHEN t.side='BUY' THEN t.outcome END),
               COALESCE((SELECT SUM(c.realized) FROM closed c WHERE c.condition=t.condition),NULL)
        FROM trades t WHERE t.condition<>'' GROUP BY t.condition
        """
        rows=[]
        for q in self.db.execute(sql):
            cond,event_id,event_slug,title,first_ts,last_ts,fills,maker,taker,buy_usd,sell_usd,buy_outcomes,official=q
            rows.append({"condition":cond,"eventId":event_id or "","eventSlug":event_slug or "","title":title or "",
                         "firstTs":first_ts,"lastTs":last_ts,"fills":fills,"makerFills":maker or 0,"takerFills":taker or 0,
                         "makerShare":(maker or 0)/fills if fills else 0,"boughtUsd":buy_usd or 0,"soldUsd":sell_usd or 0,
                         "bothSidesBought":(buy_outcomes or 0)>=2,"officialPnl":official,
                         "asset":asset(title or ""),"timeframe":timeframe(title or "")})
        return rows
    def pair_vwap(self,condition):
        d=defaultdict(lambda:[0.0,0.0])
        for outcome,sh,pr in self.db.execute("SELECT outcome,size,price FROM trades WHERE condition=? AND side='BUY'",(condition,)):
            d[outcome][0]+=sh;d[outcome][1]+=sh*pr
        vals=[cost/sh for sh,cost in d.values() if sh>0]
        return sum(vals) if len(vals)>=2 else None
    def group(self,rows,key):
        d=defaultdict(list)
        for r in rows:
            if r.get("officialPnl") is not None:d[str(r.get(key) or "UNKNOWN")].append(f(r["officialPnl"]))
        return {k:{"markets":len(v),"pnl":sum(v),"avg":mean(v),"median":median(v),"winRate":sum(x>0 for x in v)/len(v)}
                for k,v in sorted(d.items(),key=lambda kv:sum(kv[1]),reverse=True)}
    def bootstrap(self,vals,n=1200,seed=7):
        vals=[f(x) for x in vals]
        if not vals:return None
        rng=random.Random(seed);m=[]
        for _ in range(n):m.append(sum(rng.choice(vals) for __ in vals)/len(vals))
        return {"n":len(vals),"mean":mean(vals),"lo95":quantile(m,.025),"hi95":quantile(m,.975)}
    def wash(self):
        hits=self.db.execute("""
        SELECT tx,COUNT(*),COUNT(DISTINCT role),COUNT(DISTINCT side),COUNT(DISTINCT condition)
        FROM trades WHERE tx<>'' GROUP BY tx HAVING COUNT(DISTINCT role)>1 AND COUNT(DISTINCT side)>1
        """).fetchall()
        return {"transactions":len(hits),"examples":[{"tx":x[0],"rows":x[1],"conditions":x[4]} for x in hits[:25]]}
    async def gamma_market(self,condition):
        body=await self.api.get(GAMMA+"/markets",{"condition_ids":condition,"limit":5})
        return (body or [None])[0] if isinstance(body,list) else None
    def winner(self,m):
        if not m or not m.get("closed"):return None
        status=str(m.get("umaResolutionStatus") or m.get("resolutionStatus") or "").lower()
        if status and "resolv" not in status:return None
        ps=parse_list(m.get("outcomePrices"));outs=parse_list(m.get("outcomes"))
        if not ps or len(ps)!=len(outs):return None
        vals=[f(x,-1) for x in ps]
        if max(vals)<.999:return None
        return str(outs[max(range(len(vals)),key=vals.__getitem__)])
    def fee_rate(self,m):
        if not m or m.get("feesEnabled") is False:return 0.0
        fs=m.get("feeSchedule") or {}
        for k in ("takerFeeRate","taker_fee_rate","feeRate","rate"):
            if k in fs:
                x=f(fs[k]);return x/10000 if x>1 else x
        cat=str(m.get("category") or "OTHER").upper()
        if "GEO" in cat or "WORLD" in cat:return 0.0
        return FEE_RATE.get(cat,.05)
    def taker_fee(self,shares,price,m):return round(shares*self.fee_rate(m)*price*(1-price),5)
    async def copy_test(self,rows,delays=(0,1,2,5,10),stake=100.0,max_markets=300):
        # New position = earliest wallet BUY per outcome/condition. Replicate at next real taker BUY.
        eligible=[r for r in rows if r["officialPnl"] is not None]
        eligible=sorted(eligible,key=lambda r:r["firstTs"] or 0)[-max_markets:]
        out={str(d):[] for d in delays};done=0
        self.progress={"stage":"copy_test","done":0,"total":len(eligible)}
        for r in eligible:
            m=await self.gamma_market(r["condition"]);win=self.winner(m)
            if not win:continue
            src=self.db.execute("SELECT outcome,MIN(ts) FROM trades WHERE condition=? AND side='BUY' GROUP BY outcome",(r["condition"],)).fetchall()
            # one condition fetch, then reuse for all outcomes/delays
            alltr=[];cursor=None
            while True:
                q={"condition":r["condition"],"taker_only":"true","limit":1000}
                if cursor:q["cursor"]=cursor
                body=await self.api.get(DATA+"/v2/trades",q);alltr.extend(body.get("data") or [])
                pg=body.get("pagination") or {};cursor=pg.get("next_cursor")
                if not pg.get("has_more") or not cursor:break
            alltr.sort(key=lambda z:i(z.get("timestamp")))
            for d in delays:
                totalp=0.0;used=False
                for outcome,ts0 in src:
                    target=i(ts0)+d
                    nxt=next((z for z in alltr if str(z.get("side") or "").upper()=="BUY" and str(z.get("outcome") or "")==str(outcome) and i(z.get("timestamp"))>=target),None)
                    if not nxt:continue
                    pr=f(nxt.get("price"));sh=stake/pr if pr>0 else 0
                    if sh<=0:continue
                    fee=self.taker_fee(sh,pr,m);payout=sh if str(outcome)==win else 0
                    totalp+=payout-stake-fee;used=True
                if used:out[str(d)].append(totalp)
            done+=1;self.progress={"stage":"copy_test","done":done,"total":len(eligible)}
            if done%20==0:self.log("COPY_PROGRESS",self.progress)
        return {d:{"markets":len(v),"pnl":sum(v),"avg":mean(v),"winRate":sum(x>0 for x in v)/len(v) if v else None,"bootstrap":self.bootstrap(v)} for d,v in out.items()}
    def war_bets(self):
        kws=("war","strike","ceasefire","regime","invasion","attack","military")
        out=[]
        # Uses wallet fills; exact resolved-YES screening is deferred to market enrichment.
        for cond,title,outcome,pr,sh,ts in self.db.execute("SELECT condition,title,outcome,price,size,ts FROM trades WHERE side='BUY' AND price<=0.30 AND size*price>=10000"):
            if any(k in (title or "").lower() for k in kws):
                out.append({"condition":cond,"title":title,"outcome":outcome,"price":pr,"notional":pr*sh,"timestamp":ts})
        return out[:500]
    async def run(self,copy=True):
        try:
            fills=await self.ingest_trades();closed=await self.ingest_closed()
            self.progress={"stage":"official_stats"}
            stats=await self.api.get(DATA+"/v2/user-stats",{"user":self.wallet})
            pnl=await self.api.get(DATA+"/v2/user-pnl",{"user":self.wallet,"interval":"max","fidelity":"1d"})
            rows=self.market_rows()
            resolved=[r for r in rows if r["officialPnl"] is not None]
            pre=[r for r in resolved if (r["firstTs"] or 0)<1767225600]
            y26=[r for r in resolved if (r["firstTs"] or 0)>=1767225600]
            # compute pair-vwap only for a bounded diagnostic cohort to keep memory and API light
            self.progress={"stage":"pair_diagnostics","done":0,"total":min(len(resolved),1000)}
            for n,r in enumerate(sorted(resolved,key=lambda x:abs(f(x["officialPnl"])),reverse=True)[:1000],1):
                r["pairBuyVwapSum"]=self.pair_vwap(r["condition"])
                if n%100==0:self.progress["done"]=n
            eventp=defaultdict(float)
            for r in resolved:eventp[r["eventSlug"] or r["condition"]]+=f(r["officialPnl"])
            copyres=await self.copy_test(rows) if copy else None
            pts=((pnl or {}).get("data") or {}).get("points") or []
            latest=pts[-1] if pts else {}
            rep={
              "wallet":self.wallet,"generatedAt":time.time(),
              "coverage":{"fills":fills,"markets":len(rows),"closedPositions":closed,"officialPnlMarkets":len(resolved),"events":len(eventp)},
              "official":{"stats":stats,"latestPnlPoint":latest},
              "reconstructed":{"officialMarketPnlSum":sum(f(r["officialPnl"]) for r in resolved),"wins":sum(f(r["officialPnl"])>0 for r in resolved),"losses":sum(f(r["officialPnl"])<0 for r in resolved),"marketBootstrap":self.bootstrap([r["officialPnl"] for r in resolved]),"eventPnlSum":sum(eventp.values()),"eventBootstrap":self.bootstrap(list(eventp.values()))},
              "persistence":{"pre2026":{"markets":len(pre),"boughtUsd":sum(r["boughtUsd"] for r in pre),"pnl":sum(f(r["officialPnl"]) for r in pre),"bootstrap":self.bootstrap([r["officialPnl"] for r in pre])},"2026":{"markets":len(y26),"boughtUsd":sum(r["boughtUsd"] for r in y26),"pnl":sum(f(r["officialPnl"]) for r in y26),"bootstrap":self.bootstrap([r["officialPnl"] for r in y26])}},
              "strategy":{"makerShare":sum(r["makerFills"] for r in rows)/max(1,sum(r["fills"] for r in rows)),"bothSidesBoughtRate":sum(r["bothSidesBought"] for r in rows)/max(1,len(rows)),"pairBuyVwapMedianTop1000":median([r.get("pairBuyVwapSum") for r in resolved if r.get("pairBuyVwapSum") is not None]),"byAsset":self.group(resolved,"asset"),"byTimeframe":self.group(resolved,"timeframe")},
              "copyTest":copyres,"warBetCandidates":self.war_bets(),"washLowerBound":self.wash(),
              "topMarkets":[r for r in sorted(resolved,key=lambda x:f(x["officialPnl"]),reverse=True)[:100]],
              "bottomMarkets":[r for r in sorted(resolved,key=lambda x:f(x["officialPnl"]))[:100]],
              "methodology":{"walletFills":"full cursor walk, maker+taker included","pnlTruth":"Polymarket CLOSED position realized PnL per market, summed by event","split":"selected/persistence split at 2026-01-01 UTC","copy":"$100 per new outcome position at next real taker BUY after delay, fees included, hold to resolution","bootstrap":"markets resampled, 1200 draws"},
              "limitations":["This target-wallet pass uses Polymarket official per-market realized PnL as the reconciliation truth. Cash-fill+payout reconstruction and the 45,283-market universe completeness audit are implemented as the next universe stage, not silently approximated here.","Copy test has no price impact and uses public taker prints, matching the requested best-case replication design.","Oil 1-minute attribution is not reported until a crude-futures 1-minute source is connected."]
            }
            self.report=rep;self.progress={"stage":"complete","fills":fills,"markets":len(rows)}
            self.log("COMPLETE",{"coverage":rep["coverage"],"reconstructed":rep["reconstructed"],"persistence":rep["persistence"],"strategy":rep["strategy"],"copyTest":rep["copyTest"],"wash":rep["washLowerBound"]})
            return rep
        except Exception as exc:
            self.progress={"stage":"error","type":type(exc).__name__,"message":str(exc)};self.log("ERROR",self.progress);raise
        finally:
            try:self.db.commit()
            except:pass
            await self.api.close()
