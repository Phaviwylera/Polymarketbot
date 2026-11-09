"""V3 research: can a richer model beat the Polymarket price on 5m/15m crypto rounds after costs?

Data: tape export CSV (tape_export.py columns). One row = one recorded trade print with the book and spot at that moment.
Features (all known at the print, nothing from later):
  lm      logit of the side's mid                     spr   spread
  z       spot move since open on this side / sqrt(seconds left)
  dz15/60 change of that spot move over the last 15/60 s (same round, any side's prints)
  dm15/60 change of this side's mid over the last 15/60 s
  flow30  net taker buying of this side over the last 30 s (shares; buys +, sells -)
  imb     this side's best-bid size share: bidSize / (bidSize + other side's bidSize)
  secs, lane flags (BTC/ETH, 5m/15m)
Model: sklearn HistGradientBoostingClassifier on 'won'. Baseline: the mid itself.
Validation: walk forward by UTC day (train on all earlier days, score the next day). The last HOLDOUT_DAYS are not
used for any choice; they are scored once with the rule chosen on the walk-forward days.
Trading (correct accounting): the FIRST print per (round, side) where the rule fires; one trade per round and side.
  taker: pay ask + fee 0.07 p(1-p) + 0.5c delay;  maker: rest at the bid, filled only if a later print on that side
  trades strictly below it (minLater < bid); no fee. Returns per $ staked; hold to settlement.
Usage: python research/v3_model.py <tape.csv.gz> [holdout_days=3]
"""
import sys,math,time
import numpy as np,pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

FEE=0.07;DELAY=0.005;HOLDOUT_DAYS=int(sys.argv[2]) if len(sys.argv)>2 else 3
FEATS=['lm','spr','z','dz15','dz60','dm15','dm60','flow30','imb','secs','is_eth','is15']
def fee(p):return FEE*p*(1-p)

def load(path):
    t0=time.time()
    df=pd.read_csv(path)
    for c in ('size','agg','bidSize','otherBidSize'):
        if c not in df:df[c]=np.nan
    df=df[df.lane.isin(['BTC','ETH','BTC15','ETH15'])]
    df=df.dropna(subset=['bps','secs','bid','ask'])
    df=df[(df.secs>2)&(df.bid>0)&(df.ask<1)&(df.bid<df.ask)&(df.ask-df.bid<=0.05)&(df.price.between(.02,.98))].copy()
    df['s']=np.where(df.tok=='U',1.0,-1.0)
    df['mid']=(df.bid+df.ask)/2;df['spr']=df.ask-df.bid
    df['lm']=np.log(df.mid.clip(1e-4,1-1e-4)/(1-df.mid.clip(1e-4,1-1e-4)))
    df['z']=df.s*df.bps/np.sqrt(df.secs)
    df['is_eth']=df.lane.str.startswith('ETH').astype(float);df['is15']=df.lane.str.endswith('15').astype(float)
    df=df.sort_values('t').reset_index(drop=True)
    # spot path: bps of the round 15/60 s ago (any side; bps is side-independent)
    for lag in (15,60):
        left=df[['t','slug']].copy();left['tq']=left.t-lag
        right=df[['t','slug','bps']].rename(columns={'t':'tq','bps':f'bps{lag}'})
        m=pd.merge_asof(left.sort_values('tq'),right.sort_values('tq'),on='tq',by='slug',direction='backward',allow_exact_matches=True)
        m.index=left.sort_values('tq').index;df[f'bps{lag}']=m[f'bps{lag}'].reindex(df.index)
        df[f'dz{lag}']=(df.s*(df.bps-df[f'bps{lag}'])/np.sqrt(df.secs)).fillna(0.)
        # this side's mid lag
        left=df[['t','slug','tok']].copy();left['tq']=left.t-lag
        right=df[['t','slug','tok','mid']].rename(columns={'t':'tq','mid':f'mid{lag}'})
        m=pd.merge_asof(left.sort_values('tq'),right.sort_values('tq'),on='tq',by=['slug','tok'],direction='backward')
        m.index=left.sort_values('tq').index;df[f'dm{lag}']=(df.mid-m[f'mid{lag}'].reindex(df.index)).fillna(0.)
    # net taker flow on this side over the last 30 s
    sz=pd.to_numeric(df['size'],errors='coerce').fillna(0.);sg=np.where(df['agg'].astype(str).str.upper().str[0]=='B',1.,np.where(df['agg'].astype(str).str.upper().str[0]=='S',-1.,0.))
    df['sv']=sz*sg;df['csv']=df.groupby(['slug','tok']).sv.cumsum()
    left=df[['t','slug','tok']].copy();left['tq']=left.t-30
    right=df[['t','slug','tok','csv']].rename(columns={'t':'tq','csv':'csv30'})
    m=pd.merge_asof(left.sort_values('tq'),right.sort_values('tq'),on='tq',by=['slug','tok'],direction='backward')
    m.index=left.sort_values('tq').index;df['flow30']=(df.csv-m.csv30.reindex(df.index).fillna(0.))
    bs=pd.to_numeric(df.bidSize,errors='coerce');ob=pd.to_numeric(df.otherBidSize,errors='coerce')
    df['imb']=(bs/(bs+ob)).where((bs+ob)>0,0.5).fillna(0.5)
    df['day']=pd.to_datetime(df.t,unit='s').dt.strftime('%m-%d')
    print(f'loaded {len(df):,} prints, {df.slug.nunique():,} rounds, days {sorted(df.day.unique())} in {time.time()-t0:.0f}s',flush=True)
    return df

def fit(tr,max_per_round=60,seed=0,feats=None):
    feats=feats or FEATS
    # cap prints per round so busy rounds don't dominate
    # random sample across the whole round (taking the FIRST n prints left late-round prices out of training:
    # a train/test shift that made the model worse than the mid on every day)
    if max_per_round:
        r=np.random.default_rng(seed).random(len(tr));rank=pd.Series(r,index=tr.index).groupby([tr.slug,tr.tok]).rank(method='first')
        tr=tr[rank<=max_per_round]
    m=HistGradientBoostingClassifier(max_iter=300,learning_rate=0.05,max_leaf_nodes=31,min_samples_leaf=400,l2_regularization=1.0,random_state=seed)
    m.fit(tr[feats].values,tr.won.values);return m

def logloss(p,y):p=np.clip(p,1e-4,1-1e-4);return float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p)))

def trades(d,p,thr,mode):
    """First print per (round, side) where the rule fires. Returns per-$ returns and P&L for $5 stakes."""
    if mode=='taker':
        cost=d.ask.values+fee(d.ask.values)+DELAY;fire=p-cost>=thr
    else:
        cost=d.bid.values;fire=(p-cost>=thr)
    x=d.loc[fire,['slug','tok','won','ask','bid','minLater']].copy();x['c']=cost[fire];x['p']=p[fire]
    x=x[~x.duplicated(['slug','tok'])]
    if mode=='maker':x=x[x.minLater<x.bid-1e-9]
    x['ret']=(x.won-x.c)/x.c;return x

def stat(r):
    n=len(r)
    if n<2:return n,float('nan'),float('nan')
    m=r.mean();return n,m,m-1.645*r.std(ddof=1)/math.sqrt(n)

def main(path):
    df=load(path);days=sorted(df.day.unique())
    if len(days)<HOLDOUT_DAYS+3:raise SystemExit('not enough days')
    wf_days=days[2:len(days)-HOLDOUT_DAYS];hold=days[len(days)-HOLDOUT_DAYS:]
    print('walk-forward days',wf_days,'| holdout (untouched)',hold,flush=True)
    THRS=(0.0,0.01,0.02,0.03,0.05);res={(m,t):[] for m in ('taker','maker') for t in THRS};ll=[]
    for d in wf_days:
        tr=df[df.day<d];te=df[df.day==d];mdl=fit(tr);p=mdl.predict_proba(te[FEATS].values)[:,1]
        ctl=fit(tr,feats=['lm']);pc=ctl.predict_proba(te[['lm']].values)[:,1]          # control: price only
        ll.append((d,len(te),logloss(p,te.won.values),logloss(te.mid.values,te.won.values),logloss(pc,te.won.values)))
        for k in res:res[k].append(trades(te,p,k[1],k[0]))
        print(f'  {d}: prints {len(te):,} logloss model {ll[-1][2]:.4f} vs mid {ll[-1][3]:.4f} (price-only control {ll[-1][4]:.4f})',flush=True)
    mm=np.average([x[2] for x in ll],weights=[x[1] for x in ll]);mp=np.average([x[3] for x in ll],weights=[x[1] for x in ll])
    print(f'WALK-FORWARD logloss model {mm:.5f} vs mid {mp:.5f}  ({100*(mp-mm)/mp:+.2f}%)')
    best=None
    for k,v in res.items():
        x=pd.concat(v) if v else pd.DataFrame();n,m,lo=stat(x.ret) if len(x) else (0,float('nan'),float('nan'))
        days_pos=sum(1 for y in v if len(y) and y.ret.mean()>0)
        print(f'  {k[0]:5s} thr {k[1]*100:3.0f}c: trades {n:6d} per$ {m*100:+6.2f}c bad {lo*100:+6.2f}c  days positive {days_pos}/{len(v)}')
        if n>=200 and (best is None or lo>best[1]):best=(k,lo)
    print('chosen on walk-forward days only:',best)
    if best is None:return
    (mode,thr),_=best;tr=df[~df.day.isin(hold)];mdl=fit(tr);te=df[df.day.isin(hold)];p=mdl.predict_proba(te[FEATS].values)[:,1]
    print(f'HOLDOUT logloss model {logloss(p,te.won.values):.5f} vs mid {logloss(te.mid.values,te.won.values):.5f}')
    x=trades(te,p,thr,mode);n,m,lo=stat(x.ret)
    print(f'HOLDOUT {mode} thr {thr*100:.0f}c: trades {n} per$ {m*100:+.2f}c bad {lo*100:+.2f}c  $5-stake P&L {(5*x.ret).sum():+.2f}')
    for d in hold:
        y=x[x.slug.isin(te[te.day==d].slug)];print(f'   {d}: trades {len(y)} per$ {100*y.ret.mean():+.2f}c')

if __name__=='__main__':main(sys.argv[1])
