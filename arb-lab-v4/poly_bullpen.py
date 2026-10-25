"""Bullpen CLI as a READ-ONLY research source for the paper bot (record-only; never affects trading).

What it does
  * Installs the pinned Bullpen CLI binary by itself (no manual step): downloads the release archive
    from GitHub, verifies the archive AND binary SHA-256 against the values pinned below, and keeps it
    under POLY_DATA_DIR/bullpen. A mismatch refuses to run.
  * Every 30 minutes looks up, for the top wallets our own WalletIntel rates as skilled, Bullpen's
    public wallet analytics (`polymarket data wallet-stats --section all`, `trader-profile`), plus the
    public leaderboard every 6 hours. Each result is stored as a BULLPEN_SIGNAL event together with
    OUR score for that wallet at that moment, so later research can test whether Bullpen's signals
    add anything over our own wallet scoring. Nothing here feeds entries, exits or sizing.

Safety
  * Never logs in, never stores credentials: BULLPEN_HOME is an isolated, empty directory.
  * Every call carries --read-only (the CLI blocks all mutating commands) and --non-interactive.
  * Only an allow-list of `polymarket data` read commands can be run; anything else raises.
  * Minimal environment (no database URL, no tokens), telemetry and update checks disabled.
Off with POLY_BULLPEN=0.
"""
from __future__ import annotations
import asyncio,hashlib,io,json,os,platform,re,stat,tarfile,time
from pathlib import Path

VERSION='0.1.118'
# (release archive sha256, binary sha256). The x86_64 binary was cross-checked against the copy the
# official npm package installs after its own release-evidence verification.
PINNED={'aarch64':('aarch64-unknown-linux-musl','ea0373ff23947436d67c4255b0dcc4b77e33670f391131d9f56782518198147a',
                   '9488631881e49e2ca6cecd19e748157b51cd2b83d5dca5ca7489a3b654e13b88'),
        'x86_64':('x86_64-unknown-linux-musl','4f25d25935364d201de01f2113055136b58c346727684365e99d50fb68d042e9',
                  '21a7a619672285b3d080998ceae3ef510ce34ee369e6da87c353146361a032f9')}
URL='https://github.com/BullpenFi/bullpen-cli-releases/releases/download/v{v}/bullpen-{v}-{t}.tar.gz'
ALLOWED={'wallet-stats','trader-profile','leaderboard','traded','open-interest','volume'}
ADDRESS=re.compile(r'^0x[0-9a-fA-F]{40}$')
WALLETS_PER_RUN=int(os.getenv('POLY_BULLPEN_WALLETS','25'))
CURRENT=None                     # the running collector, for status reports
INTERVAL_SEC=1800;WALLET_REFRESH_SEC=12*3600;LEADERBOARD_SEC=6*3600

def _arch():
    m=platform.machine().lower()
    return 'aarch64' if m in ('aarch64','arm64') else 'x86_64' if m in ('x86_64','amd64') else None

def _sha(b):return hashlib.sha256(b).hexdigest()

def check_args(args):
    """Only `polymarket data <allowed read command> ...`; raises for anything else."""
    args=[str(a) for a in args]
    if len(args)<3 or args[0]!='polymarket' or args[1]!='data' or args[2] not in ALLOWED:
        raise ValueError(f'bullpen command not allowed: {" ".join(args[:3])}')
    if any(a in ('--yes','-y') for a in args):raise ValueError('bullpen: --yes is never allowed')
    return args

class Bullpen:
    def __init__(self,root=None,fetch=None,runner=None):
        self.root=Path(root or os.path.join(os.getenv('POLY_DATA_DIR','/tmp'),'bullpen'))
        self.bin=self.root/VERSION/'bullpen';self.home=self.root/'home'
        self.fetch=fetch;self.runner=runner          # injectable for tests
        self.seen={};self.leaderboard_at=0.;self.seq=0;self.boot=f'bullpen-{int(time.time())}'
        self.state={'enabled':True,'installed':False,'version':VERSION,'lastRunAt':None,'calls':0,'ok':0,'errors':0,
                    'lastError':None,'walletsCovered':0,'eventsRecorded':0,'mode':'read-only, record-only, no login'}

    # ---- install --------------------------------------------------------------------------------
    async def _download(self,url):
        if self.fetch:return await self.fetch(url)
        import httpx
        async with httpx.AsyncClient(timeout=120,follow_redirects=True) as c:
            r=await c.get(url);r.raise_for_status();return r.content
    async def ensure_installed(self):
        arch=_arch()
        if arch not in PINNED:raise RuntimeError(f'unsupported architecture {platform.machine()}')
        target,archive_sha,binary_sha=PINNED[arch]
        if self.bin.exists() and _sha(self.bin.read_bytes())==binary_sha:
            self.state['installed']=True;return self.bin
        blob=await self._download(URL.format(v=VERSION,t=target))
        if _sha(blob)!=archive_sha:raise RuntimeError('bullpen archive checksum mismatch; refusing to install')
        with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as tf:
            member=next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name=='bullpen'),None)
            if member is None:raise RuntimeError('bullpen binary missing from archive')
            data=tf.extractfile(member).read()
        if _sha(data)!=binary_sha:raise RuntimeError('bullpen binary checksum mismatch; refusing to install')
        self.bin.parent.mkdir(parents=True,exist_ok=True);tmp=self.bin.with_suffix('.tmp')
        tmp.write_bytes(data);tmp.chmod(stat.S_IRWXU|stat.S_IRGRP|stat.S_IXGRP);os.replace(tmp,self.bin)
        self.state['installed']=True;print('POLY_BULLPEN_INSTALLED',VERSION,arch,flush=True)
        return self.bin

    # ---- run ------------------------------------------------------------------------------------
    def argv(self,args):
        return [str(self.bin),'--read-only','--non-interactive',*check_args(args),'--output','json']
    def env(self):
        self.home.mkdir(parents=True,exist_ok=True)
        return {'PATH':'/usr/bin:/bin','HOME':str(self.home),'BULLPEN_HOME':str(self.home.resolve()),
                'BULLPEN_DISABLE_UPDATE_CHECK':'1','BULLPEN_DISABLE_TELEMETRY':'1','DO_NOT_TRACK':'1'}
    async def run(self,args,timeout=60):
        argv=self.argv(args);self.state['calls']+=1
        try:
            if self.runner:out=await self.runner(argv,self.env())
            else:
                p=await asyncio.create_subprocess_exec(*argv,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=self.env())
                try:out,_=await asyncio.wait_for(p.communicate(),timeout)
                except asyncio.TimeoutError:p.kill();raise
            data=json.loads(out or b'{}')
        except Exception as exc:
            self.state['errors']+=1;self.state['lastError']=f'{args[2]}: {type(exc).__name__}';return None
        if isinstance(data,dict) and (data.get('ok') is False or data.get('error_code')):
            self.state['errors']+=1;self.state['lastError']=f"{args[2]}: {data.get('error_code') or data.get('code')}"
        else:self.state['ok']+=1
        return data

    # ---- collect --------------------------------------------------------------------------------
    def _record(self,store,command,data,wallet=None,our=None,now=None,skill=None):
        if store is None or data is None:return
        now=time.time() if now is None else now;self.seq+=1
        store.enqueue({'bootId':self.boot,'journalSeq':self.seq,'t':now,'asset':'ALL','kind':'BULLPEN_SIGNAL',
                       'command':command,'wallet':wallet,'ourScore':our,'ourSkillTest':skill,'bullpenVersion':VERSION,'data':data})
        self.state['eventsRecorded']+=1
    async def collect_once(self,engine,store,now=None):
        now=time.time() if now is None else now
        intel=getattr(getattr(engine,'edge',None),'intel',None)
        skilled=intel.skilled() if intel is not None else {}
        due=[(w,s) for w,s in skilled.items() if ADDRESS.match(str(w)) and now-self.seen.get(w,0)>=WALLET_REFRESH_SEC][:WALLETS_PER_RUN]
        for w,s in due:
            for cmd,extra in (('wallet-stats',['--section','all']),('trader-profile',[])):
                self._record(store,cmd,await self.run(['polymarket','data',cmd,w,*extra]),w,round(float(s),6),now,
                             intel.skill_test(w) if hasattr(intel,'skill_test') else None)
            self.seen[w]=now;await asyncio.sleep(1)
        if now-self.leaderboard_at>=LEADERBOARD_SEC:
            self._record(store,'leaderboard',await self.run(['polymarket','data','leaderboard']),now=now);self.leaderboard_at=now
        self.state['lastRunAt']=now;self.state['walletsCovered']=len(self.seen)
        return len(due)
    async def loop(self,engine,first_delay=300):
        global CURRENT;CURRENT=self
        await asyncio.sleep(first_delay)
        while True:
            try:
                st=getattr(engine,'storage',None)
                if st is None or st.active:          # standby never collects (owner only)
                    await self.ensure_installed();await self.collect_once(engine,st)
                    print('POLY_BULLPEN '+json.dumps({k:self.state[k] for k in ('calls','ok','errors','lastError','walletsCovered','eventsRecorded')},separators=(',',':')),flush=True)
            except Exception as exc:
                self.state['lastError']=f'{type(exc).__name__}: {str(exc)[:120]}';print('POLY_BULLPEN_ERROR',self.state['lastError'],flush=True)
            await asyncio.sleep(INTERVAL_SEC)
