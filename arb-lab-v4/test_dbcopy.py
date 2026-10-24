"""Backup/migration copy against a real Postgres. Runs only when POLY_TEST_PG_SRC and POLY_TEST_PG_DST
point at two disposable databases (never production); otherwise skipped."""
import json,os,unittest
import poly_dbcopy
from poly_storage import SCHEMA

SRC=os.getenv('POLY_TEST_PG_SRC');DST=os.getenv('POLY_TEST_PG_DST')

@unittest.skipUnless(SRC and DST,'set POLY_TEST_PG_SRC/POLY_TEST_PG_DST to disposable databases')
class CopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.src=await asyncpg.connect(SRC);self.dst=await asyncpg.connect(DST)
        for c in (self.src,self.dst):
            await c.execute('DROP TABLE IF EXISTS poly_v161_state;DROP TABLE IF EXISTS poly_v161_events');await c.execute(SCHEMA)
        pf={'portfolio':{'equity':195.89,'realizedPnl':-.3,'trades':3,'lifetimeRealizedPnl':-4.11,'lifetimeTrades':25,'epochStart':1792782893.,'riskLocked':False,'maxDrawdownUsd':3.33}}
        await self.src.execute("INSERT INTO poly_v161_state VALUES('paper',1,$1::jsonb),('edge',1,'{}'::jsonb)",json.dumps(pf))
        await self.src.executemany('INSERT INTO poly_v161_events VALUES($1,$2,$3,$4,$5::jsonb)',
            [(f'b:{i}',1000.+i,'BTC','TRADE_ATTRIBUTION' if i%3==0 else 'SHADOW_OUTCOME',json.dumps({'i':i,'row':{'id':str(i)}})) for i in range(4500)])
    async def asyncTearDown(self):
        await self.src.close();await self.dst.close()
    async def test_copy_verifies_counts_checksums_portfolio_and_restore(self):
        logs=[];r=await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=logs.append)
        self.assertTrue(r['ok'],r);self.assertEqual(r['eventRows'],4500);self.assertEqual(r['eventRowsTarget'],4500)
        self.assertTrue(r['portfolioMatches']);self.assertEqual(r['restoreCheck']['TRADE_ATTRIBUTION'],1500)
        self.assertNotIn('postgres',logs[0].lower().replace('postgresql',''))   # no connection string in the log line
        # incremental: a second run copies nothing new and stays verified
        r2=await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None);self.assertTrue(r2['ok']);self.assertEqual(r2['eventRowsTarget'],4500)
    async def test_detects_a_target_that_diverged(self):
        await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None)
        await self.dst.execute("DELETE FROM poly_v161_events WHERE id='b:3'")
        await self.dst.execute("UPDATE poly_v161_events SET t=t+1 WHERE id='b:4'")
        a=await poly_dbcopy._summary(self.src);b=await poly_dbcopy._summary(self.dst)
        self.assertNotEqual(a['events'],b['events'])
        # the next copy restores the missing row but cannot repair a changed one: verification fails loudly
        r=await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None);self.assertFalse(r['ok']);self.assertIn('SHADOW_OUTCOME',r['mismatchedKinds'])

    async def test_epoch_timestamps_and_rows_written_during_the_copy(self):
        await self.src.execute('DELETE FROM poly_v161_events')
        await self.src.executemany('INSERT INTO poly_v161_events VALUES($1,$2,$3,$4,$5::jsonb)',
            [(f'e:{i}',1792882228.3167875+i*0.0137,'ETH','DECISION_SAMPLE','{}') for i in range(30000)])
        r=await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None);self.assertTrue(r['ok'],r['mismatchedKinds'])
        # a row the live bot writes after the copy's horizon does not fail verification of what was copied
        await self.src.execute("INSERT INTO poly_v161_events VALUES('late',1792999999,'ETH','DECISION_SAMPLE','{}')")
        a=await poly_dbcopy._summary(self.src,r['verifiedUpToT']);b=await poly_dbcopy._summary(self.dst,r['verifiedUpToT'])
        self.assertEqual(a['events'],b['events'])

    async def test_source_retention_pruning_is_mirrored(self):
        await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None)
        await self.src.execute("DELETE FROM poly_v161_events WHERE kind='SHADOW_OUTCOME' AND t<1100")   # the bot's prune
        r=await poly_dbcopy.copy_database(SRC,DST,SCHEMA,log=lambda *_:None)
        self.assertTrue(r['ok'],r['mismatchedKinds']);self.assertGreater(r['prunedToMatchSource'],0)

if __name__=='__main__':unittest.main()
