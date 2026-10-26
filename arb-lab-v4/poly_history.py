"""History backfill: study weeks of PAST BTC/ETH 5m/15m up/down rounds from Polymarket's public data (record-only).

For each past round (oldest first, so the history study's rule stays prequential):
  * Gamma: the market (closed) -> conditionId and the winner,
  * data-api: every taker trade in that market (price, size, side, outcome, wallet, time),
  * Coinbase 1-minute candles: spot at each trade and at the round's open, for spot-vs-open context
    (Polymarket resolves on Chainlink; Coinbase is a close proxy used for study only).
Rows go to a separate history Study (never mixed with the live one) and to S3 (polybot/history/...).
Spot at a trade is the close of the last minute that had FULLY closed before the trade (no look-ahead: v1 used the
close of the minute containing the trade, up to 60 s in the future, which leaked late-round outcomes; v1 results discarded).
Progress is saved, so restarts resume. Paced gently (one round every ~0.7 s) to stay polite to public APIs.
Off with POLY_HISTORY=0; depth with POLY_HISTORY_DAYS (default 21).
"""
from __future__ import annotations
import asyncio,gzip,json,os,time
from collections import defaultdict

GAMMA='https://gamma-api.polymarket.com';DATA='https://data-api.polymarket.com';COINBASE='https://api.exchange.coinbase.com'
LANES=(('BTC','btc',300,'BTC-USD'),('ETH','eth',300,'ETH-USD'),('BTC15','btc',900,'BTC-USD'),('ETH15','eth',900,'ETH-USD'))
CURRENT=None
VERSION='v2'   # v2: no-look-ahead spot; files are versioned so v1 state is never reloaded

class History:
    def __init__(self,study,root=None,s3=None,http=None,days=None):
        self.study=study;self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.s3=s3;self.http=http
        self.days=float(days or os.getenv('POLY_HISTORY_DAYS','21'))
        self.done=set();self.candles={};self.buffer=defaultdict(list)
        self.state={'roundsDone':0,'tradesSeen':0,'errors':0,'lastError':None,'oldestRound':None,'newestRound':None,'queue':0,'phase':'starting',
                    'mode':'record-only: past rounds from Polymarket public data'}
    def path(self,name):return os.path.join(self.root,name)
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=25,headers={'User-Agent':'poly-history-study'})
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    # ---- inputs -------------------------------------------------------------------------------------
    async def market(self,slug):
        d=await self.get(GAMMA+'/markets',{'slug':slug,'closed':'true'})
        m=d[0] if isinstance(d,list) and d else None
        if not m:return None,None
        outs=json.loads(m['outcomes']) if isinstance(m.get('outcomes'),str) else m.get('outcomes')
        prices=json.loads(m['outcomePrices']) if isinstance(m.get('outcomePrices'),str) else m.get('outcomePrices')
        if not outs or not prices:return m,None
        pr=[float(x) for x in prices]
        if max(pr)<.99:return m,None
        return m,str(outs[pr.index(max(pr))]).upper()
    async def trades(self,cid):
        out=[];off=0
        while off<10000:
            d=await self.get(DATA+'/trades',{'market':cid,'limit':1000,'offset':off,'takerOnly':'true'})
            rows=d.get('data') if isinstance(d,dict) else d
            if not rows:break
            out+=rows;off+=len(rows)
            if len(rows)<1000:break
            await asyncio.sleep(.25)
        return out
    async def spot_at(self,product,t):
        """Coinbase 1-minute close for the minute containing t (cached in 5-hour pages)."""
        page=int(t//18000)*18000;key=(product,page)
        if key not in self.candles:
            iso=lambda x:time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(x))
            rows=await self.get(f'{COINBASE}/products/{product}/candles',{'granularity':60,'start':iso(page),'end':iso(page+18000)})
            self.candles[key]={int(r[0]):(float(r[3]),float(r[4])) for r in rows or []}   # minute -> (open, close)
            if len(self.candles)>400:self.candles.pop(next(iter(self.candles)))
        m=self.candles[key].get(int(t//60)*60)
        return m
    # ---- one round ----------------------------------------------------------------------------------
    async def round(self,lane,coin,interval,product,start):
        slug=f'{coin}-updown-{"5m" if interval==300 else "15m"}-{start}'
        if slug in self.done:return 0
        m,winner=await self.market(slug)
        if not m:self.done.add(slug);return 0
        if winner not in ('UP','DOWN'):return 0                       # not resolved yet: retry later
        raw=await self.trades(m.get('conditionId'))
        end=start+interval;o=await self.spot_at(product,start);opn=o[0] if o else None
        rows=[]
        for t in raw:
            outc=str(t.get('outcome') or '').upper();side=str(t.get('side') or '').upper()
            try:price=float(t['price']);size=float(t['size']);ts=float(t['timestamp'])
            except (KeyError,TypeError,ValueError):continue
            if ts>1e12:ts/=1000
            if outc not in ('UP','DOWN') or side not in ('BUY','SELL') or not 0<price<1 or size<=0:continue
            sp=await self.spot_at(product,ts-60);spot=sp[1] if sp else None   # previous minute's close: known at ts
            bps=(spot/opn-1)*1e4 if spot and opn else None
            left=max(0.,min(1.,(end-ts)/interval))
            rows.append({'t':ts,'lane':lane,'slug':slug,'roundEnd':end,'token':outc,'price':price,'size':size,'aggressor':side,
                         'leftFrac':round(left,4),'secondsLeft':round(end-ts,1),'spotVsOpenBps':None if bps is None else round(bps,2),
                         'agreeBps':None if bps is None else round(bps if outc=='UP' else -bps,2),'won':outc==winner,
                         'wallet':str(t.get('proxyWallet') or '')[:42],'pToken':None,'hist':True,'v':VERSION})
        self.done.add(slug)
        if rows:
            self.study.on_round(rows,winner.lower())
            per=defaultdict(lambda:{'pnl':0.,'buys':0,'buyUsd':0.,'entrySec':[],'prices':[]})
            for r in rows:
                if not r['wallet']:continue
                w=per[r['wallet']];win=1. if r['won'] else 0.
                if r['aggressor']=='BUY':w['pnl']+=r['size']*(win-r['price']);w['buys']+=1;w['buyUsd']+=r['size']*r['price'];w['entrySec'].append(r['t']-start);w['prices'].append(r['price'])
                else:w['pnl']+=r['size']*(r['price']-win)
            if per:self.study.on_wallet_round(lane,start,winner.lower(),per)
            self.buffer[time.strftime('%Y-%m-%d',time.gmtime(start))].extend(rows)
        self.state['roundsDone']+=1;self.state['tradesSeen']+=len(rows)
        self.state['oldestRound']=min(self.state['oldestRound'] or start,start);self.state['newestRound']=max(self.state['newestRound'] or start,start)
        return len(rows)
    # ---- files / persistence ------------------------------------------------------------------------
    async def flush(self):
        for day,rows in list(self.buffer.items()):
            d=self.path('history_'+VERSION);os.makedirs(d,exist_ok=True);p=os.path.join(d,f'{day}.jsonl.gz')
            with gzip.open(p,'at') as f:
                for r in rows:f.write(json.dumps(r,separators=(',',':'))+'\n')
            self.buffer.pop(day,None)
        try:
            with open(self.path(f'history_done_{VERSION}.json'),'w') as f:json.dump(sorted(self.done),f)
            self.study.save(self.path(f'history_study_{VERSION}.json.gz'))
        except Exception as exc:self.state['lastError']=f'save: {type(exc).__name__}'
    async def upload(self):
        if self.s3 is None:return
        d=self.path('history_'+VERSION)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name>=time.strftime('%Y-%m-%d',time.gmtime(time.time()-86400)):continue   # recent days may still grow
            p=os.path.join(d,name)
            try:
                import hashlib
                await self.s3.put(p,f'polybot/tape/history-{VERSION}/{name}',hashlib.sha256(open(p,'rb').read()).hexdigest());os.remove(p)
            except Exception as exc:self.state['lastError']=f'upload: {type(exc).__name__}'
    def load(self):
        try:
            if os.path.exists(self.path(f'history_done_{VERSION}.json')):self.done=set(json.load(open(self.path(f'history_done_{VERSION}.json'))))
            self.study.load(self.path(f'history_study_{VERSION}.json.gz'))
        except Exception as exc:self.state['lastError']=f'load: {type(exc).__name__}'
    # ---- main loop ----------------------------------------------------------------------------------
    def plan(self,now=None):
        """All round starts from `days` ago up to 30 minutes ago, oldest first, across the four lanes."""
        now=time.time() if now is None else now;first=now-self.days*86400;out=[]
        for lane,coin,iv,prod in LANES:
            s=int(first//iv)*iv
            while s+iv<=now-1800:out.append((s,lane,coin,iv,prod));s+=iv
        out.sort();return [x for x in out if f'{x[2]}-updown-{"5m" if x[3]==300 else "15m"}-{x[0]}' not in self.done]
    async def loop(self,engine=None,first_delay=180):
        global CURRENT;CURRENT=self
        await asyncio.sleep(first_delay);self.load()
        while True:
            st=getattr(engine,'storage',None) if engine else None
            if st is not None and not st.active:await asyncio.sleep(60);continue
            todo=self.plan();self.state['queue']=len(todo);self.state['phase']='backfilling' if todo else 'caught up'
            for i,(start,lane,coin,iv,prod) in enumerate(todo):
                try:await self.round(lane,coin,iv,prod,start)
                except Exception as exc:self.state['errors']+=1;self.state['lastError']=f'{lane} {start}: {type(exc).__name__}';await asyncio.sleep(3)
                self.state['queue']=len(todo)-i-1
                if i%25==24:await self.flush()
                if i%500==499:await self.upload()
                await asyncio.sleep(.7)
            await self.flush();await self.upload()
            await asyncio.sleep(900)                       # then pick up newly resolved rounds every 15 min
    def snapshot(self):return {**self.state,'days':self.days,'studied':len(self.done),'version':VERSION,'spot':'previous fully-closed minute (no look-ahead)'}
