import unittest
from poly_edge import EdgeLab

class EdgeDisplayTests(unittest.TestCase):
    def _lab(self):
        lab=EdgeLab({})
        lab.trades.extend([{'asset':'BTC','strategy':'EDGE_HOLD','at':100.,'pnl':2.},{'asset':'BTC','strategy':'MOMENTUM_SCALP','at':200.,'pnl':-1.},
                           {'asset':'ETH','strategy':'MOMENTUM_SCALP','at':300.,'pnl':.5}])
        return lab
    def test_flows_since_account_reset(self):
        lab=self._lab()
        self.assertAlmostEqual(sum(f['pnl'] for f in lab.flows()),1.5)
        f=lab.flows(since=150.);self.assertEqual(sum(x['trades'] for x in f),2);self.assertAlmostEqual(sum(x['pnl'] for x in f),-.5)
        self.assertNotIn('EDGE_HOLD',{x['strategy'] for x in f})
    def test_matrix_samples_survive_restart(self):
        lab=self._lab();lab.samples['BTC'].extend({'t':float(i),'pUp':.5,'upMid':.5} for i in range(400))
        st=lab.export();self.assertEqual(len(st['samples']['BTC']),180)
        new=EdgeLab({});self.assertTrue(new.restore(st));self.assertEqual(len(new.samples['BTC']),180)
        self.assertEqual(len(new.trades),3)

class AutoPromoteTests(unittest.TestCase):
    def test_gate_pass_does_not_fund_by_default(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('POLY_EDGE_AUTO_PROMOTE',None)
            lab=EdgeLab({})
        for i in range(60):lab.strategies['edge'].add({'returnPerDollar':.5 if i%3 else -.2,'roundStart':float(i),'won':bool(i%3)})
        self.assertEqual(lab.gate_candidate(),'edge')          # the gate result is still reported...
        self.assertIsNone(lab.promoted())                      # ...but nothing is funded
        self.assertIsNone(lab.tradable({'side':'UP'}))
        self.assertFalse(lab.maker_promoted())
        snap=lab.snapshot(200.);self.assertEqual(snap['mode'],'SHADOW');self.assertEqual(snap['gateCandidate'],'edge')
