"""Rival study (read-only): how do the profitable public up/down bots actually make money?

Accounts whose sellers publish live profiles (polycatalog.io listings): @norm1e69, @GoodFallen, @98euf98a
(PolymTradeBot). All three say they buy BOTH sides of BTC/ETH 5m/15m rounds and hold to settlement, with much of
the profit from Polymarket's maker/taker rebates. This module checks that from their public trades:
  * username -> proxy wallet (Gamma public-search, then the leaderboard, then the profile page),
  * every fill in the last LOOKBACK_DAYS from the data API (takerOnly=false), and the taker-only subset, so each fill
    is labelled maker or taker,
  * per round: shares and VWAP bought on UP and DOWN, matched pairs and the PAIR COST (VWAP up + VWAP down: under $1
    means the spread itself pays), unmatched exposure, when in the window they buy, sells, MERGE activity,
  * per settled round (winner from Gamma, recent rounds only): P&L before fees, taker fees paid
    (0.07 p(1-p) per taker share), estimated maker rebate (20% of crypto taker fees, i.e. 0.014 p(1-p) per maker
    share), and the taker rebate they would need to break even.
Nothing is copied or traded; it is evidence for designing our own (paper) lanes. Off with POLY_RIVALS=0;
names via POLY_RIVALS="name1,name2" or explicit wallets "name:0x...".
"""
from __future__ import annotations
import asyncio,json,math,os,re,time
from collections import defaultdict

GAMMA='https://gamma-api.polymarket.com';DATA='https://data-api.polymarket.com'
LOOKBACK_DAYS=3;MAX_FILLS=6000;MAX_SETTLE=150;EVERY_SEC=6*3600
TAKER_FEE=0.07;MAKER_REBATE_SHARE=0.20
UPDOWN=re.compile(r'^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d+)$')
ADDR=re.compile(r'0x[a-fA-F0-9]{40}')
CURRENT=None
def tfee(p):return TAKER_FEE*p*(1-p)

def analyse(fills,taker_keys,winners):
    """fills: data-api trade rows for one wallet (BUY/SELL, outcome Up/Down, price, size, timestamp, slug).
    taker_keys: set of keys of fills that were taker fills. winners: slug -> 'UP'/'DOWN' for settled rounds."""
    rounds=defaultdict(lambda:{'UP':[0.,0.],'DOWN':[0.,0.],'sold':0.,'soldShares':0.,'first':None,'makerSh':0.,'takerSh':0.,'fees':0.,'rebate':0.,'n':0})
    for f in fills:
        m=UPDOWN.match(str(f.get('slug') or ''))
        if not m:continue
        try:p=float(f['price']);sz=float(f['size']);ts=float(f['timestamp'])
        except (KeyError,TypeError,ValueError):continue
        ts=ts/1000 if ts>1e12 else ts
        out=str(f.get('outcome') or '').upper();side=str(f.get('side') or '').upper()
        if out not in ('UP','DOWN') or side not in ('BUY','SELL'):continue
        r=rounds[f['slug']];r['n']+=1;start=float(m.group(3));win=300. if m.group(2)=='5m' else 900.
        frac=(ts-start)/win
        r['first']=frac if r['first'] is None else min(r['first'],frac)
        key=_key(f);taker=key in taker_keys
        if taker:r['takerSh']+=sz;r['fees']+=sz*tfee(p)
        else:r['makerSh']+=sz;r['rebate']+=sz*TAKER_FEE*MAKER_REBATE_SHARE*p*(1-p)
        if side=='BUY':r[out][0]+=sz;r[out][1]+=sz*p
        else:r['sold']+=sz*p;r['soldShares']+=sz;r[out][0]-=sz;r[out][1]-=sz*p   # net position
    out=[];settled=[]
    for slug,r in rounds.items():
        u,d=r['UP'],r['DOWN'];vu=u[1]/u[0] if u[0]>1e-9 else None;vd=d[1]/d[0] if d[0]>1e-9 else None
        matched=min(max(u[0],0),max(d[0],0))
        row={'slug':slug,'upShares':round(u[0],2),'downShares':round(d[0],2),'vwapUp':None if vu is None else round(vu,4),
             'vwapDown':None if vd is None else round(vd,4),'pairCost':round(vu+vd,4) if (vu is not None and vd is not None) else None,
             'matched':round(matched,2),'unmatched':round(abs(u[0]-d[0]),2),'firstBuyFrac':None if r['first'] is None else round(r['first'],3),
             'makerShare':round(r['makerSh']/(r['makerSh']+r['takerSh']),3) if (r['makerSh']+r['takerSh'])>0 else None,
             'takerFees':round(r['fees'],4),'makerRebateEst':round(r['rebate'],4),'fills':r['n'],'soldUsd':round(r['sold'],2)}
        w=winners.get(slug)
        if w in ('UP','DOWN'):
            pay=max(r[w][0],0);cost=u[1]+d[1]
            row['winner']=w;row['pnlBeforeFees']=round(pay-cost,4)   # cost is net of anything sold (sells reduce it)
            row['pnlAfterFees']=round(row['pnlBeforeFees']-r['fees'],4);row['pnlWithMakerRebate']=round(row['pnlAfterFees']+r['rebate'],4)
            settled.append(row)
        out.append(row)
    return out,settled

PHASES=((0,.2,'0-20%'),(.2,.5,'20-50%'),(.5,.8,'50-80%'),(.8,1.01,'80-100%'))
PBUCKETS=((0,.2,'0-20c'),(.2,.4,'20-40c'),(.4,.6,'40-60c'),(.6,.8,'60-80c'),(.8,1.01,'80-100c'))
def fingerprint(fills,taker_keys):
    """Behaviour fingerprint from fills: leg timing, which leg first, volume/price/taker share by window phase and by
    price bucket, clip sizes, fills per round, sells. Answers: how and when they buy, maker or taker, and at what prices."""
    by=defaultdict(list)
    for f in fills:
        m=UPDOWN.match(str(f.get('slug') or ''))
        if not m:continue
        try:p=float(f['price']);sz=float(f['size']);ts=float(f['timestamp'])
        except (KeyError,TypeError,ValueError):continue
        ts=ts/1000 if ts>1e12 else ts;win=300. if m.group(2)=='5m' else 900.
        by[f['slug']].append({'ts':ts,'frac':(ts-float(m.group(3)))/win,'p':p,'sz':sz,'out':str(f.get('outcome') or '').upper(),
                              'side':str(f.get('side') or '').upper(),'taker':_key(f) in taker_keys,'win':win})
    lags=[];first_cheap=[];clips=[];nfills=[];phase=defaultdict(lambda:[0.,0.,0.,0]);pb=defaultdict(lambda:[0.,0.,0])
    sells=0.;buys=0.;same_ts=0;rounds5=0;rounds15=0
    for slug,fs in by.items():
        fs.sort(key=lambda x:x['ts']);b=[x for x in fs if x['side']=='BUY'];nfills.append(len(fs))
        if fs and fs[0]['win']==300.:rounds5+=1
        else:rounds15+=1
        fu=next((x for x in b if x['out']=='UP'),None);fd=next((x for x in b if x['out']=='DOWN'),None)
        if fu and fd:
            lags.append(abs(fu['ts']-fd['ts']));same_ts+=int(abs(fu['ts']-fd['ts'])<=2)
            first=fu if fu['ts']<=fd['ts'] else fd;first_cheap.append(first['p']<0.5)
        for x in fs:
            if x['side']=='SELL':sells+=x['sz']*x['p'];continue
            buys+=x['sz']*x['p'];clips.append(x['sz'])
            for lo,hi,name in PHASES:
                if lo<=x['frac']<hi:ph=phase[name];ph[0]+=x['sz'];ph[1]+=x['sz']*x['p'];ph[2]+=x['sz'] if x['taker'] else 0.;ph[3]+=1
            for lo,hi,name in PBUCKETS:
                if lo<=x['p']<hi:q=pb[name];q[0]+=x['sz'];q[1]+=x['sz'] if x['taker'] else 0.;q[2]+=1
    med=lambda v:sorted(v)[len(v)//2] if v else None
    pct=lambda v,q:sorted(v)[min(len(v)-1,int(q*len(v)))] if v else None
    totsh=sum(v[0] for v in phase.values()) or 1.
    return {'rounds5m':rounds5,'rounds15m':rounds15,'legLagMedianSec':None if not lags else round(med(lags),1),
            'legsWithin2sPct':round(100*same_ts/len(lags),1) if lags else None,
            'firstLegCheapPct':round(100*sum(first_cheap)/len(first_cheap),1) if first_cheap else None,
            'clipMedian':None if not clips else round(med(clips),2),'clipP90':None if not clips else round(pct(clips,.9),2),
            'fillsPerRoundMedian':med(nfills),'sellShareOfVolumePct':round(100*sells/(sells+buys),1) if (sells+buys) else None,
            'byPhase':{n:{'sharePct':round(100*v[0]/totsh,1),'avgPrice':round(v[1]/v[0],3) if v[0] else None,
                          'takerPct':round(100*v[2]/v[0],1) if v[0] else None,'fills':v[3]} for n,v in phase.items()},
            'byPrice':{n:{'sharePct':round(100*v[0]/totsh,1),'takerPct':round(100*v[1]/v[0],1) if v[0] else None,'fills':v[2]} for n,v in pb.items()}}

def _key(f):return (str(f.get('transactionHash') or ''),str(f.get('asset') or ''),str(f.get('size')),str(f.get('price')),str(f.get('side')))

def summarize(rows,settled):
    def avg(v):v=[x for x in v if x is not None];return round(sum(v)/len(v),4) if v else None
    two=[r for r in rows if r['upShares']>0 and r['downShares']>0]
    tot=lambda k,rs:round(sum(r[k] for r in rs),2)
    s={'rounds':len(rows),'bothSidesPct':round(100*len(two)/len(rows),1) if rows else None,
       'avgPairCost':avg([r['pairCost'] for r in two]),'pairCostUnder1Pct':round(100*sum(1 for r in two if r['pairCost'] is not None and r['pairCost']<1)/len(two),1) if two else None,
       'avgUnmatchedShares':avg([r['unmatched'] for r in rows]),'avgMatchedShares':avg([r['matched'] for r in rows]),
       'medianFirstBuyFrac':sorted([r['firstBuyFrac'] for r in rows if r['firstBuyFrac'] is not None])[len(rows)//2] if rows else None,
       'makerSharePct':None,'settledRounds':len(settled)}
    mk=sum((r['makerShare'] or 0)*(r['upShares']+r['downShares']) for r in rows);al=sum(r['upShares']+r['downShares'] for r in rows if r['makerShare'] is not None)
    s['makerSharePct']=round(100*mk/al,1) if al else None
    if settled:
        ex=[r for r in settled if abs(r['upShares']-r['downShares'])>1e-6]
        on_win=[r for r in ex if (r['upShares']>r['downShares'])==(r['winner']=='UP')]
        # split P&L: matched pairs (pay 1 each) vs the unmatched excess (pays 1 only when it is on the winner)
        pair_pnl=sum(r['matched']*(1-(r['pairCost'] or 1)) for r in settled if r['pairCost'] is not None)
        s.update({'excessOnWinnerPct':round(100*len(on_win)/len(ex),1) if ex else None,'roundsWithExcess':len(ex),
                  'pairPnlBeforeFees':round(pair_pnl,2)})
        s.update({'pnlBeforeFees':tot('pnlBeforeFees',settled),'takerFees':tot('takerFees',settled),'pnlAfterFees':tot('pnlAfterFees',settled),
                  'makerRebateEst':tot('makerRebateEst',settled),'pnlWithMakerRebate':tot('pnlWithMakerRebate',settled),
                  'winningRoundsPct':round(100*sum(1 for r in settled if r['pnlBeforeFees']>0)/len(settled),1)})
        need=s['takerFees'] and -(s['pnlAfterFees']+s['makerRebateEst'])/s['takerFees']
        s['takerRebateNeededToBreakEvenPct']=None if not s['takerFees'] else round(100*max(0.,need),1)
    return s

class Rivals:
    def __init__(self,http=None,now=time.time):
        self.http=http;self.now=now;self.enabled=os.getenv('POLY_RIVALS','1')!='0'
        spec=os.getenv('POLY_RIVALS_NAMES','norm1e69,GoodFallen,98euf98a')
        self.names=[x.strip() for x in spec.split(',') if x.strip()];self.wallets={};self.results={};self.winners={}
        self.state={'runs':0,'errors':0,'lastError':None,'lastRunAt':None}
    async def _c(self):
        if self.http is None:
            import httpx;self.http=httpx.AsyncClient(timeout=25,headers={'User-Agent':'poly-rival-study'},follow_redirects=True)
        return self.http
    async def get(self,url,params=None):
        r=await (await self._c()).get(url,params=params);r.raise_for_status();return r.json()
    def _err(self,where,exc):self.state['errors']+=1;self.state['lastError']=f'{where}: {type(exc).__name__}: {str(exc)[:100]}'
    async def resolve(self,name):
        if ':' in name:n,a=name.split(':',1);return n,a.lower()
        try:
            d=await self.get(GAMMA+'/public-search',{'q':name,'search_profiles':'true','limit_per_type':10})
            for p in (d or {}).get('profiles') or []:
                if str(p.get('name') or p.get('pseudonym') or '').lower()==name.lower() and ADDR.fullmatch(str(p.get('proxyWallet') or '')):
                    return name,p['proxyWallet'].lower()
        except Exception as exc:self._err('search',exc)
        try:
            for cat in ('CRYPTO','OVERALL'):
                d=await self.get(DATA+'/v1/leaderboard',{'category':cat,'timePeriod':'MONTH','orderBy':'PNL','limit':200})
                for row in d or []:
                    if str(row.get('userName') or '').lower()==name.lower():return name,str(row['proxyWallet']).lower()
        except Exception as exc:self._err('leaderboard',exc)
        try:
            r=await (await self._c()).get(f'https://polymarket.com/@{name}');txt=r.text
            m=re.search(r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"',txt)
            if m:return name,m.group(1).lower()
        except Exception as exc:self._err('profile',exc)
        return name,None
    async def fills(self,addr,taker_only):
        out=[];off=0;cut=self.now()-LOOKBACK_DAYS*86400
        while off<MAX_FILLS:
            d=await self.get(DATA+'/trades',{'user':addr,'limit':500,'offset':off,'takerOnly':'true' if taker_only else 'false'})
            rows=d if isinstance(d,list) else (d or {}).get('data') or []
            if not rows:break
            out+=rows;off+=len(rows)
            ts=float(rows[-1].get('timestamp') or 0);ts=ts/1000 if ts>1e12 else ts
            if len(rows)<500 or ts<cut:break
            await asyncio.sleep(.3)
        return [f for f in out if (float(f.get('timestamp') or 0)/(1000 if float(f.get('timestamp') or 0)>1e12 else 1))>=cut]
    async def merges(self,addr):
        try:
            d=await self.get(DATA+'/activity',{'user':addr,'type':'MERGE','limit':500})
            return len(d if isinstance(d,list) else [])
        except Exception as exc:self._err('activity',exc);return None
    async def winner(self,slug):
        if slug in self.winners:return self.winners[slug]
        d=await self.get(GAMMA+'/markets',{'slug':slug,'closed':'true'})
        m=d[0] if isinstance(d,list) and d else None
        if not m:return None
        outs=json.loads(m['outcomes']) if isinstance(m.get('outcomes'),str) else m.get('outcomes')
        pr=[float(x) for x in (json.loads(m['outcomePrices']) if isinstance(m.get('outcomePrices'),str) else m.get('outcomePrices') or [])]
        if not outs or len(pr)!=2 or max(pr)<.99:return None
        w=str(outs[pr.index(max(pr))]).upper();self.winners[slug]=w
        if len(self.winners)>5000:self.winners.pop(next(iter(self.winners)))
        return w
    async def run(self):
        self.state['runs']+=1;self.state['lastRunAt']=self.now()
        for name in self.names:
            try:
                n,addr=await self.resolve(name)
                if not addr:self.results[n]={'error':'wallet not found'};continue
                self.wallets[n]=addr
                allf=await self.fills(addr,False);tk=await self.fills(addr,True)
                tkeys={_key(f) for f in tk}
                slugs=sorted({f.get('slug') for f in allf if UPDOWN.match(str(f.get('slug') or ''))},
                             key=lambda s:-int(UPDOWN.match(s).group(3)))
                now=self.now();wins={}
                for s in [s for s in slugs if int(UPDOWN.match(s).group(3))+900+600<now][:MAX_SETTLE]:
                    try:
                        w=await self.winner(s)
                        if w:wins[s]=w
                    except Exception as exc:self._err('winner',exc)
                    await asyncio.sleep(.15)
                rows,settled=analyse(allf,tkeys,wins)
                other=sum(1 for f in allf if not UPDOWN.match(str(f.get('slug') or '')))
                self.results[n]={'wallet':addr,'fills':len(allf),'takerFills':len(tk),'nonUpDownFills':other,'merges':await self.merges(addr),
                                 'lookbackDays':LOOKBACK_DAYS,**summarize(rows,settled),'fingerprint':fingerprint(allf,tkeys),
                                 'sample':sorted(settled,key=lambda r:r['slug'])[-5:]}
            except Exception as exc:self._err(name,exc);self.results[name]={'error':type(exc).__name__}
    def report(self):return {'enabled':self.enabled,**self.state,'names':self.names,'wallets':self.wallets,'results':self.results,
                             'notes':'read-only study of public accounts; P&L excludes their volume-tier taker rebates (up to 44-50% of taker fees at $4M-$10M monthly weighted volume)'}
    def compact(self):
        r=self.report();r['results']={k:{x:y for x,y in v.items() if x!='sample'} for k,v in r['results'].items()};return r
    async def loop(self,first_delay=300):
        global CURRENT;CURRENT=self
        await asyncio.sleep(first_delay)
        while True:
            try:await self.run()
            except Exception as exc:self._err('run',exc)
            await asyncio.sleep(EVERY_SEC)
