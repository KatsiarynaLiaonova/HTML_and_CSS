from contextlib import closing
import io
import json
import tempfile
import types
import unittest
from email.message import Message

from access import Accounts, AccessError
import server
from test_store import StoreFixture


class SharedAccessTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.accounts = Accounts(server.DATA)
        self.accounts.create('owner', 'Owner test password!', 'owner', initial=True)
        self.accounts.create('cashier-one', 'Cashier one password!', 'cashier', 1)
        self.accounts.create('cashier-two', 'Cashier two password!', 'cashier', 2)
        self.service = types.SimpleNamespace(server_port=8765, accounts=self.accounts, cookie_secure=False)

    def request(self, method, path, data=None, token=None, csrf=None):
        request = server.Handler.__new__(server.Handler)
        request.server, request.path = self.service, path
        request.headers = Message()
        request.headers['Host'] = '127.0.0.1:8765'
        request.headers['Origin'] = 'http://127.0.0.1:8765'
        if token:
            request.headers['Cookie'] = 'ali_baba_session=' + token
        if csrf:
            request.headers['X-CSRF-Token'] = csrf
        if data is not None:
            body = json.dumps(data).encode()
            request.headers['Content-Type'] = 'application/json'
            request.headers['Content-Length'] = str(len(body))
            request.rfile = io.BytesIO(body)
        captured = {}
        def reply(code, payload, *args, **kwargs):
            captured.update(code=code, payload=payload, headers=kwargs.get('headers', {}))
        request.reply = reply
        getattr(request, 'do_' + method)()
        return captured

    def login(self, user):
        passwords = {'owner': 'Owner test password!', 'cashier-one': 'Cashier one password!', 'cashier-two': 'Cashier two password!'}
        return self.accounts.login(user, passwords[user])[:2]

    def test_inventory_and_sales_require_login(self):
        for path in ('/api/state', '/api/sales', '/api/backup-download', '/api/users'):
            self.assertEqual(self.request('GET', path)['code'], 401)
        self.assertEqual(self.request('GET', '/')['code'], 200)

    def test_login_uses_httponly_cookie_and_never_returns_password_hash(self):
        result = self.request('POST', '/api/login', {'login': 'owner', 'password': 'Owner test password!'})
        self.assertEqual(result['code'], 200)
        self.assertIn('HttpOnly', result['headers']['Set-Cookie'])
        self.assertIn('SameSite=Strict', result['headers']['Set-Cookie'])
        self.assertNotIn('password', json.dumps(result['payload']))
        with closing(self.accounts.connect()) as db:
            user = db.execute("SELECT * FROM users WHERE login='owner'").fetchone()
            self.assertNotEqual(user['password_hash'], 'Owner test password!')
            self.assertNotIn('Owner test password!', self.accounts.path.read_bytes().decode('latin1'))

    def test_cashier_cannot_change_settings_or_download_whole_database(self):
        token, csrf = self.login('cashier-one')
        for path in ('/api/options', '/api/restore', '/api/users', '/api/adjust'):
            self.assertEqual(self.request('POST', path, {}, token, csrf)['code'], 403)
        self.assertEqual(self.request('GET', '/api/backup-download', token=token)['code'], 403)
        self.assertEqual(self.request('GET', '/api/users', token=token)['code'], 403)

    def test_mutation_requires_session_csrf_and_logout_revokes_session(self):
        token, csrf = self.login('owner')
        data = {'shop_name': 'Updated store'}
        self.assertEqual(self.request('POST', '/api/options', data, token)['code'], 403)
        self.assertEqual(self.request('POST', '/api/options', data, token, 'wrong')['code'], 403)
        self.assertEqual(self.request('POST', '/api/options', data, token, csrf)['code'], 200)
        self.assertEqual(self.request('POST', '/api/logout', {}, token, csrf)['code'], 200)
        self.assertEqual(self.request('GET', '/api/state', token=token)['code'], 401)

    def test_cashier_session_selects_register_even_if_request_forges_another(self):
        token, csrf = self.login('cashier-two')
        result = self.request('POST', '/api/shift-open', {'register_id': 1, 'opening': dict.fromkeys(server.CURRENCIES, 0)}, token, csrf)
        self.assertEqual(result['code'], 200)
        self.assertEqual(result['payload']['register_id'], 2)
        self.assertEqual(result['payload']['account']['role'], 'cashier')
        self.assertEqual({s['register_id'] for s in result['payload']['shifts']}, {2})
        result = self.request('GET', '/api/state', token=token)
        self.assertEqual({s['register_id'] for s in result['payload']['shifts']}, {2})

    def test_cashier_history_and_returns_do_not_expose_another_register(self):
        sale = self.sale()
        token, csrf = self.login('cashier-two')
        self.assertEqual(self.request('GET', '/api/sales', token=token)['payload'], [])
        data = {'id': sale['id'], 'items': [{'line': 0, 'qty': 1, 'restock': True}]}
        self.assertEqual(self.request('POST', '/api/return-preview', data, token, csrf)['code'], 403)
        owner, _ = self.login('owner')
        self.assertEqual(len(self.request('GET', '/api/sales', token=owner)['payload']), 1)

    def test_only_owner_can_create_cashier_accounts(self):
        token, csrf = self.login('owner')
        result = self.request('POST', '/api/users', {'login': 'replacement', 'password': 'Replacement password!', 'role': 'cashier', 'register_id': 2}, token, csrf)
        self.assertEqual(result['code'], 200)
        self.assertEqual(result['payload']['register_id'], 2)
        with self.assertRaises(AccessError):
            self.accounts.create('other-owner', 'Other owner password!', 'owner', initial=True)

    def test_failed_login_throttling_and_unknown_users(self):
        for i in range(5):
            self.assertEqual(self.request('POST', '/api/login', {'login': 'owner', 'password': 'incorrect'})['code'], 401)
        result = self.request('POST', '/api/login', {'login': 'owner', 'password': 'Owner test password!'})
        self.assertEqual(result['code'], 401)
        self.assertIn('five minutes', result['payload']['error'])
