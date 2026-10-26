"""Jev shadow forecaster (record-only; places no orders).

Jev (TypeSafe, via OpenRouter's Decisions API) is asked a yes/no question at three fixed checkpoints of every live
BTC/ETH up/down round: "will UP win?". It sees what the bot sees at that moment: lane, seconds left, the UP token's
price, spot versus the round's opening price, and the recent spot path. Its probability is recorded next to the
market price and our learner's probability, then scored when the round resolves:
  * Brier score of Jev vs the price vs the learner, per checkpoint (does Jev add anything beyond the price?),
  * pre-registered trade test H2: when Jev differs from the price by >= 5 points, buy the side Jev favours at its
    ask + 1c stress, after the taker fee, one trade per checkpoint. Judged only on rounds after REGISTERED_AT.

Off unless OPENROUTER_API_KEY is set on the server (never in code or the repo). POLY_JEV=0 turns it off.
Daily call cap: POLY_JEV_MAX_CALLS_PER_DAY (default 3000, about $0.06/day at Jev's price).
"""
from __future__ import annotations
import asyncio,gzip,json,math,os,time
from collections import deque

URL='https://openrouter.ai/api/alpha/decisions'
MODEL=os.getenv('POLY_JEV_MODEL','typesafe/jev-1.14')
CHECKPOINTS=(('early',.8),('mid',.5),('late',.2))       # ask on the first print at or below this share of the round left
EDGE=0.05;STRESS=0.01;FEE_RATE=0.07;MIN_TRADES=300
REGISTERED_AT=1793043595   # 2026-10-26 19:39 UTC; H2 forward trades must end after this
CURRENT=None
def fee(p):return FEE_RATE*p*(1-p)
def ret(cost,won):c=cost+fee(cost);return ((1. if won else 0.)-c)/c

class Jev:
    def __init__(self,root=None,key=None,http=None,now=time.time,max_calls=None):
        self.key=key if key is not None else os.getenv('OPENROUTER_API_KEY','')
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.http=http;self.now=now
        self.enabled=bool(self.key) and os.getenv('POLY_JEV','1')!='0'
        self.max_calls=int(max_calls or os.getenv('POLY_JEV_MAX_CALLS_PER_DAY','3000'))
        self.asked={};self.pending={};self.scored=deque(maxlen=20000);self.inflight=0;self.paths={}
        self.state={'calls':0,'errors':0,'lastError':None,'costUsd':0.,'day':None,'callsToday':0,'model':MODEL,'stateFormat':'object'}
    def path(self):return os.path.join(self.root,'jev_state.json.gz')
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=8)
        return self.http
    # ---- inputs ---------------------------------------------------------------------------------------
    def on_print(self,row):
        """Called for every live tape print (sync, cheap). Schedules at most one Jev call per checkpoint per round."""
        if not self.enabled or row.get('leftFrac') is None or not row.get('slug'):return None
        slug=row['slug'];lf=float(row['leftFrac'])
        if row.get('spotVsOpenBps') is not None:
            p=self.paths.setdefault(slug,deque(maxlen=30));p.append((round(float(row.get('secondsLeft') or 0)),row['spotVsOpenBps']))
            if len(self.paths)>200:self.paths.pop(next(iter(self.paths)))
        for name,at in CHECKPOINTS:
            if lf<=at and (slug,name) not in self.asked:
                if lf<at-.15:self.asked[(slug,name)]=None;continue    # joined late (e.g. after a restart): skip, never back-fill
                self.asked[(slug,name)]='asking'
                if len(self.asked)>3000:
                    for k in list(self.asked)[:1000]:self.asked.pop(k,None)
                try:return asyncio.get_running_loop().create_task(self.ask(slug,name,dict(row)))
                except RuntimeError:return None
        return None
    def _question(self,row):
        up=float(row['price']) if row['token']=='UP' else 1-float(row['price'])
        pl=row.get('pLearned');pl_up=None if pl is None else (float(pl) if row['token']=='UP' else 1-float(pl))
        interval=900 if str(row['lane']).endswith('15') else 300
        st={'market':f"{'BTC' if row['lane'].startswith('BTC') else 'ETH'} up/down, {interval//60}-minute round on Polymarket",
            'resolves':'UP if the price at the end of the round is at or above the price at its start, else DOWN',
            'secondsLeft':row.get('secondsLeft'),'upTokenPrice':round(up,3),
            'spotVsOpenBps':row.get('spotVsOpenBps'),
            'recentSpotVsOpenBps':[list(x) for x in list(self.paths.get(row['slug']) or [])[-12:]]}
        return st,up,pl_up
    async def ask(self,slug,name,row):
        day=time.strftime('%Y-%m-%d',time.gmtime(self.now()))
        if self.state['day']!=day:self.state['day']=day;self.state['callsToday']=0
        if self.state['callsToday']>=self.max_calls or self.inflight>=4:self.asked[(slug,name)]=None;return None
        st,up,pl_up=self._question(row);self.inflight+=1;self.state['callsToday']+=1;self.state['calls']+=1
        q={'up_wins':{'type':'noul','instructions':'Will this round resolve UP?',
                      'criteria':{'true':'The round ends UP (end price at or above the start price).','false':'The round ends DOWN.'}}}
        try:
            p=await self._post(st,q)
        except Exception as exc:
            self.state['errors']+=1;self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:120]}';self.asked[(slug,name)]=None;return None
        finally:self.inflight-=1
        rec={'slug':slug,'lane':row['lane'],'checkpoint':name,'t':row['t'],'roundEnd':row.get('roundEnd'),'secondsLeft':row.get('secondsLeft'),
             'priceUp':round(up,4),'pJev':round(p,4),'pLearnedUp':None if pl_up is None else round(pl_up,4),
             'askUp':row.get('ask') if row['token']=='UP' else row.get('otherAsk'),'askDown':row.get('otherAsk') if row['token']=='UP' else row.get('ask')}
        self.pending.setdefault(slug,[]).append(rec);self.asked[(slug,name)]='done';return rec
    async def _post(self,st,q):
        """OpenRouter Decisions API. The docs show `state` both as an object and as a list of context parts, so
        the object form is tried first and the list form once if the API rejects it; the working form is kept."""
        c=await self._c();hdr={'Authorization':f'Bearer {self.key}','Content-Type':'application/json'}
        forms=['object','list'] if self.state['stateFormat']=='object' else ['list','object']
        last=None
        for f in forms:
            body={'model':MODEL,'state':st if f=='object' else [json.dumps(st)],'questions':q}
            r=await c.post(URL,headers=hdr,json=body)
            if r.status_code in (400,422):last=f'HTTP {r.status_code}';continue
            r.raise_for_status();d=r.json()
            self.state['stateFormat']=f;self.state['servedBy']=d.get('model')
            try:self.state['costUsd']+=float((d.get('usage') or {}).get('cost') or 0)
            except (TypeError,ValueError):pass
            a=(d.get('answers') or d).get('up_wins') or {}
            p=a.get('noul') if isinstance(a,dict) else None
            if p is None:raise ValueError('no noul probability in answer')
            return max(0.,min(1.,float(p)))
        raise ValueError(f'request rejected in both state formats ({last})')
    # ---- labels and scores ----------------------------------------------------------------------------
    def settle(self,slug,winner):
        """EdgeLab settle listener: label this round's Jev answers."""
        self.paths.pop(slug,None)
        recs=self.pending.pop(slug,[])
        if winner not in ('up','down'):return 0
        up=winner=='up'
        for r in recs:
            r['upWon']=up;self.scored.append(r)
        return len(recs)
    @staticmethod
    def trade(r):
        """H2: buy the side Jev favours when it differs from the price by >= EDGE; ask (if known) + 1c stress, taker fee."""
        d=r['pJev']-r['priceUp']
        if abs(d)<EDGE:return None
        if d>0:q=float(r['askUp']) if r.get('askUp') else r['priceUp'];won=r['upWon']
        else:q=float(r['askDown']) if r.get('askDown') else 1-r['priceUp'];won=not r['upWon']
        return ret(min(.99,q+STRESS),won)
    def report(self):
        rows=list(self.scored);out={'enabled':self.enabled,'model':MODEL,**{k:self.state.get(k) for k in ('calls','callsToday','errors','lastError','costUsd','servedBy','stateFormat')},
                                    'pendingRounds':len(self.pending),'scored':len(rows)}
        def brier(rs,k):
            v=[(r[k]-(1. if r['upWon'] else 0.))**2 for r in rs if r.get(k) is not None];return round(sum(v)/len(v),5) if v else None
        out['byCheckpoint']={}
        for name,_ in CHECKPOINTS:
            rs=[r for r in rows if r['checkpoint']==name]
            out['byCheckpoint'][name]={'n':len(rs),'brierJev':brier(rs,'pJev'),'brierPrice':brier(rs,'priceUp'),'brierLearned':brier(rs,'pLearnedUp')}
        out['all']={'n':len(rows),'brierJev':brier(rows,'pJev'),'brierPrice':brier(rows,'priceUp'),'brierLearned':brier(rows,'pLearnedUp')}
        fwd=[x for x in (self.trade(r) for r in rows if (r.get('roundEnd') or r['t'])>=REGISTERED_AT) if x is not None]
        n=len(fwd);m=sum(fwd)/n if n else None;sd=math.sqrt(sum((x-m)**2 for x in fwd)/(n-1)) if n>1 else None
        out['H2']={'rule':f'buy the side Jev favours when |pJev - price| >= {int(EDGE*100)} points; ask + 1c stress; taker fee',
                   'pass':f'>= {MIN_TRADES} forward trades and mean - 1.645 SE > 0','trades':n,'avgReturnPerDollar':None if m is None else round(m,4),
                   'lower95':round(m-1.645*sd/math.sqrt(n),4) if sd else None,
                   'verdict':'collecting' if n<MIN_TRADES else ('CONFIRMED' if sd and m-1.645*sd/math.sqrt(n)>0 else 'not confirmed')}
        out['recent']=rows[-10:][::-1]
        return out
    # ---- persistence ----------------------------------------------------------------------------------
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'scored':list(self.scored),'pending':self.pending,'state':self.state},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.scored.extend(d.get('scored') or []);self.pending.update(d.get('pending') or {})
        for k in ('calls','errors','costUsd','stateFormat'):
            if k in (d.get('state') or {}):self.state[k]=d['state'][k]
        return True
    async def loop(self):
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self.state['lastError']=f'load: {type(exc).__name__}'
        while True:
            await asyncio.sleep(120)
            try:
                cut=self.now()-3*3600
                for s in [s for s,v in self.pending.items() if v and float(v[-1]['t'])<cut]:self.pending.pop(s,None)   # never resolved
                self.save()
            except Exception as exc:self.state['lastError']=f'save: {type(exc).__name__}'
