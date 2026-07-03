"""store.py — executor persistence (SQLite, WAL). Every number the dashboard
shows comes from here or straight from the bridge; nothing is invented.

Tables:
  trades          one row per executor trade, opened -> closed, with full context
  decisions       every engine decision INCLUDING skips (the "why" feed)
  equity_curve    equity/balance snapshot per engine cycle
  strategy_status backtest-gate result per strategy x symbol (enabled/observing)
  lessons         self-review findings per closed trade + aggregate rules fired
  daily_reports   one row per trading day: P&L, win rate, trade count
  calendar_events high-impact news events (ForexFactory weekly feed)
  engine_state    key/value: halts, cooldowns, heartbeats
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket INTEGER UNIQUE,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  entry_time TEXT, entry_price REAL,
  sl REAL, tp REAL,
  exit_time TEXT, exit_price REAL,
  pnl REAL, swap REAL, commission REAL,
  r_multiple REAL,
  exit_reason TEXT,             -- tp | sl | time_stop | friday_flat | kill | manual
  status TEXT DEFAULT 'open',   -- open | closed
  entry_spread_points REAL, entry_atr REAL,
  timeframe TEXT,               -- decision timeframe of the strategy (H1, M15, ...)
  context TEXT                  -- json: signal reason, regime, indicators at entry
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT, strategy TEXT,
  action TEXT NOT NULL,         -- enter | skip | exit | halt | manage
  side TEXT,
  reason TEXT NOT NULL,
  detail TEXT                   -- json
);
CREATE TABLE IF NOT EXISTS equity_curve (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  equity REAL, balance REAL, margin REAL, open_positions INTEGER
);
CREATE TABLE IF NOT EXISTS strategy_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL, symbol TEXT NOT NULL,
  status TEXT NOT NULL,         -- enabled | observing | cooldown | disabled
  reason TEXT,
  backtest TEXT,                -- json: full gate metrics (IS + OOS)
  UNIQUE(strategy, symbol) ON CONFLICT REPLACE
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  trade_id INTEGER,
  symbol TEXT, strategy TEXT,
  tag TEXT NOT NULL,            -- stopped_then_reversed | against_htf_trend | ...
  lesson TEXT NOT NULL,
  detail TEXT                   -- json
);
CREATE TABLE IF NOT EXISTS daily_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT UNIQUE,
  trades INTEGER, wins INTEGER, losses INTEGER,
  pnl REAL, win_rate REAL,
  equity_open REAL, equity_close REAL,
  best_trade REAL, worst_trade REAL,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS calendar_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_event TEXT NOT NULL,       -- event time UTC iso
  currency TEXT, impact TEXT, title TEXT,
  UNIQUE(ts_event, currency, title) ON CONFLICT IGNORE
);
CREATE TABLE IF NOT EXISTS engine_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);
"""


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, path: Path = config.DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        try:  # migration for DBs created before multi-timeframe strategies
            self.conn.execute("ALTER TABLE trades ADD COLUMN timeframe TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()

    # -- generic ---------------------------------------------------------------
    def insert(self, table: str, row: dict) -> int:
        row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in row.items()}
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        cur = self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        self.conn.commit()
        return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.execute(sql, params)
        self.conn.commit()

    # -- engine_state kv ---------------------------------------------------------
    def set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO engine_state (key, value, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), utcnow()))
        self.conn.commit()

    def get_state(self, key: str, default=None):
        rows = self.query("SELECT value FROM engine_state WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    # -- convenience -------------------------------------------------------------
    def decide(self, action: str, reason: str, symbol: str = "", strategy: str = "",
               side: str = "", detail: dict | None = None) -> None:
        self.insert("decisions", {
            "ts": utcnow(), "symbol": symbol, "strategy": strategy,
            "action": action, "side": side, "reason": reason,
            "detail": detail or {}})

    def open_trades(self) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE status='open'")

    def stats(self, since: str | None = None) -> dict:
        where = "WHERE status='closed'" + (" AND exit_time >= ?" if since else "")
        params = (since,) if since else ()
        rows = self.query(f"SELECT pnl, r_multiple FROM trades {where}", params)
        pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return {
            "trades": len(pnls), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "pnl": round(sum(pnls), 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "best": round(max(pnls), 2) if pnls else None,
            "worst": round(min(pnls), 2) if pnls else None,
        }


if __name__ == "__main__":
    s = Store()
    marker = f"selftest-{int(time.time())}"
    tid = s.insert("trades", {"ticket": int(time.time()), "symbol": "TESTUSD",
                              "strategy": marker, "side": "buy", "volume": 0.01,
                              "entry_time": utcnow(), "entry_price": 1.0,
                              "sl": 0.99, "tp": 1.02, "context": {"marker": marker}})
    s.execute("UPDATE trades SET status='closed', pnl=5.0, exit_time=? WHERE id=?",
              (utcnow(), tid))
    s.decide("skip", "selftest", symbol="TESTUSD", strategy=marker)
    s.set_state("selftest", {"x": 1})
    assert s.get_state("selftest") == {"x": 1}
    st = s.stats()
    assert st["trades"] >= 1
    s.execute("DELETE FROM trades WHERE id=?", (tid,))
    s.execute("DELETE FROM decisions WHERE strategy=?", (marker,))
    print("STORE ROUNDTRIP OK", st)
