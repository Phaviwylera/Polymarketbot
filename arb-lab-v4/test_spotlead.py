import tempfile,unittest,math
import poly_spotlead as sl
def row(**k):
    r={'lane':'BTC','slug':'btc-updown-5m-1','token':'UP','t':sl.REGISTERED_AT+10,'price':.86,'bid':.85,'ask':.86,'spotVsOpenBps':15.,'secondsLeft':200.,'roundEnd':sl.REGISTERED_AT+300}
    r.update(k);return r
class SpotLeadTests(unittest.TestCase):
    def test_prob_uses_spot_on_the_tokens_side(self):
        up=sl.prob('UP',.5,.52,10.,100.);dn=sl.prob('DOWN',.5,.52,10.,100.)
        self.assertGreater(up,sl.prob('UP',.5,.52,0.,100.));self.assertLess(dn,sl.prob('DOWN',.5,.52,0.,100.))
    def test_signal_filters(self):
        self.assertIsNotNone(sl.signal(row()))                                   # spot well on UP's side at 86c
        self.assertIsNone(sl.signal(row(spotVsOpenBps=-15.)))                     # spot against: no edge
        self.assertIsNone(sl.signal(row(lane='BTC15')))                          # 15m excluded
        self.assertIsNone(sl.signal(row(bid=.80,ask=.86)))                       # spread > 5c
        self.assertIsNone(sl.signal(row(secondsLeft=1.)))
        self.assertIsNone(sl.signal(row(spotVsOpenBps=None)))
    def test_first_signal_per_round_then_settle(self):
        s=sl.SpotLead(root=tempfile.mkdtemp())
        s.on_print(row());s.on_print(row(t=sl.REGISTERED_AT+20,ask=.87,bid=.86))
        self.assertEqual(len(s.open),1);o=list(s.open.values())[0];self.assertAlmostEqual(o['costUsd'],5.,2)
        self.assertEqual(s.settle('btc-updown-5m-1','up'),1)
        d=s.done[-1];c=.86+sl.fee(.86);self.assertAlmostEqual(d['pnlUsd'],o['shares']*(1-c),3);self.assertLess(d['retStressed'],d['ret'])
        r=s.report();self.assertEqual(r['S2']['stressed']['n'],1);self.assertEqual(r['wallet']['trades'],1);self.assertGreater(r['wallet']['equity'],200)
    def test_floor_stops_new_stakes_and_state_persists(self):
        d=tempfile.mkdtemp();s=sl.SpotLead(root=d)
        s.done.append({'t':sl.REGISTERED_AT+1,'lane':'ETH','shares':100.,'pnlUsd':-80.,'ret':-1.,'retStressed':-1.})
        s.on_print(row());self.assertEqual(list(s.open.values())[0]['shares'],0.)   # signal recorded, no stake below floor
        self.assertIn('hard floor',s.wallet()['guard'])
        s.save();q=sl.SpotLead(root=d);q.load();self.assertEqual(len(q.open),1);self.assertEqual(len(q.done),1)
        q.on_print(row());self.assertEqual(len(q.open),1)                       # same round+token not taken twice after restart
