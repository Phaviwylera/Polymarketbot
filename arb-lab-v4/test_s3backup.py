"""S3 backup against a real Postgres (POLY_TEST_PG_SRC, disposable) with an in-memory S3."""
import json,os,shutil,tempfile,unittest
import poly_s3backup as sb,poly_forensics as pf
from poly_storage import SCHEMA

SRC=os.getenv('POLY_TEST_PG_SRC')

class FakeS3:
    def __init__(self,tamper=False):self.objs={};self.tamper=tamper
    async def put(self,path,key,sha):
        d=tempfile.mkdtemp();dst=os.path.join(d,'o');shutil.copy(path,dst);self.objs[key]=(dst,sha)
    async def get(self,key,path):
        shutil.copy(self.objs[key][0],path)
        if self.tamper:
            with open(path,'ab') as f:f.write(b'x')

@unittest.skipUnless(SRC,'set POLY_TEST_PG_SRC to a disposable database')
class S3BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.c=await asyncpg.connect(SRC)
        await self.c.execute('DROP TABLE IF EXISTS poly_v161_state;DROP TABLE IF EXISTS poly_v161_events');await self.c.execute(SCHEMA)
        pfo={'portfolio':{'equity':193.5,'realizedPnl':-1.,'trades':4,'lifetimeRealizedPnl':-6.4,'lifetimeTrades':33,'epochStart':1.79e9,'riskLocked':False,'maxDrawdownUsd':6.98}}
        await self.c.execute("INSERT INTO poly_v161_state VALUES('paper',1,$1::jsonb),('edge',1,'{}'::jsonb)",json.dumps(pfo))
        await self.c.executemany('INSERT INTO poly_v161_events VALUES($1,$2,$3,$4,$5::jsonb)',
            [(f'e:{i}',1792800000.+i*.37,'BTC',('TRADE_ATTRIBUTION','DECISION_SAMPLE','THRESHOLD_SHADOW')[i%3],json.dumps({'i':i})) for i in range(5000)])
    async def asyncTearDown(self):await self.c.close()
    async def test_backup_verifies_and_restores(self):
        logs=[];s3=FakeS3();r=await sb.backup(SRC,s3,log=logs.append)
        self.assertTrue(r['ok'],r);self.assertEqual(r['eventRows'],5000);self.assertTrue(r['portfolioMatches'])
        self.assertEqual(r['restoreCheck']['paper'],'ok');self.assertTrue(r['key'].startswith('polybot/'))
        self.assertNotIn('postgres',logs[0].lower().replace('postgresql',''))
        header,kinds,states=sb.read_back(next(iter(s3.objs.values()))[0])
        self.assertEqual(header['schema'],'polybot-backup-v1');self.assertEqual(kinds['THRESHOLD_SHADOW']['rows'],1666)
        self.assertEqual(states['paper']['portfolio']['lifetimeTrades'],33)
    async def test_corrupted_download_fails_verification(self):
        r=await sb.backup(SRC,FakeS3(tamper=True),log=lambda *_:None)
        self.assertFalse(r['ok']);self.assertFalse(r['downloadedSha256Matches'])

class AlertTests(unittest.TestCase):
    def _alerts(self,st,now=2e9):
        return pf.alerts({'lanes':[],'storage':{'configured':True,**st}},{},{'clean':True},now=now)
    def test_backup_alerts(self):
        self.assertTrue(any('no verified backup' in a for a in self._alerts({'lastBackupAt':None})))
        self.assertTrue(any('FAILED' in a for a in self._alerts({'lastBackupAt':1.9e9,'lastBackupOk':False})))
        self.assertTrue(any('h old' in a for a in self._alerts({'lastBackupAt':2e9-40*3600,'lastBackupOk':True})))
        self.assertFalse(any('backup' in a for a in self._alerts({'lastBackupAt':2e9-3600,'lastBackupOk':True})))
