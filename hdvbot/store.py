"""Stockage local (SQLite) : historique des ventes, watchlist, anti-spam."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

from .fields import Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    uid TEXT PRIMARY KEY,
    item TEXT NOT NULL,
    price REAL NOT NULL,
    qty INTEGER NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_item_ts ON trades(item, ts DESC);
CREATE TABLE IF NOT EXISTS watch (
    item TEXT PRIMARY KEY,
    max_price REAL,
    mode TEXT NOT NULL DEFAULT 'cheaper',
    added_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    data TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (kind, item)
);
CREATE TABLE IF NOT EXISTS names (
    item TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # --- ventes ----------------------------------------------------------------
    def add_trades(self, trades: Iterable[Trade]) -> int:
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO trades(uid, item, price, qty, ts) VALUES (?,?,?,?,?)",
            [(t.uid, t.item, t.price, t.qty, t.ts) for t in trades],
        )
        self.db.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def has_trade(self, uid: str) -> bool:
        return self.db.execute("SELECT 1 FROM trades WHERE uid=?", (uid,)).fetchone() is not None

    def recent_trades(self, item: str, since_ts: int = 0, limit: int = 200) -> list[Trade]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE item=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (item, since_ts, limit),
        ).fetchall()
        return [Trade(uid=r["uid"], item=r["item"], price=r["price"], qty=r["qty"], ts=r["ts"]) for r in rows]

    def max_trade_price(self, item: str, since_ts: int = 0) -> float | None:
        r = self.db.execute("SELECT MAX(price) FROM trades WHERE item=? AND ts>=?", (item, since_ts)).fetchone()
        return r[0] if r else None

    def prune_trades(self, older_than_ts: int):
        self.db.execute("DELETE FROM trades WHERE ts < ?", (older_than_ts,))
        self.db.commit()

    # --- watchlist ---------------------------------------------------------------
    def watch_add(self, item: str, max_price: float | None = None, mode: str = "cheaper"):
        self.db.execute(
            "INSERT INTO watch(item, max_price, mode, added_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(item) DO UPDATE SET max_price=excluded.max_price, mode=excluded.mode",
            (item, max_price, mode, int(time.time())),
        )
        self.db.commit()

    def watch_remove(self, item: str) -> bool:
        cur = self.db.execute("DELETE FROM watch WHERE item=?", (item,))
        self.db.execute("DELETE FROM state WHERE kind='watch' AND item=?", (item,))
        self.db.commit()
        return cur.rowcount > 0

    def watch_list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM watch ORDER BY added_ts").fetchall()]

    # --- état anti-spam -----------------------------------------------------------
    def get_state(self, kind: str, item: str) -> dict | None:
        r = self.db.execute("SELECT data FROM state WHERE kind=? AND item=?", (kind, item)).fetchone()
        return json.loads(r["data"]) if r else None

    def state_ts(self, kind: str, item: str) -> int | None:
        r = self.db.execute("SELECT ts FROM state WHERE kind=? AND item=?", (kind, item)).fetchone()
        return r["ts"] if r else None

    def set_state(self, kind: str, item: str, data: dict):
        self.db.execute(
            "INSERT INTO state(kind, item, data, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, item) DO UPDATE SET data=excluded.data, ts=excluded.ts",
            (kind, item, json.dumps(data), int(time.time())),
        )
        self.db.commit()

    def state_items(self, kind: str) -> list[str]:
        return [r["item"] for r in self.db.execute("SELECT item FROM state WHERE kind=?", (kind,)).fetchall()]

    def clear_state(self, kind: str, item: str):
        self.db.execute("DELETE FROM state WHERE kind=? AND item=?", (kind, item))
        self.db.commit()

    # --- noms ----------------------------------------------------------------------
    def set_names(self, pairs: Iterable[tuple[str, str]]):
        self.db.executemany("INSERT OR REPLACE INTO names(item, name) VALUES (?,?)", list(pairs))
        self.db.commit()

    def get_name(self, item: str) -> str | None:
        r = self.db.execute("SELECT name FROM names WHERE item=?", (item,)).fetchone()
        return r["name"] if r else None
