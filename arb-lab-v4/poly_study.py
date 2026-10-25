"""Market study: how do winning trades happen on the BTC/ETH up/down markets? (record-only; places no orders)

Fed by the trade tape: when a round resolves, every print of that round arrives labelled (token won or lost)
with the market context at that instant (time left, spot versus the round's opening price, our model).

For each round:
  * winner, volume, the UP price path, the moment the result became "decided" (winner >= 80c and staying),
  * the best entry anyone got on the winner (lowest taker BUY price) and what the market looked like then,
  * the biggest winning taker buys.
Across rounds, a PATTERN GRID of taker buys: share of the round left x price paid x whether spot was already
on that token's side ("with" / "flat" / "against"). Each cell keeps per-ROUND average returns after the taker
fee, so its uncertainty is clustered by round (prints in a round share one outcome).
"HOW IT SHOULD BE EXECUTED" (prequential): before learning from a round, the rule "buy the first print that falls
in a cell whose round-mean minus 3 SE > 0 over >= MIN_ROUNDS earlier rounds" is applied to that round, one trade
per round, and its result recorded. This tests whether the learned pattern would have worked on unseen rounds.
Wallets: per-round top winners from the wallet study (who bought the winner, how early, at what price).
"""
from __future__ import annotations
import gzip,json,math,os,time
from collections import defaultdict,deque

FEE_RATE=0.07;MIN_ROUNDS=20
# 180 cells are tested at once, so 2 SE would let several lucky cells through; 3 SE (~0.13% one-sided each)
# keeps the expected number of false 'winning' cells well below one.
Z=3.0
LEFT_EDGES=(0.,.1,.2,.4,.6,.8,1.0001)
LEFT_NAMES=('last 10%','10-20%','20-40%','40-60%','60-80%','first 20%')
PRICE_EDGES=tuple(i/10 for i in range(11))
def fee(p):return FEE_RATE*p*(1-p)

def left_bucket(f):
    if f is None:return None
    for i,(a,b) in enumerate(zip(LEFT_EDGES,LEFT_EDGES[1:])):
        if a<=f<b:return i
    return len(LEFT_NAMES)-1
def price_bucket(p):return min(9,max(0,int(float(p)*10)))
def agree_bucket(bps):
    if bps is None:return 'unknown'
    return 'with' if bps>2 else 'against' if bps<-2 else 'flat'
def buy_return(price,won):
    c=price+fee(price);return ((1. if won else 0.)-c)/c

class Cell:
    __slots__=('n','wins','rounds','s','ss','prints')
    def __init__(self):self.n=0;self.wins=0;self.rounds=0;self.s=0.;self.ss=0.;self.prints=0
    def add_round(self,rets,wins):
        m=sum(rets)/len(rets);self.rounds+=1;self.s+=m;self.ss+=m*m;self.prints+=len(rets);self.wins+=wins
    def mean(self):return self.s/self.rounds if self.rounds else None
    def se(self):
        if self.rounds<2:return None
        m=self.mean();v=max(0.,(self.ss-self.rounds*m*m)/(self.rounds-1));return math.sqrt(v/self.rounds)
    def lower(self):
        se=self.se();return None if se is None else self.mean()-Z*se
    def export(self):return [self.n,self.wins,self.rounds,self.s,self.ss,self.prints]
    @classmethod
    def load(cls,a):
        c=cls();c.n,c.wins,c.rounds,c.s,c.ss,c.prints=a;return c

class Study:
    def __init__(self):
        self.rounds=deque(maxlen=400);self.grid=defaultdict(Cell);self.rule=deque(maxlen=2000);self.wallet_rounds=deque(maxlen=200)
        self.learn_curve=deque(maxlen=600);self.totals={'rounds':0,'prints':0,'buys':0,'winnerUp':0,'winnerDown':0};self.started=time.time()
    @staticmethod
    def key(r):return (left_bucket(r.get('leftFrac')),price_bucket(r['price']),agree_bucket(r.get('agreeBps')))
    def qualifying(self,k):
        c=self.grid.get(k);lo=c.lower() if c else None
        return bool(c and c.rounds>=MIN_ROUNDS and lo is not None and lo>0)
    # ---- one resolved round ------------------------------------------------------------------------
    def on_round(self,rows,winner,learner=None):
        rows=sorted([r for r in rows if r.get('won') is not None],key=lambda r:float(r['t']))
        if not rows:return None
        lane=rows[0]['lane'];slug=rows[0]['slug'];end=rows[0].get('roundEnd');W=winner.upper()
        buys=[r for r in rows if str(r.get('aggressor')).upper()=='BUY']
        # 1) prequential rule: first qualifying taker buy, judged with the grid BEFORE this round is learned
        pick=next((r for r in buys if self.qualifying(self.key(r))),None)
        if pick:
            ret=buy_return(float(pick['price']),pick['won'])
            self.rule.append({'slug':slug,'lane':lane,'end':end,'token':pick['token'],'price':pick['price'],'secondsLeft':pick.get('secondsLeft'),
                              'agreeBps':pick.get('agreeBps'),'won':pick['won'],'returnPerDollar':round(ret,4),'cell':self.cell_name(self.key(pick))})
        # 2) learn: per-round mean return per cell
        per=defaultdict(list);wins=defaultdict(int)
        for r in buys:
            k=self.key(r);per[k].append(buy_return(float(r['price']),r['won']));wins[k]+=int(r['won'])
        for k,v in per.items():self.grid[k].add_round(v,wins[k])
        # 3) round summary
        up=[(r.get('secondsLeft'),float(r['price']) if r['token']=='UP' else 1-float(r['price'])) for r in rows if r.get('secondsLeft') is not None]
        step=max(1,len(up)//60);path=[(round(a,1),round(b,3)) for a,b in up[::step]]
        decided=None
        for sl,pu in reversed(up):
            pw=pu if W=='UP' else 1-pu
            if pw<0.8:break
            decided=sl
        wb=[r for r in buys if r['token']==W];lb=[r for r in buys if r['token']!=W]
        best=min(wb,key=lambda r:float(r['price']),default=None)
        top=sorted(wb,key=lambda r:-float(r['size'])*(1-float(r['price'])))[:5]
        vol=sum(float(r['price'])*float(r['size']) for r in rows)
        last_spot=next((r for r in reversed(rows) if r.get('spotVsOpenBps') is not None),None)
        summ={'slug':slug,'lane':lane,'end':end,'winner':W,'prints':len(rows),'buys':len(buys),'volumeUsd':round(vol,2),
              'winnerBuys':len(wb),'loserBuys':len(lb),'winnerBuyUsd':round(sum(float(r['price'])*float(r['size']) for r in wb),2),
              'loserBuyUsd':round(sum(float(r['price'])*float(r['size']) for r in lb),2),
              'decidedSecondsLeft':decided,'finalSpotVsOpenBps':last_spot.get('spotVsOpenBps') if last_spot else None,
              'bestEntry':{k:best.get(k) for k in ('price','secondsLeft','agreeBps','pToken','pLearned','size')} if best else None,
              'topWinningBuys':[{k:r.get(k) for k in ('price','size','secondsLeft','agreeBps','pToken')} for r in top],
              'path':path,'ruleTrade':self.rule[-1] if pick else None}
        self.rounds.append(summ)
        self.totals['rounds']+=1;self.totals['prints']+=len(rows);self.totals['buys']+=len(buys);self.totals['winner'+('Up' if W=='UP' else 'Down')]+=1
        if learner is not None:
            s=learner.summary();self.learn_curve.append({'round':learner.rounds,'end':end,'brierLearned':s.get('brierLearned'),'brierPrice':s.get('brierPrice'),'brierModel':s.get('brierModel')})
        return summ
    def on_wallet_round(self,lane,round_start,winner,per):
        """per: wallet -> {'pnl','buys','buyUsd','entrySec':[...],'prices':[...]} from the wallet study."""
        top=sorted(per.items(),key=lambda kv:-kv[1]['pnl'])[:6]
        self.wallet_rounds.append({'lane':lane,'roundStart':round_start,'winner':str(winner).upper(),'wallets':len(per),
            'top':[{'wallet':w[:6]+'…'+w[-4:],'pnl':round(a['pnl'],2),'buys':a['buys'],'buyUsd':round(a['buyUsd'],2),
                    'avgEntrySec':round(sum(a['entrySec'])/len(a['entrySec']),1) if a['entrySec'] else None,
                    'avgPrice':round(sum(a['prices'])/len(a['prices']),3) if a['prices'] else None} for w,a in top]})
    # ---- reporting ---------------------------------------------------------------------------------
    @staticmethod
    def cell_name(k):
        lb,pb,ag=k;return f"{LEFT_NAMES[lb] if lb is not None else '?'} left · {pb*10}-{pb*10+10}c · spot {ag}"
    def heatmap(self,agree):
        out=[]
        for lb in range(len(LEFT_NAMES)):
            row=[]
            for pb in range(10):
                c=self.grid.get((lb,pb,agree))
                row.append(None if not c or not c.rounds else {'mean':round(c.mean(),4),'lower':None if c.lower() is None else round(c.lower(),4),
                           'rounds':c.rounds,'prints':c.prints,'winPct':round(100*c.wins/c.prints,1) if c.prints else None})
            out.append(row)
        return out
    def patterns(self,n=12):
        cells=[(k,c) for k,c in self.grid.items() if c.rounds>=5]
        cells.sort(key=lambda kc:-(kc[1].lower() if kc[1].lower() is not None else -9))
        return [{'cell':self.cell_name(k),'rounds':c.rounds,'prints':c.prints,'winPct':round(100*c.wins/c.prints,1) if c.prints else None,
                 'avgReturnPerDollar':round(c.mean(),4),'lowerBound':None if c.lower() is None else round(c.lower(),4),
                 'qualifies':self.qualifying(k)} for k,c in cells[:n]]
    def rule_summary(self):
        r=list(self.rule);n=len(r)
        if not n:return {'trades':0,'note':f'the rule trades only once a cell has >= {MIN_ROUNDS} rounds and a positive lower bound'}
        rets=[x['returnPerDollar'] for x in r];m=sum(rets)/n
        sd=math.sqrt(sum((x-m)**2 for x in rets)/(n-1)) if n>1 else None
        return {'trades':n,'winPct':round(100*sum(x['won'] for x in r)/n,1),'avgReturnPerDollar':round(m,4),
                'lowerBound':round(m-2*sd/math.sqrt(n),4) if sd else None,'totalPer1DollarTrades':round(sum(rets),3),'recent':r[-10:]}
    def snapshot(self,tape=None,learner=None):
        rs=list(self.rounds)
        dec=[x['decidedSecondsLeft'] for x in rs if x.get('decidedSecondsLeft') is not None]
        best=[x['bestEntry']['price'] for x in rs if x.get('bestEntry')]
        out={'at':time.time(),'started':self.started,'mode':'study mode: all trading paused; every market trade recorded and studied',
             'totals':self.totals,'minRoundsForRule':MIN_ROUNDS,
             'summary':{'medianDecidedSecondsLeft':sorted(dec)[len(dec)//2] if dec else None,
                        'medianBestWinnerEntry':sorted(best)[len(best)//2] if best else None,
                        'winnerBuyShareOfTakerBuyUsd':round(sum(x['winnerBuyUsd'] for x in rs)/max(1e-9,sum(x['winnerBuyUsd']+x['loserBuyUsd'] for x in rs)),4) if rs else None},
             'heatmaps':{a:self.heatmap(a) for a in ('with','flat','against')},'leftNames':LEFT_NAMES,
             'patterns':self.patterns(),'rule':self.rule_summary(),'rounds':rs[-24:][::-1],'wallets':list(self.wallet_rounds)[-12:][::-1],
             'learnCurve':list(self.learn_curve)[-300:]}
        if tape is not None:
            out['live']=[{k:r.get(k) for k in ('t','lane','token','price','size','aggressor','pToken','pLearned','secondsLeft','agreeBps','action','learnedAction')} for r in list(tape.live)[-40:]][::-1]
            out['cloud']=[{'l':r.get('leftFrac'),'p':r['price'],'a':r.get('agreeBps'),'w':r.get('won'),'s':r.get('size')} for r in list(tape.recent)[-400:]]
            out['tape']=tape.compact()
        if learner is not None:out['learner']=learner.summary()
        return out
    # ---- persistence ---------------------------------------------------------------------------------
    def save(self,path):
        st={'rounds':list(self.rounds),'grid':{json.dumps(list(k)):c.export() for k,c in self.grid.items()},'rule':list(self.rule),
            'wallets':list(self.wallet_rounds),'curve':list(self.learn_curve),'totals':self.totals,'started':self.started}
        tmp=path+'.tmp'
        with gzip.open(tmp,'wt') as f:json.dump(st,f,separators=(',',':'),default=str)
        os.replace(tmp,path)
    def load(self,path):
        if not os.path.exists(path):return False
        with gzip.open(path,'rt') as f:st=json.load(f)
        self.rounds.extend(st.get('rounds') or []);self.rule.extend(st.get('rule') or []);self.wallet_rounds.extend(st.get('wallets') or [])
        self.learn_curve.extend(st.get('curve') or []);self.totals.update(st.get('totals') or {});self.started=st.get('started',self.started)
        for k,a in (st.get('grid') or {}).items():
            kk=json.loads(k);self.grid[(kk[0],kk[1],kk[2])]=Cell.load(a)
        return True
