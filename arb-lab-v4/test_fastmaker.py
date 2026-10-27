import tempfile,unittest
import poly_fastmaker as fm
END=fm.REGISTERED_AT+1000
MK={'slug':'btc-updown-5m-9','endTs':END}
def bbo(ub,db,us=50,ds=50):return {'up':{'bid':ub,'ask':round(ub+.02,2),'bidSize':us},'down':{'bid':db,'ask':round(db+.02,2),'bidSize':ds}}
def pr(t,side,price,size,agg='SELL'):return {'receivedAt':t,'side':side,'price':price,'size':size,'aggressor':agg}
class FastMakerTests(unittest.TestCase):
    def setUp(self):self.m=fm.FastMaker(root=tempfile.mkdtemp())
    def test_latency_queue_pair_lock(self):
        m=self.m;t=END-280                                             # 93% left: quoting
        m.on_book('BTC','up',bbo(.45,.52),MK,t);r=m.cur['BTC']
        self.assertIsNone(r.q['up']);self.assertEqual(r.pending['up'][0],'post')   # not live until latency passes
        m.on_print('BTC',pr(t+.1,'up',.44,10),MK);self.assertEqual(r.inv['up'][0],0)  # before the quote is live
        m.on_print('BTC',pr(t+.3,'up',.45,60),MK)                      # live now: 50 ahead, 10 fill... capped at 5
        self.assertEqual(r.inv['up'][0],5.)
        # after the UP fill, DOWN is bid at min(best bid .52, 1-.01-.45=.54) -> .52
        m.on_book('BTC','down',bbo(.44,.52),MK,t+1);m.on_book('BTC','down',bbo(.44,.52),MK,t+1.3)
        self.assertEqual(r.q['down']['price'],.52)
        m.on_print('BTC',pr(t+2,'down',.51,9),MK);self.assertEqual(r.inv['down'][0],5.)   # trade-through
        m.on_book('BTC','up',bbo(.40,.55),MK,END-20)                  # under 15% left: round closed for quoting
        self.assertEqual(m.settle('btc-updown-5m-9','down'),1)
        x=m.done[-1];self.assertAlmostEqual(x['pnlUsd'],5-5*.45-5*.52,4);self.assertEqual(x['flattened'],0)
        rep=m.report();self.assertEqual(rep['M2']['rounds'],1);self.assertEqual(rep['pairsCompleted'],1)
    def test_no_quotes_when_pair_too_expensive(self):
        m=self.m;m.on_book('BTC','up',bbo(.49,.51),MK,END-280);self.assertIsNone(m.cur['BTC'].pending['up'])
    def test_stop_loss_sells_only_the_unpaired_excess(self):
        m=self.m;t=END-280;m.on_book('BTC','up',bbo(.45,.52,0,0),MK,t)
        m.on_print('BTC',pr(t+.3,'up',.44,5),MK)                      # UP filled 5 @ .45
        m.on_book('BTC','up',bbo(.34,.62,0,0),MK,t+5)                  # UP bid fell 11c: stop
        r=m.cur['BTC'];self.assertEqual(r.inv['up'][0],0);self.assertEqual(r.flattened,1)
        m.on_book('BTC','up',bbo(.30,.66,0,0),MK,t+6);self.assertIsNone(r.pending['down'])      # no new risk after a stop
        m.on_book('BTC','up',bbo(.30,.66),MK,END-10);m.settle('btc-updown-5m-9','down')
        x=m.done[-1];self.assertAlmostEqual(x['pnlUsd'],5*(.34-fm.fee(.34))-5*.45,4)
    def test_new_market_rolls_the_round(self):
        m=self.m;m.on_book('BTC','up',bbo(.45,.52),MK,END-280)
        m.on_book('BTC','up',bbo(.45,.52),{'slug':'btc-updown-5m-10','endTs':END+300},END+20)
        self.assertEqual(m.cur['BTC'].slug,'btc-updown-5m-10');self.assertEqual(m.done[-1]['fills'],0)
