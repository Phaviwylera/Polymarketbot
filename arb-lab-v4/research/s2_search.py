"""S2 improvement search. Correct accounting: first qualifying print per (round, side). Split by round end time:
train 50% | validate 25% | test 25% (test read once, at the end). Returns include fee; taker adds 0.5c delay cost.
Maker variant: same signal, rest a bid at the current best bid; filled only if a later print on that side trades
strictly below it (minLater < bid); no fee; no delay cost; unfilled = no trade."""
import csv,gzip,math,collections,itertools,sys
sys.path.insert(0,'/home/claude/polymarketbot/arb-lab-v4');import poly_spotlead as sl
R=[]
for r in csv.DictReader(gzip.open('tapex_sample.csv.gz','rt')):
    try:
        if r['lane'] not in ('BTC','ETH') or r['bps']=='' or r['secs']=='' or r['bid']=='' or r['ask']=='':continue
        bid,ask,secs,price=float(r['bid']),float(r['ask']),float(r['secs']),float(r['price'])
        if secs<=2 or not(0<bid<ask<1) or ask-bid>0.05 or not .02<=price<=.98:continue
        tok='UP' if r['tok']=='U' else 'DOWN';p=sl.prob(tok,bid,ask,float(r['bps']),secs)
        z=(float(r['bps']) if tok=='UP' else -float(r['bps']))/math.sqrt(secs)
        R.append((float(r['t']),r['slug'],r['tok'],r['lane'],bid,ask,secs,p,z,int(r['won']),float(r['minLater'])))
    except ValueError:continue
R.sort()
ends=collections.defaultdict(float)
for x in R:ends[x[1]]=max(ends[x[1]],x[0])
order=sorted(ends,key=ends.get);n=len(order)
part={s:('train' if i<.5*n else 'val' if i<.75*n else 'test') for i,s in enumerate(order)}
print('rounds',n,'prints',len(R),{k:sum(1 for v in part.values() if v==k) for k in ('train','val','test')})
def run(thr,alo,ahi,slo,shi,lanes,mode,perRound):
    out=collections.defaultdict(list);seen=set()
    for t,slug,tok,lane,bid,ask,secs,p,z,won,ml in R:
        k=slug if perRound else (slug,tok)
        if k in seen or lane not in lanes or not(alo<=ask<ahi) or not(slo<=secs<shi):continue
        if mode=='taker':
            if p-ask-sl.fee(ask)<thr:continue
            seen.add(k);c=min(.999,ask+.005)+sl.fee(ask);out[part[slug]].append((won-c)/c)
        else:
            if p-bid<thr:continue
            seen.add(k)
            if ml<bid-1e-9:out[part[slug]].append((won-bid)/bid)
    return out
def stat(xs):
    n=len(xs)
    if n<20:return (n,None,None)
    m=sum(xs)/n;sd=math.sqrt(sum((v-m)**2 for v in xs)/(n-1));return (n,m,m-1.645*sd/math.sqrt(n))
grid=list(itertools.product((0.01,0.02,0.03,0.05),((0.5,0.8),(0.8,0.9),(0.9,0.96),(0.5,0.96),(0.3,0.96)),((2,90),(90,200),(200,300),(2,300)),(('BTC',),('ETH',),('BTC','ETH')),('taker','maker'),(False,True)))
res=[]
for g in grid:
    o=run(g[0],*g[1],*g[2],g[3],g[4],g[5]);a,b=stat(o['train']),stat(o['val'])
    res.append((g,a,b,o))
ok=[r for r in res if r[1][1] is not None and r[2][1] is not None and r[1][0]>=60 and r[2][0]>=30]
print('configs',len(res),'with enough trades',len(ok))
print('train-positive AND val-positive (means):',sum(1 for r in ok if r[1][1]>0 and r[2][1]>0),'| expected by chance if no edge ~',round(len(ok)/4))
best=sorted([r for r in ok if r[1][1]>0],key=lambda r:-r[1][1])[:12]
print('\nTop by TRAIN mean, and how they did on VALIDATION:')
for g,a,b,o in best:print(f'  thr {g[0]*100:.0f}c ask {g[1]} secs {g[2]} {"+".join(g[3]):7s} {g[4]:5s} {"perRound" if g[5] else "perSide ":8s} | train n={a[0]:4d} {a[1]*100:+5.2f}c | val n={b[0]:4d} {b[1]*100:+5.2f}c bad {b[2]*100:+5.2f}c')
import pickle;pickle.dump([(g,a,b) for g,a,b,o in res],open('search_res.pkl','wb'))
# both-split robust: rank by min(train mean, val mean)
rob=sorted(ok,key=lambda r:-min(r[1][1],r[2][1]))[:8]
print('\nMost consistent (best worst-of-train-and-val):')
for g,a,b,o in rob:print(f'  thr {g[0]*100:.0f}c ask {g[1]} secs {g[2]} {"+".join(g[3]):7s} {g[4]:5s} {"perRound" if g[5] else "perSide ":8s} | train n={a[0]:4d} {a[1]*100:+5.2f}c | val n={b[0]:4d} {b[1]*100:+5.2f}c bad {b[2]*100:+5.2f}c')
