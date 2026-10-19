"""Portable shadow regressors and chronological, round-grouped evaluation."""
import hashlib,json,math,statistics,struct,time
from poly_features import SCHEMA,POLICY,NAMES
CAPS={'BTC':3.25,'ETH':3.25,'SOL':3.,'XRP':3.}

def solve(matrix,vector):
    a=[list(row)+[b] for row,b in zip(matrix,vector)];n=len(vector)
    for i in range(n):
        pivot=max(range(i,n),key=lambda j:abs(a[j][i]));a[i],a[pivot]=a[pivot],a[i]
        if abs(a[i][i])<1e-12:raise ValueError('singular model')
        v=a[i][i];a[i]=[x/v for x in a[i]]
        for j in range(n):
            if j!=i:
                v=a[j][i]
                if v:a[j]=[x-v*y for x,y in zip(a[j],a[i])]
    return [r[-1] for r in a]

def fit_ridge(rows):
    n=len(rows);d=len(NAMES);means=[sum(r['features'][k] for r in rows)/n for k in NAMES]
    scales=[max(1e-6,math.sqrt(sum((r['features'][k]-means[i])**2 for r in rows)/n)) for i,k in enumerate(NAMES)]
    mean=statistics.mean(r['netReturnPerShare'] for r in rows);matrix=[[0.]*d for _ in NAMES];vector=[0.]*d
    for r in rows:
        x=[(r['features'][k]-means[i])/scales[i] for i,k in enumerate(NAMES)];y=r['netReturnPerShare']-mean
        for i in range(d):
            vector[i]+=x[i]*y
            for j in range(i,d):matrix[i][j]+=x[i]*x[j]
    for i in range(d):
        matrix[i][i]+=10.
        for j in range(i):matrix[i][j]=matrix[j][i]
    return {'kind':'ridge','means':means,'scales':scales,'coefficients':solve(matrix,vector),'intercept':mean}

def predict(model,features):
    x=[float(features[k]) for k in NAMES]
    if model['kind']=='constant':value=model['value']
    elif model['kind']=='ridge':value=model['intercept']+sum(w*(v-m)/s for w,v,m,s in zip(model['coefficients'],x,model['means'],model['scales']))
    elif model['kind']=='xgboost':
        x=[struct.unpack('f',struct.pack('f',v))[0] for v in x];value=model['baseScore']
        for t in model['trees']:
            node=0
            while t['left'][node]>=0:node=t['left'][node] if x[t['feature'][node]]<t['threshold'][node] else t['right'][node]
            value+=t['threshold'][node]
    else:raise ValueError('unsupported model')
    if not math.isfinite(value):raise ValueError('nonfinite inference')
    return max(-1.1,min(1.1,value))

def fit_xgboost(rows):
    import numpy as np
    from xgboost import XGBRegressor
    x=np.array([[r['features'][k] for k in NAMES] for r in rows],dtype=np.float32);y=np.array([r['netReturnPerShare'] for r in rows],dtype=np.float32)
    estimator=XGBRegressor(n_estimators=48,max_depth=2,learning_rate=.05,min_child_weight=10,reg_lambda=10.,
        objective='reg:squarederror',random_state=16,n_jobs=1,tree_method='hist')
    estimator.fit(x,y);raw=json.loads(estimator.get_booster().save_raw(raw_format='json'))['learner']
    base=json.loads(raw['learner_model_param']['base_score']);base=float(base[0] if isinstance(base,list) else base)
    trees=[]
    for t in raw['gradient_booster']['model']['trees']:
        if any(t.get('split_type',[])):raise ValueError('unsupported categorical trees')
        trees.append({'left':t['left_children'],'right':t['right_children'],'feature':t['split_indices'],'threshold':t['split_conditions']})
    model={'kind':'xgboost','baseScore':base,'trees':trees}
    if max(abs(float(a)-predict(model,r['features'])) for a,r in zip(estimator.predict(x[:30]),rows[:30]))>1e-5:raise ValueError('portable inference mismatch')
    return model

def clean(rows):
    result=[];seen=set();rejected=0
    for r in rows:
        try:
            if r['id'] in seen:continue
            seen.add(r['id']);f=r['features'];t=float(r['featuresAt']);end=float(r['labelAt']);start=float(r['roundStart'])
            valid=r.get('schema')==SCHEMA and r.get('policy')==POLICY and r.get('labelStatus') in ('filled','no_fill','censored_loss')
            valid=valid and r['asset'] in CAPS and float(r['availableAt'])<=t+1e-6 and start<=t<end<start+300
            valid=valid and all(math.isfinite(float(f[k])) and abs(float(f[k]))<=1e9 for k in NAMES)
            valid=valid and math.isfinite(float(r['netReturnPerShare'])) and -1.1<=float(r['netReturnPerShare'])<=1.1
            if not valid:raise ValueError('invalid row')
            result.append(r)
        except (KeyError,TypeError,ValueError,OverflowError):rejected+=1
    return sorted(result,key=lambda r:r['featuresAt']),rejected

def split_rounds(rows):
    groups=sorted({float(r['roundStart']) for r in rows})
    if len(groups)<5:return None
    a=max(1,int(len(groups)*.6));b=min(len(groups)-1,max(a+1,int(len(groups)*.8)));edges=(groups[a],groups[b])
    return [[r for r in rows if lo<=r['roundStart']<hi and r['labelAt']<hi] for lo,hi in ((-math.inf,edges[0]),(edges[0],edges[1]),(edges[1],math.inf))]

def metrics(rows,model,haircut=0.):
    selected=[];used=set();round_pnl={};equity=peak=dd=0.
    for r in sorted(rows,key=lambda r:(r['featuresAt'],-predict(model,r['features']))):
        f=r['features'];key=(r['asset'],r['marketSlug']);q=r['quantity'];p=f['entry_vwap']
        if key in used or q*(p+.07*p*(1-p))>CAPS[r['asset']]+1e-9:continue
        if f['friction']+max(.01,f['stop_per_share']*.25)>f['stop_per_share']+1e-9:continue
        if predict(model,f)-haircut<=0:continue
        used.add(key);selected.append(r);pnl=q*r['netReturnPerShare'];equity+=pnl;peak=max(peak,equity);dd=max(dd,peak-equity)
        round_pnl[r['roundStart']]=round_pnl.get(r['roundStart'],0.)+pnl
    values=[round_pnl.get(g,0.) for g in sorted({r['roundStart'] for r in rows})]
    mean=statistics.mean(values) if values else 0.;se=statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else 1.
    return {'rounds':len(values),'samples':len(rows),'selectedOrders':len(selected),'filledOrders':sum(bool(r.get('entryFilled')) for r in selected),
        'netPnl':round(equity,6),'maxDrawdownUsd':round(dd,6),'roundMeanMinus2SE':round(mean-2*se,6),
        'mse':sum((predict(model,r['features'])-r['netReturnPerShare'])**2 for r in rows)/max(1,len(rows)),
        'oneOrderPerAssetPerRound':True,'censoredLabels':sum(r['labelStatus']=='censored_loss' for r in rows)}

def train(rows,xgboost=False):
    rows,rejected=clean(rows);parts=split_rounds(rows)
    report={'schema':SCHEMA,'policy':POLICY,'createdAt':time.time(),'samples':len(rows),'rejectedRows':rejected,
        'executionEnabled':False,'semanticWeight':0.,'horizonSec':30,'state':'insufficient_data',
        'minimumRounds':{'train':20,'validation':10,'test':10},'reason':'Need five complete UTC rounds and ten samples in every partition to fit a research candidate.'}
    if not parts or any(len(p)<10 for p in parts):return {'report':report,'model':None,'featureNames':list(NAMES)}
    tr,val,test=parts
    models={'no_trade':{'kind':'constant','value':0.},'constant_mean':{'kind':'constant','value':statistics.mean(r['netReturnPerShare'] for r in tr)},'ridge':fit_ridge(tr)}
    if xgboost:models['xgboost']=fit_xgboost(tr)
    validation={name:metrics(val,m) for name,m in models.items()};winner=min(models,key=lambda n:validation[n]['mse']);model=models[winner]
    errors=sorted(predict(model,r['features'])-r['netReturnPerShare'] for r in val);haircut=max(0.,errors[math.ceil(.9*len(errors))-1])
    testing={name:metrics(test,m,haircut if name==winner else 0.) for name,m in models.items()}
    sizes={name:len({r['roundStart'] for r in part}) for name,part in zip(('train','validation','test'),parts)}
    enough=all(sizes[k]>=report['minimumRounds'][k] for k in sizes);score=testing[winner]
    report.update(state='shadow_candidate',selectedModel=winner,reason='Research candidate only; no automatic order promotion.',partitionRounds=sizes,
        windows={name:{'firstFeatureAt':min(r['featuresAt'] for r in part),'lastLabelAt':max(r['labelAt'] for r in part)} for name,part in zip(('train','validation','test'),parts)},
        validation=validation,test=testing,empiricalErrorHaircut=haircut,enoughIndependentRounds=enough,
        evidenceGatePassed=bool(enough and score['filledOrders']>=20 and score['netPnl']>0 and score['roundMeanMinus2SE']>0),
        dataHash=hashlib.sha256(json.dumps([(r['id'],r['netReturnPerShare']) for r in rows],separators=(',',':')).encode()).hexdigest(),
        limitations=['Historical holdout is not live execution.','Error haircut is empirical, not a guaranteed confidence interval.',
            'Censored filled probes receive full entry-cost losses.','Market rounds group correlated assets and overlapping probes.'])
    return {'report':report,'model':model,'featureNames':list(NAMES)}

def infer(artifact,row,now):
    empty={'state':'collecting_data','executionEnabled':False,'expectedNetReturnPerShare':None,'conservativeNetReturnPerShare':None}
    if not artifact or not artifact.get('model'):return empty
    report=artifact['report']
    if report.get('schema')!=SCHEMA or report.get('policy')!=POLICY or artifact.get('featureNames')!=list(NAMES):raise ValueError('incompatible model')
    if not 0<=now-row['availableAt']<=2 or not 0<=now-report['createdAt']<=21600:return {**empty,'state':'stale_model_or_features'}
    value=predict(artifact['model'],row['features']);haircut=report.get('empiricalErrorHaircut',1.)
    return {'state':'shadow_only','model':report.get('selectedModel'),'modelVersion':report.get('dataHash','')[:12],
        'expectedNetReturnPerShare':value,'conservativeNetReturnPerShare':value-haircut,'empiricalErrorHaircut':haircut,
        'evidenceGatePassed':report.get('evidenceGatePassed',False),'executionEnabled':False,'semanticWeight':0.,'horizonSec':30}
