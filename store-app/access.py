"""Local accounts for the shared store. Kept outside inventory backup/restore."""
from contextlib import closing
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

ITERATIONS = 600_000
LIFETIME = 12 * 60 * 60


class AccessError(ValueError):
    pass


class Accounts:
    def __init__(self, folder):
        self.path = Path(folder) / 'accounts.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,login TEXT NOT NULL UNIQUE,salt TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,register_id INTEGER,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_limits(login TEXT PRIMARY KEY,attempts INTEGER NOT NULL,blocked_until REAL NOT NULL);
            ''')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def public(user):
        return {key: user[key] for key in ('id', 'login', 'role', 'register_id')}

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS).hex()

    def has_owner(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM users WHERE role='owner' AND active=1").fetchone() is not None

    def create(self, login, password, role, register_id=None, initial=False):
        login = str(login).strip().lower()
        if not login or len(login) > 80 or any(c.isspace() for c in login):
            raise AccessError('Login must be 1 to 80 characters without spaces')
        if not isinstance(password, str) or len(password) < 10 or len(password) > 256:
            raise AccessError('Use a password of 10 to 256 characters')
        if role not in ('owner', 'cashier'):
            raise AccessError('Unknown account role')
        if role == 'cashier' and (type(register_id) is not int or register_id not in (1, 2)):
            raise AccessError('Choose cash register 1 or 2')
        if role == 'owner':
            register_id = None
        salt = secrets.token_hex(16)
        digest = self.password_hash(password, salt)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if initial and (role != 'owner' or db.execute('SELECT 1 FROM users').fetchone()):
                raise AccessError('The owner account has already been initialized')
            try:
                row = db.execute('INSERT INTO users(login,salt,password_hash,role,register_id) VALUES (?,?,?,?,?)', (login, salt, digest, role, register_id))
            except sqlite3.IntegrityError as exc:
                raise AccessError('This login is already in use') from exc
            return self.public(db.execute('SELECT * FROM users WHERE id=?', (row.lastrowid,)).fetchone())

    def login(self, login, password):
        login = str(login).strip().lower()
        if not isinstance(password, str) or len(password) > 256 or len(login) > 80:
            raise AccessError('Incorrect login or password')
        current = time.time()
        with closing(self.connect()) as db:
            limit = db.execute('SELECT * FROM login_limits WHERE login=?', (login,)).fetchone()
            if limit and limit['blocked_until'] > current:
                raise AccessError('Too many login attempts. Try again in five minutes')
            user = db.execute('SELECT * FROM users WHERE login=? AND active=1', (login,)).fetchone()
            salt = user['salt'] if user else '00' * 16
            digest = self.password_hash(password, salt)
            valid = user is not None and hmac.compare_digest(digest, user['password_hash'])
            if not valid:
                with db:
                    attempts = (limit['attempts'] if limit and limit['blocked_until'] > current - 300 else 0) + 1
                    db.execute('INSERT OR REPLACE INTO login_limits VALUES (?,?,?)', (login, attempts, current + 300 if attempts >= 5 else current))
                raise AccessError('Incorrect login or password')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with db:
                db.execute('DELETE FROM login_limits WHERE login=?', (login,))
                db.execute('DELETE FROM sessions WHERE expires<=?', (current,))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, current + LIFETIME))
            return token, csrf, self.public(user)

    def authenticate(self, token):
        if not isinstance(token, str) or not token or len(token) > 200:
            raise AccessError('Sign in to ALI BABA')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.connect()) as db:
            row = db.execute('SELECT u.*,s.csrf,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.active=1 AND s.expires>?', (digest, time.time())).fetchone()
            if not row:
                raise AccessError('Your session has expired. Sign in again')
            return self.public(row), row['csrf']

    def logout(self, token):
        digest = hashlib.sha256(str(token).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))

    def users(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT id,login,role,register_id,active FROM users ORDER BY id')]


def may_write(user, path):
    if user['role'] == 'owner':
        return True
    return path in ('/api/checkout', '/api/return-preview', '/api/returns', '/api/void',
                    '/api/shift-open', '/api/shift-close', '/api/cash-movement',
                    '/api/products', '/api/batches', '/api/labels-confirm', '/api/printer-options', '/api/logout')
