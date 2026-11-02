import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor

import server
from test_store import StoreFixture


class RegisterTests(StoreFixture, unittest.TestCase):
    def command(self, register, name, data):
        with server.connect() as db:
            return server.mutate(db, '/api/' + name, data, register_id=register)

    def state(self, register=1):
        with server.connect() as db:
            return server.snapshot(db, register_id=register)

    def open_second(self, amount=0):
        self.command(2, 'shift-open', {'opening': dict(USD=0, EUR=0, EGP=amount, RUB=0)})

    def sell(self, register, qty=1):
        return self.command(register, 'checkout', dict(currency='EGP', method='cash', items=[dict(id=self.p['id'], qty=qty)], tender=2000))

    def test_two_registers_can_open_independent_shifts(self):
        self.open_second()
        self.assertEqual({s['register_id'] for s in self.state()['shifts'] if not s['closed']}, {1, 2})
        for register in (1, 2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.command(register, 'shift-open', {'opening': dict.fromkeys(server.CURRENCIES, 0)})

    def test_sales_share_stock_but_not_cash_drawers(self):
        self.open_second(100)
        first, second = self.sell(1), self.sell(2, 2)
        self.assertEqual((first['register_id'], second['register_id']), (1, 2))
        state = self.state()
        self.assertEqual(state['products'][0]['stock'], 2)
        shifts = {s['register_id']: s for s in state['shifts']}
        self.assertEqual(shifts[1]['summary']['expected']['EGP'], 500)
        self.assertEqual(shifts[2]['summary']['expected']['EGP'], 1100)
        self.assertEqual(self.state(2)['register_id'], 2)

    def test_cash_movement_and_close_affect_only_the_selected_register(self):
        self.open_second(100)
        self.command(1, 'cash-movement', dict(currency='EGP', amount=50, reason='Change'))
        before = {s['register_id']: s for s in self.state()['shifts']}
        with self.assertRaises(ValueError):
            self.command(2, 'shift-close', dict(id=before[1]['id'], closing=dict.fromkeys(server.CURRENCIES, 0)))
        self.command(1, 'shift-close', dict(id=before[1]['id'], closing=before[1]['summary']['expected']))
        with self.assertRaises(ValueError):
            self.sell(1)
        self.sell(2)
        after = {s['register_id']: s for s in self.state()['shifts']}
        self.assertTrue(after[1]['closed'])
        self.assertIsNone(after[2]['closed'])
        self.assertEqual(after[1]['summary']['expected']['EGP'], 50)
        self.assertEqual(after[2]['summary']['expected']['EGP'], 600)

    def test_context_not_request_body_selects_cash_register(self):
        self.open_second()
        result = self.command(1, 'checkout', dict(register_id=2, currency='EGP', method='cash', items=[dict(id=self.p['id'], qty=1)], tender=500))
        self.assertEqual(result['register_id'], 1)
        with self.assertRaises(ValueError):
            self.command(999, 'shift-open', dict(opening=dict.fromkeys(server.CURRENCIES, 0)))

    def test_return_reduces_cash_of_the_register_handling_the_return(self):
        self.open_second(1000)
        sale = self.sell(1)
        data = dict(id=sale['id'], items=[dict(line=0, qty=1, restock=True)], fee_refund=0)
        preview = self.command(2, 'return-preview', data)
        event = self.command(2, 'returns', dict(**data, reason='Customer return', confirmed=True, request_id='register-two-return', return_count=preview['return_count']))
        self.assertEqual(event['register_id'], 2)
        shifts = {s['register_id']: s for s in self.state()['shifts']}
        self.assertEqual(shifts[1]['summary']['expected']['EGP'], 500)
        self.assertEqual(shifts[2]['summary']['expected']['EGP'], 500)
        self.assertEqual(self.state()['products'][0]['stock'], 5)
        with self.assertRaises(ValueError):
            self.command(1, 'returns', dict(**data, reason='Customer return', confirmed=True, request_id='register-two-return', return_count=preview['return_count']))

    def test_two_registers_cannot_sell_the_same_last_stock(self):
        self.open_second()
        def attempt(register):
            try:
                self.sell(register, 3)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, (1, 2))), 1)
        self.assertEqual(self.state()['products'][0]['stock'], 2)

    def test_restart_and_schema_upgrade_keep_original_history(self):
        sale = self.sell(1)
        with server.connect() as db:
            original = db.execute('SELECT detail FROM sales WHERE id=?', (sale['id'],)).fetchone()[0]
            db.execute('DROP INDEX one_open_shift_per_register')
            db.execute('CREATE UNIQUE INDEX one_open_shift ON shifts((1)) WHERE closed IS NULL')
            db.execute('PRAGMA user_version=3')
        server.initialize()
        self.open_second()
        server.initialize()
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT detail FROM sales WHERE id=?', (sale['id'],)).fetchone()[0], original)
            self.assertEqual(db.execute('SELECT register_id FROM sales WHERE id=?', (sale['id'],)).fetchone()[0], 1)
        self.assertTrue(list(server.DATA.glob('backups/before-upgrade-*.sqlite')))

    def test_retrying_a_sale_cannot_duplicate_stock_or_cash(self):
        self.open_second()
        data = dict(request_id='same-sale-retry', currency='EGP', method='cash', items=[dict(id=self.p['id'], qty=1)], tender=500)
        first = self.command(1, 'checkout', data)
        retry = self.command(1, 'checkout', data)
        self.assertEqual(first['id'], retry['id'])
        self.assertEqual(self.state()['products'][0]['stock'], 4)
        with self.assertRaises(ValueError):
            self.command(2, 'checkout', data)
        with self.assertRaises(ValueError):
            self.command(1, 'checkout', dict(data, tender=1000))
        shift = next(s for s in self.state()['shifts'] if s['register_id'] == 1)
        self.command(1, 'shift-close', dict(id=shift['id'], closing=shift['summary']['expected']))
        self.assertEqual(self.command(1, 'checkout', data)['id'], first['id'])
        self.assertEqual(self.state()['products'][0]['stock'], 4)

    def test_each_register_keeps_its_own_printer_calibration(self):
        first = dict(width=38, height=25, receiptWidth=70, labelRotation=270)
        second = dict(width=38, height=25, receiptWidth=72, labelRotation=90)
        self.command(1, 'printer-options', {'template': first})
        self.command(2, 'printer-options', {'template': second})
        self.assertEqual(self.state(1)['options']['template'], first)
        self.assertEqual(self.state(2)['options']['template'], second)
        self.assertEqual(self.state(1)['products'][0]['signature'], self.state(2)['products'][0]['signature'])
