"""Pair accumulator R1 (record-only; places no orders): a paper replica of the behaviour observed in the public
trades of @GoodFallen (rival study, 144 settled 5m rounds: both sides in 99% of rounds, average pair cost 0.972,
~74% taker fills, legs a median 14 s apart, buying through the whole window in ~16-share clips, 35 fills a round,
no sells/merges; +$1,366 after taker fees, before any taker rebate). Their code was not used or bought.

Rule (inferred from that evidence; parameters fixed here before the forward test):
  * every order-book change on a live 5m lane, after START_AT of the window and before STOP_AT:
    - flat: take a CLIP of the side whose ask <= FIRST_MAX (the cheaper side),
    - holding: take a CLIP of side X when ask_X + (average cost of the other side) <= TARGET, i.e. the pair
      formed with what we already hold costs at most TARGET, and X is not already ahead by more than MAX_IMBALANCE,
    - a clip fills at the current ask, never more than the size shown there, after a LATENCY cool-down per side,
  * at most MAX_SHARES per side per round; hold everything to settlement.
Costs: taker fee 0.07 p (1 - p) per share. Results are reported at our real tier (no taker rebate) and, for
comparison only, at a Gold-tier 18% taker rebate (which needs $200k+ monthly weighted volume).
Variants: 'replica' TARGET 0.97 (their observed average), 'strict' TARGET 0.95, and 'wallet': the same rule sized
for a $200 PAPER WALLET ($1 clips, at most $6 bought per side per round, at most $3 lopsided), with the main
account's guards: no new entries after an $8 loss since the wallet's start, and a 1 h pause after giving back half of
a gain of $4 or more. The wallet keeps a running balance and a trade list for the terminal.
Pre-registered R1 (REGISTERED_AT, variant replica, no rebate): pass = >= 300 rounds with fills and mean round
P&L per $ - 1.645 SE > 0.  Off with POLY_PAIRACC=0.
"""
from __future__ import annotations
import gzip,json,math,os,time
from collections import deque

CLIP=5.0;MAX_SHARES=60.;MAX_IMBALANCE=20.;FIRST_MAX=0.50;START_AT=0.02;STOP_AT=0.95;LATENCY=0.25
# per variant: pair-cost target and sizing, in shares ('sh') or dollars of cost ('usd')
VARIANTS={'replica':{'target':0.97,'unit':'sh','clip':CLIP,'max':MAX_SHARES,'imb':MAX_IMBALANCE},
          'strict':{'target':0.95,'unit':'sh','clip':CLIP,'max':MAX_SHARES,'imb':MAX_IMBALANCE},
          'wallet':{'target':0.97,'unit':'usd','clip':1.0,'max':6.0,'imb':3.0}}
WALLET_BALANCE=200.0;WALLET_LOSS_LIMIT=8.0;GIVEBACK_ARM=4.0;GIVEBACK_FRACTION=0.5;GIVEBACK_PAUSE=3600.
WALLET_START=float(os.getenv('POLY_PAIRACC_WALLET_START') or 1793087927)   # owner fresh start, 2026-10-27 07:58 UTC
FEE_RATE=0.07;GOLD_REBATE=0.18;MIN_ROUNDS=300
LANES=('BTC','ETH')                       # the 5-minute lanes, as observed
REGISTERED_AT=1793087467   # 2026-10-27 07:51 UTC: forward results count only after this
CURRENT=None
def fee(p):return FEE_RATE*p*(1-p)

class Book:
    __slots__=('inv','last','fills','fees')
    def __init__(self):self.inv={'up':[0.,0.],'down':[0.,0.]};self.last={'up':-9.,'down':-9.};self.fills=0;self.fees=0.

class PairAcc:
    def __init__(self,root=None):
        self.root=root or os.getenv('POLY_DATA_DIR','/tmp');self.enabled=os.getenv('POLY_PAIRACC','1')!='0'
        self.cur={};self.wait={};self.done=deque(maxlen=20000)
        self.state={'bookEvents':0,'fills':0,'rounds':0,'errors':0,'lastError':None}
    def path(self):return os.path.join(self.root,'pairacc_state.json.gz')
    # ---- the $200 paper wallet -----------------------------------------------------------------------
    def wallet_rows(self):
        return [r for r in self.done if r.get('filled') and (r.get('v') or {}).get('wallet') and float(r.get('end') or 0)>=WALLET_START]
    def wallet_pnl(self):return sum(r['v']['wallet']['pnlUsd'] for r in self.wallet_rows())
    def wallet_open(self,t=None):
        """Guards (same limits as the main paper account): loss limit since the wallet's start, and give-back pause."""
        t=time.time() if t is None else t
        if t<WALLET_START:return False
        pnl=self.wallet_pnl();self.wpeak=max(getattr(self,'wpeak',0.),pnl)
        if pnl<=-WALLET_LOSS_LIMIT:self.wguard=f'loss limit: {pnl:+.2f} since the wallet started; no new entries';return False
        if t<getattr(self,'wpause',0.):return False
        if self.wpeak>=GIVEBACK_ARM and pnl<=self.wpeak*GIVEBACK_FRACTION:
            self.wpause=t+GIVEBACK_PAUSE;self.wpeak=pnl;self.wguard=f'gave back half of the gain: paused 1 h at {pnl:+.2f}';return False
        self.wguard=None;return True
    def wallet(self):
        rows=self.wallet_rows();pnl=self.wallet_pnl();fees=sum(r['v']['wallet']['feesUsd'] for r in rows)
        open_cost=0.;opens=[]
        for r in list(self.cur.values())+list(self.wait.values()):
            b=r['v'].get('wallet')
            if b and b.fills:
                c=b.inv['up'][1]+b.inv['down'][1];open_cost+=c
                opens.append({'slug':r['slug'],'lane':r['lane'],'up':round(b.inv['up'][0],2),'down':round(b.inv['down'][0],2),'costUsd':round(c,2)})
        wins=sum(1 for r in rows if r['v']['wallet']['pnlUsd']>0)
        return {'startedAt':WALLET_START,'startingBalance':WALLET_BALANCE,'balance':round(WALLET_BALANCE+pnl-open_cost,2),
                'equity':round(WALLET_BALANCE+pnl,2),'pnlUsd':round(pnl,2),'feesUsd':round(fees,2),'trades':len(rows),'wins':wins,
                'winRate':round(100*wins/len(rows),1) if rows else None,'openCostUsd':round(open_cost,2),'open':opens,
                'guard':getattr(self,'wguard',None) or ('paused until '+time.strftime('%H:%M UTC',time.gmtime(self.wpause)) if time.time()<getattr(self,'wpause',0.) else None),
                'limits':{'lossLimit':WALLET_LOSS_LIMIT,'perSideMaxUsd':VARIANTS['wallet']['max'],'clipUsd':VARIANTS['wallet']['clip']},
                'recent':[{'slug':r['slug'],'lane':r['lane'],'end':r['end'],'winner':r.get('winner'),**{k:r['v']['wallet'][k] for k in ('up','down','pairCost','fills','costUsd','feesUsd','pnlUsd')}} for r in rows[-20:][::-1]]}
    def _err(self,exc):self.state['errors']+=1;self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:100]}'
    def _round(self,lane,market):
        slug=(market or {}).get('slug');end=(market or {}).get('endTs')
        if not slug or not end:return None
        r=self.cur.get(lane)
        if r is None or r['slug']!=slug:
            if r is not None:self._park(r)
            r=self.cur[lane]={'slug':slug,'lane':lane,'end':float(end),'v':{v:Book() for v in VARIANTS}}
        return r
    def _park(self,r):
        if any(b.fills for b in r['v'].values()):self.wait[r['slug']]=r
        else:self.done.append({'slug':r['slug'],'lane':r['lane'],'end':r['end'],'filled':False})
    @staticmethod
    def clip_shares(cfg,a):return cfg['clip'] if cfg['unit']=='sh' else round(cfg['clip']/a,2)
    @staticmethod
    def decide(b,bbo,target,cfg=None):
        """Which side to take now (or None), given inventory b and the book. Pure, for testing."""
        cfg=cfg or {'target':target,'unit':'sh','clip':CLIP,'max':MAX_SHARES,'imb':MAX_IMBALANCE}
        best=None
        for side,other in (('up','down'),('down','up')):
            a=(bbo.get(side) or {}).get('ask')
            if a is None or not 0.01<=float(a)<=0.99:continue
            a=float(a);sh,cost=b.inv[side];osh,ocost=b.inv[other];n=PairAcc.clip_shares(cfg,a)
            if cfg['unit']=='sh':
                if sh+n>cfg['max']+1e-9 or sh-osh>=cfg['imb']-1e-9:continue
            elif cost+n*a>cfg['max']+1e-9 or (cost-ocost)>=cfg['imb']-1e-9:continue
            if sh==0 and osh==0:ok=a<=FIRST_MAX
            elif osh>0:ok=a+ocost/osh<=target+1e-9
            else:ok=False                                     # only one leg so far and it is this one: wait for the other
            if ok and (best is None or a<best[1]):best=(side,a)
        return best
    def on_book(self,lane,side,bbo,market,t=None):
        if not self.enabled or lane not in LANES:return
        try:
            t=time.time() if t is None else t;r=self._round(lane,market)
            if r is None:return
            self.state['bookEvents']+=1
            frac=1-(r['end']-t)/300.
            if frac<START_AT or frac>STOP_AT:return
            for v,cfg in VARIANTS.items():
                if v=='wallet' and not self.wallet_open(t):continue
                b=r['v'][v];pick=self.decide(b,bbo,cfg['target'],cfg)
                if not pick:continue
                s,a=pick
                if t-b.last[s]<LATENCY:continue
                size=float(((bbo.get(s) or {}).get('askSize')) or 0)
                n=self.clip_shares(cfg,a)
                n=min(n,size,(cfg['max']-b.inv[s][0]) if cfg['unit']=='sh' else n)
                if n<1 or n*a<0.999:continue                 # Polymarket's $1 minimum order
                b.inv[s][0]+=n;b.inv[s][1]+=n*a;b.fees+=n*fee(a);b.fills+=1;b.last[s]=t;self.state['fills']+=1
        except Exception as exc:self._err(exc)
    def settle(self,slug,winner):
        for lane,r in list(self.cur.items()):
            if r['slug']==slug:self._park(r);self.cur.pop(lane,None)
        r=self.wait.pop(slug,None)
        if r is None or winner not in ('up','down'):return 0
        out={'slug':slug,'lane':r['lane'],'end':r['end'],'filled':True,'winner':winner.upper(),'v':{}}
        for v,b in r['v'].items():
            cost=b.inv['up'][1]+b.inv['down'][1]
            if cost<=0:out['v'][v]=None;continue
            pay=b.inv[winner][0];gross=pay-cost
            vu=b.inv['up'][1]/b.inv['up'][0] if b.inv['up'][0] else None;vd=b.inv['down'][1]/b.inv['down'][0] if b.inv['down'][0] else None
            out['v'][v]={'up':round(b.inv['up'][0],2),'down':round(b.inv['down'][0],2),'pairCost':round(vu+vd,4) if (vu and vd) else None,
                         'fills':b.fills,'costUsd':round(cost,4),'feesUsd':round(b.fees,4),'pnlUsd':round(gross-b.fees,4),
                         'pnlGoldUsd':round(gross-b.fees*(1-GOLD_REBATE),4),'perDollar':round((gross-b.fees)/cost,4)}
        self.done.append(out);self.state['rounds']+=1;return 1
    def _sum(self,rows,v,key='pnlUsd'):
        xs=[r['v'][v] for r in rows if r.get('filled') and r['v'].get(v)]
        n=len(xs);per=[x['perDollar'] for x in xs];m=sum(per)/n if n else None
        sd=math.sqrt(sum((x-m)**2 for x in per)/(n-1)) if n>1 else None
        pcs=[x['pairCost'] for x in xs if x['pairCost'] is not None]
        return {'rounds':n,'perDollar':None if m is None else round(m,4),'lower95':round(m-1.645*sd/math.sqrt(n),4) if sd else None,
                'pnlUsd':round(sum(x['pnlUsd'] for x in xs),3),'pnlGoldUsd':round(sum(x['pnlGoldUsd'] for x in xs),3),
                'feesUsd':round(sum(x['feesUsd'] for x in xs),3),'bothSidesPct':round(100*len(pcs)/n,1) if n else None,
                'avgPairCost':round(sum(pcs)/len(pcs),4) if pcs else None,'winRoundsPct':round(100*sum(1 for x in xs if x['pnlUsd']>0)/n,1) if n else None}
    def report(self):
        rows=list(self.done);fwd=[r for r in rows if float(r.get('end') or 0)>=REGISTERED_AT]
        f=self._sum(fwd,'replica')
        return {'enabled':self.enabled,**self.state,'openRounds':len(self.cur),'awaitingResult':len(self.wait),
                'R1':{'rule':'replica of the observed rival pattern: take 5-share clips at the ask of whichever side keeps the pair cost <= 0.97; first leg <= 50c; through 2-95% of each 5m window; hold to settlement',
                      'pass':f'>= {MIN_ROUNDS} rounds with fills and mean P&L per $ (no taker rebate) - 1.645 SE > 0','registeredAt':REGISTERED_AT,
                      'forward':f,'verdict':'collecting' if f['rounds']<MIN_ROUNDS else ('CONFIRMED' if (f['lower95'] or -1)>0 else 'not confirmed')},
                'variants':{v:self._sum(rows,v) for v in VARIANTS},'wallet':self.wallet(),'recent':[r for r in rows if r.get('filled')][-6:][::-1]}
    def compact(self):
        r=self.report();r.pop('recent',None);r['wallet']={k:v for k,v in r['wallet'].items() if k not in ('recent','open')};return r
    def save(self):
        tmp=self.path()+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump({'done':list(self.done),'state':self.state},f,separators=(',',':'))
        os.replace(tmp,self.path())
    def load(self):
        if not os.path.exists(self.path()):return False
        with gzip.open(self.path(),'rt') as f:d=json.load(f)
        self.done.extend(d.get('done') or []);self.state['rounds']=int((d.get('state') or {}).get('rounds',0));return True
    async def loop(self):
        import asyncio
        global CURRENT;CURRENT=self
        try:self.load()
        except Exception as exc:self._err(exc)
        while True:
            await asyncio.sleep(120)
            try:
                now=time.time()
                for s in [s for s,r in self.wait.items() if now-r['end']>3*3600]:self.wait.pop(s,None)
                self.save()
            except Exception as exc:self._err(exc)
