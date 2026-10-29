"""Local retail store application. Python 3.10+, standard library only."""
import json, sqlite3, uuid, os, argparse, webbrowser, threading
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).parent
DATA=Path(os.environ.get('STORE_DATA_DIR', Path.home()/'.retail-store'))
CURRENCIES=('USD','EUR','EGP','RUB')
def money(n):
    value=Decimal(str(n))
    if not value.is_finite(): raise ValueError('Amount must be finite')
    return float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
def positive(v):
    d=Decimal(str(v))
    if not d.is_finite() or d<=0: raise ValueError('Value must be positive')
    return float(d)
def connect():
    DATA.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA/'store.sqlite',timeout=15)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,barcode TEXT UNIQUE NOT NULL,unit TEXT NOT NULL,price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,product TEXT REFERENCES products(id),received TEXT NOT NULL,qty INTEGER NOT NULL,remaining INTEGER NOT NULL,cost REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sales(id TEXT PRIMARY KEY,created TEXT NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL,total REAL NOT NULL,cost_egp REAL NOT NULL,rates TEXT NOT NULL,detail TEXT NOT NULL);
        ''')
        db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('rates',json.dumps({'USD':1,'EUR':1,'EGP':1,'RUB':1})))
        db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('configured','false'))

def snapshot(db):
    products=[dict(r) for r in db.execute('SELECT p.*,COALESCE(SUM(b.remaining),0)/1000.0 stock FROM products p LEFT JOIN batches b ON p.id=b.product GROUP BY p.id ORDER BY p.name')]
    batches=[dict(r) for r in db.execute('SELECT b.*,p.name FROM batches b JOIN products p ON p.id=b.product ORDER BY b.id')]
    return {'products':products,'batches':batches,'rates':json.loads(db.execute("SELECT value FROM settings WHERE key='rates'").fetchone()[0]),'configured':db.execute("SELECT value FROM settings WHERE key='configured'").fetchone()[0]=='true'}

def quantity(value,unit):
    n=Decimal(str(value))
    if not n.is_finite() or n<=0 or n*1000!=(n*1000).to_integral_value() or (unit=='piece' and n!=n.to_integral_value()):
        raise ValueError('Invalid quantity: pieces must be whole; kilograms allow 3 decimals')
    return int(n*1000)

def discount(base,spec):
    amount=Decimal(str(spec.get('value',0)))
    if not amount.is_finite() or amount<0: raise ValueError('Invalid discount')
    if spec.get('type')=='percent':
        if amount>100: raise ValueError('Discount exceeds 100%')
        result=money(Decimal(str(base))*amount/100)
    else: result=money(amount)
    if result>base: raise ValueError('Discount exceeds amount')
    return result

def checkout(db,data):
    db.execute('BEGIN IMMEDIATE')
    state=snapshot(db)
    if not state['configured']: raise ValueError('Set exchange rates before selling')
    currency=data['currency']; method=data['method']
    if currency not in CURRENCIES or method not in ('cash','card','transfer') or (method=='card' and currency!='EGP') or (method=='transfer' and currency!='RUB'):
        raise ValueError('Invalid payment currency / method')
    rates=state['rates']; lines=[]; cost=0
    if not data.get('items'): raise ValueError('Cart is empty')
    for item in data['items']:
        p=db.execute('SELECT * FROM products WHERE id=?',(item['id'],)).fetchone()
        if not p: raise ValueError('Product not found')
        qty=quantity(item['qty'],p['unit']); left=qty; allocations=[]
        for batch in db.execute('SELECT * FROM batches WHERE product=? AND remaining>0 ORDER BY received,id',(p['id'],)).fetchall():
            take=min(left,batch['remaining']); left-=take
            db.execute('UPDATE batches SET remaining=remaining-? WHERE id=?',(take,batch['id']))
            cost+=take/1000*batch['cost']; allocations.append({'batch':batch['id'],'qty':take/1000,'cost_egp':batch['cost']})
            if left==0: break
        if left: raise ValueError('Insufficient stock: '+p['name'])
        price=money(p['price']*rates[currency]); gross=money(price*qty/1000)
        reduction=discount(gross,item.get('discount',{}))
        lines.append({'id':p['id'],'name':p['name'],'barcode':p['barcode'],'unit':p['unit'],'qty':qty/1000,'price':price,'gross':gross,'discount':reduction,'net':money(gross-reduction),'fifo':allocations})
    subtotal=money(sum(l['net'] for l in lines)); reduction=discount(subtotal,data.get('discount',{})); total=money(subtotal-reduction)
    tender=money(data.get('tender',total)) if method=='cash' else total
    if tender<total: raise ValueError('Cash received is below total')
    sale={'id':uuid.uuid4().hex[:12].upper(),'created':datetime.now(timezone.utc).isoformat(),'currency':currency,'method':method,'total':total,'cost_egp':money(cost),'rates':rates,'items':lines,'subtotal':subtotal,'discount':reduction,'tender':tender,'change':money(tender-total)}
    db.execute('INSERT INTO sales VALUES (?,?,?,?,?,?,?,?)',(sale['id'],sale['created'],currency,method,total,sale['cost_egp'],json.dumps(rates),json.dumps(sale,ensure_ascii=False)))
    db.commit()
    return sale

def mutate(db,path,data):
    if path=='/api/products':
        name=data['name'].strip(); barcode=data.get('barcode','').strip().upper() or ('29'+str(uuid.uuid4().int)[:6])
        if not name or data['unit'] not in ('piece','kg'): raise ValueError('Name and unit are required')
        price=positive(data['price'])
        if not barcode.isascii() or not barcode.isalnum(): raise ValueError('Use ASCII letters and digits for barcode')
        db.execute('INSERT INTO products VALUES (?,?,?,?,?)',(uuid.uuid4().hex,name,barcode,data['unit'],price)); db.commit()
    elif path=='/api/product-update':
        name=data['name'].strip();barcode=data['barcode'].strip().upper()
        if not name or not barcode or not barcode.isascii() or not barcode.isalnum(): raise ValueError('Name and ASCII barcode are required')
        if not db.execute('SELECT id FROM products WHERE id=?',(data['id'],)).fetchone(): raise ValueError('Product not found')
        db.execute('UPDATE products SET name=?,barcode=?,price=? WHERE id=?',(name,barcode,positive(data['price']),data['id']));db.commit()
    elif path=='/api/batches':
        p=db.execute('SELECT * FROM products WHERE id=?',(data['product'],)).fetchone()
        if not p: raise ValueError('Product not found')
        qty=quantity(data['qty'],p['unit']);cost=positive(data['cost'])
        db.execute('INSERT INTO batches(product,received,qty,remaining,cost) VALUES (?,?,?,?,?)',(p['id'],datetime.now(timezone.utc).isoformat(),qty,qty,cost));db.commit()
    elif path=='/api/rates':
        rates={c:(1 if c=='USD' else positive(data[c])) for c in CURRENCIES}
        db.execute("UPDATE settings SET value=? WHERE key='rates'",(json.dumps(rates),))
        db.execute("UPDATE settings SET value='true' WHERE key='configured'");db.commit()
    elif path=='/api/checkout': return checkout(db,data)
    elif path=='/api/backup':
        folder=DATA/'backups';folder.mkdir(exist_ok=True)
        target=folder/('store-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite')
        with sqlite3.connect(target) as dest: db.backup(dest)
        return {'file':str(target)}
    else: raise ValueError('Unknown operation')
    return snapshot(db)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,code,payload,kind='application/json; charset=utf-8'):
        body=json.dumps(payload,ensure_ascii=False).encode() if kind.startswith('application/json') else payload
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if self.path=='/api/state':
            with connect() as db:self.reply(200,snapshot(db))
        elif self.path=='/api/sales':
            with connect() as db:self.reply(200,[json.loads(r['detail']) for r in db.execute('SELECT detail FROM sales ORDER BY created DESC')])
        elif self.path in ('/','/app.js','/style.css'):
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[self.path]
            self.reply(200,(ROOT/'static'/name).read_bytes(),{'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}[name])
        else:self.reply(404,{'error':'Not found'})
    def do_POST(self):
        # Accept same-origin browser requests only: protects local data from foreign websites.
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://{self.headers.get("Host")}': return self.reply(403,{'error':'Origin rejected'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.reply(415,{'error':'JSON required'})
        try:
            size=int(self.headers.get('Content-Length',0))
            if size>1000000: raise ValueError('Request too large')
            data=json.loads(self.rfile.read(size))
            with connect() as db: result=mutate(db,self.path,data)
            self.reply(200,result)
        except (ValueError,KeyError,sqlite3.Error,TypeError,ArithmeticError) as exc:self.reply(400,{'error':str(exc)})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    initialize()
    with connect() as db:mutate(db,'/api/backup',{})
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Store: http://127.0.0.1:{args.port} | Data: {DATA}',flush=True)
    if not args.no_browser: threading.Timer(.5,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':main()
