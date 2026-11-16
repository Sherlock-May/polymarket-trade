from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS market_rounds(
 slug TEXT PRIMARY KEY, rule_id TEXT NOT NULL, mode TEXT NOT NULL,
 window_start REAL NOT NULL, window_end REAL NOT NULL,
 entry_deadline REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
 local_id TEXT PRIMARY KEY, slug TEXT NOT NULL REFERENCES market_rounds(slug),
 asset_id TEXT NOT NULL, side TEXT NOT NULL, purpose TEXT NOT NULL,
 exchange_id TEXT UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(local_id),
 timestamp REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions(
 slug TEXT NOT NULL REFERENCES market_rounds(slug), asset_id TEXT NOT NULL,
 data TEXT NOT NULL, PRIMARY KEY(slug,asset_id));
CREATE TABLE IF NOT EXISTS market_snapshots(
 id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, asset_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bot_events(
 id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, level TEXT NOT NULL,
 event TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS order_round_idx ON orders(slug);
CREATE INDEX IF NOT EXISTS snapshot_time_idx ON market_snapshots(timestamp);
"""


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        with self.conn:
            yield

    def put_round(self, r):
        m = r["market"]
        self.conn.execute("INSERT INTO market_rounds VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET status=excluded.status,data=excluded.data",
                          (m["slug"], r["rule"]["id"], r["mode"], m["start"], m["end"], r["deadline"], r["status"], dumps(r)))

    def put_order(self, o):
        self.conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(local_id) DO UPDATE SET exchange_id=excluded.exchange_id,status=excluded.status,data=excluded.data",
                          (o["local_id"], o["slug"], o["asset_id"], o["side"], o["purpose"], o.get("exchange_id"), o["status"], dumps(o)))

    def insert_fill(self, f):
        return self.conn.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?)", (f["id"], f["order_id"], f["timestamp"], dumps(f))).rowcount == 1

    def rounds(self):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM market_rounds ORDER BY window_start")]

    def orders(self):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM orders")]

    def fills(self):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM fills ORDER BY timestamp,id")]

    def meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def close(self):
        self.conn.close()
