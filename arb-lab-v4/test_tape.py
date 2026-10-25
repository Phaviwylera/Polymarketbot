import asyncio,gzip,json,os,tempfile,unittest
import poly_tape as pt

T=1_793_000_000.
def pr(side,price,t=T,slug='btc-updown-5m-1'):return {'t':t,'side':side,'price':price,'size':10.,'aggressor':'BUY','marketSlug':slug}
SIG={'pUp':0.70,'se':0.03,'at':T-1}

class DecideTests(unittest.TestCase):
    def test_buy_sell_skip(self):
        self.assertEqual(pt.decide(.70,.03,.55,1)[0],'BUY')      # 0.67 conservative vs 0.55 + fee + 2c
        self.assertEqual(pt.decide(.30,.03,.55,1)[0],'SELL')     # other side worth >= 0.67 vs 0.45
        self.assertEqual(pt.decide(.56,.03,.55,1)[0],'SKIP')
        self.assertEqual(pt.decide(.70,.03,.55,30)[2],'model stale')
        self.assertEqual(pt.decide(None,None,.55,1)[2],'model absent')

class TapeTests(unittest.TestCase):
    def test_every_print_is_scored_and_labelled_at_resolution(self):
        d=tempfile.mkdtemp();tape=pt.Tape(root=d,now=lambda:T+400)
        rows=[tape.on_print('BTC',pr('up',.55),{'slug':'btc-updown-5m-1','endTs':T+60},SIG,'oracle lead below'),
              tape.on_print('BTC',pr('down',.45),{'slug':'btc-updown-5m-1','endTs':T+60},SIG),
              tape.on_print('BTC',pr('up',.69),{'slug':'btc-updown-5m-1','endTs':T+60},SIG)]
        self.assertEqual([r['action'] for r in rows],['BUY','SELL','SKIP'])
        self.assertEqual(tape.report()['pendingPrints'],3)
        self.assertEqual(tape.settle('btc-updown-5m-1','up'),3)
        rep=tape.report();o=rep['overall']
        self.assertEqual(o['labelledPrints'],3);self.assertEqual(o['rounds'],1)
        buy=(1-(.55+pt.fee(.55)))/(.55+pt.fee(.55));self.assertAlmostEqual(o['actions']['BUY']['avgReturnPerDollar'],round(buy,4),4)
        self.assertGreater(o['actions']['SELL']['avgReturnPerDollar'],0)    # SELL DOWN = hold UP, and UP won
        self.assertIsNone(o['actions']['SKIP']['avgReturnPerDollar'])
        self.assertTrue(o['modelBeatsPrice'])                                # 0.7 for the winner beats 0.55 / 0.69
        files=[os.path.join(r,f) for r,_,fs in os.walk(d) for f in fs];self.assertEqual(len(files),1)
        with gzip.open(files[0],'rt') as f:lines=[json.loads(l) for l in f]
        self.assertEqual(len(lines),3);self.assertTrue(all('won' in l for l in lines))
    def test_unresolved_rounds_expire_without_labels(self):
        tape=pt.Tape(root=tempfile.mkdtemp(),now=lambda:T+4*3600)
        tape.on_print('ETH',pr('up',.5,slug='x'),{'slug':'x'},SIG);tape.expire()
        self.assertEqual(tape.report()['pendingPrints'],0);self.assertEqual(tape.dropped,1)
        self.assertEqual(tape.settle('x','up'),0)
    def test_upload_moves_closed_hours_to_s3(self):
        d=tempfile.mkdtemp();tape=pt.Tape(root=d,now=lambda:T+6*3600)
        tape.on_print('BTC',pr('up',.55,slug='s'),{'slug':'s'},SIG);tape.settle('s','down')
        put=[]
        class S3:
            async def put(self,path,key,sha):put.append(key)
        tape.s3=S3();n=asyncio.run(tape.upload_closed_hours())
        self.assertEqual(n,1);self.assertTrue(put[0].startswith('polybot/tape/'));self.assertEqual([f for _,_,fs in os.walk(d) for f in fs],[])
    def test_edgelab_settle_feeds_listeners(self):
        from poly_edge import EdgeLab
        lab=EdgeLab({});got=[];lab.settle_listeners=[lambda s,w:got.append((s,w))]
        lab.settle('slug-1','up');self.assertEqual(got,[('slug-1','up')])
