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
        p.on_book('BTC','up',bbo(.48,.53,us=3),MK,END-290);self.assertEqual(p.cur['BTC']['v']['replica'].inv['up'][0],3.)   # capped at shown size
        p1=pa.PairAcc(root=tempfile.mkdtemp());p1.on_book('BTC','up',bbo(.48,.53,us=2),MK,END-290);self.assertEqual(p1.cur['BTC']['v']['replica'].inv['up'][0],0.)   # $0.96 < $1 minimum
        p2=pa.PairAcc(root=tempfile.mkdtemp());p2.on_book('BTC','up',bbo(.40,.40),MK,END-5)    # after 95%: nothing
        self.assertEqual(p2.cur['BTC']['v']['replica'].fills,0)
        p3=pa.PairAcc(root=tempfile.mkdtemp());p3.on_book('BTC15','up',bbo(.40,.40),{'slug':'btc-updown-15m-1','endTs':END},END-800)
        self.assertEqual(p3.cur,{})                                            # 15m lanes not traded (not observed)
        b=pa.Book();b.inv={'up':[30.,14.],'down':[10.,4.8]}
        self.assertEqual(pa.PairAcc.decide(b,bbo(.45,.45),.97)[0],'down')     # UP is 20 ahead: only DOWN

class WalletTests(unittest.TestCase):
    def test_wallet_clips_dollars_and_reports_balance(self):
        p=pa.PairAcc(root=tempfile.mkdtemp());t0=max(END-290,pa.WALLET_START+1);mk={'slug':'btc-updown-5m-w','endTs':t0+290}
        p.on_book('BTC','up',bbo(.40,.62),mk,t0);w=p.cur['BTC']['v']['wallet'];self.assertAlmostEqual(w.inv['up'][0],2.5,2)   # $1 / .40
        p.on_book('BTC','down',bbo(.45,.55),mk,t0+1);self.assertAlmostEqual(w.inv['down'][0],round(1/.55,2),2)       # .55 + .40 <= .97
        p.settle('btc-updown-5m-w','up');wl=p.wallet()
        self.assertEqual(wl['trades'],1);self.assertAlmostEqual(wl['equity'],200+wl['pnlUsd'],2);self.assertEqual(wl['recent'][0]['winner'],'UP')
    def test_wallet_loss_limit_blocks_entries(self):
        p=pa.PairAcc(root=tempfile.mkdtemp())
        p.done.append({'slug':'x','lane':'BTC','end':pa.WALLET_START+10,'filled':True,'v':{'wallet':{'pnlUsd':-8.5,'feesUsd':.1,'up':1,'down':0,'pairCost':None,'fills':1,'costUsd':8.5}}})
        self.assertFalse(p.wallet_open(pa.GUARDS_OFF_UNTIL+20));self.assertIn('loss limit',p.wallet()['guard'])   # after the owner window
    def test_main_account_fresh_start_constant(self):
        import poly_portfolio as pp
        self.assertGreater(pp.OWNER_FRESH_START_AT,1793000000)
class GuardWindowTests(unittest.TestCase):
    def test_owner_window_keeps_trading_then_guards_resume(self):
        p=pa.PairAcc(root=tempfile.mkdtemp())
        p.done.append({'slug':'x','lane':'BTC','end':pa.WALLET_START+10,'filled':True,'v':{'wallet':{'pnlUsd':-9.,'feesUsd':.1,'up':1,'down':0,'pairCost':None,'fills':1,'costUsd':9.}}})
        self.assertTrue(p.wallet_open(pa.GUARDS_OFF_UNTIL-60))          # inside the owner's no-pause window
        self.assertFalse(p.wallet_open(pa.GUARDS_OFF_UNTIL+60))         # loss stop is back afterwards
