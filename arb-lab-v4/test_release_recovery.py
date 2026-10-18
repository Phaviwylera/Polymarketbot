import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import httpx
from poly_portfolio import PaperPortfolio
from poly_server_demo import PolyServerDemo

class ReleaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_handover_keeps_known_paper_losses_and_local_state_wins(self):
        recovery=json.loads(Path(__file__).with_name('paper_recovery.json').read_text())
        # Extend only test expiry; the checkpoint itself remains unchanged.
        for a,s in recovery['assets'].items():s['paper']['endsAt']=time.time()+600
        with tempfile.TemporaryDirectory() as tmp:
            p=PaperPortfolio()
            for a,e in p.engines.items():
                e.state_file=Path(tmp)/(a+'.json')
                async def restore(e=e):self.assertTrue(e._restore_saved())
                e.start=restore
            local=p.engines['BTC'];local._new_session();local.paper['cash']=59;local._last_saved_at=0;local._save()
            response=httpx.Response(200,json={**recovery,'revision':'15.0-executable-momentum'},request=httpx.Request('GET','https://test'))
            client=AsyncMock();client.get.return_value=response;context=AsyncMock();context.__aenter__.return_value=client
            with patch('poly_portfolio.httpx.AsyncClient',return_value=context):await p.start()
            self.assertEqual(p.engines['BTC'].paper['cash'],59)
            self.assertAlmostEqual(p.engines['SOL'].paper['realizedPnl'],-.3567)
            self.assertAlmostEqual(p.engines['XRP'].paper['realizedPnl'],-.4619)
            self.assertAlmostEqual(p.snapshot()['portfolio']['totalPnl'],-1.8186)
            for e in p.engines.values():e.close()
    async def test_restore_keeps_original_reference_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=PolyServerDemo('ETH',80,state_file=Path(tmp)/'state.json');e._new_session();now=time.time()
            data={'asset':'ETH','serverTime':now-30,'paper':e.paper,'market':{'slug':'test','startTs':now-60,'endTs':now+240},'bbo':e.bbo,'spot':3000,'twap':3000,'spotAgeSec':1,'twapAgeSec':1}
            e.state_file.write_text(json.dumps(data));self.assertTrue(e._restore_saved())
            self.assertGreater(time.time()-e.spot_ts,30);self.assertGreater(time.time()-e.twap_ts,30);e.close()

if __name__=='__main__':unittest.main()
