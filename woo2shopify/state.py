"""SQLite-backed migration state: id mappings, checkpoints, failure log.

Everything the migrator writes goes through here first, so a run can be
stopped at any point and resumed without creating duplicates in Shopify.
"""

from __future__ import annotations

import csv
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import STATE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    woo_id       INTEGER PRIMARY KEY,
    email        TEXT,
    source       TEXT,           -- woo_customer | guest_order | wp_user
    shopify_gid  TEXT,
    shopify_id   TEXT,
    status       TEXT NOT NULL,  -- done | skipped | failed
    error        TEXT,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);

CREATE TABLE IF NOT EXISTS email_map (
    email        TEXT PRIMARY KEY,
    shopify_gid  TEXT,
    shopify_id   TEXT,
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS orders (
    woo_id       INTEGER PRIMARY KEY,
    woo_number   TEXT,
    email        TEXT,
    total        TEXT,
    created_at   TEXT,
    shopify_gid  TEXT,
    shopify_id   TEXT,
    shopify_name TEXT,
    status       TEXT NOT NULL,
    error        TEXT,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS variants (
    sku            TEXT PRIMARY KEY,
    variant_gid    TEXT,
    variant_id     TEXT,
    product_gid    TEXT,
    title          TEXT,
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS variant_titles (
    title_key      TEXT PRIMARY KEY,
    variant_gid    TEXT,
    sku            TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------- customers
    def record_customer(
        self,
        woo_id: int,
        email: str,
        source: str,
        status: str,
        shopify_gid: str = "",
        shopify_id: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO customers(woo_id, email, source, shopify_gid, shopify_id, status, error, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(woo_id) DO UPDATE SET "
                "email=excluded.email, source=excluded.source, shopify_gid=excluded.shopify_gid, "
                "shopify_id=excluded.shopify_id, status=excluded.status, error=excluded.error, "
                "updated_at=excluded.updated_at",
                (woo_id, (email or "").lower(), source, shopify_gid, shopify_id, status, error, time.time()),
            )
            if email and shopify_gid:
                self._conn.execute(
                    "INSERT INTO email_map(email, shopify_gid, shopify_id, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(email) DO UPDATE SET shopify_gid=excluded.shopify_gid, "
                    "shopify_id=excluded.shopify_id, updated_at=excluded.updated_at",
                    (email.lower(), shopify_gid, shopify_id, time.time()),
                )
            self._conn.commit()

    def customer_status(self, woo_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM customers WHERE woo_id=?", (woo_id,)).fetchone()

    def customer_gid_by_email(self, email: str) -> Optional[str]:
        if not email:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT shopify_gid FROM email_map WHERE email=?", (email.lower(),)
            ).fetchone()
        return row["shopify_gid"] if row else None

    def remember_email(self, email: str, shopify_gid: str, shopify_id: str = "") -> None:
        if not (email and shopify_gid):
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO email_map(email, shopify_gid, shopify_id, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET shopify_gid=excluded.shopify_gid, "
                "shopify_id=excluded.shopify_id, updated_at=excluded.updated_at",
                (email.lower(), shopify_gid, shopify_id, time.time()),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- orders
    def record_order(
        self,
        woo_id: int,
        status: str,
        woo_number: str = "",
        email: str = "",
        total: str = "",
        created_at: str = "",
        shopify_gid: str = "",
        shopify_id: str = "",
        shopify_name: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders(woo_id, woo_number, email, total, created_at, shopify_gid, shopify_id, "
                "shopify_name, status, error, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(woo_id) DO UPDATE SET woo_number=excluded.woo_number, email=excluded.email, "
                "total=excluded.total, created_at=excluded.created_at, shopify_gid=excluded.shopify_gid, "
                "shopify_id=excluded.shopify_id, shopify_name=excluded.shopify_name, status=excluded.status, "
                "error=excluded.error, updated_at=excluded.updated_at",
                (woo_id, woo_number, (email or "").lower(), total, created_at, shopify_gid,
                 shopify_id, shopify_name, status, error, time.time()),
            )
            self._conn.commit()

    def order_status(self, woo_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM orders WHERE woo_id=?", (woo_id,)).fetchone()

    # -------------------------------------------------------------- variants
    def save_variants(self, rows: Iterable[Dict[str, Any]]) -> int:
        now = time.time()
        payload = [
            (r["sku"], r["variant_gid"], r.get("variant_id", ""), r.get("product_gid", ""), r.get("title", ""), now)
            for r in rows
            if r.get("sku")
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO variants(sku, variant_gid, variant_id, product_gid, title, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET variant_gid=excluded.variant_gid, "
                "variant_id=excluded.variant_id, product_gid=excluded.product_gid, title=excluded.title, "
                "updated_at=excluded.updated_at",
                payload,
            )
            self._conn.commit()
        return len(payload)

    def save_variant_titles(self, rows: Iterable[Dict[str, Any]]) -> int:
        payload = [
            (title_key(r.get("title", "")), r["variant_gid"], r.get("sku", ""))
            for r in rows
            if r.get("title") and r.get("variant_gid")
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO variant_titles(title_key, variant_gid, sku) VALUES(?,?,?) "
                "ON CONFLICT(title_key) DO UPDATE SET variant_gid=excluded.variant_gid, sku=excluded.sku",
                payload,
            )
            self._conn.commit()
        return len(payload)

    def clear_variants(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM variants")
            self._conn.execute("DELETE FROM variant_titles")
            self._conn.commit()

    def variant_by_sku(self, sku: str) -> Optional[sqlite3.Row]:
        if not sku:
            return None
        with self._lock:
            return self._conn.execute("SELECT * FROM variants WHERE sku=?", (sku.strip(),)).fetchone()

    def variant_by_title(self, title: str) -> Optional[sqlite3.Row]:
        key = title_key(title)
        if not key:
            return None
        with self._lock:
            return self._conn.execute("SELECT * FROM variant_titles WHERE title_key=?", (key,)).fetchone()

    # --------------------------------------------------------------- reports
    def counts(self) -> Dict[str, int]:
        with self._lock:
            out: Dict[str, int] = {}
            for table in ("customers", "orders"):
                for status in ("done", "failed", "skipped"):
                    row = self._conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE status=?", (status,)
                    ).fetchone()
                    out[f"{table}_{status}"] = row["c"]
            out["variants"] = self._conn.execute("SELECT COUNT(*) AS c FROM variants").fetchone()["c"]
        return out

    def failures(self, table: str) -> List[sqlite3.Row]:
        if table not in ("customers", "orders"):
            raise ValueError(table)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM {table} WHERE status='failed' ORDER BY woo_id"
            ).fetchall()

    def dump_csv(self, table: str, path: Path) -> Path:
        if table not in ("customers", "orders", "variants"):
            raise ValueError(table)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if rows:
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow(list(row))
        return path

    def reset(self, tables: Iterable[str]) -> None:
        with self._lock:
            for table in tables:
                if table in ("customers", "orders", "variants", "variant_titles", "email_map", "meta"):
                    self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()


def title_key(title: str) -> str:
    return " ".join((title or "").lower().split())
