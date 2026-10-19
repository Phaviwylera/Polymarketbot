import asyncio,io,json,tempfile,time,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import poly_audit_export as audit
from poly_diagnostics import DiagnosticLog

class DiagnosticDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=SimpleNamespace(engines={},snapshot=lambda **kw:{'revision':'test','assets':{a:{} for a in ('BTC','ETH','SOL','XRP')}})
        for asset in ('BTC','ETH','SOL','XRP'):
            log=DiagnosticLog(str(Path(self.tmp.name)/(asset+'.sqlite3')),asset);self.addCleanup(log.db.close)
            log.append('TEST',value=asset);self.engine.engines[asset]=SimpleNamespace(diagnostics=log)
        self.manager=audit.DiagnosticDownloads()
    async def asyncTearDown(self):
        if self.manager.task:await self.manager.task
        for item in self.manager.jobs.values():
            if item.get('path'):Path(item['path']).unlink(missing_ok=True)
    async def test_repeated_preparation_shares_one_job_and_keeps_loop_responsive(self):
        original=audit._build_archive;calls=[]
        def slow(*args):
            calls.append(1);time.sleep(.1);return original(*args)
        with patch.object(audit,'_build_archive',side_effect=slow):
            first=self.manager.status(self.engine,True)
            second=self.manager.status(self.engine,True)
            self.assertEqual(first['job'],second['job']);self.assertEqual(first['state'],'preparing')
            await asyncio.sleep(.02);self.assertFalse(self.manager.task.done())
            await self.manager.task
        self.assertEqual(len(calls),1)
        ready=self.manager.status(self.engine,True);self.assertEqual(ready['job'],first['job']);self.assertEqual(ready['state'],'ready')
    async def test_real_http_prepare_poll_native_download_and_cors(self):
        import poly_engine_service as service
        with patch.object(service,'engine',self.engine),patch.object(audit,'downloads',self.manager):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),base_url='https://test') as client:
                first=await client.get('/api/poly/demo/diagnostics/status?prepare=true',headers={'Origin':'https://arb-lab-v4-unified.onrender.com'})
                self.assertEqual(first.status_code,200);self.assertEqual(first.json()['state'],'preparing')
                await self.manager.task
                ready=(await client.get('/api/poly/demo/diagnostics/status',params={'job':first.json()['job']})).json()
                r=await client.get(ready['downloadUrl'])
                self.assertEqual(r.status_code,200);self.assertIn('attachment;',r.headers['content-disposition'])
                self.assertEqual(len(r.content),int(r.headers['content-length']))
                z=zipfile.ZipFile(io.BytesIO(r.content));self.assertEqual(len(z.namelist()),11)
                self.assertIn('pair-shadow.json',z.namelist());self.assertIn('research-quality.json',z.namelist())
                self.assertEqual(set(json.loads(z.read('manifest.json'))['assets']),set(self.engine.engines))
                self.assertIn(first.headers['access-control-allow-origin'],('*','https://arb-lab-v4-unified.onrender.com'))
    async def test_unprepared_download_is_202_and_failure_can_be_retried(self):
        with patch.object(audit,'_build_archive',side_effect=RuntimeError('test')):
            r=self.manager.response(self.engine);self.assertEqual(r.status_code,202)
            await self.manager.task
        failed=self.manager.status(self.engine);self.assertEqual(failed['state'],'failed')
        retry=self.manager.status(self.engine,True);self.assertNotEqual(failed['job'],retry['job'])
        await self.manager.task;self.assertEqual(self.manager.status(self.engine)['state'],'ready')

if __name__=='__main__':unittest.main()
