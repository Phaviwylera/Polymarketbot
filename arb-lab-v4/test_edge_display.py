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
