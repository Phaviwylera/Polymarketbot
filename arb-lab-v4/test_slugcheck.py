import asyncio,tempfile,unittest
import poly_eventarb as ea,poly_eventmaker as em,poly_events as ev
class SlugCheckTests(unittest.TestCase):
    def test_pick_slug_rejects_other_markets(self):
        d=[{'slug':'other','closed':True,'outcomePrices':'["1","0"]'}]
        for mod in (ea,em,ev):
            self.assertIsNone(mod.pick_slug(d,'mine'));self.assertIsNone(mod.pick_slug({},'mine'))
            self.assertEqual(mod.pick_slug(d+[{'slug':'mine'}],'mine'),{'slug':'mine'})
    def test_s1_does_not_settle_on_another_market(self):
        a=ea.EventArb(root=tempfile.mkdtemp())
        async def fake(url,params=None):return [{'slug':'someone-else','closed':True,'outcomePrices':'["0","1"]'}]
        a.get=fake;a.s1_open['mine']={'slug':'mine','t':ea.REGISTERED_AT+1,'side':0,'cost':.95,'shares':5}
        asyncio.run(a.settle());self.assertIn('mine',a.s1_open);self.assertEqual(a.state.get('slugMismatch'),1)
    def test_unchecked_s1_rows_are_quarantined(self):
        a=ea.EventArb(root=tempfile.mkdtemp())
        a.closed.append({'lane':'S1','t':ea.REGISTERED_AT+1,'ret':-1.,'pnl':-5.,'pay':0.})
        a.closed.append({'lane':'S1','t':ea.REGISTERED_AT+2,'ret':.04,'pnl':.2,'pay':1.,'checked':True})
        s=a.report()['S1'];self.assertEqual((s['n'],s['quarantined']),(1,1))
