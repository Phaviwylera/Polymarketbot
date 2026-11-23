import asyncio,datetime as dt,tempfile,unittest
import poly_kalshi as pk
END=pk.REGISTERED_AT+600
iso=lambda t:dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
class Eng:
    def __init__(self,ua,da):self.market={'slug':'btc-updown-15m-1','endTs':END};self.bbo={'up':{'ask':ua,'askSize':50},'down':{'ask':da,'askSize':50}}
class KalshiTests(unittest.TestCase):
    def test_book_parsing_both_formats(self):
        self.assertEqual(pk.kalshi_top({'orderbook_fp':{'yes_dollars':[['0.40','10'],['0.42','5']],'no_dollars':[['0.50','7']]}}),((.42,5.),(.5,7.)))
        self.assertEqual(pk.kalshi_top({'orderbook':{'yes':[[40,10]],'no':None}}),((.4,10.),None))
    def test_gap_math(self):
        # Poly UP ask .40 + Kalshi NO ask (1 - YES bid .55 = .45): .85 + fees + 2c < 1
        g=pk.gap_pairs((.40,50),(.62,50),(.55,10),(.40,10))
        self.assertEqual(len(g),1);self.assertTrue(g[0][0].startswith('A'));self.assertLess(g[0][1],1);self.assertEqual(g[0][2],5)
        self.assertEqual(pk.gap_pairs((.50,50),(.52,50),(.48,10),(.47,10)),[])
    def test_scan_settle_resolve(self):
        k=pk.Kalshi(engines={'BTC15':Eng(.40,.62)},root=tempfile.mkdtemp(),now=lambda:END-300)
        async def get(path,params=None):
            if path=='/markets':return {'markets':[{'ticker':'KXBTC15M-X','close_time':iso(END)}]}
            if path.endswith('/orderbook'):return {'orderbook_fp':{'yes_dollars':[['0.55','10']],'no_dollars':[['0.40','10']]}}
            return {'market':{'result':'no'}}
        k.get=get;asyncio.run(k.scan())
        r=k.rounds['btc-updown-15m-1'];self.assertEqual(r['ticker'],'KXBTC15M-X');self.assertIsNotNone(r['trade'])
        self.assertEqual(k.settle('btc-updown-15m-1','down'),1)
        k.now=lambda:END+200;asyncio.run(k.resolve())
        x=k.done[-1];self.assertTrue(x['agree']);self.assertEqual(x['trade']['payPerPair'],1.)   # Poly DOWN, Kalshi NO: NO leg pays
        rep=k.report();self.assertEqual(rep['K1']['trades'],1);self.assertGreater(rep['K1']['avgPnlPerPair'],0);self.assertEqual(rep['venuesDisagreePct'],0)
        k.save();k2=pk.Kalshi(root=k.root);self.assertTrue(k2.load());self.assertEqual(len(k2.done),1)
    def test_disagreement_pays_zero_or_two(self):
        k=pk.Kalshi(engines={'BTC15':Eng(.40,.62)},root=tempfile.mkdtemp(),now=lambda:END-300)
        async def get(path,params=None):
            if path=='/markets':return {'markets':[{'ticker':'T','close_time':iso(END)}]}
            if path.endswith('/orderbook'):return {'orderbook_fp':{'yes_dollars':[['0.55','10']],'no_dollars':[]}}
            return {'market':{'result':'yes'}}
        k.get=get;asyncio.run(k.scan());k.settle('btc-updown-15m-1','down');k.now=lambda:END+200;asyncio.run(k.resolve())
        x=k.done[-1];self.assertFalse(x['agree']);self.assertEqual(x['trade']['payPerPair'],0.)    # Poly DOWN, Kalshi YES: pair A pays 0
    def test_k2_waits_for_a_gap_of_at_least_4c(self):
        END2=pk.K2_REGISTERED_AT+600;eng=Eng(.47,.62);eng.market={'slug':'btc-updown-15m-2','endTs':END2}
        k=pk.Kalshi(engines={'BTC15':eng},root=tempfile.mkdtemp(),now=lambda:END2-500)
        async def get(path,params=None):
            if path=='/markets':return {'markets':[{'ticker':'T2','close_time':iso(END2)}]}
            if path.endswith('/orderbook'):return {'orderbook_fp':{'yes_dollars':[['0.55','10']],'no_dollars':[['0.40','10']]}}
            return {'market':{'result':'no'}}
        k.get=get;asyncio.run(k.scan())
        r=k.rounds['btc-updown-15m-2'];self.assertIsNotNone(r['trade']);self.assertLess(1-r['trade']['cost'],pk.K2_MIN_GAP)
        self.assertIsNone(r.get('trade2'))                                   # 2.3c net gap: K1 takes it, K2 waits
        eng.bbo['up']['ask']=.40;k.now=lambda:END2-400;asyncio.run(k.scan())
        self.assertGreaterEqual(1-r['trade2']['cost'],pk.K2_MIN_GAP);self.assertEqual(r['trade']['polyAsk'],.47)   # K1's trade unchanged
        k.settle('btc-updown-15m-2','down');k.now=lambda:END2+200;asyncio.run(k.resolve())
        x=k.done[-1];self.assertEqual(x['trade2']['payPerPair'],1.);rep=k.report()['K2']
        self.assertEqual(rep['trades'],1);self.assertGreater(rep['avgPnlPerPair'],.04);self.assertEqual(rep['verdict'],'collecting')
    def test_k2_ignores_rounds_before_registration(self):
        old={'trade2':{'t':pk.K2_REGISTERED_AT-1,'pnlPerPair':.1,'pnlUsd':.5}}
        self.assertEqual(pk.Kalshi.k2([old])['trades'],0)
