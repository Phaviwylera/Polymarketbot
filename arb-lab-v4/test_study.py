import os,random,tempfile,unittest
import poly_study as st,poly_tape as pt,poly_learner as pl

def round_rows(rng,slug,end,winner,lane='BTC'):
    """Prints where buying the side spot already favours late in the round wins more often."""
    rows=[]
    for i in range(60):
        left=rng.random();t=end-left*300;tok=rng.choice(('UP','DOWN'));agree=rng.uniform(-20,20)
        win=(tok==winner);price=min(.97,max(.03,.5+(.3 if win else -.3)*(1-left)+rng.gauss(0,.05)))
        rows.append({'slug':slug,'lane':lane,'t':t,'roundEnd':end,'token':tok,'price':price,'size':rng.uniform(5,50),'aggressor':rng.choice(('BUY','SELL')),
                     'leftFrac':left,'secondsLeft':left*300,'agreeBps':agree if win else -abs(agree),'pToken':None,'won':win})
    return rows

class StudyTests(unittest.TestCase):
    def test_round_summary_and_grid(self):
        s=st.Study();rng=random.Random(1)
        out=s.on_round(round_rows(rng,'r1',1.8e9,'UP'),'up')
        self.assertEqual(out['winner'],'UP');self.assertGreater(out['buys'],0);self.assertTrue(out['path'])
        self.assertIsNotNone(out['bestEntry']);self.assertLessEqual(len(out['topWinningBuys']),5)
        snap=s.snapshot();self.assertEqual(snap['totals']['rounds'],1);self.assertEqual(len(snap['heatmaps']['with']),len(st.LEFT_NAMES))
    def test_rule_is_prequential_and_only_trades_proven_cells(self):
        s=st.Study();rng=random.Random(2)
        s.on_round(round_rows(rng,'r0',1.8e9,'UP'),'up');self.assertEqual(s.rule_summary()['trades'],0)   # nothing proven yet
        for k in range(1,80):w=rng.choice(('UP','DOWN'));s.on_round(round_rows(rng,f'r{k}',1.8e9+k*300,w),w.lower())
        r=s.rule_summary();self.assertGreater(r['trades'],0);self.assertTrue(any(p['qualifies'] for p in s.patterns()))
        self.assertGreater(r['avgReturnPerDollar'],0)                       # the planted pattern works on unseen rounds
    def test_noise_does_not_create_a_rule(self):
        s=st.Study();rng=random.Random(3)
        for k in range(80):
            rows=round_rows(rng,f'n{k}',1.8e9+k*300,'UP')
            for r in rows:r['price']=rng.uniform(.05,.95);r['won']=rng.random()<r['price'];r['agreeBps']=rng.uniform(-20,20)   # fair prices: no edge anywhere
            s.on_round(rows,'up')
        self.assertLess(sum(p['qualifies'] for p in s.patterns()),3)
    def test_save_load(self):
        s=st.Study();rng=random.Random(4)
        for k in range(5):s.on_round(round_rows(rng,f'r{k}',1.8e9+k*300,'DOWN'),'down')
        s.on_wallet_round('BTC',1.8e9,'down',{'0x'+'a'*40:{'pnl':3.,'buys':2,'buyUsd':10.,'entrySec':[30.],'prices':[.4]}})
        p=os.path.join(tempfile.mkdtemp(),'s.json.gz');s.save(p);n=st.Study();self.assertTrue(n.load(p))
        self.assertEqual(n.totals['rounds'],5);self.assertEqual(len(n.grid),len(s.grid));self.assertEqual(len(n.wallet_rounds),1)
    def test_tape_feeds_study_with_spot_context(self):
        T=1.8e9;tape=pt.Tape(root=tempfile.mkdtemp(),now=lambda:T);tape.study=st.Study()
        r=tape.on_print('BTC',{'t':T,'side':'up','price':.6,'size':5,'aggressor':'BUY','marketSlug':'m'},{'slug':'m','endTs':T+150},
                        {'pUp':.6,'se':.03,'at':T},None,{'spot':80100.,'open':80000.,'openTrusted':True})
        self.assertAlmostEqual(r['spotVsOpenBps'],12.5,2);self.assertAlmostEqual(r['agreeBps'],12.5,2);self.assertAlmostEqual(r['leftFrac'],.5,3)
        tape.settle('m','up');self.assertEqual(tape.study.totals['rounds'],1)
        snap=tape.study.snapshot(tape,tape.learner);self.assertTrue(snap['live']);self.assertTrue(snap['cloud'])
    def test_learner_uses_spot_feature(self):
        x=pl.features({'price':.5,'agreeBps':20,'lane':'BTC','roundEnd':100,'t':0});self.assertEqual(len(x),len(pl.NAMES))
        self.assertAlmostEqual(x[pl.NAMES.index('spotAgree')],1.0)

class TradingPausedTests(unittest.TestCase):
    def test_master_pause_blocks_every_entry_path(self):
        import poly_portfolio as pf
        self.assertTrue(pf.TRADING_PAUSED)
        class E:engines={}
        self.assertIn('study mode',pf.PaperPortfolio.pause_reason(E()))
