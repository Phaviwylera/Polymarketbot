import asyncio,datetime as dt,json,tempfile,unittest
import poly_eventarb as ea
NOW=ea.REGISTERED_AT+100
iso=lambda t:dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def mk(slug,y,n,**kw):return {'slug':slug,'clobTokenIds':json.dumps([y,n]),'outcomes':'["Yes","No"]','active':True,'closed':False,'endDate':iso(NOW+86400),**kw}
def bk(bid,ask,size=50):return {'bids':[{'price':str(bid),'size':str(size)}],'asks':[{'price':str(ask),'size':str(size)}]}
class EventArbTests(unittest.TestCase):
    def test_basket_and_conversion_math(self):
        self.assertIsNone(ea.basket_gap([(.34,10),(.33,10),(.34,10)],0.04))      # 1.01 + fees
        g=ea.basket_gap([(.30,10),(.30,8),(.30,10)],0.04);self.assertLess(g[0],1);self.assertEqual(g[1],8)
        c=ea.conversion_gaps([(.40,10),None,None],[(.5,10),(.3,10),(.35,10)],0.0)   # NO_0 .40 -> YES_1 .30 + YES_2 .35 = .65
        self.assertEqual(c[0][0],0);self.assertGreater(c[0][1],0.2)
        self.assertEqual(ea.fee_rate({'tags':[{'slug':'sports'}]}),0.05);self.assertEqual(ea.fee_rate({'tags':[{'slug':'geopolitics'}]}),0.)
    def test_scan_records_basket_and_settles(self):
        x=ea.EventArb(root=tempfile.mkdtemp(),now=lambda:NOW)
        ev={'slug':'who-wins','negRisk':True,'volume':50000,'tags':[{'slug':'politics'}],'markets':[mk('a','y1','n1'),mk('b','y2','n2'),mk('c','y3','n3')]}
        aug={**ev,'slug':'aug','negRiskAugmented':True}
        async def books(t):return {'y1':bk(.29,.30),'y2':bk(.29,.30),'y3':bk(.29,.30),'n1':bk(.69,.71),'n2':bk(.69,.71),'n3':bk(.69,.71)}
        x.books=books;asyncio.run(x.scan_n1([ev,aug]))
        self.assertIn('who-wins',x.n1_open);self.assertEqual(x.state['skippedAugmented'],1)
        async def res(slug):return [1.,0.] if slug=='b' else [0.,1.]
        x.market_result=res;asyncio.run(x.settle())
        r=x.report()['N1'];self.assertEqual(r['n'],1);self.assertGreater(r['mean'],0)
    def test_s1_buys_leader_in_window_and_handles_invalid(self):
        x=ea.EventArb(root=tempfile.mkdtemp(),now=lambda:NOW)
        ev={'slug':'e','tags':[{'slug':'sports'}],'markets':[mk('m1','t1','t2',umaResolutionStatus='proposed'),mk('m2','t3','t4',endDate=iso(NOW-600)),mk('m3','t5','t6')]}
        async def books(t):return {'t1':bk(.96,.97),'t2':bk(.02,.04),'t3':bk(.01,.03),'t4':bk(.97,.98),'t5':bk(.97,.98),'t6':bk(.01,.02)}
        x.books=books;asyncio.run(x.scan_s1([ev]))
        self.assertEqual(set(x.s1_open),{'m1','m2'});self.assertEqual(x.s1_open['m2']['side'],1)   # m3: no proposal, not past end
        async def res(slug):return [1.,0.] if slug=='m1' else [.5,.5]
        x.market_result=res;asyncio.run(x.settle())
        s=x.report()['S1'];self.assertEqual(s['n'],2);self.assertEqual(s['invalidResolutions'],1)
        x.save();y=ea.EventArb(root=x.root);self.assertTrue(y.load());self.assertEqual(len(y.closed),2)
