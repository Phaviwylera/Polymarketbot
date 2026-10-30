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
        p.gate['on']=True                                                     # trend gate open
        p.on_book('BTC','up',bbo(.40,.62),mk,t0);w=p.cur['BTC']['v']['wallet'];self.assertAlmostEqual(w.inv['up'][0],2.5,2)   # $1 / .40
        p.on_book('BTC','down',bbo(.45,.55),mk,t0+1);self.assertAlmostEqual(w.inv['down'][0],round(1/.55,2),2)       # .55 + .40 <= .97
        p.settle('btc-updown-5m-w','up');wl=p.wallet()
        self.assertEqual(wl['trades'],1);self.assertAlmostEqual(wl['equity'],200+wl['pnlUsd'],2);self.assertEqual(wl['recent'][0]['winner'],'UP')
    def test_main_account_fresh_start_constant(self):
        import poly_portfolio as pp
        self.assertGreater(pp.OWNER_FRESH_START_AT,1793000000)
def row(slug,end,shadow,wallet=None):
    v={'shadow':{'pnlUsd':shadow,'feesUsd':.01,'up':1,'down':1,'pairCost':.96,'fills':2,'costUsd':2.}}
    if wallet is not None:v['wallet']={**v['shadow'],'pnlUsd':wallet}
    return {'slug':slug,'lane':'BTC','end':end,'filled':True,'v':v}
class TrendGateTests(unittest.TestCase):
    def test_trend_rule(self):
        self.assertFalse(pa.PairAcc.trend([.1]*5,180)[0])                            # warming up
        self.assertTrue(pa.PairAcc.trend([-.1]*8+[.3]*4,180)[0])                      # last 12 net +.4, last 4 +1.2
        self.assertFalse(pa.PairAcc.trend([.5]*8+[.1,.1,-.3,-.2],180)[0])             # last 4 losing
        self.assertFalse(pa.PairAcc.trend([-.5]*8+[.1]*4,180)[0])                     # last 12 losing
        on,why,_,_=pa.PairAcc.trend([.3]*12,pa.WALLET_FLOOR-1);self.assertFalse(on);self.assertIn('hard floor',why)
    def test_shadow_trades_every_round_wallet_only_when_winning(self):
        p=pa.PairAcc(root=tempfile.mkdtemp());t0=max(END-290,pa.WALLET_START+1);mk={'slug':'btc-updown-5m-g','endTs':t0+290}
        p.on_book('BTC','up',bbo(.40,.62),mk,t0)
        self.assertGreater(p.cur['BTC']['v']['shadow'].fills,0);self.assertEqual(p.cur['BTC']['v']['wallet'].fills,0)   # gate off: watch only
        self.assertIn('warming up',p.wallet()['guard'])
        for i in range(12):p.done.append(row(f's{i}',pa.GATE_START+i,.2))
        p.refresh_gate();self.assertTrue(p.gate['on']);self.assertIsNone(p.wallet()['guard'])
        mk2={'slug':'btc-updown-5m-h','endTs':t0+590};p.on_book('BTC','up',bbo(.40,.62),mk2,t0+300)
        self.assertGreater(p.cur['BTC']['v']['wallet'].fills,0)                        # winning trend: trades
        for i in range(4):p.done.append(row(f'l{i}',pa.GATE_START+20+i,-.5))
        p.refresh_gate();self.assertFalse(p.gate['on']);self.assertIn('losing trend',p.wallet()['guard'])
        self.assertEqual([g['on'] for g in p.gate_log][-2:],[True,False])
    def test_t1_compares_gated_with_shadow(self):
        p=pa.PairAcc(root=tempfile.mkdtemp())
        p.done.append(row('a',pa.GATE_START+1,-1.0));p.done.append(row('b',pa.GATE_START+2,.4,wallet=.4))
        t=p.t1();self.assertEqual((t['shadowRounds'],t['gatedRounds']),(2,1));self.assertAlmostEqual(t['gateSavedUsd'],1.0,2);self.assertEqual(t['verdict'],'collecting')
    def test_gate_log_survives_restart(self):
        d=tempfile.mkdtemp();p=pa.PairAcc(root=d)
        for i in range(12):p.done.append(row(f's{i}',pa.GATE_START+i,.2))
        p.refresh_gate();p.save();q=pa.PairAcc(root=d);q.load()
        self.assertTrue(q.gate['on']);self.assertTrue(any(g['on'] for g in q.gate_log))
