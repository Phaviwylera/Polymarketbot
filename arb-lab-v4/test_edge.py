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

class MomentumTuningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        e=self.e=mod.PolyServerDemo('ETH',80,state_file=Path(self.tmp.name)/'s.json',risk_profile={'momentumEnabled':True,'sessionLossLimit':4,'stopLoss':.40})
        e._save=lambda:None;e._new_session();self.addCleanup(e.close);now=time.time()
        e.market={'slug':'eth-test','upToken':'u','downToken':'d','startTs':now-60,'endTs':now+240,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.open_trusted=True;e.spot_ts=e.twap_ts=now
        e._ingest_book('up',{'bids':[{'price':'.39','size':'60'}],'asks':[{'price':'.40','size':'60'}]})
        e._ingest_book('down',{'bids':[{'price':'.59','size':'60'}],'asks':[{'price':'.60','size':'60'}]})
        self.m={'fairUp':.6,'fairDown':.4,'leadBps':1.,'impulse5Bps':.3}
        e.book_history.append({'t':now-3,'marketSlug':'eth-test','up':.375,'down':.615,'upBid':.38})
        e._model=lambda:self.m
        e.edge=EdgeLab({'ETH':e})
        self.latency=patch.object(mod,'EXECUTION_LATENCY_SEC',0);self.latency.start();self.addCleanup(self.latency.stop)
    def record(self,pnls):
        self.e.edge.trades.clear()
        for x in pnls:self.e.edge.trades.append({'pnl':x,'at':0,'strategy':'MOMENTUM_SCALP'})
    def test_minimum_edge_is_five_cents(self):
        import poly_momentum
        self.assertEqual(poly_momentum.MIN_EDGE,.12)
        e=self.e;now=time.time()
        e._ingest_book('up',{'bids':[{'price':'.47','size':'60'}],'asks':[{'price':'.48','size':'60'}]})
        e.book_history.clear();e.book_history.append({'t':now-3,'marketSlug':'eth-test','up':.455,'down':.53,'upBid':.46})
        self.m['fairUp']=.55;self.m['fairDown']=.45   # edge = .55-.48-fees-.01 = 2.5c: passed the old 2c floor
        c,reason=e._momentum_signal(self.m);self.assertIsNone(c);self.assertIn('12-cent',reason)
        self.m['fairUp']=.60;self.m['fairDown']=.40   # 7.6c: passed the 5c floor, fails the 12c floor
        self.assertIsNone(e._momentum_signal(self.m)[0])
        self.m['fairUp']=.70;self.m['fairDown']=.30
        self.assertIsNotNone(e._momentum_signal(self.m)[0])
    async def test_unearned_record_keeps_minimum_size(self):
        self.record([.5,-.6]*15)
        c,_=self.e._momentum_signal(self.m);c=self.e._momentum_size(c)
        self.assertEqual(c['shares'],5);self.assertFalse(self.e.momentum_sizing['earned'])
    async def test_earned_record_scales_size_stop_and_target_within_caps(self):
        self.record([.8,-.4]*15)
        c,_=self.e._momentum_signal(self.m);base=dict(c);c=self.e._momentum_size(c)
        self.assertTrue(self.e.momentum_sizing['earned']);self.assertGreater(c['shares'],5);self.assertLessEqual(c['shares'],10)
        self.assertLessEqual(c['entryTotalCost'],80*.15+1e-6)
        self.assertAlmostEqual(c['stopLossUsd'],base['stopLossUsd']*c['shares']/5,places=3)
        self.assertTrue(await self.e._momentum_enter(c))
        pos=self.e.paper['position'];self.assertEqual(pos['shares'],c['shares']);self.assertAlmostEqual(pos['stopLossUsd'],c['stopLossUsd'])
    async def test_momentum_exit_walks_depth_when_best_bid_is_thin(self):
        self.record([])
        c,_=self.e._momentum_signal(self.m);await self.e._momentum_enter(c)
        # Only 2 shares at the best bid, the rest one tick lower: the old exit waited; the walk fills.
        self.e._ingest_book('up',{'bids':[{'price':'.30','size':'2'},{'price':'.29','size':'50'}],'asks':[{'price':'.31','size':'50'}]})
        await self.e._momentum_manage(self.m)
        self.assertIsNone(self.e.paper['position']);self.assertEqual(self.e.paper['momentumCloses'],1)
        self.assertLess(self.e.paper['realizedPnl'],0)

class SeedRecordTests(unittest.TestCase):
    def test_seed_uses_archived_and_current_sessions(self):
        import poly_portfolio
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d}):
            pf=poly_portfolio.PaperPortfolio()
            for e in pf.engines.values():e._save=lambda:None;e._new_session()
            pf.ledger_history['BTC']['old']={'sessionId':'old','events':[{'t':2,'action':'SELL','side':'UP','price':.5,'pnl':.4,'detail':'x'},{'t':1,'action':'BUY','side':'UP','price':.4,'detail':'MOMENTUM_SCALP; 5 shares; estimated edge 0.1'}]}
            e=pf.engines['ETH'];e._record('BUY',side='UP',price=.4,detail='MOMENTUM_SCALP; 5 shares');e._record('SELL',side='UP',price=.3,pnl=-.5,detail='y')
            pf.edge.trades.append({'pnl':-.5,'at':3,'strategy':'MOMENTUM_SCALP'})
            pf._seed_trade_record()
            self.assertEqual([t['pnl'] for t in pf.edge.trades],[.4,-.5]);self.assertEqual(pf.edge.trades[0]['strategy'],'MOMENTUM_SCALP')
            for x in pf.engines.values():x.close()

class FifteenMinuteLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_lane_queries_15m_slug_and_round_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            e=mod.PolyServerDemo('BTC',30,state_file=Path(d)/'s.json',interval=900,lane='BTC15');self.addCleanup(e.close)
            seen=[]
            async def get(url,params=None,headers=None):
                if 'markets' in url:
                    seen.append(params['slug']);ts=int(params['slug'].rsplit('-',1)[1])
                    return [{'slug':params['slug'],'outcomes':'["Up","Down"]','clobTokenIds':'["u","d"]','acceptingOrders':True,'conditionId':'0xc'}]
                return {'bids':[{'price':'.49','size':'50'}],'asks':[{'price':'.51','size':'50'}]}
            e._get=get;m=await e.fetch_current_market()
            self.assertTrue(seen[0].startswith('btc-updown-15m-'));self.assertEqual(int(seen[0].rsplit('-',1)[1])%900,0)
            self.assertEqual(m['endTs']-m['startTs'],900);self.assertEqual(m['interval'],'15m');self.assertEqual(m['lane'],'BTC15')
    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):mod.PolyServerDemo('BTC',30,interval=60)
    def test_calibration_cells_are_separate_per_round_length(self):
        self.assertNotEqual(edge.time_bucket(200,300),edge.time_bucket(200,900))
        c=Calibrator();tb=edge.time_bucket(500,900)
        for _ in range(50):c.update([{'tb':tb,'pb':5,'fairUp':.5,'pUp':.5}],True)
        self.assertGreater(c.estimate(.55,.5,500,900)['pUp'],.8)
        self.assertAlmostEqual(c.estimate(.55,.5,200,300)['pUp'],.5)
    def test_portfolio_lanes_retire_sol_xrp_and_fund_15m(self):
        import poly_portfolio
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d}):
            pf=poly_portfolio.PaperPortfolio()
            self.assertEqual(set(pf.engines),{'BTC','ETH','BTC15','ETH15'});self.assertEqual(sum(poly_portfolio.ALLOCATIONS.values()),200)
            self.assertEqual(pf.engines['ETH15'].interval,900);self.assertEqual(pf.engines['ETH15'].asset,'ETH')
            for e in pf.engines.values():self.assertGreaterEqual(e.inventory_stop,.30)   # stop leaves room for fees and spread
            self.assertIn('SOL',pf.ledger_history);self.assertIn('XRP',pf.ledger_history)
            for e in pf.engines.values():e.close()

class ResolutionLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_winner_requests_closed_markets(self):
        with tempfile.TemporaryDirectory() as d:
            e=mod.PolyServerDemo('BTC',60,state_file=Path(d)/'s.json');self.addCleanup(e.close);calls=[]
            async def get(url,params=None,headers=None):
                calls.append(dict(params))
                if params.get('closed')!='true':return []          # Gamma hides closed markets by default
                return [{'closed':True,'umaResolutionStatus':'resolved','outcomes':'["Up", "Down"]','outcomePrices':'["0", "1"]'}]
            e._get=get
            self.assertEqual(await e._resolved_winner('btc-updown-5m-1'),'down')
            self.assertEqual(calls[0]['closed'],'true')
    async def test_unresolved_market_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            e=mod.PolyServerDemo('BTC',60,state_file=Path(d)/'s.json');self.addCleanup(e.close)
            async def get(url,params=None,headers=None):
                return [{'closed':False,'umaResolutionStatus':None,'outcomes':'["Up","Down"]','outcomePrices':'["0.5","0.5"]'}]
            e._get=get;self.assertIsNone(await e._resolved_winner('x'))

class LedgerArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_env_adds_missing_sessions_without_overwriting(self):
        import poly_portfolio,base64,gzip,json
        archive={'ETH':{'old1':{'sessionId':'old1','realizedPnl':1.5,'trades':2,'events':[]},'keep':{'sessionId':'keep','realizedPnl':99}},'SOL':{'s':{'sessionId':'s','realizedPnl':-.5,'trades':1,'events':[]}}}
        enc=base64.b64encode(gzip.compress(json.dumps(archive).encode())).decode()
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d,'POLY_LEDGER_ARCHIVE_B64':enc}):
            pf=poly_portfolio.PaperPortfolio();pf.ledger_history['ETH']['keep']={'sessionId':'keep','realizedPnl':1.0}
            for e in pf.engines.values():
                async def start(e=e):e._new_session()
                e.start=start
            with patch.object(pf.edge,'start',lambda:None):await pf.start()
            self.assertEqual(pf.ledger_history['ETH']['old1']['realizedPnl'],1.5);self.assertEqual(pf.ledger_history['ETH']['keep']['realizedPnl'],1.0)
            expected=sum(float(x.get('realizedPnl') or 0) for h in pf.ledger_history.values() for x in h.values())
            self.assertAlmostEqual(pf.snapshot()['portfolio']['priorSessionRealizedPnl'],expected);self.assertIn('s',pf.ledger_history['SOL'])
            if pf.cycle_task:pf.cycle_task.cancel()
            for e in pf.engines.values():e.close()

class DashboardDataTests(unittest.TestCase):
    def test_samples_resolutions_and_flows(self):
        lab=EdgeLab({});now=time.time();e=FakeEngine(now)
        m={'rawFairUp':.6,'fairUp':.58}
        lab.evaluate('BTC',e,m,now);lab.evaluate('BTC',e,m,now+1)
        e.spot_ts=e.twap_ts=now+6;e.book_observed={'up':now+6,'down':now+6};lab.evaluate('BTC',e,m,now+6)
        self.assertEqual(len(lab.samples['BTC']),2);self.assertAlmostEqual(lab.samples['BTC'][0]['upMid'],.395)
        lab.settle('btc-updown-5m-1','up')
        r=lab.resolutions[-1];self.assertEqual(r['winner'],'up');self.assertEqual(r['lane'],'BTC');self.assertIsNotNone(r['lastPUp'])
        lab.on_trade({'netPnlUsd':.5,'closedAt':1,'strategy':'MOMENTUM_SCALP','asset':'ETH'});lab.on_trade({'netPnlUsd':-.2,'closedAt':2,'strategy':'MOMENTUM_SCALP','asset':'ETH'})
        f=lab.flows()[0];self.assertEqual((f['trades'],f['wins'],f['losses']),(2,1,1));self.assertAlmostEqual(f['pnl'],.3)
        snap=lab.snapshot();self.assertIn('samples',snap);self.assertIn('flows',snap);self.assertEqual(len(snap['resolutions']),1)

class MarketTrustTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.lab=EdgeLab({});self.m={'rawFairUp':.75,'fairUp':.70,'fairDown':.30}
    def fill(self,true_p):
        import random;rng=random.Random(7)
        for i in range(400):
            ask=.30;p=.55   # model always claims 25 points of edge
            self.lab.strategies['edge'].add({'p':p,'ask':ask,'cost':ask+edge.fee(ask),'won':rng.random()<true_p,'returnPerDollar':0,'roundStart':i})
    def test_overconfident_model_earns_no_trust(self):
        self.fill(.30);t=self.lab.trust()
        self.assertLess(t['conservativeTrust'],.1);self.assertLess(t['brierMarket'],t['brierModel'])
    def test_accurate_model_earns_trust_and_calibrated_bets(self):
        self.fill(.55);t=self.lab.trust();self.assertGreater(t['conservativeTrust'],.6)
        tb=edge.time_bucket(180);pb=edge.prob_bin(.75)
        for i in range(200):self.lab.cal.update([{'tb':tb,'pb':pb,'fairUp':.7,'pUp':.7}],i<150)
        c=self.lab.evaluate('BTC',FakeEngine(self.now),self.m,self.now)
        self.assertIn('edge_cal',self.lab.rounds['btc-updown-5m-1']['bets']);self.assertIn('calibratedKelly',c)
        self.assertLessEqual(c['calibratedP'],c['p']+1e-9)
    def test_no_trust_means_no_calibrated_bet(self):
        tb=edge.time_bucket(180);pb=edge.prob_bin(.75)
        for i in range(200):self.lab.cal.update([{'tb':tb,'pb':pb,'fairUp':.7,'pUp':.7}],i<150)
        self.lab.evaluate('BTC',FakeEngine(self.now),self.m,self.now)
        self.assertNotIn('edge_cal',self.lab.rounds['btc-updown-5m-1']['bets'])
    def test_calibrated_only_candidate_not_tradable_by_raw_variant(self):
        for i in range(60):self.lab.strategies['edge'].add(bet(.9 if i%2 else -.5,i))
        self.assertIsNone(self.lab.tradable({'side':'UP','calibratedOnly':True}))
