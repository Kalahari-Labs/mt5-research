"""gate.py — backtest-before-trade gate. Nothing trades until it passes here.

For each strategy x symbol: pull deep REAL history through the bridge, run the
full-cost backtest, split trades into in-sample (first 70% of bars) and
out-of-sample (last 30%), and require the OOS segment to stand on its own:

  n_trades total  >= GATE_MIN_TRADES
  OOS profit factor >= GATE_MIN_PF_OOS
  OOS expectancy (R) >= GATE_MIN_EXPECTANCY_R
  max drawdown <= GATE_MAX_DD_PCT
  full-period profit factor >= 1.0

Combos that fail run in OBSERVE mode: full pipeline, journaled decisions,
zero orders. This repo's own research (29 configs, all failed walk-forward)
is the reason this gate exists and the reason its thresholds do not bend.
Results land in strategy_status with the complete metrics, honest and visible
on the dashboard.
"""
from __future__ import annotations

import json
import time

from . import config
from .analysis import Bars
from .backtester import SymbolSpec, run_backtest, compute_metrics
from .bridge import Bridge, BridgeError
from .store import Store, utcnow
from .strategies import REGISTRY


def split_metrics(result: dict, bars: Bars, oos_frac: float) -> dict:
    """Recompute metrics separately for trades entered before/after the split bar."""
    split_t = int(bars.time[int(bars.n * (1.0 - oos_frac))])
    is_trades = [t for t in result["trades"] if t["entry_t"] < split_t]
    oos_trades = [t for t in result["trades"] if t["entry_t"] >= split_t]
    init = result["initial_equity"]
    return {
        "full": result["metrics"],
        "is": compute_metrics(is_trades, init, init + sum(t["pnl"] for t in is_trades), 0.0),
        "oos": compute_metrics(oos_trades, init, init + sum(t["pnl"] for t in oos_trades), 0.0),
        "segments": segment_pfs(result["trades"], bars, config.GATE_STABILITY_SEGMENTS),
        "split_t": split_t, "bars": bars.n,
        "window": [int(bars.time[0]), int(bars.time[-1])],
    }


def segment_pfs(trades: list[dict], bars: Bars, n_segments: int) -> list:
    """Profit factor of each sequential time slice of the window (stability:
    an edge that only existed in one slice is a streak, not an edge)."""
    if n_segments < 2:
        return []
    edges = [int(bars.time[int(bars.n * k / n_segments)]) for k in range(n_segments)]
    edges.append(int(bars.time[-1]) + 1)
    out = []
    for k in range(n_segments):
        seg = [t for t in trades if edges[k] <= t["entry_t"] < edges[k + 1]]
        wins = sum(t["pnl"] for t in seg if t["pnl"] > 0)
        losses = -sum(t["pnl"] for t in seg if t["pnl"] <= 0)
        pf = round(wins / losses, 3) if losses > 0 else (None if not seg else float("inf"))
        out.append({"n": len(seg), "pf": pf})
    return out


def evaluate(metrics: dict) -> tuple[bool, str]:
    full, oos = metrics["full"], metrics["oos"]
    if full.get("n", 0) < config.GATE_MIN_TRADES:
        return False, "only %s trades (< %s): not enough evidence" % (
            full.get("n", 0), config.GATE_MIN_TRADES)
    if oos.get("n", 0) < max(5, int(config.GATE_MIN_TRADES * 0.2)):
        return False, "only %s OOS trades: not enough held-out evidence" % oos.get("n", 0)
    pf_oos = oos.get("profit_factor") or 0.0
    if pf_oos < config.GATE_MIN_PF_OOS:
        return False, "OOS profit factor %.3f < %.2f" % (pf_oos, config.GATE_MIN_PF_OOS)
    if oos.get("expectancy_r", -1) < config.GATE_MIN_EXPECTANCY_R:
        return False, "OOS expectancy %.4fR < %.2fR" % (
            oos.get("expectancy_r", -1), config.GATE_MIN_EXPECTANCY_R)
    if full.get("max_dd_pct", 100) > config.GATE_MAX_DD_PCT:
        return False, "max drawdown %.1f%% > %.1f%%" % (
            full["max_dd_pct"], config.GATE_MAX_DD_PCT)
    if (full.get("profit_factor") or 0.0) < 1.0:
        return False, "full-period profit factor %.3f < 1.0" % (full.get("profit_factor") or 0.0)
    segments = metrics.get("segments") or []
    if segments:
        ok_segs = sum(1 for s in segments
                      if s["n"] > 0 and (s["pf"] or 0) >= 1.0)
        if ok_segs < config.GATE_STABILITY_MIN_OK:
            return False, "profitable in only %d/%d time segments (need %d) — " \
                "streak, not edge" % (ok_segs, len(segments),
                                      config.GATE_STABILITY_MIN_OK)
    return True, "PASS: OOS pf=%.3f exp=%.3fR n=%s" % (
        pf_oos, oos["expectancy_r"], full["n"])


def run_gate(store: Store, bridge: Bridge, symbols: list[str] | None = None,
             verbose: bool = True) -> dict:
    """Backtest every strategy x symbol on fresh broker history; write
    strategy_status rows. Returns {(strategy, symbol): status}."""
    symbols = symbols or config.SYMBOLS
    # drop rows for strategies/symbols that no longer exist so the dashboard
    # and risk layer never consult a ghost combo
    store.prune_strategy_status(REGISTRY.keys(), symbols)
    out = {}
    for sym in symbols:
        bars_by_tf: dict[str, Bars] = {}
        try:
            spec = SymbolSpec.from_bridge(bridge.symbol(sym))
        except (BridgeError, ValueError, KeyError) as e:
            for name in REGISTRY:
                store.insert("strategy_status", {
                    "ts": utcnow(), "strategy": name, "symbol": sym,
                    "status": "observing", "reason": "gate data error: %r" % e,
                    "backtest": {}})
                out[(name, sym)] = "observing"
            continue
        for name, strat in REGISTRY.items():
            tf = strat.timeframe or config.TIMEFRAME
            t0 = time.time()
            try:
                if tf not in bars_by_tf:
                    bars_by_tf[tf] = Bars(bridge.bars(sym, tf, config.BARS_BACKTEST))
                bars = bars_by_tf[tf]
            except (BridgeError, ValueError) as e:
                store.insert("strategy_status", {
                    "ts": utcnow(), "strategy": name, "symbol": sym,
                    "status": "observing", "reason": "gate data error: %r" % e,
                    "backtest": {}})
                out[(name, sym)] = "observing"
                continue
            result = run_backtest(bars, strat, spec)
            metrics = split_metrics(result, bars, config.GATE_OOS_FRAC)
            passed, reason = evaluate(metrics)
            status = "enabled" if passed else "observing"
            store.insert("strategy_status", {
                "ts": utcnow(), "strategy": name, "symbol": sym,
                "status": status, "reason": reason, "backtest": metrics})
            out[(name, sym)] = status
            if verbose:
                f = metrics["full"]
                print("gate %-18s %-9s %-4s %-9s %5.1fs  n=%-4s pf=%-6s oos_pf=%-6s  %s"
                      % (name, sym, tf, status.upper(), time.time() - t0,
                         f.get("n", 0), f.get("profit_factor"),
                         metrics["oos"].get("profit_factor"), reason))
    store.set_state("gate_last_run", utcnow())
    return out


def gate_stale(store: Store) -> bool:
    last = store.get_state("gate_last_run")
    if not last:
        return True
    then = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
    return (time.time() - then) > config.GATE_REFRESH_HOURS * 3600


def enabled_combos(store: Store) -> set[tuple[str, str]]:
    return store.enabled_combos()


if __name__ == "__main__":
    s = Store()
    b = Bridge()
    res = run_gate(s, b)
    print(json.dumps({"%s/%s" % k: v for k, v in res.items()}, indent=2))
