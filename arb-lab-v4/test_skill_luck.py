import random,unittest
from poly_edge import WalletIntel

class SkillLuckTests(unittest.TestCase):
    def _intel(self):
        wi=WalletIntel();rng=random.Random(7)
        for i in range(400):                       # pure coin-flip wallets
            wi.wallets[f'0x{i:040x}']={'rounds':60,'pnl':1.,'recent':[rng.choice((-1.,1.)) for _ in range(60)]}
        wi.wallets['0xskill']={'rounds':60,'pnl':30.,'recent':[1.,1.,-.5]*20}
        wi.wallets['0xlucky']={'rounds':60,'pnl':50.,'recent':[100.]+[.6]*30+[-.5]*29}
        return wi
    def test_luck_is_not_called_skill_and_real_skill_is(self):
        r=self._intel().skill_vs_luck()
        self.assertEqual(r['per']['0xskill']['verdict'],'skill')
        coin=[v['verdict'] for k,v in r['per'].items() if k.startswith('0x0')]
        self.assertLessEqual(sum(v!='luck or noise' for v in coin),4)   # FDR keeps coin-flippers out
    def test_one_big_win_is_flagged(self):
        r=self._intel().skill_vs_luck()
        self.assertIn(r['per']['0xlucky']['verdict'],('one big win','luck or noise'))
        self.assertNotEqual(r['per']['0xlucky']['verdict'],'skill')
    def test_summary_and_cache(self):
        wi=self._intel();a=wi.skill_vs_luck();self.assertIs(a,wi.skill_vs_luck())
        self.assertEqual(a['tested'],402);self.assertIn('ourSkilledTop',a)
        self.assertEqual(wi.skill_test('0xskill')['verdict'],'skill');self.assertIsNone(wi.skill_test('nope'))
