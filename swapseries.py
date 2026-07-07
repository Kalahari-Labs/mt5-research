"""swapseries.py — PHASE 7: the historical swap series (research plane ONLY).

WHY (fixed a priori): every carry number in Phases 4b/5/6 applies TODAY's broker
swap quote across the whole backtest history. PHASE6.md lists this constant-quote
approximation as its first documented limitation and names "a historical swap
series" as the first thing that would upgrade the evidence. No such series exists
and it cannot be backfilled — brokers do not publish their past swap points. The
only honest fix is to START RECORDING, so a future phase can backtest against
quotes that were actually live at the time.

WHAT THIS IS: an instrument, not a strategy. It captures the broker's current
swap quote per symbol — from the running MT5 bridge (`/symbol?name=X`), or from
the frozen data/<SYM>_swap.json captures when the bridge is down — and appends it
to data/swap_history.csv: one row per (symbol, UTC capture date), keep-first,
append-only.

THE CAUSALITY CONTRACT (pre-registered in the Phase 7 brief, oracle-tested in
tests/test_phase7.py): a quote captured on UTC day D may first influence bars
dated STRICTLY AFTER D. `per_bar_swap` implements exactly that strict
inequality, so even an intraday bar can never see a quote captured later the
same day. Bars before the first capture fall back to the constant spec —
bit-for-bit the Phase 4b/5/6 behaviour — and the fallback count is returned,
never hidden.

Recorder vs loader: the RECORDER stores whatever the broker quotes (a swap_mode
change is itself information); the LOADER refuses to convert any mode other than
1 (points) — the same deliberate refusal as config.load_swap_spec.

stdlib + numpy only. Do NOT touch intel/executor/.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from config import DATA_DIR, swap_spec_path

HISTORY_NAME = "swap_history.csv"
FIELDS = ("captured_utc", "date_utc", "symbol", "swap_long", "swap_short",
          "swap_mode", "swap_rollover3days", "point", "digits",
          "trade_contract_size", "ref_price", "source")
REQUIRED = ("swap_long", "swap_short", "swap_mode", "point")
DEFAULT_BRIDGE = os.environ.get("MI_BRIDGE_URL", "http://127.0.0.1:8787")


def history_path(data_dir=None) -> Path:
    return Path(data_dir or DATA_DIR) / HISTORY_NAME


# ───────────────────────────── capture ──────────────────────────────────────
def row_from_spec(raw: dict, symbol: str, source: str,
                  captured_utc: str | None = None, ref_price=None) -> dict:
    """One history row from a broker symbol spec (bridge payload or *_swap.json
    contents — both carry the same MT5 symbol_info fields)."""
    missing = [k for k in REQUIRED if raw.get(k) is None]
    if missing:
        raise ValueError(f"{symbol}: spec missing {missing} — refusing to record "
                         f"a partial quote")
    ts = captured_utc or datetime.now(timezone.utc).isoformat()
    return {
        "captured_utc": ts,
        "date_utc": ts[:10],
        "symbol": symbol,
        "swap_long": raw["swap_long"],
        "swap_short": raw["swap_short"],
        "swap_mode": raw["swap_mode"],
        "swap_rollover3days": raw.get("swap_rollover3days", ""),
        "point": raw["point"],
        "digits": raw.get("digits", ""),
        "trade_contract_size": raw.get("trade_contract_size", ""),
        "ref_price": "" if ref_price is None else ref_price,
        "source": source,
    }


def capture_bridge(symbols, base_url: str | None = None,
                   timeout: float = 10.0) -> tuple[list[dict], list[str]]:
    """Fresh quotes from the running MT5 bridge. Returns (rows, errors) — a
    down bridge or an unknown symbol is an error string, never an exception."""
    base = (base_url or DEFAULT_BRIDGE).rstrip("/")
    rows, errors = [], []
    for sym in symbols:
        try:
            with urllib.request.urlopen(f"{base}/symbol?name={sym}",
                                        timeout=timeout) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                errors.append(f"{sym}: bridge said {d['error']!r}")
                continue
            rows.append(row_from_spec(d, sym, source="bridge",
                                      ref_price=d.get("bid")))
        except Exception as e:  # noqa: BLE001 — a capture must never crash the caller
            errors.append(f"{sym}: {e.__class__.__name__}: {e}")
    return rows, errors


def capture_files(symbols=None, data_dir=None) -> list[dict]:
    """Rows from the frozen data/<SYM>_swap.json captures, dated at their OWN
    captured_utc (so seeding history from the files lands the quote at its true
    historical date, not today). symbols=None → every *_swap.json present."""
    base = Path(data_dir or DATA_DIR)
    if symbols is None:
        symbols = sorted(p.name[:-len("_swap.json")]
                         for p in base.glob("*_swap.json"))
    rows = []
    for sym in symbols:
        path = base / f"{sym}_swap.json" if data_dir else swap_spec_path(sym)
        raw = json.loads(path.read_text())
        rows.append(row_from_spec(raw, sym, source="file",
                                  captured_utc=raw.get("captured_utc")))
    return rows


# ───────────────────────────── record ───────────────────────────────────────
def record(rows, data_dir=None) -> tuple[int, int]:
    """Append rows to the history CSV. Idempotent: one row per
    (symbol, date_utc), KEEP-FIRST — a day's first capture is THE capture for
    that day; re-runs are no-ops. Returns (n_added, n_skipped)."""
    path = history_path(data_dir)
    seen = set()
    if path.exists():
        with path.open() as f:
            for r in csv.DictReader(f):
                seen.add((r["symbol"], r["date_utc"]))
    added = skipped = 0
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for row in rows:
            key = (row["symbol"], row["date_utc"])
            if key in seen:
                skipped += 1
                continue
            w.writerow({k: row.get(k, "") for k in FIELDS})
            seen.add(key)
            added += 1
    return added, skipped


# ───────────────────────────── load ──────────────────────────────────────────
def load_series(symbol: str, data_dir=None) -> dict | None:
    """The recorded series for one symbol, in ENGINE terms (per-night PRICE
    units, broker sign kept), sorted by capture date. None if no rows yet.
    Refuses swap_mode != 1 exactly like config.load_swap_spec — extend
    deliberately, don't guess."""
    path = history_path(data_dir)
    if not path.exists():
        return None
    by_date: dict[str, dict] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            if r["symbol"] != symbol:
                continue
            if int(float(r["swap_mode"])) != 1:
                raise ValueError(
                    f"{symbol} @ {r['date_utc']}: swap_mode={r['swap_mode']} not "
                    f"implemented (only 1 = points). Extend load_series "
                    f"deliberately, not by guessing.")
            by_date.setdefault(r["date_utc"], r)          # keep-first
    if not by_date:
        return None
    dates = sorted(by_date)
    rows = [by_date[d] for d in dates]
    point = np.array([float(r["point"]) for r in rows])
    return {
        "symbol": symbol,
        "dates": np.array(dates, dtype="datetime64[D]"),
        "swap_long_per_night": np.array([float(r["swap_long"]) for r in rows]) * point,
        "swap_short_per_night": np.array([float(r["swap_short"]) for r in rows]) * point,
        "captured_utc": [r["captured_utc"] for r in rows],
        "sources": [r["source"] for r in rows],
        "n": len(rows),
    }


def per_bar_swap(bar_times, capture_dates, values, fallback) -> tuple[np.ndarray, int]:
    """Per-bar swap value under the causality contract: bar t gets the latest
    capture dated STRICTLY BEFORE the bar's UTC day; bars before the first
    capture get `fallback` (the constant spec — Phase 4b/5/6 behaviour).
    Returns (per_bar_values, n_fallback_bars). Feed the result straight into
    strategies.carry_momentum.carry_bps_per_year, which already accepts
    per-bar arrays."""
    bar_days = np.asarray(bar_times).astype("datetime64[D]")
    cap = np.asarray(capture_dates).astype("datetime64[D]")
    if cap.size and np.any(cap[1:] < cap[:-1]):
        raise ValueError("capture_dates must be sorted ascending")
    vals = np.asarray(values, dtype=float)
    if cap.shape != vals.shape:
        raise ValueError("capture_dates and values must be the same length")
    idx = np.searchsorted(cap, bar_days, side="left") - 1   # latest capture < bar day
    out = np.where(idx >= 0, vals[np.maximum(idx, 0)], float(fallback))
    return out, int((idx < 0).sum())


# ───────────────────────────── CLI ───────────────────────────────────────────
def cmd_record() -> None:
    file_rows = capture_files()
    syms = sorted({r["symbol"] for r in file_rows})
    bridge_rows, errors = capture_bridge(syms)
    for e in errors:
        print(f"  bridge: {e}")
    a1, s1 = record(file_rows)
    a2, s2 = record(bridge_rows)
    print(f"swap_history: +{a1 + a2} rows ({s1 + s2} already recorded) -> "
          f"{history_path()}")
    if not bridge_rows and errors:
        print("  NOTE: bridge unreachable — only the frozen file captures were "
              "recorded. Start the executor (or bridge) and re-run for a fresh "
              "quote; one capture per symbol per UTC day is kept.")


def cmd_show(symbol: str) -> None:
    s = load_series(symbol)
    if s is None:
        print(f"no recorded swap history for {symbol} ({history_path()})")
        return
    print(f"{symbol}: {s['n']} capture(s), {s['dates'][0]} -> {s['dates'][-1]}")
    for i in range(s["n"]):
        print(f"  {s['dates'][i]}  long {s['swap_long_per_night'][i]:+.8f}  "
              f"short {s['swap_short_per_night'][i]:+.8f}  px/night  "
              f"[{s['sources'][i]}]")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "record"
    if cmd == "record":
        cmd_record()
    elif cmd == "show" and len(sys.argv) > 2:
        cmd_show(sys.argv[2].upper())
    else:
        raise SystemExit("usage: python3 swapseries.py [record | show SYMBOL]")
