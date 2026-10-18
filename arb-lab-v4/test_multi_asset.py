import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from poly_server_demo import PolyServerDemo
from poly_portfolio import PaperPortfolio,ASSETS

class MultiAssetTests(unittest.IsolatedAsyncioTestCase):
    def lane(self,asset='ETH'):
        e=PolyServerDemo(asset,balance=5,state_file='/tmp/test-do-not-save.json');e._save=lambda:None;e._new_session();return e
    def test_total_capital_is_twenty_not_twenty_per_asset(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        s=p.snapshot('ETH');self.assertEqual(s['asset'],'ETH');self.assertEqual(s['paper']['cash'],5)
        self.assertEqual(s['portfolio']['equity'],20);self.assertEqual(len(s['portfolio']['assets']),4)
        self.assertEqual(len({e.state_file for e in p.engines.values()}),4)
        with self.assertRaises(ValueError):p.snapshot('DOGE')
    def test_asset_feed_cannot_contaminate_other_asset(self):
        e=self.lane('ETH');now=time.time()*1000
        e._handle_rtds({'topic':'crypto_prices_chainlink','payload':{'symbol':'btc/usd','value':80000,'timestamp':now}})
        self.assertIsNone(e.spot)
        e._handle_rtds({'topic':'crypto_prices_chainlink','payload':{'symbol':'eth/usd','value':3000,'timestamp':now}})
        self.assertEqual(e.spot,3000)
    async def test_discovery_uses_asset_slug_and_its_own_tokens(self):
        e=self.lane('SOL');seen=[]
        async def fetch(url,params=None,headers=None):
            seen.append((url,params))
            if url.endswith('/markets'):
                return [{'slug':params['slug'],'outcomes':'["Up","Down"]','clobTokenIds':'["sol-up","sol-down"]','active':True,'acceptingOrders':True}]
            return {'bids':[{'price':'.4','size':'10'}],'asks':[{'price':'.45','size':'10'}]}
        e._get=fetch;m=await e.fetch_current_market()
        self.assertTrue(m['slug'].startswith('sol-updown-5m-'));self.assertEqual(m['upToken'],'sol-up');self.assertEqual(m['asset'],'SOL')
    def test_portfolio_loss_locks_all_lanes_and_stays_locked(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        p.engines['BTC'].paper['cash']=4;p.engines['ETH'].paper['cash']=4.4
        s=p.snapshot();self.assertTrue(s['portfolio']['riskLocked'])
        self.assertTrue(all(e.paper['status']=='risk_locked' for e in p.engines.values()))
        p.engines['BTC'].paper['cash']=5
        self.assertFalse(p.entry_allowed())
    def test_asset_profiles_are_separate_and_fee_aware(self):
        b=self.lane('BTC');e=self.lane('ETH')
        self.assertNotEqual(b.pair_target,e.pair_target)
        self.assertEqual(e.session_loss_limit,.5)
        e.bbo['up']['ask']=.49;e.bbo['down']['ask']=.49
        self.assertGreater(e._pair_state()['takerPairCost'],1)
        self.assertFalse(e._pair_state()['directEligible'])
    async def test_unresolved_inventory_is_not_settled_from_price_tick(self):
        e=self.lane();now=time.time();e.market={'slug':'eth-test','startTs':now-300,'endTs':now}
        inv=e._inventory_get();e._inventory_add_lot(inv,'up',5,.1,0,'MAKER');e.twap=9000;e.open_ref=1000
        e._resolved_winner=AsyncMock(return_value=None)
        await e._settle_inventory('eth-test');self.assertTrue(e._inventory_has_residual());self.assertEqual(e.paper['realizedPnl'],0)
        self.assertEqual(inv['status'],'AWAITING_SETTLEMENT')
        e._resolved_winner=AsyncMock(return_value='down')
        await e._settle_inventory('eth-test');self.assertFalse(e._inventory_has_residual());self.assertAlmostEqual(e.paper['realizedPnl'],-.5)
    async def test_reset_does_not_erase_open_exposure(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        e=p.engines['XRP'];e.market={'slug':'xrp-test'};inv=e._inventory_get();e._inventory_add_lot(inv,'down',5,.1,0,'MAKER')
        self.assertEqual((await p.reset())['status'],'blocked');self.assertTrue(e._inventory_has_residual())
    async def test_directional_lag_entry_cannot_place_order(self):
        e=self.lane();await e._enter({'ask':.5},{},99)
        self.assertEqual(e.paper['cash'],5);self.assertEqual(e.paper['executionAttempts'],0)
        self.assertFalse(e.snapshot()['risk']['directionalLagEntriesEnabled'])
    async def test_resolution_requires_authoritative_closed_result(self):
        e=self.lane();e._get=AsyncMock(return_value=[{'closed':False,'outcomes':'["Up","Down"]','outcomePrices':'["1","0"]'}])
        self.assertIsNone(await e._resolved_winner('test'))
        e._get=AsyncMock(return_value=[{'closed':True,'umaResolutionStatus':'resolved','outcomes':'["Up","Down"]','outcomePrices':'["0","1"]'}])
        self.assertEqual(await e._resolved_winner('test'),'down')

if __name__=='__main__':unittest.main()
