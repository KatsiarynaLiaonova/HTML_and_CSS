"""Upgrade populated historical layouts, not just their user_version number."""
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest

import server
from test_store import StoreFixture


class PopulatedMigrationTests(StoreFixture, unittest.TestCase):
    def command(self, name, data):
        with server.connect() as db:
            return server.mutate(db, '/api/' + name, data)

    def legacy_database(self, populated=True):
        if populated:
            sale = self.sale(items=[{'id': self.p['id'], 'qty': 1}])
            request = dict(id=sale['id'], items=[dict(line=0, qty=.5, restock=True)], fee_refund=0)
            preview = self.command('return-preview', request)
            self.command('returns', dict(request, confirmed=True, reason='Legacy return', request_id='legacy-return', return_count=preview['return_count']))
            voided = self.sale(items=[{'id': self.p['id'], 'qty': 1}])
            self.command('void', dict(id=voided['id'], reason='Legacy correction', confirmed=True))
            self.command('cash-movement', dict(currency='EGP', amount=12, reason='Legacy change'))
        with server.connect() as db:
            state = server.snapshot(db)
        self.command('labels-confirm', {'items': [dict(id=p['id'], signature=p['signature']) for p in state['products']]})
        self.command('shift-close', dict(id=state['shifts'][0]['id'], closing=state['shifts'][0]['summary']['expected']))
        self.command('shift-open', dict(opening=dict(USD=0, EUR=0, EGP=50, RUB=0)))
        path = server.DATA / 'legacy.sqlite'
        with closing(sqlite3.connect(path)) as legacy, legacy:
            legacy.executescript((Path(__file__).parent / 'fixtures/schema3.sql').read_text())
            with server.connect() as source:
                for (table,) in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    columns = [r[1] for r in legacy.execute(f'PRAGMA table_info({table})')]
                    for row in source.execute(f'SELECT {",".join(columns)} FROM {table}'):
                        values = list(row)
                        if table == 'settings':
                            if row['key'] == 'store_id':
                                continue
                            if row['key'] == 'options':
                                options = json.loads(row['value']); options.pop('time_zone', None)
                                values[columns.index('value')] = json.dumps(options)
                        if table in ('sales', 'sale_returns'):
                            detail = json.loads(row['detail']); detail.pop('register_id', None)
                            values[columns.index('detail')] = json.dumps(detail)
                        legacy.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in columns)})', values)
            self.assertEqual(legacy.execute('PRAGMA foreign_key_check').fetchall(), [])
        return path

    def rows(self, db):
        return {table: (columns, [tuple(r) for r in db.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY rowid')])
                for table, columns in [(r[0], [c[1] for c in db.execute(f'PRAGMA table_info({r[0]})')])
                                       for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]}

    def test_populated_schema_three_startup_preserves_all_financial_rows(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path)) as db:
            original = self.rows(db)
        path.replace(server.DATA / 'store.sqlite')
        server.initialize()
        server.initialize()
        with server.connect() as db:
            for table, (columns, rows) in original.items():
                if table == 'settings':
                    continue  # New store identity/time-zone settings are intentional.
                self.assertEqual([tuple(r) for r in db.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY rowid')], rows, table)
            for key, value in original['settings'][1]:
                expected = json.loads(value)
                if key == 'options':
                    expected['time_zone'] = server.DEFAULTS['time_zone']
                self.assertEqual(server.get_setting(db, key), expected, key)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')), [])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], server.SCHEMA)
            for table in ('sales', 'shifts'):
                self.assertEqual({r[0] for r in db.execute(f'SELECT register_id FROM {table}')}, {1})
                self.assertTrue(any(r['table'] == 'registers' for r in db.execute(f'PRAGMA foreign_key_list({table})')))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f'UPDATE {table} SET register_id=999')
            self.assertEqual(len(server.sales_list(db)[-1]['returns']), 1)
            db.rollback()  # The deliberately rejected UPDATEs opened a transaction.
            server.mutate(db, '/api/shift-open', {'opening': dict.fromkeys(server.CURRENCIES, 0)}, register_id=2)
        copies = list(server.DATA.glob('backups/before-upgrade-*.sqlite'))
        self.assertEqual(len(copies), 1)
        with closing(sqlite3.connect(copies[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(self.rows(db), original)

    def test_upgrade_with_shifts_but_no_sales(self):
        self.legacy_database(populated=False).replace(server.DATA / 'store.sqlite')
        server.initialize()
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sales').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shifts WHERE register_id=1').fetchone()[0], 2)

    def test_restart_after_failed_three_zero_upgrade(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path)) as db, db:
            # 3.0 committed these new empty tables before ADD COLUMN failed.
            db.executescript("CREATE TABLE registers(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE); INSERT INTO registers VALUES(1,'Касса 1'),(2,'Касса 2');")
        path.replace(server.DATA / 'store.sqlite')
        server.initialize()
        with server.connect() as db:
            self.assertEqual(len(server.sales_list(db)), 2)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')), [])

    def test_invalid_legacy_references_roll_back_and_restore_enforcement(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE batches SET product='missing-product'")
        path.replace(server.DATA / 'store.sqlite')
        with server.connect() as db:
            with self.assertRaisesRegex(ValueError, 'integrity'):
                server.migrate(db)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertNotIn('register_id', {r['name'] for r in db.execute('PRAGMA table_info(sales)')})
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='registers'").fetchone())

    def test_restore_populated_schema_three_backup(self):
        path = self.legacy_database()
        self.command('shift-close', dict(id=2, closing=dict(USD=0, EUR=0, EGP=50, RUB=0)))
        server.restore_database(base64.b64encode(path.read_bytes()).decode(), 'RESTORE')
        with server.connect() as db:
            self.assertEqual(len(server.sales_list(db)), 2)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], server.SCHEMA)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')), [])
