"""Plain-text developer views of the live paper engine.

Browser- and antivirus-friendly (no ZIP), safe to share: paper state only, no
credentials, request headers or environment values. A compact copy is also
printed to stdout every few minutes so it is readable from the hosting logs.
"""
from __future__ import annotations
import json,time

def _r(x,n=4):
    try:return round(float(x),n)
    except (TypeError,ValueError):return None

def _age(t,now):
    try:return round(now-float(t),1)
    except (TypeError,ValueError):return None

def report(engine,now=None):
    now=time.time() if now is None else now
    snap=engine.snapshot(include_assets=True)
    pf=snap.get('portfolio') or {};edge=snap.get('edge') or {};research=snap.get('research') or {}
    lanes={}
    for asset,a in (snap.get('assets') or {}).items():
        p=a.get('paper') or {};pos=p.get('position');m=a.get('market') or {}
        st=a.get('strategies') or {};sig=st.get('edgeSignal') or {};best=sig.get('best') or {}
        lanes[asset]={'status':p.get('status'),'lock':p.get('riskLockReason'),'decision':a.get('decision'),'mode':a.get('mode'),
            'market':m.get('slug'),'secondsLeft':_r(float(m['endTs'])-now,0) if m.get('endTs') else None,'feed':a.get('feed'),
            'equity':_r(p.get('equity'),2),'realizedPnl':_r(p.get('realizedPnl')),'trades':p.get('trades'),'wins':p.get('wins'),'losses':p.get('losses'),
            'position':{k:pos.get(k) for k in ('mode','side','shares','entryPrice','entryTotalCost','strategy')} if pos else None,
            'orders':p.get('strategyOrders'),'fills':p.get('strategyFills'),
            'edge':{'decision':sig.get('decision'),'pUp':_r(sig.get('pUp')),'heuristicFairUp':_r(sig.get('heuristicFairUp')),'cellRounds':sig.get('cellRounds'),
                    'bestSide':best.get('side'),'ask':best.get('ask'),'conservativeEdge':_r(best.get('conservativeEdge')),'smartFlow':_r(best.get('smartFlow'),2)},
            'strategyDecisions':{'edge':st.get('edge'),'model':st.get('model'),'momentumSizing':st.get('momentumSizing')},
            'recentEvents':[{'ago':_age(e.get('t'),now),'action':e.get('action'),'side':e.get('side'),'price':e.get('price'),'pnl':_r(e.get('pnl')),'detail':str(e.get('detail',''))[:140]}
                            for e in (p.get('events') or [])[:8]]}
    strategies={k:{x:v.get(x) for x in ('bets','rounds','wins','winRatePct','meanReturnPerDollar','meanMinus2SE','gatePassed')}
                for k,v in (edge.get('strategies') or {}).items()}
    wallets=edge.get('wallets') or {}
    training=research.get('training') or {}
    att=(snap.get('attribution') or {})
    return {'generatedAt':now,'generatedAtUtc':time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(now)),'revision':pf.get('revision'),
        'storage':{k:(snap.get('infrastructure') or {}).get(k) for k in ('backend','state','checkpointAgeSec','errors','lastError','databaseExpiresAt')},
        'portfolio':{'equity':_r(pf.get('equity'),2),'totalPnl':_r(pf.get('totalPnl')),'realizedPnl':_r(pf.get('realizedPnl')),'trades':pf.get('trades'),
                     'lifetimeRealizedPnl':_r(pf.get('lifetimeRealizedPnl')),'lifetimeTrades':pf.get('lifetimeTrades'),'riskLocked':pf.get('riskLocked'),'maxDrawdownUsd':_r(pf.get('maxDrawdownUsd'))},
        'edge':{'mode':edge.get('mode'),'promoted':edge.get('promotedStrategy'),'strategies':strategies,
                'maker':{k:(edge.get('maker') or {}).get(k) for k in ('bets','rounds','wins','winRatePct','meanReturnPerDollar','meanMinus2SE','gatePassed')},'calibration':edge.get('calibration'),'modelTrust':edge.get('modelTrust'),
                'pendingRounds':edge.get('pendingRounds'),'errors':edge.get('errors'),
                'wallets':{k:wallets.get(k) for k in ('walletsTracked','roundsStudied','skilledWallets','skilledEntryProfile','errors','lastError')},
                'readiness':edge.get('readiness'),'autoCycle':edge.get('autoCycle')},
        'researchModel':{'state':training.get('state'),'samples':training.get('samples') or research.get('labelledSamples'),'rounds':research.get('independentRounds'),
                         'selectedModel':training.get('selectedModel'),'evidenceGatePassed':training.get('evidenceGatePassed'),'reason':training.get('reason')},
        'closedTrades':{'total':(att.get('total') or {}),'recent':[{k:t.get(k) for k in ('asset','strategy','side','entryPrice','exitPrice','netPnlUsd','holdSec','exitReason')} for t in (att.get('recent') or [])[:10]]},
        'lanes':lanes}

def _line(label,value):return f'{label:<22}{value}'

def text(rep):
    out=[f"POLY/EDGE developer report · {rep['generatedAtUtc']} UTC · revision {rep['revision']}",'='*78]
    pf=rep['portfolio'];out+=[_line('Portfolio equity',f"${pf['equity']}  total P&L {pf['totalPnl']}  realized {pf['realizedPnl']}  trades {pf['trades']}  locked {pf['riskLocked']}"),
        _line('Lifetime',f"realized {pf['lifetimeRealizedPnl']}  trades {pf['lifetimeTrades']}  max drawdown ${pf['maxDrawdownUsd']}"),
        _line('Storage',json.dumps(rep['storage'])),'']
    e=rep['edge'];out+=[f"EDGELAB · mode {e['mode']} · promoted {e['promoted']} · pending rounds {e['pendingRounds']} · errors {e['errors']}"]
    for k,s in e['strategies'].items():out.append(_line('  '+k,json.dumps(s)))
    out.append(_line('  maker (pairs)',json.dumps(e.get('maker'))))
    out+=[_line('  model trust',json.dumps(e.get('modelTrust'))),_line('  calibration',json.dumps(e['calibration'])),_line('  wallets',json.dumps(e['wallets'])),
          _line('  readiness',json.dumps({'ready':(e['readiness'] or {}).get('readyForRealMoney'),'checks':[(c['check'],c['value'],c['pass']) for c in (e['readiness'] or {}).get('checks',[])]})),
          _line('  auto-cycle',json.dumps((e['autoCycle'] or {}).get('recent'))),'',
          _line('RESEARCH MODEL',json.dumps(rep['researchModel'])),'',
          _line('CLOSED TRADES',json.dumps({k:rep['closedTrades']['total'].get(k) for k in ('trades','netPnlUsd','winRatePct','expectancyUsd','profitFactor')}))]
    for t in rep['closedTrades']['recent']:out.append('  '+json.dumps(t))
    for asset,l in rep['lanes'].items():
        out+=['',f"--- {asset} · {l['status']} · {l['mode']} · {l['market']} ({l['secondsLeft']}s left) · equity ${l['equity']} realized {l['realizedPnl']} trades {l['trades']} ({l['wins']}W/{l['losses']}L)",
              _line('  decision',l['decision']),_line('  edge',json.dumps(l['edge'])),_line('  strategies',json.dumps(l['strategyDecisions'])),
              _line('  position',json.dumps(l['position'])),_line('  orders/fills',json.dumps({'orders':l['orders'],'fills':l['fills']}))]
        if l['lock']:out.append(_line('  lock',l['lock']))
        for ev in l['recentEvents']:out.append(f"    {ev['ago']:>7}s ago  {ev['action']:<8} {ev['side'] or '':<5} {ev['price'] if ev['price'] is not None else '':<7} {'' if ev['pnl'] is None else 'pnl '+str(ev['pnl'])}  {ev['detail']}")
    return '\n'.join(out)+'\n'

def events(engine,asset=None,limit=200,action=None):
    rows=[]
    for a,lane in engine.engines.items():
        if asset and a!=asset.upper():continue
        for ev in ((lane.paper or {}).get('events') or []):
            if action and str(ev.get('action','')).upper()!=action.upper():continue
            rows.append({'asset':a,**ev})
    rows.sort(key=lambda r:r.get('t',0),reverse=True)
    return rows[:max(1,min(2000,int(limit)))]

def compact_lines(rep):
    """Short single-line JSON records for stdout; each well under hosting log line limits."""
    head={k:rep[k] for k in ('generatedAtUtc','revision','storage','portfolio','researchModel')}
    e=rep['edge'];head['edge']={k:e.get(k) for k in ('mode','promoted','strategies','modelTrust','calibration','pendingRounds','errors')}
    head['wallets']={k:(e['wallets'] or {}).get(k) for k in ('walletsTracked','roundsStudied','skilledWallets','errors','lastError')}
    head['readiness']=[(c['check'][:24],c['value'],c['pass']) for c in (e['readiness'] or {}).get('checks',[])]
    out=['POLY_DEV_REPORT '+json.dumps(head,separators=(',',':'),default=str)]
    for asset,l in rep['lanes'].items():
        out.append('POLY_DEV_LANE '+json.dumps({'asset':asset,**{k:l[k] for k in ('status','lock','decision','market','secondsLeft','realizedPnl','trades','position','edge','strategyDecisions','fills')}},separators=(',',':'),default=str))
    return out

import re
_EDGE=re.compile(r'(?:estimated edge|edge)\s+(-?[0-9.]+)')
def trade_rows(engine):
    """Closed trades rebuilt from every retained session ledger (archived + current), oldest first."""
    rows=[]
    for asset,lane in engine.engines.items():
        sessions=list((getattr(engine,'ledger_history',{}) or {}).get(asset,{}).values())
        if lane.paper:sessions.append(lane.paper)
        for s in sessions:
            buy=None
            for ev in reversed(s.get('events') or []):
                a=ev.get('action')
                if a=='BUY':buy=ev
                elif a in ('SELL','SETTLE') and ev.get('pnl') is not None:
                    d=str((buy or {}).get('detail',''));m=_EDGE.search(d)
                    rows.append({'asset':asset,'session':s.get('sessionId'),'entryAt':_r((buy or {}).get('t'),1),'exitAt':_r(ev.get('t'),1),
                        'mode':d.split(';')[0].split(' ')[0] if buy else None,'side':ev.get('side'),'entry':(buy or {}).get('price'),'exit':ev.get('price'),
                        'pnl':_r(ev.get('pnl'),5),'holdSec':_r(float(ev['t'])-float(buy['t']),1) if buy else None,
                        'estEdge':float(m.group(1)) if m else None,'reason':str(ev.get('detail','')).split(';')[0][:60]})
                    buy=None
    rows.sort(key=lambda r:r['exitAt'] or 0)
    return rows

def trades_csv(rows):
    cols=['asset','session','entryAt','exitAt','mode','side','entry','exit','pnl','holdSec','estEdge','reason']
    return ','.join(cols)+'\n'+''.join(','.join('' if r.get(c) is None else str(r[c]).replace(',',' ') for c in cols)+'\n' for r in rows)

EDGE_COLS=('strategy','asset','marketSlug','t','remaining','side','ask','cost','p','pConservative','edge','smartFlow','won','returnPerDollar')
def edge_bets_csv(edge):
    """Every settled EdgeLab shadow bet (mispricing variants and maker cycles), oldest first."""
    rows=[]
    for name,rec in list(getattr(edge,'strategies',{}).items())+[('maker',getattr(edge,'maker',None))]:
        for b in (list(rec.bets) if rec else []):rows.append({'strategy':name,**b})
    rows.sort(key=lambda r:r.get('t') or r.get('at') or 0)
    def cell(v):
        if isinstance(v,float):return f'{v:.5g}'
        return '' if v is None else str(v).replace(',',' ')
    return ','.join(EDGE_COLS)+'\n'+''.join(','.join(cell(r.get(c)) for c in EDGE_COLS)+'\n' for r in rows)
