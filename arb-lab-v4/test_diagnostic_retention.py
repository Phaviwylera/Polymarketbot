import io,json,tempfile,unittest
from pathlib import Path
from poly_diagnostics import DiagnosticLog

class DiagnosticRetentionTests(unittest.TestCase):
    def test_raw_flood_cannot_prune_state_or_trade_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=DiagnosticLog(str(Path(tmp)/'log.db'),'BTC')
            self.addCleanup(log.close);log.MAX_ROWS=10
            log.append('ENGINE_EVENT',event={'action':'BUY'});log.append('STATE',context={'position':'open'})
            for i in range(2000):log.append('CLOB_MESSAGE',n=i)
            out=io.BytesIO();meta=log.export(out);rows=[json.loads(x) for x in out.getvalue().splitlines()]
            self.assertTrue(any(x['kind']=='ENGINE_EVENT' for x in rows));self.assertEqual(meta['retainedStateRows'],1)
            self.assertLessEqual(meta['retainedRawRows'],12);self.assertFalse(meta['complete'])
    def test_export_snapshot_cannot_lose_rows_to_later_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=DiagnosticLog(str(Path(tmp)/'log.db'),'ETH');self.addCleanup(log.close);log.MAX_ROWS=10
            for i in range(20):log.append('CLOB_MESSAGE',n=i)
            captured=log.capture()
            for i in range(2000):log.append('CLOB_MESSAGE',n=i+20)
            out=io.BytesIO();meta=log.export(out,captured)
            rows=[json.loads(x) for x in out.getvalue().splitlines()]
            self.assertEqual([x['n'] for x in rows],list(range(20)));self.assertTrue(meta['complete'])
            self.assertFalse(log._captures)
