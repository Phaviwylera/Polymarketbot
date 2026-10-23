import asyncio,time,unittest
from types import SimpleNamespace
import poly_scout as sc

def eng(asset,tag,start,bid,ask,now):
    return SimpleNamespace(asset=asset,tag=tag,market={'startTs':start},bbo={'up':{'bid':bid,'ask':ask,'bidSize':50,'askSize':50}},book_observed={'up':now})

class ScoutTests(unittest.TestCase):
    def fill(self,tr,rounds,jump,spread=.01):
        t=1000.
        for r in range(rounds):
            start=r*300.
            for i in range(12):
                mid=.5+(jump if i%4>=2 else 0)
                tr.add(start+20+i*20,start,mid-spread/2,mid+spread/2,40,40)
    def test_metrics_measure_friction_and_opportunity(self):
        tr=sc.MarketTrack('sol',300,'5m');self.fill(tr,3,.12)
        m=tr.metrics();self.assertEqual(m['rounds'],3)
        self.assertAlmostEqual(m['frictionPerShare'],2*sc.fee(.5)+.01,places=2)
        self.assertGreater(m['opportunityRate'],0);self.assertLessEqual(m['opportunityRate'],1)
        calm=sc.MarketTrack('doge',300,'5m');self.fill(calm,3,.0)
        self.assertEqual(calm.metrics()['opportunityRate'],0)
    def test_verdicts_compare_with_traded_lanes(self):
        s=sc.AssetScout({'BTC':eng('BTC','5m',0,.49,.5,time.time()),'ETH':eng('ETH','5m',0,.49,.5,time.time())},ledger=lambda:{'btc-5m':{'trades':3,'netPnlUsd':1.2}})
        for k in ('btc-5m','eth-5m'):self.fill(s.tracks[k],8,.12)
        self.fill(s.tracks['sol-5m'],8,.12)            # as lively as the benchmark
        self.fill(s.tracks['xrp-5m'],8,.0)             # never moves enough
        self.fill(s.tracks['doge-5m'],8,.12,spread=.05)
        self.fill(s.tracks['bnb-5m'],2,.12)            # too few rounds
        s.tracks['hype-5m'].listed=False
        snap=s.snapshot();m=snap['markets']
        self.assertEqual(m['btc-5m']['verdict'],'ACTIVE');self.assertEqual(m['btc-5m']['netSinceFreshStart'],1.2)
        self.assertEqual(m['sol-5m']['verdict'],'CANDIDATE');self.assertEqual(m['xrp-5m']['verdict'],'WEAK')
        self.assertEqual(m['doge-5m']['verdict'],'TOO_WIDE');self.assertEqual(m['bnb-5m']['verdict'],'OBSERVING')
        self.assertEqual(m['hype-5m']['verdict'],'NOT_LISTED');self.assertEqual(snap['candidates'],['sol-5m'])
        self.assertEqual(snap['mode'],'READ_ONLY');self.assertIn('sol-5m',s.compact()['m'])
    def test_network_failures_never_escape(self):
        s=sc.AssetScout({})
        async def boom(*a,**k):raise RuntimeError('down')
        s._get=boom;asyncio.run(s.step(now=1000.))
        self.assertGreater(s.errors,0);self.assertEqual(s.snapshot()['candidates'],[])
    def test_scout_has_no_order_path(self):
        src=open(sc.__file__).read()
        for word in ('_enter','_try_sell','post_order','create_order','paper['):self.assertNotIn(word,src)

class PortfolioWiringTests(unittest.TestCase):
    def test_portfolio_exposes_scout_without_affecting_snapshot(self):
        import os,tempfile
        from unittest.mock import patch
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        with patch.dict(os.environ,{'POLY_DATA_DIR':d.name}):
            import poly_portfolio;p=poly_portfolio.PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        snap=p.snapshot();self.assertEqual(snap['scout']['mode'],'READ_ONLY');self.assertIn('portfolio',snap)
        p.scout.snapshot=lambda:(_ for _ in ()).throw(ValueError());p._scout_cache=None
        self.assertIn('error',p.scout_snapshot())   # a scout bug cannot break the snapshot

if __name__=='__main__':unittest.main()
