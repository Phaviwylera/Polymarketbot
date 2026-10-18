import asyncio,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import poly_server_demo as mod
from poly_momentum import signal

class MomentumExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=mod.PolyServerDemo('ETH',80,state_file=Path(self.tmp.name)/'state.json',risk_profile={'momentumEnabled':True,'sessionLossLimit':4})
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
        await self.e._momentum_enter(self.candidate());pos=dict(self.e.paper['position']);self.e.bbo['up'].update(bid=.48,bidSize=100)
        await self.e._momentum_manage(self.m)
        self.assertIsNone(self.e.paper['position']);expected=5*(.48-mod._fee_per_share(.48))-pos['entryTotalCost']
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
        c=self.candidate();await self.e._momentum_enter(c);self.e.bbo['up'].update(bid=.48,bidSize=100)
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

if __name__=='__main__':unittest.main()
