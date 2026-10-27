import asyncio,datetime as dt,json,math,tempfile,unittest
import poly_xmarket as px,poly_threshold as pt
NOW=px.REGISTERED_AT+3600
def exp_name(ts):
    d=dt.datetime.fromtimestamp(ts,dt.timezone.utc);return f"{d.day}{d.strftime('%b').upper()}{d.strftime('%y')}"
def chain_rows(exp,F=80000,ivs=None):
    ivs=ivs or {k:50 for k in range(60000,100001,5000)}
    return [{'instrument_name':f'BTC-{exp_name(exp)}-{k}-{cp}','mark_iv':iv,'underlying_price':F} for k,iv in ivs.items() for cp in 'CP']
class R:
    def __init__(self,d):self.d=d;self.status_code=200
    def raise_for_status(self):pass
    def json(self):return self.d
class XTests(unittest.TestCase):
    def test_flat_smile_matches_lognormal_and_skew_moves_it(self):
        e1=NOW+2*86400+3600;T=NOW+2*86400            # one Deribit expiry just after T
        e1=int(e1//86400)*86400+8*3600
        if e1<T:e1+=86400
        ch=px.parse_deribit(chain_rows(e1));self.assertEqual(len(ch),1)
        p=px.deribit_prob(ch,82000,T,NOW);ref=pt.prob_above(80000,82000,.5,T-NOW)
        self.assertAlmostEqual(p,ref,2)
        sk={k:50+(80000-k)/2000 for k in range(60000,100001,5000)}  # iv falls with strike -> digital above is higher
        p2=px.deribit_prob(px.parse_deribit(chain_rows(e1,ivs=sk)),82000,T,NOW);self.assertGreater(p2,p)
        self.assertIsNone(px.deribit_prob(ch,82000,e1+86400,NOW))   # no expiry after T
    def test_ladder_gap_trade_and_settlement(self):
        x=px.XMarket(root=tempfile.mkdtemp(),now=lambda:NOW);end=NOW+86400
        m1={'asset':'BTC','event':'e','slug':'a80','strike':80000,'endAt':end,'yesToken':'y1','noToken':'n1'}
        m2={'asset':'BTC','event':'e','slug':'a82','strike':82000,'endAt':end,'yesToken':'y2','noToken':'n2'}
        async def disc(asset,now):return [m1,m2] if asset=='BTC' else []
        async def books(toks):
            b=lambda bid,ask:{'bids':[{'price':str(bid),'size':'100'}],'asks':[{'price':str(ask),'size':'100'}]}
            return {'y1':b(.40,.42),'n1':b(.56,.58),'y2':b(.40,.45),'n2':b(.40,.45)}   # YES80 .42 + NO82 .45 < 1
        async def chain(cur):return {}
        x.tl.discover=disc;x.tl.books=books;x.chain=chain
        asyncio.run(x.scan());self.assertEqual(x.state['gapsSeen'],1);self.assertEqual(len(x.arbs),1)
        a=next(iter(x.arbs.values()));self.assertGreater(a['gainPerPair'],0)
        x.now=lambda:end+700
        async def get(url,params=None):return [{'closed':True,'outcomePrices':json.dumps(['1','0'] if params['slug']=='a80' else ['0','1'])}]
        x.tl.get=get;asyncio.run(x.resolve())                         # 80k YES won, 82k NO won: pays 2 per pair
        r=x.report();self.assertEqual(r['ladder']['pairsSettled'],1);self.assertGreater(r['ladder']['pnlUsd'],0)
        x.save();y=px.XMarket(root=x.root);self.assertTrue(y.load());self.assertEqual(len(y.closed),1)
    def test_deribit_trade_snapshot_and_score(self):
        x=px.XMarket(root=tempfile.mkdtemp(),now=lambda:NOW);end=NOW+3300     # inside the 1h horizon window (3000-3600 s left)
        m={'asset':'BTC','event':'e','slug':'a78','strike':78000,'endAt':end,'yesToken':'y','noToken':'n'}
        ch={end+3600:{'F':80000,'smile':[(k,.5) for k in range(60000,100001,5000)]}}
        x._deribit(m,(.60,100),(.42,100),(.58,100),(.40,100),ch,NOW)
        self.assertIn(('a78','1h'),x.snaps);self.assertIn('a78',x.trades);self.assertEqual(x.trades['a78']['side'],'YES')
        x.now=lambda:end+700
        async def get(url,params=None):return [{'closed':True,'outcomePrices':'["1","0"]'}]
        x.tl.get=get;asyncio.run(x.resolve())
        r=x.report();self.assertEqual(r['X1']['trades'],1);self.assertEqual(r['calibration']['1h']['n'],1)
