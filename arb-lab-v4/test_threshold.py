import json,math,unittest
import poly_threshold as th

NOW=1_793_000_000.;END=NOW+86400

class Resp:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data

def candles(spot,n,period,vol):
    # alternating returns with the requested annualized volatility
    r=vol*math.sqrt(period/th.YEAR);out=[];p=spot
    for i in range(n):out.append([NOW-(n-i)*period,0,0,0,p,0]);p*=math.exp(r if i%2 else -r)
    out[-1][4]=spot;return out

class FakeHttp:
    def __init__(self,books,closed=None):
        self.books=books;self.closed=closed or {};self.posts=0
        strikes=[108000,110000,112000]
        self.event={'slug':'bitcoin-above-on-october-27','title':'Bitcoin above ___ on October 27?','markets':[
            {'slug':f'btc-{k}','groupItemTitle':f'{k:,}','question':f'Will the price of Bitcoin be above ${k:,} on October 27?',
             'outcomes':'["Yes","No"]','clobTokenIds':json.dumps([f'y{k}',f'n{k}']),'endDate':'2026-10-27T16:00:00Z','closed':False,'active':True}
            for k in strikes]}
    async def get(self,url,params=None):
        if 'coinbase' in url:
            g=params['granularity'];return Resp(candles(110000.,300 if g==300 else 170,g,0.8))
        if url.endswith('/events'):return Resp([self.event] if params.get('tag_slug') else [])
        if url.endswith('/markets'):return Resp([self.closed.get(params['slug'],{'closed':False})])
        if url.endswith('/book'):return Resp(self.books.get(params['token_id'],{}))
        raise AssertionError(url)
    async def post(self,url,json=None):
        self.posts+=1;return Resp([{**self.books[t['token_id']],'asset_id':t['token_id']} for t in json if t['token_id'] in self.books])

class Store:
    url=None;active=True
    def __init__(self):self.events=[]
    def enqueue(self,e):self.events.append(e)

def book(ask,size=500):return {'asks':[{'price':str(ask),'size':str(size)},{'price':'0.99','size':'10'}],'bids':[]}

class ThresholdTests(unittest.IsolatedAsyncioTestCase):
    def test_model_math(self):
        self.assertAlmostEqual(th.prob_above(100,100,0.5,1e-9*th.YEAR+1),0.5,2)
        self.assertGreater(th.prob_above(110,100,0.5,86400),0.9)
        p=th.prob_above(100,105,0.6,86400);self.assertAlmostEqual(th.implied_vol(100,105,86400,p),0.6,3)
        self.assertAlmostEqual(th.realized_vol([c[4] for c in candles(100.,300,300,0.4)],300),0.4,1)
        self.assertEqual(th.parse_strike({'groupItemTitle':'112,000'}),112000.)
        self.assertEqual(th.parse_strike({'question':'Will Bitcoin be above $95.5k on Oct 1?'}),95500.)
        self.assertTrue(th.is_above_event({'title':'Ethereum above ___ on October 3?'},'ethereum'))
        self.assertFalse(th.is_above_event({'title':'Bitcoin price between on October 3?'},'bitcoin'))

    def test_rules(self):
        m={'endAt':END,'strike':110000.}
        p=th.prob_above(110000.,110000.,0.5,86400)
        self.assertEqual(th.decide(m,110000.,0.5,(0.40,500),(0.62,500),NOW,1.0)[0],'YES')
        self.assertIsNone(th.decide(m,110000.,0.5,(0.40,500),(0.62,500),NOW,1.6)[0])           # vol filter
        self.assertIsNone(th.decide(m,110000.,0.5,(0.15,500),(0.90,500),NOW,1.0)[0])           # below 20c / no edge
        self.assertIsNone(th.decide(m,110000.,0.5,(round(p-0.01,2),500),(0.9,500),NOW,1.0)[0]) # edge after fee < 3c
        self.assertIsNone(th.decide({'endAt':NOW+600,'strike':1.},110000.,0.5,(0.4,500),None,NOW,1.0)[0])
        self.assertIsNone(th.decide(m,110000.,0.5,(0.40,5),None,NOW,1.0)[0])                   # too thin

    async def _lane(self,books,closed=None):
        lane=th.ThresholdLane(http=FakeHttp(books,closed));return lane

    async def test_scan_enters_cheap_side_and_resolves(self):
        lane=th.ThresholdLane(http=FakeHttp({}))
        for m in lane.http.event['markets']:m['endDate']=th.dt.datetime.fromtimestamp(END,th.dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        sigma,spot=await lane.vol('BTC-USD')
        pa=th.prob_above(spot,110000.,sigma,END-NOW);p2=th.prob_above(spot,112000.,sigma,END-NOW)
        lane.http.books={'y110000':book(round(pa+.01,3)),'n110000':book(round(1-pa+.01,3)),        # ATM fair: sets implied vol
                         'y112000':book(round(p2-.08,3)),'n112000':book(round(1-p2+.09,3)),        # 112k Yes 8c cheap
                         'y108000':book(.97),'n108000':book(.05)}
        st=Store();await lane.scan(st,NOW)
        entries=[e['row'] for e in st.events if e['action']=='ENTRY']
        self.assertEqual([(r['strike'],r['side']) for r in entries],[(112000.,'YES')],lane.state['skipped'])
        pos=entries[0];self.assertTrue(.2<=pos['entryPrice']<=.95);self.assertGreaterEqual(pos['edge'],th.MIN_EDGE)
        self.assertLess(max(pos['volRatio'],1/pos['volRatio']),1.5)
        await lane.scan(st,NOW+300);self.assertEqual(len([e for e in st.events if e['action']=='ENTRY']),1)   # never re-enters
        await lane.resolve(st,END+700);self.assertEqual(len(lane.open),1)                                       # not resolved yet
        lane.http.closed[pos['slug']]={'closed':True,'outcomePrices':'["1","0"]'}
        await lane.resolve(st,END+700)
        out=[e for e in st.events if e['action']=='OUTCOME'][0]['row']
        self.assertTrue(out['won']);self.assertAlmostEqual(out['pnl'],pos['shares']-pos['costUsd'],5)
        self.assertEqual(lane.report()['resolved'],1);self.assertEqual(lane.open,{})

    async def test_vol_filter_blocks_mispriced_ladder_and_restore(self):
        books={f'{s}{k}':book(.5) for s in 'yn' for k in (108000,110000,112000)}   # every strike at 50c: nonsense ladder
        lane=await self._lane(books);st=Store()
        for m in lane.http.event['markets']:m['endDate']=th.dt.datetime.fromtimestamp(END,th.dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        await lane.scan(st,NOW)
        # restore rebuilds open positions and never double-counts outcomes
        fresh=th.ThresholdLane(http=FakeHttp({}))
        fresh.restore(st.events+[{'action':'OUTCOME','row':{'slug':'btc-x','pnl':1.,'won':True,'entryPrice':.5,'shares':10,'endAt':END,'asset':'BTC'}}]*2)
        self.assertEqual(len(fresh.closed),1);self.assertTrue(fresh.state['restored'])
        self.assertEqual(set(fresh.open),{e['row']['slug'] for e in st.events if e['action']=='ENTRY'})

    async def test_daily_slug_with_year_is_found(self):
        class H(FakeHttp):
            async def get(self,url,params=None):
                if url.endswith('/events') and params.get('slug')=='bitcoin-above-on-october-27-2026':return Resp([self.event])
                if url.endswith('/events'):return Resp([])
                return await super().get(url,params)
        lane=th.ThresholdLane(http=H({}))
        ms=await lane.discover('BTC',th.dt.datetime(2026,10,26,12,tzinfo=th.dt.timezone.utc).timestamp())
        self.assertEqual(len(ms),3);self.assertEqual({m['event'] for m in ms},{'bitcoin-above-on-october-27'})

    def test_report_gate(self):
        lane=th.ThresholdLane()
        for i in range(70):
            lane.closed.append({'slug':f's{i}','asset':'BTC','entryPrice':.5,'shares':10.,'won':i%2==0,'endAt':NOW+i*86400,
                                'pnl':(10-5.0175) if i%2==0 else -5.0175})
        r=lane.report();self.assertEqual(r['resolved'],70);self.assertIn(r['status'],('PASS','FAIL'))
        self.assertTrue(r['checks']['trades']);self.assertIsNotNone(r['dayBlockBootstrap'])
        self.assertEqual(th.ThresholdLane().report()['status'],'COLLECTING')

    def test_signal_kind_is_durable(self):
        import poly_storage;self.assertIn('THRESHOLD_SHADOW',poly_storage.KINDS)

if __name__=='__main__':unittest.main()
