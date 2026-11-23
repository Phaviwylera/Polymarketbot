import os,unittest
from unittest import mock
import poly_lanes
class RetiredLaneTests(unittest.TestCase):
    def test_retired_lanes_do_not_start_even_if_env_says_on(self):
        import poly_maker,poly_fastmaker,poly_xmarket,poly_pairacc,poly_eventmaker,poly_spotlead
        env={'POLY_LANES_TESTING':'0','POLY_MAKER':'1','POLY_FASTMAKER':'1','POLY_XMARKET':'1','POLY_PAIRACC':'1','POLY_EVENTMAKER':'1','POLY_SPOTLEAD':'1'}
        with mock.patch.dict(os.environ,env):
            for cls in (poly_maker.Maker,poly_fastmaker.FastMaker,poly_xmarket.XMarket,poly_pairacc.PairAcc,poly_eventmaker.EventMaker):
                self.assertFalse(cls().enabled,cls.__name__)
            self.assertTrue(poly_spotlead.SpotLead().enabled)        # S2 is not retired
    def test_summary_names_reasons(self):
        s=poly_lanes.summary();self.assertIn('pairacc',s['lanes']);self.assertTrue(all(v['why'] for v in s['lanes'].values()))
        self.assertNotIn('spotlead',s['lanes']);self.assertNotIn('kalshi',s['lanes'])
