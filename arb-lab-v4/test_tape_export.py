import os,tempfile,unittest
from unittest import mock
import tape_export as te
class TapeExportTests(unittest.TestCase):
    def test_row_of(self):
        r=te.row_of({'t':1.23456,'lane':'BTC','slug':'s','token':'UP','price':.55,'won':True,'spotVsOpenBps':3.1,'secondsLeft':120,'bid':.54,'ask':.56,'aggressor':'BUY'})
        self.assertEqual(r[:8],(1.235,'BTC','s','U',.55,'','B',1));self.assertIsNone(te.row_of({'slug':'s','price':.5}))   # unlabelled
    def test_min_later_per_side(self):
        rows=[(0.,'BTC','s','U',.5),(0.1,'BTC','s','U',.4),(1.,'BTC','s','U',.6),(2.,'BTC','s','U',.3),(0.5,'BTC','s','D',.5)]
        out={(r[3],r[0]):r[-1] for r in te.with_min_later(rows)}
        self.assertEqual([out[('U',t)] for t in (0.,.1,1.,2.)],[.3,.3,.3,9.]);self.assertEqual(out[('D',.5)],9.)
    def test_starts_once_per_id(self):
        d=tempfile.mkdtemp()
        with mock.patch.dict(os.environ,{'POLY_DATA_DIR':d,'POLY_STATUS_GH_TOKEN':'x','POLY_BACKUP_S3_BUCKET':'b'}),\
             mock.patch.object(te,'request_id',return_value='abc'),mock.patch('subprocess.Popen') as P:
            self.assertIsNotNone(te.maybe_start());open(os.path.join(d,'export_done_abc'),'w').write('{}')
            self.assertIsNone(te.maybe_start());self.assertEqual(P.call_count,1)
    def test_needs_credentials(self):
        with mock.patch.dict(os.environ,{'POLY_STATUS_GH_TOKEN':'','POLY_BACKUP_S3_BUCKET':''}),mock.patch.object(te,'request_id',return_value='abc'):
            self.assertIsNone(te.maybe_start())
