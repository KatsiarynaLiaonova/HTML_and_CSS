"""Nile Store: local retail accounting, standard library only."""
import argparse
import base64
import copy
import json
import os
import sqlite3
import tempfile
import threading
import uuid
import webbrowser
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = '2.0.0'
SCHEMA = 2
ROOT = Path(__file__).parent
DATA = Path(os.environ.get('STORE_DATA_DIR', Path.home() / '.retail-store'))
CURRENCIES = ('USD', 'EUR', 'EGP', 'RUB')
LOCK = threading.RLock()
BACKUP_ERROR = ''
CHOOSE_FOLDER = None
DEFAULTS = {
    'rounding': {c: .01 for c in CURRENCIES},
    'fees': {m: {'percent': 0, 'fixed': 0} for m in ('card', 'transfer')},
    'backup_dir': '', 'shop_name': 'NILE',
    'template': {'width': 38, 'height': 25, 'receiptWidth': 70},
}

def now():
    return datetime.now(timezone.utc).isoformat()

def decimal(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError('Amount must be finite')
    return n

def money(value):
    return float(decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))

def positive(value):
    n = decimal(value)
    if n <= 0:
        raise ValueError('Value must be positive')
    return float(n)

def nonnegative(value):
    n = decimal(value)
    if n < 0:
        raise ValueError('Value cannot be negative')
    return float(n)

@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'store.sqlite', timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()

def get_setting(db, key):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None

def put_setting(db, key, value):
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

def revision(db):
    put_setting(db, 'revision', uuid.uuid4().hex)

def local_backup(db, folder=None, prefix='store'):
    folder = Path(folder or DATA / 'backups').expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (prefix + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite')
    temp = target.with_suffix('.partial')
    try:
        with closing(sqlite3.connect(temp)) as dest, dest:
            db.backup(dest)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return str(target)

def backup(db, external=True):
    # Always keep a local recovery copy even if an external disk is disconnected.
    global BACKUP_ERROR
    result = {'file': local_backup(db)}
    folder = get_setting(db, 'options') or {}
    folder = folder.get('backup_dir', '') if external else ''
    BACKUP_ERROR = ''
    if folder and Path(folder).expanduser().resolve() != (DATA / 'backups').resolve():
        try:
            if not Path(folder).expanduser().is_dir():
                raise OSError('External backup folder is unavailable. Connect the disk or create/select an existing folder.')
            result['external_file'] = local_backup(db, folder)
        except OSError as exc:
            BACKUP_ERROR = str(exc)
            result['warning'] = BACKUP_ERROR
    return result

def migrate(db, safety=True):
    old_version = db.execute('PRAGMA user_version').fetchone()[0]
    if old_version > SCHEMA:
        raise ValueError('This database needs a newer version of Nile Store')
    if safety and old_version < SCHEMA and db.execute("SELECT 1 FROM sqlite_master WHERE name='sales'").fetchone():
        local_backup(db, prefix='before-upgrade')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,barcode TEXT UNIQUE NOT NULL,unit TEXT NOT NULL,price REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,product TEXT REFERENCES products(id),received TEXT NOT NULL,qty INTEGER NOT NULL,remaining INTEGER NOT NULL,cost REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales(id TEXT PRIMARY KEY,created TEXT NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL,total REAL NOT NULL,cost_egp REAL NOT NULL,rates TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY,opened TEXT NOT NULL,closed TEXT,opening TEXT NOT NULL,closing TEXT,summary TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS one_open_shift ON shifts((1)) WHERE closed IS NULL;
    CREATE TABLE IF NOT EXISTS voids(sale_id TEXT PRIMARY KEY REFERENCES sales(id),created TEXT NOT NULL,reason TEXT NOT NULL,shift_id INTEGER NOT NULL REFERENCES shifts(id),fee_refunded INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS cash_movements(id INTEGER PRIMARY KEY,shift_id INTEGER REFERENCES shifts(id),created TEXT,currency TEXT,amount REAL,reason TEXT);
    CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY,created TEXT,product TEXT REFERENCES products(id),kind TEXT,qty INTEGER,reason TEXT,detail TEXT);
    CREATE TABLE IF NOT EXISTS label_prices(product TEXT PRIMARY KEY REFERENCES products(id),signature TEXT NOT NULL,printed TEXT NOT NULL);
    ''')
    additions = {
        'products': {'min_stock': 'REAL NOT NULL DEFAULT 0'},
        'batches': {'supplier': "TEXT NOT NULL DEFAULT ''", 'invoice': "TEXT NOT NULL DEFAULT ''", 'origin': "TEXT NOT NULL DEFAULT 'purchase'"},
        'sales': {'shift_id': 'INTEGER REFERENCES shifts(id)'},
    }
    for table, columns in additions.items():
        existing = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    for key, value in {'rates': dict.fromkeys(CURRENCIES, 1), 'configured': False, 'options': DEFAULTS, 'revision': uuid.uuid4().hex}.items():
        if get_setting(db, key) is None:
            put_setting(db, key, value)
    db.execute('PRAGMA user_version=2')

def initialize():
    with connect() as db:
        migrate(db)

def quantity(value, unit, zero=False):
    n = decimal(value)
    if n < 0 or (not zero and n == 0) or n * 1000 != (n * 1000).to_integral_value() or (unit == 'piece' and n != n.to_integral_value()):
        raise ValueError('Pieces must be whole; kilograms allow 3 decimals')
    return int(n * 1000)

def price_for(product, currency, rates, options):
    step = decimal(options['rounding'][currency])
    value = decimal(product['price']) * decimal(rates[currency])
    return money((value / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step)

def signature(product, rates, options):
    return json.dumps([product['name'], product['barcode'], [price_for(product, c, rates, options) for c in CURRENCIES], options['template']], ensure_ascii=False)

def sales_list(db):
    rows = db.execute('SELECT s.detail,v.created AS voided,v.reason,v.fee_refunded FROM sales s LEFT JOIN voids v ON v.sale_id=s.id ORDER BY s.created DESC')
    result = []
    for row in rows:
        sale = json.loads(row['detail'])
        sale['void'] = {'created': row['voided'], 'reason': row['reason'], 'fee_refunded': bool(row['fee_refunded'])} if row['voided'] else None
        result.append(sale)
    return result

def shift_summary(db, shift):
    opening = json.loads(shift['opening'])
    expected = dict(opening)
    totals = {c: {m: 0 for m in ('cash', 'card', 'transfer')} for c in CURRENCIES}
    fees = dict.fromkeys(CURRENCIES, 0)
    for row in db.execute('SELECT detail FROM sales WHERE shift_id=?', (shift['id'],)):
        s = json.loads(row['detail'])
        totals[s['currency']][s['method']] += s['total']
        fees[s['currency']] += s.get('fee', 0)
    for row in db.execute('SELECT s.detail,v.fee_refunded FROM voids v JOIN sales s ON s.id=v.sale_id WHERE v.shift_id=?', (shift['id'],)):
        s = json.loads(row['detail'])
        totals[s['currency']][s['method']] -= s['total']
        if row['fee_refunded']:
            fees[s['currency']] -= s.get('fee', 0)
    for c in CURRENCIES:
        expected[c] += totals[c]['cash']
    movements = [dict(r) for r in db.execute('SELECT * FROM cash_movements WHERE shift_id=? ORDER BY id', (shift['id'],))]
    for movement in movements:
        expected[movement['currency']] += movement['amount']
    return {'expected': {c: money(v) for c, v in expected.items()}, 'payments': {c: {m: money(v) for m, v in ms.items()} for c, ms in totals.items()}, 'fees': {c: money(v) for c, v in fees.items()}, 'movements': movements}

def shifts_list(db):
    result = []
    for row in db.execute('SELECT * FROM shifts ORDER BY id DESC'):
        s = dict(row)
        s['opening'] = json.loads(s['opening'])
        s['closing'] = json.loads(s['closing']) if s['closing'] else None
        s['summary'] = json.loads(s['summary']) if s['summary'] else shift_summary(db, row)
        result.append(s)
    return result

def snapshot(db):
    rates, options = get_setting(db, 'rates'), get_setting(db, 'options')
    products = [dict(r) for r in db.execute('SELECT p.*,COALESCE(SUM(b.remaining),0)/1000.0 stock,COALESCE(SUM(b.remaining*b.cost),0)/1000.0 stock_value FROM products p LEFT JOIN batches b ON p.id=b.product GROUP BY p.id ORDER BY p.name')]
    printed = {r['product']: r['signature'] for r in db.execute('SELECT * FROM label_prices')}
    for p in products:
        p['prices'] = {c: price_for(p, c, rates, options) for c in CURRENCIES}
        p['signature'] = signature(p, rates, options)
        p['label_changed'] = printed.get(p['id']) != p['signature']
    return {
        'products': products,
        'batches': [dict(r) for r in db.execute('SELECT b.*,p.name FROM batches b JOIN products p ON p.id=b.product ORDER BY b.id')],
        'rates': rates, 'configured': get_setting(db, 'configured'), 'options': options,
        'revision': get_setting(db, 'revision'), 'can_choose_folder': CHOOSE_FOLDER is not None, 'version': VERSION, 'data_dir': str(DATA),
        'shifts': shifts_list(db), 'backup_warning': BACKUP_ERROR,
        'movements': [dict(r) for r in db.execute('SELECT m.*,p.name FROM movements m JOIN products p ON p.id=m.product ORDER BY m.id DESC')],
    }

def discount(base, spec):
    amount = decimal(spec.get('value', 0))
    if amount < 0 or spec.get('type', 'fixed') not in ('percent', 'fixed'):
        raise ValueError('Invalid discount')
    if spec.get('type') == 'percent':
        if amount > 100:
            raise ValueError('Discount exceeds 100%')
        result = money(decimal(base) * amount / 100)
    else:
        result = money(amount)
    if result > base:
        raise ValueError('Discount exceeds amount')
    return result

def consume(db, product, qty):
    left, allocations = qty, []
    for batch in db.execute('SELECT * FROM batches WHERE product=? AND remaining>0 ORDER BY received,id', (product,)).fetchall():
        take = min(left, batch['remaining'])
        left -= take
        db.execute('UPDATE batches SET remaining=remaining-? WHERE id=?', (take, batch['id']))
        allocations.append({'batch': batch['id'], 'qty': take / 1000, 'cost_egp': batch['cost']})
        if not left:
            break
    if left:
        raise ValueError('Insufficient stock')
    return allocations

def active_shift(db):
    shift = db.execute('SELECT * FROM shifts WHERE closed IS NULL').fetchone()
    if not shift:
        raise ValueError('Open a cash shift before this operation')
    return shift

def checkout(db, data):
    db.execute('BEGIN IMMEDIATE')
    shift = active_shift(db)
    state = snapshot(db)
    if not state['configured']:
        raise ValueError('Set exchange rates before selling')
    if data.get('revision', state['revision']) != state['revision']:
        raise ValueError('Prices changed. Reload the cart before selling')
    currency, method = data['currency'], data['method']
    if currency not in CURRENCIES or method not in ('cash', 'card', 'transfer') or (method == 'card' and currency != 'EGP') or (method == 'transfer' and currency != 'RUB'):
        raise ValueError('Invalid payment currency / method')
    rates, options = state['rates'], state['options']
    lines, cost = [], Decimal(0)
    if not data.get('items'):
        raise ValueError('Cart is empty')
    for item in data['items']:
        p = db.execute('SELECT * FROM products WHERE id=?', (item['id'],)).fetchone()
        if not p:
            raise ValueError('Product not found')
        qty = quantity(item['qty'], p['unit'])
        allocations = consume(db, p['id'], qty)
        cost += sum((decimal(a['qty']) * decimal(a['cost_egp']) for a in allocations), Decimal(0))
        price = price_for(p, currency, rates, options)
        gross = money(decimal(price) * qty / 1000)
        reduction = discount(gross, item.get('discount', {}))
        lines.append({'id': p['id'], 'name': p['name'], 'barcode': p['barcode'], 'unit': p['unit'], 'qty': qty / 1000, 'price': price, 'gross': gross, 'discount': reduction, 'net': money(decimal(gross) - decimal(reduction)), 'fifo': allocations})
    subtotal = money(sum((decimal(l['net']) for l in lines), Decimal(0)))
    reduction = discount(subtotal, data.get('discount', {}))
    total = money(decimal(subtotal) - decimal(reduction))
    tender = money(data.get('tender', total)) if method == 'cash' else total
    if tender < total:
        raise ValueError('Cash received is below total')
    fee_rule = options['fees'].get(method, {'percent': 0, 'fixed': 0})
    fee = money(decimal(total) * decimal(fee_rule['percent']) / 100 + decimal(fee_rule['fixed'])) if total else 0
    sale = {'id': uuid.uuid4().hex[:12].upper(), 'created': now(), 'currency': currency, 'method': method, 'total': total, 'cost_egp': money(cost), 'rates': rates, 'items': lines, 'subtotal': subtotal, 'discount': reduction, 'tender': tender, 'change': money(decimal(tender) - decimal(total)), 'fee': fee, 'fee_rule': fee_rule, 'shift_id': shift['id'], 'shop_name': options['shop_name']}
    for line in lines:
        record_movement(db, line['id'], 'sale', -quantity(line['qty'], line['unit']), sale['id'], line['fifo'])
    db.execute('INSERT INTO sales(id,created,currency,method,total,cost_egp,rates,detail,shift_id) VALUES (?,?,?,?,?,?,?,?,?)', (sale['id'], sale['created'], currency, method, total, sale['cost_egp'], json.dumps(rates), json.dumps(sale, ensure_ascii=False), shift['id']))
    db.commit()
    return sale

def checked_options(data, current):
    result = copy.deepcopy(current)
    for key in data:
        if key not in DEFAULTS:
            raise ValueError('Unknown setting')
    if 'rounding' in data:
        rounding = {}
        for c in CURRENCIES:
            step = positive(data['rounding'][c])
            if step < .01 or step > 100 or decimal(step) * 100 != (decimal(step) * 100).to_integral_value():
                raise ValueError('Rounding increment must be 0.01 to 100, in whole cents')
            rounding[c] = step
        if rounding['USD'] != .01:
            raise ValueError('USD selling prices remain fixed; USD increment must be 0.01')
        result['rounding'] = rounding
    if 'fees' in data:
        fees = {}
        for m in ('card', 'transfer'):
            percent, fixed = nonnegative(data['fees'][m]['percent']), nonnegative(data['fees'][m]['fixed'])
            if percent > 100:
                raise ValueError('Commission exceeds 100%')
            fees[m] = {'percent': percent, 'fixed': money(fixed)}
        result['fees'] = fees
    if 'backup_dir' in data:
        folder = str(data['backup_dir']).strip()
        if folder and not Path(folder).expanduser().is_absolute():
            raise ValueError('Backup folder must be an absolute path')
        result['backup_dir'] = folder
    if 'shop_name' in data:
        name = str(data['shop_name']).strip()
        if not name or len(name) > 160:
            raise ValueError('Store name is required (up to 160 characters)')
        result['shop_name'] = name
    if 'template' in data:
        template = {k: positive(data['template'][k]) for k in ('width', 'height', 'receiptWidth')}
        if any(v < 10 or v > 200 for v in template.values()):
            raise ValueError('Paper dimensions must be 10 to 200 mm')
        result['template'] = template
    return result

def new_rates(data):
    return {c: 1 if c == 'USD' else positive(data[c]) for c in CURRENCIES}

def preview(db, data):
    state = snapshot(db)
    rates = new_rates(data['rates']) if 'rates' in data else state['rates']
    options = checked_options(data.get('options', {}), state['options'])
    return {'revision': state['revision'], 'rates': rates, 'options': options, 'products': [{'id': p['id'], 'name': p['name'], 'old': p['prices'], 'new': {c: price_for(p, c, rates, options) for c in CURRENCIES}} for p in state['products']]}

def record_movement(db, product, kind, qty, reason, allocations):
    db.execute('INSERT INTO movements(created,product,kind,qty,reason,detail) VALUES (?,?,?,?,?,?)', (now(), product, kind, qty, reason, json.dumps(allocations)))

def mutate(db, path, data):
    if path == '/api/checkout':
        return checkout(db, data)
    if path == '/api/preview':
        return preview(db, data)
    if path == '/api/backup':
        return backup(db)
    db.execute('BEGIN IMMEDIATE')
    if path in ('/api/products', '/api/product-update'):
        name = data['name'].strip()
        barcode = data.get('barcode', '').strip().upper()
        if not barcode and path == '/api/products':
            while True:
                barcode = '29' + str(uuid.uuid4().int)[:6]
                if not db.execute('SELECT 1 FROM products WHERE barcode=?', (barcode,)).fetchone():
                    break
        if not name or not barcode or len(barcode) > 40 or not barcode.isascii() or not barcode.isalnum():
            raise ValueError('Name and ASCII barcode (up to 40 characters) are required')
        price = money(positive(data['price']))
        if price <= 0:
            raise ValueError('Price must be at least 0.01 USD')
        if path == '/api/products':
            if data['unit'] not in ('piece', 'kg'):
                raise ValueError('Invalid unit')
            minimum = quantity(data.get('min_stock', 0), data['unit'], zero=True) / 1000
            db.execute('INSERT INTO products(id,name,barcode,unit,price,min_stock) VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex, name, barcode, data['unit'], price, minimum))
        else:
            p = db.execute('SELECT * FROM products WHERE id=?', (data['id'],)).fetchone()
            if not p:
                raise ValueError('Product not found')
            minimum = quantity(data.get('min_stock', p['min_stock']), p['unit'], zero=True) / 1000
            db.execute('UPDATE products SET name=?,barcode=?,price=?,min_stock=? WHERE id=?', (name, barcode, price, minimum, p['id']))
        revision(db)
    elif path == '/api/batches':
        p = db.execute('SELECT * FROM products WHERE id=?', (data['product'],)).fetchone()
        if not p:
            raise ValueError('Product not found')
        qty, cost = quantity(data['qty'], p['unit']), positive(data['cost'])
        row = db.execute('INSERT INTO batches(product,received,qty,remaining,cost,supplier,invoice) VALUES (?,?,?,?,?,?,?)', (p['id'], now(), qty, qty, cost, str(data.get('supplier', '')).strip(), str(data.get('invoice', '')).strip()))
        record_movement(db, p['id'], 'receipt', qty, str(data.get('invoice', '')), [{'batch': row.lastrowid, 'qty': qty / 1000, 'cost_egp': cost}])
    elif path in ('/api/rates', '/api/options', '/api/reprice'):
        if data.get('revision', get_setting(db, 'revision')) != get_setting(db, 'revision'):
            raise ValueError('Settings changed. Preview again')
        if path == '/api/rates':
            put_setting(db, 'rates', new_rates(data))
            put_setting(db, 'configured', True)
        elif path == '/api/options':
            put_setting(db, 'options', checked_options({k: v for k, v in data.items() if k != 'revision'}, get_setting(db, 'options')))
        else:
            if 'rates' in data:
                put_setting(db, 'rates', new_rates(data['rates']))
                put_setting(db, 'configured', True)
            put_setting(db, 'options', checked_options(data.get('options', {}), get_setting(db, 'options')))
        revision(db)
    elif path == '/api/shift-open':
        opening = {c: money(nonnegative(data['opening'][c])) for c in CURRENCIES}
        db.execute('INSERT INTO shifts(opened,opening) VALUES (?,?)', (now(), json.dumps(opening)))
    elif path == '/api/shift-close':
        shift = active_shift(db)
        if int(data['id']) != shift['id']:
            raise ValueError('Shift changed')
        closing = {c: money(nonnegative(data['closing'][c])) for c in CURRENCIES}
        summary = shift_summary(db, shift)
        summary['difference'] = {c: money(decimal(closing[c]) - decimal(summary['expected'][c])) for c in CURRENCIES}
        db.execute('UPDATE shifts SET closed=?,closing=?,summary=? WHERE id=?', (now(), json.dumps(closing), json.dumps(summary), shift['id']))
    elif path == '/api/cash-movement':
        shift = active_shift(db)
        reason = str(data['reason']).strip()
        amount = money(data['amount'])
        if data['currency'] not in CURRENCIES or not reason or not amount:
            raise ValueError('Currency, nonzero amount and reason are required')
        if shift_summary(db, shift)['expected'][data['currency']] + amount < 0:
            raise ValueError('Cash withdrawal exceeds expected cash')
        db.execute('INSERT INTO cash_movements(shift_id,created,currency,amount,reason) VALUES (?,?,?,?,?)', (shift['id'], now(), data['currency'], amount, reason))
    elif path == '/api/void':
        shift = active_shift(db)
        reason = str(data['reason']).strip()
        if not reason or data.get('confirmed') is not True:
            raise ValueError('Confirm payment reversal and provide a reason')
        row = db.execute('SELECT detail FROM sales WHERE id=?', (data['id'],)).fetchone()
        if not row:
            raise ValueError('Sale not found')
        s = json.loads(row['detail'])
        if db.execute('SELECT 1 FROM voids WHERE sale_id=?', (s['id'],)).fetchone():
            raise ValueError('Sale already cancelled')
        for line in s['items']:
            for a in line['fifo']:
                db.execute('UPDATE batches SET remaining=remaining+? WHERE id=?', (quantity(a['qty'], line['unit']), a['batch']))
            record_movement(db, line['id'], 'void', quantity(line['qty'], line['unit']), reason + ' / ' + s['id'], line['fifo'])
        db.execute('INSERT INTO voids VALUES (?,?,?,?,?)', (s['id'], now(), reason, shift['id'], int(data.get('fee_refunded') is True)))
    elif path == '/api/adjust':
        p = db.execute('SELECT * FROM products WHERE id=?', (data['product'],)).fetchone()
        if not p:
            raise ValueError('Product not found')
        reason = str(data['reason']).strip()
        if not reason or data['kind'] not in ('count', 'writeoff'):
            raise ValueError('Adjustment type and reason are required')
        stock = db.execute('SELECT COALESCE(SUM(remaining),0) FROM batches WHERE product=?', (p['id'],)).fetchone()[0]
        if quantity(data['expected_stock'], p['unit'], zero=True) != stock:
            raise ValueError('Stock changed. Recount and try again')
        entered = quantity(data['qty'], p['unit'], zero=data['kind'] == 'count')
        delta = entered - stock if data['kind'] == 'count' else -entered
        if delta < 0:
            allocations = consume(db, p['id'], -delta)
        elif delta > 0:
            cost = positive(data['cost'])
            row = db.execute('INSERT INTO batches(product,received,qty,remaining,cost,origin) VALUES (?,?,?,?,?,?)', (p['id'], now(), delta, delta, cost, 'count'))
            allocations = [{'batch': row.lastrowid, 'qty': delta / 1000, 'cost_egp': cost}]
        else:
            allocations = []
        record_movement(db, p['id'], data['kind'], delta, reason, allocations)
    elif path == '/api/labels-confirm':
        state = snapshot(db)
        for item in data['items']:
            p = next((p for p in state['products'] if p['id'] == item['id']), None)
            if not p or item['signature'] != p['signature']:
                raise ValueError('Prices changed since printing; reprint labels')
            db.execute('INSERT OR REPLACE INTO label_prices VALUES (?,?,?)', (p['id'], p['signature'], now()))
    else:
        raise ValueError('Unknown operation')
    db.commit()
    if path == '/api/shift-close':
        backup(db)
    return snapshot(db)

def restore_database(encoded, confirmed):
    if confirmed != 'RESTORE':
        raise ValueError('Type RESTORE to confirm replacing the database')
    raw = base64.b64decode(encoded, validate=True)
    if not raw.startswith(b'SQLite format 3\x00'):
        raise ValueError('Not a SQLite backup')
    fd, temp_name = tempfile.mkstemp(suffix='.sqlite', dir=DATA)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_bytes(raw)
        with closing(sqlite3.connect(temp)) as source, source:
            source.row_factory = sqlite3.Row
            if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or source.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Backup integrity check failed')
            if source.execute('PRAGMA user_version').fetchone()[0] > SCHEMA:
                raise ValueError('Backup needs a newer app version')
            for table, cols in {'products': 'id,name,barcode,unit,price', 'batches': 'id,product,received,qty,remaining,cost', 'sales': 'id,created,currency,method,total,cost_egp,rates,detail', 'settings': 'key,value'}.items():
                source.execute(f'SELECT {cols} FROM {table} LIMIT 1')
            rates = get_setting(source, 'rates')
            new_rates(rates)
            if decimal(rates['USD']) != 1:
                raise ValueError('Invalid base currency in backup')
            if get_setting(source, 'options') is not None:
                checked_options(get_setting(source, 'options'), copy.deepcopy(DEFAULTS))
            migrate(source, safety=False)
            source.commit()
            with connect() as current:
                if current.execute('SELECT 1 FROM shifts WHERE closed IS NULL').fetchone():
                    raise ValueError('Close the cash shift before restoring a backup')
                safety = local_backup(current, prefix='before-restore')
                source.backup(current)
        with connect() as db:
            revision(db)
        return {'safety_copy': safety}
    finally:
        temp.unlink(missing_ok=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload, kind='application/json; charset=utf-8', filename=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if kind.startswith('application/json') else payload
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Host rejected'})
        try:
            with LOCK:
                if self.path in ('/api/state', '/api/sales'):
                    with connect() as db:
                        self.reply(200, snapshot(db) if self.path.endswith('state') else sales_list(db))
                elif self.path == '/api/backup-download':
                    with connect() as db:
                        result = backup(db)
                    self.reply(200, Path(result['file']).read_bytes(), 'application/octet-stream', Path(result['file']).name)
                elif self.path in ('/', '/app.js', '/style.css', '/extra.js', '/translations.js'):
                    name = 'index.html' if self.path == '/' else self.path[1:]
                    kind = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
                    self.reply(200, (ROOT / 'static' / name).read_bytes(), kind + '; charset=utf-8')
                else:
                    self.reply(404, {'error': 'Not found'})
        except (ValueError, sqlite3.Error, OSError) as exc:
            self.reply(400, {'error': str(exc)})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin != f'http://{self.headers.get("Host")}'):
            return self.reply(403, {'error': 'Origin rejected'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if size < 0 or size > 96 * 1024 * 1024:
                raise ValueError('Request too large (maximum 64 MB database)')
            data = json.loads(self.rfile.read(size))
            with LOCK:
                if self.path == '/api/choose-backup-folder':
                    if CHOOSE_FOLDER is None:
                        raise ValueError('Folder picker is available in the desktop launcher')
                    result = {'folder': CHOOSE_FOLDER()}
                elif self.path == '/api/restore':
                    result = restore_database(data['file'], data['confirmed'])
                else:
                    with connect() as db:
                        result = mutate(db, self.path, data)
            self.reply(200, result)
        except (ValueError, KeyError, sqlite3.Error, TypeError, ArithmeticError, OSError) as exc:
            self.reply(400, {'error': str(exc)})

def daily_backup(stop):
    while not stop.wait(24 * 60 * 60):
        with LOCK:
            with connect() as db:
                backup(db)

def create_server(port=8765):
    initialize()
    with connect() as db:
        backup(db)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = create_server(args.port)
    stop = threading.Event()
    threading.Thread(target=daily_backup, args=(stop,), daemon=True).start()
    print(f'Nile Store {VERSION}: http://127.0.0.1:{server.server_port} | Data: {DATA}', flush=True)
    if not args.no_browser:
        threading.Timer(.5, lambda: webbrowser.open(f'http://127.0.0.1:{server.server_port}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()

if __name__ == '__main__':
    main()
