import os,tempfile,unittest
import poly_maker as pm
E=pm.REGISTERED_AT+1000
def pr(t,tok,price,size,aggr,lf,bid=.45,bs=100,ask=.47,obid=.52,obs=50,oask=.54,slug='btc-updown-5m-1'):
    return {'t':t,'lane':'BTC','slug':slug,'roundEnd':E,'token':tok,'price':price,'size':size,'aggressor':aggr,'leftFrac':lf,
            'bid':bid,'bidSize':bs,'ask':ask,'otherBid':obid,'otherBidSize':obs,'otherAsk':oask}
class MakerTests(unittest.TestCase):
    def setUp(self):self.m=pm.Maker(root=tempfile.mkdtemp());self.m.enabled=True
    def test_no_quote_before_90pct_left(self):
        self.m.on_print(pr(E-290,'UP',.46,5,'BUY',.97));self.assertEqual(self.m.q,{})
    def test_queue_then_fill_and_settle(self):
        m=self.m;m.on_print(pr(E-260,'UP',.46,5,'BUY',.87))            # posts join @ .45 (100 ahead), improve @ .46, DOWN join @ .52
        q=m.q['btc-updown-5m-1']['v']
        self.assertEqual(q['join']['UP']['price'],.45);self.assertEqual(q['join']['UP']['ahead'],100);self.assertEqual(q['improve']['UP']['price'],.46)
        m.on_print(pr(E-259.9,'UP',.45,80,'SELL',.86))                  # within latency: ignored
        self.assertEqual(q['join']['UP']['ahead'],100)
        m.on_print(pr(E-250,'UP',.45,80,'SELL',.83))                    # eats 80 of the 100 ahead
        self.assertEqual(q['join']['UP']['filled'],0);self.assertAlmostEqual(q['join']['UP']['ahead'],20)
        m.on_print(pr(E-240,'UP',.45,25,'SELL',.8))                     # 20 ahead, 5 fill us
        self.assertAlmostEqual(q['join']['UP']['filled'],5)
        m.on_print(pr(E-200,'UP',.44,3,'SELL',.66))                     # trade-through: rest fills
        self.assertEqual(q['join']['UP']['state'],'filled');self.assertAlmostEqual(q['join']['UP']['filled'],5/.45,3)
        self.assertEqual(q['improve']['UP']['state'],'filled')          # .44 < .46 as well
        m.on_print(pr(E-20,'DOWN',.40,10,'SELL',.07))                   # after cancel time: no fill
        self.assertEqual(q['join']['DOWN']['state'],'cancelled')
        self.assertEqual(m.settle('btc-updown-5m-1','up'),1)
        j=m.done[-1]['v']['join'];self.assertEqual(j['fills'],1);self.assertAlmostEqual(j['pnlUsd'],5/.45-5,3);self.assertFalse(j['both'])
        r=m.report();self.assertEqual(r['M1']['forward']['roundsFilled'],1);self.assertEqual(r['M1']['verdict'],'collecting')
        m.save();m2=pm.Maker(root=m.root);self.assertTrue(m2.load());self.assertEqual(len(m2.done),1)
    def test_buy_prints_and_higher_prices_do_not_fill(self):
        m=self.m;m.on_print(pr(E-260,'UP',.46,5,'BUY',.87))
        m.on_print(pr(E-250,'UP',.45,500,'BUY',.83));m.on_print(pr(E-240,'UP',.47,500,'SELL',.8))
        self.assertEqual(m.q['btc-updown-5m-1']['v']['join']['UP']['filled'],0)
    def test_both_sides_filled_captures_spread(self):
        m=self.m;m.on_print(pr(E-260,'UP',.46,5,'BUY',.87,bid=.45,obid=.52))
        m.on_print(pr(E-250,'UP',.40,5,'SELL',.83));m.on_print(pr(E-240,'DOWN',.50,5,'SELL',.8,bid=.50,obid=.48))
        m.settle('btc-updown-5m-1','down');j=m.done[-1]['v']['join']
        self.assertTrue(j['both']);self.assertGreater(j['pnlUsd'],-5)   # 0.45+0.52 < 1 per share pair
    def test_peg_follows_the_bid_to_the_back_of_the_queue(self):
        m=self.m;m.on_print(pr(E-260,'UP',.46,5,'BUY',.87,bid=.45,bs=100))
        m.on_print(pr(E-250,'UP',.47,5,'BUY',.83,bid=.46,bs=30))
        p=m.q['btc-updown-5m-1']['v']['peg']['UP'];self.assertEqual(p['price'],.46);self.assertEqual(p['ahead'],30);self.assertEqual(p['reposts'],1)
        self.assertEqual(m.q['btc-updown-5m-1']['v']['join']['UP']['price'],.45)
