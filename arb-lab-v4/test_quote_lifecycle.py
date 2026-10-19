import asyncio,io,json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from poly_server_demo import PolyServerDemo
from poly_portfolio import PaperPortfolio
from poly_diagnostics import DiagnosticLog
from poly_audit_export import diagnostic_response

class QuoteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=PolyServerDemo('ETH',80,state_file=Path(self.tmp.name)/'state.json',risk_profile={'walletQuotes':True,'residualCap':2,'firstPriceCap':.6,'sessionLossLimit':4})
        e=self.e;e._save=lambda:None;e._new_session();now=time.time()
        e.market={'slug':'test','upToken':'u','downToken':'d','startTs':now-60,'endTs':now+240,'minOrderSize':5,'minTickSize':.01,'acceptingOrders':True}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.spot_ts=e.twap_ts=now;e.open_trusted=True
        e.bbo={'up':{'bid':.33,'ask':.34,'bidSize':10,'askSize':100},'down':{'bid':.66,'ask':.67,'bidSize':10,'askSize':100}}
        self.inv=e._inventory_get();e._inventory_place_quote(self.inv,'up',.28,5,'FIRST')
        self.addCleanup(e.diagnostics.db.close)
    def print(self,price=.27,size=5,aggressor='SELL',ts=None):
        self.e._handle_clob({'event_type':'last_trade_price','asset_id':'u','price':str(price),'size':str(size),'side':aggressor,'timestamp':(time.time() if ts is None else ts)*1000})
    def test_fresh_candidate_preference_does_not_cancel_resting_quote(self):
        e=self.e
        with patch.object(e,'_inventory_first_leg_candidate',return_value={'side':'down'}):e._inventory_process_quote(self.inv,{})
        self.assertIsNotNone(self.inv['quote']);self.assertEqual(e.paper['inventoryCancels'],0)
    def test_print_fills_at_receipt_before_later_hedge_deterioration(self):
        self.print();self.assertEqual(self.e.paper['inventoryFills'],1)
        self.e._handle_clob({'event_type':'best_bid_ask','asset_id':'d','best_ask':'.9'})
        self.assertEqual(self.e.paper['inventoryFills'],1);self.assertTrue(self.e._inventory_has_residual())
    def test_deterioration_before_print_cancels_and_cannot_fill(self):
        self.e._handle_clob({'event_type':'best_bid_ask','asset_id':'d','best_ask':'.9'})
        self.assertIsNone(self.inv['quote']);self.print();self.assertEqual(self.e.paper['inventoryFills'],0)
        self.assertEqual(self.inv['lastAction'],'hedge all-in cost exceeds target')
    def test_buy_aggressor_cannot_fill_bid(self):
        self.print(aggressor='BUY');self.assertEqual(self.e.paper['inventoryFills'],0)
    def test_old_and_future_and_timestamp_missing_prints_cannot_fill(self):
        self.print(ts=time.time()-10);self.print(ts=time.time()+60)
        self.e._handle_clob({'event_type':'last_trade_price','asset_id':'u','price':'.27','size':'5','side':'SELL'})
        self.assertEqual(self.e.paper['inventoryFills'],0)
    def test_small_through_print_not_full_fill_and_duplicate_not_counted(self):
        ts=time.time();self.print(size=2.5,ts=ts);self.print(size=2.5,ts=ts)
        self.assertEqual(self.e.paper['inventoryFills'],0)
        self.print(size=2.5,ts=ts+.001);self.assertEqual(self.e.paper['inventoryFills'],1)
    def test_at_bid_requires_known_queue_ahead(self):
        self.e._inventory_cancel_quote(self.inv,'test');self.e._inventory_place_quote(self.inv,'up',.33,5,'FIRST')
        # This quote would violate the all-in gate; improve the hedge book first.
        self.e.bbo['down']['ask']=.62
        self.print(price=.33,size=5);self.assertEqual(self.e.paper['inventoryFills'],0)
        self.print(price=.33,size=10);self.assertEqual(self.e.paper['inventoryFills'],1)
    def test_expired_or_risk_locked_or_stale_quote_does_not_fill(self):
        for gate in ('ttl','risk','feed'):
            with self.subTest(gate=gate):
                self.inv['quote']['createdAt']=time.time()-21 if gate=='ttl' else time.time()
                if gate=='risk':self.e.paper['status']='risk_locked'
                if gate=='feed':self.e.spot_ts=0
                self.print();self.assertEqual(self.e.paper['inventoryFills'],0)
                self.e.paper['status']='running';self.e.spot_ts=time.time()
                self.e._inventory_place_quote(self.inv,'up',.28,5,'FIRST')
    def test_existing_order_depth_cash_and_loss_budget_revalidation(self):
        for gate,reason in [('depth','hedge depth insufficient'),('cash','hedge cash insufficient'),('budget','remaining lane loss budget insufficient')]:
            with self.subTest(gate=gate):
                self.e.bbo['down']['askSize']=0 if gate=='depth' else 100
                self.e.paper['cash']=2 if gate=='cash' else 80
                self.e.session_loss_limit=1 if gate=='budget' else 100
                self.assertEqual(self.e._inventory_resting_reason(self.inv,{},self.inv['quote']),reason)
    def test_full_journal_preserves_raw_print_and_linked_fill_evidence(self):
        self.print();out=io.BytesIO();meta=self.e.diagnostics.export(out)
        rows=[json.loads(x) for x in out.getvalue().splitlines()]
        self.assertTrue(meta['complete']);self.assertTrue(all('journalSeq' in x for x in rows))
        self.assertTrue(any(x['kind']=='CLOB_MESSAGE' for x in rows))
        placed=next(x for x in rows if x['kind']=='QUOTE_PLACE');filled=next(x for x in rows if x['kind']=='QUOTE_FILL_EVIDENCE')
        self.assertEqual(placed['quote']['id'],filled['quote']['id']);self.assertEqual(filled['triggerTrade']['aggressor'],'SELL')
    def test_retention_explicitly_reports_missing_earlier_events(self):
        log=self.e.diagnostics;log.MAX_CRITICAL_ROWS=10
        for i in range(1000):log.append('TEST',n=i)
        out=io.BytesIO();meta=log.export(out)
        self.assertTrue(meta['earlierRowsPruned']);self.assertFalse(meta['complete']);self.assertLessEqual(meta['retained'],30)
    def test_invalid_nonfinite_volume_and_wrong_market_cannot_fill(self):
        self.print(size=float('inf'));self.assertEqual(self.e.paper['inventoryFills'],0)
        self.inv['quote']['marketSlug']='other-market'
        self.print();self.assertEqual(self.e.paper['inventoryFills'],0)
    def test_empty_authoritative_book_removes_stale_hedge_liquidity(self):
        self.e._handle_clob({'event_type':'book','asset_id':'d','bids':[],'asks':[]})
        self.assertIsNone(self.e.bbo['down']['ask']);self.assertIsNone(self.inv['quote'])
    def test_logging_failure_is_visible_without_changing_paper_balance(self):
        before=self.e.paper['cash'];db=self.e.diagnostics.db
        with patch.object(self.e.diagnostics,'db',None):
            meta=self.e.diagnostics.export(io.BytesIO());self.assertFalse(meta['enabled']);self.assertFalse(meta['complete'])
        self.assertEqual(self.e.paper['cash'],before)
    def test_export_compression_does_not_block_feed_event_loop(self):
        from types import SimpleNamespace
        portfolio=SimpleNamespace(engines={'ETH':self.e},snapshot=lambda **kw:{'paperOnly':True})
        original=self.e.diagnostics.export
        def slow_export(*args):
            time.sleep(.08);return original(*args)
        async def run():
            task=asyncio.create_task(diagnostic_response(portfolio))
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            return await task
        with patch.object(self.e.diagnostics,'export',side_effect=slow_export):
            response=asyncio.run(run())
        async def consume():
            async for chunk in response.body_iterator:pass
        asyncio.run(consume())
    def test_all_asset_bundle_contains_four_journals_and_manifest(self):
        p=PaperPortfolio()
        for a,e in p.engines.items():
            e.diagnostics.db.close();e.diagnostics=DiagnosticLog(str(Path(self.tmp.name)/(a+'.sqlite3')),a)
            self.addCleanup(e.diagnostics.db.close);e._save=lambda:None;e._new_session()
        response=asyncio.run(diagnostic_response(p))
        async def collect():
            return b''.join([chunk async for chunk in response.body_iterator])
        z=zipfile.ZipFile(io.BytesIO(asyncio.run(collect())))
        self.assertEqual(set(z.namelist()),{'snapshot.json','manifest.json','README.txt','execution-attribution.json','storage-status.json','BTC.ndjson','ETH.ndjson','SOL.ndjson','XRP.ndjson'})
        self.assertEqual(set(json.loads(z.read('manifest.json'))['assets']),set(p.engines))

if __name__=='__main__':unittest.main()
