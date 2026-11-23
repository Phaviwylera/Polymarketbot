import random,unittest
from unittest import mock
import poly_lossbook as lb
NOW=1795000000.
def s2row(i,ask,won,secs=150,bps=10.,lane='BTC'):
    c=ask+lb.fee(ask);cs=min(.999,ask+.005)+lb.fee(ask);sh=5/c
    return {'t':NOW-3600*i,'lane':lane,'token':'UP','ask':ask,'p':ask+.03,'bps':bps,'secs':secs,'shares':sh,'won':won,
            'retStressed':((1 if won else 0)-cs)/cs,'pnlUsd':sh*((1 if won else 0)-c)}
class LossBookTests(unittest.TestCase):
    def test_flags_a_clearly_losing_segment_only_with_enough_trades(self):
        rows=[s2row(i,.92,i%4!=0,secs=30) for i in range(200)]          # late 92c buys that win 75%: clearly losing
        rows+=[s2row(i,.60,i%10<8,secs=250) for i in range(200)]       # 60c buys winning 80%: clearly winning
        b=lb.s2_book(rows,NOW)
        self.assertEqual(b['segments']['timeLeft']['<60s']['flag'],'losing')
        self.assertEqual(b['segments']['askBand']['60-80c']['flag'],'winning')
        small=lb.s2_book(rows[:20],NOW);self.assertEqual(small['segments']['timeLeft']['<60s']['flag'],'unclear')   # n < 50
        self.assertEqual(b['losses']['tags']['late entry (<60s left)'],50)                 # the 50 losing late 92c buys
    def test_noise_is_not_flagged(self):
        random.seed(3);rows=[s2row(i,.9,random.random()<.9+lb.fee(.9)+.005) for i in range(400)]   # fair coin at cost: no edge
        flags=lb.flagged(lb.s2_book(rows,NOW));self.assertEqual([f for f in flags if f['dimension']=='lane'],[])
    def test_k1_counts_venue_disagreement_losses(self):
        rows=[{'lane':'BTC15','agree':True,'trade':{'pair':'A','cost':.97,'left':.5,'t':NOW,'pnlPerPair':.03,'pnlUsd':.15}} for _ in range(60)]
        rows+=[{'lane':'BTC15','agree':False,'trade':{'pair':'A','cost':.97,'left':.5,'t':NOW,'pnlPerPair':-.97,'pnlUsd':-4.85}}]
        b=lb.k1_book(rows,NOW);self.assertEqual(b['losses']['fromVenueDisagreement'],1);self.assertAlmostEqual(b['losses']['usdFromVenueDisagreement'],-4.85)
        self.assertIn('DISAGREED',b['segments']['venuesAgreed']);self.assertEqual(b['segments']['gap']['2-4c']['n'],61)
    def test_report_without_running_lanes(self):
        with mock.patch('poly_spotlead.CURRENT',None),mock.patch('poly_kalshi.CURRENT',None):
            r=lb.report(compact=True);self.assertIsNone(r['S2']);self.assertEqual(r['flags'],{'S2':[],'K1':[]})
