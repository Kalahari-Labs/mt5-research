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
from datetime import datetime, timezone
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
  partial_closed INTEGER DEFAULT 0, -- 1 if a partial TP was already taken
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
CREATE TABLE IF NOT EXISTS pending_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  sl REAL, tp REAL,
  reason TEXT,
  detail TEXT,                  -- json: context/regime
  status TEXT DEFAULT 'pending', -- pending | approved | denied | expired | executed
  ts_created TEXT NOT NULL,
  ts_expires TEXT NOT NULL
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
        try:  # migration for DBs created before partial exits
            self.conn.execute("ALTER TABLE trades ADD COLUMN partial_closed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
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
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return {
            "trades": len(pnls), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "pnl": round(sum(pnls), 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "best": round(max(pnls), 2) if pnls else None,
            "worst": round(min(pnls), 2) if pnls else None,
        }

    def streaks(self) -> dict:
        """Win/loss streak stats from closed trade history (oldest-first)."""
        rows = self.query(
            "SELECT pnl FROM trades WHERE status='closed' ORDER BY exit_time ASC")
        pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
        if not pnls:
            return {"current": 0, "max_win": 0, "max_loss": 0, "total": 0}
        max_win = cur_win = 0
        max_loss = cur_loss = 0
        for p in pnls:
            if p > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win = max(max_win, cur_win)
            max_loss = max(max_loss, cur_loss)
        # current streak: positive = consecutive wins, negative = consecutive losses
        cur_w = cur_l = 0
        for p in reversed(pnls):
            if p > 0:
                if cur_l > 0:
                    break
                cur_w += 1
            else:
                if cur_w > 0:
                    break
                cur_l += 1
        return {"current": cur_w if cur_w else -cur_l,
                "max_win": max_win, "max_loss": max_loss, "total": len(pnls)}

    # -- trades (named queries — the schema lives here, not in callers) -----------
    def trade(self, trade_id: int) -> dict | None:
        rows = self.query("SELECT * FROM trades WHERE id=?", (trade_id,))
        return rows[0] if rows else None

    def close_trade(self, trade_id: int, *, exit_time: str, exit_price,
                    pnl: float, swap: float, commission: float,
                    r_multiple, exit_reason: str) -> None:
        """Finalize a trade the broker closed (SL/TP/reconcile). `pnl` is the
        net figure the caller already summed across the closing deals."""
        self.execute(
            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, pnl=?, "
            "swap=?, commission=?, r_multiple=?, exit_reason=? WHERE id=?",
            (exit_time, exit_price, pnl, swap, commission, r_multiple,
             exit_reason, trade_id))

    def update_trade_sl(self, trade_id: int, sl: float) -> None:
        self.execute("UPDATE trades SET sl=? WHERE id=?", (sl, trade_id))

    def mark_partial_closed(self, trade_id: int) -> None:
        self.execute("UPDATE trades SET partial_closed=1 WHERE id=?", (trade_id,))

    def recent_trades(self, limit: int = 60) -> list[dict]:
        return self.query(
            "SELECT ticket, symbol, strategy, side, volume, entry_time, entry_price, "
            "sl, tp, exit_time, exit_price, pnl, r_multiple, exit_reason, status "
            "FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def combos(self) -> list[dict]:
        """Live results grouped by strategy x symbol (closed trades only)."""
        return self.query(
            "SELECT strategy, symbol, COUNT(*) n, SUM(pnl>0) wins, "
            "ROUND(SUM(pnl),2) pnl, ROUND(AVG(r_multiple),3) avg_r "
            "FROM trades WHERE status='closed' AND pnl IS NOT NULL "
            "GROUP BY strategy, symbol ORDER BY pnl DESC")

    def count_losses_since(self, strategy: str, symbol: str, since: str) -> int:
        """Closed losing trades for a combo since `since` (weekly-guard input)."""
        return self.query(
            "SELECT COUNT(*) AS c FROM trades WHERE strategy=? AND symbol=? "
            "AND status='closed' AND exit_time >= ? AND pnl <= 0",
            (strategy, symbol, since))[0]["c"]

    def closed_pnls_between(self, lo: str, hi: str) -> list[float]:
        rows = self.query(
            "SELECT pnl FROM trades WHERE status='closed' AND exit_time BETWEEN ? AND ?",
            (lo, hi))
        return [r["pnl"] for r in rows if r["pnl"] is not None]

    # -- decisions ---------------------------------------------------------------
    def recent_decisions(self, limit: int = 60) -> list[dict]:
        return self.query(
            "SELECT ts, symbol, strategy, action, side, reason FROM decisions "
            "ORDER BY id DESC LIMIT ?", (limit,))

    # -- equity ------------------------------------------------------------------
    def equity_curve(self, limit: int = 500) -> list[dict]:
        """Oldest-first equity points (most recent `limit` snapshots)."""
        rows = self.query(
            "SELECT ts, equity, balance FROM equity_curve ORDER BY id DESC LIMIT ?",
            (limit,))
        return rows[::-1]

    def last_equity(self) -> float | None:
        rows = self.query("SELECT equity FROM equity_curve ORDER BY id DESC LIMIT 1")
        return rows[0]["equity"] if rows else None

    def day_open_equity(self, day: str) -> float | None:
        """Equity at the first snapshot on UTC `day` (YYYY-MM-DD) — the
        reference the daily-loss halt measures against."""
        rows = self.query(
            "SELECT equity FROM equity_curve WHERE ts >= ? ORDER BY id ASC LIMIT 1",
            (day + "T00:00:00Z",))
        return float(rows[0]["equity"]) if rows else None

    def equity_between(self, lo: str, hi: str) -> list[float]:
        rows = self.query(
            "SELECT equity FROM equity_curve WHERE ts BETWEEN ? AND ? ORDER BY id",
            (lo, hi))
        return [r["equity"] for r in rows]

    # -- strategy gate status ----------------------------------------------------
    def strategy_statuses(self) -> list[dict]:
        """Every strategy x symbol gate row (full metrics), stable order."""
        return self.query(
            "SELECT ts, strategy, symbol, status, reason, backtest FROM strategy_status "
            "ORDER BY strategy, symbol")

    def strategy_status(self, strategy: str, symbol: str) -> dict | None:
        rows = self.query(
            "SELECT status, reason FROM strategy_status WHERE strategy=? AND symbol=?",
            (strategy, symbol))
        return rows[0] if rows else None

    def enabled_combos(self) -> set:
        return {(r["strategy"], r["symbol"]) for r in self.query(
            "SELECT strategy, symbol FROM strategy_status WHERE status='enabled'")}

    def prune_strategy_status(self, strategies, symbols) -> None:
        """Drop gate rows for strategies/symbols that no longer exist, so the
        dashboard and risk layer never consult a ghost combo."""
        strategies, symbols = list(strategies), list(symbols)
        if strategies:
            marks = ",".join("?" * len(strategies))
            self.execute(f"DELETE FROM strategy_status WHERE strategy NOT IN ({marks})",
                         tuple(strategies))
        if symbols:
            marks = ",".join("?" * len(symbols))
            self.execute(f"DELETE FROM strategy_status WHERE symbol NOT IN ({marks})",
                         tuple(symbols))

    # -- lessons -----------------------------------------------------------------
    def recent_lessons(self, limit: int = 40) -> list[dict]:
        return self.query(
            "SELECT ts, symbol, strategy, tag, lesson FROM lessons "
            "ORDER BY id DESC LIMIT ?", (limit,))

    def lesson_counts(self, limit: int = 12) -> list[dict]:
        return self.query(
            "SELECT tag, COUNT(*) c FROM lessons GROUP BY tag ORDER BY c DESC LIMIT ?",
            (limit,))

    def lesson_counts_between(self, lo: str, hi: str) -> list[dict]:
        return self.query(
            "SELECT tag, COUNT(*) AS c FROM lessons WHERE ts BETWEEN ? AND ? "
            "GROUP BY tag ORDER BY c DESC", (lo, hi))

    # -- daily reports -----------------------------------------------------------
    def daily_reports(self, limit: int = 14) -> list[dict]:
        return self.query(
            "SELECT * FROM daily_reports ORDER BY date DESC LIMIT ?", (limit,))

    def delete_daily_report(self, date: str) -> None:
        self.execute("DELETE FROM daily_reports WHERE date=?", (date,))

    # -- calendar events ---------------------------------------------------------
    def upcoming_events(self, limit: int = 20, now: str | None = None) -> list[dict]:
        return self.query(
            "SELECT ts_event, currency, title FROM calendar_events "
            "WHERE ts_event >= ? ORDER BY ts_event LIMIT ?", (now or utcnow(), limit))

    def events_in_window(self, currencies, lo: str, hi: str) -> list[dict]:
        currencies = list(currencies)
        if not currencies:
            return []
        marks = ",".join("?" * len(currencies))
        return self.query(
            f"SELECT currency, title, ts_event FROM calendar_events "
            f"WHERE impact='high' AND currency IN ({marks}) AND ts_event BETWEEN ? AND ?",
            tuple(currencies) + (lo, hi))

    def insert_calendar_event(self, ts_event: str, currency: str, title: str) -> bool:
        """Insert one high-impact event; returns True if it was new (the table's
        UNIQUE constraint ignores duplicates)."""
        before = self.conn.total_changes
        self.insert("calendar_events", {
            "ts_event": ts_event, "currency": currency,
            "impact": "high", "title": title})
        return self.conn.total_changes - before > 0

    # -- symbol monitor + engine internals ---------------------------------------
    def symbol_views(self) -> list[dict]:
        """What the engine saw on each symbol at its last closed bar (written by
        engine.symbol_view) — the dashboard's 'what is the bot thinking' feed."""
        return [json.loads(r["value"]) for r in self.query(
            "SELECT value FROM engine_state WHERE key LIKE 'symbol_view:%' ORDER BY key")]

    def cooldowns(self, now: datetime | None = None) -> list[dict]:
        """Currently-active combo cooldowns set by review.py (consecutive-loss
        protection). Expired entries are filtered out."""
        now = now or datetime.now(timezone.utc)
        out = []
        for r in self.query(
                "SELECT key, value FROM engine_state WHERE key LIKE 'cooldown:%'"):
            try:
                until = json.loads(r["value"])
                if datetime.fromisoformat(until) <= now:
                    continue  # expired — no longer blocking
            except (ValueError, TypeError):
                until = r["value"]
            parts = r["key"].split(":", 2)
            if len(parts) == 3:
                out.append({"strategy": parts[1], "symbol": parts[2], "until": until})
        return sorted(out, key=lambda c: c["until"])

    # -- pending trades (HITL) ---------------------------------------------------
    def pending_trades(self, status: str = "pending") -> list[dict]:
        return self.query(
            "SELECT * FROM pending_trades WHERE status=? ORDER BY id DESC", (status,))

    def set_pending_status(self, pending_id: int, status: str,
                           reason: str | None = None) -> None:
        if reason is None:
            self.execute("UPDATE pending_trades SET status=? WHERE id=?",
                         (status, pending_id))
        else:
            self.execute("UPDATE pending_trades SET status=?, reason=? WHERE id=?",
                         (status, reason, pending_id))


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
