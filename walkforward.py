"""walkforward.py — rolling walk-forward / out-of-sample validation.

STRATEGY-AGNOSTIC: per fold it grid-searches the active strategy's walk-forward
params on the IN-SAMPLE window ONLY, picks the best by PROFIT FACTOR (requiring
>= WF_MIN_TRADES in-sample to be eligible), then applies that fixed param set to
the immediately-following OUT-OF-SAMPLE window. The search grid comes from the
strategy's `wf_grid()`; if it declares none (the default), the harness falls back
to the SMA fast/slow grid in `WalkForwardConfig` — so SMA's behaviour is unchanged.
The OOS window is strictly AFTER the IS window — no peeking, no overlap. Every OOS
segment is concatenated into ONE continuous out-of-sample equity curve: that curve
is the honest performance estimate. The REALISTIC cost model (config.REALISTIC_COSTS)
is used throughout.

Honest caveat: walk-forward REDUCES but does NOT eliminate overfitting risk. It
guards against fitting *parameters* to one period, but if you try enough strategy
*ideas*, some will clear OOS by luck. A good OOS result is necessary, not
sufficient.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

import strategies
from config import STRATEGY, BACKTEST, WALKFORWARD, REALISTIC_COSTS, BASE_DIR
from data import load_ohlcv
from backtest import _simulate, _pf, _bars_per_year, cost_dict
from registry import ResultsRegistry, multiple_testing_warning


@dataclass(frozen=True)
class Fold:
    is_start: int     # inclusive
    is_end: int       # exclusive  -> last in-sample index is is_end-1
    oos_start: int    # inclusive  (== is_end: strictly after the IS window)
    oos_end: int      # exclusive


def make_folds(n_bars: int, is_bars: int, oos_bars: int, step: int) -> list[Fold]:
    """Rolling, non-anchored folds. Guarantees oos_start == is_end, so the OOS
    window never overlaps and always starts strictly after the last IS index."""
    folds = []
    start = 0
    while start + is_bars + oos_bars <= n_bars:
        folds.append(Fold(start, start + is_bars,
                          start + is_bars, start + is_bars + oos_bars))
        start += step
    return folds


def _eligible_combos(fast_grid, slow_grid):
    return [(f, s) for f in fast_grid for s in slow_grid if f < s]


def wf_combos(strategy, cfg):
    """The list of param dicts grid-searched per IS window. If the strategy declares
    a `wf_grid()` (e.g. ts_momentum), use its validated cross-product (nested in
    declared key order, for deterministic tie-breaks). Otherwise fall back to SMA's
    fast/slow grid from `WalkForwardConfig` — the original path, combo-for-combo and
    in the same order, so SMA's walk-forward is unchanged."""
    grid = strategy.wf_grid()
    if grid is None:
        return [{"fast": f, "slow": s}
                for f, s in _eligible_combos(cfg.fast_grid, cfg.slow_grid)]
    keys = list(grid.keys())
    combos = []
    for vals in itertools.product(*(list(grid[k]) for k in keys)):
        combo = dict(zip(keys, vals))
        if strategy.validate_params(**combo):
            combos.append(combo)
    return combos


def best_in_sample(o, c, t, cfg, cost, strategy):
    """Grid-search on the IS window; return (best_params, is_result) ranked by
    profit factor among combos with >= min_trades, or None if none qualify."""
    best = None
    best_key = None
    for combo in wf_combos(strategy, cfg):
        r = _simulate(o, c, t, strategy, combo,
                      BACKTEST.initial_cash, BACKTEST.exposure,
                      BACKTEST.allow_short, cost, warmup=0,
                      timeframe_min=STRATEGY.timeframe_min, symbol=STRATEGY.symbol)
        if r.n_trades < cfg.min_trades_in_sample:
            continue
        key = 1e12 if r.profit_factor == float("inf") else r.profit_factor
        if best is None or key > best_key:
            best, best_key = (dict(combo), r), key
    return best


def _pooled_stats(equity, trade_pnls, initial_cash):
    """Headline metrics from the concatenated OOS curve + pooled OOS trades."""
    n_tr = int(trade_pnls.size)
    wins = trade_pnls[trade_pnls > 0]
    losses = trade_pnls[trade_pnls < 0]
    gp = float(wins.sum()) if wins.size else 0.0
    gl = float(-losses.sum()) if losses.size else 0.0
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    peak = np.maximum.accumulate(equity)
    dd = float(((equity - peak) / np.where(peak == 0, np.nan, peak)).min() * 100.0)
    return {
        "total_return_pct": (float(equity[-1]) / initial_cash - 1.0) * 100.0,
        "final_equity": float(equity[-1]),
        "win_rate_pct": (wins.size / n_tr * 100.0) if n_tr else 0.0,
        "profit_factor": pf,
        "expectancy_per_trade": float(trade_pnls.mean()) if n_tr else 0.0,
        "max_drawdown_pct": dd,
        "n_trades": n_tr,
    }


def run_walkforward(cfg=WALKFORWARD, cost=REALISTIC_COSTS):
    strategy = strategies.get(STRATEGY.name)
    data = load_ohlcv(STRATEGY.symbol, STRATEGY.timeframe_min)
    o, c, t = data.open, data.close, data.time
    folds = make_folds(len(c), cfg.in_sample_bars, cfg.out_of_sample_bars, cfg.step)

    running_equity = BACKTEST.initial_cash
    oos_segments = []
    oos_trade_chunks = []
    selected_configs = []          # one per eligible fold; the configs that touch OOS
    rows = []

    for k, fold in enumerate(folds):
        io = o[fold.is_start:fold.is_end]
        ic = c[fold.is_start:fold.is_end]
        it = t[fold.is_start:fold.is_end]
        best = best_in_sample(io, ic, it, cfg, cost, strategy)

        if best is None:
            rows.append({"k": k, "fold": fold, "params": None,
                         "is": None, "oos": None,
                         "is_dates": (str(it[0])[:10], str(it[-1])[:10])})
            continue

        combo, is_res = best
        # Seed the OOS indicator with `warmup_bars` of REAL prior history (all <
        # oos_start, so no look-ahead); trading + equity start exactly at oos_start.
        # For SMA warmup_bars == slow, so this is identical to the original path.
        w = min(strategy.warmup_bars(**combo), fold.oos_start)
        lo = fold.oos_start - w
        oo = o[lo:fold.oos_end]
        oc = c[lo:fold.oos_end]
        ot = t[lo:fold.oos_end]
        oos_res = _simulate(oo, oc, ot, strategy, combo,
                            running_equity, BACKTEST.exposure, BACKTEST.allow_short,
                            cost, warmup=w, timeframe_min=STRATEGY.timeframe_min,
                            symbol=STRATEGY.symbol)

        seg = oos_res.equity_curve
        oos_segments.append(seg if not oos_segments else seg[1:])  # drop seam dupe
        oos_trade_chunks.append(oos_res.trade_pnls)
        running_equity = float(oos_res.equity_curve[-1])
        selected_configs.append(dict(combo))

        rows.append({"k": k, "fold": fold, "params": dict(combo),
                     "is": is_res, "oos": oos_res,
                     "is_dates": (str(it[0])[:10], str(it[-1])[:10])})

    oos_equity = (np.concatenate(oos_segments) if oos_segments
                  else np.array([BACKTEST.initial_cash], dtype=float))
    oos_trades = (np.concatenate(oos_trade_chunks) if oos_trade_chunks
                  else np.array([], dtype=float))
    pooled = _pooled_stats(oos_equity, oos_trades, BACKTEST.initial_cash)

    return {"folds": folds, "rows": rows, "oos_equity": oos_equity,
            "oos_trades": oos_trades, "pooled": pooled, "cfg": cfg, "cost": cost,
            "symbol": STRATEGY.symbol, "tf": STRATEGY.timeframe_min,
            "strategy": STRATEGY.name, "selected_configs": selected_configs,
            "data_start": str(t[0]), "data_end": str(t[-1]), "n_bars": len(c)}


def _mean(vals):
    arr = np.array([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    return float(arr.mean()) if arr.size else float("nan")


def _annualise(return_pct, bars, bars_per_year):
    if bars <= 0:
        return float("nan")
    return ((1.0 + return_pct / 100.0) ** (bars_per_year / bars) - 1.0) * 100.0


def _aggregates(res):
    """IS = mean across folds (IS windows OVERLAP, so they can only be averaged).
    OOS = trade-POOLED from the continuous curve (OOS windows are disjoint, so
    pooling is valid AND consistent with the headline). Returns are annualised so
    the 6-mo IS and 1-mo OOS horizons are comparable. The naive per-fold OOS means
    are also returned, only to expose how much rosier averaging-of-ratios looks."""
    rows, cfg, pooled = res["rows"], res["cfg"], res["pooled"]
    bpy = _bars_per_year(res["tf"])
    el = [r for r in rows if r["is"] is not None]
    n_oos_bars = int(res["oos_equity"].shape[0])
    init = BACKTEST.initial_cash

    return {
        "n_folds": len(rows), "n_eligible": len(el),
        # returns, annualised
        "is_ret": _mean([_annualise(r["is"].total_return_pct, cfg.in_sample_bars, bpy) for r in el]),
        "oos_ret": _annualise(pooled["total_return_pct"], n_oos_bars, bpy),
        # horizon-independent: mean IS vs pooled OOS
        "is_win": _mean([r["is"].win_rate_pct for r in el]),
        "oos_win": pooled["win_rate_pct"],
        "is_pf": _mean([r["is"].profit_factor for r in el]),
        "oos_pf": pooled["profit_factor"],
        "is_exp": _mean([r["is"].expectancy_pct for r in el]),
        "oos_exp": pooled["expectancy_per_trade"] / init * 100.0,
        "is_dd": _mean([r["is"].max_drawdown_pct for r in el]),
        "oos_dd": pooled["max_drawdown_pct"],
        # naive per-fold OOS means — shown ONLY as a cautionary contrast
        "oos_pf_meanfold": _mean([r["oos"].profit_factor for r in el]),
        "oos_win_meanfold": _mean([r["oos"].win_rate_pct for r in el]),
    }


def _combo_str(params) -> str:
    """Render a chosen param dict compactly, e.g. {'fast':20,'slow':50} -> '20/50',
    {'lookback':120,'anchor':200} -> '120/200'."""
    if not params:
        return "—"
    return "/".join(str(v) for v in params.values())


def build_markdown(res) -> str:
    cfg, agg, p = res["cfg"], _aggregates(res), res["pooled"]
    # Describe the grid that was ACTUALLY searched: the strategy's wf_grid() if it
    # declares one (e.g. ts_momentum), else the SMA fast/slow grid from config.
    _wfg = strategies.get(res["strategy"]).wf_grid()
    grid_desc = (f"fast {list(cfg.fast_grid)} × slow {list(cfg.slow_grid)}"
                 if _wfg is None
                 else " × ".join(f"{k} {list(v)}" for k, v in _wfg.items()))
    L = []
    L.append("# WALKFORWARD.md — Rolling Out-of-Sample Validation\n")
    L.append(f"- Symbol/TF: **{res['symbol']} H{res['tf'] // 60}**, {res['n_bars']} bars\n"
             f"- Window: **{cfg.in_sample_bars} IS / {cfg.out_of_sample_bars} OOS**, "
             f"step {cfg.step} (rolling, non-anchored)\n"
             f"- Selection: best **profit factor** in-sample, ≥ {cfg.min_trades_in_sample} IS trades to qualify\n"
             f"- Grid: {grid_desc}\n"
             f"- Costs: spread={res['cost'].spread_pips}p, comm/lot={res['cost'].commission_per_lot}, "
             f"slip={res['cost'].slippage_pips}p, fill={res['cost'].fill_timing}\n"
             f"- Folds: {agg['n_folds']} ({agg['n_eligible']} with an eligible param set)\n")

    L.append("\n## Per-fold (IS picks → OOS realized)\n")
    L.append("| # | IS window | best params | IS PF | IS tr | IS ret% | OOS ret% | OOS PF | OOS tr | OOS win% |")
    L.append("|--:|---|---|--:|--:|--:|--:|--:|--:|--:|")
    for r in res["rows"]:
        d0, d1 = r["is_dates"]
        if r["is"] is None:
            L.append(f"| {r['k']} | {d0}→{d1} | — (none ≥{cfg.min_trades_in_sample} tr) | | | | | | | |")
            continue
        i, oo = r["is"], r["oos"]
        L.append(f"| {r['k']} | {d0}→{d1} | {_combo_str(r['params'])} | {_pf(i.profit_factor)} "
                 f"| {i.n_trades} | {i.total_return_pct:+.2f} | {oo.total_return_pct:+.2f} "
                 f"| {_pf(oo.profit_factor)} | {oo.n_trades} | {oo.win_rate_pct:.1f} |")

    def deg(a, b):
        return f"{a:.3f} → {b:.3f}  (Δ {b - a:+.3f})"

    L.append("\n## Aggregate: in-sample expectation vs out-of-sample reality\n")
    L.append("**mean IS** = average across eligible folds (IS windows OVERLAP, so they can "
             "only be averaged). **OOS** = trade-**pooled** from the continuous curve (OOS "
             "windows are disjoint → pooling is valid and matches the headline). Returns are "
             "**annualised** so the ~6-mo IS and ~1-mo OOS horizons compare; expectancy is "
             "% of starting equity per trade.\n")
    L.append("| Metric | mean IS | OOS (pooled) | degradation |")
    L.append("|---|--:|--:|---|")
    L.append(f"| Return % (ann.) | {agg['is_ret']:+.2f} | {agg['oos_ret']:+.2f} | {agg['oos_ret'] - agg['is_ret']:+.2f} |")
    L.append(f"| Win rate % | {agg['is_win']:.2f} | {agg['oos_win']:.2f} | {agg['oos_win'] - agg['is_win']:+.2f} |")
    L.append(f"| Profit factor | {agg['is_pf']:.3f} | {_pf(agg['oos_pf'])} | {agg['oos_pf'] - agg['is_pf']:+.3f} |")
    L.append(f"| Expectancy/trade % | {agg['is_exp']:+.4f} | {agg['oos_exp']:+.4f} | {agg['oos_exp'] - agg['is_exp']:+.4f} |")
    L.append(f"| Max drawdown % | {agg['is_dd']:.2f} | {agg['oos_dd']:.2f} | {agg['oos_dd'] - agg['is_dd']:+.2f} |")
    L.append(f"\n> ⚠ Statistical trap: the **naive per-fold OOS mean** profit factor is "
             f"{agg['oos_pf_meanfold']:.3f} and win rate {agg['oos_win_meanfold']:.1f}% — far "
             f"rosier than the pooled {_pf(agg['oos_pf'])} / {agg['oos_win']:.1f}%. Averaging "
             f"ratios across short windows is upward-biased; the trade-pooled continuous curve "
             f"is the figure to trust.")

    L.append("\n## Headline: the continuous OOS equity curve (the honest number)\n")
    L.append("Every OOS segment chained end-to-end — this is the only number that was "
             "never optimised on:\n")
    L.append(f"- Total OOS return: **{p['total_return_pct']:+.2f}%** "
             f"(start {BACKTEST.initial_cash:,.0f} → end {p['final_equity']:,.0f})")
    L.append(f"- OOS profit factor: **{_pf(p['profit_factor'])}**, win rate {p['win_rate_pct']:.1f}%, "
             f"expectancy/trade {p['expectancy_per_trade']:+.2f}")
    L.append(f"- OOS max drawdown: **{p['max_drawdown_pct']:.2f}%**, total OOS trades: {p['n_trades']}\n")

    L.append("## Reading this\n")
    L.append("- IS→OOS degradation is expected and is the whole point: it quantifies how "
             "much the in-sample pick was flattered by fitting.\n"
             "- For SMA(20/50)-style crossovers, OOS is expected to land **worse than IS and "
             "likely still negative**. That is the tool working correctly, not a bug to fix.\n"
             "- Walk-forward controls *parameter* overfitting only. Trying many strategy ideas "
             "re-introduces selection bias across ideas — track how many you tried.\n"
             "- Set realistic costs to your broker's real figures (see FILL_MODEL.md) before "
             "trusting absolute OOS returns.\n")
    return "\n".join(L)


def print_summary(res) -> None:
    agg, p = _aggregates(res), res["pooled"]
    bar = "=" * 76
    print("\n" + bar)
    print(f"  WALK-FORWARD — {res['symbol']} H{res['tf'] // 60}  "
          f"({res['cfg'].in_sample_bars} IS / {res['cfg'].out_of_sample_bars} OOS, "
          f"step {res['cfg'].step})  {agg['n_eligible']}/{agg['n_folds']} folds")
    print(bar)
    print(f"  {'Metric':<22}{'mean IS':>14}{'OOS pooled':>14}{'degradation':>16}")
    print("  " + "-" * 66)
    print(f"  {'Return % (ann.)':<22}{agg['is_ret']:>14.2f}{agg['oos_ret']:>14.2f}{agg['oos_ret'] - agg['is_ret']:>+16.2f}")
    print(f"  {'Win rate %':<22}{agg['is_win']:>14.2f}{agg['oos_win']:>14.2f}{agg['oos_win'] - agg['is_win']:>+16.2f}")
    print(f"  {'Profit factor':<22}{agg['is_pf']:>14.3f}{agg['oos_pf']:>14.3f}{agg['oos_pf'] - agg['is_pf']:>+16.3f}")
    print(f"  {'Expectancy/trade %':<22}{agg['is_exp']:>14.4f}{agg['oos_exp']:>14.4f}{agg['oos_exp'] - agg['is_exp']:>+16.4f}")
    print(f"  {'Max drawdown %':<22}{agg['is_dd']:>14.2f}{agg['oos_dd']:>14.2f}{agg['oos_dd'] - agg['is_dd']:>+16.2f}")
    print(bar)
    print(f"  CONTINUOUS OOS CURVE — the honest estimate (start {BACKTEST.initial_cash:,.0f}):")
    print(f"    total return {p['total_return_pct']:+.2f}%   "
          f"PF {_pf(p['profit_factor'])}   win {p['win_rate_pct']:.1f}%   "
          f"maxDD {p['max_drawdown_pct']:.2f}%   trades {p['n_trades']}")
    print(f"  ⚠ naive per-fold OOS mean would show PF {agg['oos_pf_meanfold']:.2f} / "
          f"win {agg['oos_win_meanfold']:.1f}% — averaging-of-ratios bias; trust the pooled curve.")
    print(bar)


if __name__ == "__main__":
    res = run_walkforward()
    print_summary(res)
    md = build_markdown(res)
    out = BASE_DIR / "WALKFORWARD.md"
    out.write_text(md)
    print(f"\n  Full per-fold report written to {out}")

    agg, p, cfg = _aggregates(res), res["pooled"], res["cfg"]
    metrics_is = {"return_pct": round(agg["is_ret"], 4), "win_rate_pct": round(agg["is_win"], 4),
                  "profit_factor": round(agg["is_pf"], 4),
                  "expectancy_pct": round(agg["is_exp"], 6), "max_dd_pct": round(agg["is_dd"], 4)}
    metrics_oos = {"return_pct": round(p["total_return_pct"], 4),
                   "win_rate_pct": round(p["win_rate_pct"], 4),
                   "profit_factor": None if p["profit_factor"] == float("inf") else round(p["profit_factor"], 4),
                   "expectancy": round(p["expectancy_per_trade"], 4),
                   "max_dd_pct": round(p["max_drawdown_pct"], 4), "trades": p["n_trades"]}
    _wf_grid = strategies.get(res["strategy"]).wf_grid()
    grid_spec = {"is_bars": cfg.in_sample_bars, "oos_bars": cfg.out_of_sample_bars,
                 "step": cfg.step, "select": "profit_factor",
                 "min_trades": cfg.min_trades_in_sample,
                 "grid_fast": list(cfg.fast_grid), "grid_slow": list(cfg.slow_grid)}
    if _wf_grid is not None:   # non-SMA strategy: record its actual search grid
        grid_spec = {"is_bars": cfg.in_sample_bars, "oos_bars": cfg.out_of_sample_bars,
                     "step": cfg.step, "select": "profit_factor",
                     "min_trades": cfg.min_trades_in_sample,
                     "wf_grid": {k: list(v) for k, v in _wf_grid.items()}}
    dm = {"symbol": res["symbol"], "timeframe": res["tf"], "data_start": res["data_start"],
          "data_end": res["data_end"], "n_bars": res["n_bars"]}
    try:
        reg = ResultsRegistry()
        rh, dup = reg.log_run("walkforward", res["strategy"], grid_spec,
                              cost_dict(res["cost"]), dm, metrics_is=metrics_is,
                              metrics_oos=metrics_oos, oos_configs=res["selected_configs"],
                              notes=f"OOS ret {p['total_return_pct']:+.2f}% PF {_pf(p['profit_factor'])}")
        n, _ = reg.multiple_testing_count()
        reg.close()
        print(f"  logged to results registry (hash {rh}{'  ↻ DUPLICATE of a prior run' if dup else ''})\n")
        print("  " + multiple_testing_warning(n).replace("\n", "\n  ") + "\n")
    except Exception as e:
        print(f"  [registry] skipped: {e}")
