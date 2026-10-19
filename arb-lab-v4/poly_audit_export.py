"""Create one consistent, compressed development bundle from all paper lanes."""
import asyncio,json,os,tempfile,time,uuid,zipfile
from pathlib import Path
from fastapi.responses import StreamingResponse,JSONResponse

def capture_journals(engine):
    journals=[(asset,lane.diagnostics,lane.diagnostics.capture()) for asset,lane in engine.engines.items()]
    research=getattr(engine,'research',None)
    if research is not None:journals.append(('RESEARCH',research.journal,research.journal.capture()))
    return journals

async def diagnostic_response(engine):
    # Capture IDs/state quickly, then keep compression and journal reads off the feed loop.
    snapshot=json.dumps(engine.snapshot(include_assets=True),allow_nan=False,indent=2)
    journals=capture_journals(engine)
    path=await asyncio.to_thread(_build_archive,snapshot,journals)
    return _archive_response(path,delete_after=True)

def _build_archive(snapshot,journals):
    fd,path=tempfile.mkstemp(prefix='poly-audit-',suffix='.zip');os.close(fd)
    manifest={'format':'poly-edge-diagnostics-v16.1','exportedAt':time.time(),'paperOnly':True,
              'coverage':'Accepted CLOB messages, one-second stale-message samples and rejection counts, accepted asset RTDS, quote/print decisions, engine events, one-second state. RESEARCH.ndjson contains causal directional probes, pair quote/queue/fill/hedge/exit outcomes and model evaluations. pair-shadow.json is isolated virtual performance; research-quality.json distinguishes conservative censored labels. Not an exchange execution ledger.',
              'limitations':['Per asset: 100000 raw messages, 20000 state observations and 20000 critical events retained separately; older rows may be pruned.','Ephemeral disk: restart/deploy can erase journals.','Feed gaps and unknown queue depth prevent exact execution reconstruction.','Maker fills require sell-aggressor volume during the quote lifetime; no partial fills are assumed.'], 'assets':{}}
    try:
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.writestr('snapshot.json',snapshot)
            parsed=json.loads(snapshot)
            z.writestr('execution-attribution.json',json.dumps(parsed.get('attribution',{}),indent=2))
            z.writestr('storage-status.json',json.dumps(parsed.get('infrastructure',{}),indent=2))
            research=parsed.get('research') or {}
            z.writestr('pair-shadow.json',json.dumps(research.get('pairShadow',{}),indent=2))
            z.writestr('research-quality.json',json.dumps(research.get('labelQuality',{}),indent=2))
            for asset,journal,capture in journals:
                with z.open(asset+'.ndjson','w') as output:manifest['assets'][asset]=journal.export(output,capture)
            z.writestr('manifest.json',json.dumps(manifest,indent=2))
            z.writestr('README.txt','Start with manifest.json for coverage, gaps and retention. snapshot.json is the all-asset state. Each ASSET.ndjson is chronological, with journalSeq, receipt time, market and session identity. Raw CLOB messages retain exchange timestamps. QUOTE_PLACE, QUOTE_PRINT, QUOTE_FILL_EVIDENCE and QUOTE_CANCEL link by quote id. STATE and ENGINE_EVENT contain book, feed, risk, inventory and cash context. No profit is guaranteed.\n')
    except Exception:
        Path(path).unlink(missing_ok=True);raise
    finally:
        for asset,journal,capture in journals:journal.release_capture(capture)
    return path

def _archive_response(path,delete_after=False):
    def chunks():
        try:
            with open(path,'rb') as file:
                while True:
                    chunk=file.read(256*1024)
                    if not chunk:break
                    yield chunk
        finally:
            if delete_after:Path(path).unlink(missing_ok=True)
    return StreamingResponse(chunks(),media_type='application/zip',headers={
        'Content-Disposition':'attachment; filename="poly-edge-development-'+time.strftime('%Y-%m-%dT%H-%M-%SZ',time.gmtime())+'.zip"',
        'Content-Length':str(Path(path).stat().st_size),'Cache-Control':'no-store'})

class DiagnosticDownloads:
    """One archive job at a time; polling never starts another compression worker."""
    def __init__(self):
        self.current=None;self.jobs={};self.task=None
    def status(self,engine,prepare=False,job=None):
        now=time.time()
        if job:
            item=self.jobs.get(job)
            if item is None:return {'state':'expired','detail':'Prepare a new download.'}
            return self._public(item)
        active=self.current and self.current['state']=='preparing'
        fresh=self.current and self.current['state']=='ready' and now-self.current['finishedAt']<60
        if prepare and not active and not fresh:
            # Retain recent completed files so an older ready link remains usable.
            for key,item in list(self.jobs.items()):
                if item['state']!='preparing' and now-item.get('finishedAt',now)>600:
                    if item.get('path'):Path(item['path']).unlink(missing_ok=True)
                    del self.jobs[key]
            snapshot=json.dumps(engine.snapshot(include_assets=True),allow_nan=False,indent=2)
            journals=capture_journals(engine)
            item={'job':uuid.uuid4().hex,'state':'preparing','startedAt':now,'path':None}
            self.current=item;self.jobs[item['job']]=item
            self.task=asyncio.create_task(self._prepare(item,snapshot,journals))
        return self._public(self.current) if self.current else {'state':'idle'}
    @staticmethod
    def _public(item):
        result={k:v for k,v in item.items() if k!='path'}
        result['elapsedSeconds']=round(time.time()-item['startedAt'],1)
        if item['state']=='ready':result['downloadUrl']='/api/poly/demo/diagnostics?job='+item['job']
        return result
    async def _prepare(self,item,snapshot,journals):
        try:
            item['path']=await asyncio.to_thread(_build_archive,snapshot,journals)
            item.update(state='ready',bytes=Path(item['path']).stat().st_size,finishedAt=time.time())
        except Exception as exc:
            for asset,journal,capture in journals:journal.release_capture(capture)
            item.update(state='failed',detail='Archive preparation failed. Try again.',finishedAt=time.time())
            print('POLY_EXPORT_ERROR',repr(exc),flush=True)
    def response(self,engine,job=None):
        item=self.jobs.get(job) if job else None
        if not job:
            self.status(engine,prepare=True);item=self.current
        if item and item['state']=='ready':return _archive_response(item['path'])
        status=self._public(item) if item else {'state':'expired','detail':'Prepare a new download.'}
        return JSONResponse(status,status_code=202 if item and item['state']=='preparing' else 404,headers={'Cache-Control':'no-store','Retry-After':'2'})

downloads=DiagnosticDownloads()
