import http.client
from contextlib import closing
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest

from access import Accounts
from connections import create_gateway, prepare_certificate, start_lan_server, validate_client_url
import server


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.original = server.DATA
        self.temp = tempfile.TemporaryDirectory()
        server.DATA = Path(self.temp.name)
        server.initialize()
        self.accounts = Accounts(server.DATA)
        self.accounts.create('owner', 'Owner transport password!', 'owner', initial=True)
        self.services = []
        self.tls = start_lan_server(server.Handler, self.accounts, server.DATA / 'network', '127.0.0.1', port=0)
        self.start(self.tls)
        self.url = f'https://127.0.0.1:{self.tls.server_port}'
        self.certificate = server.DATA / 'network' / 'connection-certificate.pem'

    def start(self, service):
        threading.Thread(target=service.serve_forever, daemon=True).start()
        self.services.append(service)
        return service

    def tearDown(self):
        for service in reversed(self.services):
            service.shutdown(); service.server_close()
        server.DATA = self.original
        self.temp.cleanup()

    def request(self, service, path, data=None, cookie=None, csrf=None):
        connection = http.client.HTTPConnection('127.0.0.1', service.server_port, timeout=5)
        headers = {'Origin': f'http://127.0.0.1:{service.server_port}'}
        if cookie: headers['Cookie'] = cookie
        if csrf: headers['X-CSRF-Token'] = csrf
        if data is not None: headers['Content-Type'] = 'application/json'
        connection.request('POST' if data is not None else 'GET', path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def test_gateway_logs_in_over_verified_tls_and_preserves_csrf(self):
        gateway = self.start(create_gateway(self.url, self.certificate))
        self.assertEqual(self.request(gateway, '/api/state')[0], 401)
        status, headers, body = self.request(gateway, '/api/login', dict(login='owner', password='Owner transport password!'))
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertNotIn('; Secure', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(self.request(gateway, '/api/state', cookie=cookie)[0], 200)
        self.assertEqual(self.request(gateway, '/api/options', dict(shop_name='Owner updated remotely'), cookie=cookie)[0], 403)
        result = self.request(gateway, '/api/options', dict(shop_name='Owner updated remotely'), cookie=cookie, csrf=body['csrf'])
        self.assertEqual(result[0], 200)
        self.assertEqual(result[2]['options']['shop_name'], 'Owner updated remotely')

    def test_untrusted_certificate_does_not_allow_login(self):
        wrong, _ = prepare_certificate(server.DATA / 'wrong-server', '127.0.0.1')
        gateway = self.start(create_gateway(self.url, wrong))
        self.assertEqual(self.request(gateway, '/api/login', dict(login='owner', password='Owner transport password!'))[0], 502)
        with closing(self.accounts.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_direct_tls_checks_hostname_and_secure_cookie(self):
        context = ssl.create_default_context(cafile=str(self.certificate))
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        connection = http.client.HTTPSConnection('127.0.0.1', self.tls.server_port, context=context, timeout=5)
        connection.request('POST', '/api/login', json.dumps(dict(login='owner', password='Owner transport password!')), {'Content-Type': 'application/json', 'Origin': self.url})
        response = connection.getresponse();response.read()
        self.assertEqual(response.status, 200)
        self.assertIn('; Secure', response.getheader('Set-Cookie'))
        connection.close()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_plain_http_or_embedded_credentials_are_rejected(self):
        for url in ('http://192.168.1.2:8443', 'https://user:password@192.168.1.2', 'https://example.com/path', 'https://example.com/?token=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_client_url(url)

    def test_existing_certificate_is_preserved_and_wrong_address_refused(self):
        first = self.certificate.read_bytes()
        prepare_certificate(server.DATA / 'network', '127.0.0.1')
        self.assertEqual(first, self.certificate.read_bytes())
        with self.assertRaises(ValueError):
            prepare_certificate(server.DATA / 'network', '192.168.1.2')
