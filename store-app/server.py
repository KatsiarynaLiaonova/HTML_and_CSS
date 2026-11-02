"""ALI BABA: local retail accounting, standard library server."""
import argparse
import hmac
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
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath, PureWindowsPath
from access import Accounts, AccessError, may_write

VERSION = '3.0.1'
SCHEMA = 4
ROOT = Path(__file__).parent
DATA = Path(os.environ.get('STORE_DATA_DIR', Path.home() / '.retail-store'))
CURRENCIES = ('USD', 'EUR', 'EGP', 'RUB')
LOCK = threading.RLock()
BACKUP_ERROR = ''
CHOOSE_FOLDER = None
DEFAULTS = {
    'rounding': {c: .01 for c in CURRENCIES},
    'fees': {m: {'percent': 0, 'fixed': 0} for m in ('card', 'transfer')},
    'backup_dir': '', 'shop_name': 'ALI BABA', 'time_zone': 'Africa/Cairo',
    'template': {'width': 38, 'height': 25, 'receiptWidth': 70, 'labelRotation': 0},
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
        raise ValueError('This database needs a newer version of ALI BABA')
    if safety and old_version < SCHEMA and db.execute("SELECT 1 FROM sqlite_master WHERE name='sales'").fetchone():
        local_backup(db, prefix='before-upgrade')
    # SQLite rejects ADD COLUMN ... REFERENCES ... DEFAULT 1 on populated
    # tables while enforcement is enabled. Suspend it only on this migration
    # connection, validate every reference before committing, then restore it.
    # executescript historically committed pending work; do so before PRAGMA,
    # which cannot change foreign_keys inside an active transaction.
    db.commit()
    foreign_keys = db.execute('PRAGMA foreign_keys').fetchone()[0]
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        _migrate_schema(db, old_version)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Database integrity check failed during upgrade')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute(f'PRAGMA foreign_keys={foreign_keys}')

def _migrate_schema(db, old_version):
    db.executescript('''
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,barcode TEXT UNIQUE NOT NULL,unit TEXT NOT NULL,price REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,product TEXT REFERENCES products(id),received TEXT NOT NULL,qty INTEGER NOT NULL,remaining INTEGER NOT NULL,cost REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales(id TEXT PRIMARY KEY,created TEXT NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL,total REAL NOT NULL,cost_egp REAL NOT NULL,rates TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS registers(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
    INSERT OR IGNORE INTO registers VALUES (1,'Касса 1');
    INSERT OR IGNORE INTO registers VALUES (2,'Касса 2');
    CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY,opened TEXT NOT NULL,closed TEXT,opening TEXT NOT NULL,closing TEXT,summary TEXT);
    CREATE TABLE IF NOT EXISTS voids(sale_id TEXT PRIMARY KEY REFERENCES sales(id),created TEXT NOT NULL,reason TEXT NOT NULL,shift_id INTEGER NOT NULL REFERENCES shifts(id),fee_refunded INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS cash_movements(id INTEGER PRIMARY KEY,shift_id INTEGER REFERENCES shifts(id),created TEXT,currency TEXT,amount REAL,reason TEXT);
    CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY,created TEXT,product TEXT REFERENCES products(id),kind TEXT,qty INTEGER,reason TEXT,detail TEXT);
    CREATE TABLE IF NOT EXISTS label_prices(product TEXT PRIMARY KEY REFERENCES products(id),signature TEXT NOT NULL,printed TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sale_returns(id TEXT PRIMARY KEY,sale_id TEXT NOT NULL REFERENCES sales(id),created TEXT NOT NULL,shift_id INTEGER NOT NULL REFERENCES shifts(id),request_id TEXT NOT NULL UNIQUE,request_payload TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS returns_by_sale ON sale_returns(sale_id);
    CREATE TABLE IF NOT EXISTS sale_requests(request_id TEXT PRIMARY KEY,register_id INTEGER NOT NULL REFERENCES registers(id),payload TEXT NOT NULL,sale_id TEXT NOT NULL REFERENCES sales(id));
    CREATE TABLE IF NOT EXISTS register_print_settings(register_id INTEGER PRIMARY KEY REFERENCES registers(id),settings TEXT NOT NULL);
    ''')
    additions = {
        'products': {'min_stock': 'REAL NOT NULL DEFAULT 0'},
        'batches': {'supplier': "TEXT NOT NULL DEFAULT ''", 'invoice': "TEXT NOT NULL DEFAULT ''", 'origin': "TEXT NOT NULL DEFAULT 'purchase'"},
        'sales': {'shift_id': 'INTEGER REFERENCES shifts(id)', 'register_id': 'INTEGER NOT NULL DEFAULT 1 REFERENCES registers(id)'},
        'shifts': {'register_id': 'INTEGER NOT NULL DEFAULT 1 REFERENCES registers(id)'},
    }
    for table, columns in additions.items():
        existing = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    db.execute('DROP INDEX IF EXISTS one_open_shift')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_open_shift_per_register ON shifts(register_id) WHERE closed IS NULL')
    for key, value in {'rates': dict.fromkeys(CURRENCIES, 1), 'configured': False, 'options': DEFAULTS, 'store_id': uuid.uuid4().hex, 'revision': uuid.uuid4().hex}.items():
        if get_setting(db, key) is None:
            put_setting(db, key, value)
    options = get_setting(db, 'options')
    if 'time_zone' not in options:
        options['time_zone'] = DEFAULTS['time_zone']
        put_setting(db, 'options', options)
    if old_version < 3 and options.get('shop_name') in ('NILE', 'Nile Store'):
        options['shop_name'] = 'ALI BABA'
        put_setting(db, 'options', options)
        revision(db)
    if 'labelRotation' not in options['template']:
        options['template']['labelRotation'] = 0
        put_setting(db, 'options', options)
    db.execute(f'PRAGMA user_version={SCHEMA}')

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
    layout = {k: v for k, v in options['template'].items() if k != 'labelRotation'}
    return json.dumps([product['name'], product['barcode'], [price_for(product, c, rates, options) for c in CURRENCIES], layout], ensure_ascii=False)

def sales_list(db, register_id=None):
    returns = {}
    for row in db.execute('SELECT sale_id,detail FROM sale_returns ORDER BY rowid'):
        returns.setdefault(row['sale_id'], []).append(json.loads(row['detail']))
    rows = db.execute('SELECT s.detail,s.register_id,v.created AS voided,v.reason,v.fee_refunded,vs.register_id AS void_register_id FROM sales s LEFT JOIN voids v ON v.sale_id=s.id LEFT JOIN shifts vs ON vs.id=v.shift_id WHERE (? IS NULL OR s.register_id=?) ORDER BY s.created DESC', (register_id, register_id))
    result = []
    for row in rows:
        sale = json.loads(row['detail'])
        sale.setdefault('register_id', row['register_id'])
        sale['void'] = {'created': row['voided'], 'reason': row['reason'], 'fee_refunded': bool(row['fee_refunded']), 'register_id': row['void_register_id'] or 1} if row['voided'] else None
        sale['returns'] = returns.get(sale['id'], [])
        sale['return_lines'] = return_lines(sale, sale['returns'])
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
    for row in db.execute('SELECT detail FROM sale_returns WHERE shift_id=?', (shift['id'],)):
        r = json.loads(row['detail'])
        totals[r['currency']][r['method']] -= r['total']
        fees[r['currency']] -= r['fee_refund']
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

def snapshot(db, register_id=1):
    rates, options = get_setting(db, 'rates'), get_setting(db, 'options')
    print_settings = db.execute('SELECT settings FROM register_print_settings WHERE register_id=?', (register_id,)).fetchone()
    products = [dict(r) for r in db.execute('SELECT p.*,COALESCE(SUM(b.remaining),0)/1000.0 stock,COALESCE(SUM(b.remaining*b.cost),0)/1000.0 stock_value FROM products p LEFT JOIN batches b ON p.id=b.product GROUP BY p.id ORDER BY p.name')]
    printed = {r['product']: r['signature'] for r in db.execute('SELECT * FROM label_prices')}
    for p in products:
        p['prices'] = {c: price_for(p, c, rates, options) for c in CURRENCIES}
        p['signature'] = signature(p, rates, options)
        p['label_changed'] = printed.get(p['id']) != p['signature']
    if print_settings:
        options['template'].update(json.loads(print_settings['settings']))
    return {
        'store_id': get_setting(db, 'store_id'), 'register_id': register_id, 'registers': [dict(r) for r in db.execute('SELECT * FROM registers ORDER BY id')],
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

def apportioned_cents(amount, weights):
    """Assign a receipt's cents once, including the final rounding remainder."""
    total = int(decimal(amount) * 100)
    weights = [decimal(w) for w in weights]
    denominator = sum(weights, Decimal(0))
    if not denominator:
        if total:
            raise ValueError('Invalid original receipt amounts')
        return [0] * len(weights)
    shares = [Decimal(total) * w / denominator for w in weights]
    result = [int(s) for s in shares]
    order = sorted(range(len(weights)), key=lambda i: (-(shares[i] - result[i]), i))
    for i in order[:total - sum(result)]:
        result[i] += 1
    return result

def return_lines(sale, returns):
    totals = apportioned_cents(sale['total'], [line['net'] for line in sale['items']])
    result = []
    for index, line in enumerate(sale['items']):
        bought = quantity(line['qty'], line['unit'])
        returned = sum(quantity(item['qty'], line['unit']) for event in returns for item in event['items'] if item['line'] == index)
        refunded = sum((decimal(item['refund']) for event in returns for item in event['items'] if item['line'] == index), Decimal(0))
        result.append({'line': index, 'qty': bought / 1000, 'returned_qty': returned / 1000,
                       'remaining_qty': (bought - returned) / 1000, 'refund_total': totals[index] / 100,
                       'refunded': money(refunded), 'refund_remaining': money(decimal(totals[index]) / 100 - refunded)})
    return result

def fifo_slice(line, start, count):
    """Select units from the *original* sale, never from current stock/prices."""
    result = []
    left = count
    for allocation in line['fifo']:
        size = quantity(allocation['qty'], line['unit'])
        skipped = min(start, size)
        start -= skipped
        take = min(size - skipped, left)
        if take:
            result.append({'batch': allocation['batch'], 'qty': take / 1000, 'cost_egp': allocation['cost_egp']})
            left -= take
        if not left:
            return result
    if left:
        raise ValueError('Original receipt has incomplete batch allocations')
    return result

def allocation_cost(allocations):
    return sum((decimal(a['qty']) * decimal(a['cost_egp']) for a in allocations), Decimal(0))

def return_preview(db, data, register_id=1):
    shift = active_shift(db, register_id)
    row = db.execute('SELECT detail FROM sales WHERE id=?', (data['id'],)).fetchone()
    if not row:
        raise ValueError('Sale not found')
    sale = json.loads(row['detail'])
    if db.execute('SELECT 1 FROM voids WHERE sale_id=?', (sale['id'],)).fetchone():
        raise ValueError('Cannot return a cancelled sale')
    previous = [json.loads(r['detail']) for r in db.execute('SELECT detail FROM sale_returns WHERE sale_id=? ORDER BY rowid', (sale['id'],))]
    remaining = return_lines(sale, previous)
    selected = data.get('items')
    if not isinstance(selected, list) or not selected:
        raise ValueError('Select at least one item to return')
    fee = decimal(data.get('fee_refund', 0))
    fee_remaining = decimal(sale.get('fee', 0)) - sum((decimal(r['fee_refund']) for r in previous), Decimal(0))
    if fee < 0 or fee * 100 != (fee * 100).to_integral_value() or fee > fee_remaining:
        raise ValueError('Fee refund must not exceed the remaining original commission')
    line_costs = [allocation_cost(line['fifo']) for line in sale['items']]
    cost_cents = apportioned_cents(sale['cost_egp'], line_costs)
    items, seen = [], set()
    for choice in selected:
        index = choice['line']
        if type(index) is not int or index < 0 or index >= len(sale['items']) or index in seen:
            raise ValueError('Select each original receipt line only once')
        seen.add(index)
        line, available = sale['items'][index], remaining[index]
        count = quantity(choice['qty'], line['unit'])
        before = quantity(available['returned_qty'], line['unit'], zero=True)
        bought = quantity(line['qty'], line['unit'])
        if before + count > bought:
            raise ValueError('Return exceeds the quantity still available on this receipt')
        restock = choice.get('restock', True)
        if type(restock) is not bool:
            raise ValueError('Choose whether the returned item can be restocked')
        total_cents = int(decimal(available['refund_total']) * 100)
        def refund_at(qty):
            return int((decimal(total_cents) * qty / bought).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        allocations = fifo_slice(line, before, count)
        def cost_at(qty):
            if not qty or not line_costs[index]:
                return 0
            raw = allocation_cost(fifo_slice(line, 0, qty))
            return int((decimal(cost_cents[index]) * raw / line_costs[index]).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        original_cost = (cost_at(before + count) - cost_at(before)) / 100
        items.append({'line': index, 'product': line['id'], 'name': line['name'], 'unit': line['unit'], 'qty': count / 1000,
                      'restock': restock, 'refund': (refund_at(before + count) - refund_at(before)) / 100,
                      'cost_egp': original_cost if restock else 0,
                      'loss_cost_egp': 0 if restock else original_cost, 'fifo': allocations})
    return {'sale_id': sale['id'], 'currency': sale['currency'], 'method': sale['method'],
            'rates': sale['rates'], 'shop_name': sale.get('shop_name', 'ALI BABA'),
            'items': items, 'total': money(sum(decimal(i['refund']) for i in items)),
            'cost_egp': money(sum(decimal(i['cost_egp']) for i in items)),
            'loss_cost_egp': money(sum(decimal(i['loss_cost_egp']) for i in items)),
            'fee_refund': money(fee), 'fee_remaining': money(fee_remaining),
            'return_count': len(previous), 'shift_id': shift['id'], 'register_id': register_id}

def customer_return(db, data, register_id=1):
    request_id = data.get('request_id')
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
        raise ValueError('A unique return request ID is required')
    payload = json.dumps({k: data.get(k) for k in ('id', 'items', 'reason', 'confirmed', 'fee_refund', 'return_count')}, sort_keys=True, allow_nan=False)
    existing = db.execute('SELECT request_payload,detail FROM sale_returns WHERE request_id=?', (request_id,)).fetchone()
    if existing:
        if existing['request_payload'] != payload:
            raise ValueError('This return request ID was already used for a different operation')
        event = json.loads(existing['detail'])
        if event.get('register_id', 1) != register_id:
            raise ValueError('Return request belongs to another cash register')
        return event
    reason = data.get('reason')
    if not isinstance(reason, str) or not reason.strip() or data.get('confirmed') is not True:
        raise ValueError('Confirm the money was returned and provide a reason')
    event = return_preview(db, data, register_id)
    if type(data.get('return_count')) is not int or data['return_count'] != event['return_count']:
        raise ValueError('Returns changed. Preview this return again')
    event.update(id=uuid.uuid4().hex[:12].upper(), created=now(), reason=reason.strip(), request_id=request_id)
    for item in event['items']:
        product = db.execute('SELECT id FROM products WHERE id=?', (item['product'],)).fetchone()
        if not product:
            raise ValueError('Original product not found')
        if item['restock']:
            for allocation in item['fifo']:
                db.execute('UPDATE batches SET remaining=remaining+? WHERE id=?', (quantity(allocation['qty'], item['unit']), allocation['batch']))
        record_movement(db, product['id'], 'return' if item['restock'] else 'return-damaged',
                        quantity(item['qty'], item['unit']) if item['restock'] else 0,
                        event['reason'] + ' / ' + event['sale_id'] + ' / ' + event['id'], item['fifo'])
    db.execute('INSERT INTO sale_returns VALUES (?,?,?,?,?,?,?)',
               (event['id'], event['sale_id'], event['created'], event['shift_id'], request_id, payload, json.dumps(event, ensure_ascii=False)))
    return event

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

def active_shift(db, register_id=1):
    if type(register_id) is not int or not db.execute('SELECT 1 FROM registers WHERE id=?', (register_id,)).fetchone():
        raise ValueError('Cash register not found')
    shift = db.execute('SELECT * FROM shifts WHERE closed IS NULL AND register_id=?', (register_id,)).fetchone()
    if not shift:
        raise ValueError('Open a cash shift before this operation')
    return shift

def checkout(db, data, register_id=1):
    db.execute('BEGIN IMMEDIATE')
    if data.get('store_id') is not None and data['store_id'] != get_setting(db, 'store_id'):
        raise ValueError('The store database changed. Reload the application')
    request_id = data.get('request_id')
    payload = None
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
            raise ValueError('A unique sale request ID is required')
        payload = json.dumps({k: v for k, v in data.items() if k != 'request_id'}, sort_keys=True, allow_nan=False)
        previous = db.execute('SELECT * FROM sale_requests WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if previous['register_id'] != register_id or previous['payload'] != payload:
                raise ValueError('Sale request ID already belongs to another operation')
            result = json.loads(db.execute('SELECT detail FROM sales WHERE id=?', (previous['sale_id'],)).fetchone()[0])
            db.commit()
            return result
    shift = active_shift(db, register_id)
    state = snapshot(db, register_id)
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
    sale = {'id': uuid.uuid4().hex[:12].upper(), 'created': now(), 'currency': currency, 'method': method, 'total': total, 'cost_egp': money(cost), 'rates': rates, 'items': lines, 'subtotal': subtotal, 'discount': reduction, 'tender': tender, 'change': money(decimal(tender) - decimal(total)), 'fee': fee, 'fee_rule': fee_rule, 'shift_id': shift['id'], 'register_id': register_id, 'shop_name': options['shop_name']}
    for line in lines:
        record_movement(db, line['id'], 'sale', -quantity(line['qty'], line['unit']), sale['id'], line['fifo'])
    db.execute('INSERT INTO sales(id,created,currency,method,total,cost_egp,rates,detail,shift_id,register_id) VALUES (?,?,?,?,?,?,?,?,?,?)', (sale['id'], sale['created'], currency, method, total, sale['cost_egp'], json.dumps(rates), json.dumps(sale, ensure_ascii=False), shift['id'], register_id))
    if request_id is not None:
        db.execute('INSERT INTO sale_requests VALUES (?,?,?,?)', (request_id, register_id, payload, sale['id']))
    db.commit()
    return sale

def checked_options(data, current):
    result = copy.deepcopy(current)
    for key in data:
        if key not in DEFAULTS:
            raise ValueError('Unknown setting')
    if 'time_zone' in data:
        if data['time_zone'] not in ('Africa/Cairo', 'UTC', 'Europe/Moscow', 'Europe/Minsk', 'Europe/Istanbul'):
            raise ValueError('Choose a supported store time zone')
        result['time_zone'] = data['time_zone']
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
        for key, value in template.items():
            if decimal(value) == decimal(current['template'][key]):
                template[key] = current['template'][key]
        rotation = data['template'].get('labelRotation', current['template'].get('labelRotation', 0))
        if isinstance(rotation, bool) or decimal(rotation) not in (0, 90, 180, 270):
            raise ValueError('Label print rotation must be 0, 90, 180 or 270 degrees')
        template['labelRotation'] = int(decimal(rotation))
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

def mutate(db, path, data, register_id=1):
    if path == '/api/checkout':
        return checkout(db, data, register_id)
    if path == '/api/preview':
        return preview(db, data)
    if path == '/api/return-preview':
        return return_preview(db, data, register_id)
    if path == '/api/backup':
        return backup(db)
    db.execute('BEGIN IMMEDIATE')
    if path == '/api/returns':
        result = customer_return(db, data, register_id)
        db.commit()
        return result
    if path == '/api/printer-options':
        options = checked_options({'template': data['template']}, get_setting(db, 'options'))
        db.execute('INSERT OR REPLACE INTO register_print_settings VALUES (?,?)', (register_id, json.dumps(options['template'])))
    elif path in ('/api/products', '/api/product-update'):
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
            if 'template' in data:
                db.execute('DELETE FROM register_print_settings WHERE register_id=?', (register_id,))
        else:
            if 'rates' in data:
                put_setting(db, 'rates', new_rates(data['rates']))
                put_setting(db, 'configured', True)
            put_setting(db, 'options', checked_options(data.get('options', {}), get_setting(db, 'options')))
        revision(db)
    elif path == '/api/shift-open':
        opening = {c: money(nonnegative(data['opening'][c])) for c in CURRENCIES}
        if type(register_id) is not int or not db.execute('SELECT 1 FROM registers WHERE id=?', (register_id,)).fetchone():
            raise ValueError('Cash register not found')
        db.execute('INSERT INTO shifts(opened,opening,register_id) VALUES (?,?,?)', (now(), json.dumps(opening), register_id))
    elif path == '/api/shift-close':
        shift = active_shift(db, register_id)
        if int(data['id']) != shift['id']:
            raise ValueError('Shift changed')
        closing = {c: money(nonnegative(data['closing'][c])) for c in CURRENCIES}
        summary = shift_summary(db, shift)
        summary['difference'] = {c: money(decimal(closing[c]) - decimal(summary['expected'][c])) for c in CURRENCIES}
        db.execute('UPDATE shifts SET closed=?,closing=?,summary=? WHERE id=?', (now(), json.dumps(closing), json.dumps(summary), shift['id']))
    elif path == '/api/cash-movement':
        shift = active_shift(db, register_id)
        reason = str(data['reason']).strip()
        amount = money(data['amount'])
        if data['currency'] not in CURRENCIES or not reason or not amount:
            raise ValueError('Currency, nonzero amount and reason are required')
        if shift_summary(db, shift)['expected'][data['currency']] + amount < 0:
            raise ValueError('Cash withdrawal exceeds expected cash')
        db.execute('INSERT INTO cash_movements(shift_id,created,currency,amount,reason) VALUES (?,?,?,?,?)', (shift['id'], now(), data['currency'], amount, reason))
    elif path == '/api/void':
        shift = active_shift(db, register_id)
        reason = str(data['reason']).strip()
        if not reason or data.get('confirmed') is not True:
            raise ValueError('Confirm payment reversal and provide a reason')
        row = db.execute('SELECT detail FROM sales WHERE id=?', (data['id'],)).fetchone()
        if not row:
            raise ValueError('Sale not found')
        s = json.loads(row['detail'])
        if db.execute('SELECT 1 FROM voids WHERE sale_id=?', (s['id'],)).fetchone():
            raise ValueError('Sale already cancelled')
        if db.execute('SELECT 1 FROM sale_returns WHERE sale_id=?', (s['id'],)).fetchone():
            raise ValueError('This receipt has customer returns. Return the remaining items instead of cancelling it')
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
    return snapshot(db, register_id)

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
                options = get_setting(source, 'options')
                folder = options.get('backup_dir', '')
                if folder and not Path(folder).is_absolute() and (PurePosixPath(folder).is_absolute() or PureWindowsPath(folder).is_absolute()):
                    options['backup_dir'] = ''
                    put_setting(source, 'options', options)
                checked_options(options, copy.deepcopy(DEFAULTS))
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
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def reply(self, code, payload, kind='application/json; charset=utf-8', filename=None, headers=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if kind.startswith('application/json') else payload
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        allowed = getattr(self.server, 'allowed_hosts', (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'))
        return self.headers.get('Host') in allowed

    def session_token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        token = cookies.get('ali_baba_session')
        return token.value if token else ''

    def identity(self):
        accounts = getattr(self.server, 'accounts', None)
        if accounts is None:
            return {'id': 0, 'login': 'local', 'role': 'owner', 'register_id': 1}, None
        return accounts.authenticate(self.session_token())

    def session_cookie(self, token, remove=False):
        # HTTPS listeners and the local Tailscale reverse proxy set this explicitly.
        secure = getattr(self.server, 'cookie_secure', False) or self.headers.get('Origin', '').startswith('https://') or self.headers.get('X-Forwarded-Proto') == 'https'
        value = 'ali_baba_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + ('0' if remove else '43200')
        return value + ('; Secure' if secure else '')

    def scoped_snapshot(self, result, account):
        result['account'] = account
        result['shared'] = getattr(self.server, 'accounts', None) is not None
        if account['role'] != 'owner':
            result['shifts'] = [s for s in result['shifts'] if s['register_id'] == account['register_id']]
        if getattr(self.server, 'cookie_secure', False) or self.headers.get('X-Forwarded-Proto') == 'https' or self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            result['can_choose_folder'] = False
        return result

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Host rejected'})
        try:
            account, csrf = self.identity() if self.path.startswith('/api/') else (None, None)
            register = (account['register_id'] or 1) if account else 1
            with LOCK:
                if self.path == '/api/me':
                    self.reply(200, {'account': account, 'csrf': csrf, 'shared': getattr(self.server, 'accounts', None) is not None})
                elif self.path == '/api/users':
                    if account['role'] != 'owner' or getattr(self.server, 'accounts', None) is None:
                        return self.reply(403, {'error': 'Owner access required'})
                    self.reply(200, self.server.accounts.users())
                elif self.path in ('/api/state', '/api/sales'):
                    with connect() as db:
                        if self.path.endswith('state'):
                            result = snapshot(db, register)
                            result = self.scoped_snapshot(result, account)
                            self.reply(200, result)
                        else:
                            self.reply(200, sales_list(db, register if account['role'] != 'owner' else None))
                elif self.path == '/api/backup-download':
                    if account['role'] != 'owner':
                        return self.reply(403, {'error': 'Owner access required'})
                    with connect() as db:
                        result = backup(db)
                    self.reply(200, Path(result['file']).read_bytes(), 'application/octet-stream', Path(result['file']).name)
                elif self.path in ('/', '/app.js', '/style.css', '/extra.js', '/translations.js', '/scanner.js', '/accounts.js'):
                    name = 'index.html' if self.path == '/' else self.path[1:]
                    kind = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
                    self.reply(200, (ROOT / 'static' / name).read_bytes(), kind + '; charset=utf-8')
                else:
                    self.reply(404, {'error': 'Not found'})
        except AccessError as exc:
            self.reply(401, {'error': str(exc)})
        except (ValueError, sqlite3.Error, OSError) as exc:
            self.reply(400, {'error': str(exc)})

    def do_POST(self):
        origin = self.headers.get('Origin')
        valid_origins = getattr(self.server, 'allowed_origins', {f'http://{self.headers.get("Host")}', f'https://{self.headers.get("Host")}'})
        if not self.valid_host() or (origin and origin not in valid_origins):
            return self.reply(403, {'error': 'Origin rejected'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if size < 0 or size > 96 * 1024 * 1024:
                raise ValueError('Request too large (maximum 64 MB database)')
            data = json.loads(self.rfile.read(size))
            accounts = getattr(self.server, 'accounts', None)
            if self.path == '/api/login':
                if accounts is None:
                    return self.reply(400, {'error': 'Accounts are available in shared mode'})
                token, csrf, account = accounts.login(data.get('login', ''), data.get('password', ''))
                return self.reply(200, {'account': account, 'csrf': csrf}, headers={'Set-Cookie': self.session_cookie(token)})
            account, csrf = self.identity()
            register = account['register_id'] or 1
            if accounts is not None:
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                    return self.reply(403, {'error': 'Session confirmation required. Sign in again'})
                if not may_write(account, self.path):
                    return self.reply(403, {'error': 'Owner access required'})
                if self.path == '/api/checkout' and not data.get('request_id'):
                    return self.reply(400, {'error': 'A unique sale request ID is required'})
                if self.path == '/api/logout':
                    accounts.logout(self.session_token())
                    return self.reply(200, {'ok': True}, headers={'Set-Cookie': self.session_cookie('', remove=True)})
                if self.path == '/api/users':
                    result = accounts.create(data['login'], data['password'], data.get('role', 'cashier'), data.get('register_id'))
                    return self.reply(200, result)
            if self.path == '/api/choose-backup-folder':
                if CHOOSE_FOLDER is None or getattr(self.server, 'cookie_secure', False) or self.headers.get('X-Forwarded-Proto') == 'https' or self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                    raise ValueError('Choose the backup folder on the main computer')
                return self.reply(200, {'folder': CHOOSE_FOLDER()})
            with LOCK:
                if accounts is not None and account['role'] != 'owner' and self.path in ('/api/void', '/api/return-preview', '/api/returns'):
                    with connect() as db:
                        original = db.execute('SELECT register_id FROM sales WHERE id=?', (data.get('id'),)).fetchone()
                    if not original or original['register_id'] != register:
                        return self.reply(403, {'error': 'Receipt belongs to another cash register'})
                if self.path == '/api/restore':
                    result = restore_database(data['file'], data['confirmed'])
                else:
                    with connect() as db:
                        result = mutate(db, self.path, data, register_id=register)
            if isinstance(result, dict) and 'products' in result and 'shifts' in result:
                result = self.scoped_snapshot(result, account)
            self.reply(200, result)
        except AccessError as exc:
            self.reply(401, {'error': str(exc)})
        except (ValueError, KeyError, sqlite3.Error, TypeError, ArithmeticError, OSError) as exc:
            self.reply(400, {'error': str(exc)})

def daily_backup(stop):
    while not stop.wait(24 * 60 * 60):
        with LOCK:
            with connect() as db:
                backup(db)

def create_server(port=8765, accounts=None, allowed_hosts=None):
    initialize()
    with connect() as db:
        backup(db)
    service = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    service.daemon_threads = False
    service.accounts = accounts
    if allowed_hosts is not None:
        service.allowed_hosts = allowed_hosts
    return service

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = create_server(args.port)
    stop = threading.Event()
    threading.Thread(target=daily_backup, args=(stop,), daemon=True).start()
    print(f'ALI BABA {VERSION}: http://127.0.0.1:{server.server_port} | Data: {DATA}', flush=True)
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
