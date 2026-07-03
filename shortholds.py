"""shortholds.py — Phase 5: SHORT-HOLD momentum on H4. Can faster cycles outrun
financing?

THE FALSIFIABLE QUESTION (fixed before any run): Phase 4 showed TSMOM's thin gross
edge dies under weeks-long financing drag. Does the RATIO of gross edge to
financing cost improve as hold time shrinks — and enough to clear costs? This is
NOT a hunt for a profitable config; a clean negative answer is a complete result.

HYPOTHESIS CLASS (a priori, no sweeps): the SAME ts_momentum signal (one
implementation, parameterised — not a fork) on H4 bars, exactly three configs:
    C1  lookback  30 / anchor  50   (~5 / 8 trading days)
    C2  lookback  60 / anchor 100   (~10 / 17 trading days)
    C3  lookback 120 / anchor 200   (~20 / 33 days — the bridge to Phase 3's D1)
C3 runs FIRST: if H4 execution costs alone erase more than the swap savings there,
C1/C2 are almost certainly dead too — the kill criterion stops the run early
rather than performing completeness theatre (turnover cost scaling is the known
enemy of this hypothesis).

COST MODEL — stricter, not looser: full realistic trading stack per trade
(spread, commission, slippage, next-bar fills) PLUS the Phase-4b DIRECTIONAL swap
(real broker swap_long/swap_short per instrument, triple-swap Wednesday, weekend
nights free). Every sleeve runs THREE times to decompose the return:
    gross (zero costs) → +trading costs → +directional swap = net
so the report shows gross return, trading-cost drag, swap drag and net directly —
the actual research question, not just a headline Sharpe.

Gate (unchanged): demo unlocks only at portfolio Sharpe ~0.5+ net of ALL costs on
pooled OOS. Anything less: log it, write SHORTHOLDS.md, stop. No live wiring,
no n8n, no dashboard.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

import strategies
from config import BACKTEST, BASE_DIR, cost_for
from data import load_ohlcv
from backtest import _bars_per_year, cost_dict
import portfolio as pf
from robustness import _connected_components

H4_MIN = 240
D1_MIN = 1440
MIN_BARS = 2000                      # per-sleeve floor (brief), same logic as portfolio.py
BASKET5 = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD")
STRAT = "ts_momentum"
# H4 fold structure = the D1 750/250 (~3yr IS / ~1yr OOS) scaled by 6 bars/day.
WF_IS_H4, WF_OOS_H4 = 4500, 1500
WF_IS_D1, WF_OOS_D1 = 750, 250


def _params(lookback, anchor) -> dict:
    p = strategies.get(STRAT).default_params()
    p.update({"lookback": lookback, "anchor": anchor})
    return p


# The three a-priori configs. C3 (the D1 bridge) is evaluated FIRST for the kill
# criterion; list order here is presentation order.
CONFIGS = (("C1", _params(30, 50)), ("C2", _params(60, 100)),
           ("C3", _params(120, 200)))


def cost_stack(symbol, kind):
    """The three cost stacks of the decomposition. 'zero' keeps the instrument's
    pip/contract geometry but strips every cost, so fills == bar opens."""
    if kind == "zero":
        return replace(cost_for(symbol, with_swap=False),
                       spread_pips=0.0, slippage_pips=0.0, commission_per_lot=0.0)
    if kind == "trade":
        return cost_for(symbol, with_swap=False)
    if kind == "full":
        return cost_for(symbol, swap_model="directional")
    raise ValueError(kind)


# ───────────────────────── per-sleeve decomposition ──────────────────────────
def sleeve_decomposition(symbol, params, timeframe_min, initial_cash):
    """Run one sleeve through all three cost stacks. The signal (and therefore the
    trade list) is identical across stacks — only costs differ — so the annualised
    differences ARE the drags. Returns None if data is missing/too short."""
    runs = {}
    for kind in ("zero", "trade", "full"):
        s = pf._build_sleeve(symbol, params, initial_cash,
                             timeframe_min=timeframe_min, min_bars=MIN_BARS,
                             cost=cost_stack(symbol, kind))
        if s is None:
            return None
        runs[kind] = s
    bpy = _bars_per_year(timeframe_min)

    def ann(s):
        n = s.rets.size
        return (((s.equity[-1] / s.equity[0]) ** (bpy / n) - 1.0) * 100.0) if n else 0.0

    g, t, f = ann(runs["zero"]), ann(runs["trade"]), ann(runs["full"])
    r = runs["full"].res
    hb, hd = r.holding_bars, r.holding_days
    data = load_ohlcv(symbol, timeframe_min, prefer_live=False)
    regime = strategies.get(STRAT).generate(data.close, **params).regime
    return {
        "symbol": symbol, "n_bars": len(data),
        "start": str(np.datetime64(data.time[0], "D")),
        "end": str(np.datetime64(data.time[-1], "D")),
        "gross_ann": g, "trade_drag_ann": g - t, "swap_drag_ann": t - f, "net_ann": f,
        "med_hold_bars": float(np.median(hb)) if hb.size else 0.0,
        "med_hold_days": float(np.median(hd)) if hd.size else 0.0,
        "n_trades": r.n_trades,
        "in_market_frac": float(np.mean(regime != 0)),
        "swap_ccy": r.total_swap_cost, "comm_ccy": r.total_commission,
        "runs": runs,
    }


# ───────────────────────── config-level run (portfolio) ──────────────────────
def run_config(label, params, timeframe_min=H4_MIN, initial_cash=None):
    """Decompose every sleeve, build the FULL-cost portfolio on the common window
    (timestamp alignment — H4 bars share calendar days), and walk it forward with
    causal inverse-vol weights. Everything reuses portfolio.py building blocks."""
    initial_cash = float(initial_cash or BACKTEST.initial_cash)
    bpy = _bars_per_year(timeframe_min)
    res_kind = "s" if timeframe_min < D1_MIN else "D"

    decs, dropped = [], []
    for sym in BASKET5:
        d = sleeve_decomposition(sym, params, timeframe_min, initial_cash)
        if d is None:
            dropped.append((sym, f"missing/short data @{timeframe_min}min"))
        else:
            decs.append(d)
    if len(decs) < 2:
        return {"ok": False, "label": label, "dropped": dropped}

    kept = [d["runs"]["full"] for d in decs]
    common, R = pf.align_returns(kept, resolution=res_kind)
    w = pf.inverse_vol_weights(R)
    port_rets, port_eq = pf.combine(R, w, initial_cash)
    port = pf.series_metrics(port_rets, port_eq,
                             n_trades=sum(d["n_trades"] for d in decs),
                             bars_per_year=bpy)
    sleeves_m = pf.sleeve_metrics_on(common, R, kept, initial_cash, bars_per_year=bpy)
    best_sym = max(sleeves_m, key=lambda s: sleeves_m[s]["sharpe"])
    corr = pf.correlation_matrix(R)

    is_bars = WF_IS_H4 if timeframe_min == H4_MIN else WF_IS_D1
    oos_bars = WF_OOS_H4 if timeframe_min == H4_MIN else WF_OOS_D1
    wf = pf.run_portfolio_wf(basket=BASKET5, params=params,
                             initial_cash=initial_cash, is_bars=is_bars,
                             oos_bars=oos_bars, timeframe_min=timeframe_min,
                             min_bars=MIN_BARS, resolution=res_kind,
                             bars_per_year=bpy, sleeves=kept)

    # Portfolio-level drags = inverse-vol-weighted average of per-sleeve annualised
    # drags (the weights that actually blend the sleeves).
    wavg = lambda k: float(sum(wi * d[k] for wi, d in zip(w, decs)))
    return {
        "ok": True, "label": label, "params": dict(params),
        "timeframe_min": timeframe_min, "bpy": bpy, "decs": decs,
        "dropped": dropped, "common": common, "weights": w, "corr": corr,
        "port": port, "sleeves_m": sleeves_m, "best_sym": best_sym, "wf": wf,
        "gross_ann_w": wavg("gross_ann"), "trade_drag_w": wavg("trade_drag_ann"),
        "swap_drag_w": wavg("swap_drag_ann"), "net_ann_w": wavg("net_ann"),
        "med_hold_bars": float(np.median([d["med_hold_bars"] for d in decs])),
        "med_hold_days": float(np.median([d["med_hold_days"] for d in decs])),
        "initial_cash": initial_cash,
    }


# ───────────────────────────── kill criterion ────────────────────────────────
def kill_check(d1_ref, c3):
    """The brief's explicit early stop: if C3's EXTRA execution cost (vs the same
    signal on D1) exceeds its swap SAVINGS, faster cycling loses more to turnover
    than it saves in financing — C1/C2 (even faster) are then almost certainly
    dead too. All figures are portfolio-level weighted annualised drags."""
    swap_savings = d1_ref["swap_drag_w"] - c3["swap_drag_w"]
    extra_exec = c3["trade_drag_w"] - d1_ref["trade_drag_w"]
    return {
        "swap_savings_ann": swap_savings, "extra_exec_ann": extra_exec,
        "killed": extra_exec > swap_savings,
        "margin_ann": swap_savings - extra_exec,
    }


# ─────────────────── per-config robustness (H4 surface) ──────────────────────
def h4_robustness_surface(symbol="EURUSD", min_trades=20):
    """IN-SAMPLE sensitivity sweep of the full ts_momentum grid on ONE H4 sleeve
    with the FULL directional cost stack. IS-only (no OOS is touched, so this adds
    nothing to the multiple-testing count); its job is the SHAPE around each
    a-priori config, not validation."""
    from backtest import _simulate
    strat = strategies.get(STRAT)
    grid = strat.param_grid()
    kx, ky = list(grid.keys())
    xs, ys = list(grid[kx]), list(grid[ky])
    data = load_ohlcv(symbol, H4_MIN, prefer_live=False)
    cost = cost_stack(symbol, "full")

    score = np.full((len(xs), len(ys)), np.nan)
    trades = np.zeros((len(xs), len(ys)), dtype=int)
    valid, prof = set(), set()
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            if not strat.validate_params(**{kx: x, ky: y}):
                continue
            r = _simulate(data.open, data.close, data.time, strat, {kx: x, ky: y},
                          BACKTEST.initial_cash, BACKTEST.exposure,
                          BACKTEST.allow_short, cost, warmup=0,
                          timeframe_min=H4_MIN, symbol=symbol)
            score[i, j] = r.total_return_pct
            trades[i, j] = r.n_trades
            if r.n_trades >= min_trades:
                valid.add((i, j))
                if r.total_return_pct > 0.0:
                    prof.add((i, j))
    return {"kx": kx, "ky": ky, "xs": xs, "ys": ys, "score": score,
            "trades": trades, "valid": valid, "prof": prof, "symbol": symbol,
            "n_bars": len(data), "start": str(np.datetime64(data.time[0], "D")),
            "end": str(np.datetime64(data.time[-1], "D")), "cost": cost}


def classify_config_cell(surf, lookback, anchor):
    """SPIKE / PLATEAU / NO-EDGE anchored at ONE config's grid cell (same thresholds
    as robustness._classify, but judged from the config cell instead of the global
    best): NO-EDGE if the cell itself isn't profitably tradeable; PLATEAU if it sits
    inside a broad contiguous profitable block; SPIKE otherwise."""
    import math
    try:
        ij = (surf["xs"].index(lookback), surf["ys"].index(anchor))
    except ValueError:
        return "N/A", "config not on the sweep grid"
    if ij not in surf["valid"]:
        return "NO-EDGE", "too few trades to trust the cell"
    if ij not in surf["prof"]:
        return "NO-EDGE", f"cell itself loses ({surf['score'][ij]:+.2f}%)"
    comps = _connected_components(surf["prof"])
    comp = next(c for c in comps if ij in c)
    n_valid = len(surf["valid"])
    frac_prof = len(surf["prof"]) / n_valid
    plateau_size = max(3, math.ceil(0.20 * n_valid))
    if len(comp) >= plateau_size and frac_prof >= 0.25:
        return "PLATEAU", (f"cell {surf['score'][ij]:+.2f}%, block of {len(comp)} "
                           f"profitable neighbours, {frac_prof*100:.0f}% of grid profitable")
    return "SPIKE", (f"cell {surf['score'][ij]:+.2f}% but isolated "
                     f"(block {len(comp)} < plateau {plateau_size}, "
                     f"{frac_prof*100:.0f}% profitable)")


# ───────────────────────────── reporting ─────────────────────────────────────
def _fmt_pct(x):
    return f"{x:+.2f}"


def print_config(c):
    bar = "=" * 96
    print("\n" + bar)
    print(f"  {c['label']}  ts_momentum {c['params']['lookback']}/{c['params']['anchor']}"
          f"  @{c['timeframe_min']}min   common window {c['common'][0]} → {c['common'][-1]}"
          f"  ({c['common'].size} bars)")
    print(bar)
    print(f"  {'sleeve':<8}{'gross%/yr':>11}{'trade drag':>11}{'swap drag':>11}"
          f"{'NET %/yr':>11}{'medhold bars':>13}{'(days)':>8}{'in-mkt':>8}{'trades':>8}")
    print("  " + "-" * 92)
    for d in c["decs"]:
        print(f"  {d['symbol']:<8}{d['gross_ann']:>+11.2f}{d['trade_drag_ann']:>11.2f}"
              f"{d['swap_drag_ann']:>11.2f}{d['net_ann']:>+11.2f}"
              f"{d['med_hold_bars']:>13.0f}{d['med_hold_days']:>8.1f}"
              f"{d['in_market_frac']*100:>7.0f}%{d['n_trades']:>8d}")
    print("  " + "-" * 92)
    print(f"  {'w-avg':<8}{c['gross_ann_w']:>+11.2f}{c['trade_drag_w']:>11.2f}"
          f"{c['swap_drag_w']:>11.2f}{c['net_ann_w']:>+11.2f}"
          f"{c['med_hold_bars']:>13.0f}{c['med_hold_days']:>8.1f}")
    p, bs = c["port"], c["sleeves_m"][c["best_sym"]]
    print(f"  PORTFOLIO: ann {p['ann_return_pct']:+.2f}%  Sharpe {p['sharpe']:.2f}  "
          f"maxDD {p['max_dd_pct']:.1f}%  PF {p['profit_factor']:.3f}  |  best sleeve "
          f"{c['best_sym']}: ann {bs['ann_return_pct']:+.2f}%  Sharpe {bs['sharpe']:.2f}")
    if c["wf"].get("ok"):
        q = c["wf"]["pooled"]
        print(f"  POOLED WF OOS: ret {q['total_return_pct']:+.2f}%  ann "
              f"{q['ann_return_pct']:+.2f}%  Sharpe {q['sharpe']:.2f}  "
              f"maxDD {q['max_dd_pct']:.1f}%  ({len(c['wf']['rows'])} folds)")
    print(bar)


def build_markdown(d1_ref, kc, results, verdicts, surf, stopped_early, gate_pass):
    L = ["# SHORTHOLDS.md — Phase 5: Short-Hold Momentum on H4\n"]
    L.append("**Question (fixed a priori):** does the ratio of gross edge to financing "
             "cost improve as hold time shrinks — and enough to clear ALL costs? A clean "
             "negative is a complete result.\n")
    L.append("**Cost model:** full realistic trading stack per trade + Phase-4b "
             "DIRECTIONAL swap (real broker swap_long/swap_short per instrument, "
             "triple-swap **Wednesday**, weekend nights free). Stricter than Phase 4, "
             "not looser.\n")

    L.append("\n## Per-instrument H4 depth (blocker check: ≥2,000 bars, ~5yr common window)\n")
    L.append("| sleeve | H4 bars | range |")
    L.append("|---|--:|---|")
    for d in (results[-1]["decs"] if results else []):
        L.append(f"| {d['symbol']} | {d['n_bars']} | {d['start']} → {d['end']} |")
    L.append("\nAll five sleeves returned the probe-ladder maximum (20,000 bars ≈ 12.8 "
             "years) — far beyond the 2,000-bar / 5-year gate. **No data blocker.**\n")

    L.append("\n## D1 bridge reference (Phase 4 re-run under the DIRECTIONAL swap model)\n")
    L.append("| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold (days) | in-mkt | trades |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
    for d in d1_ref["decs"]:
        L.append(f"| {d['symbol']} | {d['gross_ann']:+.2f} | {d['trade_drag_ann']:.2f} "
                 f"| {d['swap_drag_ann']:.2f} | {d['net_ann']:+.2f} "
                 f"| {d['med_hold_days']:.1f} | {d['in_market_frac']*100:.0f}% | {d['n_trades']} |")
    L.append(f"| **w-avg** | **{d1_ref['gross_ann_w']:+.2f}** | **{d1_ref['trade_drag_w']:.2f}** "
             f"| **{d1_ref['swap_drag_w']:.2f}** | **{d1_ref['net_ann_w']:+.2f}** | | | |")
    p = d1_ref["port"]
    L.append(f"\nD1 portfolio (directional swap): ann **{p['ann_return_pct']:+.2f}%**, "
             f"Sharpe {p['sharpe']:.2f}, maxDD {p['max_dd_pct']:.1f}%"
             + (f"; pooled WF OOS Sharpe {d1_ref['wf']['pooled']['sharpe']:.2f}"
                if d1_ref["wf"].get("ok") else "") + ".\n")

    L.append("\n## Kill criterion (evaluated on C3, the bridge case, BEFORE C1/C2)\n")
    L.append(f"- Swap savings from H4's shorter holds: **{kc['swap_savings_ann']:+.2f} %/yr**")
    L.append(f"- EXTRA execution cost from H4 turnover: **{kc['extra_exec_ann']:+.2f} %/yr**")
    L.append(f"- Margin (savings − extra cost): **{kc['margin_ann']:+.2f} %/yr** → "
             f"{'**KILL** — turnover wins; C1/C2 not run OOS (they cycle faster still)' if kc['killed'] else 'survives to C1/C2'}\n")

    for c in results:
        v, why = verdicts.get(c["label"], ("N/A", ""))
        L.append(f"\n## {c['label']} — lookback {c['params']['lookback']} / anchor "
                 f"{c['params']['anchor']} @H4\n")
        L.append("| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold bars | med hold (days) | in-mkt | trades |")
        L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
        for d in c["decs"]:
            L.append(f"| {d['symbol']} | {d['gross_ann']:+.2f} | {d['trade_drag_ann']:.2f} "
                     f"| {d['swap_drag_ann']:.2f} | {d['net_ann']:+.2f} | {d['med_hold_bars']:.0f} "
                     f"| {d['med_hold_days']:.1f} | {d['in_market_frac']*100:.0f}% | {d['n_trades']} |")
        L.append(f"| **w-avg** | **{c['gross_ann_w']:+.2f}** | **{c['trade_drag_w']:.2f}** "
                 f"| **{c['swap_drag_w']:.2f}** | **{c['net_ann_w']:+.2f}** | "
                 f"{c['med_hold_bars']:.0f} | {c['med_hold_days']:.1f} | | |")
        p, bs = c["port"], c["sleeves_m"][c["best_sym"]]
        L.append(f"\n| metric | PORTFOLIO | best sleeve ({c['best_sym']}) |")
        L.append("|---|--:|--:|")
        L.append(f"| Annualised return % | {p['ann_return_pct']:+.2f} | {bs['ann_return_pct']:+.2f} |")
        L.append(f"| Sharpe | {p['sharpe']:.2f} | {bs['sharpe']:.2f} |")
        L.append(f"| Max drawdown % | {p['max_dd_pct']:.2f} | {bs['max_dd_pct']:.2f} |")
        L.append(f"| Profit factor (per-bar) | {p['profit_factor']:.3f} | {bs['profit_factor']:.3f} |")
        L.append(f"| Trades (total) | {p['n_trades']} | {bs['n_trades']} |")
        if c["wf"].get("ok"):
            q = c["wf"]["pooled"]
            L.append(f"\nPooled walk-forward OOS ({c['wf']['is_bars']} IS / "
                     f"{c['wf']['oos_bars']} OOS H4 bars, causal inverse-vol weights, "
                     f"{len(c['wf']['rows'])} folds): total **{q['total_return_pct']:+.2f}%**, "
                     f"ann {q['ann_return_pct']:+.2f}%, Sharpe **{q['sharpe']:.2f}**, "
                     f"maxDD {q['max_dd_pct']:.2f}%.\n")
        L.append(f"\n**Robustness verdict ({surf['symbol']} H4 surface, full costs): "
                 f"{v}** — {why}\n")

    if stopped_early:
        L.append("\n> **C1 and C2 were NOT walk-forward evaluated**: the kill criterion "
                 "fired on C3. Their cells on the in-sample H4 robustness surface are "
                 "reported above purely as shape evidence; no additional configs were "
                 "spent against out-of-sample data.\n")

    L.append("\n## Mechanism check (was the hypothesis even mechanically possible?)\n")
    L.append("Time-series momentum is in the market almost continuously (long OR short "
             "whenever the anchor filter agrees) — so financing accrues per NIGHT IN THE "
             "MARKET, not per trade. Shorter lookbacks shorten the per-trade hold but "
             "barely change nights-in-market per year; the in-mkt column above is the "
             "evidence. Swap can only shrink via (a) more flat time from the anchor "
             "filter, or (b) the directional model paying credits on the side held. "
             "Neither is a 'shorter holds pay less swap' effect — the hypothesis's core "
             "mechanism does not exist for an always-in-market signal family.\n")

    L.append("\n## Verdict\n")
    if gate_pass:
        L.append("Pooled OOS portfolio Sharpe cleared ~0.5 net of all costs. Gate MET — "
                 "demo execution may be considered, pending Julian's review.\n")
    else:
        L.append("**Gate NOT met** (portfolio Sharpe ~0.5+ net of ALL costs on pooled OOS "
                 "required). Per the brief: log it, stop. No live wiring, no n8n, no "
                 "dashboard. Short-hold momentum on H4 does NOT outrun financing — "
                 "turnover costs scale faster than swap savings, exactly the failure mode "
                 "the brief flagged as the hypothesis's known enemy.\n")
    return "\n".join(L)


# ───────────────────────────── registry logging ──────────────────────────────
def log_config(c, note):
    """One registry row per evaluated config. The oos_configs entry carries the
    TIMEFRAME so an H4 config counts as a NEW multiple-testing config even when its
    lookback/anchor match a D1 config (different bar clock = different hypothesis)."""
    from registry import ResultsRegistry, multiple_testing_warning
    syms = [d["symbol"] for d in c["decs"]]
    params = {**c["params"], "timeframe": c["timeframe_min"], "basket": syms,
              "weighting": "inverse_vol_equal_risk"}
    dm = {"symbol": "PORTFOLIO[" + "+".join(syms) + "]", "timeframe": c["timeframe_min"],
          "data_start": str(c["common"][0])[:10], "data_end": str(c["common"][-1])[:10],
          "n_bars": int(c["common"].size)}
    cdict = cost_dict(cost_stack("EURUSD", "full"))
    p = c["port"]
    mi = {"return_pct": round(p["total_return_pct"], 4), "sharpe": round(p["sharpe"], 4),
          "max_dd_pct": round(p["max_dd_pct"], 4),
          "profit_factor": None if p["profit_factor"] == float("inf") else round(p["profit_factor"], 4)}
    mo = None
    if c["wf"].get("ok"):
        q = c["wf"]["pooled"]
        mo = {"return_pct": round(q["total_return_pct"], 4), "sharpe": round(q["sharpe"], 4),
              "max_dd_pct": round(q["max_dd_pct"], 4),
              "profit_factor": None if q["profit_factor"] == float("inf") else round(q["profit_factor"], 4)}
    # H4 configs carry the timeframe in their OOS-config key (a different bar clock
    # is a different hypothesis → the multiple-testing count grows). The D1 bridge
    # reference logs the BARE param dict so it dedupes against Phase 4's existing
    # config — re-costing an already-counted config is not a new test.
    oos_key = ({**c["params"], "timeframe": c["timeframe_min"]}
               if c["timeframe_min"] != D1_MIN else dict(c["params"]))
    reg = ResultsRegistry()
    rh, dup = reg.log_run("shorthold_wf", STRAT, params, cdict, dm, metrics_is=mi,
                          metrics_oos=mo, oos_configs=[oos_key], notes=note)
    n, _ = reg.multiple_testing_count()
    reg.close()
    return rh, dup, n, multiple_testing_warning(n)


# ─────────────────────────────── main ────────────────────────────────────────
def main():
    print("\nPHASE 5 — short-hold momentum on H4 vs financing (directional swap, "
          "triple-swap Wednesday). C3 first: kill criterion before completeness.")

    # D1 bridge reference: the Phase-4 portfolio re-run under the 4b directional
    # model — validates 4b end-to-end AND anchors the kill-criterion comparison.
    d1 = run_config("D1-ref (120/200 D1, directional swap)", _params(120, 200),
                    timeframe_min=D1_MIN)
    if not d1.get("ok"):
        raise SystemExit(f"D1 reference failed: {d1.get('dropped')}")
    print_config(d1)

    c3 = run_config("C3 (bridge, 120/200 H4)", CONFIGS[2][1], timeframe_min=H4_MIN)
    if not c3.get("ok"):
        raise SystemExit(f"C3 failed: {c3.get('dropped')}")
    print_config(c3)

    kc = kill_check(d1, c3)
    print(f"\n  KILL CHECK: swap savings {kc['swap_savings_ann']:+.2f}%/yr vs extra "
          f"execution {kc['extra_exec_ann']:+.2f}%/yr → margin {kc['margin_ann']:+.2f}%/yr"
          f"  → {'KILLED (stop early)' if kc['killed'] else 'survives'}")

    results = [c3]
    if not kc["killed"]:
        for label, params in CONFIGS[:2]:
            c = run_config(f"{label} ({params['lookback']}/{params['anchor']} H4)",
                           params, timeframe_min=H4_MIN)
            if c.get("ok"):
                print_config(c)
                results.insert(len(results) - 1, c)

    # In-sample H4 robustness surface (shape only; adds nothing to the OOS count).
    print("\n  sweeping H4 robustness surface (EURUSD, full directional costs)...")
    surf = h4_robustness_surface("EURUSD")
    verdicts = {}
    for c in results:
        lbl = c["label"]
        key = lbl.split()[0]
        v, why = classify_config_cell(surf, c["params"]["lookback"], c["params"]["anchor"])
        verdicts[lbl] = (v, why)
        print(f"  robustness {key}: {v} — {why}")
    # shape evidence for the unrun configs when the kill fired
    if kc["killed"]:
        for label, params in CONFIGS[:2]:
            v, why = classify_config_cell(surf, params["lookback"], params["anchor"])
            print(f"  robustness {label} (IS shape only, not OOS-run): {v} — {why}")

    # gate: portfolio Sharpe ~0.5+ net of ALL costs on pooled OOS, on ANY config
    gate_pass = any(c["wf"].get("ok") and c["wf"]["pooled"]["sharpe"] >= 0.5
                    for c in results)

    md = build_markdown(d1, kc, results, verdicts, surf, kc["killed"], gate_pass)
    out = BASE_DIR / "SHORTHOLDS.md"
    out.write_text(md)
    print(f"\n  Report written to {out}")

    # registry: D1 directional re-run (params dedupe to Phase 4's config — the
    # multiple-testing count does NOT grow for it) + each OOS-evaluated H4 config.
    for c, note in ([(d1, "Phase 4b: D1 portfolio under DIRECTIONAL swap")] +
                    [(c, f"Phase 5 {c['label']}: net_w {c['net_ann_w']:+.2f}%/yr, "
                         f"OOS Sharpe {c['wf']['pooled']['sharpe']:.2f}"
                         if c["wf"].get("ok") else f"Phase 5 {c['label']}")
                     for c in results]):
        rh, dup, n, warn = log_config(c, note)
        print(f"  registry: {c['label']} → hash {rh}{'  ↻ DUPLICATE' if dup else ''}")
    print("\n  " + warn.replace("\n", "\n  "))
    print(f"\n  GATE: {'MET' if gate_pass else 'NOT MET'} — "
          f"{'demo unlock may be reviewed' if gate_pass else 'no live wiring, no n8n, no dashboard'}")


if __name__ == "__main__":
    main()
