"""Loss book: where do the live paper strategies (S2 spot lead, K1 Polymarket vs Kalshi) make and lose money?

Splits every settled trade into segments (price band, time left, spot move, side, edge, hour, recent vs earlier; for K1
gap size, pair type and whether the two venues agreed) and scores each segment. With dozens of segments some look bad
by chance, so a segment is flagged only at 99% confidence (z = 2.58): 'losing' when even its optimistic bound is below
zero, 'winning' when even its pessimistic bound is above zero; anything else is 'unclear'. Read-only: it changes no
trade. A flagged segment is evidence for a NEW frozen experiment (ops/EVOLUTION.md), never a reason to edit a running
lane's rule. S2 is scored on its realistic return (0.5c delay cost included).
"""
from __future__ import annotations
import math,time

Z=2.58;MIN_N=50;RECENT_DAYS=7
def fee(p):return 0.07*p*(1-p)

def band(x,edges,labels):
    if x is None:return 'unknown'
    for e,l in zip(edges,labels):
        if x<e:return l
    return labels[-1]

def score(xs,pnl=None):
    n=len(xs)
    if not n:return {'n':0}
    m=sum(xs)/n;sd=math.sqrt(sum((v-m)**2 for v in xs)/(n-1)) if n>1 else 0.;se=sd/math.sqrt(n) if n>1 else float('inf')
    lo,hi=m-Z*se,m+Z*se
    flag='unclear' if n<MIN_N else ('losing' if hi<0 else 'winning' if lo>0 else 'unclear')
    out={'n':n,'mean':round(m,5),'lo99':round(lo,5) if n>1 else None,'hi99':round(hi,5) if n>1 else None,
         'winPct':round(100*sum(1 for v in xs if v>0)/n,1),'flag':flag}
    if pnl is not None:out['pnlUsd']=round(sum(pnl),2)
    return out

def segments(rows,dims,ret,pnl):
    out={}
    for name,fn in dims.items():
        g={}
        for r in rows:g.setdefault(fn(r),[]).append(r)
        out[name]={k:score([ret(r) for r in v],[pnl(r) for r in v]) for k,v in sorted(g.items(),key=lambda kv:str(kv[0]))}
    return out

def s2_dims(now):
    hour=lambda r:time.gmtime(float(r.get('t') or 0)).tm_hour
    return {
        'lane':lambda r:r.get('lane'),
        'side':lambda r:str(r.get('token') or '?')[:1],
        'askBand':lambda r:band(r.get('ask'),(.6,.8,.9,.95),('<60c','60-80c','80-90c','90-95c','95c+')),
        'timeLeft':lambda r:band(r.get('secs'),(60,120,200),('<60s','60-120s','120-200s','200s+')),
        'spotMove':lambda r:band(None if r.get('bps') is None else abs(float(r['bps'])),(5,15,30),('<5bps','5-15bps','15-30bps','30bps+')),
        'modelEdge':lambda r:band(None if r.get('p') is None or r.get('ask') is None else r['p']-r['ask']-fee(r['ask']),(.02,.04),('1-2c','2-4c','4c+')),
        'hourUtc':lambda r:band(hour(r),(6,12,18),('00-05','06-11','12-17','18-23')),
        'period':lambda r:'last 7 days' if now-float(r.get('t') or 0)<RECENT_DAYS*86400 else 'earlier',
    }

def k1_dims(now):
    tr=lambda r:r['trade']
    return {
        'lane':lambda r:r.get('lane'),
        'pairType':lambda r:str(tr(r).get('pair') or '?')[:1],
        'gap':lambda r:band(1-float(tr(r)['cost']),(.02,.04,.08),('<2c','2-4c','4-8c','8c+')),
        'timeLeft':lambda r:band(tr(r).get('left'),(.2,.5,.8),('last 20%','20-50%','50-80%','80%+')),
        'venuesAgreed':lambda r:'agreed' if r.get('agree',True) else 'DISAGREED',
        'period':lambda r:'last 7 days' if now-float(tr(r).get('t') or 0)<RECENT_DAYS*86400 else 'earlier',
    }

def s2_book(rows,now):
    rows=[r for r in rows if r.get('shares') and r.get('retStressed') is not None]
    if not rows:return None
    seg=segments(rows,s2_dims(now),lambda r:r['retStressed'],lambda r:r['pnlUsd'])
    losses=[r for r in rows if r['pnlUsd']<0]
    causes={'late entry (<60s left)':sum(1 for r in losses if (r.get('secs') or 999)<60),
            'weak spot lead (<5bps)':sum(1 for r in losses if r.get('bps') is not None and abs(float(r['bps']))<5),
            'expensive (95c+ ask)':sum(1 for r in losses if (r.get('ask') or 0)>=.95),
            'thin model edge (1-2c)':sum(1 for r in losses if r.get('p') is not None and r['p']-r['ask']-fee(r['ask'])<.02)}
    return {'unit':'return per $ staked, 0.5c delay cost included','overall':score([r['retStressed'] for r in rows],[r['pnlUsd'] for r in rows]),
            'segments':seg,'losses':{'count':len(losses),'usd':round(sum(r['pnlUsd'] for r in losses),2),'tags':causes},
            'biggestLosses':[{k:r.get(k) for k in ('t','lane','token','ask','p','bps','secs','pnlUsd')} for r in sorted(losses,key=lambda r:r['pnlUsd'])[:8]]}

def k1_book(rows,now):
    rows=[r for r in rows if r.get('trade') and r['trade'].get('pnlPerPair') is not None]
    if not rows:return None
    seg=segments(rows,k1_dims(now),lambda r:r['trade']['pnlPerPair'],lambda r:r['trade'].get('pnlUsd') or 0.)
    losses=[r for r in rows if r['trade']['pnlPerPair']<0]
    dis=[r for r in losses if r.get('agree') is False]
    return {'unit':'P&L per $1 pair (both fees and 1c per leg included)','overall':score([r['trade']['pnlPerPair'] for r in rows],[r['trade'].get('pnlUsd') or 0. for r in rows]),
            'segments':seg,'losses':{'count':len(losses),'usd':round(sum(r['trade'].get('pnlUsd') or 0. for r in losses),2),
                                     'fromVenueDisagreement':len(dis),'usdFromVenueDisagreement':round(sum(r['trade'].get('pnlUsd') or 0. for r in dis),2)}}

def flagged(book):
    if not book:return []
    return [{'dimension':d,'segment':k,**v} for d,segs in book['segments'].items() for k,v in segs.items() if v.get('flag') in ('losing','winning')]

def report(compact=False,now=None):
    import poly_spotlead,poly_kalshi
    now=now or time.time()
    s2=s2_book(list(poly_spotlead.CURRENT.done),now) if poly_spotlead.CURRENT else None
    k1=k1_book(list(poly_kalshi.CURRENT.done),now) if poly_kalshi.CURRENT else None
    out={'method':'segments flagged only at 99% confidence and n >= 50; evidence for new experiments, never edits to a running rule',
         'S2':s2,'K1':k1,'flags':{'S2':flagged(s2),'K1':flagged(k1)}}
    if compact:
        for b in (s2,k1):
            if b:b.pop('segments',None);b.pop('biggestLosses',None)
    return out
