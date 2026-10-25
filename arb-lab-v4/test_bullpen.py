import asyncio,hashlib,io,json,tarfile,tempfile,unittest
from unittest.mock import patch
import poly_bullpen as pb

def _archive(binary=b'#!fake bullpen'):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tf:
        ti=tarfile.TarInfo('bullpen');ti.size=len(binary);tf.addfile(ti,io.BytesIO(binary))
    return buf.getvalue(),binary

_real_sleep=asyncio.sleep
async def _nosleep(*_):await _real_sleep(0)

class Store:
    active=True
    def __init__(self):self.events=[]
    def enqueue(self,e):self.events.append(e)

class Intel:
    def skilled(self):return {'0x'+'a'*40:2.5,'0x'+'b'*40:1.1,'not-an-address':9.}
class Edge:intel=Intel()
class Engine:
    edge=Edge();storage=None

class BullpenTests(unittest.IsolatedAsyncioTestCase):
    def _pin(self,blob,binary):
        return patch.dict(pb.PINNED,{pb._arch():('t',hashlib.sha256(blob).hexdigest(),hashlib.sha256(binary).hexdigest())})

    def test_only_read_commands_are_allowed(self):
        for bad in (['trade','buy','x','Yes','10'],['polymarket','clob','order'],['polymarket','data','my-trades'],
                    ['login'],['polymarket','data','leaderboard','--yes']):
            with self.assertRaises(ValueError):pb.check_args(bad)
        b=pb.Bullpen(root=tempfile.mkdtemp());argv=b.argv(['polymarket','data','wallet-stats','0x'+'a'*40])
        self.assertEqual(argv[1:3],['--read-only','--non-interactive']);self.assertEqual(argv[-2:],['--output','json'])

    def test_environment_carries_no_secrets(self):
        with patch.dict('os.environ',{'POLY_DATABASE_URL':'postgres://secret','POLY_STATUS_GH_TOKEN':'tok'}):
            env=pb.Bullpen(root=tempfile.mkdtemp()).env()
        self.assertNotIn('secret',json.dumps(env));self.assertNotIn('tok',json.dumps(env))
        self.assertEqual(env['BULLPEN_DISABLE_TELEMETRY'],'1');self.assertEqual(env['BULLPEN_DISABLE_UPDATE_CHECK'],'1')

    async def test_install_verifies_checksums(self):
        blob,binary=_archive()
        async def fetch(url):return blob
        with self._pin(blob,binary):
            b=pb.Bullpen(root=tempfile.mkdtemp(),fetch=fetch);path=await b.ensure_installed()
            self.assertEqual(path.read_bytes(),binary);self.assertTrue(b.state['installed'])
            async def boom(url):raise AssertionError('must not re-download a verified binary')
            b.fetch=boom;await b.ensure_installed()
        async def tampered(url):return _archive(b'evil')[0]
        with self._pin(blob,binary):
            with self.assertRaises(RuntimeError):await pb.Bullpen(root=tempfile.mkdtemp(),fetch=tampered).ensure_installed()

    async def test_collect_records_signals_with_our_score_and_skips_bad_wallets(self):
        calls=[]
        async def runner(argv,env):calls.append(argv);return json.dumps({'status':'ok','cmd':argv[5]}).encode()
        b=pb.Bullpen(root=tempfile.mkdtemp(),runner=runner);st=Store()
        with patch.object(pb.asyncio,'sleep',new=_nosleep):
            n=await b.collect_once(Engine(),st,now=1.8e9)
        self.assertEqual(n,2)
        self.assertTrue(all(c[1]=='--read-only' for c in calls));self.assertFalse(any('not-an-address' in c for c in calls))
        kinds=[(e['command'],e['wallet'],e['ourScore']) for e in st.events]
        self.assertIn(('wallet-stats','0x'+'a'*40,2.5),kinds);self.assertIn(('leaderboard',None,None),kinds)
        self.assertTrue(all(e['kind']=='BULLPEN_SIGNAL' for e in st.events))
        # refreshed only after 12h
        with patch.object(pb.asyncio,'sleep',new=_nosleep):
            self.assertEqual(await b.collect_once(Engine(),st,now=1.8e9+3600),0)

    async def test_errors_are_counted_not_raised(self):
        async def runner(argv,env):return b'not json'
        b=pb.Bullpen(root=tempfile.mkdtemp(),runner=runner)
        self.assertIsNone(await b.run(['polymarket','data','leaderboard']));self.assertEqual(b.state['errors'],1)
        async def err(argv,env):return json.dumps({'ok':False,'error_code':'AUTH_REQUIRED'}).encode()
        b.runner=err;await b.run(['polymarket','data','leaderboard']);self.assertIn('AUTH_REQUIRED',b.state['lastError'])

    def test_signal_kind_is_durable(self):
        import poly_storage;self.assertIn('BULLPEN_SIGNAL',poly_storage.KINDS)

    async def test_real_binary_read_only_if_available(self):
        """Uses the real pinned CLI when it can be installed here; skipped offline."""
        b=pb.Bullpen(root=tempfile.mkdtemp())
        try:await asyncio.wait_for(b.ensure_installed(),60)
        except Exception as exc:self.skipTest(f'bullpen download unavailable: {type(exc).__name__}')
        p=await asyncio.create_subprocess_exec(str(b.bin),'--version',stdout=asyncio.subprocess.PIPE,env=b.env())
        out,_=await p.communicate();self.assertIn(pb.VERSION,out.decode())

if __name__=='__main__':unittest.main()
