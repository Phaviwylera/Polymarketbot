import asyncio,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import poly_portfolio
from poly_portfolio import PaperPortfolio,TOTAL_BALANCE
from poly_server_demo import PolyServerDemo
from poly_scout import AssetScout

def lane(tmp,key='BTC15',iv=900):
    return PolyServerDemo('BTC',balance=30,interval=iv,lane=key,state_file=Path(tmp)/f'{key}.json')

class PausedLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_flat_lane_opens_no_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            e=lane(d);e.paused=True;e._save=lambda:None
            with patch.object(e,'_seed_sigma',AsyncMock()) as seed:
                await e.start()
            self.assertEqual(e.tasks,[]);self.assertFalse(seed.called)
            self.assertEqual(set(e.feed.values()),{'paused'});self.assertIsNotNone(e.paper)
    async def test_paused_lane_with_open_position_still_runs_exits(self):
        with tempfile.TemporaryDirectory() as d:
            e=lane(d);e.paused=True;e._save=lambda:None
            def restore():
                e._new_session();e.paper['position']={'side':'UP','shares':5};return True
            with patch.object(e,'_restore_saved',side_effect=restore),patch.object(e,'_seed_sigma',AsyncMock()),\
                 patch.object(PolyServerDemo,'market_worker',AsyncMock()),patch.object(PolyServerDemo,'clob_worker',AsyncMock()),\
                 patch.object(PolyServerDemo,'rtds_worker',AsyncMock()),patch.object(PolyServerDemo,'engine_worker',AsyncMock()),\
                 patch.object(PolyServerDemo,'position_worker',AsyncMock()):
                await e.start()
                self.assertEqual(len(e.tasks),5)
                await asyncio.gather(*e.tasks)
            self.assertEqual(e.paper['position']['side'],'UP')   # restored, not replaced by a new session
    def test_scout_measures_paused_lane_by_polling(self):
        with tempfile.TemporaryDirectory() as d:
            a=lane(d,'BTC',300);b=lane(d,'BTC15',900);b.paused=True;b.tasks=[]
            s=AssetScout({'BTC':a,'BTC15':b})
            self.assertEqual(s.active_keys(),{'btc-5m'})

class AccountEquityTests(unittest.TestCase):
    def test_headline_counts_earlier_sessions_since_fresh_start(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        p.epoch=time.time()-3600
        p.ledger_history['BTC']['old_in_epoch']={'sessionId':'old_in_epoch','startedAt':p.epoch+10,'realizedPnl':-3.0,'trades':7}
        p.ledger_history['ETH']['before_epoch']={'sessionId':'before_epoch','startedAt':p.epoch-9999,'realizedPnl':15.0,'trades':100}
        live=p.engines['ETH'].paper['sessionId']   # a running session that also sits in history must not count twice
        p.ledger_history['ETH'][live]={'sessionId':live,'startedAt':time.time(),'realizedPnl':-50.0,'trades':9}
        pf=p.snapshot()['portfolio']
        self.assertAlmostEqual(pf['equity'],TOTAL_BALANCE)          # per-session value kept for risk limits
        self.assertAlmostEqual(pf['accountEquity'],TOTAL_BALANCE-3.0)
        self.assertAlmostEqual(pf['accountPnl'],-3.0)
        self.assertEqual(pf['lifetimeTrades'],7);self.assertEqual(pf['archivedTrades'],100)

class LaneSwitchTests(unittest.TestCase):
    def test_paused_lanes_from_environment(self):
        with patch.object(poly_portfolio,'PAUSED_LANES',frozenset({'BTC15','ETH15'})):
            p=PaperPortfolio()
        self.assertEqual({k for k,e in p.engines.items() if e.paused},{'BTC15','ETH15'})
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        self.assertEqual(p.snapshot()['portfolio']['pausedLanes'],['BTC15','ETH15'])

class ResearchStatsCacheTests(unittest.TestCase):
    def test_counts_refresh_when_labels_change(self):
        from poly_shadow import ResearchHub
        with tempfile.TemporaryDirectory() as d:
            h=ResearchHub({},str(Path(d)/'r.sqlite3'))
            for i in range(3):h.rows.append({'id':str(i),'roundStart':i//2,'labelStatus':'observed'})
            self.assertEqual(h._row_stats()[1],2)
            h.rows.append({'id':'3','roundStart':9,'labelStatus':'censored_loss'})
            counts,rounds=h._row_stats();self.assertEqual(rounds,3);self.assertEqual(counts['censored_loss'],1)

class HttpClientReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_keepalive_client_per_loop(self):
        with tempfile.TemporaryDirectory() as d:
            e=lane(d);c1=e._http();c2=e._http()
            self.assertIs(c1,c2);await c1.aclose()
            self.assertIsNot(e._http(),c1)   # a closed client is replaced
            await e._http().aclose()

if __name__=='__main__':unittest.main()
