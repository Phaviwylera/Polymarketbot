"""Resumable public-wallet archive. Python 3.10+; standard library only."""
import argparse, csv, gzip, hashlib, json, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request, zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

WALLET='0x31393e2f8333d4a6e2e31ca6424defe8b6cb9e2e'
BASE='https://data-api.polymarket.com'

def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def now():return datetime.now(timezone.utc).isoformat()
def number(value):
    try:return float(value)
    except (ValueError,TypeError):return 0.0

def request(path, params):
    url=BASE+path+'?'+urllib.parse.urlencode(params)
    for attempt in range(8):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'PolyWalletResearchArchive/1.0','Accept':'application/json'})
            with urllib.request.urlopen(req,timeout=45) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429,500,502,503,504):
                raise RuntimeError(f'HTTP {exc.code} from {path}. Access failed; existing pages are preserved. No complete-history claim can be made.') from exc
            delay=min(30,2**attempt)
            try:delay=min(60,float(exc.headers.get('Retry-After',delay)))
            except ValueError:pass
        except (urllib.error.URLError,TimeoutError):delay=min(30,2**attempt)
        if attempt==7:raise RuntimeError(f'{path}: retries exhausted; run again to resume')
        print(f'Retry {attempt+1}/8 in {delay}s',flush=True);time.sleep(delay)

def connect(root,wallet):
    root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(root/'wallet.sqlite3')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS metadata(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS checkpoint(kind TEXT PRIMARY KEY,cursor TEXT,page INTEGER,finished INTEGER);
CREATE TABLE IF NOT EXISTS pages(kind TEXT,page INTEGER,fingerprint TEXT,cursor TEXT,rows INTEGER,PRIMARY KEY(kind,page),UNIQUE(kind,fingerprint));
CREATE TABLE IF NOT EXISTS records(kind TEXT,fingerprint TEXT,raw TEXT,PRIMARY KEY(kind,fingerprint));''')
    old=db.execute("SELECT v FROM metadata WHERE k='wallet'").fetchone()
    if old and old[0]!=wallet:raise RuntimeError('This folder belongs to another wallet. Choose a different --out folder.')
    with db:db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('wallet',wallet))
    return db

def ingest(db,root,kind,wallet,fetch=request):
    checkpoint=db.execute('SELECT cursor,page,finished FROM checkpoint WHERE kind=?',(kind,)).fetchone()
    cursor,page,finished=checkpoint if checkpoint else (None,0,0)
    if finished:return
    path='/v2/trades' if kind=='trades' else '/v2/positions'
    rawdir=root/'raw'/kind;rawdir.mkdir(parents=True,exist_ok=True)
    while True:
        params={'user':wallet,'limit':1000}
        if kind=='trades':params['taker_only']='false'
        else:params['status']='CLOSED'
        if cursor:params['cursor']=cursor
        body=fetch(path,params)
        if not isinstance(body,dict) or not isinstance(body.get('data'),list) or not isinstance(body.get('pagination'),dict):
            raise RuntimeError('Unexpected response schema; refusing to label history complete.')
        rows=body['data'];pg=body['pagination']
        if not all(isinstance(row,dict) for row in rows):raise RuntimeError('Unexpected record schema')
        for row in rows:
            owner=row.get('proxy_wallet') or row.get('proxyWallet')
            if owner and str(owner).lower()!=wallet:raise RuntimeError('Wallet filter mismatch; archive stopped.')
        if 'has_more' not in pg:raise RuntimeError('Missing pagination completion flag')
        more=pg['has_more']
        if not isinstance(more,bool):raise RuntimeError('Unexpected pagination flag type')
        next_cursor=pg.get('next_cursor')
        if more and (not rows or not next_cursor or next_cursor==cursor):raise RuntimeError('Pagination made no progress; archive stopped without claiming completion.')
        if next_cursor and db.execute('SELECT 1 FROM pages WHERE kind=? AND cursor=?',(kind,next_cursor)).fetchone():
            raise RuntimeError('Repeated cursor detected; archive stopped.')
        # A response page has its own hash, independently of advertised row counts.
        fingerprint=hashlib.sha256(encode(rows).encode()).hexdigest()
        if db.execute('SELECT 1 FROM pages WHERE kind=? AND fingerprint=?',(kind,fingerprint)).fetchone():
            raise RuntimeError('Repeated data page detected; archive stopped. Counts are not unique fills.')
        page+=1
        dest=rawdir/f'{page:08d}.json.gz';tmp=dest.with_suffix('.tmp')
        with gzip.open(tmp,'wt',encoding='utf-8') as stream:json.dump(body,stream,ensure_ascii=False)
        tmp.replace(dest)
        before=db.total_changes
        with db:
            for row in rows:
                raw=encode(row);key=hashlib.sha256(raw.encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO records VALUES(?,?,?)',(kind,key,raw))
            inserted=db.total_changes-before
            db.execute('INSERT INTO pages VALUES(?,?,?,?,?)',(kind,page,fingerprint,next_cursor,len(rows)))
            db.execute('INSERT OR REPLACE INTO checkpoint VALUES(?,?,?,?)',(kind,next_cursor,page,int(not more)))
        print(f'{kind}: page {page:,}; received {len(rows):,}; new distinct records {inserted:,}; checkpoint saved',flush=True)
        cursor=next_cursor
        if not more:return
        if page%100==0:summary(db,root,wallet)
        time.sleep(.15)

def summary(db,root,wallet,errors=None):
    counts=dict(db.execute('SELECT kind,COUNT(*) FROM records GROUP BY kind'))
    checkpoints={k:{'pages':p,'apiExhausted':bool(done)} for k,p,done in db.execute('SELECT kind,page,finished FROM checkpoint')}
    roles=Counter();markets=defaultdict(set);buy=sell=0.;ts=[];position_pnl=0.;pnl_rows=0
    for (raw,) in db.execute("SELECT raw FROM records WHERE kind='trades'"):
        row=json.loads(raw);side=str(row.get('side','')).upper()
        amount=number(row.get('size'))*number(row.get('price'))
        if side=='BUY':buy+=amount
        elif side=='SELL':sell+=amount
        cond=row.get('condition_id') or row.get('conditionId')
        if cond and side=='BUY':markets[cond].add(str(row.get('outcome')))
        role=str(row.get('trader_side') or row.get('role') or '').upper()
        roles[role if role in ('MAKER','TAKER') else 'UNKNOWN']+=1
        stamp=number(row.get('timestamp'))
        if stamp:ts.append(stamp)
    for (raw,) in db.execute("SELECT raw FROM records WHERE kind='closed'"):
        row=json.loads(raw)
        value=row.get('realized_pnl',row.get('realizedPnl'))
        if value is not None:position_pnl+=number(value);pnl_rows+=1
    report={'wallet':wallet,'generatedAt':now(),'distinctRawRecords':counts,'coverage':checkpoints,'roles':dict(roles),
            'tradeTimestampRangeUtc':[datetime.fromtimestamp(v,timezone.utc).isoformat() for v in (min(ts),max(ts))] if ts else [],
            'grossBuyNotionalUsd':buy,'grossSellNotionalUsd':sell,'buyMarketsObserved':len(markets),
            'bothOutcomesBoughtMarketsObserved':sum(len(v)>=2 for v in markets.values()),
            'apiReportedClosedPositionPnlSumUsd':position_pnl if pnl_rows else None,'closedRowsWithPnl':pnl_rows,
            'errors':errors or [],'limitations':['Distinct raw records are deduplicated by full-record content; the API does not guarantee one record per unique economic fill.',
            'API pagination exhaustion is not independent proof of complete on-chain history or maker coverage.',
            'Gross BUY/SELL notionals exclude payouts, merges, fees, rebates and transfers; their difference is not profit.',
            'Buying both outcomes does not prove profitable pairing. Maker share is unavailable for records without an explicit role.',
            'Closed-position PnL is an API-reported aggregate, not independently reconstructed cash-flow profit.']}
    (root/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    lines=['# Wallet research — downloaded public data',f'Wallet: `{wallet}`',f'Generated: {report["generatedAt"]}',
           '',f'Distinct trade records: {counts.get("trades",0):,}',f'Distinct closed-position records: {counts.get("closed",0):,}',
           f'API pagination coverage: {encode(checkpoints)}',f'Observed BUY notional: ${buy:,.2f}',f'Observed SELL notional: ${sell:,.2f}',
           f'Markets with BUY records: {len(markets):,}',f'Markets with two or more bought outcomes: {report["bothOutcomesBoughtMarketsObserved"]:,}',
           f'Explicit roles: {dict(roles)}','', '## Limits','']+['- '+v for v in report['limitations']]
    if errors:lines+=['','## Retrieval errors','']+['- '+v for v in errors]
    lines+=['','This is a descriptive data report. It does not establish a profitable or copyable strategy.']
    (root/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    with zipfile.ZipFile(root/'Research_Summary.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for name in ['summary.json','REPORT.md']:archive.write(root/name,name)
    return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',default=r'D:\Phavi\PolyWalletResearch');parser.add_argument('--wallet',default=WALLET);parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    wallet=args.wallet.lower()
    if len(wallet)!=42 or not wallet.startswith('0x') or any(c not in '0123456789abcdef' for c in wallet[2:]):parser.error('Invalid public wallet address')
    root=Path(args.out);db=connect(root,wallet);errors=[]
    print('Public wallet:',wallet,'\nSaving to:',root.resolve(),'\nNo wallet keys or login are required.',flush=True)
    try:
        if not args.report_only:
            for kind in ('trades','closed'):
                try:ingest(db,root,kind,wallet)
                except Exception as exc:errors.append(kind+': '+str(exc));print(errors[-1],flush=True)
    except KeyboardInterrupt:errors.append('Interrupted by user. Run START_DOWNLOAD.cmd again to resume.')
    finally:
        summary(db,root,wallet,errors);db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
    print('\nSaved REPORT.md, summary.json and Research_Summary.zip. Upload Research_Summary.zip here for review.\nFull records remain in wallet.sqlite3 and raw/.',flush=True)
    return 1 if errors else 0

if __name__=='__main__':sys.exit(main())
