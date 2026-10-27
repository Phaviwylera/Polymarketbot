import asyncio,unittest
import poly_rivals as pr
S='btc-updown-5m-1792800000'
def f(out,side,price,size,ts=1792800100,h='h1'):return {'slug':S,'outcome':out,'side':side,'price':price,'size':size,'timestamp':ts,'transactionHash':h,'asset':out}
class RivalTests(unittest.TestCase):
    def test_pair_cost_maker_share_and_pnl(self):
        fills=[f('Up','BUY',.45,10,h='a'),f('Down','BUY',.52,10,ts=1792800160,h='b'),f('Up','BUY',.50,2,h='c')]
        tk={pr._key(fills[2])}                                               # only the last fill was a taker
        rows,settled=pr.analyse(fills,tk,{S:'DOWN'})
        r=rows[0];self.assertAlmostEqual(r['vwapUp'],(4.5+1)/12,4);self.assertEqual(r['matched'],10);self.assertEqual(r['unmatched'],2)
        self.assertAlmostEqual(r['firstBuyFrac'],100/300,3);self.assertAlmostEqual(r['makerShare'],20/22,3)
        x=settled[0];self.assertAlmostEqual(x['pnlBeforeFees'],10-(4.5+5.2+1.0),4)
        self.assertAlmostEqual(x['takerFees'],2*pr.tfee(.5),5);self.assertGreater(x['makerRebateEst'],0)
        s=pr.summarize(rows,settled);self.assertEqual(s['bothSidesPct'],100.);self.assertEqual(s['settledRounds'],1)
    def test_sells_reduce_cost_and_shares(self):
        rows,settled=pr.analyse([f('Up','BUY',.40,10,h='a'),f('Up','SELL',.60,4,h='b')],set(),{S:'UP'})
        self.assertAlmostEqual(settled[0]['pnlBeforeFees'],6-(4.0-2.4),4)
    def test_resolve_by_search_then_leaderboard(self):
        r=pr.Rivals()
        async def get(url,params=None):
            if url.endswith('/public-search'):return {'profiles':[{'name':'Other','proxyWallet':'0x'+'1'*40}]}
            return [{'userName':'norm1e69','proxyWallet':'0x'+'A'*40}]
        r.get=get;self.assertEqual(asyncio.run(r.resolve('norm1e69')),('norm1e69','0x'+'a'*40))
        self.assertEqual(asyncio.run(r.resolve('x:0xABC')),('x','0xabc'))
    def test_fingerprint(self):
        fills=[f('Up','BUY',.30,10,ts=1792800010,h='a'),f('Down','BUY',.68,10,ts=1792800011,h='b'),f('Up','BUY',.92,4,ts=1792800280,h='c'),f('Up','SELL',.95,2,ts=1792800290,h='d')]
        fp=pr.fingerprint(fills,{pr._key(fills[2])})
        self.assertEqual(fp['legLagMedianSec'],1.0);self.assertEqual(fp['legsWithin2sPct'],100.);self.assertEqual(fp['firstLegCheapPct'],100.)
        self.assertEqual(fp['byPhase']['80-100%']['takerPct'],100.);self.assertEqual(fp['byPrice']['20-40c']['fills'],1);self.assertGreater(fp['sellShareOfVolumePct'],0)
