import asyncio,tempfile,unittest
import poly_eventmaker as em
NOW=em.REGISTERED_AT+3600
def bk(bid,ask,bs=100,as_=100):return {'bids':[{'price':str(bid),'size':str(bs)}],'asks':[{'price':str(ask),'size':str(as_)}]}
class EventMakerTests(unittest.TestCase):
    def setUp(self):
        self.x=em.EventMaker(root=tempfile.mkdtemp(),now=lambda:NOW)
        self.m=em.Mkt('game','0xc','Y','N','sports',NOW+5*86400);self.x.mk['game']=self.m
    def run_poll(self,book,trades,t):
        self.x.now=lambda:t
        async def get(url,params=None):return book if url.endswith('/book') else trades
        self.x.get=get;asyncio.run(self.x.poll(self.m))
    def test_quotes_queue_fills_merge_and_rebate(self):
        self.run_poll(bk(.40,.43),[],NOW)                                   # post bid .40 (100 ahead), ask .43 (100 ahead)
        self.assertEqual(self.m.bid['price'],.40);self.assertEqual(self.m.ask['ahead'],100)
        tr=[{'asset':'Y','side':'SELL','price':.40,'size':105,'timestamp':NOW+5,'transactionHash':'a'},       # eats 100 ahead, fills 5
            {'asset':'N','side':'BUY','price':.58,'size':10,'timestamp':NOW+6,'transactionHash':'b'},         # YES-terms SELL at .42 > bid: no
            {'asset':'Y','side':'BUY','price':.45,'size':20,'timestamp':NOW+7,'transactionHash':'c'}]          # through our ask: fill 10
        self.run_poll(bk(.40,.43),tr,NOW+30)
        self.assertAlmostEqual(self.m.fills,2);self.assertEqual(self.m.yes_sh,0.);self.assertEqual(self.m.no_sh,5.)  # 5 YES + 5 NO merged
        self.assertAlmostEqual(self.m.cash,-5*.40-10*(1-.43)+5,4);self.assertGreater(self.m.rebate,0)
        self.run_poll(bk(.40,.43),tr,NOW+60);self.assertEqual(self.m.fills,2)  # same trades are not counted twice
    def test_skew_stops_bidding_when_long(self):
        self.m.yes_sh=25.;self.m.bid=None;self.run_poll(bk(.40,.43),[],NOW)
        self.assertIsNone(self.m.bid);self.assertIsNotNone(self.m.ask)
    def test_settle_and_report(self):
        self.m.yes_sh=10.;self.m.cash=-4.;self.m.end=NOW-10
        async def get(url,params=None):return [{'slug':(params or {}).get('slug'),'closed':True,'outcomePrices':'["1","0"]'}]
        self.x.get=get;asyncio.run(self.x.settle_closed())
        self.assertNotIn('game',self.x.mk);self.assertAlmostEqual(self.x.closed[-1]['pnlUsd'],6.,4)
        self.assertIn('M3',self.x.report())
