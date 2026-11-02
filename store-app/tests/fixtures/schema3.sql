-- Actual 2.1.1 layout (schema 3), before multiple cash registers existed.
CREATE TABLE products(id TEXT PRIMARY KEY,name TEXT NOT NULL,barcode TEXT UNIQUE NOT NULL,unit TEXT NOT NULL,price REAL NOT NULL);
CREATE TABLE batches(id INTEGER PRIMARY KEY,product TEXT REFERENCES products(id),received TEXT NOT NULL,qty INTEGER NOT NULL,remaining INTEGER NOT NULL,cost REAL NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE sales(id TEXT PRIMARY KEY,created TEXT NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL,total REAL NOT NULL,cost_egp REAL NOT NULL,rates TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE shifts(id INTEGER PRIMARY KEY,opened TEXT NOT NULL,closed TEXT,opening TEXT NOT NULL,closing TEXT,summary TEXT);
CREATE UNIQUE INDEX one_open_shift ON shifts((1)) WHERE closed IS NULL;
CREATE TABLE voids(sale_id TEXT PRIMARY KEY REFERENCES sales(id),created TEXT NOT NULL,reason TEXT NOT NULL,shift_id INTEGER NOT NULL REFERENCES shifts(id),fee_refunded INTEGER NOT NULL);
CREATE TABLE cash_movements(id INTEGER PRIMARY KEY,shift_id INTEGER REFERENCES shifts(id),created TEXT,currency TEXT,amount REAL,reason TEXT);
CREATE TABLE movements(id INTEGER PRIMARY KEY,created TEXT,product TEXT REFERENCES products(id),kind TEXT,qty INTEGER,reason TEXT,detail TEXT);
CREATE TABLE label_prices(product TEXT PRIMARY KEY REFERENCES products(id),signature TEXT NOT NULL,printed TEXT NOT NULL);
CREATE TABLE sale_returns(id TEXT PRIMARY KEY,sale_id TEXT NOT NULL REFERENCES sales(id),created TEXT NOT NULL,shift_id INTEGER NOT NULL REFERENCES shifts(id),request_id TEXT NOT NULL UNIQUE,request_payload TEXT NOT NULL,detail TEXT NOT NULL);
CREATE INDEX returns_by_sale ON sale_returns(sale_id);
ALTER TABLE products ADD COLUMN min_stock REAL NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN supplier TEXT NOT NULL DEFAULT '';
ALTER TABLE batches ADD COLUMN invoice TEXT NOT NULL DEFAULT '';
ALTER TABLE batches ADD COLUMN origin TEXT NOT NULL DEFAULT 'purchase';
ALTER TABLE sales ADD COLUMN shift_id INTEGER REFERENCES shifts(id);
PRAGMA user_version=3;
