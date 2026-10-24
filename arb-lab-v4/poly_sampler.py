"""Low-rate sampling profiler for the live host. A daemon thread looks at the main thread's stack 20
times a second and counts, per function, how often it was on the stack (inclusive) and on top
(self). Samples where the event loop waits in select() are counted as idle. Every REPORT_SEC it
prints one POLY_CPU_SAMPLE line, so the CPU hogs on the 0.1-CPU host are named from evidence."""
from __future__ import annotations
import json,os,sys,threading,time
from collections import Counter

INTERVAL=float(os.getenv('POLY_SAMPLER_INTERVAL','0.05'))
REPORT_SEC=float(os.getenv('POLY_SAMPLER_REPORT_SEC','300'))
IDLE_FUNCS={'select','poll','_run_once_wait','epoll'}

def _label(code):
    return f"{os.path.basename(code.co_filename)}:{code.co_name}"

class Sampler:
    def __init__(self,thread_id=None):
        self.thread_id=thread_id or threading.main_thread().ident
        self.incl=Counter();self.self_=Counter();self.samples=0;self.idle=0;self.started=time.time()
        self.stop=threading.Event();self.thread=None
    def sample_once(self):
        frame=sys._current_frames().get(self.thread_id)
        if frame is None:return
        self.samples+=1
        top=frame.f_code
        if top.co_name in IDLE_FUNCS and 'selectors' in top.co_filename:
            self.idle+=1;return
        self.self_[_label(top)]+=1;seen=set();f=frame
        while f is not None:
            k=_label(f.f_code)
            if k not in seen:seen.add(k);self.incl[k]+=1
            f=f.f_back
    def report(self,top=14):
        busy=max(1,self.samples-self.idle)
        out={'seconds':round(time.time()-self.started),'samples':self.samples,'busyPct':round(100*(self.samples-self.idle)/max(1,self.samples),1),
             'self':[[k,round(100*v/busy,1)] for k,v in self.self_.most_common(top)],
             'inclusive':[[k,round(100*v/busy,1)] for k,v in self.incl.most_common(top+10) if not k.startswith(('base_events.py','events.py','runners.py','server.py:main','threading.py'))][:top]}
        self.incl.clear();self.self_.clear();self.samples=0;self.idle=0;self.started=time.time()
        return out
    def run(self):
        last=time.time()
        while not self.stop.wait(INTERVAL):
            try:self.sample_once()
            except Exception:pass
            if time.time()-last>=REPORT_SEC:
                last=time.time()
                try:print('POLY_CPU_SAMPLE '+json.dumps(self.report(),separators=(',',':')),flush=True)
                except Exception:pass
    def start(self):
        if self.thread is None:
            self.thread=threading.Thread(target=self.run,name='poly-sampler',daemon=True);self.thread.start()
        return self
