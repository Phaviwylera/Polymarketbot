import copy,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from poly_portfolio import PaperPortfolio
from poly_storage import DurableStore
import poly_engine_service as service

class RestartRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=PaperPortfolio();self.p.data_root=Path(self.tmp.name)
        for a,e in self.p.engines.items():
            e.state_file=Path(self.tmp.name)/(a+'.json');e._new_session();e.paper['endsAt']=time.time()-1;e.paper['status']='complete'
        self.storage=DurableStore(self.p,url='')
        self.old={a:copy.deepcopy(e.paper) for a,e in self.p.engines.items()}
    async def asyncTearDown(self):
        for e in self.p.engines.values():e.close()
        self.tmp.cleanup()
    async def post(self):
        with patch.object(service,'engine',self.p),patch.object(service,'storage',self.storage):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),base_url='https://test') as c:
                return await c.post('/api/poly/demo/server/reset')
    async def test_restart_endpoint_archives_results_and_rearms_all_four_lanes(self):
        e=self.p.engines['BTC'];e.paper.update(realizedPnl=2,cash=62,trades=2,consecutiveLosses=2,riskLockReason='two consecutive losses',status='risk_locked')
        self.p.started=True;config=self.p.get_config();r=await self.post();self.assertEqual(r.status_code,200);s=r.json()
        self.assertEqual(set(s['assets']),{'BTC','ETH','SOL','XRP'});self.assertEqual(s['portfolio']['lifetimeRealizedPnl'],2)
        self.assertEqual(s['portfolio']['priorSessionRealizedPnl'],2);self.assertEqual(s['portfolio']['lifetimeTrades'],2)
        for a,lane in s['assets'].items():
            p=lane['paper'];self.assertNotEqual(p['sessionId'],self.old[a]['sessionId']);self.assertEqual(p['status'],'running');self.assertIsNone(p['riskLockReason']);self.assertGreater(p['endsAt'],time.time()+10790)
            self.assertEqual(p['trades'],0);self.assertEqual(p['cash'],p['startingBalance'])
        self.assertEqual(self.p.get_config(),config)
    async def test_open_exposure_returns_409_and_does_not_erase_session(self):
        e=self.p.engines['BTC'];e.pending=True;r=await self.post();self.assertEqual(r.status_code,409)
        self.assertEqual(r.json()['status'],'blocked');self.assertEqual(e.paper,self.old['BTC'])
    async def test_database_handover_returns_409_and_cannot_clear_lock(self):
        self.storage.url='postgresql://unused';r=await self.post();self.assertEqual(r.status_code,409)
        self.assertIn('handover',r.json()['detail']);self.assertEqual(self.p.engines['BTC'].paper,self.old['BTC'])

if __name__=='__main__':unittest.main()
