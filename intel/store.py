"""store.py — persistence layer with two interchangeable backends.

Supabase (PostgREST over urllib, no SDK needed) when SUPABASE_URL +
SUPABASE_SERVICE_KEY are set in .env; otherwise local SQLite at
intel/data/intel.sqlite with the same table shapes as migrations/0001_init.sql.

Read-only trading boundary: this module stores observations. Nothing in it can
reach a broker.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "intel.sqlite"
MIGRATIONS_DIR = BASE_DIR / "migrations"

TABLES = (
    "price_snapshots", "technical_signals", "news_events",
    "sentiment_scores", "market_state", "system_health",
)


def _load_env() -> None:
    """Tiny .env loader (python-dotenv is not installable here)."""
    for env_path in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()


def _pg_to_sqlite(sql: str) -> str:
    """Translate the Postgres migration DDL to SQLite so one schema file rules both."""
    sql = re.sub(r"bigint generated always as identity primary key",
                 "INTEGER PRIMARY KEY AUTOINCREMENT", sql)
    sql = re.sub(r"timestamptz", "TEXT", sql)
    sql = re.sub(r"double precision", "REAL", sql)
    sql = re.sub(r"jsonb", "TEXT", sql)
    sql = re.sub(r"default now\(\)", "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))", sql)
    return sql


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SqliteBackend:
    name = "sqlite"

    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    def migrate(self) -> None:
        for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
            self.conn.executescript(_pg_to_sqlite(mig.read_text()))
        self.conn.commit()

    def insert(self, table: str, row: dict) -> None:
        row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in row.items()}
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        try:
            self.conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        except sqlite3.IntegrityError:
            pass  # unique-constraint dupes (same bar/headline seen again) are expected
        self.conn.commit()

    def insert_many(self, table: str, rows: list[dict]) -> int:
        n = 0
        for row in rows:
            before = self.conn.total_changes
            self.insert(table, row)
            n += self.conn.total_changes - before
        return n

    def latest(self, table: str, symbol: str | None = None, limit: int = 50) -> list[dict]:
        where, params = "", []
        if symbol:
            where, params = "WHERE symbol = ?", [symbol]
        cur = self.conn.execute(
            f"SELECT * FROM {table} {where} ORDER BY id DESC LIMIT ?", params + [limit])
        return [dict(r) for r in cur.fetchall()]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


class SupabaseBackend:
    """PostgREST via urllib. Activated by SUPABASE_URL + SUPABASE_SERVICE_KEY."""
    name = "supabase"

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key

    def _req(self, method: str, path: str, body=None, prefer: str | None = None):
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}/rest/v1/{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def insert(self, table: str, row: dict) -> None:
        try:
            self._req("POST", table, [row], prefer="resolution=ignore-duplicates")
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise

    def insert_many(self, table: str, rows: list[dict]) -> int:
        if rows:
            self._req("POST", table, rows, prefer="resolution=ignore-duplicates")
        return len(rows)

    def latest(self, table: str, symbol: str | None = None, limit: int = 50) -> list[dict]:
        q = f"{table}?order=id.desc&limit={limit}"
        if symbol:
            q += f"&symbol=eq.{symbol}"
        return self._req("GET", q) or []


def get_store():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if url and key:
        return SupabaseBackend(url, key)
    return SqliteBackend()


def heartbeat(store, source: str, status: str = "ok", cycle: int | None = None,
              latency_ms: float | None = None, message: str = "") -> None:
    store.insert("system_health", {
        "ts": utcnow(), "symbol": "SYSTEM", "source": source, "status": status,
        "cycle": cycle, "latency_ms": latency_ms, "message": message,
    })


if __name__ == "__main__":
    # Round-trip self-test (Phase 1 checklist item).
    s = get_store()
    marker = f"roundtrip-{int(time.time())}"
    s.insert("price_snapshots", {
        "ts": utcnow(), "symbol": "TESTUSD", "source": marker, "timeframe": "H1",
        "bar_time": utcnow(), "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume": 42.0,
    })
    rows = s.latest("price_snapshots", symbol="TESTUSD", limit=1)
    row = rows[0]
    assert row["source"] == marker and row["close"] == 1.5 and row["volume"] == 42.0, row
    print(f"ROUNDTRIP OK backend={s.name} row_id={row['id']} source={row['source']}")
