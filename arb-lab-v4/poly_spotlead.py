"""S2 spot-lead taker (paper only; places no real orders).

Finding (offline, recorded tape 25-30 Oct 2026, 3,469 rounds, fit on the first 60% of rounds by time, judged on the last
40% only): on the 5-minute BTC/ETH markets the live spot move since the round opened carries information the token
price does not. Logistic fit on train:  logit P(token wins) = A*logit(mid) + B*z + C,  z = spot move in bps on this
token's side / sqrt(seconds left). 5m: A=1.222 (+-0.018), B=0.2170 (+-0.056), C=-0.340. On the held-out days the model's
error score beat the mid by 2.2%; buying the first print per round and token where p - ask - fee >= 1c returned +2.6c
per $ over 791 rounds (lower 95% +0.8c), positive on both full test days and on BTC and ETH; without spot the same rule
fires 13 times. 15-minute lanes showed no spot effect (B -0.11 +-0.10) and are excluded. Eight settings were tried,
so this is a hypothesis to confirm forward, with every number below frozen before the test.

Rule (frozen): 5m BTC/ETH only; on every recorded print with a live book (0 < bid < ask < 1, spread <= 5c, more than 2 s
left, spot known) compute p for that token; the first time p - ask - fee(ask) >= THR in a round for a token, buy it at
the ask; hold to settlement. Costs: taker fee 0.07 p(1-p); the test is judged with a further STRESS (0.5c) on the price
for latency. Pre-registered S2 (from REGISTERED_AT): pass = >= 300 rounds traded and mean return per $ (stressed)
- 1.645 SE > 0.  A $200 paper wallet takes each signal with a fixed $5 stake; hard floor WALLET_FLOOR equity.
Off with POLY_SPOTLEAD=0.
"""
from __future__ import annotations
import gzip,json,math,os,time
from collections import deque

A,B,C=1.222,0.2170,-0.340
LANES=('BTC','ETH');THR=0.01;STRESS=0.005;MAX_SPREAD=0.05;MIN_SECS=2.0;MIN_ROUNDS=300
FEE_RATE=0.07;WALLET_BALANCE=200.0;STAKE=5.0;WALLET_FLOOR=120.0
REGISTERED_AT=float(os.getenv('POLY_SPOTLEAD_REGISTERED_AT') or 1793394000)   # 2026-10-30 21:00 UTC
CURRENT=None
def fee(p):return FEE_RATE*p*(1-p)
def _lg(p):p=min(max(p,1e-4),1-1e-4);return math.log(p/(1-p))
def _f(x):
    try:return None if x is None or x=='' else float(x)
    except (TypeError,ValueError):return None

def prob(token,bid,ask,bps,secs):
    """Model probability that `token` ('UP'/'DOWN') wins. Pure, for testing."""
    z=(bps if str(token).upper().startswith('U') else -bps)/math.sqrt(secs)
    x=A*_lg((bid+ask)/2)+B*z+C
    return 1/(1+math.exp(-x))

def signal(row):
    """(p, ask) when this print qualifies, else None. Pure, for testing."""
    if row.get('lane') not in LANES:return None
    bid,ask,bps,secs,price=_f(row.get('bid')),_f(row.get('ask')),_f(row.get('spotVsOpenBps')),_f(row.get('secondsLeft')),_f(row.get('price'))
    if None in (bid,ask,bps,secs,price) or secs<=MIN_SECS or not 0.02<=price<=0.98:return None
    if not (0<bid<ask<1) or ask-bid>MAX_SPREAD:return None
    p=prob(row.get('token'),bid,ask,bps,secs)
    return (p,ask) if p-ask-fee(ask)>=THR else None

class SpotLead:
    def __init__(self,root=None):
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_SPOTLEAD','1')!='0'
        self.open={};self.taken=set();self.done=deque(maxlen=20000)
        self.state={'prints':0,'signals':0,'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'spotlead_state.json.gz')
    def _err(self,exc):self.state['errors']+=1;self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:100]}'
    def equity(self):return WALLET_BALANCE+sum(d['pnlUsd'] for d in self.done)
    def on_print(self,row):
        if not self.enabled:return
        try:
            self.state['prints']+=1
            key=(row.get('slug'),str(row.get('token') or '').upper())
            if not key[0] or key in self.taken:return
            s=signal(row)
            if not s:return
            p,ask=s;self.taken.add(key);self.state['signals']+=1
            if len(self.taken)>4000:self.taken=set(list(self.taken)[-2000:])
            stake=STAKE if self.equity()-sum(o['costUsd'] for o in self.open.values())>WALLET_FLOOR+STAKE else 0.
            c=ask+fee(ask);sh=round(stake/c,4) if stake else 0.
            self.open[f'{key[0]}|{key[1]}']={'slug':key[0],'lane':row.get('lane'),'token':key[1],'t':round(float(row.get('t') or time.time()),3),
                'p':round(p,4),'ask':ask,'bid':_f(row.get('bid')),'bps':_f(row.get('spotVsOpenBps')),'secs':_f(row.get('secondsLeft')),
                'shares':sh,'costUsd':round(sh*c,4),'end':row.get('roundEnd')}
        except Exception as exc:self._err(exc)
    def settle(self,slug,winner):
        n=0
        for k in [k for k,o in self.open.items() if o['slug']==slug]:
            o=self.open.pop(k)
            if winner not in ('up','down'):continue
            won=o['token'].startswith(winner[0].upper());pay=1. if won else 0.
            c=o['ask']+fee(o['ask']);cs=min(.999,o['ask']+STRESS)+fee(o['ask'])
            self.done.append({**o,'winner':winner.upper(),'won':won,'ret':round((pay-c)/c,5),'retStressed':round((pay-cs)/cs,5),
                              'pnlUsd':round(o['shares']*(pay-c),4)});n+=1
        return n
    @staticmethod
    def _stats(xs):
        n=len(xs)
        if not n:return {'n':0}
        m=sum(xs)/n;sd=math.sqrt(sum((x-m)**2 for x in xs)/(n-1)) if n>1 else None
        return {'n':n,'mean':round(m,5),'lower95':round(m-1.645*sd/math.sqrt(n),5) if sd else None,'winPct':round(100*sum(1 for x in xs if x>0)/n,1)}
    def wallet(self):
        rows=list(self.done);pnl=sum(r['pnlUsd'] for r in rows if r['shares']);traded=[r for r in rows if r['shares']]
        oc=sum(o['costUsd'] for o in self.open.values());wins=sum(1 for r in traded if r['pnlUsd']>0)
        eq=0.;peak=0.;dd=0.
        for r in traded:eq+=r['pnlUsd'];peak=max(peak,eq);dd=max(dd,peak-eq)
        fl={}
        for r in traded:
            a=fl.setdefault(r['lane'],{'lane':r['lane'],'strategy':'SPOT_LEAD','trades':0,'wins':0,'losses':0,'pnl':0.,'won':0.,'lost':0.})
            a['trades']+=1;a['pnl']+=r['pnlUsd']
            if r['pnlUsd']>0:a['wins']+=1;a['won']+=r['pnlUsd']
            else:a['losses']+=1;a['lost']+=-r['pnlUsd']
        return {'startingBalance':WALLET_BALANCE,'stakeUsd':STAKE,'floorUsd':WALLET_FLOOR,'equity':round(WALLET_BALANCE+pnl,2),'balance':round(WALLET_BALANCE+pnl-oc,2),
                'pnlUsd':round(pnl,2),'trades':len(traded),'wins':wins,'winRate':round(100*wins/len(traded),1) if traded else None,'openRounds':len(self.open),
                'openCostUsd':round(oc,2),'maxDrawdownUsd':round(dd,2),'guard':None if WALLET_BALANCE+pnl>WALLET_FLOOR+STAKE else f'hard floor ${WALLET_FLOOR:.0f}: no new stakes',
                'flows':[{**a,'pnl':round(a['pnl'],3),'won':round(a['won'],3),'lost':round(a['lost'],3)} for a in fl.values()],
                'open':list(self.open.values())[-8:],
                'recent':[{k:r.get(k) for k in ('t','lane','token','ask','p','won','pnlUsd','retStressed')} for r in rows[-20:][::-1]]}
    def report(self):
        fwd=[r for r in self.done if r['t']>=REGISTERED_AT];s=self._stats([r['retStressed'] for r in fwd]);u=self._stats([r['ret'] for r in fwd])
        v='collecting' if s['n']<MIN_ROUNDS else ('CONFIRMED' if (s.get('lower95') or -1)>0 else 'not confirmed')
        by={ln:self._stats([r['retStressed'] for r in fwd if r['lane']==ln]) for ln in LANES}
        return {'enabled':self.enabled,**self.state,'model':{'A':A,'B':B,'C':C,'thr':THR,'lanes':list(LANES)},
                'S2':{'rule':'5m BTC/ETH: first print per round and token where p(spot model) - ask - fee >= 1c; buy at the ask; hold to settlement',
                      'pass':f'>= {MIN_ROUNDS} trades and mean return per $ with 0.5c latency stress - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'stressed':s,'unstressed':u,'byLane':by,'verdict':v},
                'wallet':self.wallet()}
    def compact(self):
        r=self.report();r['wallet']={k:v for k,v in r['wallet'].items() if k not in ('open','recent','flows')};return r
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'done':list(self.done),'open':self.open,'state':self.state},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.done.extend(d.get('done') or []);self.open.update(d.get('open') or {})
        for k in ('prints','signals'):self.state[k]=int((d.get('state') or {}).get(k,0))
        self.taken.update(tuple(k.split('|',1)) for k in self.open);return True
    async def loop(self):
        import asyncio
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err(exc)
        while True:
            await asyncio.sleep(120)
            try:
                now=time.time()
                for k in [k for k,o in self.open.items() if o.get('end') and now-float(o['end'])>3*3600]:self.open.pop(k,None)
                self.save()
            except Exception as exc:self._err(exc)
