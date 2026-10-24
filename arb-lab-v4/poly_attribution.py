"""Accounting for observed closed paper positions, separate from shadow labels."""
import math,time
from collections import deque,defaultdict

class Attribution:
    def __init__(self):self.rows=deque(maxlen=5000);self.ids=set()
    def add(self,row):
        ident=row.get('id')
        if not ident or ident in self.ids:return
        if len(self.rows)==self.rows.maxlen:self.ids.discard(self.rows[0]['id'])
        self.rows.append(row);self.ids.add(ident)
    def summary(self):
        groups=defaultdict(list)
        for r in self.rows:groups[(r['asset'],r.get('strategy','unknown'))].append(r)
        def stats(rows):
            pnl=[r['netPnlUsd'] for r in rows];gain=sum(max(0,p) for p in pnl);loss=-sum(min(0,p) for p in pnl)
            return {'trades':len(rows),'netPnlUsd':sum(pnl),'grossPnlUsd':sum(r['grossPnlUsd'] for r in rows),
                    'feesUsd':sum(r['feesUsd'] for r in rows),'winRatePct':100*sum(p>0 for p in pnl)/len(pnl) if pnl else None,
                    'expectancyUsd':sum(pnl)/len(pnl) if pnl else None,'profitFactor':gain/loss if loss else None,
                    'averageHoldSec':sum(r['holdSec'] for r in rows)/len(rows) if rows else None,
                    'stopOvershootUsd':sum(r.get('stopOvershootUsd',0) for r in rows),
                    'averageEntryLatencyMs':sum(r.get('entryLatencyMs',0) for r in rows)/len(rows) if rows else None}
        return {'scope':'Observed V16.1 position closes only; earlier retained ledger totals remain separate.',
                'retainedLimit':5000,'total':stats(list(self.rows)),
                'groups':[{'asset':a,'strategy':s,**stats(rows)} for (a,s),rows in groups.items()],
                'recent':list(self.rows)[-20:][::-1]}

def closed_trade(asset,session,pos,exit_price,exit_fee,reason,now=None,latency_ms=0):
    now=time.time() if now is None else now;qty=float(pos['shares']);entry=float(pos['entryPrice'])
    entry_fee=float(pos.get('entryFee',pos['entryTotalCost']-qty*entry));gross=qty*(exit_price-entry)
    fees=entry_fee+exit_fee;net=qty*exit_price-exit_fee-pos['entryTotalCost'];stop=float(pos.get('stopLossUsd') or 0)
    return {'id':pos.get('tradeId') or f'{session}:{asset}:{pos["entryAt"]}',
            'asset':asset,'sessionId':session,'marketSlug':pos['marketSlug'],'side':pos['side'],
            'strategy':pos.get('mode','unknown'),'entryAt':pos['entryAt'],'closedAt':now,'quantity':qty,
            'entryPrice':entry,'exitPrice':exit_price,'grossPnlUsd':gross,'feesUsd':fees,'netPnlUsd':net,
            'holdSec':max(0,now-pos['entryAt']),'exitReason':reason,'stopOvershootUsd':max(0,-net-stop) if 'stop' in reason and stop else 0,
            'entryLatencyMs':pos.get('entryLatencyMs',0),'exitLatencyMs':latency_ms,
            'entrySlippageUsd':qty*(entry-pos.get('requestedAsk',entry)),
            'entryFeatures':pos.get('entryFeatures',{}),'accountingErrorUsd':net-(gross-fees),
            # identity, latency intervals and stop evidence (added 2026-10-25; absent on older trades)
            'strategyVersion':pos.get('strategyVersion'),'configHash':pos.get('configHash'),'experimentsHash':pos.get('experimentsHash'),
            'stopLossUsd':pos.get('stopLossUsd'),'takeProfitUsd':pos.get('takeProfitUsd'),'entryBid':pos.get('entryBid'),
            'signalToOrderMs':pos.get('signalToOrderMs'),
            'exitTriggerToFillMs':round((now-float(pos['exitRequestedAt']))*1000,1) if pos.get('exitRequestedAt') else None,
            'exitRetries':int(pos.get('exitRetries') or 0),'exitTriggerBid':pos.get('exitTriggerBid'),
            'exitTriggerBookAgeSec':pos.get('exitTriggerBookAgeSec'),'exitTriggerBids':pos.get('exitTriggerBids'),
            'experiments':_experiments(pos,net)}

def _experiments(pos,net):
    if not pos.get('experiments'):return None
    try:
        import poly_experiments
        return poly_experiments.finalize(pos,net)
    except Exception as exc:return {'error':type(exc).__name__}
