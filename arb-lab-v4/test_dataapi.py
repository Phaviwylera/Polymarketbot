import asyncio,unittest
import poly_dataapi as da
class DataApiTests(unittest.TestCase):
    def test_v1_names(self):
        r=da.v1({'proxy_wallet':'0xa','condition_id':'0xc','token_id':'1','outcome_index':0,'transaction_hash':'h','user_name':'u','price':.5})
        self.assertEqual(r,{'proxyWallet':'0xa','conditionId':'0xc','asset':'1','outcomeIndex':0,'transactionHash':'h','userName':'u','price':.5})
    def test_trades_pages_by_cursor_with_v2_params(self):
        calls=[]
        async def get(url,params):
            calls.append((url,dict(params)))
            if 'cursor' not in params:return {'data':[{'price':.4,'token_id':'a'}],'pagination':{'next_cursor':'c2'}}
            return {'data':[{'price':.6,'token_id':'b'}],'pagination':{'next_cursor':None}}
        rows=asyncio.run(da.trades(get,market='0xc',limit=1000,taker_only=False,max_rows=5000,pause=0))
        self.assertEqual([r['asset'] for r in rows],['a','b']);self.assertTrue(calls[0][0].endswith('/v2/trades'))
        self.assertEqual(calls[0][1]['condition'],'0xc');self.assertEqual(calls[0][1]['taker_only'],'false');self.assertNotIn('offset',calls[0][1])
        self.assertEqual(calls[1][1]['cursor'],'c2')
    def test_max_rows_and_plain_list(self):
        async def get(url,params):return [{'price':i} for i in range(10)]
        self.assertEqual(len(asyncio.run(da.trades(get,market='x',limit=5,pause=0))),5)
    def test_user_window_only_with_user(self):
        seen={}
        async def get(url,params):seen.update(params);return {'data':[]}
        asyncio.run(da.trades(get,market='x',start=5,pause=0));self.assertNotIn('start',seen)
        asyncio.run(da.trades(get,user='0xu',start=5,pause=0));self.assertEqual(seen['start'],5)
