import asyncio,io,json,unittest
import poly_status_report as sr

class TeeTests(unittest.TestCase):
    def test_passes_through_and_keeps_important_lines_redacted(self):
        out=io.StringIO();t=sr.LineTee(out)
        t.write('INFO: GET /health 200\nPOLY_STORAGE_ERROR TimeoutError step=flush\n')
        t.write('POLY_BACKUP_ERROR postgresql://u:pw@h:5432/db and ghp_'+'a'*30+'\nPOLY_CPU_SAMPLE {"busyPct":7}\n')
        self.assertIn('GET /health',out.getvalue())
        self.assertEqual(len(t.lines),2);self.assertNotIn('pw@',''.join(t.lines));self.assertNotIn('ghp_a',''.join(t.lines))
        self.assertIn('[redacted]',t.lines[1]);self.assertEqual(t.cpu,'{"busyPct":7}')

class Resp:
    def __init__(self,code,data=None):self.status_code=code;self._d=data or {}
    def json(self):return self._d
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError(self.status_code)
class Client:
    def __init__(self):self.calls=[];self.files={}
    async def get(self,url,headers=None,params=None):
        self.calls.append(('GET',url))
        if url.endswith('/git/ref/heads/bot-status'):return Resp(404)
        if url.endswith('/git/ref/heads/main'):return Resp(200,{'object':{'sha':'abc'}})
        if '/contents/' in url:return Resp(200,{'sha':'s1'}) if url in self.files else Resp(404)
        return Resp(200,{'default_branch':'main'})
    async def post(self,url,headers=None,json=None):self.calls.append(('POST',url,json));return Resp(201)
    async def put(self,url,headers=None,json=None):self.calls.append(('PUT',url,json));self.files[url]=json;return Resp(201)

class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_branch_then_updates_with_sha(self):
        c=Client();p=sr.GitHubPublisher('tok','o/r','bot-status',client=c)
        await p.put('status/latest.json',{'a':1},'m');await p.put('status/latest.json',{'a':2},'m')
        posts=[x for x in c.calls if x[0]=='POST'];self.assertEqual(posts[0][2],{'ref':'refs/heads/bot-status','sha':'abc'})
        puts=[x for x in c.calls if x[0]=='PUT'];self.assertNotIn('sha',puts[0][2]);self.assertEqual(puts[1][2]['sha'],'s1')
        self.assertEqual(puts[0][2]['branch'],'bot-status')

class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_no_environment_values(self):
        import os
        from poly_portfolio import PaperPortfolio
        p=PaperPortfolio()
        for e in p.engines.values():e._save=lambda:None;e._new_session()
        os.environ['POLY_DATABASE_URL']='postgresql://secret@x/db'
        try:s=sr.snapshot(p,{'verdict':'FAIL'},None,0.)
        finally:os.environ.pop('POLY_DATABASE_URL')
        txt=json.dumps(s,default=str);self.assertNotIn('secret@',txt);self.assertEqual(s['audit']['verdict'],'FAIL');self.assertIn('BTC',s['lanes'])

if __name__=='__main__':unittest.main()
