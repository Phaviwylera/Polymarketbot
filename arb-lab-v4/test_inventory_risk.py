import asyncio
import time
import unittest
from unittest.mock import patch
import poly_server_demo as mod

class InventoryRiskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.e=mod.PolyServerDemo();self.e._save=lambda:None
        self.e._new_session();now=time.time()
        self.e.market={'slug':'test','startTs':now-60,'endTs':now+240,'minOrderSize':5,'minTickSize':.01,'acceptingOrders':True}
        self.e.feed=dict.fromkeys(('market','clob','rtds'),'live')
        self.e.spot_ts=self.e.twap_ts=now;self.e.open_trusted=True
        self.e.bbo={'up':{'bid':.09,'ask':.10,'bidSize':100,'askSize':100},'down':{'bid':.86,'ask':.87,'bidSize':100,'askSize':100}}
        self.inv=self.e._inventory_get();self.m={'leadBps':1,'impulse5Bps':1}
        self.latency=patch.object(mod,'EXECUTION_LATENCY_SEC',0);self.latency.start();self.addCleanup(self.latency.stop)
    def lot(self,price=.09,age=0):
        self.e._inventory_add_lot(self.inv,'up',5,price,0,'MAKER')
        self.inv['upLots'][0]['t']-=age
    def quote(self):
        self.inv['quote']={'kind':'COMPLEMENT','side':'down','price':.78,'shares':5,'createdAt':time.time(),'queueNeed':1,'seen':100,'lastTradeSeq':0}
    def test_reject_expensive_first_leg_from_diagnostic(self):
        self.e.bbo['up'].update(bid=.20,ask=.21)
        self.assertIsNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
    def test_requires_profitable_hedge_and_cash_and_size(self):
        self.assertIsNotNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
        self.e.bbo['down']['ask']=.94
        self.assertIsNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
        self.e.bbo['down'].update(ask=.87,askSize=4)
        self.assertIsNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
        self.e.bbo['down']['askSize']=100;self.e.paper['cash']=1
        self.assertIsNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
    def test_orphan_quote_cannot_reopen_exposure(self):
        self.quote();self.e._inventory_process_quote(self.inv,self.m)
        self.assertIsNone(self.inv['quote']);self.assertEqual(self.e.paper['cash'],20)
    async def test_taker_merge_cancels_complement(self):
        self.lot();self.quote()
        self.assertTrue(await self.e._inventory_taker_complement(self.inv,'up',.09))
        self.assertIsNone(self.inv['quote']);self.assertFalse(self.e._inventory_has_residual())
        self.assertAlmostEqual(self.e.paper['cash'],20+self.e.paper['realizedPnl'])
    async def test_recovery_cancels_complement(self):
        self.lot();self.quote();self.e.bbo['up']['bid']=.15
        self.assertTrue(await self.e._inventory_recovery_exit(self.inv,'up'))
        self.assertIsNone(self.inv['quote']);self.assertFalse(self.e._inventory_has_residual())
    async def test_timed_exit_takes_small_loss_instead_of_waiting(self):
        self.lot(age=16);self.quote();self.inv['quote']['seen']=0;self.e.bbo['down']['ask']=.99;self.e.bbo['up']['bid']=.08
        await self.e._inventory_step(self.m)
        self.assertFalse(self.e._inventory_has_residual());self.assertEqual(self.e.paper['losses'],1)
        self.assertGreater(self.e.paper['realizedPnl'],-.45)
        self.assertGreater(self.e.paper['cooldownUntil'],time.time());self.assertIsNone(self.inv['quote'])
    async def test_no_liquidity_does_not_invent_exit(self):
        self.lot(age=16);self.e.bbo['down']['ask']=.99;self.e.bbo['up']['bidSize']=0
        await self.e._inventory_step(self.m)
        self.assertTrue(self.e._inventory_has_residual());self.assertEqual(self.inv['status'],'EXIT_PENDING')
        self.assertEqual(self.e.paper['trades'],0)
    async def test_market_rollover_aborts_delayed_exit(self):
        self.lot();self.e.bbo['up']['bid']=.15
        async def rollover(_):self.e.market_version+=1
        with patch.object(mod.asyncio,'sleep',rollover):
            self.assertFalse(await self.e._inventory_recovery_exit(self.inv,'up'))
        self.assertTrue(self.e._inventory_has_residual());self.assertFalse(self.e.pending)
    def test_drawdown_units_and_marked_loss_lock(self):
        self.e.paper['cash']=18
        snap=self.e.snapshot()['paper']
        self.assertEqual(snap['maxDrawdownUsd'],2);self.assertAlmostEqual(snap['maxDrawdownPct'],10)
        self.assertEqual(snap['status'],'risk_locked')
    async def test_stale_feed_cancels_without_fill(self):
        self.quote();self.e.spot_ts=0
        await self.e._inventory_step(self.m)
        self.assertIsNone(self.inv['quote']);self.assertEqual(self.e.paper['inventoryFills'],0)
    async def test_price_stop_exits_before_timeout(self):
        self.lot();self.e.bbo['up']['bid']=.04;self.e.bbo['down']['ask']=.99
        await self.e._inventory_step(self.m)
        self.assertFalse(self.e._inventory_has_residual());self.assertEqual(self.e.paper['losses'],1)
    def test_unsettled_old_inventory_is_preserved(self):
        self.lot();self.e.market['slug']='next'
        self.assertIsNone(self.e._inventory_get());self.assertTrue(self.e._inventory_has_residual())
        self.assertEqual(self.e.paper['status'],'risk_locked')
    def test_cooldown_prevents_entry(self):
        self.e.paper['cooldownUntil']=time.time()+90
        self.assertIsNone(self.e._inventory_first_leg_candidate(self.inv,self.m))
    async def test_direct_pair_respects_lock(self):
        self.e.paper['status']='risk_locked'
        self.assertFalse(await self.e._try_direct_pair({'directEligible':True}))

if __name__=='__main__':unittest.main()
