"""Create one consistent, compressed development bundle from all paper lanes."""
import json,tempfile,time,zipfile
from fastapi.responses import StreamingResponse

def diagnostic_response(engine):
    # Called on the engine event loop: no feed callbacks interleave this snapshot/export.
    file=tempfile.SpooledTemporaryFile(max_size=4*1024*1024,mode='w+b')
    manifest={'format':'poly-edge-diagnostics-v14.1','exportedAt':time.time(),'paperOnly':True,
              'coverage':'Received CLOB messages, accepted asset RTDS, quote/print decisions, engine events, one-second state. Not an exchange execution ledger.',
              'limitations':['Rolling 100000-event tail per asset; older events may be pruned.','Ephemeral disk: restart/deploy can erase journals.','Feed gaps and unknown queue depth prevent exact execution reconstruction.','Maker fills require sell-aggressor volume during the quote lifetime; no partial fills are assumed.'], 'assets':{}}
    try:
        with zipfile.ZipFile(file,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('snapshot.json',json.dumps(engine.snapshot(include_assets=True),allow_nan=False,indent=2))
            for asset,lane in engine.engines.items():
                with z.open(asset+'.ndjson','w') as output:manifest['assets'][asset]=lane.diagnostics.export(output)
            z.writestr('manifest.json',json.dumps(manifest,indent=2))
            z.writestr('README.txt','Start with manifest.json for coverage, gaps and retention. snapshot.json is the all-asset state. Each ASSET.ndjson is chronological, with journalSeq, receipt time, market and session identity. Raw CLOB messages retain exchange timestamps. QUOTE_PLACE, QUOTE_PRINT, QUOTE_FILL_EVIDENCE and QUOTE_CANCEL link by quote id. STATE and ENGINE_EVENT contain book, feed, risk, inventory and cash context. No profit is guaranteed.\n')
        file.seek(0)
    except Exception:
        file.close();raise
    def chunks():
        try:
            while True:
                chunk=file.read(256*1024)
                if not chunk:break
                yield chunk
        finally:file.close()
    return StreamingResponse(chunks(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="poly-edge-development-'+time.strftime('%Y-%m-%dT%H-%M-%SZ',time.gmtime())+'.zip"','Cache-Control':'no-store'})
