import json,tempfile,time,unittest
from unittest.mock import patch
import poly_devlog

class DevLogTests(unittest.TestCase):
    def test_report_text_events_and_compact_lines_without_secrets(self):
        import poly_portfolio
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'POLY_DATA_DIR':d,'POLY_DATABASE_URL':''}):
            pf=poly_portfolio.PaperPortfolio()
            for e in pf.engines.values():e._save=lambda:None;e._new_session()
            pf.engines['BTC']._record('BUY',side='UP',price=.41,detail='EDGE_HOLD test')
            rep=poly_devlog.report(pf)
            self.assertEqual(set(rep['lanes']),{'BTC','ETH','BTC15','ETH15'})
            body=poly_devlog.text(rep);self.assertIn('EDGELAB',body);self.assertIn('EDGE_HOLD test',body)
            rows=poly_devlog.events(pf,'btc',10);self.assertEqual(rows[0]['action'],'BUY')
            self.assertEqual(poly_devlog.events(pf,None,10,'sell'),[])
            lines=poly_devlog.compact_lines(rep)
            self.assertEqual(len(lines),5);self.assertTrue(all(len(l)<16000 for l in lines))
            json.loads(lines[0].split(' ',1)[1])
            self.assertNotIn('postgres',body.lower().replace('postgresql',''))
            for e in pf.engines.values():e.close()
if __name__=='__main__':unittest.main()

class RulesScorecardTests(unittest.TestCase):
    def test_splits_history_backtest_and_forward(self):
        import poly_devlog,poly_momentum as pm
        live=pm.RULES_LIVE_AT
        rows=[{'pnl':.5,'entry':.40,'estEdge':.15,'entryAt':live-900,'exitAt':live-890},    # matches rules, before live
              {'pnl':-.3,'entry':.50,'estEdge':.20,'entryAt':live-800,'exitAt':live-790},   # above the ceiling
              {'pnl':-.4,'entry':.40,'estEdge':.05,'entryAt':live-700,'exitAt':live-690},   # below the edge floor
              {'pnl':.7,'entry':.30,'estEdge':.25,'entryAt':live+60,'exitAt':live+70},
              {'pnl':-.5,'entry':.42,'estEdge':.13,'entryAt':live+120,'exitAt':live+130}]
        s=poly_devlog.rules_scorecard(None,rows)
        self.assertEqual(s['allHistory']['trades'],5);self.assertEqual(s['matchingCurrentRules']['trades'],3)
        self.assertEqual(s['sinceRulesLive']['trades'],2);self.assertEqual(s['sinceRulesLive']['winRatePct'],50.)
        m=s['matchingCurrentRules'];self.assertAlmostEqual(m['netPnlUsd'],.7);self.assertAlmostEqual(m['profitFactor'],1.2/.5,places=3)
        self.assertEqual(poly_devlog._metrics([])['trades'],0)
