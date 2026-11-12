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
    def test_restart_in_window_compares_counters_with_zero(self):
        # 2026-11-12: server restarted ~24 h after the old snapshot; rivals ran 4 times in each boot (4 == 4) and was
        # flagged as stalled although it was running. Counters reset at boot, so the baseline must be 0.
        day=86400
        old=snap(0,rivals={'enabled':True,'runs':4,'errors':300},events={'enabled':True,'scans':50,'errors':0})
        new=snap(day,rivals={'enabled':True,'runs':4,'errors':0},events={'enabled':True,'scans':0,'errors':250})
        new['uptimeSec']=day-3600
        f,rows=dr.review(new,old,main_head='abc1234ffff',now=day+60)
        kinds={(x['kind'],x.get('lane')) for x in f}
        self.assertNotIn(('stalled','rivals'),kinds)
        self.assertIn(('stalled','events'),kinds)          # still 0 scans 23 h after boot
        self.assertIn(('outage','events'),kinds)           # 250 errors since boot
        self.assertNotIn(('outage','rivals'),kinds)
    def test_fresh_boot_is_not_flagged_stalled(self):
        old=snap(0,rivals={'enabled':True,'runs':4});new=snap(86400,rivals={'enabled':True,'runs':0});new['uptimeSec']=600
        self.assertEqual(dr.review(new,old,main_head='abc1234ffff',now=86460)[0],[])
    def test_activity_keys_match_the_lanes_status(self):
        # 2026-11-12: xmarket reports 'scans', not 'polls', so its stall check never ran.
        old=snap(0,xmarket={'enabled':True,'scans':280,'errors':0});new=snap(100,xmarket={'enabled':True,'scans':280,'errors':0})
        f,_=dr.review(new,old,main_head='abc1234ffff',now=200)
        self.assertIn(('stalled','xmarket'),{(x['kind'],x.get('lane')) for x in f})
