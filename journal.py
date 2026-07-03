"""journal.py — append-only research journal.

Logs every signal, risk decision, rejection reason, and (demo) fill with a UTC
timestamp and a short reasoning string. SQLite by default; Supabase is used iff
SUPABASE_URL + SUPABASE_KEY are set AND the supabase package is importable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import JOURNAL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteJournal:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                   id      TEXT PRIMARY KEY,
                   ts      TEXT NOT NULL,
                   symbol  TEXT,
                   event   TEXT NOT NULL,
                   signal  TEXT,
                   decision TEXT,
                   reason  TEXT,
                   volume  REAL,
                   price   REAL,
                   extra   TEXT)""")
        self._conn.commit()

    def record(self, event, symbol=None, signal=None, decision=None,
               reason=None, volume=None, price=None, **extra) -> str:
        rid = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO journal VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, _now(), symbol, event, signal, decision, reason,
             volume, price, json.dumps(extra) if extra else None))
        self._conn.commit()
        return rid

    def recent(self, limit=20):
        cur = self._conn.execute(
            "SELECT ts,event,symbol,signal,decision,reason,volume,price "
            "FROM journal ORDER BY ts DESC LIMIT ?", (limit,))
        return cur.fetchall()

    def close(self):
        self._conn.close()


class SupabaseJournal:
    TABLE = "trade_journal"

    def __init__(self, url, key):
        from supabase import create_client  # imported only when configured
        self._client = create_client(url, key)

    def record(self, event, symbol=None, signal=None, decision=None,
               reason=None, volume=None, price=None, **extra) -> str:
        rid = str(uuid.uuid4())
        self._client.table(self.TABLE).insert({
            "id": rid, "ts": _now(), "symbol": symbol, "event": event,
            "signal": signal, "decision": decision, "reason": reason,
            "volume": volume, "price": price,
            "extra": json.dumps(extra) if extra else None}).execute()
        return rid

    def recent(self, limit=20):
        res = (self._client.table(self.TABLE)
               .select("*").order("ts", desc=True).limit(limit).execute())
        return res.data

    def close(self):
        pass


def get_journal(config=JOURNAL):
    """Return the active journal backend (Supabase if configured, else SQLite)."""
    if config.use_supabase:
        try:
            return SupabaseJournal(config.supabase_url, config.supabase_key)
        except Exception as e:
            print(f"[journal] Supabase unavailable ({e}); falling back to SQLite.")
    return SQLiteJournal(config.sqlite_path)
