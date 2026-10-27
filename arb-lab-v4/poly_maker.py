"""Maker lane M1 (record-only; places no orders): would resting bids on BTC/ETH up/down rounds make money?

Every live trade print on the tape drives a simulated resting BUY quote on BOTH tokens of each round (UP and DOWN).
Honest fills, using the order book and the trades that actually printed:
  * the quote is posted at the first print with <= 90% of the round left, sized $5, at the token's best bid,
  * queue position: we join the BACK of the queue, behind the size already resting at our price,
  * a later print on that token STRICTLY BELOW our price fills us (the book traded through us),
  * a SELL print AT our price eats the queue ahead of us first; only the size beyond it fills us (partial fills),
  * 0.5 s order latency; quotes are cancelled at 10% of the round left; nothing is filled after that.
Variants (all recorded on the same prints):
  join    - static quote at the best bid when posted,
  improve - static quote 1c above the best bid (front of the queue, pays 1c more; only if still below the ask),
  peg     - re-pegs to the current best bid whenever it changes (goes to the back of the queue each time),
  pair    - (added after M1 was registered; exploratory) EQUAL shares on UP and DOWN at their best bids, posted together
            only when the two bids cost <= 98c a pair, so a double fill locks in the spread,
  pairGuard - pair, but anything still resting is cancelled once either token's bid moves 10c from where it was posted.
Filled shares are held to resolution: P&L = shares x (1 if the token won else 0) - shares x price - maker fee.
Maker fee default 0 (POLY_MAKER_FEE_RATE to stress it); no rebate is assumed.
Both tokens filled in one round = spread captured (cost of UP + DOWN below $1).
Pre-registered test M1 (registered REGISTERED_AT): variant 'join', rounds ending after registration with any fill;
pass = >= 300 such rounds and mean round P&L per $ filled - 1.645 SE > 0.
Off with POLY_MAKER=0.
"""
from __future__ import annotations
import gzip,json,math,os,time
from collections import deque

VARIANTS=('join','improve','peg','pair','pairGuard')
PAIRS=('pair','pairGuard');PAIR_SHARES=5.0;PAIR_MAX_COST=0.98;GUARD_MOVE=0.10
STAKE=5.0;POST_AT=0.9;CANCEL_AT=0.1;LATENCY=0.5;MIN_ROUNDS=300
FEE_RATE=float(os.getenv('POLY_MAKER_FEE_RATE','0'))
REGISTERED_AT=1793071725   # 2026-10-27 03:28 UTC: forward results count only after this
CURRENT=None
def fee(p):return FEE_RATE*p*(1-p)

class Maker:
    def __init__(self,root=None):
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp')
        self.enabled=os.getenv('POLY_MAKER','1')!='0'
        self.q={}            # slug -> {variant -> {token -> quote}}
        self.done=deque(maxlen=20000);self.state={'prints':0,'rounds':0,'quotes':0,'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'maker_state.json.gz')
    @staticmethod
    def _books(row):
        """Best bid (and size) for both tokens at this print, from the tape row's book snapshot."""
        me=row['token'];other='DOWN' if me=='UP' else 'UP'
        return {me:(row.get('bid'),row.get('bidSize'),row.get('ask')),other:(row.get('otherBid'),row.get('otherBidSize'),row.get('otherAsk'))}
    def _post(self,quote_slot,tok,bid,size,ask,t,variant):
        if bid is None or not 0.01<=float(bid)<=0.98:return None
        bid=float(bid);ahead=float(size or 0)
        if variant in PAIRS:return {'token':tok,'price':bid,'ahead':ahead,'shares':PAIR_SHARES,'filled':0.,'cost':0.,'postedAt':t,'state':'resting','reposts':0}
        if variant=='improve':
            if ask is None or bid+0.01>=float(ask)-1e-9:return None   # would cross the spread: not a maker order
            bid=round(bid+0.01,2);ahead=0.
        self.state['quotes']+=1
        return {'token':tok,'price':bid,'ahead':ahead,'shares':round(STAKE/bid,4),'filled':0.,'cost':0.,'postedAt':t,'state':'resting','reposts':0}
    def on_print(self,row):
        if not self.enabled or not row.get('slug') or row.get('leftFrac') is None:return
        try:self._on_print(row)
        except Exception as exc:self.state['errors']+=1;self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:100]}'
    def _on_print(self,row):
        self.state['prints']+=1
        slug=row['slug'];lf=float(row['leftFrac']);t=float(row['t']);books=self._books(row)
        r=self.q.get(slug)
        if r is None:
            if lf>POST_AT or lf<CANCEL_AT:return
            r=self.q[slug]={'lane':row['lane'],'roundEnd':row.get('roundEnd'),'v':{v:{} for v in VARIANTS}}
            if len(self.q)>400:self.q.pop(next(iter(self.q)))
        # post any quote not yet posted (a token's book may only appear on a later print)
        if lf>=CANCEL_AT:
            for v in PAIRS:   # both legs at once, or not at all
                if r['v'][v] or r.get('pairTried'):continue
                (b1,s1,_),(b2,s2,_)=books['UP'],books['DOWN']
                if b1 is None or b2 is None:continue
                r['pairTried']=v=='pairGuard'
                if float(b1)+float(b2)<=PAIR_MAX_COST and 0.01<=float(b1)<=0.98 and 0.01<=float(b2)<=0.98:
                    for tok,(bid,size,ask) in books.items():
                        qq=self._post(r,tok,bid,size,ask,t,v)
                        if qq:r['v'][v][tok]=qq
            for v in VARIANTS:
                if v in PAIRS:continue
                for tok,(bid,size,ask) in books.items():
                    if tok not in r['v'][v]:
                        qq=self._post(r,tok,bid,size,ask,t,v)
                        if qq:r['v'][v][tok]=qq
        # fills from this print (only the printed token's quotes)
        tok=row['token'];price=float(row['price']);size=float(row.get('size') or 0);side=str(row.get('aggressor') or '').upper()
        for v in VARIANTS:
            q=r['v'][v].get(tok)
            if not q or q['state']!='resting':continue
            if lf<CANCEL_AT:q['state']='cancelled';continue
            if t<q['postedAt']+LATENCY:continue
            left=q['shares']-q['filled']
            if price<q['price']-1e-9:fill=left                                  # traded through our level
            elif abs(price-q['price'])<1e-9 and side=='SELL':                   # sold into the bid at our level
                eat=min(q['ahead'],size);q['ahead']-=eat;fill=min(left,size-eat)
            else:fill=0.
            if fill>0:
                q['filled']+=fill;q['cost']+=fill*q['price']
                if q['filled']>=q['shares']-1e-9:q['state']='filled'
            # peg: follow the current best bid when it moves (back of the queue at the new level)
            if v=='peg' and q['state']=='resting':
                bid,bsize,_=books.get(tok,(None,None,None))
                if bid is not None and abs(float(bid)-q['price'])>1e-9 and 0.01<=float(bid)<=0.98:
                    # filled shares keep their price; the remainder moves
                    q['price']=float(bid);q['ahead']=float(bsize or 0);q['postedAt']=t;q['reposts']+=1
                    q['shares']=q['filled']+round((STAKE-q['cost'])/float(bid),4) if STAKE>q['cost'] else q['filled']
        # pairGuard: cancel what is still resting once either token's bid has moved GUARD_MOVE from its post
        g=r['v'].get('pairGuard') or {}
        for tk,q in g.items():
            bid=(books.get(tk) or (None,))[0]
            if q['state']=='resting' and bid is not None and abs(float(bid)-q['price'])>=GUARD_MOVE-1e-9:
                for q2 in g.values():
                    if q2['state']=='resting':q2['state']='cancelled'
                break
        # cancel everything once the round is nearly over
        if lf<CANCEL_AT:
            for v in VARIANTS:
                for q in r['v'][v].values():
                    if q['state']=='resting':q['state']='cancelled'
    def settle(self,slug,winner):
        r=self.q.pop(slug,None)
        if not r or winner not in ('up','down'):return 0
        W=winner.upper();out={'slug':slug,'lane':r['lane'],'roundEnd':r['roundEnd'],'winner':W,'v':{}}
        for v in VARIANTS:
            qs=r['v'][v];pnl=0.;cost=0.;fills=0
            for tok,q in qs.items():
                if q['filled']<=0:continue
                fills+=1;won=tok==W;cost+=q['cost']
                pnl+=q['filled']*(1. if won else 0.)-q['cost']-fee(q['cost']/q['filled'])*q['filled']
            both=all(q['filled']>0 for q in qs.values()) and len(qs)==2
            out['v'][v]={'quotes':len(qs),'fills':fills,'both':both,'costUsd':round(cost,4),'pnlUsd':round(pnl,4),
                         'perDollar':round(pnl/cost,4) if cost>0 else None,
                         'fillPrices':{tok:round(q['cost']/q['filled'],3) for tok,q in qs.items() if q['filled']>0}}
        self.done.append(out);self.state['rounds']+=1;return 1
    # ---- report --------------------------------------------------------------------------------------
    @staticmethod
    def _sum(rows,v):
        rs=[r['v'][v] for r in rows if r['v'].get(v)]
        filled=[x for x in rs if x['costUsd']>0];n=len(filled)
        per=[x['perDollar'] for x in filled];m=sum(per)/n if n else None
        sd=math.sqrt(sum((x-m)**2 for x in per)/(n-1)) if n>1 else None
        tot=sum(x['pnlUsd'] for x in filled);cost=sum(x['costUsd'] for x in filled)
        return {'rounds':len(rs),'roundsFilled':n,'fillRate':round(n/len(rs),3) if rs else None,
                'bothFilled':sum(1 for x in rs if x['both']),'pnlUsd':round(tot,3),'costUsd':round(cost,2),
                'perDollarPooled':round(tot/cost,4) if cost else None,'perDollarMean':None if m is None else round(m,4),
                'lower95':round(m-1.645*sd/math.sqrt(n),4) if sd else None}
    def report(self):
        rows=list(self.done);fwd=[r for r in rows if float(r.get('roundEnd') or 0)>=REGISTERED_AT]
        f=self._sum(fwd,'join')
        verdict='collecting' if f['roundsFilled']<MIN_ROUNDS else ('CONFIRMED' if (f['lower95'] or -1)>0 else 'not confirmed')
        lanes={}
        for r in rows:lanes.setdefault(r['lane'],[]).append(r)
        return {'enabled':self.enabled,**self.state,'openRounds':len(self.q),'feeRate':FEE_RATE,
                'M1':{'rule':'resting bids at the best bid on both tokens, back of the queue, fills only on trade-through or after the queue ahead is eaten; hold to resolution',
                      'pass':f'>= {MIN_ROUNDS} forward rounds with a fill and mean P&L per $ - 1.645 SE > 0','registeredAt':REGISTERED_AT,'forward':f,'verdict':verdict},
                'variants':{v:self._sum(rows,v) for v in VARIANTS},
                'byLane':{l:self._sum(rs,'join') for l,rs in sorted(lanes.items())},
                'recent':rows[-8:][::-1]}
    def compact(self):
        r=self.report();r.pop('recent',None);return r
    # ---- persistence ----------------------------------------------------------------------------------
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'done':list(self.done),'state':self.state},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.done.extend(d.get('done') or [])
        for k in ('rounds','quotes'):self.state[k]=int((d.get('state') or {}).get(k,0))
        return True
    async def loop(self):
        import asyncio
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self.state['lastError']=f'load: {type(exc).__name__}'
        while True:
            await asyncio.sleep(120)
            try:self.save()
            except Exception as exc:self.state['lastError']=f'save: {type(exc).__name__}'
