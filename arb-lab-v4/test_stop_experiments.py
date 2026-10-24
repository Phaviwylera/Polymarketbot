"""Stop experiments, identity stamps, latency intervals and stop forensics, on mocked books only."""
import copy,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
import poly_experiments as px
import poly_strategy as ps
import poly_forensics as pf

def book(bids,asks):
    return {'bids':[{'price':str(p),'size':str(q)} for p,q in bids],'asks':[{'price':str(p),'size':str(q)} for p,q in asks]}

class Lane(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=mod.PolyServerDemo('ETH',80,state_file=Path(self.tmp.name)/'state.json',risk_profile={'momentumEnabled':True,'sessionLossLimit':4,'stopLoss':.30})
        e=self.e;e._save=lambda:None;e._new_session();self.addCleanup(e.close);now=time.time()
        e.market={'slug':'eth-test','upToken':'u','downToken':'d','startTs':now-60,'endTs':now+240,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.open_trusted=True;e.spot_ts=e.twap_ts=now
        e._ingest_book('up',book([(.39,40),(.38,40)],[(.40,50)]));e._ingest_book('down',book([(.59,40)],[(.60,50)]))
        self.m={'fairUp':.6,'fairDown':.4,'leadBps':1.,'impulse5Bps':1.5}
        e.book_history.append({'t':now-3,'marketSlug':'eth-test','up':.375,'down':.615});e._model=lambda:self.m
        for p in (patch.object(mod,'EXECUTION_LATENCY_SEC',0),patch.dict(ps.EXPERIMENTS['A'],{'latencySec':0})):p.start();self.addCleanup(p.stop)
        self.closed=[];e.trade_observer=self.closed.append
    def candidate(self):
        s,r=self.e._momentum_signal(self.m);self.assertIsNotNone(s,r);return s
    def set_bids(self,levels):
        self.e._ingest_book('up',book(levels,[(.40,50)]))

class ShadowExperimentTests(Lane):
    async def test_entry_is_stamped_and_gate_b_recorded_without_changing_the_fill(self):
        cash=self.e.paper['cash'];self.assertTrue(await self.e._momentum_enter(self.candidate()))
        pos=self.e.paper['position'];ident=ps.identity()
        self.assertEqual((pos['strategyVersion'],pos['configHash']),(ident['strategyVersion'],ident['configHash']))
        self.assertIsNotNone(pos['signalToOrderMs']);self.assertGreaterEqual(pos['signalToOrderMs'],0)
        b=pos['experiments']['B'];self.assertEqual(b['id'],'B-depth-gate-v1');self.assertIn('wouldReject',b)
        self.assertAlmostEqual(cash-self.e.paper['cash'],pos['entryTotalCost'])   # paper accounting unchanged
    async def test_earlier_exit_is_shadow_only_and_compared_at_the_real_close(self):
        await self.e._momentum_enter(self.candidate());pos=self.e.paper['position'];stop=pos['stopLossUsd']
        # A loss between 0.6x and 1x the stop: the shadow policy exits, the real position stays open.
        self.set_bids([(.38,40),(.37,40)]);self.m['leadBps']=-.5
        await self.e._momentum_manage(self.m);await self.e._momentum_manage(self.m)
        self.assertIsNotNone(self.e.paper['position'],'shadow experiment must not close the real position')
        a=self.e.paper['position']['experiments']['A'];self.assertEqual(a['state'],'filled')
        # The book keeps falling; the real stop fires and fills lower.
        self.set_bids([(.31,40),(.30,40)]);await self.e._momentum_manage(self.m)
        self.assertIsNone(self.e.paper['position']);t=self.closed[-1]
        ex=t['experiments']['A'];self.assertTrue(ex['exitedEarlier'])
        self.assertAlmostEqual(ex['realNetPnlUsd'],t['netPnlUsd'],places=4)
        self.assertGreater(ex['shadowNetPnlUsd'],ex['realNetPnlUsd'])
        self.assertEqual(t['exitTriggerBid'],.31);self.assertIsNotNone(t['exitTriggerToFillMs']);self.assertEqual(t['configHash'],ps.identity()['configHash'])
        p=self.e.paper;self.assertAlmostEqual(p['startingBalance']+p['realizedPnl'],p['cash'],places=6)   # ledger invariant
        self.assertLess(t['netPnlUsd'],-stop)
    async def test_missed_recovery_is_counted(self):
        await self.e._momentum_enter(self.candidate())
        self.set_bids([(.38,40),(.37,40)]);await self.e._momentum_manage(self.m);await self.e._momentum_manage(self.m)
        self.set_bids([(.50,100)]);await self.e._momentum_manage(self.m)   # real recovers to the profit target
        ex=self.closed[-1]['experiments']['A'];self.assertTrue(ex['missedRecovery']);self.assertGreater(ex['realNetPnlUsd'],0)

class PureFunctionTests(unittest.TestCase):
    def test_threshold_price_hits_the_stop_exactly(self):
        cost=5*(.4+px.fee(.4));thr=px.threshold_price(5,cost,.3)
        self.assertAlmostEqual(px.net_at(5,thr,cost),-.3,places=5)
    def test_gate_b_rejects_thin_or_risky_books(self):
        cfg=ps.EXPERIMENTS['B'];cost=5*(.4+px.fee(.4))
        self.assertTrue(px.gate_b([{'price':.39,'size':6}],5,cost,.3,.01,cfg)['wouldReject'])          # depth < 2x
        self.assertFalse(px.gate_b([{'price':.39,'size':50},{'price':.38,'size':50}],5,cost,.6,.01,cfg)['wouldReject'])
        r=px.gate_b([{'price':.39,'size':50}],5,cost,.1,.01,cfg);self.assertTrue(r['wouldReject']);self.assertIn('stressed',r['reason'])
    def test_identity_hash_changes_with_any_parameter(self):
        import poly_momentum as pm
        h=ps.identity()['configHash']
        with patch.object(pm,'MIN_EDGE',.13),patch.object(ps,'_cache',None):self.assertNotEqual(ps.identity()['configHash'],h)
        self.assertEqual(ps.identity()['configHash'],h)

class ForensicsTests(unittest.TestCase):
    def session(self):
        # newest first, as the engine stores them
        return {'lane':'ETH','paper':{'events':[
            {'t':110.9,'action':'SELL','side':'UP','price':.33,'pnl':-.4947,'detail':'momentum net loss stop; VWAP 0.330'},
            {'t':110.4,'action':'ORDER','side':'UP','price':.34,'detail':'paper FAK sell (depth walk); momentum net loss stop'},
            {'t':110.3,'action':'NOFILL','side':'UP','detail':'exit depth moved during latency'},
            {'t':109.9,'action':'ORDER','side':'UP','price':.35,'detail':'paper FAK sell (depth walk); momentum net loss stop'},
            {'t':100.0,'action':'BUY','side':'UP','price':.40,'detail':'MOMENTUM_SCALP; 5.00 shares; confirmed depth'}]}}
    def test_stop_is_reconstructed_from_the_event_chain(self):
        rows=pf.reconstruct_stops([self.session()],[{'asset':'ETH','entryAt':100.,'stopLossUsd':.30,'entryFeatures':{'bookMove':.02}}])
        r=rows[0];self.assertEqual((r['triggerBid'],r['exitPrice'],r['retries']),(.35,.33,1))
        self.assertAlmostEqual(r['decisionToFillSec'],1.0);self.assertAlmostEqual(r['lossAfterTriggerUsd'],.10,places=6)
        self.assertGreater(r['overshootUsd'],.05);self.assertTrue(any('retr' in c for c in r['causes']))
        s=pf.stop_summary(rows);self.assertEqual(s['stops'],1);self.assertEqual(s['overshootsAbove5c'],1)
    def test_reconstruction_is_deterministic(self):
        a=pf.reconstruct_stops([self.session()],[]);b=pf.reconstruct_stops([copy.deepcopy(self.session())],[])
        self.assertEqual(a,b)
    def test_experiment_report_compares_on_the_same_trades(self):
        rows=[{'strategy':'MOMENTUM_SCALP','entryAt':i*300.,'netPnlUsd':-.6 if i%2 else .4,'exitReason':'momentum net loss stop' if i%2 else 'target',
               'experiments':{'A':{'id':'A','realNetPnlUsd':-.6 if i%2 else .4,'shadowNetPnlUsd':-.35 if i%2 else .4,'exitedEarlier':bool(i%2),'missedRecovery':False},
                              'B':{'id':'B','wouldReject':bool(i%2),'realNetPnlUsd':-.6 if i%2 else .4}}} for i in range(10)]
        r=pf.experiments_report(rows)
        self.assertAlmostEqual(r['A']['meanDifferencePerTrade'],.125);self.assertEqual(r['A']['exitedEarlier'],5)
        self.assertEqual(r['B']['rejectRatePct'],50.);self.assertEqual(r['B']['wouldReject']['netPnlUsd'],-3.)
    def test_alerts_flag_operational_problems(self):
        now=1000.
        st={'lanes':[{'lane':'BTC','feed':{'clob':'error:Timeout'},'bookAgeSec':30,'clobReconnectsLastHour':9,
                      'position':{'mode':'MOMENTUM_SCALP','entryAt':now-200,'exitRequestedAt':now-20}}],
            'storage':{'configured':True,'lastError':'TimeoutError','checkpointAgeSec':45,'databaseExpiresAt':'1970-01-10T00:00:00Z'}}
        a=' | '.join(pf.alerts(st,{'sessionsWithProblems':1},{'clean':False,'trades':3,'marketRisk':{'heldToSettlement':1}},now))
        for frag in ('clob feed','book 30s','9 order-book reconnects','scalp open 200s','exit pending 20s','write error','checkpoint 45s',
                     'expires','no verified backup','ledger','fill audit','held to settlement'):
            self.assertIn(frag,a)

if __name__=='__main__':unittest.main()
