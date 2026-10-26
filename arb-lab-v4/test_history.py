import asyncio,json,os,tempfile,unittest
import poly_history as ph,poly_study as st

START=1_792_800_000                      # a 5-minute boundary
class Resp:
    def __init__(self,d):self.d=d
    def raise_for_status(self):pass
    def json(self):return self.d
class Http:
    def __init__(self):self.calls=[]
    async def get(self,url,params=None):
        self.calls.append((url,dict(params or {})))
        if url.endswith('/markets'):
            if params.get('closed')!='true':return Resp([])
            return Resp([{'conditionId':'0xc','outcomes':'["Up","Down"]','outcomePrices':'["1","0"]','closed':True}])
        if url.endswith('/trades'):
            if params['offset']>0:return Resp([])
            return Resp([{'proxyWallet':'0x'+'a'*40,'side':'BUY','outcome':'Up','price':0.6,'size':10,'timestamp':START+200},
                         {'proxyWallet':'0x'+'b'*40,'side':'BUY','outcome':'Down','price':0.4,'size':5,'timestamp':START+250},
                         {'proxyWallet':'0x'+'b'*40,'side':'SELL','outcome':'Up','price':0.7,'size':3,'timestamp':START+260}])
        if 'candles' in url:
            return Resp([[START+60*i,0,0,80000+i,80000+i*2,0] for i in range(300)])
        raise AssertionError(url)

class HistoryTests(unittest.TestCase):
    def test_round_builds_labelled_rows_with_spot_context_and_feeds_the_study(self):
        h=ph.History(st.Study('history'),root=tempfile.mkdtemp(),http=Http(),days=1)
        n=asyncio.run(h.round('BTC','btc',300,'BTC-USD',START));self.assertEqual(n,3)
        self.assertIn('btc-updown-5m-%d'%START,h.done);self.assertEqual(h.study.totals['rounds'],1)
        r=h.study.rounds[-1];self.assertEqual(r['winner'],'UP');self.assertEqual(r['buys'],2)
        rows=h.buffer[next(iter(h.buffer))];up=[x for x in rows if x['token']=='UP'][0]
        self.assertTrue(up['won']);self.assertGreater(up['spotVsOpenBps'],0);self.assertAlmostEqual(up['leftFrac'],100/300,3)
        self.assertEqual(h.study.wallet_rounds[-1]['winner'],'UP')
        self.assertEqual(asyncio.run(h.round('BTC','btc',300,'BTC-USD',START)),0)        # never twice
    def test_spot_has_no_look_ahead(self):
        h=ph.History(st.Study('history'),root=tempfile.mkdtemp(),http=Http(),days=1)
        asyncio.run(h.round('BTC','btc',300,'BTC-USD',START))
        up=[x for x in h.buffer[next(iter(h.buffer))] if x['token']=='UP'][0]       # trade at START+200 (minute 3)
        self.assertAlmostEqual(up['spotVsOpenBps'],(80004/80000-1)*1e4,2)           # minute 2's close (ended START+180), not minute 3's
        self.assertEqual(h.snapshot()['version'],'v3')
    def test_plan_is_oldest_first_and_skips_done(self):
        h=ph.History(st.Study('history'),root=tempfile.mkdtemp(),http=Http(),days=1)
        p=h.plan(now=START+86400);self.assertEqual(p,sorted(p));self.assertTrue(all(x[0]+x[3]<=START+86400-1800 for x in p))
        h.done.add(f'btc-updown-5m-{p[0][0]}' if p[0][2]=='btc' and p[0][3]==300 else 'x');self.assertLessEqual(len(h.plan(now=START+86400)),len(p))
    def test_flush_and_resume(self):
        d=tempfile.mkdtemp();h=ph.History(st.Study('history'),root=d,http=Http(),days=1)
        asyncio.run(h.round('BTC','btc',300,'BTC-USD',START));asyncio.run(h.flush())
        self.assertTrue(os.listdir(os.path.join(d,'history_v3')))
        h2=ph.History(st.Study('history'),root=d,http=Http(),days=1);h2.load()
        self.assertIn('btc-updown-5m-%d'%START,h2.done);self.assertEqual(h2.study.totals['rounds'],1)
    def test_unresolved_market_is_retried_later(self):
        class U(Http):
            async def get(self,url,params=None):
                if url.endswith('/markets'):return Resp([{'conditionId':'0xc','outcomes':'["Up","Down"]','outcomePrices':'["0.5","0.5"]'}])
                return await super().get(url,params)
        h=ph.History(st.Study('history'),root=tempfile.mkdtemp(),http=U(),days=1)
        self.assertEqual(asyncio.run(h.round('BTC','btc',300,'BTC-USD',START)),0);self.assertNotIn('btc-updown-5m-%d'%START,h.done)

class StudyTimeTests(unittest.TestCase):
    def test_time_of_day_mistakes_reversals(self):
        from test_study import round_rows
        import random
        s=st.Study();rng=random.Random(5)
        for k in range(30):s.on_round(round_rows(rng,f'r{k}',START+k*3600,'UP'),'up')
        tod=s.time_of_day();self.assertEqual(len(tod),24);self.assertTrue(any(h['buys'] for h in tod))
        self.assertEqual(tod[0]['istHour'],5)                     # UTC 00 = IST 05:30 hour
        self.assertTrue(s.mistakes());self.assertIsInstance(s.reversals(),dict)
        c=s.compact();self.assertIn('bestWindows',c);self.assertIn('mistakes',c)

class LearnerMigrationTests(unittest.TestCase):
    def test_new_features_keep_old_weights(self):
        import poly_learner as pl
        L=pl.Learner();old={'names':['bias','logitPrice','spotAgree'],'w':[.1,1.2,.5],'g2':[1,1,1],'rounds':355,'prints':9,'score':{},'recent':[]}
        self.assertTrue(L.restore(old));self.assertEqual(L.rounds,355)
        self.assertAlmostEqual(L.w[pl.NAMES.index('logitPrice')],1.2);self.assertAlmostEqual(L.w[pl.NAMES.index('spotAgree')],.5)
        self.assertEqual(L.w[pl.NAMES.index('hourSin')],0.);self.assertIn('hourSin',L.migratedFeatures)
