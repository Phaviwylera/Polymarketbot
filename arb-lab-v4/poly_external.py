"""Public exchange market features, separate from the resolution oracle."""
import asyncio,json,math,time
from collections import deque
from datetime import datetime
import websockets
ASSETS=('BTC','ETH','SOL','XRP')
class ExchangeFeeds:
    def __init__(self):
        self.data={a:{} for a in ASSETS};self.history={};self.flows={};self.ids={};self.tasks=[];self.rejected=0
        self.status={'coinbase':'connecting','binance':'connecting'};self.metrics={p:{'messages':0,'reconnects':0,'lastReceivedAt':None,'lastError':None} for p in self.status}
    @staticmethod
    def timestamp(v):
        if isinstance(v,str) and 'T' in v:return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
        v=float(v);return v/1000 if v>1e12 else v
    def ingest(self,provider,message,now=None):
        now=time.time() if now is None else now;m=message.get('data',message)
        try:
            if provider=='coinbase':
                if m.get('type')!='ticker':return
                asset=m['product_id'].split('-')[0];ts=self.timestamp(m['time']);p=float(m['price'])
                spread=(float(m['best_ask'])-float(m['best_bid']))/p*1e4;ident=int(m['sequence']);qty=funding=None
            else:
                if m.get('e') not in ('aggTrade','markPriceUpdate'):return
                asset=m['s'].removesuffix('USDT');ts=self.timestamp(m['E']);p=float(m['p']);spread=0.
                ident=int(m['a']) if m['e']=='aggTrade' else 0
                qty=float(m['q'])*(-1 if m['m'] else 1) if ident else None
                funding=float(m['r']) if m['e']=='markPriceUpdate' else None
            if asset not in self.data:return
            key=(provider,asset);old=self.data[asset].get(provider,{})
            if not all(math.isfinite(x) for x in (ts,p,spread)) or p<=0 or spread<0 or not -1<=now-ts<=5 or ts<old.get('eventAt',0):self.rejected+=1;return
            if ident and ident<=self.ids.get(key,0):return
            if ident:self.ids[key]=ident
            h=self.history.setdefault(key,deque(maxlen=128))
            if h and ts-h[-1][0]<.1:h[-1]=(ts,p)
            else:h.append((ts,p))
            while h and h[0][0]<ts-10:h.popleft()
            prior=[r for r in h if r[0]<=ts-5]
            flow=self.flows.setdefault(key,deque(maxlen=16))
            if qty is not None:
                if not math.isfinite(qty):self.rejected+=1;return
                sec=int(ts)
                if flow and flow[-1][0]==sec:
                    _,signed,total=flow.pop();flow.append((sec,signed+qty,total+abs(qty)))
                else:flow.append((sec,qty,abs(qty)))
            while flow and flow[0][0]<ts-5:flow.popleft()
            row={**old,'eventAt':ts,'receivedAt':now,'price':p,'spreadBps':spread,
                'return5Bps':math.log(p/prior[-1][1])*1e4 if prior else 0.,
                'flow5':sum(q for t,q,total in flow)/max(sum(total for t,q,total in flow),1e-9)}
            if funding is not None and math.isfinite(funding):row.update(fundingRate=funding,fundingAt=ts,nextFundingAt=self.timestamp(m['T']))
            self.data[asset][provider]=row;self.status[provider]='live'
            self.metrics[provider]['messages']+=1;self.metrics[provider]['lastReceivedAt']=now
        except (ValueError,KeyError,TypeError,OverflowError):self.rejected+=1
    async def worker(self,provider):
        backoff=1
        while True:
            try:
                # Binance now routes aggregate trades and mark prices through /market.
                url='wss://ws-feed.exchange.coinbase.com' if provider=='coinbase' else 'wss://fstream.binance.com/market/stream?streams='+'/'.join(f'{a.lower()}usdt@{ch}' for a in ASSETS for ch in ('aggTrade','markPrice@1s'))
                async with websockets.connect(url,open_timeout=10,close_timeout=2,ping_interval=20,ping_timeout=20,max_queue=16) as ws:
                    if provider=='coinbase':await ws.send(json.dumps({'type':'subscribe','product_ids':[a+'-USD' for a in ASSETS],'channels':['ticker','heartbeat']}))
                    backoff=1
                    while True:
                        m=json.loads(await asyncio.wait_for(ws.recv(),30))
                        if m.get('type')=='error' or m.get('code'):raise ValueError('subscription rejected')
                        self.ingest(provider,m);await asyncio.sleep(0)
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.status[provider]='unavailable: '+type(exc).__name__
                self.metrics[provider]['reconnects']+=1;self.metrics[provider]['lastError']=type(exc).__name__
                await asyncio.sleep(backoff);backoff=min(60,backoff*2)
    def start(self):
        if not self.tasks:self.tasks=[asyncio.create_task(self.worker(p),name='v16-'+p) for p in self.status]
    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True);self.tasks=[]
    def snapshot(self):
        now=time.time()
        statuses={p:('stale' if s=='live' and (not self.metrics[p]['lastReceivedAt'] or now-self.metrics[p]['lastReceivedAt']>5) else s) for p,s in self.status.items()}
        return {'providers':statuses,'health':{p:{**m,'ageSec':now-m['lastReceivedAt'] if m['lastReceivedAt'] else None} for p,m in self.metrics.items()},'rejected':self.rejected,'assets':{a:{p:{**r,'ageSec':now-r['eventAt'],'fresh':0<=now-r['eventAt']<=5} for p,r in d.items()} for a,d in self.data.items()}}
