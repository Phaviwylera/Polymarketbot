"""Isolated causal maker-first / taker-hedge experiment. Never submits orders.

Sell volume consuming displayed queue is a fill proxy. Complete sets receive a
hypothetical $1 credit, not an actual merge, redemption or funded paper trade.
"""
import copy,math,uuid
from collections import Counter,deque
from poly_features import number,walk
POLICY='pair-maker-queue-taker-hedge-v3-guard'
QUOTE_TTL_SEC=20.        # v2 used 10s: 52 quotes in 2h, zero queue fills before expiry
MAX_ATTEMPTS_PER_ROUND=4 # v2 allowed one; more attempts gather evidence faster under the same fill rules
LATENCY=.35
GUARD_IMPULSE_BPS=.5     # 5s oracle impulse against the quoted side
GUARD_FAST_BPS=1.0       # 5s Binance/Coinbase move against the quoted side
MARKOUT_SEC=5.

def adverse(c,side,now):
    """A resting bid on `side` is picked off when the underlying moves against it. Uses the fastest
    fresh signals: Polymarket's oracle impulse and external exchange returns (each only if fresh)."""
    d=1 if side=='up' else -1;m=c.get('model') or {};hits=[]
    imp=m.get('impulse5Bps')
    if imp is not None and d*number(imp)<-GUARD_IMPULSE_BPS:hits.append(f'oracle impulse {number(imp):+.2f}bps')
    for name,r in (c.get('fast') or {}).items():
        if r is not None and d*number(r)<-GUARD_FAST_BPS:hits.append(f'{name} {number(r):+.2f}bps')
    return '; '.join(hits) or None

class PairShadow:
    def __init__(self,asset,balance,emit):
        self.asset=asset;self.balance=float(balance);self.emit=emit
        self.cash=self.balance;self.realized=0.;self.observed=0.;self.censored=0.;self.fees=0.
        self.peak=self.balance;self.drawdown=0.;self.counts=Counter();self.reasons=Counter()
        self.active=None;self.last_market=None;self.reason='warming';self.last_check=0.;self.recent=deque(maxlen=200)
        self.residual_cap=min(3.25,self.balance*.05);self.cycle_cap=min(8.,self.balance*.2)
        self.stop=min(.4,self.balance*.005);self.loss_limit=self.balance*.03
        self.markouts=deque(maxlen=200);self.attempts={}
    def event(self,kind,now,**data):
        self.emit('PAIR_'+kind,{'policy':POLICY,'asset':self.asset,'at':now,'executionEnabled':False,**data})
    def block(self,reason):self.reason=reason;self.reasons[reason]+=1
    @staticmethod
    def books(c,now):
        m=c.get('market') or {}
        if not m.get('acceptingOrders') or not m.get('slug'):return None,'market unavailable'
        if number(m.get('endTs'))-number(m.get('startTs'))!=300:return None,'five-minute markets only'
        if not number(m.get('startTs'))<=now<number(m.get('endTs')):return None,'outside market lifetime'
        if any(c.get('feed',{}).get(k)!='live' for k in ('clob','market')):return None,'book feed unavailable'
        for side in ('up','down'):
            t=number(c.get('bookObservedAt',{}).get(side),-1e9);lag=number(c.get('bookEventLagSec',{}).get(side),1e9)
            if t>now+1 or lag< -1 or max(0,now-t)+max(0,lag)>3:return None,'book stale or future'
            b=c.get('bbo',{}).get(side,{})
            if not 0<number(b.get('bid'))<number(b.get('ask'))<1:return None,'non-executable book'
            book=c.get('depth',{}).get(side,{})
            bids=[number(r.get('price')) for r in book.get('bids',[]) if number(r.get('size'))>0]
            asks=[number(r.get('price')) for r in book.get('asks',[]) if number(r.get('size'))>0]
            if not bids or not asks or abs(max(bids)-number(b['bid']))>1e-8 or abs(min(asks)-number(b['ask']))>1e-8:return None,'depth and BBO disagree'
        return c['depth'],'collecting'
    def close(self,now,status,proceeds,fees,reason):
        a=self.active;cost=a.get('entryCost',0.);net=proceeds-cost
        self.cash+=proceeds;self.realized+=net;self.fees+=fees
        if status=='censored_loss':self.censored+=net
        else:self.observed+=net
        self.counts[status]+=1;self.counts['completed']+=1
        row={'id':a['id'],'marketSlug':a['marketSlug'],'roundStart':a['start'],'side':a['side'],
             'quantity':a['quantity'],'createdAt':a['createdAt'],'entryAt':a.get('entryAt'),
             'closedAt':now,'status':status,'reason':reason,'entryCostUsd':cost,
             'proceedsUsd':proceeds,'cashFlowConvention':'proceeds net of hedge spend or exit fees','feesUsd':fees,'netPnlUsd':net,'accountingErrorUsd':net-(proceeds-cost),
             'maxUnhedgedSec':now-a.get('entryAt',now),'censored':status=='censored_loss','markout5s':a.get('markout'),
             'fillModel':'sell-volume queue proxy; no partial fills'}
        self.recent.append(row);self.event('OUTCOME',now,**row);self.active=None;self.reason=reason
        self.peak=max(self.peak,self.cash);self.drawdown=max(self.drawdown,self.peak-self.cash)
    def cancel(self,now,reason):
        a=self.active;self.counts['cancelled']+=1;self.event('CANCEL',now,id=a['id'],marketSlug=a['marketSlug'],reason=reason,queueConsumed=a['consumed']);self.active=None;self.reason=reason
    def step(self,c,now):
        book,why=self.books(c,now);m=c.get('market') or {};a=self.active
        if a:
            same=m.get('slug')==a['marketSlug'];gap=now-a['lastSeenAt']
            if not same or now>=a['end'] or gap>3:
                if a['phase']=='quote':self.cancel(now,'quote invalidated by gap or rollover')
                else:self.close(now,'censored_loss',0.,0.,'gap or rollover: full first-leg cost bound')
                return
            if book:a['lastSeenAt']=now
            if not book:self.reason=why;return
            q=a['quantity'];side=a['side'];opposite='down' if side=='up' else 'up'
            if a['phase']=='quote':
                if now-a['createdAt']>=QUOTE_TTL_SEC:self.cancel(now,'quote TTL');return
                bad=adverse(c,side,now)
                if bad:self.counts['guardCancels']+=1;self.cancel(now,'adverse-selection guard: '+bad);return
                if now-a['createdAt']<LATENCY:return
                if 'activatedAt' not in a:
                    if a['price']<min(number(p.get('price')) for p in book[side]['bids'] if number(p.get('size'))>0)-1e-9:
                        self.cancel(now,'quote queue beyond retained depth on activation');return
                    a['activatedAt']=now
                    a['lastSeq']=max([number(p.get('seq')) for p in c.get('tradePrints',[])]+[a['lastSeq']])
                    a['queueAhead']=max(a['queueAhead'],sum(number(p.get('size')) for p in book[side]['bids'] if number(p.get('price'))>=a['price']-1e-9))
                    if number(c['bbo'][side]['ask'])<=a['price']:self.cancel(now,'resting quote would cross on activation')
                    return
                hedge=walk(book[opposite]['asks'],q,True)
                for p in sorted(c.get('tradePrints',[]),key=lambda p:p.get('seq',0)):
                    seq=number(p.get('seq'))
                    if seq<=a['lastSeq']:continue
                    a['lastSeq']=seq
                    received=number(p.get('receivedAt'),now+1);t=number(p.get('t'),-1e9)
                    if p.get('marketSlug')!=a['marketSlug'] or p.get('side')!=side or str(p.get('aggressor')).upper()!='SELL':continue
                    if not a['activatedAt']<=t<=received<=now or received-t>3 or not p.get('feedReady'):continue
                    if number(p.get('price'),1)>a['price']+1e-9:continue
                    a['consumed']+=max(0,number(p.get('size')))
                if a['consumed']+1e-9<a['queueAhead']+q:
                    if a['price']<min(number(p.get('price')) for p in book[side]['bids'] if number(p.get('size'))>0)-1e-9:
                        self.cancel(now,'quote queue moved beyond retained depth');return
                    if not hedge or a['price']+hedge['vwap']+hedge['feePerShare']>.98+1e-9:self.cancel(now,'pair edge disappeared before fill')
                    return
                cost=q*a['price'];self.cash-=cost
                b=c['bbo'][side];a.update(phase='hedge',entryAt=now,entryCost=cost,fillMid=(number(b['bid'])+number(b['ask']))/2)
                self.counts['firstFills']+=1;self.event('FIRST_FILL',now,id=a['id'],marketSlug=a['marketSlug'],side=side,quantity=q,price=a['price'],queueAhead=a['queueAhead'],sellVolume=a['consumed'],entryCostUsd=cost)
                return
            if a['phase']=='hedge':
                if 'markout' not in a and now-a['entryAt']>=MARKOUT_SEC:
                    b=c['bbo'][side];a['markout']=(number(b['bid'])+number(b['ask']))/2-a['fillMid']
                    self.markouts.append(a['markout'])
                if now-a['entryAt']<LATENCY:return
                hedge=walk(book[opposite]['asks'],q,True)
                hedge_cost=q*(hedge['vwap']+hedge['feePerShare']) if hedge else math.inf
                if hedge and a['entryCost']+hedge_cost<=q*.98+1e-9 and hedge_cost<=self.cash+1e-9:
                    self.event('HEDGE_FILL',now,id=a['id'],marketSlug=a['marketSlug'],side=opposite,fill=hedge,netPairCost=(a['entryCost']+hedge_cost)/q)
                    self.close(now,'paired',q-hedge_cost,q*hedge['feePerShare'],'hypothetical complete-set credit minus hedge cost');return
                sell=walk(book[side]['bids'],q,False)
                net=q*(sell['vwap']-sell['feePerShare'])-a['entryCost'] if sell else None
                mark=self.cash+(q*(sell['vwap']-sell['feePerShare']) if sell else 0.)
                self.peak=max(self.peak,mark);self.drawdown=max(self.drawdown,self.peak-mark)
                if now-a['entryAt']>=15 or (net is not None and net<=-self.stop) or a['end']-now<=30:
                    a.update(phase='exit',exitRequestedAt=now,exitReason='unhedged timeout' if now-a['entryAt']>=15 else 'unhedged risk stop')
                    self.event('EXIT_REQUEST',now,id=a['id'],marketSlug=a['marketSlug'],reason=a['exitReason'],markedNetPnlUsd=net)
                return
            if a['phase']=='exit':
                if now-a['exitRequestedAt']>3:self.close(now,'censored_loss',0.,0.,'exit unavailable: full first-leg cost bound');return
                if now-a['exitRequestedAt']<LATENCY:return
                sell=walk(book[side]['bids'],q,False)
                if sell:self.close(now,'unwind',q*(sell['vwap']-sell['feePerShare']),q*sell['feePerShare'],a['exitReason'])
                return
        if now-self.last_check<1:return
        self.last_check=now
        if not book:self.block(why);return
        if self.realized<=-self.loss_limit:self.block('shadow loss budget reached');return
        if self.attempts.get(m['slug'],0)>=MAX_ATTEMPTS_PER_ROUND:self.block(f'{MAX_ATTEMPTS_PER_ROUND} shadow attempts this round');return
        if now-number(m['startTs'])<20 or number(m['endTs'])-now<=75:self.block('outside pair entry window');return
        q=max(5.,number(m.get('minOrderSize')));choices=[]
        for side in ('up','down'):
            other='down' if side=='up' else 'up';hedge=walk(book[other]['asks'],q,True)
            if adverse(c,side,now):self.counts['guardSkips']+=1;continue
            if not hedge or not walk(book[side]['bids'],q,False):continue
            tick=max(.001,number(m.get('minTickSize'),.01))
            ceiling=min(number(c['bbo'][side]['bid']),.98-hedge['vwap']-hedge['feePerShare'],self.residual_cap/q,.6)
            price=round(math.floor((ceiling+1e-12)/tick)*tick,6)
            # Queue beyond the retained depth is unknown: never invent a fill.
            lowest=min(number(p.get('price')) for p in book[side]['bids'] if number(p.get('size'))>0)
            if not 0<price<1 or price<lowest-1e-9:continue
            cost=q*(price+hedge['vwap']+hedge['feePerShare'])
            if price<=.6 and q*price<=self.residual_cap+1e-9 and cost<=min(self.cycle_cap,self.cash)+1e-9 and cost/q<=.98+1e-9:
                queue=sum(number(p.get('size')) for p in book[side]['bids'] if number(p.get('price'))>=price-1e-9)
                if queue>0:choices.append((cost,side,price,queue))
        if not choices:self.block('no executable pair margin within exposure budget');return
        cost,side,price,queue=min(choices);self.last_market=m['slug']
        self.attempts={m['slug']:self.attempts.get(m['slug'],0)+1}
        self.active={'id':uuid.uuid4().hex,'marketSlug':m['slug'],'start':m['startTs'],'end':m['endTs'],
            'createdAt':now,'lastSeenAt':now,'phase':'quote','side':side,'quantity':q,'price':price,'queueAhead':queue,'consumed':0.,
            'lastSeq':max([number(p.get('seq')) for p in c.get('tradePrints',[])]+[0.])}
        self.counts['quotes']+=1;self.reason='waiting for sell volume through queue'
        self.event('QUOTE',now,**copy.deepcopy(self.active),estimatedPairCostUsd=cost,residualCapUsd=self.residual_cap,cycleCapUsd=self.cycle_cap)
    def export_state(self):
        return {'policy':POLICY,'asset':self.asset,'balance':self.balance,'cash':self.cash,'realized':self.realized,
                'observed':self.observed,'censored':self.censored,'fees':self.fees,'peak':self.peak,'drawdown':self.drawdown,
                'counts':dict(self.counts),'reasons':dict(self.reasons),'last_market':self.last_market,
                'active':copy.deepcopy(self.active),'recent':list(self.recent)}
    def restore(self,state,now):
        if not state or state.get('policy')!=POLICY or state.get('asset')!=self.asset or state.get('balance')!=self.balance:return
        for k in ('cash','realized','observed','censored','fees','peak','drawdown'):
            if not math.isfinite(float(state[k])):raise ValueError('invalid pair checkpoint')
        for k in ('cash','realized','observed','censored','fees','peak','drawdown','last_market','active'):setattr(self,k,copy.deepcopy(state.get(k)))
        self.counts=Counter(state.get('counts',{}));self.reasons=Counter(state.get('reasons',{}));self.recent=deque(state.get('recent',[]),maxlen=200)
        if self.active:
            if self.active['phase']=='quote':self.cancel(now,'restart: queue continuity unavailable')
            else:self.close(now,'censored_loss',0.,0.,'restart: full first-leg cost bound')
    def snapshot(self):
        a=self.active or {};cost=a.get('entryCost',0.)
        return {'policy':POLICY,'mode':'SHADOW_ONLY','executionEnabled':False,'asset':self.asset,'virtualStartingBalance':self.balance,
            'virtualCash':self.cash,'conservativePnlUsd':self.realized,'observedProxyPnlUsd':self.observed,
            'censoredLossBoundUsd':self.censored,'feesUsd':self.fees,'maxDrawdownUsd':self.drawdown,
            'unhedgedCostUsd':cost,'unhedgedSince':a.get('entryAt'),'phase':a.get('phase','idle'),
            'counts':dict(self.counts),'reasonCounts':dict(self.reasons),'reason':self.reason,'active':copy.deepcopy(self.active),'recent':list(self.recent),
            'adverseSelection':{'guard':{'oracleImpulseBps':GUARD_IMPULSE_BPS,'exchangeReturnBps':GUARD_FAST_BPS},'markoutSec':MARKOUT_SEC,
                'fillsMeasured':len(self.markouts),'meanMarkout':(sum(self.markouts)/len(self.markouts)) if self.markouts else None,
                'note':'Mid-price change on the filled side 5s after a first-leg fill; negative means fills were picked off.'},
            'limits':{'cycleCapUsd':self.cycle_cap,'residualCapUsd':self.residual_cap,'stopUsd':self.stop,'lossLimitUsd':self.loss_limit,'maxUnhedgedSec':15,'quoteTtlSec':QUOTE_TTL_SEC,'attemptsPerRound':MAX_ATTEMPTS_PER_ROUND,'maxPairCost':.98},
            'assumptions':{'makerFee':0,'takerFee':'0.07*p*(1-p), assumed per share','rebates':0,'latencySec':LATENCY,'partialFills':False,'queueModel':'displayed queue plus full quantity consumed by fresh sell prints'},
            'note':'Isolated hypothetical cash; censored loss bounds are not observed losses. No promotion or real execution.'}


def wallet_research_summary():
    return {'status':'analysed','targetWallet':'0x31393e2f8333d4a6e2e31ca6424defe8b6cb9e2e',
        'reconciliation':{'feeModel':'0.07*p*(1-p) per-share crypto taker assumption; zero maker fee',
            'historicalFeesVerified':False,'makerTakerRolesKnown':False,'latencyHalfLifeEstimated':False},
        'findings':['Both outcomes were bought in 54,390 of 63,742 observed markets; this does not prove arbitrage.',
            'Lifetime average pair prices do not establish simultaneous executable prices.',
            'Reported closed-position profit is not independently reconciled net wallet cash profit.',
            'BTC/ETH five-minute patterns warrant separate shadow tests; results do not support equal sizing across all assets.'],
        'workstationMode':'PAPER_BASELINE_WITH_ISOLATED_PAIR_SHADOW','executionEnabled':False}
