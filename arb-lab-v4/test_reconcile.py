import json,os,time,unittest
import poly_reconcile as pr
from poly_portfolio import PaperPortfolio

def ev(t,a,pnl=None,price=.4,detail='MOMENTUM_SCALP; 5.00 shares'):
    e={'t':t,'action':a,'price':price,'detail':detail,'side':'UP'}
    if pnl is not None:e['pnl']=pnl
    return e

class MissingTradeTests(unittest.TestCase):
    def setUp(self):
        self.p=PaperPortfolio()
        for e in self.p.engines.values():e._save=lambda:None;e._new_session()
        self.p._save_history=lambda:None
        eth=self.p.engines['ETH'].paper;self.sid=eth['sessionId']
        # live session (restored from the copy) has one closed trade
        eth['events']=[ev(1010,'SELL',.5,.5,'momentum net profit target'),ev(1000,'BUY')]
        # the old database's version of the same session has a second, later trade
        self.old={'assets':{'ETH':{'paper':{'sessionId':self.sid,'startingBalance':80,'events':[
            ev(2010,'SELL',-.45,.33,'momentum net loss stop'),ev(2000,'BUY'),ev(1010,'SELL',.5,.5,'momentum net profit target'),ev(1000,'BUY')]}}},
            'ledgerHistory':{}}
    def test_recovers_only_the_trade_the_live_ledger_lacks(self):
        r=pr.missing_trades(self.old,self.p,after_t=1500)
        self.assertEqual(len(r),1);lane,s=r[0]
        self.assertEqual((lane,s['trades'],s['realizedPnl']),('ETH',1,-.45));self.assertEqual(s['sessionId'],'recovered:'+self.sid)
        self.assertEqual([e['action'] for e in reversed(s['events'])],['BUY','SELL'])
    def test_idempotent_and_ledger_counts_it(self):
        for lane,s in pr.missing_trades(self.old,self.p,after_t=1500):self.p.ledger_history[lane][s['sessionId']]=s
        self.assertEqual(pr.missing_trades(self.old,self.p,after_t=1500),[])
        self.p.epoch=1.;pf=self.p.snapshot()['portfolio']
        self.assertAlmostEqual(pf['accountPnl'],-.45+0);self.assertEqual(pf['lifetimeTrades'],1)
        import poly_audit
        led=poly_audit.ledger_reconcile([{'lane':'ETH','live':False,'paper':s} for s in self.p.ledger_history['ETH'].values()],0.,0.)
        self.assertEqual(led['sessionsWithProblems'],0)
    def test_never_recovers_trades_from_before_the_copy(self):
        self.assertEqual(pr.missing_trades(self.old,self.p,after_t=2050),[])

SRC=os.getenv('POLY_TEST_PG_SRC');DST=os.getenv('POLY_TEST_PG_DST')
@unittest.skipUnless(SRC and DST,'needs disposable Postgres databases')
class ReconcileDbTests(MissingTradeTests,unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_against_two_databases(self):
        import asyncpg
        from poly_storage import SCHEMA
        now=time.time()
        for u in (SRC,DST):
            c=await asyncpg.connect(u);await c.execute('DROP TABLE IF EXISTS poly_v161_state;DROP TABLE IF EXISTS poly_v161_events');await c.execute(SCHEMA);await c.close()
        old=json.loads(json.dumps(self.old))
        for e in old['assets']['ETH']['paper']['events']:e['t']+=now-3000
        live=self.p.engines['ETH'].paper
        for e in live['events']:e['t']+=now-3000
        c=await asyncpg.connect(SRC)
        await c.execute("INSERT INTO poly_v161_state VALUES('paper',1,$1::jsonb)",json.dumps(old))
        trade={'id':'t-late','asset':'ETH','netPnlUsd':-.45}
        await c.execute("INSERT INTO poly_v161_events VALUES('late',$1,'ETH','TRADE_ATTRIBUTION',$2::jsonb)",now-990,json.dumps({'trade':trade}))
        await c.close()
        class Store:url=DST;engine=self.p
        logs=[];r=await pr.reconcile(SRC,Store(),log=logs.append)
        self.assertEqual((r['tradesRecovered'],r['pnlRecoveredUsd'],r['eventsAdded'],r['attributionRowsRecovered']),(1,-.45,1,1))
        self.assertNotIn('postgres',logs[0].replace('postgresql',''))
        r2=await pr.reconcile(SRC,Store(),log=lambda *_:None);self.assertEqual((r2['tradesRecovered'],r2['eventsAdded']),(0,0))

if __name__=='__main__':unittest.main()
