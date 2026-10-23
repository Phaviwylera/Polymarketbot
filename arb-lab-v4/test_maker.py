import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
from poly_pair_shadow import PairShadow,adverse,POLICY
from poly_edge import EdgeLab
from poly_attribution import Attribution
from test_v16_research import context,NOW

class GuardTests(unittest.TestCase):
    def test_adverse_detects_moves_against_the_quoted_side_only(self):
        c={'model':{'impulse5Bps':-1.2},'fast':{'binance':None,'coinbase':None}}
        self.assertIn('oracle impulse',adverse(c,'up',0));self.assertIsNone(adverse(c,'down',0))
        c={'model':{'impulse5Bps':0},'fast':{'binance':2.0}}
        self.assertIn('binance',adverse(c,'down',0));self.assertIsNone(adverse(c,'up',0))
        self.assertIsNone(adverse({'model':{'impulse5Bps':-.3},'fast':{}},'up',0))   # inside threshold
    def test_policy_is_versioned(self):self.assertIn('v3-guard',POLICY)

class PairGuardTests(unittest.TestCase):
    def setUp(self):self.events=[];self.p=PairShadow('BTC',60,lambda k,r:self.events.append((k,r)))
    def c(self,t,imp=.3,up=(.4,.42),down=(.54,.55)):
        c=context(t);c['market']['slug']='btc-pair';c['model']={**c['model'],'impulse5Bps':imp};c['fast']={}
        for s,(bid,ask) in zip(('up','down'),(up,down)):
            c['bbo'][s]={'bid':bid,'ask':ask};c['depth'][s]={'bids':[{'price':bid,'size':10}],'asks':[{'price':ask,'size':100}]}
        return c
    def test_resting_quote_is_cancelled_when_price_moves_against_it(self):
        self.p.step(self.c(NOW),NOW);side=self.p.active['side']
        self.p.step(self.c(NOW+.4,imp=-2 if side=='up' else 2),NOW+.4)
        self.assertIsNone(self.p.active);self.assertEqual(self.p.counts['guardCancels'],1)
        self.assertTrue(any('adverse-selection guard' in r.get('reason','') for k,r in self.events if k=='PAIR_CANCEL'))
    def test_no_quote_on_a_side_moving_against_it(self):
        self.p.step(self.c(NOW,imp=-3),NOW)
        self.assertTrue(self.p.active is None or self.p.active['side']=='down')
    def test_markout_recorded_and_carried_into_outcome(self):
        self.p.step(self.c(NOW),NOW);self.p.step(self.c(NOW+.4),NOW+.4);side=self.p.active['side']
        c=self.c(NOW+.8);c['tradePrints']=[{'seq':1,'t':NOW+.8,'receivedAt':NOW+.8,'marketSlug':'btc-pair','side':side,'aggressor':'SELL','price':self.p.active['price'],'size':15,'feedReady':True}]
        # make the hedge too expensive so the first leg stays open long enough for a markout
        if side=='up':c['bbo']['down']={'bid':.64,'ask':.65};c['depth']['down']={'bids':[{'price':.64,'size':10}],'asks':[{'price':.65,'size':100}]}
        else:c['bbo']['up']={'bid':.52,'ask':.53};c['depth']['up']={'bids':[{'price':.52,'size':10}],'asks':[{'price':.53,'size':100}]}
        self.p.step(c,NOW+.8)
        self.assertEqual(self.p.active['phase'],'hedge')
        hold=dict(up=(.40,.42),down=(.64,.65)) if side=='up' else dict(up=(.52,.53),down=(.54,.55))
        self.p.step(self.c(NOW+3,**hold),NOW+3)
        later=dict(up=(.38,.40),down=(.64,.65)) if side=='up' else dict(up=(.52,.53),down=(.52,.54))
        self.p.step(self.c(NOW+5.9,**later),NOW+5.9)
        if True:
            self.assertEqual(len(self.p.markouts),1)
            self.assertIn('meanMarkout',self.p.snapshot()['adverseSelection'])

class MakerGateTests(unittest.TestCase):
    def row(self,net,start,q=5):return {'quantity':q,'netPnlUsd':net,'roundStart':start,'status':'paired','closedAt':start+60,'asset':'BTC'}
    def test_gate_from_pair_outcomes_and_persistence(self):
        lab=EdgeLab({})
        for i in range(30):lab.on_pair_outcome(self.row(.2,i))
        self.assertFalse(lab.maker_promoted())                      # fewer than 40 cycles
        for i in range(30,60):lab.on_pair_outcome(self.row(.25 if i%2 else .1,i))
        self.assertTrue(lab.maker_promoted())
        other=EdgeLab({});other.restore(lab.export());self.assertTrue(other.maker_promoted())
        self.assertIn('maker',lab.snapshot())
    def test_losing_maker_record_stays_shadow(self):
        lab=EdgeLab({})
        for i in range(60):lab.on_pair_outcome(self.row(.2 if i%3==0 else -.3,i))
        self.assertFalse(lab.maker_promoted())
    def test_ignores_rows_without_quantity_or_pnl(self):
        lab=EdgeLab({});lab.on_pair_outcome({'quantity':0,'netPnlUsd':1});lab.on_pair_outcome({'quantity':5})
        self.assertEqual(len(lab.maker.bets),0)

class FundedMakerTests(unittest.TestCase):
    def engine(self,interval=300):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        e=mod.PolyServerDemo('BTC',60,state_file=Path(d.name)/'s.json',interval=interval,lane='BTC' if interval==300 else 'BTC15',risk_profile={'momentumEnabled':True,'walletQuotes':True,'residualCap':1.5,'firstPriceCap':.6,'stopLoss':.3,'sessionLossLimit':3})
        e._save=lambda:None;e._new_session();self.addCleanup(e.close);e.edge=EdgeLab({'BTC':e});return e
    def promote(self,e):
        for i in range(60):e.edge.on_pair_outcome({'quantity':5,'netPnlUsd':.25 if i%2 else .1,'roundStart':i})
    def test_funded_maker_off_until_promoted_and_only_on_5m(self):
        e=self.engine();self.assertFalse(e._maker_enabled());self.assertIn('not promoted',e.maker_decision)
        self.promote(e);self.assertTrue(e._maker_enabled())
        e15=self.engine(900);self.promote(e15);self.assertFalse(e15._maker_enabled());self.assertIn('5-minute',e15.maker_decision)
    def test_funded_guard(self):
        e=self.engine();self.assertIsNotNone(e._maker_guard('up',{'impulse5Bps':-1}));self.assertIsNone(e._maker_guard('down',{'impulse5Bps':-1}))
    def test_inventory_closes_reach_the_trade_record(self):
        e=self.engine();att=Attribution();rows=[];e.trade_observer=lambda r:(att.add(r),rows.append(r))
        e._report_inventory(.12,5,.97,1.0,'complete-set merge')
        self.assertEqual(rows[0]['strategy'],'PAIR_INVENTORY');self.assertAlmostEqual(att.summary()['total']['netPnlUsd'],.12)

if __name__=='__main__':unittest.main()

class HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_momentum_rejects_entries_at_or_above_45c(self):
        import poly_momentum
        self.assertEqual(poly_momentum.MAX_ENTRY_PRICE,.45)
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        e=mod.PolyServerDemo('ETH',80,state_file=Path(d.name)/'s.json',risk_profile={'momentumEnabled':True,'stopLoss':.40,'sessionLossLimit':4})
        e._save=lambda:None;e._new_session();self.addCleanup(e.close);now=time.time()
        e.market={'slug':'eth-t','upToken':'u','downToken':'d','startTs':now-60,'endTs':now+240,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.open_trusted=True;e.spot_ts=e.twap_ts=now
        e._ingest_book('up',{'bids':[{'price':'.46','size':'60'}],'asks':[{'price':'.47','size':'60'}]})
        e._ingest_book('down',{'bids':[{'price':'.52','size':'60'}],'asks':[{'price':'.53','size':'60'}]})
        e.book_history.append({'t':now-3,'marketSlug':'eth-t','up':.445,'down':.54,'upBid':.45})
        c,reason=e._momentum_signal({'fairUp':.80,'fairDown':.20,'leadBps':1.,'impulse5Bps':1.5})
        self.assertIsNone(c);self.assertIn('45c',reason)
    async def test_yield_stops_entries_and_engines(self):
        import poly_portfolio
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d}):
            pf=poly_portfolio.PaperPortfolio()
            for e in pf.engines.values():e._save=lambda:None;e._new_session()
            self.assertTrue(pf.entry_allowed())
            await pf.yield_to('newer-boot')
            self.assertTrue(pf.yielded);self.assertFalse(pf.entry_allowed())
            for e in pf.engines.values():self.assertEqual(e.tasks,[]);e.close()
    async def test_research_rows_handed_over_without_duplicates(self):
        import poly_portfolio,asyncio
        from poly_external import ExchangeFeeds
        from poly_semantic import SemanticResearch
        async def parked(*a):await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d}):
            pf=poly_portfolio.PaperPortfolio()
            pf.handover_rows=[{'id':'a','x':1},{'id':'b','x':2},{'id':'a','x':1},'junk']
            with patch.object(ExchangeFeeds,'worker',parked),patch.object(SemanticResearch,'worker',parked):
                pf.start_research()
                self.assertEqual(sorted(r['id'] for r in pf.research.rows),['a','b'])
                await pf.research.close()
            for e in pf.engines.values():e.close()

class PairAttemptTests(unittest.TestCase):
    def c(self,t):
        c=context(t);c['market']['slug']='btc-pair';c['model']={**c['model'],'impulse5Bps':1.5};c['fast']={}
        for s,(bid,ask) in zip(('up','down'),((.4,.42),(.54,.55))):
            c['bbo'][s]={'bid':bid,'ask':ask};c['depth'][s]={'bids':[{'price':bid,'size':10}],'asks':[{'price':ask,'size':100}]}
        return c
    def test_requotes_after_ttl_up_to_the_round_limit(self):
        import poly_pair_shadow as ps
        p=PairShadow('BTC',60,lambda k,r:None);t=NOW
        for i in range(ps.MAX_ATTEMPTS_PER_ROUND):
            p.step(self.c(t),t);self.assertIsNotNone(p.active,f'attempt {i}')
            for k in range(1,int(ps.QUOTE_TTL_SEC)+2,2):p.step(self.c(t+k),t+k)   # no prints: TTL expiry
            self.assertIsNone(p.active);t+=ps.QUOTE_TTL_SEC+3
        p.step(self.c(t),t);self.assertIsNone(p.active);self.assertIn('attempts this round',p.reason)
        self.assertEqual(p.counts['quotes'],ps.MAX_ATTEMPTS_PER_ROUND)
