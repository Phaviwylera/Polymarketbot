import os,gzip,json,csv,io,base64,random,urllib.request,boto3
B=os.environ['B'];s3=boto3.client('s3',region_name='ap-southeast-2');random.seed(11)
keys=[];tok=None
while True:
    kw={'Bucket':B,'Prefix':'polybot/tape/'}
    if tok:kw['ContinuationToken']=tok
    r=s3.list_objects_v2(**kw);keys+=[o['Key'] for o in r.get('Contents',[]) if '/history' not in o['Key']]
    if not r.get('IsTruncated'):break
    tok=r['NextContinuationToken']
buf={};out=io.StringIO();w=csv.writer(out);w.writerow(['t','lane','slug','tok','price','won','bps','secs','bid','ask','minLater','nPrints']);kept=0;rounds=0
def f(x):
    try:return round(float(x),5)
    except (TypeError,ValueError):return ''
def flush(slug):
    global kept,rounds
    rs=buf.pop(slug);rounds+=1
    for tk in ('U','D'):
        xs=sorted((r for r in rs if r[3]==tk),key=lambda r:r[0])
        if not xs:continue
        n=len(xs);suf=[9.]*(n+1)
        for i in range(n-1,-1,-1):suf[i]=min(suf[i+1],xs[i][4])
        mins=[];j=0
        for i in range(n):
            j=max(j,i+1)
            while j<n and xs[j][0]<=xs[i][0]+0.25:j+=1
            mins.append(suf[j])
        idx=sorted(random.sample(range(len(xs)),40)) if len(xs)>40 else range(len(xs))
        for k in idx:
            x=xs[k];w.writerow([*x,round(mins[k],4),len(xs)]);kept+=1
for k in sorted(keys):
    txt=gzip.decompress(s3.get_object(Bucket=B,Key=k)['Body'].read()).decode()
    tmax=0
    for line in txt.splitlines():
        try:r=json.loads(line)
        except Exception:continue
        if r.get('won') is None or r.get('slug') is None:continue
        t=float(r.get('t') or 0);tmax=max(tmax,t)
        buf.setdefault(r['slug'],[]).append((round(t,3),r.get('lane'),r['slug'],(r.get('token') or '?')[0],float(r['price']),int(bool(r['won'])),f(r.get('spotVsOpenBps')),f(r.get('secondsLeft')),f(r.get('bid')),f(r.get('ask'))))
    for s in [s for s,rs in buf.items() if max(x[0] for x in rs)<tmax-1800]:flush(s)
for s in list(buf):flush(s)
data=gzip.compress(out.getvalue().encode())
tokn=os.environ['POLY_STATUS_GH_TOKEN'];repo=os.environ.get('POLY_STATUS_REPO','Phaviwylera/polymarketbot')
H={'Authorization':'Bearer '+tokn,'Accept':'application/vnd.github+json','User-Agent':'poly-export'}
def req(m,u,b=None):
    rq=urllib.request.Request(u,method=m,headers=H,data=None if b is None else json.dumps(b).encode())
    try:
        with urllib.request.urlopen(rq) as resp:return resp.status,json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:return e.code,{}
base='https://api.github.com/repos/'+repo
st,_=req('GET',base+'/git/ref/heads/data-export')
if st==404:
    _,main=req('GET',base+'/git/ref/heads/main');req('POST',base+'/git/refs',{'ref':'refs/heads/data-export','sha':main['object']['sha']})
st,cur=req('GET',base+'/contents/tape/tapex_sample.csv.gz?ref=data-export')
body={'message':'tape sample export','branch':'data-export','content':base64.b64encode(data).decode()}
if st==200:body['sha']=cur['sha']
st,_=req('PUT',base+'/contents/tape/tapex_sample.csv.gz',body)
print('files',len(keys),'rounds',rounds,'kept',kept,'bytes',len(data),'put',st)
