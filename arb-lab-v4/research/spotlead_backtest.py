# SUPERSEDED 2026-10-31: the trade simulation here keys trades by round and lets the second side overwrite the first
# (hidden look-ahead). Its +2.6c result is an artifact. Use research/spotlead_replay.py.
"""Does live spot know something the Polymarket price doesn't? Offline test on the recorded tape.
Split by time: fit on the first 60% of rounds (by round end), judge on the last 40% only.
Model (per lane family 5m/15m): logit P(token wins) = a*logit(mid) + b*z + c,  z = signed spot move / sqrt(seconds left).
If b is ~0 on train, spot adds nothing beyond price. Then trade sims on test, first signal per round+token:
  taker: buy at ask when p - ask - fee >= thr
  maker: bid at bid when p - bid >= thr; filled only if a LATER print on that token in that round is strictly below our bid
Returns per $ clustered by round (one number per round) -> mean, lower 95%.
"""
import csv,gzip,math,sys,collections,random
import numpy as np
P=sys.argv[1]
rows=[]
with gzip.open(P,'rt') as f:
    for r in csv.DictReader(f):
        try:
            if r['bps'] in ('','None') or r['secs'] in ('','None'):continue
            secs=float(r['secs']);price=float(r['price'])
            if secs<=2 or not 0.02<=price<=0.98:continue
            bid=float(r['bid']) if r['bid'] not in ('','None') else None;ask=float(r['ask']) if r['ask'] not in ('','None') else None
            if bid is None or ask is None or not (0<bid<ask<1) or ask-bid>0.05:continue
            sgn=1 if r['tok']=='U' else -1
            rows.append((float(r['t']),r['lane'],r['slug'],r['tok'],price,int(r['won']),sgn*float(r['bps']),secs,bid,ask,float(r['minLater'])))
        except (ValueError,KeyError):continue
rows.sort()
print('usable prints',len(rows),'rounds',len({r[2] for r in rows}))
def lg(p):p=min(max(p,1e-4),1-1e-4);return math.log(p/(1-p))
def fee(p):return 0.07*p*(1-p)
fam=lambda lane:'15m' if lane.endswith('15') else '5m'
ends=collections.defaultdict(float)
for r in rows:ends[r[2]]=max(ends[r[2]],r[0])
order=sorted(ends,key=ends.get);cut=set(order[:int(.6*len(order))])
def feats(r):
    mid=(r[8]+r[9])/2;z=r[6]/math.sqrt(r[7]);return [lg(mid),z,1.0]
def fit(X,y,iters=60,l2=1e-3):
    X=np.array(X);y=np.array(y);w=np.zeros(X.shape[1])
    for _ in range(iters):
        p=1/(1+np.exp(-X@w));g=X.T@(p-y)+l2*w;H=(X.T*(p*(1-p)))@X+l2*np.eye(len(w));w-=np.linalg.solve(H,g)
    p=1/(1+np.exp(-X@w));cov=np.linalg.inv((X.T*(p*(1-p)))@X+l2*np.eye(len(w)))
    return w,np.sqrt(np.diag(cov))
models={}
for fm in ('5m','15m'):
    tr=[r for r in rows if fam(r[1])==fm and r[2] in cut]
    if not tr:continue
    # thin to <= 40 prints per round so a few busy rounds don't dominate; SEs are still optimistic (prints within a round correlate)
    by=collections.defaultdict(list)
    for r in tr:by[r[2]].append(r)
    random.seed(7);tr=[x for v in by.values() for x in (random.sample(v,40) if len(v)>40 else v)]
    w,se=fit([feats(r) for r in tr],[r[5] for r in tr]);models[fm]=w
    print(f'{fm} train n={len(tr)} rounds={len(by)}  a(price)={w[0]:.3f}±{se[0]:.3f}  b(spot)={w[1]:.4f}±{se[1]:.4f}  c={w[2]:.3f}')
def pmod(r):
    w=models[fam(r[1])];x=np.dot(w,feats(r));return 1/(1+math.exp(-x))
te=[r for r in rows if r[2] not in cut]
for fm in ('5m','15m'):
    t=[r for r in te if fam(r[1])==fm]
    if not t:continue
    bm=np.mean([(pmod(r)-r[5])**2 for r in t]);bp=np.mean([((r[8]+r[9])/2-r[5])**2 for r in t])
    print(f'{fm} TEST prints={len(t)} brier model={bm:.5f} mid={bp:.5f} improvement={100*(bp-bm)/bp:.2f}%')
# trade sims on test
byround=collections.defaultdict(list)
for r in te:byround[(r[2],r[3])].append(r)
def summarize(name,per_round):
    xs=list(per_round.values());n=len(xs)
    if n<5:print(f'  {name}: n={n}');return
    m=np.mean(xs);s=np.std(xs,ddof=1);print(f'  {name}: rounds={n} mean/$={m*100:+.2f}c lower95={(m-1.645*s/math.sqrt(n))*100:+.2f}c win={100*np.mean([x>0 for x in xs]):.1f}%')
for thr in (0.01,0.02,0.03,0.05):
    tk={};mk={}
    for key,rs in byround.items():
        for i,r in enumerate(rs):
            p=pmod(r)
            if key[0] not in tk and p-r[9]-fee(r[9])>=thr:
                c=r[9]+fee(r[9]);tk[key[0]]=((1 if r[5] else 0)-c)/c
            if key[0] not in mk and p-r[8]>=thr and r[10]<r[8]-1e-9:mk[key[0]]=((1 if r[5] else 0)-r[8])/r[8]
            if key[0] in tk and key[0] in mk:break
    print(f'edge >= {thr*100:.0f}c');summarize('taker at ask (fee paid)',tk);summarize('maker at bid (filled on trade-through)',mk)
