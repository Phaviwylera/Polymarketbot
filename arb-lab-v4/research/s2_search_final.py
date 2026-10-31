exec(open('research/s2_search.py').read().split("grid=list(")[0].replace("print('rounds'","(lambda *a,**k:None)('rounds'"))
for name,cfg in (('BTC 90-96c, 200s+ left, taker',(0.01,0.9,0.96,200,300,('BTC',),'taker',False)),
                 ('BTC 90-96c, any time, taker',(0.01,0.9,0.96,2,300,('BTC',),'taker',False)),
                 ('S2 as running now',(0.01,0.0,1.0,2,300,('BTC','ETH'),'taker',False))):
    o=run(*cfg)
    for k in ('train','val','test'):
        s=stat(o[k]);print(f"{name:32s} {k:5s} n={s[0]:4d} mean={'—' if s[1] is None else f'{s[1]*100:+5.2f}c'} bad={'—' if s[2] is None else f'{s[2]*100:+6.2f}c'}")
