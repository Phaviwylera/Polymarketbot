import unittest
import poly_forensics as pf,poly_strategy as ps

def t(at,edge,pnl,lane='BTC',price=.4):
    return {'strategy':'MOMENTUM_SCALP','entryAt':at,'netPnlUsd':pnl,'entryFeatures':{'modelEdgeEstimate':edge},'lane':lane,'entryPrice':price,
            'holdSec':8,'exitReason':'momentum net profit target','experiments':{}}

class ExperimentCTests(unittest.TestCase):
    def test_scores_forward_trades_only(self):
        reg=ps.EXPERIMENTS['C']['registeredAt']
        rows=[t(reg-100,.15,-1.),t(reg-50,.25,+1.),         # the in-sample trades that suggested the gate: excluded
              t(reg+10,.15,-.5),t(reg+20,.25,+.4),t(reg+30,.30,-.2)]
        c=pf.experiments_report(rows)['C']
        self.assertEqual(c['forwardTrades'],3)
        self.assertEqual(c['wouldReject']['trades'],1);self.assertEqual(c['experimentAccepted']['trades'],2)
        self.assertAlmostEqual(c['experimentAccepted']['netPnlUsd'],.2,6)
    def test_config_hash_is_v179(self):
        saved=ps._cache;ps._cache=None
        try:self.assertEqual(ps.identity()['configHash'],'de066565818b')   # v17.9 (rule A); fresh: earlier tests may have cached it under a patch
        finally:ps._cache=saved
    def test_bands_by_lane_and_price(self):
        b=pf.bands([t(1,.2,1.,'ETH',.35),t(2,.2,-1.,'BTC',.42)])
        self.assertIn('ETH',b['byLane']);self.assertIn('0.3-0.4',b['byEntryPrice']);self.assertIn('0.4-0.45',b['byEntryPrice'])
