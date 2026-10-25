"""Priority work 2026-10-25: experiment D (maker entry), E (learner gate), v17.9 gate, restart safety."""
import asyncio,os,tempfile,time,unittest
from unittest.mock import patch
import poly_experiments as px,poly_forensics as pf,poly_tape as pt,poly_strategy as ps
from test_stop_experiments import Lane

class ExperimentDTests(unittest.TestCase):
    def d(self):return {'id':'D','token':'up','marketSlug':'m','price':.39,'postedAt':100.,'latencySec':.35,'windowSec':2.,'state':'resting'}
    def test_fills_only_on_a_trade_through_after_latency_inside_window(self):
        d=self.d();px.d_on_print(d,{'t':100.1,'side':'up','marketSlug':'m','price':.38});self.assertEqual(d['state'],'resting')   # before latency
        px.d_on_print(d,{'t':100.5,'side':'up','marketSlug':'m','price':.39});self.assertEqual(d['state'],'resting')             # at our price: queue
        px.d_on_print(d,{'t':100.6,'side':'down','marketSlug':'m','price':.30});self.assertEqual(d['state'],'resting')           # other token
        px.d_on_print(d,{'t':100.7,'side':'up','marketSlug':'m','price':.38});self.assertEqual(d['state'],'filled')
        e=self.d();px.d_on_print(e,{'t':103.,'side':'up','marketSlug':'m','price':.30});self.assertEqual(e['state'],'expired')
    def test_finalize_shadow_pnl(self):
        d=self.d();d.update(state='filled');pos={'shares':5.,'entryTotalCost':5*(.40+px.fee(.40)),'experiments':{'D':d}}
        out=px.finalize(pos,real_net=.5)['D'];self.assertTrue(out['filled'])
        self.assertAlmostEqual(out['shadowNetPnlUsd'],.5+pos['entryTotalCost']-5*.39,5)    # same exit, cheaper zero-fee entry
        miss=self.d();pos2={'shares':5.,'entryTotalCost':2.,'experiments':{'D':miss}}
        o2=px.finalize(pos2,real_net=.5)['D'];self.assertFalse(o2['filled']);self.assertEqual(o2['shadowNetPnlUsd'],0.)
        self.assertEqual(miss['state'],'unfilled at real exit')

class LaneDETests(Lane):
    async def test_entry_records_d_and_e(self):
        self.assertTrue(await self.e._momentum_enter(self.candidate()))
        ex=self.e.paper['position']['experiments'];self.assertIn('D',ex);self.assertEqual(ex['D']['state'],'resting')
        self.assertLessEqual(ex['D']['price'],.39)
        self.assertNotIn('E',ex) if self.e.edge is None else self.assertIn('E',ex)

class GateTests(unittest.TestCase):
    def rows(self,n,pnl):return [{'entryAt':1.8e9+i*3600,'netPnlUsd':pnl(i),'exitPrice':.5,'configHash':'x','strategy':'MOMENTUM_SCALP'} for i in range(n)]
    def test_collecting_then_pass_or_fail(self):
        g=pf.evaluation_gate(self.rows(10,lambda i:.3),now=1.8e9+10*3600);self.assertEqual(g['status'],'COLLECTING');self.assertGreater(g['daysToSample'],0)
        g=pf.evaluation_gate(self.rows(70,lambda i:.4 if i%3 else -.2),now=1.8e9+70*3600);self.assertEqual(g['status'],'PASS',g['checks'])
        g=pf.evaluation_gate(self.rows(70,lambda i:.2 if i%2 else -.3),now=1.8e9+70*3600);self.assertEqual(g['status'],'FAIL')
    def test_version_report_has_gate_for_current_config_only(self):
        ident=ps.identity();rows=self.rows(3,lambda i:.1)
        for r in rows[:2]:r['configHash']=ident['configHash']
        v=pf.version_report(rows,ident);self.assertEqual(v['gate']['metrics']['trades'],2)

class TapeRestartTests(unittest.TestCase):
    def test_pending_rounds_and_totals_survive_a_restart(self):
        d=tempfile.mkdtemp();T=1.8e9;sig={'pUp':.7,'se':.03,'at':T-1}
        a=pt.Tape(root=os.path.join(d,'tape'),now=lambda:T)
        a.on_print('BTC',{'t':T,'side':'up','price':.55,'size':5,'marketSlug':'r1'},{'slug':'r1','endTs':T+60},sig);a.settle('r1','up')
        a.on_print('BTC',{'t':T,'side':'up','price':.55,'size':5,'marketSlug':'r2'},{'slug':'r2','endTs':T+60},sig)   # still open
        a.save_state()
        b=pt.Tape(root=os.path.join(d,'tape'),now=lambda:T+120);self.assertTrue(b.load_state())
        self.assertEqual(b.agg.n,1);self.assertIn('r2',b.pending);self.assertEqual(b.settle('r2','down'),1);self.assertEqual(b.agg.n,2)
    def test_stale_round_resolved_from_market_record(self):
        T=1.8e9;tape=pt.Tape(root=tempfile.mkdtemp(),now=lambda:T+2000)
        tape.on_print('ETH',{'t':T,'side':'down','price':.4,'size':5,'marketSlug':'old'},{'slug':'old','endTs':T+60},{'pUp':.5,'se':.03,'at':T})
        async def fetch(slug):return {'closed':True,'outcomes':'["Up","Down"]','outcomePrices':'["0","1"]'}
        self.assertEqual(asyncio.run(tape.resolve_stale(fetch)),1);self.assertNotIn('old',tape.pending)
        self.assertTrue(tape.recent[-1]['won'])                          # DOWN token, DOWN won
    def test_unresolved_market_stays_pending(self):
        T=1.8e9;tape=pt.Tape(root=tempfile.mkdtemp(),now=lambda:T+2000)
        tape.on_print('ETH',{'t':T,'side':'up','price':.4,'size':5,'marketSlug':'x'},{'slug':'x','endTs':T+60},{'pUp':.5,'se':.03,'at':T})
        async def fetch(slug):return {'closed':False,'outcomes':'["Up","Down"]','outcomePrices':'["0.5","0.5"]'}
        self.assertEqual(asyncio.run(tape.resolve_stale(fetch)),0);self.assertIn('x',tape.pending)

class BackupMarkerTests(unittest.TestCase):
    def test_latest_backup_uses_verified_markers_only(self):
        import poly_s3backup as sb
        class C:
            def list_objects_v2(self,**kw):return {'Contents':[{'Key':'polybot/2026-10-25/backup-1792932797.jsonl.gz'},{'Key':'polybot/2026-10-25/verified-1792931796.json'}]}
        s3=sb.S3('b',client=C());self.assertEqual(asyncio.run(s3.latest_backup_at()),1792931796)
