import tempfile,unittest,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
class StoreFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();server.DATA=Path(self.temp.name);server.initialize()
        with server.connect() as db:
            server.mutate(db,'/api/shift-open',{'opening':dict.fromkeys(server.CURRENCIES,0)})
            server.mutate(db,'/api/rates',{'EUR':.9,'EGP':50,'RUB':90})
            self.p=server.mutate(db,'/api/products',{'name':'قهوة Кофе Coffee','barcode':'123456','unit':'kg','price':10})['products'][0]
            for qty,cost in [(2,100),(3,150)]:server.mutate(db,'/api/batches',{'product':self.p['id'],'qty':qty,'cost':cost})
    def tearDown(self):self.temp.cleanup()
    def sale(self,**kw):
        d={'currency':'EGP','method':'cash','items':[{'id':self.p['id'],'qty':3}],'tender':2000};d.update(kw)
        with server.connect() as db:return server.checkout(db,d)

class StoreTests(StoreFixture,unittest.TestCase):
    def test_fifo_and_change(self):
        s=self.sale();self.assertEqual(s['cost_egp'],350);self.assertEqual(s['total'],1500);self.assertEqual(s['change'],500)
        self.assertEqual([b['qty'] for b in s['items'][0]['fifo']],[2,1])
        with server.connect() as db:self.assertEqual([b['remaining'] for b in server.snapshot(db)['batches']],[0,2000])
    def test_payment_restrictions(self):
        for method,currency in [('card','USD'),('card','EUR'),('transfer','EGP'),('transfer','USD')]:
            with self.assertRaises(ValueError):self.sale(method=method,currency=currency)
        self.assertEqual(self.sale(method='transfer',currency='RUB')['total'],2700)
    def test_insufficient_stock_rolls_back(self):
        with self.assertRaises(ValueError):self.sale(items=[{'id':self.p['id'],'qty':6}])
        with server.connect() as db:
            self.assertEqual(sum(b['remaining'] for b in server.snapshot(db)['batches']),5000)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)
    def test_late_failure_rolls_back(self):
        with self.assertRaises(ValueError):self.sale(tender=1)
        with server.connect() as db:self.assertEqual(sum(b['remaining'] for b in server.snapshot(db)['batches']),5000)
    def test_line_and_bill_discounts(self):
        s=self.sale(items=[{'id':self.p['id'],'qty':1,'discount':{'type':'percent','value':10}}],discount={'type':'fixed','value':25})
        self.assertEqual(s['total'],425);self.assertEqual(s['cost_egp'],100)
    def test_rates_preserve_history_and_fixed_prices(self):
        s=self.sale()
        with server.connect() as db:
            server.mutate(db,'/api/rates',{'EUR':.8,'EGP':60,'RUB':100});state=server.snapshot(db)
            old=json.loads(db.execute('SELECT detail FROM sales').fetchone()[0])
        self.assertEqual(old['total'],1500);self.assertEqual(old['rates']['EGP'],50)
        self.assertEqual(state['products'][0]['price'],10);self.assertEqual(state['batches'][0]['cost'],100)
    def test_weight_precision(self):
        self.assertEqual(self.sale(items=[{'id':self.p['id'],'qty':.125}])['total'],62.5)
        with self.assertRaises(ValueError):server.quantity(.0001,'kg')
        with self.assertRaises(ValueError):server.quantity(1.5,'piece')
    def test_invalid_discount_rolls_back(self):
        with self.assertRaises(ValueError):self.sale(discount={'type':'percent','value':101})
        with server.connect() as db:self.assertEqual(sum(b['remaining'] for b in server.snapshot(db)['batches']),5000)
    def test_edit_preserves_receipt(self):
        sale=self.sale()
        with server.connect() as db:
            server.mutate(db,'/api/product-update',{'id':self.p['id'],'name':'New name','barcode':'NEW123','price':12})
            old=json.loads(db.execute('SELECT detail FROM sales').fetchone()[0])
            self.assertEqual(server.snapshot(db)['products'][0]['price'],12)
        self.assertEqual(old['items'][0]['name'],'قهوة Кофе Coffee')
        self.assertEqual(old['items'][0]['price'],500)
    def test_nonfinite_cash_rejected(self):
        for value in ['NaN','Infinity','-Infinity']:
            with self.assertRaises(ValueError): self.sale(tender=value)
    def test_unconfigured_rates_block_sale(self):
        with server.connect() as db: db.execute("UPDATE settings SET value='false' WHERE key='configured'")
        with self.assertRaises(ValueError): self.sale()
    def test_backup(self):
        with server.connect() as db:r=server.mutate(db,'/api/backup',{})
        with server.sqlite3.connect(r['file']) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM products').fetchone()[0],1)
    def test_concurrent_stock(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt(_):
            try:self.sale();return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(attempt,range(2))),1)
        with server.connect() as db:self.assertEqual(sum(b['remaining'] for b in server.snapshot(db)['batches']),2000)
if __name__=='__main__':unittest.main()
