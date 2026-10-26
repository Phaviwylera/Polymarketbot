import asyncio,gzip,json,math,os,random,tempfile,unittest
import poly_study as st,poly_history as ph

def fair_round(rng,slug,end,T=300):
    """A fair market: UP price = P(final drift > 0 | path so far); prints at random times on both tokens."""
    W=0.;rows=[];path=[]
    for i in range(1,T):
        W+=rng.gauss(0,1);tau=T-i;p=0.5*(1+math.erf(W/math.sqrt(2*tau)));path.append((i,p))
    W+=rng.gauss(0,1);up=W>0
    for i,p in path:
        for _ in range(rng.randint(0,3)):
            tok=rng.choice(('UP','DOWN'));pr=p if tok=='UP' else 1-p
            if not .01<pr<.99:continue
            rows.append({'t':end-T+i,'lane':'BTC','slug':slug,'roundEnd':end,'token':tok,'price':pr,'size':1,'aggressor':'BUY',
                         'leftFrac':(T-i)/T,'secondsLeft':T-i,'agreeBps':None,'won':(tok=='UP')==up})
    return rows,'up' if up else 'down'

class FirstTouchTests(unittest.TestCase):
    def test_fair_market_shows_no_calibration_mistake(self):
        rng=random.Random(3);s=st.Study('t')
        for k in range(1500):
            rows,w=fair_round(rng,f'r{k}',1_792_800_000+300*k);s.on_round(rows,w)
        for m in s.mistakes():                     # the old all-prints mean showed about +/-6 points here
            self.assertLess(abs(m['actualMinusPrice']),0.035,m)
    def test_old_estimator_state_is_not_reloaded(self):
        p=os.path.join(tempfile.mkdtemp(),'s.json.gz')
        with gzip.open(p,'wt') as f:json.dump({'totals':{'rounds':9},'grid':{}},f)
        s=st.Study('t');self.assertFalse(s.load(p));self.assertEqual(s.totals['rounds'],0)
        s.save(p);self.assertTrue(st.Study('t').load(p))
    def test_feed_rebuilds_rounds_oldest_first_once(self):
        rng=random.Random(4);rows=[];s=st.Study('t')
        for k in (2,0,1):rows+=fair_round(rng,f'r{k}',1_792_800_000+300*k,60)[0]
        seen=set();self.assertEqual(st.feed(s,rows,seen),3);self.assertEqual([r['slug'] for r in s.rounds],['r0','r1','r2'])
        self.assertEqual(st.feed(s,rows,seen),0)
    def test_history_rebuild_from_saved_day_files(self):
        d=tempfile.mkdtemp();rng=random.Random(5);rows=[]
        for k in range(3):rows+=fair_round(rng,f'btc-updown-5m-{k}',1_792_800_000+300*k,60)[0]
        os.makedirs(os.path.join(d,'history_v3'))
        with gzip.open(os.path.join(d,'history_v3','2026-10-05.jsonl.gz'),'wt') as f:
            for r in rows:f.write(json.dumps(r)+'\n')
        h=ph.History(st.Study('history'),root=d,days=1);h.done={r['slug'] for r in rows}
        self.assertEqual(asyncio.run(h.rebuild()),3);self.assertEqual(h.study.totals['rounds'],3)
        self.assertEqual(asyncio.run(h.rebuild()),0)                    # not twice
