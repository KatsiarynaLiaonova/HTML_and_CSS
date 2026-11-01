import unittest

import server
from test_store import StoreFixture


class PrintSettingsTests(StoreFixture, unittest.TestCase):
    def command(self, name, data):
        with server.connect() as db:
            return server.mutate(db, '/api/' + name, data)

    def current(self):
        with server.connect() as db:
            return server.snapshot(db)

    def test_rotation_is_saved_without_swapping_measured_label_dimensions(self):
        for rotation in (0, 90, 180, 270):
            result = self.command('options', {'template': dict(width=38, height=25, receiptWidth=70, labelRotation=rotation)})
            template = result['options']['template']
            self.assertEqual(template, dict(width=38, height=25, receiptWidth=70, labelRotation=rotation))
            self.assertEqual(self.current()['options']['template'], template)

    def test_invalid_rotation_rejects_the_whole_settings_update(self):
        before = self.current()['options']
        for rotation in (-90, 45, 360, True, 'NaN', 'Infinity', ''):
            with self.subTest(rotation=rotation), self.assertRaises((ValueError, ArithmeticError)):
                self.command('options', {'shop_name': 'Should not be saved', 'template': dict(width=38, height=25, receiptWidth=70, labelRotation=rotation)})
            self.assertEqual(self.current()['options'], before)

    def test_older_template_updates_preserve_selected_rotation(self):
        self.command('options', {'template': dict(width=38, height=25, receiptWidth=70, labelRotation=270)})
        result = self.command('options', {'template': dict(width=38, height=25, receiptWidth=75)})
        self.assertEqual(result['options']['template']['labelRotation'], 270)
        self.assertEqual(result['options']['template']['receiptWidth'], 75)

    def test_existing_database_defaults_to_no_rotation_and_keeps_sales(self):
        sale = self.sale()
        with server.connect() as db:
            options = server.get_setting(db, 'options')
            options['template'].pop('labelRotation')
            server.put_setting(db, 'options', options)
            original = db.execute('SELECT detail FROM sales WHERE id=?', (sale['id'],)).fetchone()[0]
        stock = self.current()['products'][0]['stock']
        server.initialize()
        self.assertEqual(self.current()['options']['template'], dict(width=38, height=25, receiptWidth=70, labelRotation=0))
        self.assertEqual(self.current()['products'][0]['stock'], stock)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT detail FROM sales WHERE id=?', (sale['id'],)).fetchone()[0], original)

    def test_rotation_does_not_mark_unchanged_prices_for_replacement(self):
        p = self.current()['products'][0]
        self.command('labels-confirm', {'items': [{'id': p['id'], 'signature': p['signature']}]})
        result = self.command('options', {'template': dict(width=38, height=25, receiptWidth=70, labelRotation=270)})
        self.assertFalse(result['products'][0]['label_changed'])
        self.assertEqual(result['products'][0]['signature'], p['signature'])
