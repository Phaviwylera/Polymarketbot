exec(open('spotlead.py').read().split("# trade sims on test")[0].replace("print(","(lambda *a,**k:None)("))
import time as _t
def first_taker(pfun,thr,sel=lambda r:True):
    out={}
    for key,rs in byround.items():
        for r in rs:
            if not sel(r):continue
            p=pfun(r)
            if p-r[9]-fee(r[9])>=thr:c=r[9]+fee(r[9]);out[key[0]]=(((1 if r[5] else 0)-c)/c,r);break
    return out
byround=collections.defaultdict(list)
for r in te:byround[(r[2],r[3])].append(r)
def show(name,d):
    xs=[v[0] for v in d.values()];n=len(xs)
    if n<5:print(f'{name:44s} n={n}');return
    m=np.mean(xs);s=np.std(xs,ddof=1);print(f'{name:44s} n={n:4d} mean={m*100:+6.2f}c lower95={(m-1.645*s/math.sqrt(n))*100:+6.2f}c win={100*np.mean([x>0 for x in xs]):5.1f}% avgAsk={np.mean([v[1][9] for v in d.values()]):.3f}')
# price-only model (no spot) refit on train
pm={}
for fm in ('5m','15m'):
    tr=[r for r in rows if fam(r[1])==fm and r[2] in cut]
    w,_=fit([[feats(r)[0],1.0] for r in tr],[r[5] for r in tr]);pm[fm]=w;print(fm,'price-only a,c',w.round(3))
pprice=lambda r:1/(1+math.exp(-(pm[fam(r[1])][0]*feats(r)[0]+pm[fam(r[1])][1])))
for thr in (0.01,0.02):
    show(f'full model thr {thr}',first_taker(pmod,thr))
    show(f'price-only (no spot) thr {thr}',first_taker(pprice,thr))
d=first_taker(pmod,0.01)
print('-- full model thr 1c, by lane / ask band / day')
for ln in ('BTC','ETH','BTC15','ETH15'):show(' lane '+ln,{k:v for k,v in d.items() if v[1][1]==ln})
for lo,hi in ((0,.5),(.5,.8),(.8,.9),(.9,.95),(.95,1)):show(f' ask {lo}-{hi}',{k:v for k,v in d.items() if lo<=v[1][9]<hi})
days=collections.defaultdict(dict)
for k,v in d.items():days[_t.strftime('%m-%d',_t.gmtime(v[1][0]))][k]=v
for dd in sorted(days):show(' day '+dd,days[dd])
for lo,hi in ((0,30),(30,90),(90,200),(200,900)):show(f' secs left {lo}-{hi}',{k:v for k,v in d.items() if lo<=v[1][7]<hi})
