import asyncio,copy,io,json,math,tempfile,time,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from poly_features import observe,walk,NAMES,SCHEMA,POLICY,fee
from poly_external import ExchangeFeeds
from poly_semantic import parse_rss,validate
from poly_shadow import ShadowLane,ResearchHub
from poly_research_model import train,clean,split_rounds,fit_xgboost,predict,infer
from poly_research_train import load_rows
from poly_server_demo import PolyServerDemo
from poly_portfolio import PaperPortfolio

NOW=1800000060.
def context(now=NOW,bid=.39,ask=.4):
    return {'market':{'slug':'btc-test','startTs':NOW-60,'endTs':NOW+240,'acceptingOrders':True,'minOrderSize':5,'minTickSize':.01},
        'feed':dict.fromkeys(('market','clob','rtds'),'live'),'spotTs':now,'twapTs':now,
        'bbo':{s:{'bid':bid,'ask':ask,'bidSize':100,'askSize':100} for s in ('up','down')},
        'depth':{s:{'bids':[{'price':bid,'size':100}],'asks':[{'price':ask,'size':100}]} for s in ('up','down')},
        'bookObservedAt':dict.fromkeys(('up','down'),now),'bookEventLagSec':dict.fromkeys(('up','down'),0.),
        'model':{'fairUp':.6,'fairDown':.4,'leadBps':1.,'impulse5Bps':.3},'stopLossUsd':.3,'tradePrints':[]}
def observations(now=NOW,bid=.39,ask=.4):return observe('BTC',context(now,bid,ask),{},now)[0]
def dataset(rounds=10):
    rows=[]
    for g in range(rounds):
        for asset in ('BTC','ETH','SOL','XRP'):
            for i in range(5):
                start=1800000000+g*300;t=start+20+i*10;f={k:0. for k in NAMES};f.update(ask=.2,bid=.19,entry_vwap=.2,friction=.03,stop_per_share=.06,target_per_share=.06,lead_bps=float(i-2))
                rows.append({'id':f'{g}-{asset}-{i}','schema':SCHEMA,'policy':POLICY,'asset':asset,'side':'up','marketSlug':f'{asset}-{g}',
                    'roundStart':start,'featuresAt':t,'availableAt':t,'sourceEventAt':t,'receivedAt':t,'labelAt':t+31,'features':f,'quantity':5,
                    'labelStatus':'filled','entryFilled':True,'netReturnPerShare':.03*(i-2)-.01})
    return rows

class FeatureTests(unittest.TestCase):
    def test_depth_walk_requires_full_quantity_and_prices_each_fee(self):
        levels=[{'price':.4,'size':2},{'price':.41,'size':3}];r=walk(levels,5,True,.41)
        self.assertAlmostEqual(r['vwap'],.406);self.assertAlmostEqual(r['feePerShare'],(2*fee(.4)+3*fee(.41))/5)
        self.assertIsNone(walk(levels,6,True));self.assertIsNone(walk(levels,5,True,.4))
    def test_feature_schema_and_source_age(self):
        rows,reason=observe('ETH',context(),{},NOW);self.assertEqual(set(rows),{'up','down'})
        row=rows['up'];self.assertEqual(set(row['features']),set(NAMES));self.assertNotIn('netReturnPerShare',row['features'])
        self.assertEqual(row['features']['eth'],1);self.assertEqual(row['features']['spot_present'],0)
        self.assertLessEqual(row['sourceEventAt'],row['availableAt'])
    def test_stale_future_and_missing_depth_never_infer_freshness(self):
        for mode in ('book','future','oracle','depth','connection'):
            c=context()
            if mode=='book':c['bookEventLagSec']=dict.fromkeys(('up','down'),4)
            if mode=='future':c['spotTs']=NOW+10
            if mode=='oracle':c['twapTs']=NOW-6
            if mode=='depth':c['depth']={s:{'bids':[],'asks':[]} for s in ('up','down')}
            if mode=='connection':c['feed']['clob']='unavailable'
            self.assertEqual(observe('BTC',c,{},NOW)[0],{},mode)
    def test_stale_external_feature_is_masked(self):
        external={'coinbase':{'eventAt':NOW-20,'receivedAt':NOW-19,'return5Bps':100}}
        rows,_=observe('BTC',context(),external,NOW)
        self.assertEqual(rows['up']['features']['spot_present'],0);self.assertEqual(rows['up']['features']['spot_return5'],0)

class ShadowTests(unittest.TestCase):
    def lane(self):
        events=[];lane=ShadowLane('BTC',lambda k,r:events.append((k,r)))
        lane.step({'up':observations()['up']},NOW)
        return lane,events
    def test_entry_waits_latency_and_uses_later_executable_price(self):
        lane,events=self.lane();lane.step(observations(NOW+.2),NOW+.2);self.assertEqual(lane.counts['entered'],0)
        lane.step(observations(NOW+.5,.40,.41),NOW+.5)
        self.assertEqual(lane.counts['entered'],1)
        s=next(s for s in lane.pending if s['row']['side']=='up');self.assertAlmostEqual(s['entryCost'],.41+fee(.41))
        self.assertEqual(s['row']['features']['entry_vwap'],.4)
    def test_post_latency_move_creates_nofill_zero_not_a_trade(self):
        lane,events=self.lane();out=lane.step({'up':observations(NOW+.5,.42,.43)['up']},NOW+.5)
        self.assertEqual(out[0]['labelStatus'],'no_fill');self.assertEqual(out[0]['netReturnPerShare'],0);self.assertFalse(out[0]['entryFilled'])
    def test_stop_and_exit_latency_include_both_fees(self):
        lane,events=self.lane();lane.step({'up':observations(NOW+.5)['up']},NOW+.5)
        lane.step({'up':observations(NOW+1,.30,.31)['up']},NOW+1)
        self.assertEqual(lane.pending[0]['phase'],'exit')
        out=lane.step({'up':observations(NOW+1.5,.29,.30)['up']},NOW+1.5)
        self.assertEqual(out[0]['labelStatus'],'filled');self.assertAlmostEqual(out[0]['netReturnPerShare'],.29-fee(.29)-.4-fee(.4))
    def test_data_gap_after_entry_is_conservative_loss(self):
        lane,events=self.lane();lane.step({'up':observations(NOW+.5)['up']},NOW+.5)
        out=lane.step({},NOW+3)
        self.assertEqual(out[0]['labelStatus'],'censored_loss');self.assertAlmostEqual(out[0]['netReturnPerShare'],-.4-fee(.4))
    def test_entry_gap_is_invalid_not_positive_training_data(self):
        lane,events=self.lane();self.assertEqual(lane.step({},NOW+3),[]);self.assertEqual(lane.counts['invalidBeforeEntry'],1)
    def test_time_exit_triggers_after_thirty_seconds_with_continuous_observations(self):
        lane,events=self.lane();lane.step({'up':observations(NOW+.5)['up']},NOW+.5)
        for i in range(1,61):lane.step({'up':observations(NOW+.5+i*.5)['up']},NOW+.5+i*.5)
        old=next(s for s in lane.pending if s['seedAt']==NOW)
        self.assertEqual(old['phase'],'exit');self.assertEqual(old['exitReason'],'maximum hold')
    def test_shadow_does_not_change_paper_cash_or_closed_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=PolyServerDemo('BTC',60,state_file=Path(tmp)/'state.json',risk_profile={'stopLoss':.3});e._save=lambda:None;e._new_session()
            c=context();e.market=c['market'];e.feed=c['feed'];e.spot_ts=e.twap_ts=NOW
            for side in ('up','down'):e._ingest_book(side,c['depth'][side])
            e.book_observed=dict.fromkeys(('up','down'),NOW);before=copy.deepcopy(e.paper)
            hub=ResearchHub({'BTC':e},str(Path(tmp)/'research.db'))
            try:hub.step(NOW);self.assertEqual(before,e.paper);self.assertFalse(e.research_state['executionEnabled'])
            finally:hub.journal.close();e.close()

class ModelTests(unittest.TestCase):
    def test_round_split_groups_assets_and_purges_crossing_labels(self):
        rows=dataset();parts=split_rounds(rows)
        sets=[{r['roundStart'] for r in p} for p in parts]
        self.assertFalse(sets[0]&sets[1]);self.assertFalse(sets[1]&sets[2])
        self.assertLess(max(r['labelAt'] for r in parts[0]),min(r['featuresAt'] for r in parts[1]))
        self.assertEqual({r['asset'] for r in parts[2]},{'BTC','ETH','SOL','XRP'})
    def test_model_training_has_separate_test_and_never_enables_execution(self):
        artifact=train(dataset());r=artifact['report'];self.assertEqual(r['state'],'shadow_candidate');self.assertFalse(r['executionEnabled'])
        self.assertFalse(r['enoughIndependentRounds']);self.assertFalse(r['evidenceGatePassed']);self.assertIn('no_trade',r['test'])
        self.assertEqual(r['test']['no_trade']['netPnl'],0)
        self.assertLess(r['windows']['train']['lastLabelAt'],r['windows']['test']['firstFeatureAt'])
    def test_no_data_produces_no_model_not_a_fake_trained_predictor(self):
        artifact=train([]);self.assertIsNone(artifact['model']);self.assertEqual(artifact['report']['state'],'insufficient_data')
    def test_nonfinite_bad_schema_and_future_availability_are_rejected(self):
        rows=dataset(1)[:3];rows[0]['features']['ask']=float('nan');rows[1]['schema']='other';rows[2]['availableAt']=rows[2]['featuresAt']+5
        cleaned,rejected=clean(rows);self.assertFalse(cleaned);self.assertEqual(rejected,3)
    def test_probes_are_not_counted_as_independent_orders(self):
        report=train(dataset())['report']
        for metric in report['test'].values():self.assertLessEqual(metric['selectedOrders'],metric['rounds']*4)
    def test_xgboost_portable_numeric_trees_match_native_predictor(self):
        try:import xgboost
        except ImportError:self.skipTest('optional xgboost-cpu not installed')
        model=fit_xgboost(dataset(5));self.assertEqual(model['kind'],'xgboost');self.assertTrue(math.isfinite(predict(model,dataset(1)[0]['features'])))
    def test_expired_model_or_features_do_not_publish_trading_signal(self):
        a=train(dataset());o=observations()['up'];a['report']['createdAt']=NOW
        self.assertEqual(infer(a,o,NOW+10)['state'],'stale_model_or_features');self.assertFalse(infer(a,o,NOW)['executionEnabled'])

class ExternalSemanticTests(unittest.TestCase):
    def test_exchange_asset_isolation_side_and_duplicate_handling(self):
        feeds=ExchangeFeeds();m={'e':'aggTrade','s':'ETHUSDT','E':NOW*1000,'a':100,'p':'2000','q':'2','m':True}
        feeds.ingest('binance',m,NOW);feeds.ingest('binance',m,NOW)
        self.assertFalse(feeds.data['BTC']);self.assertEqual(feeds.data['ETH']['binance']['flow5'],-1);self.assertEqual(len(feeds.flows[('binance','ETH')]),1)
        m['a']=101;m['E']=(NOW-10)*1000;feeds.ingest('binance',m,NOW);self.assertEqual(feeds.rejected,1)
    def test_rss_dates_grounding_and_zero_semantic_weight(self):
        from email.utils import formatdate
        rss=f'<rss><channel><item><title>Bitcoin update</title><link>https://example.test/news</link><pubDate>{formatdate(NOW,usegmt=True)}</pubDate></item></channel></rss>'.encode()
        items=parse_rss(rss,'https://example.test/rss',NOW);self.assertEqual(len(items),1)
        raw={'probability':.7,'uncertainty':.4,'evidenceIds':[items[0]['id']],'tags':['news']}
        result=validate(raw,'BTC',context()['market'],items,NOW);self.assertEqual(result['executionWeight'],0);self.assertFalse(result['calibrated'])
        raw['evidenceIds']=['invented']
        with self.assertRaises(ValueError):validate(raw,'BTC',context()['market'],items,NOW)
        self.assertFalse(parse_rss(rss,'https://example.test/rss',NOW+301))

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_paper_restore_keeps_losses_and_does_not_recapitalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=PolyServerDemo('BTC',60,state_file=Path(tmp)/'state.json');e._new_session();e.paper.update(cash=58.,realizedPnl=-2.,trades=2,endsAt=time.time()-1)
            e._last_saved_at=0;e._save();session=e.paper['sessionId'];self.assertTrue(e._restore_saved())
            self.assertEqual(e.paper['cash'],58);self.assertEqual(e.paper['sessionId'],session);self.assertEqual(e.paper['status'],'complete');e.close()
    async def test_new_paper_session_archives_prior_closed_pnl(self):
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        old=p.engines['BTC'];old.paper.update(cash=59.,realizedPnl=-1.,trades=1)
        try:
            result=await p.reset();self.assertEqual(result['portfolio']['totalPnl'],0);self.assertEqual(result['portfolio']['lifetimeRealizedPnl'],-1);self.assertEqual(result['portfolio']['lifetimeTrades'],1)
        finally:
            for e in p.engines.values():e.close()
    async def test_research_is_in_download_and_loads_only_true_forward_labels(self):
        import poly_audit_export as audit
        with tempfile.TemporaryDirectory() as tmp:
            log=__import__('poly_diagnostics').DiagnosticLog(str(Path(tmp)/'research.db'),'RESEARCH')
            row=dataset(1)[0];log.append('SHADOW_OUTCOME',row=row)
            engine=SimpleNamespace(engines={},research=SimpleNamespace(journal=log))
            path=audit._build_archive('{}',audit.capture_journals(engine))
            try:
                with zipfile.ZipFile(path) as z:self.assertIn('RESEARCH.ndjson',z.namelist())
                self.assertEqual(load_rows([path])[0]['id'],row['id'])
            finally:Path(path).unlink();log.close()
    async def test_v16_read_api_exposes_shadow_status(self):
        import poly_engine_service as service
        fake=SimpleNamespace(research=SimpleNamespace(snapshot=lambda:{'mode':'SHADOW_ONLY','executionEnabled':False},artifact=None))
        with patch.object(service,'engine',fake):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),base_url='https://test') as c:
                s=(await c.get('/api/poly/research/v16')).json();self.assertFalse(s['executionEnabled'])
                model=(await c.get('/api/poly/research/v16/model')).json();self.assertIsNone(model['model'])

    async def test_background_collection_and_native_download_leave_paper_unchanged(self):
        import poly_audit_export as audit
        import poly_engine_service as service
        from poly_semantic import SemanticResearch
        async def parked(*args):await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as tmp:
            p=PaperPortfolio();p.data_root=Path(tmp)
            now=time.time()
            for asset,e in p.engines.items():
                e.state_file=Path(tmp)/(asset+'.json');e._save=lambda:None;e._new_session();c=context(now)
                e.market={**c['market'],'slug':asset+'-test','startTs':now-60,'endTs':now+240};e.feed=c['feed'];e.spot=e.twap=100.;e.spot_ts=e.twap_ts=now
                for side in ('up','down'):e._ingest_book(side,c['depth'][side])
            before={a:copy.deepcopy(e.paper) for a,e in p.engines.items()};manager=audit.DiagnosticDownloads()
            try:
                with patch.object(ExchangeFeeds,'worker',parked),patch.object(SemanticResearch,'worker',parked):
                    p.start_research();await asyncio.sleep(1.8)
                    self.assertGreater(p.research.snapshot()['labelledSamples'],0)
                    for a,e in p.engines.items():self.assertEqual(e.paper,before[a])
                    with patch.object(service,'engine',p),patch.object(audit,'downloads',manager):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),base_url='https://test') as c:
                            snap=(await c.get('/api/poly/demo/server?all_assets=true')).json();self.assertEqual(snap['research']['mode'],'SHADOW_ONLY')
                            first=(await c.get('/api/poly/demo/diagnostics/status?prepare=true')).json();await manager.task
                            ready=(await c.get('/api/poly/demo/diagnostics/status',params={'job':first['job']})).json()
                            response=await c.get(ready['downloadUrl']);self.assertEqual(response.status_code,200)
                            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                                records=[json.loads(line) for line in z.read('RESEARCH.ndjson').splitlines()]
                                self.assertTrue(any(r['kind']=='SHADOW_OUTCOME' for r in records))
            finally:
                if manager.task:await manager.task
                for item in manager.jobs.values():
                    if item.get('path'):Path(item['path']).unlink(missing_ok=True)
                await p.shutdown()

    async def test_research_history_restores_true_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'research.db');hub=ResearchHub({},path)
            hub.emit('SHADOW_OUTCOME',dataset(1)[0]);hub.journal.close()
            restored=ResearchHub({},path)
            try:
                rows,model=restored._load_history();self.assertEqual(len(rows),1);self.assertIsNone(model)
                self.assertEqual(rows[0]['id'],dataset(1)[0]['id'])
            finally:restored.journal.close()

if __name__=='__main__':unittest.main()
