"""Online learner that corrects the MARKET PRICE using our model, learned from every settled trade print.

Why a correction on the market price: the traded price is already a strong probability estimate (on settled
bets the market's Brier beats our raw model's), so the useful question is when and by how much our model
should move away from it. The learner is a logistic regression
    logit P(token wins) = w . x,   x = [1, logit(price), logit(p_model) - logit(price), time-left share,
                                        time-left x logit(price), disagreement x time-left, log size, aggressor,
                                        lane one-hot]
Starting weights reproduce the market price exactly (w = logit(price) coefficient 1, rest 0), so it can only
improve on the market if the data says so.

Honest scoring (prequential): every round is scored with the weights learned BEFORE that round resolved, then
learned from. Prints in a round share one outcome, so each round carries the same total weight regardless of
how many prints it had (weight 1/n per print): rounds, not prints, are the sample size. Reported: Brier of the
market price, of our raw model and of the learner on the same prints, per round, with a block bootstrap of the
learner-minus-price difference.
"""
from __future__ import annotations
import math

LANES=('BTC','ETH','BTC15','ETH15')
NAMES=('bias','logitPrice','modelMinusPrice','timeLeft','timeLeftXlogitPrice','disagreeXtimeLeft','logSize','aggressorBuy','spotAgree','spotAgreeXlate')+tuple('lane_'+l for l in LANES)
EPS=1e-4

def logit(p):p=min(1-EPS,max(EPS,float(p)));return math.log(p/(1-p))
def sigmoid(z):return 1/(1+math.exp(-max(-30.,min(30.,z))))

def features(row):
    price=float(row['price']);lp=logit(price)
    pm=row.get('pToken');d=(logit(pm)-lp) if pm is not None else 0.
    interval=900. if str(row.get('lane','')).endswith('15') else 300.
    end=row.get('roundEnd');t=float(row.get('t') or 0)
    left=min(1.,max(0.,(float(end)-t)/interval)) if end else .5
    ag=row.get('agreeBps');ag=max(-3.,min(3.,float(ag)/20.)) if ag is not None else 0.   # spot vs open, signed toward this token
    x=[1.,lp,d,left,left*lp,d*left,math.log1p(max(0.,float(row.get('size') or 0)))/5,1. if str(row.get('aggressor')).upper()=='BUY' else 0.,ag,ag*(1-left)]
    x+=[1. if row.get('lane')==l else 0. for l in LANES]
    return x

class Learner:
    def __init__(self,lr=0.1,l2=1e-3):
        self.w=[0.]*len(NAMES);self.w[1]=1.0           # = market price
        self.g2=[1e-8]*len(NAMES);self.lr=lr;self.l2=l2
        self.rounds=0;self.prints=0
        self.score={'price':0.,'model':0.,'learned':0.,'modelRounds':0,'w':0.}   # round-weighted Brier sums
        self.round_diffs=[]                                                       # (learned - price) Brier per round
        self.recent=[]                                                            # (roundEnd, learned-price diff, model-price diff)
    def predict(self,row):return sigmoid(sum(a*b for a,b in zip(self.w,features(row))))
    def learn_round(self,rows):
        """Score the round with the current weights (prequential), then update. rows carry 'won'."""
        rows=[r for r in rows if r.get('won') is not None and r.get('price') is not None]
        if not rows:return None
        n=len(rows);bp=bl=bm=0.;nm=0
        for r in rows:
            y=1. if r['won'] else 0.;q=float(r['price']);pl=self.predict(r)
            bp+=(q-y)**2;bl+=(pl-y)**2
            if r.get('pToken') is not None:bm+=(float(r['pToken'])-y)**2;nm+=1
        bp/=n;bl/=n
        self.score['price']+=bp;self.score['learned']+=bl;self.score['w']+=1
        if nm:self.score['model']+=bm/nm;self.score['modelRounds']+=1
        self.round_diffs.append(bl-bp);self.round_diffs=self.round_diffs[-5000:]
        self.recent.append((rows[0].get('roundEnd'),round(bl-bp,5),round(bm/nm-bp,5) if nm else None));self.recent=self.recent[-500:]
        # update: ONE AdaGrad step per round on the round's average gradient. (Per-print AdaGrad steps would undo the
        # 1/n weighting, because AdaGrad normalises each step: a 1,000-print round would move the model 1,000x more.)
        grad=[0.]*len(self.w)
        for r in rows:
            x=features(r);y=1. if r['won'] else 0.;g=sigmoid(sum(a*b for a,b in zip(self.w,x)))-y
            for j,xj in enumerate(x):grad[j]+=g*xj/n
        for j in range(len(self.w)):
            gj=grad[j]+self.l2*(self.w[j]-(1. if j==1 else 0.))     # shrink toward the market-price prior
            self.g2[j]+=gj*gj;self.w[j]-=self.lr*gj/math.sqrt(self.g2[j])
        self.rounds+=1;self.prints+=n
        return {'brierPrice':bp,'brierLearned':bl}
    def summary(self):
        import poly_audit
        w=self.score['w']
        out={'rounds':self.rounds,'prints':self.prints,'method':'prequential: each round scored before it is learned from; rounds weighted equally',
             'brierPrice':round(self.score['price']/w,5) if w else None,'brierLearned':round(self.score['learned']/w,5) if w else None,
             'brierModel':round(self.score['model']/self.score['modelRounds'],5) if self.score['modelRounds'] else None,
             'weights':{k:round(v,4) for k,v in zip(NAMES,self.w)}}
        if w:out['learnedBeatsPrice']=out['brierLearned']<out['brierPrice']
        rows=[{'entryAt':float(e or 0),'pnl':d} for e,d,_ in self.recent if e]
        out['learnedMinusPriceBootstrap']=poly_audit.block_bootstrap_mean(rows,block_sec=1) if len(rows)>=5 else None
        return out
    def export(self):return {'w':self.w,'g2':self.g2,'rounds':self.rounds,'prints':self.prints,'score':self.score,'recent':self.recent[-500:],'names':list(NAMES)}
    def restore(self,s):
        if not isinstance(s,dict) or s.get('names')!=list(NAMES):return False
        self.w=[float(v) for v in s['w']];self.g2=[float(v) for v in s['g2']];self.rounds=int(s.get('rounds',0));self.prints=int(s.get('prints',0))
        self.score.update(s.get('score') or {});self.recent=[tuple(r) for r in s.get('recent') or []]
        return True
