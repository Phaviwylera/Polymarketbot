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
            self.assertEqual(set(rep['lanes']),{'BTC','ETH','SOL','XRP'})
            body=poly_devlog.text(rep);self.assertIn('EDGELAB',body);self.assertIn('EDGE_HOLD test',body)
            rows=poly_devlog.events(pf,'btc',10);self.assertEqual(rows[0]['action'],'BUY')
            self.assertEqual(poly_devlog.events(pf,None,10,'sell'),[])
            lines=poly_devlog.compact_lines(rep)
            self.assertEqual(len(lines),5);self.assertTrue(all(len(l)<16000 for l in lines))
            json.loads(lines[0].split(' ',1)[1])
            self.assertNotIn('postgres',body.lower().replace('postgresql',''))
            for e in pf.engines.values():e.close()
if __name__=='__main__':unittest.main()
