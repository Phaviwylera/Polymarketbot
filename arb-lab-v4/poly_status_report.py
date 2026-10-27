"""Health and audit snapshots published to a GitHub branch, for hosts whose logs the operator's tools
cannot read (e.g. an EC2 instance). Enabled only when POLY_STATUS_GH_TOKEN is set.

Every REPORT_SEC it writes status/latest.json and once an hour status/hourly/<UTC hour>.json on branch
POLY_STATUS_BRANCH (default bot-status) of POLY_STATUS_REPO. Content: host load and memory, process
memory, feed and storage state, portfolio totals, the compact audit, the last CPU profile and the most
recent important log lines. Paper results and health only: no environment values, URLs or tokens.
"""
from __future__ import annotations
import asyncio,base64,collections,io,json,os,re,sys,time

REPORT_SEC=float(os.getenv('POLY_STATUS_REPORT_SEC','900'))
KEEP=re.compile(r'(Traceback|Error|ERROR|POLY_STORAGE|RECONNECT|LOOP_STALL|POLY_BACKUP|POLY_TRADE |POLY_AUDIT_ERROR|POLY_LANE_PAUSED|POLY_BASELINE|Shutting down|Killed)')
SECRET=re.compile(r'(postgres(ql)?://\S+|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})')

class LineTee(io.TextIOBase):
    """Passes stdout through unchanged and keeps the last important lines (secrets redacted)."""
    def __init__(self,inner,size=80):
        self.inner=inner;self.lines=collections.deque(maxlen=size);self.cpu=None;self._buf=''
    def write(self,s):
        n=self.inner.write(s);self._buf+=s
        while '\n' in self._buf:
            line,self._buf=self._buf.split('\n',1)
            if line.startswith('POLY_CPU_SAMPLE '):self.cpu=line[16:][:4000]
            elif KEEP.search(line):self.lines.append(time.strftime('%H:%M:%S ',time.gmtime())+SECRET.sub('[redacted]',line)[:400])
        return n
    def flush(self):self.inner.flush()
    def isatty(self):return False
    @property
    def encoding(self):return getattr(self.inner,'encoding','utf-8')

def host_stats():
    out={}
    try:out['loadAvg']=[float(x) for x in open('/proc/loadavg').read().split()[:3]]
    except OSError:pass
    try:
        mem={l.split(':')[0]:int(l.split()[1]) for l in open('/proc/meminfo') if ':' in l}
        out['memTotalMb']=round(mem.get('MemTotal',0)/1024);out['memAvailableMb']=round(mem.get('MemAvailable',0)/1024)
    except (OSError,ValueError,IndexError):pass
    try:out['processRssMb']=round(int(next(l for l in open('/proc/self/status') if l.startswith('VmRSS')).split()[1])/1024)
    except (OSError,StopIteration,ValueError):pass
    try:out['cpuCount']=os.cpu_count()
    except Exception:pass
    return out

def snapshot(engine,audit_compact,tee,started_at,now=None):
    now=time.time() if now is None else now;pf={}
    try:pf=engine.snapshot('BTC').get('portfolio') or {}
    except Exception as exc:pf={'error':type(exc).__name__}
    lanes={}
    for k,e in getattr(engine,'engines',{}).items():
        pos=(e.paper or {}).get('position')
        lanes[k]={'feed':dict(e.feed),'paused':bool(getattr(e,'paused',False) and not e.tasks),'decision':str(e.momentum_decision)[:100],
                  'position':{x:pos.get(x) for x in ('side','entryPrice','shares','entryAt','mode')} if pos else None}
    storage=engine.storage.status() if getattr(engine,'storage',None) else {}
    return {'at':now,'atUtc':time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(now)),'uptimeSec':round(now-started_at),
            'host':os.getenv('POLY_HOST_LABEL','unknown'),'revision':_revision(),'hostStats':host_stats(),
            'portfolio':{k:pf.get(k) for k in ('accountEquity','accountPnl','equity','realizedPnl','trades','lifetimeRealizedPnl','lifetimeTrades',
                                               'epochPnl','maxDrawdownUsd','riskLocked','guard','pausedLanes','revision')},
            'lanes':lanes,'storage':storage,'bullpen':_bullpen_state(),'threshold':_threshold_state(),'tape':_tape_state(),'jev':_jev_state(),'newLanes':_lanes_state(),'study':_study_state(),'skillVsLuck':_skill_state(engine),'audit':audit_compact,'cpuProfile':(tee.cpu if tee else None),
            'recentLog':list(tee.lines)[-60:] if tee else []}

def _bullpen_state():
    try:
        import poly_bullpen;b=poly_bullpen.CURRENT;return b.state if b else None
    except Exception:return None

def _skill_state(engine):
    try:return {k:v for k,v in engine.edge.intel.skill_vs_luck().items() if k!='per'}
    except Exception:return None

def _study_state():
    try:
        import poly_tape,poly_history;t=poly_tape.CURRENT;h=poly_history.CURRENT
        return {'live':t.study.compact() if t and t.study else None,'history':h.study.compact() if h else None,'backfill':h.snapshot() if h else None,'liveRebuilt':getattr(t,'study_rebuilt',None) if t else None}
    except Exception as exc:return {'error':type(exc).__name__}

def _lanes_state():
    out={}
    for name in ('poly_maker','poly_xmarket','poly_events','poly_fastmaker','poly_kalshi','poly_rivals','poly_pairacc','poly_eventarb','poly_eventmaker'):
        try:
            import importlib;m=importlib.import_module(name);out[name[5:]]=m.CURRENT.compact() if m.CURRENT else None
        except Exception as exc:out[name[5:]]={'error':type(exc).__name__}
    return out

def _jev_state():
    try:
        import poly_jev;j=poly_jev.CURRENT
        if not j:return None
        r=j.report();r.pop('recent',None);return r
    except Exception as exc:return {'error':type(exc).__name__}

def _tape_state():
    try:
        import poly_tape;t=poly_tape.CURRENT;return t.compact() if t else None
    except Exception as exc:return {'error':type(exc).__name__}

def _threshold_state():
    try:
        import poly_threshold,time as _t;t=poly_threshold.CURRENT
        if not t:return None
        c=t.compact()   # plus the open positions, so a stuck resolution is visible without server access
        c['openPositions']=[{'slug':p.get('slug'),'side':p.get('side'),'entryPrice':p.get('entryPrice'),
                             'endAtUtc':_t.strftime('%Y-%m-%d %H:%M',_t.gmtime(float(p['endAt']))) if p.get('endAt') else None}
                            for p in list(t.open.values())[:20]]
        return c
    except Exception as exc:return {'error':type(exc).__name__}

def _revision():
    try:
        import subprocess
        return subprocess.run(['git','rev-parse','--short','HEAD'],capture_output=True,text=True,timeout=5,cwd=os.path.dirname(__file__) or '.').stdout.strip() or None
    except Exception:return os.getenv('RENDER_GIT_COMMIT')

class GitHubPublisher:
    def __init__(self,token,repo,branch,client=None):
        self.token=token;self.repo=repo;self.branch=branch;self.client=client;self.ready=False
    def _h(self):return {'Authorization':f'Bearer {self.token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'poly-status'}
    async def _c(self):
        if self.client is None:
            import httpx;self.client=httpx.AsyncClient(timeout=20)
        return self.client
    async def ensure_branch(self):
        c=await self._c();base=f'https://api.github.com/repos/{self.repo}'
        r=await c.get(f'{base}/git/ref/heads/{self.branch}',headers=self._h())
        if r.status_code==404:
            main=(await c.get(f'{base}',headers=self._h())).json().get('default_branch','main')
            sha=(await c.get(f'{base}/git/ref/heads/{main}',headers=self._h())).json()['object']['sha']
            r=await c.post(f'{base}/git/refs',headers=self._h(),json={'ref':f'refs/heads/{self.branch}','sha':sha})
        r.raise_for_status();self.ready=True
    async def put(self,path,obj,message):
        c=await self._c();url=f'https://api.github.com/repos/{self.repo}/contents/{path}'
        if not self.ready:await self.ensure_branch()
        cur=await c.get(url,headers=self._h(),params={'ref':self.branch})
        body={'message':message,'branch':self.branch,'content':base64.b64encode(json.dumps(obj,indent=1,default=str).encode()).decode()}
        if cur.status_code==200:body['sha']=cur.json()['sha']
        r=await c.put(url,headers=self._h(),json=body);r.raise_for_status();return r.status_code

async def run(engine,get_audit,tee,started_at,publisher,report_sec=REPORT_SEC):
    last_hour=None
    await asyncio.sleep(min(120.,report_sec))
    while True:
        try:
            try:aud=await get_audit()
            except Exception as exc:aud={'error':type(exc).__name__}
            snap=snapshot(engine,aud,tee,started_at)
            await publisher.put('status/latest.json',snap,f"status {snap['atUtc']}")
            hour=time.strftime('%Y-%m-%dT%H',time.gmtime())
            if hour!=last_hour:
                await publisher.put(f'status/hourly/{hour}.json',snap,f'hourly status {hour}');last_hour=hour
        except asyncio.CancelledError:raise
        except Exception as exc:print('POLY_STATUS_REPORT_ERROR',type(exc).__name__,flush=True)
        await asyncio.sleep(report_sec)

def install_tee():
    if not isinstance(sys.stdout,LineTee):sys.stdout=LineTee(sys.stdout)
    return sys.stdout
