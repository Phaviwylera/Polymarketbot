"""One-time research export of the recorded tape (paper bot; read-only on trading).

Triggered at server startup when export_request.txt names an id that has not been exported yet (marker file in
POLY_DATA_DIR). Runs as a separate low-priority process: streams every tape file from the backup bucket
(POLY_BACKUP_S3_BUCKET, polybot/tape/, history excluded), writes one gzip CSV row per labelled print, and uploads it
in 20 MB parts plus a manifest to the data-export branch with the existing status token (POLY_STATUS_GH_TOKEN).
Columns: t lane slug tok price size agg won bps secs bid ask bidSize otherBid otherAsk otherBidSize minLater
(minLater = lowest later print on the same side of the same round, more than 0.25 s after this one; 9 when none).
"""
from __future__ import annotations
import base64,gzip,json,os,sys,time,urllib.error,urllib.request

COLS=['t','lane','slug','tok','price','size','agg','won','bps','secs','bid','ask','bidSize','otherBid','otherAsk','otherBidSize','minLater']
PART=20*1024*1024
HERE=os.path.dirname(os.path.abspath(__file__))
def _f(x,d):
    try:return round(float(x),d)
    except (TypeError,ValueError):return ''
def row_of(r):
    """Tape JSON row -> tuple (without minLater), or None if unlabelled."""
    if r.get('won') is None or not r.get('slug'):return None
    try:price=float(r['price'])
    except (TypeError,ValueError,KeyError):return None
    return (round(float(r.get('t') or 0),3),r.get('lane'),r['slug'],(r.get('token') or '?')[:1],price,_f(r.get('size'),2),(r.get('aggressor') or '')[:1],
            int(bool(r['won'])),_f(r.get('spotVsOpenBps'),2),_f(r.get('secondsLeft'),1),_f(r.get('bid'),3),_f(r.get('ask'),3),_f(r.get('bidSize'),1),
            _f(r.get('otherBid'),3),_f(r.get('otherAsk'),3),_f(r.get('otherBidSize'),1))
def with_min_later(rows):
    """Rows of ONE round: per side, sorted by time, append the min price traded more than 0.25 s later."""
    out=[]
    for tk in sorted({r[3] for r in rows}):
        xs=sorted((r for r in rows if r[3]==tk),key=lambda r:r[0]);n=len(xs);suf=[9.]*(n+1)
        for i in range(n-1,-1,-1):suf[i]=min(suf[i+1],xs[i][4])
        j=0
        for i in range(n):
            j=max(j,i+1)
            while j<n and xs[j][0]<=xs[i][0]+0.25:j+=1
            out.append((*xs[i],round(suf[j],4)))
    return out
def gh(token,repo):
    H={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'poly-export'}
    base='https://api.github.com/repos/'+repo
    def req(m,path,b=None):
        rq=urllib.request.Request(base+path,method=m,headers=H,data=None if b is None else json.dumps(b).encode())
        try:
            with urllib.request.urlopen(rq,timeout=300) as resp:return resp.status,json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:return e.code,{}
    def put(path,data,msg):
        st,cur=req('GET',f'/contents/{path}?ref=data-export')
        body={'message':msg,'branch':'data-export','content':base64.b64encode(data).decode()}
        if st==200 and cur.get('sha'):body['sha']=cur['sha']
        return req('PUT','/contents/'+path,body)[0]
    def ensure_branch():
        if req('GET','/git/ref/heads/data-export')[0]==404:
            _,main=req('GET','/git/ref/heads/main');req('POST','/git/refs',{'ref':'refs/heads/data-export','sha':main['object']['sha']})
    return put,ensure_branch
def run(xid):
    import csv,boto3
    root=os.getenv('POLY_DATA_DIR','/tmp');bucket=os.environ['POLY_BACKUP_S3_BUCKET']
    s3=boto3.client('s3',region_name=os.getenv('AWS_REGION') or os.getenv('POLY_BACKUP_S3_REGION') or 'ap-southeast-2')
    keys=[];tok=None
    while True:
        kw={'Bucket':bucket,'Prefix':'polybot/tape/'}
        if tok:kw['ContinuationToken']=tok
        r=s3.list_objects_v2(**kw);keys+=[o['Key'] for o in r.get('Contents',[]) if '/history' not in o['Key'] and o['Key'].endswith('.gz')]
        if not r.get('IsTruncated'):break
        tok=r['NextContinuationToken']
    tmp=os.path.join(root,f'tape_export_{xid}.csv.gz');n=0;rounds=0;bad=0;buf={}
    with gzip.open(tmp,'wt',compresslevel=9) as out:
        w=csv.writer(out);w.writerow(COLS)
        def flush(s):
            nonlocal n,rounds
            rs=with_min_later(buf.pop(s));rounds+=1;n+=len(rs);w.writerows(rs)
        for k in sorted(keys):
            try:txt=gzip.decompress(s3.get_object(Bucket=bucket,Key=k)['Body'].read()).decode()
            except Exception:bad+=1;continue
            tmax=0.
            for line in txt.splitlines():
                try:r=row_of(json.loads(line))
                except Exception:r=None
                if r:buf.setdefault(r[2],[]).append(r);tmax=max(tmax,r[0])
            for s in [s for s,rs in buf.items() if max(x[0] for x in rs)<tmax-1800]:flush(s)
            time.sleep(0.05)
        for s in list(buf):flush(s)
    put,ensure=gh(os.environ['POLY_STATUS_GH_TOKEN'],os.getenv('POLY_STATUS_REPO','Phaviwylera/polymarketbot'));ensure()
    data=open(tmp,'rb').read();parts=[data[i:i+PART] for i in range(0,len(data),PART)];sts=[]
    for i,p in enumerate(parts):sts.append(put(f'tape/{xid}/part{i:02d}.bin',p,f'tape export {xid} part {i}'));time.sleep(2)
    man={'id':xid,'finishedAt':time.time(),'files':len(keys),'badFiles':bad,'rounds':rounds,'rows':n,'bytes':len(data),'parts':len(parts),'putStatus':sts,'columns':COLS}
    put(f'tape/{xid}/manifest.json',json.dumps(man,indent=1).encode(),f'tape export {xid} manifest')
    os.remove(tmp)
    if all(s in (200,201) for s in sts):open(os.path.join(root,f'export_done_{xid}'),'w').write(json.dumps(man))
    print('TAPE_EXPORT_DONE',json.dumps(man),flush=True)

def request_id():
    p=os.path.join(HERE,'export_request.txt')
    try:return open(p).read().strip().split()[0]
    except (OSError,IndexError):return None
def maybe_start():
    """Called at server startup. Starts the export once per request id, as a niced child process."""
    xid=request_id()
    if not xid or not os.getenv('POLY_STATUS_GH_TOKEN') or not os.getenv('POLY_BACKUP_S3_BUCKET'):return None
    root=os.getenv('POLY_DATA_DIR','/tmp')
    if os.path.exists(os.path.join(root,f'export_done_{xid}')):return None
    import subprocess
    log=open(os.path.join(root,f'export_{xid}.log'),'a')
    return subprocess.Popen(['nice','-n','19',sys.executable,os.path.abspath(__file__),xid],stdout=log,stderr=log,cwd=HERE,start_new_session=True)

if __name__=='__main__':run(sys.argv[1])
