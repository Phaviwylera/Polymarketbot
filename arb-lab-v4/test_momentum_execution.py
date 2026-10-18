import asyncio,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
from poly_momentum import signal

class MomentumExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=mod.PolyServerDemo('ETH',80,state_file=Path(self.tmp.name)/'state.json',risk_profile={'momentumEnabled':True,'sessionLossLimit':4,'stopLoss':.30})
        e=self.e;e._save=lambda:None;e._new_session();self.addCleanup(e.close);now=time.time()
        e.market={'slug':'eth-test','upToken':'u','downToken':'d','startTs':now-60,'endTs':now+240,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.open_trusted=True;e.spot_ts=e.twap_ts=now
        e._ingest_book('up',{'bids':[{'price':'.39','size':'40'}],'asks':[{'price':'.40','size':'50'}]})
        e._ingest_book('down',{'bids':[{'price':'.59','size':'40'}],'asks':[{'price':'.60','size':'50'}]})
        self.m={'fairUp':.6,'fairDown':.4,'leadBps':1.,'impulse5Bps':.3}
        e.book_history.append({'t':now-3,'marketSlug':'eth-test','up':.375,'down':.615})
        e._model=lambda:self.m
        self.latency=patch.object(mod,'EXECUTION_LATENCY_SEC',0);self.latency.start();self.addCleanup(self.latency.stop)
    def candidate(self):
        s,r=self.e._momentum_signal(self.m);self.assertIsNotNone(s,r);return s
    async def test_funded_marketable_fill_and_fee_accounting_for_every_asset(self):
        for a in ('BTC','ETH','SOL','XRP'):
            self.e.asset=a;self.e.balance={'BTC':60,'ETH':80,'SOL':30,'XRP':30}[a];self.e.paper['cash']=self.e.balance;self.e.paper['startingBalance']=self.e.balance;self.e.paper['tradedMarkets']=[]
            candidate=self.candidate();cash=self.e.paper['cash']
            self.assertTrue(await self.e._momentum_enter(candidate))
            pos=self.e.paper['position'];self.assertEqual(pos['mode'],'MOMENTUM_SCALP')
            self.assertAlmostEqual(cash-self.e.paper['cash'],pos['entryTotalCost'])
            self.assertGreater(pos['entryFee'],0);self.assertEqual(pos['shares'],5);self.assertLessEqual(pos['entryTotalCost'],min(3.25,self.e.balance*.1))
            self.e.paper['cash']+=pos['entryTotalCost'];self.e.paper['position']=None
    async def test_profit_exit_uses_actual_bid_and_both_fees(self):
        self.e._save=lambda:self.e.snapshot()
        await self.e._momentum_enter(self.candidate());pos=dict(self.e.paper['position']);self.e.bbo['up'].update(bid=.50,bidSize=100)
        await self.e._momentum_manage(self.m)
        self.assertIsNone(self.e.paper['position']);expected=5*(.50-mod._fee_per_share(.50))-pos['entryTotalCost']
        self.assertAlmostEqual(self.e.paper['realizedPnl'],expected);self.assertAlmostEqual(self.e.paper['cash'],80+expected)
        self.assertEqual(self.e.paper['trades'],1);self.assertEqual(self.e.paper['momentumCloses'],1)
        self.assertLess(self.e.paper['peakEquity'],81)
    async def test_post_latency_price_move_does_not_create_fill(self):
        c=self.candidate()
        async def change(*args):self.e.bbo['up']['ask']=.43
        with patch.object(mod.asyncio,'sleep',side_effect=change):self.assertFalse(await self.e._momentum_enter(c))
        self.assertIsNone(self.e.paper['position']);self.assertEqual(self.e.paper['cash'],80)
    async def test_session_and_market_change_abort_delayed_entry(self):
        for change in ('market','session'):
            c=self.candidate();old=self.e.paper
            async def mutate(*args):
                if change=='market':self.e.market_version+=1
                else:self.e._new_session()
            with patch.object(mod.asyncio,'sleep',side_effect=mutate):self.assertFalse(await self.e._momentum_enter(c))
            self.assertIsNone(self.e.paper['position']);self.assertEqual(self.e.paper['cash'],80)
    async def test_risk_lock_or_stale_or_unknown_depth_prevents_entry(self):
        for gate in ('risk','book','oracle','depth'):
            c=self.candidate();e=self.e
            if gate=='risk':e.paper['status']='risk_locked';self.assertFalse(await e._momentum_enter(c));e.paper['status']='running'
            else:
                old=(e.book_observed['up'],e.spot_ts,e.bbo['up']['askSize'])
                if gate=='book':e.book_observed['up']=time.time()-10
                if gate=='oracle':e.spot_ts=time.time()-10
                if gate=='depth':e.bbo['up']['askSize']=0
                self.assertIsNone(e._momentum_signal(self.m)[0])
                e.book_observed['up'],e.spot_ts,e.bbo['up']['askSize']=old
    async def test_loss_exit_works_without_model_and_starts_cooldown(self):
        await self.e._momentum_enter(self.candidate());self.e.bbo['up'].update(bid=.30,bidSize=100)
        await self.e._momentum_manage(None)
        self.assertIsNone(self.e.paper['position']);self.assertLess(self.e.paper['realizedPnl'],0)
        self.assertGreater(self.e.paper['cooldownUntil'],time.time());self.assertEqual(self.e.paper['losses'],1)
    async def test_one_entry_per_round_no_martingale(self):
        c=self.candidate();await self.e._momentum_enter(c);self.e.bbo['up'].update(bid=.50,bidSize=100)
        await self.e._momentum_manage(self.m);self.assertFalse(await self.e._momentum_enter(c))
        self.assertEqual(self.e.paper['momentumFills'],1)
    async def test_old_model_and_deep_passive_quote_are_not_required(self):
        self.e.lag_trading_enabled=False
        inv=self.e._inventory_get();self.e._inventory_place_quote(inv,'up',.35,5,'FIRST')
        await self.e._momentum_step(self.m)
        self.assertEqual(self.e.paper['momentumFills'],1);self.assertIsNone(inv['quote'])
    def test_price_change_updates_quantity_and_removes_exhausted_levels(self):
        e=self.e
        e._handle_clob({'event_type':'price_change','timestamp':time.time()*1000,'price_changes':[{'asset_id':'u','side':'SELL','price':'.40','size':'2','best_bid':'.39','best_ask':'.40'}]})
        self.assertEqual(e.bbo['up']['askSize'],2);self.assertIsNone(e._momentum_signal(self.m)[0])
        e._handle_clob({'event_type':'price_change','timestamp':time.time()*1000,'price_changes':[{'asset_id':'u','side':'SELL','price':'.40','size':'0','best_bid':'.39','best_ask':'.41'}]})
        self.assertEqual(e.bbo['up']['ask'],.41);self.assertEqual(e.bbo['up']['askSize'],0)
    def test_cheap_countertrend_rebound_is_rejected(self):
        self.m.update(fairUp=.35,fairDown=.65)
        self.e.bbo['up'].update(ask=.12,bid=.11)
        self.e.book_history.clear();self.e.book_history.append({'t':time.time()-3,'marketSlug':'eth-test','up':.09,'down':.91})
        candidate,reason=self.e._momentum_signal(self.m)
        self.assertIsNone(candidate);self.assertEqual(reason,'estimated round direction does not confirm scalp direction')
    def test_ask_only_midpoint_change_cannot_confirm_buy(self):
        self.e.book_history.clear();self.e.book_history.append({'t':time.time()-3,'marketSlug':'eth-test','up':.385,'upBid':.39,'down':.615})
        candidate,reason=self.e._momentum_signal(self.m)
        self.assertIsNone(candidate);self.assertEqual(reason,'best bid has not risen a full tick')
    def test_delayed_book_cannot_override_fresh_depth(self):
        e=self.e;e._handle_clob({'event_type':'book','asset_id':'u','timestamp':(time.time()-60)*1000,'bids':[{'price':'.8','size':'100'}],'asks':[{'price':'.9','size':'100'}]})
        self.assertEqual(e.bbo['up']['ask'],.40)
    async def test_pending_exit_cannot_affect_new_market_or_session(self):
        await self.e._momentum_enter(self.candidate());old=self.e.paper;cash=old['cash']
        async def mutate(*args):self.e.market_version+=1
        with patch.object(mod.asyncio,'sleep',side_effect=mutate):self.assertFalse(await self.e._try_sell('test'))
        self.assertEqual(old['cash'],cash);self.assertIsNotNone(old['position'])

    def test_configured_stop_rejects_friction_and_preserves_risk_headroom(self):
        self.e.inventory_stop=.15
        c,reason=self.e._momentum_signal(self.m)
        self.assertIsNone(c);self.assertIn('fees and spread',reason)
        self.e.inventory_stop=.40;c=self.candidate()
        self.assertEqual(c['stopLossUsd'],.40);self.assertGreaterEqual(c['takeProfitUsd'],c['stopLossUsd'])
        self.assertGreaterEqual(c['adverseMoveBudgetUsd'],.10)
    async def test_stale_exit_is_logged_and_latched_until_executable(self):
        await self.e._momentum_enter(self.candidate());self.e.bbo['up'].update(bid=.30,bidSize=100)
        async def stale(*args):self.e.book_observed['up']=time.time()-10
        with patch.object(mod.asyncio,'sleep',side_effect=stale):await self.e._momentum_manage(self.m)
        self.assertIsNotNone(self.e.paper['position'])
        self.assertIn('loss stop',self.e.paper['position']['exitRequestedReason'])
        self.assertTrue(any(x['action']=='NOFILL' and 'book older' in x['detail'] for x in self.e.paper['events']))
        self.e.book_observed['up']=time.time();self.e.bbo['up']['bid']=.40
        await self.e._momentum_manage(None)
        self.assertIsNone(self.e.paper['position'])
    async def test_delayed_entry_deadline_never_debits_cash(self):
        c=self.candidate();now=time.time()
        async def slow(*args):clock.return_value=now+1.2
        with patch.object(mod.time,'time',return_value=now) as clock,patch.object(mod.asyncio,'sleep',side_effect=slow):
            self.assertFalse(await self.e._momentum_enter(c))
        self.assertEqual(self.e.paper['cash'],80);self.assertIsNone(self.e.paper['position'])
        self.assertIn('deadline exceeded',self.e.paper['events'][0]['detail'])
    def test_market_discovery_recovers_only_timestamped_opening_twap(self):
        e=self.e;e.open_ref=None;e.open_trusted=False;start=e.market['startTs']
        e.twap_hist.extend([{'t':start-1,'p':3000},{'t':start+5,'p':3001},{'t':start+60,'p':3010}])
        e._recover_open_reference();self.assertEqual(e.open_ref,3000);self.assertTrue(e.open_trusted)
        e.open_ref=None;e.open_trusted=False;e.twap_hist.clear();e.twap_hist.append({'t':start+60,'p':3010})
        e._recover_open_reference();self.assertIsNone(e.open_ref);self.assertFalse(e.open_trusted)

    def test_stale_backlog_requests_resync_and_aggregates_warnings(self):
        e=self.e
        for i in range(60):
            e._handle_clob({'event_type':'book','asset_id':'u','timestamp':(time.time()-20)*1000,'bids':[],'asks':[]})
        self.assertTrue(e._clob_resync_needed);self.assertEqual(e.stale_book_messages,60)
        self.assertEqual(e.bbo['up']['ask'],.40)
        import io,json
        out=io.BytesIO();e.diagnostics.export(out)
        warnings=[json.loads(x) for x in out.getvalue().splitlines() if json.loads(x)['kind']=='STALE_BOOK_MESSAGE']
        self.assertEqual(len(warnings),1)

    async def test_dedicated_risk_worker_exits_without_strategy_loop(self):
        await self.e._momentum_enter(self.candidate())
        self.e.paper['position']['entryAt']-=31
        task=asyncio.create_task(self.e.position_worker())
        try:
            await asyncio.sleep(.15)
            self.assertIsNone(self.e.paper['position'])
            self.assertEqual(self.e.paper['trades'],1)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

if __name__=='__main__':unittest.main()
