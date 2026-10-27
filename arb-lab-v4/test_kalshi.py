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
