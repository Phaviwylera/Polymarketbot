import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from poly_server_demo import PolyServerDemo
from poly_portfolio import PaperPortfolio,ASSETS,TOTAL_BALANCE,ALLOCATIONS

class MultiAssetTests(unittest.IsolatedAsyncioTestCase):
    def lane(self,asset='ETH'):
        e=PolyServerDemo(asset,balance=5,state_file='/tmp/test-do-not-save.json');e._save=lambda:None;e._new_session();return e
    def test_total_capital_and_weighted_allocations(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        s=p.snapshot('ETH');self.assertEqual(s['asset'],'ETH');self.assertEqual(s['paper']['cash'],ALLOCATIONS['ETH'])
        self.assertEqual(s['portfolio']['equity'],TOTAL_BALANCE);self.assertEqual(len(s['portfolio']['assets']),4)
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
        p.engines['BTC'].paper['cash']=ALLOCATIONS['BTC']-4;p.engines['ETH'].paper['cash']=ALLOCATIONS['ETH']-3
        s=p.snapshot();self.assertTrue(s['portfolio']['riskLocked'])
        self.assertTrue(all(e.paper['status']=='risk_locked' for e in p.engines.values()))
        p.engines['BTC'].paper['cash']=ALLOCATIONS['BTC']
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
        e=p.engines['ETH15'];e.market={'slug':'eth-updown-15m-test'};inv=e._inventory_get();e._inventory_add_lot(inv,'down',5,.1,0,'MAKER')
        self.assertEqual((await p.reset())['status'],'blocked');self.assertTrue(e._inventory_has_residual())
    def test_all_asset_snapshot_has_no_circular_or_selected_only_data(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        s=p.snapshot(include_assets=True);json.dumps(s)
        self.assertEqual(set(s['assets']),set(ASSETS))
        self.assertEqual(sum(a['paper']['startingBalance'] for a in s['assets'].values()),TOTAL_BALANCE)
    def test_wallet_quote_is_funded_fee_aware_and_bounded(self):
        p=PaperPortfolio();e=p.engines['ETH'];e._save=lambda:None;e._new_session();now=time.time()
        e.market={'slug':'test','startTs':now-60,'endTs':now+240,'minOrderSize':5,'minTickSize':.01,'acceptingOrders':True}
        e.feed=dict.fromkeys(('market','clob','rtds'),'live');e.spot_ts=e.twap_ts=now;e.open_trusted=True
        e.bbo={'up':{'bid':.33,'ask':.34,'bidSize':10,'askSize':100},'down':{'bid':.66,'ask':.67,'bidSize':10,'askSize':100}}
        inv=e._inventory_get();candidate=e._inventory_first_leg_candidate(inv,{})
        self.assertIsNotNone(candidate);self.assertLessEqual(candidate['projected'],e.pair_target)
        self.assertLessEqual(candidate['shares']*candidate['price'],e.residual_cap)
        e._inventory_place_quote(inv,candidate['side'],candidate['price'],candidate['shares'],'FIRST')
        e.trade_prints.append({'t':time.time(),'receivedAt':time.time(),'aggressor':'SELL','marketSlug':'test','feedReady':True,'seq':1,'side':candidate['side'],'price':candidate['price']-.01,'size':.1})
        e._inventory_process_quote(inv,{})
        self.assertEqual(e.paper['inventoryFills'],0)
        e.trade_prints.append({'t':time.time(),'receivedAt':time.time(),'aggressor':'SELL','marketSlug':'test','feedReady':True,'seq':2,'side':candidate['side'],'price':candidate['price']-.01,'size':4.9})
        e._inventory_process_quote(inv,{})
        self.assertEqual(e.paper['inventoryFills'],1)
        self.assertGreaterEqual(e.paper['cash'],0)
        e.bbo['down']['askSize']=0;self.assertIsNone(e._inventory_first_leg_candidate(inv,{}))
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
