import math,random,tempfile,unittest
import poly_learner as pl,poly_tape as pt

def rnd_rows(rng,slug,end,signal):
    """One round: true chance p; market prices noisy around a stale value; our model sees p when signal=True."""
    p=rng.uniform(.2,.8);won=rng.random()<p;rows=[]
    for i in range(rng.randint(5,60)):
        price=min(.97,max(.03,.5+.5*(p-.5)+rng.gauss(0,.05)))        # market under-reacts to the truth
        m=min(.99,max(.01,p+rng.gauss(0,.03))) if signal else min(.99,max(.01,rng.uniform(.2,.8)))
        rows.append({'slug':slug,'lane':'BTC','t':end-200+i,'roundEnd':end,'price':price,'pToken':m,'size':10,'aggressor':'BUY','won':won})
    return rows

class LearnerTests(unittest.TestCase):
    def test_starts_as_the_market_price(self):
        L=pl.Learner()
        for q in (.1,.37,.5,.83):self.assertAlmostEqual(L.predict({'price':q,'pToken':None,'lane':'ETH'}),q,6)
    def test_learns_a_real_edge_over_the_market(self):
        rng=random.Random(1);L=pl.Learner()
        for k in range(600):L.learn_round(rnd_rows(rng,f's{k}',1e9+k*300,True))
        s=L.summary();self.assertTrue(s['learnedBeatsPrice'],s);self.assertGreater(s['weights']['modelMinusPrice'],.2)
    def test_noise_model_does_not_beat_the_market_by_much(self):
        rng=random.Random(2);L=pl.Learner()
        for k in range(600):L.learn_round(rnd_rows(rng,f's{k}',1e9+k*300,False))
        s=L.summary();self.assertLess(abs(s['weights']['modelMinusPrice']),.25)
        self.assertLess(s['brierLearned']-s['brierPrice'],.01)          # stays near the market, does not blow up
    def test_each_round_counts_once(self):
        L=pl.Learner()
        big=[{'slug':'a','lane':'BTC','t':1,'roundEnd':2,'price':.5,'pToken':.9,'size':1,'aggressor':'BUY','won':True}]*1000
        L.learn_round(big);w_big=list(L.w)
        L2=pl.Learner();L2.learn_round(big[:1])
        self.assertAlmostEqual(w_big[2],L2.w[2],4)                     # 1000 prints of one round == 1 print
    def test_export_restore(self):
        rng=random.Random(3);L=pl.Learner()
        for k in range(50):L.learn_round(rnd_rows(rng,f's{k}',1e9+k*300,True))
        M=pl.Learner();self.assertTrue(M.restore(L.export()));self.assertEqual(M.w,L.w);self.assertEqual(M.rounds,50)

class TapeLearnerTests(unittest.TestCase):
    def test_prints_get_learned_probability_and_learning_happens_at_settle(self):
        T=1.8e9;tape=pt.Tape(root=tempfile.mkdtemp(),now=lambda:T)
        r=tape.on_print('BTC',{'t':T,'side':'up','price':.55,'size':5,'aggressor':'BUY','marketSlug':'m'},{'slug':'m','endTs':T+100},{'pUp':.7,'se':.03,'at':T-1})
        self.assertIn('pLearned',r);self.assertAlmostEqual(r['pLearned'],.55,3)   # untrained: equals the price
        self.assertEqual(r['learnedAction'],'SKIP')
        tape.settle('m','up');self.assertEqual(tape.learner.rounds,1)
        c=tape.compact();self.assertEqual(c['learner']['rounds'],1)
    def test_warm_start_replays_history_once(self):
        import asyncio
        rng=random.Random(4);tape=pt.Tape(root=tempfile.mkdtemp())
        rows=[x for k in range(30) for x in rnd_rows(rng,f's{k}',1e9+k*300,True)]
        for x in rows:x['modelAgeSec']=1.
        class S3:
            async def list_get(self,prefix,days=7):return rows
        tape.s3=S3();self.assertEqual(asyncio.run(tape.warm_start()),30);self.assertEqual(tape.learner.rounds,30)
        self.assertEqual(asyncio.run(tape.warm_start()),0)            # already has state: no double learning
    def test_edgelab_persists_learner(self):
        from poly_edge import EdgeLab
        lab=EdgeLab({});lab.learner.learn_round(rnd_rows(random.Random(5),'s',1e9,True))
        new=EdgeLab({});new.restore(lab.export());self.assertEqual(new.learner.rounds,1)
