import os,sys,unittest
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'tools'))
import daily_review as dr
def snap(at,rev='abc1234',**lanes):return {'at':at,'revision':rev,'newLanes':lanes}
class DailyReviewTests(unittest.TestCase):
    def test_flags_error_spike_stall_stale_and_deploy_lag(self):
        old=snap(0,eventmaker={'enabled':True,'polls':100,'errors':5},spotlead={'enabled':True,'prints':10,'errors':0})
        new=snap(1000,eventmaker={'enabled':True,'polls':100,'errors':5000,'lastError':'410 Gone'},spotlead={'enabled':True,'prints':50,'errors':0})
        f,_=dr.review(new,old,main_head='fff9999',now=1000+3600)
        kinds={(x['kind'],x.get('lane')) for x in f}
        self.assertIn(('outage','eventmaker'),kinds);self.assertIn(('stalled','eventmaker'),kinds)
        self.assertNotIn(('stalled','spotlead'),kinds);self.assertIn(('deploy',None),kinds)
        self.assertTrue(any('min old' in x['what'] for x in f))
    def test_quiet_day_has_no_findings(self):
        old=snap(0,spotlead={'enabled':True,'prints':10,'errors':1});new=snap(100,spotlead={'enabled':True,'prints':90,'errors':2})
        self.assertEqual(dr.review(new,old,main_head='abc1234ffff',now=200)[0],[])
    def test_headline_reads_test_blocks(self):
        h=dr.headline('spotlead',{'S2':{'verdict':'not confirmed','stressed':{'n':10,'mean':-.01,'lower95':-.03}}})
        self.assertEqual((h['test'],h['n'],h['verdict']),('S2',10,'not confirmed'))
