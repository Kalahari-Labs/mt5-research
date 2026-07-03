"""registry.py — results registry over the existing SQLite journal DB.

Logs every backtest and walk-forward run (strategy, params, costs, data range,
in-sample + out-of-sample metrics) with a UTC timestamp and a content hash, so
identical re-runs are detectable and a dead idea is never re-tested blind.

Also tracks the MULTIPLE-TESTING count: how many DISTINCT strategy+param configs
have ever been evaluated against out-of-sample data (deduped across runs). The
more configs tried, the more likely an OOS "winner" is luck.

CLI:
  python3 registry.py list [N]    # recent runs
  python3 registry.py count       # multiple-testing count + warning
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import JOURNAL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(payload) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ResultsRegistry:
    """Two tables alongside the journal: `results` (one row per run) and
    `oos_configs` (one row per DISTINCT config ever OOS-evaluated)."""

    def __init__(self, path=None):
        self.path = str(path or JOURNAL.sqlite_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.path)
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, run_type TEXT, strategy TEXT, params TEXT, cost TEXT,
                symbol TEXT, timeframe INTEGER, data_start TEXT, data_end TEXT,
                n_bars INTEGER, metrics_is TEXT, metrics_oos TEXT,
                n_configs_oos INTEGER, run_hash TEXT, notes TEXT);
            CREATE TABLE IF NOT EXISTS oos_configs (
                strategy TEXT, params_hash TEXT, params TEXT, first_seen TEXT,
                PRIMARY KEY (strategy, params_hash));
            """)
        self.c.commit()

    def _content_hash(self, run_type, strategy, params, cost, data_meta) -> str:
        return _hash({"t": run_type, "s": strategy, "p": params, "c": cost,
                      "sym": data_meta.get("symbol"), "tf": data_meta.get("timeframe"),
                      "ds": data_meta.get("data_start"), "de": data_meta.get("data_end")})

    def log_run(self, run_type, strategy, params, cost, data_meta,
                metrics_is=None, metrics_oos=None, oos_configs=None, notes=""):
        """Log one run. Returns (run_hash, is_duplicate). `oos_configs` is a list of
        param dicts that were evaluated against OOS data (walk-forward only)."""
        run_hash = self._content_hash(run_type, strategy, params, cost, data_meta)
        is_dup = self.c.execute(
            "SELECT COUNT(*) FROM results WHERE run_hash=?", (run_hash,)).fetchone()[0] > 0
        self.c.execute(
            "INSERT INTO results (ts,run_type,strategy,params,cost,symbol,timeframe,"
            "data_start,data_end,n_bars,metrics_is,metrics_oos,n_configs_oos,run_hash,notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), run_type, strategy, json.dumps(params), json.dumps(cost),
             data_meta.get("symbol"), data_meta.get("timeframe"),
             data_meta.get("data_start"), data_meta.get("data_end"),
             data_meta.get("n_bars"),
             json.dumps(metrics_is) if metrics_is is not None else None,
             json.dumps(metrics_oos) if metrics_oos is not None else None,
             len({_hash(c) for c in oos_configs}) if oos_configs else 0,
             run_hash, notes))
        if oos_configs:
            for cfg in oos_configs:
                self.c.execute(
                    "INSERT OR IGNORE INTO oos_configs VALUES (?,?,?,?)",
                    (strategy, _hash(cfg), json.dumps(cfg), _now()))
        self.c.commit()
        return run_hash, is_dup

    def list_runs(self, limit=20):
        return self.c.execute(
            "SELECT ts,run_type,strategy,params,metrics_is,metrics_oos,"
            "n_configs_oos,run_hash FROM results ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()

    def multiple_testing_count(self):
        n = self.c.execute("SELECT COUNT(*) FROM oos_configs").fetchone()[0]
        rows = self.c.execute(
            "SELECT strategy,params,first_seen FROM oos_configs "
            "ORDER BY strategy,first_seen").fetchall()
        return n, rows

    def close(self):
        self.c.close()


def multiple_testing_warning(n: int) -> str:
    exp_false = n * 0.05
    return (
        f"MULTIPLE-TESTING COUNT: {n} distinct strategy+param config(s) have been "
        f"evaluated against out-of-sample data (deduped across all logged runs).\n"
        f"  ⚠ The more configs you test, the more likely the best OOS result is LUCK.\n"
        f"  Rough intuition: at a 5% false-positive rate, ~{exp_false:.1f} of {n} configs "
        f"would look like 'winners' by pure chance.\n"
        f"  A survivor needs a MUCH higher bar — or fresh, never-tested data — before it "
        f"means anything. This is a counter + warning, not a significance test.")


def _print_runs(rows):
    if not rows:
        print("  (no runs logged yet)")
        return
    print(f"  {'when (UTC)':<20} {'type':<11} {'strategy':<14} {'params':<22} {'IS/OOS ret%':<16} hash")
    print("  " + "-" * 92)
    for ts, rtype, strat, params, mis, moos, ncfg, rh in rows:
        def ret(j):
            if not j:
                return "—"
            d = json.loads(j)
            v = d.get("return_pct", d.get("total_return_pct"))
            return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"
        isret, oosret = ret(mis), ret(moos)
        p = json.loads(params) if params else {}
        pstr = " ".join(f"{k}={v}" for k, v in p.items())[:21] if isinstance(p, dict) else str(params)[:21]
        print(f"  {ts[:19]:<20} {rtype:<11} {strat:<14} {pstr:<22} "
              f"{isret + '/' + oosret:<16} {rh}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    reg = ResultsRegistry()
    if cmd == "list":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        print(f"\n  RESULTS REGISTRY — last {n} runs ({reg.path})")
        _print_runs(reg.list_runs(n))
        print()
    elif cmd == "count":
        n, rows = reg.multiple_testing_count()
        print("\n  " + multiple_testing_warning(n).replace("\n", "\n  "))
        if rows:
            print("\n  distinct OOS-evaluated configs:")
            for strat, params, first in rows:
                print(f"    {strat:<14} {params:<28} first seen {first[:19]}")
        print()
    else:
        print(f"unknown command '{cmd}'. Use: list [N] | count")
    reg.close()
