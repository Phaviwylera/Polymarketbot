import tempfile,unittest
import poly_pairacc as pa
END=pa.REGISTERED_AT+1000;MK={'slug':'btc-updown-5m-7','endTs':END}
def bbo(ua,da,us=100,ds=100):return {'up':{'ask':ua,'askSize':us},'down':{'ask':da,'askSize':ds}}
class PairAccTests(unittest.TestCase):
    def test_accumulates_pairs_under_target_and_settles(self):
        p=pa.PairAcc(root=tempfile.mkdtemp());t0=END-290                    # ~3% into the window
        p.on_book('BTC','up',bbo(.48,.53),MK,t0)                               # first leg: UP (cheaper, <= .50)
        b=p.cur['BTC']['v']['replica'];self.assertEqual(b.inv['up'][0],5.)
        p.on_book('BTC','down',bbo(.50,.51),MK,t0+1)                           # DOWN .51 + .48 > .97: no
        self.assertEqual(b.inv['down'][0],0.)
        p.on_book('BTC','down',bbo(.53,.49),MK,t0+2)                           # .49 + .48 = .97: take DOWN
        self.assertEqual(b.inv['down'][0],5.);self.assertEqual(p.cur['BTC']['v']['strict'].inv['down'][0],0.)
        p.on_book('BTC','up',bbo(.48,.49),MK,t0+2.1)                           # within latency for UP? UP last at t0: ok -> pair .48+.49
        self.assertEqual(b.inv['up'][0],10.)
        self.assertEqual(p.settle('btc-updown-5m-7','down'),1)
        x=p.done[-1]['v']['replica'];cost=5*.48+5*.49+5*.48
        self.assertAlmostEqual(x['pnlUsd'],5-cost-(10*pa.fee(.48)+5*pa.fee(.49)),4)
        r=p.report();self.assertEqual(r['R1']['forward']['rounds'],1)
    def test_limits_imbalance_size_and_window(self):
        p=pa.PairAcc(root=tempfile.mkdtemp())
        p.on_book('BTC','up',bbo(.48,.53,us=2),MK,END-290);self.assertEqual(p.cur['BTC']['v']['replica'].inv['up'][0],2.)
        p2=pa.PairAcc(root=tempfile.mkdtemp());p2.on_book('BTC','up',bbo(.40,.40),MK,END-5)    # after 95%: nothing
        self.assertEqual(p2.cur['BTC']['v']['replica'].fills,0)
        p3=pa.PairAcc(root=tempfile.mkdtemp());p3.on_book('BTC15','up',bbo(.40,.40),{'slug':'btc-updown-15m-1','endTs':END},END-800)
        self.assertEqual(p3.cur,{})                                            # 15m lanes not traded (not observed)
        b=pa.Book();b.inv={'up':[30.,14.],'down':[10.,4.8]}
        self.assertEqual(pa.PairAcc.decide(b,bbo(.45,.45),.97)[0],'down')     # UP is 20 ahead: only DOWN
