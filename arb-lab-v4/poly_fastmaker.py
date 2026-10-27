"""Fast maker lane M2 (record-only; places no orders): does market-making work if quotes move with the book?

Driven by every order-book change on the live BTC/ETH up/down lanes (not just by trades), with an order latency:
a new quote only takes effect LATENCY seconds after the book change that caused it, and until then the old quote can
still be filled (that is how fast makers still get picked off).
Strategy (one pair per round, equal shares on both tokens):
  * Flat: bid PAIR_SHARES on UP and on DOWN at their best bids, only while bid_UP + bid_DOWN <= 1 - MIN_EDGE.
  * After one side fills: keep bidding the other side at min(its best bid, 1 - MIN_EDGE - our average cost), so a
    second fill completes the pair at a locked profit; stop quoting the filled side.
  * Risk control: if the unhedged side's best bid falls STOP_MOVE below our cost, or less than FLATTEN_AT of the
    round is left, sell the unhedged shares at the best bid as a taker (fee applies). Pairs are held to resolution.
  * Quoting starts at START_AT of the round left and stops at FLATTEN_AT.
Queue: we join the back of the queue at our price (the displayed size there when we post). A SELL print at our
price eats the queue ahead first; a print below our price fills us. Prints and book changes use receive times.
Pre-registered test M2 (REGISTERED_AT): rounds with any fill; pass = >= 300 such rounds and the mean round P&L per
$ committed - 1.645 SE > 0.  Off with POLY_FASTMAKER=0.
"""
from __future__ import annotations
import gzip,json,math,os,time
from collections import deque

PAIR_SHARES=5.0;MIN_EDGE=0.01;LATENCY=0.25;STOP_MOVE=0.10;START_AT=0.95;FLATTEN_AT=0.15;FEE_RATE=0.07;MIN_ROUNDS=300
REGISTERED_AT=1793085345   # 2026-10-27 07:15 UTC: forward results count only after this
CURRENT=None
def fee(p):return FEE_RATE*p*(1-p)
def rebate(p):return 0.20*FEE_RATE*p*(1-p)    # maker share of crypto taker fees (estimate: assumes our fills match fee-paying takers)
OTHER={'up':'down','down':'up'}

class Round:
    __slots__=('slug','lane','end','interval','bbo','q','pending','inv','cash','flattened','events','closed','reb')
    def __init__(self,slug,lane,end,interval):
        self.slug=slug;self.lane=lane;self.end=end;self.interval=interval
        self.bbo={'up':{},'down':{}};self.q={'up':None,'down':None};self.pending={'up':None,'down':None}
        self.inv={'up':[0.,0.],'down':[0.,0.]}   # side -> [shares, cost]
        self.cash=0.;self.flattened=0;self.events=0.;self.closed=False;self.reb=0.

class FastMaker:
    def __init__(self,root=None):
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_FASTMAKER','1')!='0'
        self.cur={};self.wait={};self.done=deque(maxlen=20000)
        self.state={'bookEvents':0,'prints':0,'requotes':0,'fills':0,'flattens':0,'rounds':0,'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'fastmaker_state.json.gz')
    def _err(self,exc):self.state['errors']+=1;self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:100]}'
    # ---- helpers -------------------------------------------------------------------------------------
    def _round(self,lane,market):
        slug=(market or {}).get('slug');end=(market or {}).get('endTs')
        if not slug or not end:return None
        r=self.cur.get(lane)
        if r is None or r.slug!=slug:
            if r is not None and not r.closed:self._close(r)
            r=self.cur[lane]=Round(slug,lane,float(end),900. if str(lane).endswith('15') else 300.)
        return r
    def _close(self,r):
        """Round over for quoting: whatever is resting is cancelled; inventory waits for the result."""
        r.closed=True;r.q={'up':None,'down':None};r.pending={'up':None,'down':None}
        if any(v[0]>0 for v in r.inv.values()) or r.flattened:self.wait[r.slug]=r
        else:self.done.append({'slug':r.slug,'lane':r.lane,'end':r.end,'fills':0})
    def _left(self,r,t):return max(0.,min(1.,(r.end-t)/r.interval))
    def _target(self,r,side):
        """Price we want to bid on `side`, or None to not quote it."""
        b=r.bbo[side].get('bid');ob=r.bbo[OTHER[side]].get('bid')
        if b is None or r.flattened:return None                         # after a stop-out, no new risk this round
        sh,cost=r.inv[side];osh,ocost=r.inv[OTHER[side]]
        if sh>=PAIR_SHARES-1e-9:return None                              # this leg is done
        if osh>sh+1e-9:                                                  # complete the pair at a locked profit
            cap=1-MIN_EDGE-ocost/osh;p=min(float(b),math.floor(cap*100+1e-9)/100)
            return p if p>=0.01 else None
        if osh>0:return None
        if ob is None or float(b)+float(ob)>1-MIN_EDGE+1e-9:return None  # flat: only when the pair is cheap enough
        return float(b) if 0.01<=float(b)<=0.98 else None
    def _requote(self,r,side,t):
        p=self._target(r,side);q=r.q[side]
        if p is None:
            if q is not None or r.pending[side] is not None:r.pending[side]=('cancel',t+LATENCY)
            return
        if q is not None and abs(q['price']-p)<1e-9:r.pending[side]=None;return
        size=float(r.bbo[side].get('bidSize') or 0) if abs(p-float(r.bbo[side].get('bid') or -1))<1e-9 else 0.
        r.pending[side]=('post',t+LATENCY,p,size);self.state['requotes']+=1
    def _apply_due(self,r,t):
        for side in ('up','down'):
            pd=r.pending[side]
            if pd and pd[1]<=t:
                r.pending[side]=None
                r.q[side]=None if pd[0]=='cancel' else {'price':pd[2],'ahead':pd[3],'postedAt':pd[1]}
    def _flatten(self,r,side,t,why):
        """Sell only the unpaired excess on `side` at its best bid (taker, fee); paired shares stay to resolution."""
        sh,cost=r.inv[side];osh=r.inv[OTHER[side]][0];bid=r.bbo[side].get('bid');x=sh-osh
        if x<=1e-9 or bid is None:return
        b=float(bid);xc=cost*x/sh
        r.cash+=x*(b-fee(b))-xc;r.inv[side]=[sh-x,cost-xc];r.flattened+=1;self.state['flattens']+=1
        r.events+=xc                                                    # capital that went into the flattened shares
        r.q={'up':None,'down':None};r.pending={'up':None,'down':None}
    def _risk(self,r,t):
        us=r.inv['up'][0]-r.inv['down'][0]
        if abs(us)<1e-9:return
        side='up' if us>0 else 'down';sh,cost=r.inv[side];avg=cost/sh;bid=r.bbo[side].get('bid')
        if self._left(r,t)<FLATTEN_AT:self._flatten(r,side,t,'time')
        elif bid is not None and float(bid)<=avg-STOP_MOVE+1e-9:self._flatten(r,side,t,'stop')
    # ---- feeds ---------------------------------------------------------------------------------------
    def on_book(self,lane,side,bbo,market,t=None):
        if not self.enabled:return
        try:
            t=time.time() if t is None else t;r=self._round(lane,market)
            if r is None or r.closed:return
            self.state['bookEvents']+=1
            for s in ('up','down'):r.bbo[s]=dict((bbo or {}).get(s) or {})
            self._apply_due(r,t);lf=self._left(r,t)
            if lf<FLATTEN_AT:
                self._risk(r,t);self._close(r);return
            self._risk(r,t)
            if lf<=START_AT:
                for s in ('up','down'):self._requote(r,s,t)
        except Exception as exc:self._err(exc)
    def on_print(self,lane,pr,market):
        if not self.enabled:return
        try:
            r=self.cur.get(lane)
            if r is None or r.closed or (market or {}).get('slug')!=r.slug:return
            t=float(pr.get('receivedAt') or time.time());self._apply_due(r,t);self.state['prints']+=1
            side=pr.get('side');q=r.q.get(side)
            if not q or t<q['postedAt']:return
            price=float(pr['price']);size=float(pr.get('size') or 0);agg=str(pr.get('aggressor') or '').upper()
            sh,cost=r.inv[side];room=PAIR_SHARES-sh
            if room<=1e-9:return
            if price<q['price']-1e-9:fill=room
            elif abs(price-q['price'])<1e-9 and agg=='SELL':
                eat=min(q['ahead'],size);q['ahead']-=eat;fill=min(room,size-eat)
            else:fill=0.
            if fill>0:
                r.inv[side]=[sh+fill,cost+fill*q['price']];self.state['fills']+=1;r.reb+=fill*rebate(q['price'])
                for s in ('up','down'):self._requote(r,s,t)
        except Exception as exc:self._err(exc)
    def settle(self,slug,winner):
        for lane,r in list(self.cur.items()):
            if r.slug==slug and not r.closed:self._close(r)
        r=self.wait.pop(slug,None)
        if r is None or winner not in ('up','down'):return 0
        pay=r.inv[winner][0];held=r.inv['up'][1]+r.inv['down'][1]
        pnl=pay-held+r.cash;cost=held+r.events                           # cash already nets flattened cost
        pairs=min(r.inv['up'][0],r.inv['down'][0])
        self.done.append({'slug':slug,'lane':r.lane,'end':r.end,'fills':1,'pairs':round(pairs,4),'flattened':r.flattened,
                          'costUsd':round(cost,4),'cashUsd':round(r.cash,4),'pnlUsd':round(pnl,4),'rebateUsd':round(r.reb,5),
                          'perDollar':round(pnl/cost,4) if cost>0 else None})
        self.state['rounds']+=1;return 1
    # ---- report --------------------------------------------------------------------------------------
    def report(self):
        rows=list(self.done);fwd=[x for x in rows if x.get('fills') and float(x.get('end') or 0)>=REGISTERED_AT and x.get('perDollar') is not None]
        n=len(fwd);per=[x['perDollar'] for x in fwd];m=sum(per)/n if n else None
        sd=math.sqrt(sum((x-m)**2 for x in per)/(n-1)) if n>1 else None;lo=round(m-1.645*sd/math.sqrt(n),4) if sd else None
        allr=len(rows);filled=[x for x in rows if x.get('fills')]
        return {'enabled':self.enabled,**self.state,'openRounds':sum(1 for r in self.cur.values() if not r.closed),'awaitingResult':len(self.wait),
                'M2':{'rule':'quote both tokens for equal shares at the best bid (pair <= 99c), complete the pair after a fill, stop-loss 10c, flatten at 15% left; 250 ms latency, back of the queue',
                      'pass':f'>= {MIN_ROUNDS} rounds with a fill and mean P&L per $ - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'rounds':n,'avgPerDollar':None if m is None else round(m,4),'lower95':lo,'pnlUsd':round(sum(x['pnlUsd'] for x in fwd),3),
                      'rebateUsd':round(sum(x.get('rebateUsd') or 0 for x in fwd),4),
                      'verdict':'collecting' if n<MIN_ROUNDS else ('CONFIRMED' if lo is not None and lo>0 else 'not confirmed')},
                'roundsSeen':allr,'roundsFilled':len(filled),'fillRate':round(len(filled)/allr,3) if allr else None,
                'pairsCompleted':sum(1 for x in filled if (x.get('pairs') or 0)>=PAIR_SHARES-1e-6),
                'stoppedOut':sum(1 for x in filled if x.get('flattened')),'recent':filled[-8:][::-1]}
    def compact(self):
        r=self.report();r.pop('recent',None);return r
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'done':list(self.done),'state':self.state},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.done.extend(d.get('done') or [])
        for k in ('fills','flattens','rounds','requotes'):self.state[k]=int((d.get('state') or {}).get(k,0))
        return True
    async def loop(self):
        import asyncio
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err(exc)
        while True:
            await asyncio.sleep(120)
            try:
                now=time.time()
                for s in [s for s,r in self.wait.items() if now-r.end>3*3600]:self.wait.pop(s,None)   # never resolved
                self.save()
            except Exception as exc:self._err(exc)
