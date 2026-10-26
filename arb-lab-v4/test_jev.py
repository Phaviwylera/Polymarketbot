import asyncio,os,tempfile,unittest
import poly_jev as pj

class R:
    def __init__(self,code,d):self.status_code=code;self.d=d
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError(self.status_code)
    def json(self):return self.d
class Http:
    def __init__(self,p=.7,reject_object=False):self.p=p;self.calls=[];self.reject=reject_object
    async def post(self,url,headers=None,json=None):
        self.calls.append(json)
        if self.reject and isinstance(json['state'],dict):return R(400,{})
        return R(200,{'model':'typesafe/jev-1.14-x','answers':{'up_wins':{'type':'noul','noul':self.p}},'usage':{'cost':1e-6}})
T=pj.REGISTERED_AT+100
def row(lf,price=.5,token='UP',slug='btc-updown-5m-1'):
    return {'t':T+300*(1-lf),'lane':'BTC','slug':slug,'roundEnd':T+300,'token':token,'price':price,'leftFrac':lf,'secondsLeft':300*lf,
            'spotVsOpenBps':3.0,'ask':price+.01,'otherAsk':1-price+.01,'pLearned':.55}

class JevTests(unittest.TestCase):
    def test_off_without_key(self):
        self.assertFalse(pj.Jev(key='').enabled)
    def test_one_call_per_checkpoint_label_and_score(self):
        async def go():
            j=pj.Jev(root=tempfile.mkdtemp(),key='k',http=Http(.7),now=lambda:T)
            ts=[j.on_print(row(lf)) for lf in (.95,.79,.78,.5,.49,.2)]
            await asyncio.gather(*[t for t in ts if t])
            return j
        j=asyncio.run(go());self.assertEqual(j.state['calls'],3)                 # early, mid, late once each
        self.assertNotIn('upTokenPrice',str(j.http.calls[0]['questions']))      # question is fixed; context goes in state
        self.assertEqual(j.http.calls[0]['state']['upTokenPrice'],.5)
        self.assertEqual(j.settle('btc-updown-5m-1','up'),3)
        r=j.report();self.assertEqual(r['all']['n'],3);self.assertAlmostEqual(r['all']['brierJev'],.09,4);self.assertAlmostEqual(r['all']['brierPrice'],.25,4)
        self.assertEqual(r['H2']['trades'],3)                                    # 0.7 vs 0.5: buy UP at ask .51 + 1c
        self.assertAlmostEqual(r['H2']['avgReturnPerDollar'],pj.ret(.52,True),4)
        j.save();j2=pj.Jev(root=j.root,key='k');self.assertTrue(j2.load());self.assertEqual(len(j2.scored),3)
    def test_small_disagreement_is_not_a_trade_and_late_join_is_skipped(self):
        async def go():
            j=pj.Jev(root=tempfile.mkdtemp(),key='k',http=Http(.52),now=lambda:T)
            t=j.on_print(row(.3));                                               # joined at 30% left: early+mid skipped
            if t:await t
            return j
        j=asyncio.run(go());self.assertEqual(j.state['calls'],0)
        async def go2():
            t=j.on_print(row(.2));await t
        asyncio.run(go2());j.settle('btc-updown-5m-1','down')
        self.assertEqual(j.report()['H2']['trades'],0)
    def test_falls_back_to_list_state_format(self):
        async def go():
            j=pj.Jev(root=tempfile.mkdtemp(),key='k',http=Http(.6,reject_object=True),now=lambda:T)
            await j.on_print(row(.79));return j
        j=asyncio.run(go());self.assertEqual(j.state['stateFormat'],'list');self.assertEqual(len(j.pending['btc-updown-5m-1']),1)
    def test_daily_cap(self):
        async def go():
            j=pj.Jev(root=tempfile.mkdtemp(),key='k',http=Http(),now=lambda:T,max_calls=1)
            for k in range(3):
                t=j.on_print(row(.79,slug=f's{k}'))
                if t:await t
            return j
        self.assertEqual(asyncio.run(go()).state['calls'],1)
