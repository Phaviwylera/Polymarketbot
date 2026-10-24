import unittest
import poly_audit as pa

def trade(**kw):
    t={'id':'t','strategy':'MOMENTUM_SCALP','quantity':5.,'entryPrice':.30,'exitPrice':.40,'grossPnlUsd':.5,
       'entryLatencyMs':360.,'entrySlippageUsd':0.,'stopOvershootUsd':0.,'accountingErrorUsd':0.,'holdSec':10.}
    t['feesUsd']=5*(pa.fee(t['entryPrice'])+pa.fee(t['exitPrice']));t.update(kw);return t

class ExecutionAuditTests(unittest.TestCase):
    def test_clean_trade_passes_every_check(self):
        r=pa.execution_audit([trade()])
        self.assertTrue(r['clean']);self.assertEqual(sum(r['flagCounts'].values()),0)
    def test_each_unrealistic_fill_is_flagged(self):
        r=pa.execution_audit([trade(id='fee',feesUsd=0.),trade(id='fast',entryLatencyMs=50.),trade(id='slip',entrySlippageUsd=.2),
                              trade(id='over',stopOvershootUsd=.2),trade(id='acct',accountingErrorUsd=.01),trade(id='hold',holdSec=90.),
                              trade(id='px',exitPrice=1.2,feesUsd=5*(pa.fee(.3)+pa.fee(1.2)))])
        self.assertFalse(r['clean'])
        for k,tid in (('feeMismatch','fee'),('latencyTooFast','fast'),('adverseSlippage','slip'),('stopOvershoot','over'),
                      ('accountingError','acct'),('holdTooLong','hold'),('priceOutOfRange','px')):
            self.assertIn(tid,r['flaggedIds'][k],k)

class WalkForwardTests(unittest.TestCase):
    def rows(self):
        # Training era: cheap entries (<40c) win, expensive lose. Test era: the reverse.
        out=[]
        for i in range(60):
            cheap=i%2==0;late=i>=pa.MIN_TRAIN
            win=(cheap and not late) or (not cheap and late)
            out.append({'entryAt':1000+i,'entry':.30 if cheap else .55,'estEdge':.2,'pnl':.5 if win else -.5,'mode':'MOMENTUM_SCALP'})
        return out
    def test_rules_are_chosen_on_earlier_trades_only(self):
        wf=pa.walk_forward(self.rows())
        self.assertEqual(wf['folds'][0]['chosenRule']['maxEntryPrice'],.35)   # what the past favoured
        self.assertLess(wf['outOfSample']['netPnlUsd'],0)                      # and it fails forward: no look-ahead
        self.assertGreater(wf['baselineTakeAll']['trades'],wf['outOfSample']['trades'])
    def test_verdict_fails_small_or_negative_samples(self):
        v=pa.verdict(pa.walk_forward(self.rows()))
        self.assertEqual(v['status'],'FAIL');self.assertTrue(any('out-of-sample trades' in r for r in v['reasons']))
    def test_verdict_passes_a_strong_long_record(self):
        rows=[{'entryAt':i,'entry':.3,'estEdge':.2,'pnl':.4 if i%4 else -.2,'mode':'MOMENTUM_SCALP'} for i in range(200)]
        self.assertEqual(pa.verdict(pa.walk_forward(rows))['status'],'PASS')

class StatsTests(unittest.TestCase):
    def test_bootstrap_is_reproducible_and_brackets_the_mean(self):
        x=[.5,-.3,.2,.1,-.4,.6,.0,.3]
        a=pa.bootstrap_mean(x);b=pa.bootstrap_mean(x)
        self.assertEqual(a,b);self.assertLess(a['lower95'],sum(x)/len(x));self.assertGreater(a['upper95'],sum(x)/len(x))
        self.assertIsNone(pa.bootstrap_mean([1.,2.]))
    def test_metrics(self):
        m=pa.metrics([1.,-.5,-.5,2.])
        self.assertEqual(m['trades'],4);self.assertEqual(m['winRatePct'],50.);self.assertEqual(m['maxDrawdownUsd'],1.)
        self.assertEqual(m['profitFactor'],3.)

class CalibrationTests(unittest.TestCase):
    def test_market_baseline_beats_a_miscalibrated_model(self):
        bets=[{'marketSlug':f'm{i}','side':'UP','t':i,'p':.8,'ask':.3,'won':i%10<3,'returnPerDollar':(1/.3-1) if i%10<3 else -1} for i in range(100)]
        c=pa.calibration(pa._Edge({'edge':bets,'edge_cal':bets}))   # duplicates across variants count once
        self.assertEqual(c['bets'],100);self.assertFalse(c['modelBeatsMarket']);self.assertLess(c['brierSkillVsMarket'],0)
        self.assertEqual(c['model']['reliability'][0]['actualWinPct'],30.)

class AuditV2Tests(unittest.TestCase):
    def test_settled_trades_are_not_fee_or_price_errors(self):
        t=trade(id='set',exitPrice=1.0,exitReason='verified settlement',holdSec=200.,feesUsd=5*pa.fee(.30))
        r=pa.execution_audit([t])
        self.assertTrue(r['clean']);self.assertEqual(r['marketRisk']['heldToSettlement'],1)
    def test_stop_overshoot_is_market_risk_not_an_error(self):
        r=pa.execution_audit([trade(stopOvershootUsd=.4)])
        self.assertTrue(r['clean']);self.assertEqual(r['marketRisk']['stopOvershoot'],1)
    def test_ledger_reconcile_catches_drift(self):
        ok={'sessionId':'a','startedAt':10,'trades':1,'realizedPnl':.5,'startingBalance':60,'cash':60.5,'events':[{'action':'SELL','pnl':.5}]}
        bad={'sessionId':'b','startedAt':10,'trades':2,'realizedPnl':.9,'startingBalance':60,'cash':60.0,'events':[{'action':'SELL','pnl':.5}]}
        r=pa.ledger_reconcile([{'lane':'BTC','live':True,'paper':ok},{'lane':'ETH','live':True,'paper':bad}],0.,0.)
        self.assertEqual(r['sessionsWithProblems'],1);self.assertEqual(r['problems'][0]['sessionId'],'b');self.assertEqual(len(r['problems'][0]['problems']),3)
    def test_window_counts_explain_the_24_vs_29_gap(self):
        rows=[{'entryAt':t,'pnl':.1} for t in (100,150,210,260,300)]
        w=pa.window_counts(rows,epoch=200,rules_live_at=120)
        self.assertEqual((w['sinceFreshStart'],w['sinceRulesLive'],w['betweenTheTwoTimes']),(3,4,1));self.assertTrue(w['explained'])
    def test_block_bootstrap_keeps_a_round_together(self):
        rows=[{'entryAt':r*300+k,'pnl':1. if r%2 else -1.} for r in range(10) for k in range(3)]
        b=pa.block_bootstrap_mean(rows);self.assertEqual(b['blocks'],10);self.assertEqual(b['trades'],30)
    def test_stress_makes_exits_worse(self):
        st=pa.stress([{'pnl':.3,'exit':.4},{'pnl':-.2,'exit':.2},{'pnl':.6,'exit':1.0}])
        self.assertLess(st['exitWorseBy1Tick']['netPnlUsd'],st['exitWorseBy0Tick']['netPnlUsd'])
        self.assertLess(st['exitWorseBy2Tick']['netPnlUsd'],st['exitWorseBy1Tick']['netPnlUsd'])
    def test_verdict_blocks_on_fragile_edge_and_errors(self):
        rows=[{'entryAt':i*300,'entry':.3,'estEdge':.2,'pnl':.4 if i%4 else -.2,'exit':.4,'mode':'MOMENTUM_SCALP'} for i in range(200)]
        wf=pa.walk_forward(rows);oos=wf.pop('_oosRows');wf['outOfSampleBlockBootstrap']=pa.block_bootstrap_mean(oos);wf['stressOutOfSample']=pa.stress(oos)
        self.assertEqual(pa.verdict(wf,{'clean':True,'errors':{}},{'sessionsWithProblems':0})['status'],'PASS')
        v=pa.verdict(wf,{'clean':False,'errors':{'feeMismatch':1}},{'sessionsWithProblems':2})
        self.assertEqual(v['status'],'FAIL');self.assertTrue(any('execution' in r for r in v['reasons']));self.assertTrue(any('ledger' in r for r in v['reasons']))
        wf['stressOutOfSample']={'exitWorseBy1Tick':{'avgPnlUsd':-.01}}
        self.assertTrue(any('1 tick worse' in r for r in pa.verdict(wf)['reasons']))

if __name__=='__main__':unittest.main()
