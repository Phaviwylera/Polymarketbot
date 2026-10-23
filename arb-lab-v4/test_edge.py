import asyncio,math,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
import poly_edge as edge
from poly_edge import Calibrator,StrategyRecord,WalletIntel,EdgeLab,size_stake,kelly_binary

def bet(ret,start,won=True):
    return {'returnPerDollar':ret,'roundStart':start,'won':won,'side':'UP','cost':.5}

class CalibrationTests(unittest.TestCase):
    def test_prior_dominates_empty_cell_then_data_moves_estimate(self):
        c=Calibrator();e=c.estimate(.7,.6,100)
        self.assertAlmostEqual(e['pUp'],.6);self.assertEqual(e['cellRounds'],0)
        tb=edge.time_bucket(100);pb=edge.prob_bin(.7)
        for _ in range(90):c.update([{'tb':tb,'pb':pb,'fairUp':.6,'pUp':.6}],True)
        after=c.estimate(.7,.6,100)
        self.assertGreater(after['pUp'],.95);self.assertLess(after['se'],e['se'])
        # Other cells are untouched.
        self.assertAlmostEqual(c.estimate(.2,.3,100)['pUp'],.3)
    def test_brier_scores_the_estimate_live_before_learning(self):
        c=Calibrator();c.update([{'tb':0,'pb':5,'fairUp':.5,'pUp':.9}],True)
        s=c.summary();self.assertAlmostEqual(s['brierHeuristic'],.25);self.assertAlmostEqual(s['brierCalibrated'],.01)
    def test_export_restore_roundtrip(self):
        c=Calibrator();c.update([{'tb':2,'pb':3,'fairUp':.4,'pUp':.4}],False)
        d=Calibrator();d.restore(c.export());self.assertEqual(d.cells[(2,3)],[1.,0.])

class SizingTests(unittest.TestCase):
    def test_kelly_binary(self):
        self.assertAlmostEqual(kelly_binary(.6,.5),.2);self.assertEqual(kelly_binary(.4,.5),0)
    def test_quarter_kelly_capped_at_15_percent_of_allocation(self):
        shares=size_stake(kelly_full=.8,equity=60,allocation=60,loss_budget=100,unit_cost=.5,min_shares=5)
        self.assertAlmostEqual(shares*.5,9.,places=1)   # 0.25*0.8*60=12 -> capped at 15% of 60 = 9
        shares=size_stake(kelly_full=.4,equity=60,allocation=60,loss_budget=100,unit_cost=.5,min_shares=5)
        self.assertAlmostEqual(shares*.5,6.,places=1)   # 0.25*0.4*60=6
    def test_loss_budget_caps_stake(self):
        shares=size_stake(kelly_full=.8,equity=60,allocation=60,loss_budget=3,unit_cost=.5,min_shares=5)
        self.assertLessEqual(shares*.5,3+1e-9)
    def test_minimum_order_never_exceeds_full_kelly(self):
        # quarter Kelly $1.5 < min order $2.6, but full Kelly $6 >= $2.6 -> minimum order allowed
        self.assertEqual(size_stake(kelly_full=.1,equity=60,allocation=60,loss_budget=10,unit_cost=.52,min_shares=5),5.)
        # full Kelly $1.2 < $2.6 -> skip
        self.assertEqual(size_stake(kelly_full=.02,equity=60,allocation=60,loss_budget=10,unit_cost=.52,min_shares=5),0.)
        self.assertEqual(size_stake(kelly_full=0,equity=60,allocation=60,loss_budget=10,unit_cost=.5,min_shares=5),0.)

class GateTests(unittest.TestCase):
    def test_gate_requires_volume_rounds_and_significance(self):
        s=StrategyRecord('edge')
        for i in range(39):s.add(bet(.2,i))
        self.assertFalse(s.stats()['gatePassed'])          # too few bets
        s.add(bet(.2,39));self.assertTrue(s.stats()['gatePassed'])    # 40 consistent winners across 40 rounds
        few=StrategyRecord('edge')
        for i in range(50):few.add(bet(.2,i%5))
        self.assertFalse(few.stats()['gatePassed'])        # only 5 distinct rounds
    def test_positive_noisy_record_passes_and_losing_record_fails(self):
        s=StrategyRecord('edge')
        for i in range(60):s.add(bet(.9 if i%2 else -.5,i,bool(i%2)))
        self.assertTrue(s.stats()['gatePassed'])
        bad=StrategyRecord('edge')
        for i in range(60):bad.add(bet(.5 if i%3==0 else -.5,i))
        self.assertFalse(bad.stats()['gatePassed'])
    def test_gate_uses_recent_window_so_decay_demotes(self):
        s=StrategyRecord('edge')
        for i in range(60):s.add(bet(.9 if i%2 else -.5,i))
        for i in range(60,260):s.add(bet(-.5 if i%2 else .3,i))
        self.assertFalse(s.stats()['gatePassed'])

class WalletTests(unittest.TestCase):
    def rows(self,wallet,outcome,side,price,size,t):
        return {'proxyWallet':wallet,'outcome':outcome.title(),'side':side,'price':price,'size':size,'timestamp':t}
    def test_parse_and_round_pnl(self):
        w=WalletIntel()
        trades=w.parse([self.rows('0xA','up','BUY',.4,10,1000),self.rows('0xA','down','SELL',.7,5,1010),{'bad':1}])
        self.assertEqual(len(trades),2)
        w.learn_round(trades,'up',990)
        a=w.wallets['0xa']
        # buy up 10 @ .4 wins: +6 ; sell down 5 @ .7 and down loses: +3.5
        self.assertAlmostEqual(a['pnl'],9.5);self.assertEqual(a['rounds'],1);self.assertEqual(a['entrySec'],[10.])
    def test_skill_needs_repeat_positive_history_and_live_flow_counts_only_skilled(self):
        w=WalletIntel()
        for r in range(8):
            w.learn_round(w.parse([self.rows('0xgood','up','BUY',.4,10,r*300+30),self.rows('0xluck','up','BUY',.4,10,r*300+30)]),'up' if r!=3 else 'down',r*300)
        w.learn_round(w.parse([self.rows('0xonce','down','BUY',.1,100,5000)]),'down',4990)
        skilled=w.skilled();self.assertIn('0xgood',skilled);self.assertNotIn('0xonce',skilled)
        now=10000
        w.live_flow('BTC',w.parse([self.rows('0xgood','down','BUY',.5,7,now-5),self.rows('0xonce','up','BUY',.5,99,now-5),self.rows('0xgood','up','BUY',.5,3,now-500)]),now,skilled)
        self.assertEqual(w.live['BTC']['down'],7.);self.assertEqual(w.live['BTC']['up'],0.)

class FakeEngine:
    def __init__(self,now,ask_up=.40,ask_down=.62):
        self.market={'slug':'btc-updown-5m-1','startTs':now-120,'endTs':now+180,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01,'conditionId':'0xc'}
        self.open_trusted=True;self.feed=dict.fromkeys(('market','clob','rtds'),'live');self.spot_ts=self.twap_ts=now
        self.bbo={'up':{'bid':ask_up-.01,'ask':ask_up,'bidSize':50,'askSize':50},'down':{'bid':ask_down-.01,'ask':ask_down,'bidSize':50,'askSize':50}}
        self.book_observed={'up':now,'down':now}

class EdgeLabTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.lab=EdgeLab({})
        self.m={'rawFairUp':.75,'fairUp':.70,'fairDown':.30}
    def teach(self,up_rate,n=200):
        tb=edge.time_bucket(180);pb=edge.prob_bin(.75)
        for i in range(n):self.lab.cal.update([{'tb':tb,'pb':pb,'fairUp':.7,'pUp':.7}],i<up_rate*n)
    def test_detects_mispricing_against_calibrated_probability(self):
        self.teach(.75);e=FakeEngine(self.now)
        c=self.lab.evaluate('BTC',e,self.m,self.now)
        self.assertIsNotNone(c);self.assertEqual(c['side'],'UP');self.assertGreater(c['conservativeEdge'],0)
        self.assertGreater(c['kellyFull'],0)
    def test_no_signal_when_price_is_fair(self):
        self.teach(.42);e=FakeEngine(self.now)
        self.assertIsNone(self.lab.evaluate('BTC',e,self.m,self.now))
    def test_stale_or_untrusted_inputs_never_signal(self):
        self.teach(.75)
        for breaker in ('trust','oracle','book','spread'):
            e=FakeEngine(self.now)
            if breaker=='trust':e.open_trusted=False
            if breaker=='oracle':e.spot_ts=self.now-10
            if breaker=='book':e.book_observed={'up':self.now-10,'down':self.now-10}
            if breaker=='spread':e.bbo['up']['bid']=.30
            self.assertIsNone(self.lab.evaluate('BTC',e,self.m,self.now),breaker)
    def test_shadow_bet_recorded_once_per_round_then_settled_and_learned(self):
        self.teach(.75);e=FakeEngine(self.now)
        for _ in range(5):self.lab.evaluate('BTC',e,self.m,self.now)
        r=self.lab.rounds['btc-updown-5m-1'];self.assertIn('edge',r['bets']);self.assertNotIn('edge_smart',r['bets'])
        before=self.lab.cal.summary()['samples']
        self.lab.settle('btc-updown-5m-1','up')
        st=self.lab.strategies['edge'].stats();self.assertEqual(st['bets'],1);self.assertEqual(st['wins'],1)
        self.assertGreater(self.lab.cal.summary()['samples'],before)
        self.assertEqual(list(self.lab._pending_study)[0][:2],('0xc','up'))
    def test_not_tradable_until_promoted(self):
        self.teach(.75);c=self.lab.evaluate('BTC',FakeEngine(self.now),self.m,self.now)
        self.assertIsNone(self.lab.tradable(c))
        for i in range(60):self.lab.strategies['edge'].add(bet(.9 if i%2 else -.5,i))
        self.assertEqual(self.lab.tradable(c),'edge')
    def test_hold_exit_only_when_market_overpays(self):
        self.teach(.75);self.lab.evaluate('BTC',FakeEngine(self.now),self.m,self.now)
        p=self.lab.signals['BTC']['pUp']
        self.assertIsNone(self.lab.hold_exit('BTC','UP',p-.05))
        self.assertIsNotNone(self.lab.hold_exit('BTC','UP',min(.99,p+.05)))
    def test_export_restore_and_readiness(self):
        self.teach(.75)
        for i in range(10):self.lab.on_trade({'netPnlUsd':.1,'closedAt':1000+i})
        state=self.lab.export();other=EdgeLab({});self.assertTrue(other.restore(state))
        self.assertEqual(len(other.trades),10);self.assertEqual(other.cal.summary()['samples'],self.lab.cal.summary()['samples'])
        r=other.readiness(200);self.assertFalse(r['readyForRealMoney']);self.assertFalse(r['liveTradingEnabled'])

class EngineStrategyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=mod.PolyServerDemo('BTC',60,state_file=Path(self.tmp.name)/'s.json',risk_profile={'momentumEnabled':True,'sessionLossLimit':3,'stopLoss':.30})
        e=self.e;e._save=lambda:None;e._new_session();self.addCleanup(e.close);now=time.time()
        e.market={'slug':'btc-updown-5m-1','upToken':'u','downToken':'d','startTs':now-120,'endTs':now+180,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01,'conditionId':'0xc'}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.open_trusted=True;e.spot_ts=e.twap_ts=now
        e._ingest_book('up',{'bids':[{'price':'.39','size':'200'}],'asks':[{'price':'.40','size':'200'}]})
        e._ingest_book('down',{'bids':[{'price':'.59','size':'200'}],'asks':[{'price':'.60','size':'200'}]})
        self.m={'rawFairUp':.75,'fairUp':.70,'fairDown':.30,'leadBps':0.,'impulse5Bps':0.}
        e._model=lambda:self.m
        self.lab=EdgeLab({'BTC':e});e.edge=self.lab
        tb=edge.time_bucket(180);pb=edge.prob_bin(.75)
        for i in range(200):self.lab.cal.update([{'tb':tb,'pb':pb,'fairUp':.7,'pUp':.7}],i<150)
        self.latency=patch.object(mod,'EXECUTION_LATENCY_SEC',0);self.latency.start();self.addCleanup(self.latency.stop)
    def promote(self):
        for i in range(60):self.lab.strategies['edge'].add(bet(.9 if i%2 else -.5,i))
    async def test_shadow_strategy_does_not_trade(self):
        await self.e._momentum_step(self.m)
        self.assertIsNone(self.e.paper['position']);self.assertIn('not promoted',self.e.edge_decision)
    async def test_promoted_edge_buys_kelly_size_within_caps_and_settles(self):
        self.promote();await self.e._momentum_step(self.m)
        pos=self.e.paper['position'];self.assertIsNotNone(pos);self.assertEqual(pos['mode'],'EDGE_HOLD');self.assertEqual(pos['side'],'UP')
        self.assertGreater(pos['shares'],5)                          # confidence sizing above the old fixed minimum
        self.assertLessEqual(pos['entryTotalCost'],60*.15+1e-6)      # 15% of allocation
        self.assertLessEqual(pos['entryTotalCost'],3+1e-6)           # never more than the remaining loss budget
        cash=self.e.paper['cash']
        async def winner(slug):return 'up'
        self.e._resolved_winner=winner
        await self.e._settle_position('btc-updown-5m-1')
        self.assertIsNone(self.e.paper['position']);self.assertAlmostEqual(self.e.paper['cash'],cash+pos['shares'])
        self.assertGreater(self.e.paper['realizedPnl'],0)
    async def test_edge_hold_is_not_closed_by_scalp_stops(self):
        self.promote();await self.e._momentum_step(self.m)
        self.e._ingest_book('up',{'bids':[{'price':'.20','size':'200'}],'asks':[{'price':'.21','size':'200'}]})
        await self.e._momentum_manage(self.m)
        self.assertIsNotNone(self.e.paper['position'])              # binary is held to verified resolution
    async def test_edge_hold_takes_profit_when_market_overpays(self):
        self.promote();await self.e._momentum_step(self.m);shares=self.e.paper['position']['shares']
        self.e._ingest_book('up',{'bids':[{'price':'.95','size':'500'}],'asks':[{'price':'.96','size':'500'}]})
        self.lab.evaluate('BTC',self.e,self.m)
        await self.e._momentum_manage(self.m)
        self.assertIsNone(self.e.paper['position']);self.assertGreater(self.e.paper['realizedPnl'],0)
    async def test_losing_settlement_starts_cooldown(self):
        self.promote();await self.e._momentum_step(self.m)
        async def loser(slug):return 'down'
        self.e._resolved_winner=loser;await self.e._settle_position('btc-updown-5m-1')
        self.assertLess(self.e.paper['realizedPnl'],0);self.assertGreater(self.e.paper['cooldownUntil'],time.time())
    async def test_portfolio_loss_budget_blocks_entries(self):
        self.promote();self.e.loss_budget=lambda:0.
        await self.e._momentum_step(self.m);self.assertIsNone(self.e.paper['position'])
    async def test_model_scalp_requires_evidence_gate(self):
        now=time.time()
        pred={'evidenceGatePassed':False,'conservativeNetReturnPerShare':.05,'expectedNetReturnPerShare':.08,'empiricalErrorHaircut':.06}
        self.e.research_state={'sides':{'up':{'at':now,'prediction':pred}}}
        self.assertIsNone(self.e._model_candidate()[0])
        pred['evidenceGatePassed']=True
        c,reason=self.e._model_candidate();self.assertIsNotNone(c,reason)
        self.assertGreaterEqual(c['shares'],5);self.assertLessEqual(c['shares']*c['cost'],60*.15+1e-6)
        self.assertAlmostEqual(c['stopLossUsd'],.30*c['shares']/5,places=3)

class AutoCycleTests(unittest.TestCase):
    def test_complete_and_cooled_locked_lanes_roll_over_without_exposure(self):
        import poly_portfolio
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d}):
            pf=poly_portfolio.PaperPortfolio();pf.started=True
            for e in pf.engines.values():e._save=lambda:None;e._new_session()
            btc=pf.engines['BTC'];old=btc.paper['sessionId'];btc.paper.update(status='complete',trades=3,realizedPnl=-.5)
            eth=pf.engines['ETH'];eth.paper.update(status='risk_locked',trades=2,realizedPnl=-1)
            now=time.time()
            self.assertEqual(pf.auto_cycle(now),['BTC'])
            self.assertNotEqual(btc.paper['sessionId'],old);self.assertIn(old,pf.ledger_history['BTC'])
            self.assertEqual(pf.auto_cycle(now+60),[])                 # lock cooling down
            eth.paper['position']={'marketSlug':'x'}
            self.assertEqual(pf.auto_cycle(now+3600),[])               # open exposure is never reset
            eth.paper['position']=None
            self.assertEqual(pf.auto_cycle(now+3600),['ETH'])
            for e in pf.engines.values():e.close()

if __name__=='__main__':unittest.main()
