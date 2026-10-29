from contextlib import closing
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from test_store import StoreFixture
import server

class OperationTests(StoreFixture,unittest.TestCase):
    def command(self, name, data):
        with server.connect() as db:
            return server.mutate(db, '/api/'+name, data)

    def current(self):
        with server.connect() as db:
            return server.snapshot(db)

    def close_shift(self):
        shift=self.current()['shifts'][0]
        return self.command('shift-close',{'id':shift['id'],'closing':{c:max(0,v) for c,v in shift['summary']['expected'].items()}})

    def test_shift_cash_differences_and_non_cash(self):
        self.sale(items=[{'id':self.p['id'],'qty':1}])
        self.sale(method='card',items=[{'id':self.p['id'],'qty':1}])
        self.command('cash-movement',{'currency':'EGP','amount':100,'reason':'Change float'})
        self.command('cash-movement',{'currency':'EGP','amount':-50,'reason':'Safe deposit'})
        shift=self.current()['shifts'][0]
        self.assertEqual(shift['summary']['expected']['EGP'],550)
        self.assertEqual(shift['summary']['payments']['EGP']['card'],500)
        self.command('shift-close',{'id':shift['id'],'closing':dict(USD=0,EUR=0,EGP=540,RUB=0)})
        s=self.current()['shifts'][0]
        self.assertEqual(s['summary']['difference']['EGP'],-10)
        with self.assertRaises(ValueError):self.sale()
        with self.assertRaises(ValueError):self.command('shift-close',{'id':s['id'],'closing':dict.fromkeys(server.CURRENCIES,0)})

    def test_only_one_shift(self):
        with self.assertRaises(sqlite3.IntegrityError):self.command('shift-open',{'opening':dict.fromkeys(server.CURRENCIES,0)})

    def test_void_restores_exact_fifo_and_is_once_only(self):
        s=self.sale()
        with self.assertRaises(ValueError):self.command('void',{'id':s['id'],'reason':'Wrong item','confirmed':False})
        self.command('void',{'id':s['id'],'reason':'Wrong item','confirmed':True})
        self.assertEqual([b['remaining'] for b in self.current()['batches']],[2000,3000])
        with self.assertRaises(ValueError):self.command('void',{'id':s['id'],'reason':'Again','confirmed':True})
        self.assertEqual(self.current()['shifts'][0]['summary']['expected']['EGP'],0)
        with server.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT detail FROM sales').fetchone()[0])['total'],1500)
            self.assertEqual(server.sales_list(db)[0]['void']['reason'],'Wrong item')

    def test_old_sale_void_does_not_change_closed_shift(self):
        s=self.sale();closed=self.close_shift()['shifts'][0]
        self.command('shift-open',{'opening':dict(USD=0,EUR=0,EGP=2000,RUB=0)})
        self.command('void',{'id':s['id'],'reason':'Error discovered','confirmed':True})
        shifts=self.current()['shifts']
        self.assertEqual(shifts[1]['summary'],closed['summary'])
        self.assertEqual(shifts[0]['summary']['expected']['EGP'],500)

    def test_fees_and_refund_policy(self):
        self.command('options',{'fees':{'card':{'percent':2,'fixed':1},'transfer':{'percent':1,'fixed':5}}})
        s=self.sale(method='card',items=[{'id':self.p['id'],'qty':1}])
        self.assertEqual(s['fee'],11)
        self.assertEqual(s['total'],500)
        self.command('void',{'id':s['id'],'reason':'Wrong item','confirmed':True})
        self.assertEqual(self.current()['shifts'][0]['summary']['fees']['EGP'],11)
        r=self.sale(method='transfer',currency='RUB',items=[{'id':self.p['id'],'qty':1}])
        self.assertEqual(r['fee'],14)
        self.command('void',{'id':r['id'],'reason':'Wrong item','confirmed':True,'fee_refunded':True})
        self.assertEqual(self.current()['shifts'][0]['summary']['fees']['RUB'],0)

    def test_count_shortage_surplus_writeoff_and_journal(self):
        self.command('adjust',{'product':self.p['id'],'kind':'count','qty':3,'expected_stock':5,'reason':'Count'})
        self.assertEqual([b['remaining'] for b in self.current()['batches']],[0,3000])
        self.command('adjust',{'product':self.p['id'],'kind':'count','qty':4,'cost':160,'expected_stock':3,'reason':'Found'})
        self.assertEqual(self.current()['products'][0]['stock_value'],610)
        self.command('adjust',{'product':self.p['id'],'kind':'writeoff','qty':.5,'expected_stock':4,'reason':'Damaged'})
        self.assertEqual(self.current()['products'][0]['stock'],3.5)
        self.assertEqual(self.current()['movements'][0]['qty'],-500)
        with self.assertRaises(ValueError):self.command('adjust',{'product':self.p['id'],'kind':'writeoff','qty':4,'expected_stock':3.5,'reason':'Damaged'})
        self.assertEqual(self.current()['products'][0]['stock'],3.5)

    def test_stale_count_rejected(self):
        with self.assertRaises(ValueError):self.command('adjust',{'product':self.p['id'],'kind':'count','qty':2,'expected_stock':4,'reason':'Count'})

    def test_preview_rounding_labels_checkout_identical(self):
        self.command('product-update',{'id':self.p['id'],'name':'Coffee','barcode':'123456','price':9.7472,'min_stock':5})
        preview=self.command('preview',{'options':{'rounding':dict(USD=.01,EUR=.01,EGP=5,RUB=.5)}})
        self.assertEqual(preview['products'][0]['new']['EGP'],490)
        self.assertEqual(self.current()['products'][0]['prices']['EGP'],487.5)
        self.command('reprice',{'options':{'rounding':preview['options']['rounding']},'revision':preview['revision']})
        s=self.sale(items=[{'id':self.p['id'],'qty':1}])
        self.assertEqual(s['items'][0]['price'],self.current()['products'][0]['prices']['EGP'])
        self.assertEqual(s['total'],490)

    def test_stale_prices_and_label_ack_rejected(self):
        state=self.current();p=state['products'][0]
        self.command('labels-confirm',{'items':[{'id':p['id'],'signature':p['signature']}]})
        self.assertFalse(self.current()['products'][0]['label_changed'])
        self.command('rates',{'EUR':.9,'EGP':55,'RUB':90})
        self.assertTrue(self.current()['products'][0]['label_changed'])
        with self.assertRaises(ValueError):self.sale(revision=state['revision'])
        with self.assertRaises(ValueError):self.command('labels-confirm',{'items':[{'id':p['id'],'signature':p['signature']}]})

    def test_external_backup_and_restore_roundtrip(self):
        self.close_shift()
        with tempfile.TemporaryDirectory() as dest:
            self.command('options',{'backup_dir':dest})
            result=self.command('backup',{})
            self.assertTrue(Path(result['external_file']).exists())
            encoded=base64.b64encode(Path(result['file']).read_bytes()).decode()
            old_revision=self.current()['revision']
            self.command('products',{'name':'Later','unit':'piece','price':5,'barcode':'999'})
            with server.LOCK:restored=server.restore_database(encoded,'RESTORE')
            self.assertTrue(Path(restored['safety_copy']).exists())
            self.assertEqual(len(self.current()['products']),1)
            self.assertNotEqual(self.current()['revision'],old_revision)

    def test_restore_refuses_open_shift_invalid_file_and_newer_version(self):
        result=self.command('backup',{})
        raw=Path(result['file']).read_bytes()
        with self.assertRaises(ValueError):server.restore_database(base64.b64encode(raw).decode(),'RESTORE')
        with self.assertRaises(ValueError):server.restore_database(base64.b64encode(b'bad').decode(),'RESTORE')
        self.close_shift()
        with closing(sqlite3.connect(result['file'])) as db, db:db.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):server.restore_database(base64.b64encode(Path(result['file']).read_bytes()).decode(),'RESTORE')
        self.assertEqual(len(self.current()['products']),1)

    def test_half_cent_weight_discount(self):
        self.command('product-update',{'id':self.p['id'],'name':'Test','barcode':'123456','price':.01})
        s=self.sale(currency='USD',items=[{'id':self.p['id'],'qty':.5,'discount':{'type':'percent','value':50}}])
        self.assertEqual(s['items'][0]['gross'],.01)
        self.assertEqual(s['items'][0]['discount'],.01)
        self.assertEqual(s['total'],0)

    def test_supplier_invoice_and_minimum(self):
        self.command('batches',{'product':self.p['id'],'qty':1,'cost':99,'supplier':'مورد','invoice':'INV-1'})
        self.assertEqual(self.current()['batches'][-1]['supplier'],'مورد')
        self.assertEqual(self.current()['batches'][-1]['invoice'],'INV-1')
        self.command('product-update',{'id':self.p['id'],'name':self.p['name'],'price':10,'barcode':'123456','min_stock':7})
        p=self.current()['products'][0]
        self.assertLessEqual(p['stock'],p['min_stock'])

    def test_external_backup_failure_keeps_local(self):
        occupied=server.DATA/'a-file';occupied.write_text('not a folder')
        self.command('options',{'backup_dir':str(occupied)})
        result=self.command('backup',{})
        self.assertTrue(Path(result['file']).exists())
        self.assertIn('warning',result)

class MigrationTests(StoreFixture,unittest.TestCase):
    def test_migrate_v1_without_losing_history(self):
        s=self.sale()
        with server.connect() as db:
            # Recreate precisely the first-version table layout in a separate file.
            target=server.DATA/'v1.sqlite'
            with closing(sqlite3.connect(target)) as v1, v1:
                for name in ('products','batches','settings','sales'):
                    cols={'products':'id,name,barcode,unit,price','batches':'id,product,received,qty,remaining,cost','settings':'key,value','sales':'id,created,currency,method,total,cost_egp,rates,detail'}[name]
                    definitions={'products':'id TEXT PRIMARY KEY,name TEXT,barcode TEXT UNIQUE,unit TEXT,price REAL','batches':'id INTEGER PRIMARY KEY,product TEXT,received TEXT,qty INTEGER,remaining INTEGER,cost REAL','settings':'key TEXT PRIMARY KEY,value TEXT','sales':'id TEXT PRIMARY KEY,created TEXT,currency TEXT,method TEXT,total REAL,cost_egp REAL,rates TEXT,detail TEXT'}[name]
                    v1.execute(f'CREATE TABLE {name}({definitions})')
                    for row in db.execute(f'SELECT {cols} FROM {name}'):
                        if name=='settings' and row['key'] not in ('rates','configured'):continue
                        values=list(row)
                        if name=='sales':
                            detail=json.loads(values[-1]);detail.pop('shift_id',None);detail.pop('fee',None);values[-1]=json.dumps(detail)
                        v1.execute(f'INSERT INTO {name} VALUES ({",".join("?" for _ in row)})',values)
        self.close_shift_for_migration()
        result=server.restore_database(base64.b64encode(target.read_bytes()).decode(),'RESTORE')
        self.assertEqual(self.current_for_migration()['products'][0]['stock'],2)
        with server.connect() as db:
            self.assertEqual(server.sales_list(db)[0]['id'],s['id'])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
    def close_shift_for_migration(self):
        with server.connect() as db:server.mutate(db,'/api/shift-close',{'id':1,'closing':dict.fromkeys(server.CURRENCIES,0)})
    def current_for_migration(self):
        with server.connect() as db:return server.snapshot(db)
