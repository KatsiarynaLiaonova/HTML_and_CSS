"""Customer returns must preserve money, stock and the original receipt."""
import base64
import copy
import json
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_store import StoreFixture
import server


class ReturnTests(StoreFixture, unittest.TestCase):
    def command(self, name, data):
        with server.connect() as db:
            return server.mutate(db, '/api/' + name, data)

    def current(self):
        with server.connect() as db:
            return server.snapshot(db)

    def history(self, sale):
        with server.connect() as db:
            return next(s for s in server.sales_list(db) if s['id'] == sale['id'])

    def request(self, sale, items, fee_refund=0):
        data = {'id': sale['id'], 'items': items, 'fee_refund': fee_refund}
        preview = self.command('return-preview', data)
        data.update(reason='Customer return', confirmed=True,
                    request_id=uuid.uuid4().hex, return_count=preview['return_count'])
        return preview, data

    def return_items(self, sale, items, fee_refund=0):
        _, data = self.request(sale, items, fee_refund)
        return self.command('returns', data)

    def close_shift(self):
        shift = self.current()['shifts'][0]
        self.command('shift-close', {
            'id': shift['id'],
            'closing': {c: max(0, v) for c, v in shift['summary']['expected'].items()},
        })
        return self.current()['shifts'][0]

    def test_preview_does_not_change_cash_stock_or_history(self):
        sale = self.sale()
        before = self.current()
        preview, _ = self.request(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        self.assertEqual(preview['total'], 500)
        self.assertEqual(preview['cost_egp'], 100)
        self.assertEqual(preview['currency'], 'EGP')
        self.assertEqual(preview['method'], 'cash')
        self.assertEqual(self.current(), before)
        self.assertEqual(self.history(sale)['returns'], [])

    def test_successive_returns_restore_original_fifo_and_exact_cash(self):
        sale = self.sale()
        refunds = []
        for qty, expected_batches, expected_cash in [
            (1.5, [1500, 2000], 750),
            (1, [2000, 2500], 250),
            (.5, [2000, 3000], 0),
        ]:
            refunds.append(self.return_items(sale, [{'line': 0, 'qty': qty, 'restock': True}]))
            state = self.current()
            self.assertEqual([b['remaining'] for b in state['batches']], expected_batches)
            self.assertEqual(state['shifts'][0]['summary']['expected']['EGP'], expected_cash)
        self.assertEqual([r['cost_egp'] for r in refunds], [150, 125, 75])
        self.assertEqual(sum(r['total'] for r in refunds), sale['total'])
        self.assertEqual(sum(r['cost_egp'] for r in refunds), sale['cost_egp'])
        self.assertEqual(self.history(sale)['return_lines'][0]['remaining_qty'], 0)
        with server.connect() as db:
            original = json.loads(db.execute('SELECT detail FROM sales').fetchone()[0])
        self.assertEqual(original, sale)

    def test_damaged_return_does_not_restore_sellable_stock_or_reverse_its_cost(self):
        sale = self.sale()
        damaged = self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': False}])
        self.assertEqual(damaged['total'], 500)
        self.assertEqual(damaged['cost_egp'], 0)
        self.assertEqual(self.current()['products'][0]['stock'], 2)
        good = self.return_items(sale, [{'line': 0, 'qty': 2, 'restock': True}])
        self.assertEqual(good['cost_egp'], 250)
        self.assertEqual(self.current()['products'][0]['stock'], 4)
        self.assertEqual(sale['cost_egp'] - good['cost_egp'], 100)

    def test_line_and_receipt_discounts_are_refunded_at_original_paid_amount(self):
        sale = self.sale(items=[{
            'id': self.p['id'], 'qty': 2, 'discount': {'type': 'percent', 'value': 10},
        }], discount={'type': 'fixed', 'value': 50})
        self.assertEqual(sale['total'], 850)
        first = self.return_items(sale, [{'line': 0, 'qty': .5, 'restock': True}])
        second = self.return_items(sale, [{'line': 0, 'qty': 1.5, 'restock': True}])
        self.assertEqual(first['total'], 212.5)
        self.assertEqual(second['total'], 637.5)

    def test_splitting_a_return_never_creates_or_loses_a_cent(self):
        self.command('product-update', {
            'id': self.p['id'], 'name': 'Cheap item', 'barcode': '123456', 'price': .01,
        })
        sale = self.sale(currency='USD', discount={'type': 'fixed', 'value': .01})
        refunds = [self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
                   for _ in range(3)]
        self.assertEqual(sale['total'], .02)
        self.assertEqual(sum(Decimal(str(r['total'])) for r in refunds), Decimal('.02'))
        self.assertTrue(all(r['total'] >= 0 for r in refunds))
        self.assertEqual(self.current()['shifts'][0]['summary']['expected']['USD'], 0)

    def test_repeated_product_lines_keep_their_own_discount_and_return_limit(self):
        sale = self.sale(items=[
            {'id': self.p['id'], 'qty': 1, 'discount': {'type': 'percent', 'value': 20}},
            {'id': self.p['id'], 'qty': 1},
        ], discount={'type': 'fixed', 'value': 90})
        first = self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        second = self.return_items(sale, [{'line': 1, 'qty': 1, 'restock': True}])
        self.assertEqual(first['total'], 360)
        self.assertEqual(second['total'], 450)
        self.assertEqual(first['total'] + second['total'], sale['total'])

    def test_receipt_discount_rounding_across_lines_sums_to_the_receipt(self):
        self.command('product-update', {
            'id': self.p['id'], 'name': 'Cheap item', 'barcode': '123456', 'price': .01,
        })
        sale = self.sale(currency='USD', items=[{'id': self.p['id'], 'qty': 1} for _ in range(3)],
                         discount={'type': 'fixed', 'value': .01})
        refunds = [self.return_items(sale, [{'line': i, 'qty': 1, 'restock': True}]) for i in (2, 0, 1)]
        self.assertEqual(sum(Decimal(str(r['total'])) for r in refunds), Decimal('.02'))
        self.assertEqual(sum(r['cost_egp'] for r in refunds), sale['cost_egp'])

    def test_fractional_fifo_costs_balance_for_split_and_damaged_returns(self):
        for index, restocks in enumerate([(True, True, True), (True, False, True)]):
            with self.subTest(restocks=restocks):
                state = self.command('products', {
                    'name': 'Fractional cost', 'barcode': 'FRACTION' + str(index),
                    'unit': 'kg', 'price': 100,
                })
                product = next(p for p in state['products'] if p['barcode'] == 'FRACTION' + str(index))
                for cost in (5, 15, 25):
                    self.command('batches', {'product': product['id'], 'qty': .001, 'cost': cost})
                sale = self.sale(currency='USD', items=[{'id': product['id'], 'qty': .003}])
                self.assertEqual(sale['cost_egp'], .05)
                refunds = [self.return_items(sale, [{'line': 0, 'qty': .001, 'restock': restock}])
                           for restock in restocks]
                reversed_cost = sum(Decimal(str(r['cost_egp'])) for r in refunds)
                lost_cost = sum(Decimal(str(r['loss_cost_egp'])) for r in refunds)
                self.assertEqual(reversed_cost + lost_cost, Decimal('.05'))
                self.assertTrue(all(r['cost_egp'] >= 0 and r['loss_cost_egp'] >= 0 for r in refunds))
                if all(restocks):
                    self.assertEqual(reversed_cost, Decimal('.05'))
                batches = [b for b in self.current()['batches'] if b['product'] == product['id']]
                self.assertEqual([b['remaining'] for b in batches], [int(v) for v in restocks])

    def test_free_discounted_item_can_be_returned_without_cash_movement(self):
        sale = self.sale(items=[{'id': self.p['id'], 'qty': 1}],
                         discount={'type': 'percent', 'value': 100})
        refund = self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        self.assertEqual(refund['total'], 0)
        self.assertEqual(self.current()['products'][0]['stock'], 5)
        self.assertEqual(self.current()['shifts'][0]['summary']['expected']['EGP'], 0)

    def test_returns_use_historical_price_name_currency_and_exchange_rates(self):
        sale = self.sale()
        self.command('rates', {'EUR': .8, 'EGP': 60, 'RUB': 100})
        self.command('product-update', {
            'id': self.p['id'], 'name': 'Renamed', 'barcode': 'NEW123', 'price': 20,
        })
        refund = self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        self.assertEqual(refund['total'], 500)
        self.assertEqual(refund['rates'], sale['rates'])
        self.assertEqual(refund['currency'], sale['currency'])
        self.assertEqual(self.history(sale)['items'][0]['name'], sale['items'][0]['name'])

    def test_card_commission_stays_an_expense_unless_actually_refunded(self):
        self.command('options', {'fees': {
            'card': {'percent': 2, 'fixed': 1}, 'transfer': {'percent': 0, 'fixed': 0},
        }})
        sale = self.sale(method='card')
        self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        summary = self.current()['shifts'][0]['summary']
        self.assertEqual(summary['fees']['EGP'], 31)
        self.assertEqual(summary['payments']['EGP']['card'], 1000)
        self.assertEqual(summary['expected']['EGP'], 0)
        self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}], fee_refund=20)
        with self.assertRaises(ValueError):
            self.request(sale, [{'line': 0, 'qty': 1, 'restock': True}], fee_refund=11.01)
        self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}], fee_refund=11)
        self.assertEqual(self.current()['shifts'][0]['summary']['fees']['EGP'], 0)

    def test_previous_shift_remains_closed_and_refund_uses_current_shift(self):
        sale = self.sale()
        closed = self.close_shift()
        self.command('shift-open', {'opening': dict(USD=0, EUR=0, EGP=1000, RUB=0)})
        refund = self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        shifts = self.current()['shifts']
        self.assertEqual(shifts[1]['summary'], closed['summary'])
        self.assertEqual(shifts[0]['summary']['expected']['EGP'], 500)
        self.assertEqual(refund['shift_id'], shifts[0]['id'])

    def test_return_requires_open_shift_reason_and_refund_confirmation(self):
        sale = self.sale()
        _, data = self.request(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        for field, value in [('reason', ''), ('reason', '  '), ('confirmed', False)]:
            invalid = dict(data, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.command('returns', invalid)
        self.assertEqual(self.history(sale)['returns'], [])
        self.close_shift()
        with self.assertRaises(ValueError):
            self.command('returns', data)

    def test_invalid_or_over_returned_quantities_leave_everything_unchanged(self):
        sale = self.sale()
        before = self.current()
        for items in [[], [{'line': 0, 'qty': 0, 'restock': True}],
                      [{'line': 0, 'qty': -1, 'restock': True}],
                      [{'line': 0, 'qty': 3.001, 'restock': True}],
                      [{'line': 0, 'qty': .0001, 'restock': True}],
                      [{'line': 1, 'qty': 1, 'restock': True}],
                      [{'line': 0, 'qty': 1, 'restock': True}, {'line': 0, 'qty': 1, 'restock': True}],
                      [{'line': 0, 'qty': 1, 'restock': True}, {'line': 9, 'qty': 1, 'restock': True}]]:
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.request(sale, items)
            self.assertEqual(self.current(), before)
        self.return_items(sale, [{'line': 0, 'qty': 2, 'restock': True}])
        with self.assertRaises(ValueError):
            self.request(sale, [{'line': 0, 'qty': 1.001, 'restock': True}])

    def test_multi_line_return_rolls_back_after_a_late_database_failure(self):
        sale = self.sale(items=[{'id': self.p['id'], 'qty': 1}, {'id': self.p['id'], 'qty': 1}])
        _, data = self.request(sale, [
            {'line': 0, 'qty': 1, 'restock': True},
            {'line': 1, 'qty': 1, 'restock': True},
        ])
        before = self.current()
        original_record = server.record_movement
        recorded = []

        def fail_on_second_line(*args):
            recorded.append(args)
            if len(recorded) == 2:
                raise sqlite3.OperationalError('Simulated disk failure')
            return original_record(*args)

        with patch.object(server, 'record_movement', side_effect=fail_on_second_line):
            with self.assertRaises(sqlite3.OperationalError):
                self.command('returns', data)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.history(sale)['returns'], [])
        self.command('returns', data)
        self.assertEqual(self.current()['products'][0]['stock'], 5)

    def test_piece_quantities_cannot_be_fractional(self):
        state = self.command('products', {'name': 'Piece', 'barcode': 'PIECE', 'unit': 'piece', 'price': 2})
        piece = next(p for p in state['products'] if p['barcode'] == 'PIECE')
        self.command('batches', {'product': piece['id'], 'qty': 2, 'cost': 10})
        sale = self.sale(items=[{'id': piece['id'], 'qty': 2}])
        with self.assertRaises(ValueError):
            self.request(sale, [{'line': 0, 'qty': .5, 'restock': True}])

    def test_full_void_and_partial_returns_cannot_both_refund_the_same_sale(self):
        sale = self.sale(items=[{'id': self.p['id'], 'qty': 1}])
        self.command('void', {'id': sale['id'], 'reason': 'Mistake', 'confirmed': True})
        with self.assertRaises(ValueError):
            self.request(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        second = self.sale(items=[{'id': self.p['id'], 'qty': 2}])
        self.return_items(second, [{'line': 0, 'qty': 1, 'restock': True}])
        with self.assertRaises(ValueError):
            self.command('void', {'id': second['id'], 'reason': 'Mistake', 'confirmed': True})

    def test_retried_request_is_idempotent_and_changed_payload_is_rejected(self):
        sale = self.sale()
        _, data = self.request(sale, [{'line': 0, 'qty': 1, 'restock': True}])
        original = self.command('returns', data)
        replay = self.command('returns', copy.deepcopy(data))
        self.assertEqual(replay, original)
        self.assertEqual(len(self.history(sale)['returns']), 1)
        self.assertEqual(self.current()['products'][0]['stock'], 3)
        changed = copy.deepcopy(data)
        changed['items'][0]['qty'] = .5
        with self.assertRaises(ValueError):
            self.command('returns', changed)

    def test_stale_or_concurrent_confirmations_cannot_over_refund(self):
        sale = self.sale()
        _, first = self.request(sale, [{'line': 0, 'qty': 2, 'restock': True}])
        second = dict(first, request_id=uuid.uuid4().hex)

        def attempt(data):
            try:
                return self.command('returns', data)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [first, second]))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.history(sale)['returns']), 1)
        self.assertEqual(self.current()['products'][0]['stock'], 4)
        self.assertEqual(self.current()['shifts'][0]['summary']['expected']['EGP'], 500)

    def test_legacy_sale_without_shift_is_returnable_without_rewriting_it(self):
        sale = self.sale()
        legacy = dict(sale)
        legacy.pop('shift_id', None)
        legacy.pop('fee', None)
        with server.connect() as db:
            db.execute('UPDATE sales SET detail=?,shift_id=NULL WHERE id=?', (json.dumps(legacy), sale['id']))
        refund = self.return_items(legacy, [{'line': 0, 'qty': 1, 'restock': True}])
        self.assertEqual(refund['total'], 500)
        self.assertEqual(refund['fee_refund'], 0)
        with server.connect() as db:
            stored = json.loads(db.execute('SELECT detail FROM sales WHERE id=?', (sale['id'],)).fetchone()[0])
        self.assertEqual(stored, legacy)

    def test_schema_two_upgrade_preserves_sales_stock_and_makes_safety_copy(self):
        sale = self.sale()
        with server.connect() as db:
            db.execute('DROP TABLE sale_returns')
            db.execute('PRAGMA user_version=2')
        server.initialize()
        with server.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], server.SCHEMA)
        self.assertEqual(self.history(sale)['returns'], [])
        self.assertEqual(self.current()['products'][0]['stock'], 2)
        self.assertTrue(list((server.DATA / 'backups').glob('before-upgrade-*.sqlite')))
        self.return_items(sale, [{'line': 0, 'qty': 1, 'restock': True}])

    def test_backup_restore_keeps_return_history_and_prevents_second_refund(self):
        sale = self.sale()
        refund = self.return_items(sale, [{'line': 0, 'qty': 2, 'restock': True}])
        self.close_shift()
        result = self.command('backup', {})
        encoded = base64.b64encode(Path(result['file']).read_bytes()).decode()
        self.command('products', {'name': 'Later', 'unit': 'piece', 'price': 1, 'barcode': 'LATER'})
        server.restore_database(encoded, 'RESTORE')
        self.assertEqual(self.history(sale)['returns'][0]['id'], refund['id'])
        self.assertEqual(self.history(sale)['return_lines'][0]['remaining_qty'], 1)
        self.command('shift-open', {'opening': dict(USD=0, EUR=0, EGP=1000, RUB=0)})
        with self.assertRaises(ValueError):
            self.request(sale, [{'line': 0, 'qty': 2, 'restock': True}])


if __name__ == '__main__':
    unittest.main()
