"""Replay the production momentum path on retained STATE samples (not tick-complete).
Usage: python replay_momentum.py /path/to/development.zip
Latency advances to the next recorded observation at least 350ms later. Missing
book-event ages are treated as observed at sample receipt; this is an execution
regression check, not an out-of-sample profitability backtest.
"""
import asyncio,copy,json,sys,tempfile,zipfile
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
ALLOCATIONS={'BTC':60.,'ETH':80.,'SOL':30.,'XRP':30.}

async def replay(asset,rows,tmp):
    now=[rows[0]['t']];index=[0];latest=[None]
    with patch.object(mod.time,'time',side_effect=lambda:now[0]):
        e=mod.PolyServerDemo(asset,ALLOCATIONS[asset],state_file=Path(tmp)/(asset+'.json'),risk_profile={'momentumEnabled':True,'sessionLossLimit':ALLOCATIONS[asset]*.05})
        e._save=lambda:None;e._new_session();e._model=lambda:latest[0]
        def apply(i):
            r=rows[i];now[0]=r['t'];e.market=copy.deepcopy(r['market']);e.bbo=copy.deepcopy(r['bbo']);e.feed=copy.deepcopy(r['feed'])
            e.spot=r.get('spot');e.spot_ts=r['spotTs'];e.twap=r.get('twap');e.twap_ts=r['twapTs'];e.open_trusted=r.get('openTrusted');e.open_ref=r.get('openRef')
            e.book_observed={'up':r['t'],'down':r['t']};latest[0]=r.get('model')
        async def latency(seconds):
            target=now[0]+seconds;j=index[0]+1
            while j<len(rows) and rows[j]['t']<target:j+=1
            if j<len(rows):index[0]=j;apply(j)
            else:now[0]=target
        try:
            with patch.object(mod.asyncio,'sleep',side_effect=latency):
                while index[0]<len(rows):
                    apply(index[0]);await e._momentum_step(latest[0]);index[0]+=1
            p=e.paper;events=[x for x in reversed(p['events']) if x['action'] in {'BUY','SELL','NOFILL'}]
            return {'asset':asset,'samples':len(rows),'orders':p.get('momentumOrders',0),'fills':p.get('momentumFills',0),'closedTrades':p['trades'],'realizedPnl':round(p['realizedPnl'],6),'markedPnl':round(e._equity()-ALLOCATIONS[asset],6),'fees':round(p['fees'],6),'openPosition':p.get('position'),'events':events}
        finally:e.close()

async def main(path):
    results=[]
    with tempfile.TemporaryDirectory() as tmp,zipfile.ZipFile(path) as z:
        for asset in ALLOCATIONS:
            rows=[]
            for line in z.open(asset+'.ndjson'):
                r=json.loads(line)
                if r.get('kind')=='STATE' and r.get('context',{}).get('market'):rows.append({'t':r['t'],**r['context']})
            results.append(await replay(asset,rows,tmp))
    return {'purpose':'production-path execution regression on retained samples; not a profitability backtest','limitations':['State samples are roughly 1–4 seconds apart, not tick complete.','Retained asset windows differ; earlier records were pruned.','Unknown historical book ages are treated as observed at sample receipt.','Parameters were reviewed using this sample; results are in-sample and not validation of future returns.'],'assets':results,'totalRealizedPnl':round(sum(x['realizedPnl'] for x in results),6)}

if __name__=='__main__':
    result=asyncio.run(main(sys.argv[1]));print('REPLAY_RESULT_JSON '+json.dumps(result))
