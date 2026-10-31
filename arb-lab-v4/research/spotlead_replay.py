import csv,gzip,math,collections,sys
sys.path.insert(0,'/home/claude/polymarketbot/arb-lab-v4');import poly_spotlead as sl
rows=sorted(csv.DictReader(gzip.open(sys.argv[1] if len(sys.argv)>1 else 'tapex_sample.csv.gz','rt')),key=lambda r:float(r['t']))
ends=collections.defaultdict(float)
for r in rows:ends[r['slug']]=max(ends[r['slug']],float(r['t']))
order=sorted(ends,key=ends.get);test=set(order[int(.6*len(order)):])
sig=[]
for r in rows:
    if r['slug'] not in test:continue
    s=sl.signal({'lane':r['lane'],'bid':r['bid'] or None,'ask':r['ask'] or None,'spotVsOpenBps':r['bps'] or None,'secondsLeft':r['secs'] or None,'price':r['price'],'token':'UP' if r['tok']=='U' else 'DOWN'})
    if s:ask=s[1];c=ask+sl.fee(ask);sig.append((float(r['t']),r['slug'],r['tok'],(int(r['won'])-c)/c))
def st(name,xs):
    n=len(xs);m=sum(xs)/n;sd=math.sqrt(sum((v-m)**2 for v in xs)/(n-1));print(f'{name:58s} n={n:4d} per$={m*100:+5.2f}c bad={100*(m-1.645*sd/math.sqrt(n)):+5.2f}c (no delay cost)')
a={};[a.setdefault((s,k),x) for t,s,k,x in sig];st('A first signal per round AND side (what live S2 does)',list(a.values()))
b={};[b.setdefault(s,x) for t,s,k,x in sig];st('B first signal per round, earliest in time',list(b.values()))
# C: my earlier backtest's loop: groups per (round,side) in first-seen order, dict keyed by round -> later side overwrites
g=collections.OrderedDict()
for t,s,k,x in sig:g.setdefault((s,k),x)
c={};[c.__setitem__(s,x) for (s,k),x in g.items()];st('C earlier backtest (round key overwritten by the second side)',list(c.values()))
both=[s for s in b if (s,'U') in a and (s,'D') in a];print('rounds where both sides signalled:',len(both),'of',len(b))
