"""Daily verified backup of the bot's Postgres data to S3 (POLY_BACKUP_S3_BUCKET).

A backup is a gzip JSON-lines file: one header line, every state row, then every event (keyset-paginated,
so memory stays flat). It counts as verified only when all of these pass:
  1. per-kind event counts and max(t) in the file equal the database's, up to the backup's horizon;
  2. the object is downloaded back from S3 and its SHA-256 equals the uploaded file's;
  3. restore check on the downloaded copy: the paper/edge/model state rows parse, and the paper
     checkpoint's portfolio totals equal the database's.
Credentials come from the instance role (no keys in config). The database URL is never logged.
"""
from __future__ import annotations
import asyncio,gzip,hashlib,json,os,tempfile,time

BATCH=2000;PREFIX='polybot/'

def _sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()

def read_back(path):
    """Counts, max t per kind and parsed state rows from a backup file (the restore-side view)."""
    kinds={};states={};header=None
    with gzip.open(path,'rt') as f:
        for i,line in enumerate(f):
            r=json.loads(line)
            if i==0:header=r;continue
            if r['type']=='state':states[r['name']]=json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload']
            else:
                k=kinds.setdefault(r['kind'],{'rows':0,'maxT':0.});k['rows']+=1;k['maxT']=max(k['maxT'],float(r['t']))
    return header,kinds,states

def portfolio(state):
    p=(state or {}).get('portfolio') or {}
    return {k:p.get(k) for k in ('equity','realizedPnl','trades','lifetimeRealizedPnl','lifetimeTrades','epochStart','riskLocked','maxDrawdownUsd')}

async def dump(conn,path):
    """Write the backup file from an open asyncpg connection; returns the database-side expectation."""
    cutoff=float(await conn.fetchval('SELECT coalesce(max(t),0) FROM poly_v161_events'))
    expect={r['kind']:{'rows':int(r['n']),'maxT':float(r['m'])} for r in
            await conn.fetch('SELECT kind,count(*) n,max(t) m FROM poly_v161_events WHERE t<=$1 GROUP BY kind',cutoff)}
    states=await conn.fetch('SELECT name,updated_at,payload::text AS payload FROM poly_v161_state')
    with gzip.open(path,'wt',compresslevel=6) as f:
        f.write(json.dumps({'schema':'polybot-backup-v1','createdAt':time.time(),'verifiedUpToT':cutoff})+'\n')
        for r in states:f.write(json.dumps({'type':'state','name':r['name'],'updated_at':float(r['updated_at']),'payload':r['payload']})+'\n')
        last_t=-1.;last_id=''
        while True:
            rows=await conn.fetch('SELECT id,t,asset,kind,payload::text AS payload FROM poly_v161_events WHERE t<=$1 AND (t,id)>($2,$3) ORDER BY t,id LIMIT $4',
                                  cutoff,last_t,last_id,BATCH)
            if not rows:break
            for r in rows:f.write(json.dumps({'type':'event','id':r['id'],'t':float(r['t']),'asset':r['asset'],'kind':r['kind'],'payload':r['payload']})+'\n')
            last_t=float(rows[-1]['t']);last_id=rows[-1]['id'];await asyncio.sleep(.02)
    paper=next((json.loads(r['payload']) for r in states if r['name']=='paper'),None)
    return {'cutoff':cutoff,'kinds':expect,'stateRows':sorted(r['name'] for r in states),'portfolio':portfolio(paper)}

class S3:
    """Thin boto3 wrapper (calls run in a thread). Injectable for tests."""
    def __init__(self,bucket,region=None,client=None):
        self.bucket=bucket;self._c=client;self.region=region or os.getenv('AWS_REGION') or os.getenv('POLY_BACKUP_S3_REGION') or 'ap-southeast-2'
    def c(self):
        if self._c is None:
            import boto3;self._c=boto3.client('s3',region_name=self.region)
        return self._c
    async def put(self,path,key,sha):
        await asyncio.to_thread(self.c().upload_file,path,self.bucket,key,ExtraArgs={'Metadata':{'sha256':sha},'ServerSideEncryption':'AES256'})
    async def put_json(self,key,obj):
        body=json.dumps(obj,separators=(',',':'),default=str).encode()
        await asyncio.to_thread(self.c().put_object,Bucket=self.bucket,Key=key,Body=body,ServerSideEncryption='AES256',ContentType='application/json')
    async def get(self,key,path):
        await asyncio.to_thread(self.c().download_file,self.bucket,key,path)
    async def latest_backup_at(self):
        """Epoch of the newest VERIFIED backup (its marker key), or None."""
        def run():
            r=self.c().list_objects_v2(Bucket=self.bucket,Prefix=PREFIX+'20');best=None
            for o in r.get('Contents',[]):
                if '/verified-' not in o['Key']:continue          # only backups that passed every check
                k=o['Key'].rsplit('verified-',1)[-1].split('.')[0]
                if k.isdigit():best=max(best or 0,int(k))
            return best
        return await asyncio.to_thread(run)
    async def list_get(self,prefix,days=7):
        """Labelled tape rows from the last `days` of hourly files under prefix (warm start of the learner)."""
        def run():
            import io
            cut=time.time()-days*86400;rows=[];token=None
            while True:
                kw={'Bucket':self.bucket,'Prefix':prefix}
                if token:kw['ContinuationToken']=token
                r=self.c().list_objects_v2(**kw)
                for o in r.get('Contents',[]):
                    if o['LastModified'].timestamp()<cut:continue
                    body=self.c().get_object(Bucket=self.bucket,Key=o['Key'])['Body'].read()
                    with gzip.open(io.BytesIO(body),'rt') as f:rows+=[json.loads(l) for l in f if l.strip()]
                if not r.get('IsTruncated'):break
                token=r.get('NextContinuationToken')
            return rows
        return await asyncio.to_thread(run)

async def backup(url,s3,log=print,workdir=None):
    import asyncpg
    started=time.time();work=tempfile.mkdtemp(prefix='polybot-backup-',dir=workdir)
    local=os.path.join(work,'backup.jsonl.gz');back=os.path.join(work,'readback.jsonl.gz')
    conn=await asyncpg.connect(url,timeout=20,command_timeout=120)
    try:expect=await dump(conn,local)
    finally:await conn.close()
    try:
        sha=_sha(local);key=f"{PREFIX}{time.strftime('%Y-%m-%d',time.gmtime(started))}/backup-{int(started)}.jsonl.gz"
        await s3.put(local,key,sha);await s3.get(key,back)
        sha_back=_sha(back);kinds={};states={};read_error=None
        if sha_back==sha:
            try:_,kinds,states=read_back(back)
            except (OSError,ValueError,EOFError) as exc:read_error=type(exc).__name__
        mismatched=sorted(k for k in set(expect['kinds'])|set(kinds) if expect['kinds'].get(k)!=kinds.get(k))
        restore={n:('ok' if n in states else 'absent') for n in ('paper','edge','model')}
        pf_ok=portfolio(states.get('paper'))==expect['portfolio']
        ok=bool(sha==sha_back and not read_error and not mismatched and restore['paper']=='ok' and pf_ok)
        report={'ok':ok,'at':time.time(),'target':'s3','key':key,'bytes':os.path.getsize(local),'sha256':sha,'downloadedSha256Matches':sha==sha_back,'readError':read_error,
                'eventRows':sum(k['rows'] for k in kinds.values()),'kinds':len(kinds),'mismatchedKinds':mismatched,'stateRows':expect['stateRows'],
                'restoreCheck':restore,'portfolioMatches':pf_ok,'portfolio':expect['portfolio'],'verifiedUpToT':expect['cutoff'],
                'seconds':round(time.time()-started,1)}
        if ok and hasattr(s3,'put_json'):   # marker: lets a restart know a verified backup already exists
            try:await s3.put_json(key.rsplit('/',1)[0]+f'/verified-{int(started)}.json',report)
            except Exception:report['markerError']=True
        log('POLY_BACKUP '+json.dumps(report,separators=(',',':'),default=str))
        return report
    finally:
        for p in (local,back):
            try:os.remove(p)
            except OSError:pass
        try:os.rmdir(work)
        except OSError:pass
