"""forwardtest.py — PHASE 8: forward-test reconciliation (research plane, READ-ONLY).

WHY (fixed a priori): GUIDE.md §6 step 4 requires weekly reconciliation of live
fills against model assumptions — spread paid, slippage, swap charged vs
CostModel — before any forward test can graduate, and §6 step 5 kills the test
if realized costs exceed modelled costs by >50%. The §6 protocol itself stays
LOCKED (no strategy has passed the gate); this module is the INSTRUMENT that
protocol will need, proven now on the demo journal that is already accruing.

WHAT IT DOES: opens the executor's journal STRICTLY READ-ONLY (sqlite
`mode=ro` URI — this module must never be able to write to the execution
plane), takes every closed trade, and reconciles:

  spread — realized `entry_spread_points` (converted to pips via the broker's
           captured point size) vs the research CostModel's `spread_pips`.
  swap   — realized broker charge vs the model's expectation:
           per-night directional quote × units held × rollover nights, using
           BIT-FOR-BIT the backtest's night-counting convention (backtest.py
           `nights_mult`): one night per UTC midnight crossed, keyed on the
           weekday of the day being LEFT — Mon–Fri 1×, the broker's triple
           day 3×, Sat/Sun 0×. Any broker rollover-hour mismatch therefore
           shows up as reconciliation error instead of being silently
           calibrated away.

WHAT IT CANNOT DO (schema gaps, reported, never guessed): slippage — the
journal records the FILL price only; there is no requested-price column, so
slippage is unmeasurable until the executor journals it.

stdlib + numpy only. Reads intel/executor/data/executor.sqlite; writes NOTHING.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from config import BASE_DIR, cost_for, load_swap_spec

DB_DEFAULT = Path(BASE_DIR) / "intel" / "executor" / "data" / "executor.sqlite"
KILL_RATIO = 1.5          # GUIDE.md §6 step 5: realized costs >50% over modelled


# ───────────────────────────── inputs ────────────────────────────────────────
def default_spec_fn(symbol: str):
    """The broker spec captured in data/<SYM>_swap.json, in engine terms
    (config.load_swap_spec REUSED). None when no spec was ever captured."""
    try:
        return load_swap_spec(symbol)
    except (FileNotFoundError, ValueError):
        return None


def default_cost_fn(symbol: str):
    try:
        return cost_for(symbol)
    except Exception:  # noqa: BLE001 — a symbol without a cost model is a flag, not a crash
        return None


def load_trades(db_path=None) -> list[dict]:
    """Closed trades from the executor journal, READ-ONLY. A missing journal is
    a clean refusal, not an empty result — absence of evidence must be loud."""
    path = Path(db_path or DB_DEFAULT)
    if not path.exists():
        raise FileNotFoundError(f"no executor journal at {path} — nothing to "
                                f"reconcile (is the executor deployed here?)")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = ("ticket", "symbol", "strategy", "side", "volume", "entry_time",
                "entry_price", "exit_time", "exit_price", "pnl", "swap",
                "commission", "exit_reason", "entry_spread_points")
        rows = c.execute(
            f"SELECT {','.join(cols)} FROM trades WHERE status='closed' "
            f"ORDER BY entry_time").fetchall()
    finally:
        c.close()
    return [dict(zip(cols, r)) for r in rows]


# ───────────────────────────── conventions ───────────────────────────────────
def nights_between(entry_iso: str, exit_iso: str, triple_weekday: int) -> float:
    """Rollover-night multiplier between two UTC timestamps — EXACTLY the
    backtest's convention (backtest.py nights_mult): for each UTC day d in
    [date(entry), date(exit)), weekday(d)<5 charges 1 night (3 on the triple
    day), Sat/Sun charge 0 (the triple day carries the weekend, T+2)."""
    d = datetime.fromisoformat(entry_iso.replace("Z", "+00:00")).date()
    d1 = datetime.fromisoformat(exit_iso.replace("Z", "+00:00")).date()
    n = 0.0
    while d < d1:
        wd = d.weekday()
        if wd < 5:
            n += 3.0 if wd == triple_weekday else 1.0
        d += timedelta(days=1)
    return n


def quote_to_account_fx(symbol: str, exit_price: float):
    """Quote-currency → account-currency (USD) factor at exit. None = this
    module does not know the conversion — the trade is flagged 'unconverted'
    and excluded from currency totals, never guessed."""
    if symbol.endswith("USD") or symbol in ("GOLD", "SILVER", "XAUUSD", "XAGUSD"):
        return 1.0
    if symbol.startswith("USD") and exit_price:
        return 1.0 / float(exit_price)
    return None


def expected_swap_ccy(trade: dict, spec: dict):
    """Model-expected financing for one closed trade, account currency, broker
    sign (negative = cost). Returns (value | None, note)."""
    per_night = (spec["swap_long_per_night"] if trade["side"] == "buy"
                 else spec["swap_short_per_night"])
    nights = nights_between(trade["entry_time"], trade["exit_time"],
                            spec["swap_triple_weekday"])
    units = float(trade["volume"]) * float(spec["raw"]["trade_contract_size"])
    fx = quote_to_account_fx(trade["symbol"], trade["exit_price"])
    if fx is None:
        return None, "unconverted quote currency"
    return per_night * nights * units * fx, f"{nights:g} night(s)"


def realized_spread_pips(trade: dict, spec: dict, cost):
    """entry_spread_points (broker points) → pips of the research CostModel."""
    pts = trade.get("entry_spread_points")
    if pts is None:
        return None
    return float(pts) * float(spec["raw"]["point"]) / float(cost.pip_size)


# ───────────────────────────── the report ────────────────────────────────────
def build_report(db_path=None, spec_fn=default_spec_fn,
                 cost_fn=default_cost_fn) -> dict:
    trades = load_trades(db_path)
    rows, no_spec = [], []
    for t in trades:
        spec, cost = spec_fn(t["symbol"]), cost_fn(t["symbol"])
        if spec is None or cost is None:
            no_spec.append(t["symbol"])
            continue
        exp_swap, note = expected_swap_ccy(t, spec)
        rows.append({
            **t,
            "nights_note": note,
            "swap_expected": exp_swap,
            "swap_diff": (None if exp_swap is None
                          else float(t["swap"] or 0.0) - exp_swap),
            "spread_realized_pips": realized_spread_pips(t, spec, cost),
            "spread_model_pips": float(cost.spread_pips),
        })

    by_symbol = {}
    for r in rows:
        s = by_symbol.setdefault(r["symbol"], {"n": 0, "spreads": [],
                                               "model": r["spread_model_pips"]})
        s["n"] += 1
        if r["spread_realized_pips"] is not None:
            s["spreads"].append(r["spread_realized_pips"])
    for sym, s in by_symbol.items():
        s["n_spread"] = len(s["spreads"])
        s["median_realized"] = float(np.median(s["spreads"])) if s["spreads"] else None
        s["ratio"] = (s["median_realized"] / s["model"]
                      if s["spreads"] and s["model"] > 0 else None)
        s["breach"] = bool(s["ratio"] is not None and s["ratio"] > KILL_RATIO)

    conv = [r for r in rows if r["swap_expected"] is not None]
    swap_totals = {
        "realized": sum(float(r["swap"] or 0.0) for r in conv),
        "expected": sum(r["swap_expected"] for r in conv),
        "n": len(conv),
        "max_abs_diff": max((abs(r["swap_diff"]) for r in conv), default=0.0),
    }
    return {
        "rows": rows,
        "by_symbol": by_symbol,
        "swap_totals": swap_totals,
        "no_spec_symbols": sorted(set(no_spec)),
        "breaches": sorted(s for s, v in by_symbol.items() if v["breach"]),
        "schema_gaps": ["slippage: journal records fill price only (no "
                        "requested-price column) — unmeasurable until the "
                        "executor journals it"],
        "kill_ratio": KILL_RATIO,
        "n_trades": len(trades),
    }


def render_text(rep: dict) -> str:
    L = [f"FORWARD-TEST RECONCILIATION — {rep['n_trades']} closed trade(s), "
         f"kill threshold {rep['kill_ratio']}x (GUIDE.md §6)"]
    L.append(f"{'ticket':<11}{'symbol':<8}{'side':<5}{'nights':<12}"
             f"{'swap real':>10}{'swap model':>11}{'spread real':>12}{'model':>7}")
    for r in rep["rows"]:
        sp = ("—" if r["spread_realized_pips"] is None
              else f"{r['spread_realized_pips']:.1f}p")
        se = "—" if r["swap_expected"] is None else f"{r['swap_expected']:+.2f}"
        L.append(f"{r['ticket']:<11}{r['symbol']:<8}{r['side']:<5}"
                 f"{r['nights_note']:<12}{(r['swap'] or 0.0):>+10.2f}{se:>11}"
                 f"{sp:>12}{r['spread_model_pips']:>6.1f}p")
    L.append("")
    for sym, s in sorted(rep["by_symbol"].items()):
        med = "—" if s["median_realized"] is None else f"{s['median_realized']:.1f}p"
        ratio = "—" if s["ratio"] is None else f"{s['ratio']:.2f}x"
        L.append(f"  {sym:<8} spread: median realized {med} vs model "
                 f"{s['model']:.1f}p -> {ratio}"
                 f"{'  ** BREACH **' if s['breach'] else ''}")
    st = rep["swap_totals"]
    L.append(f"  swap: realized {st['realized']:+.2f} vs model "
             f"{st['expected']:+.2f} over {st['n']} trade(s), "
             f"max |diff| {st['max_abs_diff']:.2f}")
    for g in rep["schema_gaps"]:
        L.append(f"  gap: {g}")
    if rep["no_spec_symbols"]:
        L.append(f"  no captured spec (excluded): {', '.join(rep['no_spec_symbols'])}")
    return "\n".join(L)


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else None
    print(render_text(build_report(db)))
