import asyncio,copy,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from poly_attribution import Attribution,closed_trade
from poly_storage import DurableStore,checkpoint,LOCK_ID
from poly_diagnostics import DiagnosticLog
from poly_portfolio import PaperPortfolio
from poly_server_demo import PolyServerDemo

class AccountingTests(unittest.TestCase):
    def test_closed_trade_reconciles_fees_slippage_and_stop_overshoot(self):
        pos={'shares':5,'entryPrice':.42,'entryFee':.08526,'entryTotalCost':2.18526,
             'entryAt':100,'side':'UP','marketSlug':'test','mode':'MOMENTUM_SCALP',
             'requestedAsk':.41,'stopLossUsd':.3,'entryLatencyMs':350}
        r=closed_trade('BTC','session',pos,.34,.07854,'net loss stop',110,400)
        self.assertAlmostEqual(r['grossPnlUsd'],-.4);self.assertAlmostEqual(r['feesUsd'],.1638)
        self.assertAlmostEqual(r['netPnlUsd'],-.5638);self.assertAlmostEqual(r['accountingErrorUsd'],0)
        self.assertAlmostEqual(r['stopOvershootUsd'],.2638);self.assertAlmostEqual(r['entrySlippageUsd'],.05)
        self.assertEqual(r['holdSec'],10)
        a=Attribution();a.add(r);a.add(r);self.assertEqual(a.summary()['total']['trades'],1)
        self.assertEqual(a.summary()['total']['profitFactor'],0)
    def test_checkpoint_excludes_raw_data_and_preserves_ledger(self):
        source={'revision':'16.1','serverTime':100,'portfolio':{'trades':12},'ledgerHistory':{'BTC':{}},
                'research':{'large':[]},'assets':{'BTC':{'asset':'BTC','paper':{'cash':59,'trades':3},'market':{'slug':'test'},'depth':{'large':[]}}}}
        out=checkpoint(source);self.assertEqual(out['assets']['BTC']['paper']['cash'],59)
        self.assertNotIn('research',out);self.assertNotIn('depth',out['assets']['BTC'])
        out['assets']['BTC']['paper']['cash']=0;self.assertEqual(source['assets']['BTC']['paper']['cash'],59)
    def test_journal_hook_has_stable_event_identity_and_immutable_copy(self):
        with tempfile.TemporaryDirectory() as t:
            e=SimpleNamespace(engines={});s=DurableStore(e,url='postgresql://unused')
            d=DiagnosticLog(str(Path(t)/'test.db'),'BTC');d.on_event=s.enqueue
            p={'netPnlUsd':1};d.append('TRADE_ATTRIBUTION',trade=p);p['netPnlUsd']=7
            row=s.queue[0];self.assertEqual(json.loads(row[-1])['trade']['netPnlUsd'],1)
            self.assertEqual(row[0],d.boot+':1');d.close()

class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_database_blocks_entries_until_fresh_commit(self):
        s=DurableStore(SimpleNamespace(engines={}),url='postgresql://unused')
        self.assertFalse(s.entry_allowed());s.owner=s.active=s.ready=True;s.last_commit=time.time()
        self.assertTrue(s.entry_allowed());s.last_commit-=11;self.assertTrue(s.entry_allowed());s.last_commit-=10;self.assertFalse(s.entry_allowed())
        s.last_commit=time.time();s.dropped=1;self.assertFalse(s.entry_allowed())
        self.assertNotIn('postgresql://unused',json.dumps(s.status()))
    async def test_local_mode_has_explicit_non_durable_status(self):
        e=SimpleNamespace(engines={},start=AsyncMock(),start_research=lambda:None,shutdown=AsyncMock())
        s=DurableStore(e,url='');await s.start();self.assertTrue(s.entry_allowed())
        self.assertEqual(s.status()['state'],'local_only');await s.close();e.shutdown.assert_awaited_once()
    async def test_failed_transaction_keeps_events_for_idempotent_retry(self):
        class Transaction:
            async def __aenter__(self):pass
            async def __aexit__(self,*args):return False
        conn=SimpleNamespace(transaction=lambda:Transaction(),executemany=AsyncMock(),execute=AsyncMock(side_effect=RuntimeError('failure')))
        s=DurableStore(SimpleNamespace(engines={},research=None,snapshot=lambda **k:{'assets':{}}),url='postgresql://unused');s.conn=conn;s.owner=True
        event={'bootId':'boot','journalSeq':1,'t':100,'kind':'SHADOW_OUTCOME','asset':'BTC','row':{'id':'one'}};s.enqueue(event)
        with self.assertRaises(RuntimeError):await s.flush()
        self.assertEqual(len(s.queue),1);self.assertEqual(s.last_commit,0)
        conn.execute=AsyncMock();await s.flush();self.assertEqual(len(s.queue),0)
        self.assertIn('ON CONFLICT(id) DO NOTHING',conn.executemany.call_args.args[0])
    async def test_storage_pause_does_not_permanently_risk_lock_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=PolyServerDemo('BTC',60,state_file=Path(tmp)/'state');e._new_session();e.operational_guard=lambda:False
            self.assertFalse(await e._momentum_enter({}));e._update_risk()
            self.assertEqual(e.paper['status'],'running');e.close()
    async def test_standby_serves_last_checkpoint_and_never_starts_engine(self):
        e=SimpleNamespace(engines={},start=AsyncMock())
        s=DurableStore(e,url='postgresql://unused');s.ready=True;s.standby={'assets':{'BTC':{'paper':{'cash':63}}},'portfolio':{'trades':9}}
        snap=s.snapshot(include_assets=True);self.assertEqual(snap['assets']['BTC']['paper']['cash'],63)
        self.assertEqual(snap['infrastructure']['state'],'standby');self.assertFalse(snap['infrastructure']['writer']);e.start.assert_not_awaited()

if __name__=='__main__':unittest.main()
