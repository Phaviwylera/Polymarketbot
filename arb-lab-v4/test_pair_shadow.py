import copy,io,json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from poly_pair_shadow import PairShadow,POLICY
from poly_features import fee
from poly_shadow import ResearchHub
from poly_portfolio import PaperPortfolio
from poly_storage import DurableStore
from test_v16_research import context,NOW

class PairTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.p=PairShadow('BTC',60,lambda k,r:self.events.append((k,r)));self.now=NOW
    def c(self,t,up=(.4,.42),down=(.54,.55)):
        c=context(t);c['market']['slug']='btc-pair'
        for s,(bid,ask) in zip(('up','down'),(up,down)):
            c['bbo'][s]={'bid':bid,'ask':ask};c['depth'][s]={'bids':[{'price':bid,'size':10}],'asks':[{'price':ask,'size':100}]}
        return c
    def seed(self):
        self.p.step(self.c(NOW),NOW);self.assertEqual(self.p.active['phase'],'quote')
        self.p.step(self.c(NOW+.4),NOW+.4);self.assertEqual(self.p.active['activatedAt'],NOW+.4)
    def print(self,t,seq=1,size=15,**kw):
        return {'seq':seq,'t':t,'receivedAt':t,'marketSlug':'btc-pair','side':'up','aggressor':'SELL','price':.4,'size':size,'feedReady':True,**kw}
    def fill(self):
        self.seed();c=self.c(NOW+.8);c['tradePrints']=[self.print(NOW+.8)];self.p.step(c,NOW+.8);self.assertEqual(self.p.active['phase'],'hedge')
    def test_queue_and_activation_prevent_touch_or_historical_fills(self):
        self.seed();c=self.c(NOW+.8);c['tradePrints']=[self.print(NOW+.1)];self.p.step(c,NOW+.8)
        self.assertEqual(self.p.active['consumed'],0)
        c=self.c(NOW+1.2);c['tradePrints']=[self.print(NOW+1.2,seq=2,size=14)];self.p.step(c,NOW+1.2)
        self.assertEqual(self.p.active['phase'],'quote')
        c=self.c(NOW+1.6);c['tradePrints']=[self.print(NOW+1.2,seq=2,size=14),self.print(NOW+1.6,seq=3,size=1)];self.p.step(c,NOW+1.6)
        self.assertEqual(self.p.active['phase'],'hedge');self.assertEqual(self.p.cash,58)
    def test_buy_aggressor_other_market_and_unready_prints_do_not_fill(self):
        self.seed();c=self.c(NOW+.8);c['tradePrints']=[self.print(NOW+.8,seq=1,aggressor='BUY'),self.print(NOW+.8,seq=2,marketSlug='old'),self.print(NOW+.8,seq=3,feedReady=False)]
        self.p.step(c,NOW+.8);self.assertEqual(self.p.active['consumed'],0)
    def test_pair_cash_fees_and_distinct_second_leg_latency(self):
        self.fill();self.p.step(self.c(NOW+1),NOW+1);self.assertIsNotNone(self.p.active)
        self.p.step(self.c(NOW+1.2),NOW+1.2);self.assertIsNone(self.p.active)
        expected=5*(1-.4-.55-fee(.55));self.assertAlmostEqual(self.p.realized,expected)
        self.assertAlmostEqual(self.p.cash,60+expected);self.assertAlmostEqual(self.p.fees,5*fee(.55))
        self.assertAlmostEqual(self.p.observed,self.p.realized);self.assertEqual(self.p.censored,0)
        self.p.step(self.c(NOW+5),NOW+5);self.assertIsNone(self.p.active)
    def test_changed_hedge_price_after_fill_is_exposure_not_free_cancel(self):
        self.seed();c=self.c(NOW+.8,down=(.64,.65));c['tradePrints']=[self.print(NOW+.8)]
        self.p.step(c,NOW+.8);self.assertEqual(self.p.active['phase'],'hedge');self.assertEqual(self.p.cash,58)
        self.p.step(self.c(NOW+1.2,down=(.64,.65)),NOW+1.2);self.assertEqual(self.p.active['phase'],'hedge')
    def test_entry_requires_fresh_consistent_full_depth_and_cost_budget(self):
        for change in ('stale','depth','expensive','notional'):
            p=PairShadow('BTC',60,lambda *a:None);c=self.c(NOW)
            if change=='stale':c['bookEventLagSec']['down']=4
            if change=='depth':
                for side in ('up','down'):c['depth'][side]['asks'][0]['size']=1
            if change=='expensive':c=self.c(NOW,down=(.64,.65))
            if change=='notional':c['market']['minOrderSize']=100
            p.step(c,NOW);self.assertIsNone(p.active,change)
    def test_gap_with_filled_inventory_is_separate_loss_bound(self):
        self.fill();self.p.step(self.c(NOW+5),NOW+5)
        self.assertEqual(self.p.censored,-2);self.assertEqual(self.p.observed,0);self.assertEqual(self.p.cash,58)
        self.assertTrue(self.p.snapshot()['recent'][0]['censored'])
    def test_gap_before_entry_never_debits_cash(self):
        self.seed();self.p.step(self.c(NOW+5),NOW+5);self.assertEqual(self.p.cash,60);self.assertEqual(self.p.realized,0)
    def test_stop_unwinds_after_latency_with_net_fee_accounting(self):
        self.fill();self.p.step(self.c(NOW+1.2,up=(.3,.32),down=(.7,.72)),NOW+1.2)
        self.assertEqual(self.p.active['phase'],'exit')
        self.p.step(self.c(NOW+1.4,up=(.3,.32),down=(.7,.72)),NOW+1.4);self.assertIsNotNone(self.p.active)
        self.p.step(self.c(NOW+1.6,up=(.3,.32),down=(.7,.72)),NOW+1.6)
        self.assertAlmostEqual(self.p.realized,5*(.3-fee(.3)) -2);self.assertEqual(self.p.counts['unwind'],1)
    def test_restart_preserves_budget_and_records_unobservable_inventory_once(self):
        self.fill();state=self.p.export_state();other=PairShadow('BTC',60,lambda *a:None);other.restore(state,NOW+2)
        self.assertEqual(other.cash,58);self.assertEqual(other.censored,-2);self.assertIsNone(other.active)
        again=PairShadow('BTC',60,lambda *a:None);again.restore(other.export_state(),NOW+3)
        self.assertEqual(again.censored,-2);self.assertEqual(again.counts['completed'],1)
    def test_loss_gate_blocks_new_round_and_allocation_does_not_expand(self):
        self.p.realized=-2;self.p.step(self.c(NOW),NOW);self.assertIsNone(self.p.active)
        self.assertEqual(self.p.reason,'shadow loss budget reached')
        self.assertLessEqual(PairShadow('SOL',30,lambda *a:None).residual_cap,1.5)

class PairIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hub_pair_activity_never_changes_existing_paper_or_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=PaperPortfolio()
            for e in p.engines.values():e._save=lambda:None;e._new_session()
            hub=ResearchHub(p.engines,str(Path(tmp)/'research.db'));p.research=hub
            c=PairTests().c(NOW);before={a:copy.deepcopy(e.paper) for a,e in p.engines.items()};config=p.get_config()
            try:
                with patch.object(hub,'context',lambda e:copy.deepcopy(c)):
                    hub.step(NOW);hub.step(NOW+.4)
                self.assertEqual(p.get_config(),config)
                for a,e in p.engines.items():self.assertEqual(e.paper,before[a])
                self.assertEqual(hub.pair_snapshot()['mode'],'SHADOW_ONLY')
                self.assertTrue(all(v['phase']=='quote' for v in hub.pair_snapshot()['assets'].values() if v['asset'] in ('BTC','ETH')))
                import poly_audit_export as audit
                path=audit._build_archive(json.dumps(p.snapshot(include_assets=True)),audit.capture_journals(p))
                try:
                    with zipfile.ZipFile(path) as z:
                        self.assertIn('pair-shadow.json',z.namelist());self.assertIn('research-quality.json',z.namelist())
                        self.assertFalse(json.loads(z.read('pair-shadow.json'))['executionEnabled'])
                        self.assertIn(b'PAIR_QUOTE',z.read('RESEARCH.ndjson'))
                finally:Path(path).unlink()
            finally:await p.shutdown()
    async def test_pair_events_checkpoint_and_retry_share_transaction(self):
        class Tx:
            async def __aenter__(self):pass
            async def __aexit__(self,*a):return False
        research=SimpleNamespace(pair_checkpoint=lambda:{'BTC':{'cash':58}},artifact=None)
        eng=SimpleNamespace(engines={},research=research,snapshot=lambda **kw:{'assets':{}})
        store=DurableStore(eng,url='postgresql://unused');store.owner=True
        store.conn=SimpleNamespace(transaction=lambda:Tx(),executemany=AsyncMock(),execute=AsyncMock())
        store.enqueue({'bootId':'test','journalSeq':1,'t':NOW,'kind':'PAIR_OUTCOME','asset':'RESEARCH','row':{'asset':'BTC'}})
        await store.flush();self.assertEqual(store.saved,1)
        checkpoint=json.loads(store.conn.execute.call_args_list[0].args[2]);self.assertEqual(checkpoint['pairResearchCheckpoint']['BTC']['cash'],58)

if __name__=='__main__':unittest.main()
