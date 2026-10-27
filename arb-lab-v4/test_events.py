import asyncio,datetime as dt,json,tempfile,unittest
import poly_events as pe
END=1_792_000_000
iso=lambda t:dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def mk(slug,won=True,vol=90000,closed=None,**kw):
    return {'slug':slug,'question':'Will it?','outcomes':'["Yes","No"]','clobTokenIds':'["t1","t2"]','volumeNum':vol,
            'endDate':iso(END),'closedTime':iso(closed) if closed else None,'outcomePrices':'["1","0"]' if won else '["0","1"]',**kw}
class R:
    def __init__(self,d):self.d=d
    def raise_for_status(self):pass
    def json(self):return self.d
class Http:
    def __init__(self,hist):self.hist=hist;self.calls=[]
    async def get(self,url,params=None):
        self.calls.append((url,params))
        if url.endswith('/prices-history'):return R({'history':self.hist})
        return R([])
class EventTests(unittest.TestCase):
    def test_eligibility(self):
        self.assertTrue(pe.eligible(mk('a')));self.assertFalse(pe.eligible(mk('btc-updown-5m-1')));self.assertFalse(pe.eligible(mk('a',vol=10)))
        self.assertFalse(pe.eligible({**mk('a'),'outcomes':'["A","B"]'}))
    def test_rule_and_price_at(self):
        self.assertEqual(pe.rule(.88,True),('YES',round(1/.90-1,5)));self.assertEqual(pe.rule(.12,False)[0],'NO');self.assertIsNone(pe.rule(.5,True))
        h=[{'t':END-4*86400,'p':.7},{'t':END-3*86400-60,'p':.85}]
        self.assertEqual(pe.price_at(h,END-3*86400),.85);self.assertIsNone(pe.price_at(h,END-10*86400))
    def test_study_uses_scheduled_end_and_skips_after_close(self):
        h=[{'t':END-d*86400,'p':.6+d/100} for d in (7,3,1)]+[{'t':END-6*3600,'p':.9}]
        e=pe.Events(http=Http(h),root=tempfile.mkdtemp())
        r=asyncio.run(e.study_market(mk('a')));self.assertEqual(set(r['p']),{'7d','3d','1d','6h'})
        r2=asyncio.run(e.study_market(mk('b',closed=END-2*86400)))      # closed early: 1d and 6h not tradeable
        self.assertEqual(set(r2['p']),{'7d','3d'})
        self.assertIsNone(asyncio.run(e.study_market(mk('a'))))           # once
        rep=e.report();self.assertEqual(rep['historyMarkets'],2);self.assertEqual(rep['calibration']['3d']['markets'],2)
        e.save();e2=pe.Events(root=e.root);self.assertTrue(e2.load());self.assertEqual(len(e2.hist),2);self.assertIn('a',e2.done)
    def test_forward_records_at_horizon_and_settles(self):
        now=pe.REGISTERED_AT+100;end=now+3*86400-600
        class F(Http):
            async def get(s,url,params=None):
                if params.get('closed')=='false':return R([{**mk('f'),'endDate':iso(end),'bestBid':'0.86','bestAsk':'0.88'}])
                return R([{**mk('f'),'closed':True}])
        e=pe.Events(http=F([]),root=tempfile.mkdtemp(),now=lambda:now)
        asyncio.run(e.scan_forward());self.assertIn('f|3d',e.fwd)
        e.now=lambda:end+4000;asyncio.run(e.resolve_forward())
        self.assertEqual(len(e.fwd_closed),1);f=e.report()['E1']['forward'];self.assertEqual(f['n'],1);self.assertAlmostEqual(f['mean'],1/.89-1,3)
    def test_wider_settings_restart_the_day_walk(self):
        e=pe.Events(root=tempfile.mkdtemp());e.state['dayCursor']=123;e.save()
        import gzip,json as js
        st=js.load(gzip.open(e.path(),'rt'));st['cfg']=[60,50000.];js.dump(st,gzip.open(e.path(),'wt'))
        e2=pe.Events(root=e.root);e2.load();self.assertIsNone(e2.state['dayCursor'])
        e3=pe.Events(root=e.root);e3.state['dayCursor']=5;e3.save();e4=pe.Events(root=e.root);e4.load();self.assertEqual(e4.state['dayCursor'],5)
    def test_volume_tiers(self):
        e=pe.Events(root=tempfile.mkdtemp())
        e.hist={'a':{'slug':'a','endAt':END,'yesWon':True,'volume':20000,'p':{'3d':.85}},'b':{'slug':'b','endAt':END,'yesWon':False,'volume':900000,'p':{'3d':.1}}}
        bv=e.report()['E1']['byVolume'];self.assertEqual(bv['$10k-50k']['favouriteRule']['n'],1);self.assertEqual(bv['$250k+']['longshotsUnder20c']['n'],1)
