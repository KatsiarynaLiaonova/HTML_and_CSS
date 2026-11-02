"""Verified HTTPS transport between the main register and local desktop clients."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import certifi
from urllib.parse import urlsplit


def local_address():
    for address in socket.gethostbyname_ex(socket.gethostname())[2]:
        ip = ipaddress.ip_address(address)
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            return address
    return ''


def read_config(folder):
    path = Path(folder) / 'connection.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'mode': 'local'}


def save_config(folder, config):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'connection.json'
    temp = path.with_suffix('.partial')
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def validate_client_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Use the HTTPS address of the main register without a path or password')
    return parsed._replace(netloc=parsed.netloc.lower().removesuffix(':443'))


def prepare_certificate(folder, address):
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or not ip.is_private:
        raise ValueError('Enter the private IPv4 address of the main register in your shop')
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    certificate, key_file = folder / 'connection-certificate.pem', folder / 'connection-key.pem'
    if certificate.exists() and key_file.exists():
        cert = x509.load_pem_x509_certificate(certificate.read_bytes())
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if ip not in names.get_values_for_type(x509.IPAddress):
            raise ValueError('The main register address changed. Keep its existing address or replace the certificate on every client before changing it')
        return certificate, key_file
    if certificate.exists() or key_file.exists():
        raise ValueError('Incomplete connection certificate. Restore its matching certificate and key')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ALI BABA main register')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1095))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return certificate, key_file


def start_lan_server(handler, accounts, folder, address, port=8443):
    if not accounts.has_owner():
        raise ValueError('Initialize the owner account on the main register first')
    certificate, key = prepare_certificate(folder, address)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    service = ThreadingHTTPServer((address, port), handler)
    service.daemon_threads = False
    try:
        service.socket = context.wrap_socket(service.socket, server_side=True)
    except Exception:
        service.server_close()
        raise
    service.accounts = accounts
    service.allowed_hosts = {f'{address}:{service.server_port}'}
    service.cookie_secure = True
    service.allowed_origins = {f'https://{address}:{service.server_port}'}
    return service


class ClientGateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        local = f'127.0.0.1:{self.server.server_port}'
        origin = self.headers.get('Origin')
        if self.headers.get('Host') != local or (origin and origin != 'http://' + local):
            self.send_error(403, 'Host or origin rejected')
            return
        if not self.path.startswith('/') or self.path.startswith('//'):
            self.send_error(400, 'Invalid request path')
            return
        connection = None
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= 96 * 1024 * 1024:
                raise ValueError('Request too large')
            destination = self.server.destination
            connection = http.client.HTTPSConnection(destination.hostname, destination.port or 443, timeout=30, context=self.server.tls_context)
            headers = {name: self.headers[name] for name in ('Content-Type', 'Cookie', 'X-CSRF-Token') if name in self.headers}
            headers.update(Host=destination.netloc, Origin='https://' + destination.netloc)
            connection.request(self.command, self.path, self.rfile.read(length) if length else None, headers)
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() in ('content-type', 'content-disposition', 'cache-control', 'x-content-type-options'):
                    self.send_header(name, value)
                elif name.lower() == 'set-cookie':
                    # The verified TLS hop is remote; the browser hop is loopback only.
                    self.send_header(name, ';'.join(part for part in value.split(';') if part.strip().lower() != 'secure'))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError, http.client.HTTPException):
            message = 'Нет подключения к главной кассе. Проверьте, что она включена, программа запущена и локальная сеть работает. No connection to the main register.'
            if self.path == '/':
                body = ('<!doctype html><meta charset="utf-8"><title>ALI BABA</title><h1>ALI BABA</h1><p>' + message + '</p><button onclick="location.reload()">Повторить / Retry</button><button onclick="window.pywebview.api.configure_connection()">Настройка подключения / Connection</button>').encode()
                content = 'text/html; charset=utf-8'
            else:
                body = json.dumps({'error': message}, ensure_ascii=False).encode()
                content = 'application/json; charset=utf-8'
            self.send_response(502)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            if connection:
                connection.close()


def create_gateway(url, certificate=None, port=0):
    destination = validate_client_url(url)
    context = ssl.create_default_context(cafile=str(certificate) if certificate else certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    service = ThreadingHTTPServer(('127.0.0.1', port), ClientGateway)
    service.destination, service.tls_context = destination, context
    return service
