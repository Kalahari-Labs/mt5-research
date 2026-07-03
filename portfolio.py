"""portfolio.py — does the validated single-instrument momentum edge become
TRADEABLE through cross-asset diversification, once honest holding costs (overnight
swap) are charged?  (Phase 4.)

WHAT THIS DOES (and, just as important, what it does NOT do):
  * Runs the SAME fixed momentum signal — ONE parameter set from the D1 plateau
    centre (lookback=120, anchor=200, the ts_momentum defaults), applied UNIFORMLY
    to every instrument. Params are NOT fitted per instrument: that would re-introduce
    curve-fitting and explode the multiple-testing count. The only thing under test
    here is DIVERSIFICATION, not parameter search.
  * Each instrument is run INDEPENDENTLY through the unchanged single-instrument
    engine (`backtest._simulate`) with the realistic cost model PLUS a conservative
    per-instrument overnight-swap drag (config.cost_for / INSTRUMENT_COSTS).
  * Sleeves are scaled to EQUAL RISK by inverse-volatility weighting (naive risk
    parity) so no single instrument dominates the portfolio.
  * Sleeves are combined on their COMMON overlapping window into one portfolio
    return stream / equity curve.

Reporting (print + PORTFOLIO.md): portfolio vs single-EURUSD metrics side by side,
the sleeve return-correlation matrix, and the diversification effect (portfolio
Sharpe vs the average single-sleeve Sharpe). The walk-forward (run_portfolio_wf)
re-estimates the inverse-vol weights on each IS window and applies them to the
following OOS window — so the risk weights are set causally and the pooled OOS
curve is the honest estimate. Fixed params mean this tests TEMPORAL STABILITY, not
selection.

stdlib + numpy only. (pandas would align sleeves with a DatetimeIndex join and
compute the corr matrix with DataFrame.corr; numpy does both exactly here.)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import strategies
from config import BACKTEST, PORTFOLIO_BASKET, cost_for, BASE_DIR
from data import load_ohlcv
from backtest import _simulate, _bars_per_year

D1_MIN = 1440
FIXED_STRATEGY = "ts_momentum"
# The ONE fixed param set: the D1 plateau centre (== ts_momentum defaults 120/200).
FIXED_PARAMS = strategies.get(FIXED_STRATEGY).default_params()
# Sleeves are aligned on their COMMON window (intersection of dates), so ONE short
# sleeve truncates the whole portfolio's history. A sleeve must therefore carry
# enough D1 bars for the 200-bar signal warmup PLUS a multi-fold 750/250 walk-forward
# on the common window: ~2000 bars (≈ 8 years). Anything shorter is DROPPED and
# reported with its exact bar count rather than silently poisoning the window.
MIN_BARS = 2000
BARS_PER_YEAR = _bars_per_year(D1_MIN)   # 260 for D1


# ───────────────────────────── sleeves ──────────────────────────────────────
@dataclass
class Sleeve:
    symbol: str
    dates: np.ndarray        # datetime64, length nbars
    equity: np.ndarray       # engine equity curve, length nbars (starts at initial_cash)
    rets: np.ndarray         # per-bar strategy returns, length nbars-1 (aligned to dates[1:])
    n_trades: int
    swap_rate_annual: float
    res: object = None       # full BacktestResult (swap/commission/holding detail, Phase 5)


def _build_sleeve(symbol, params, initial_cash, timeframe_min=D1_MIN,
                  min_bars=MIN_BARS, cost=None) -> Sleeve | None:
    """Run the fixed momentum signal on one instrument through the unchanged engine.
    Default cost = realistic + symmetric swap (the Phase-4 model); pass an explicit
    `cost` for other stacks (Phase 4b directional, swap-off, zero-cost gross).
    Returns None if data is missing/too short."""
    try:
        data = load_ohlcv(symbol, timeframe_min, prefer_live=False)
    except FileNotFoundError:
        return None
    if len(data) < min_bars:
        return None
    strat = strategies.get(FIXED_STRATEGY)
    if cost is None:
        cost = cost_for(symbol, with_swap=True)
    res = _simulate(data.open, data.close, data.time, strat, params,
                    initial_cash, BACKTEST.exposure, BACKTEST.allow_short, cost,
                    warmup=0, timeframe_min=timeframe_min, source=data.source,
                    symbol=symbol)
    eq = res.equity_curve
    rets = np.zeros(eq.shape[0] - 1)
    prev = eq[:-1]
    mask = prev != 0
    rets[mask] = np.diff(eq)[mask] / prev[mask]
    return Sleeve(symbol=symbol, dates=data.time, equity=eq, rets=rets,
                  n_trades=res.n_trades, swap_rate_annual=cost.swap_rate_annual,
                  res=res)


def build_sleeves(basket=PORTFOLIO_BASKET, params=FIXED_PARAMS,
                  initial_cash=None, timeframe_min=D1_MIN, min_bars=MIN_BARS,
                  cost_fn=None):
    """Build every available sleeve; return (kept, dropped). `dropped` is a list of
    (symbol, reason) so the report can state exactly what was used vs dropped.
    `cost_fn(symbol) -> CostModel` overrides the default Phase-4 cost stack."""
    initial_cash = float(initial_cash or BACKTEST.initial_cash)
    kept, dropped = [], []
    for symbol in basket:
        cost = cost_fn(symbol) if cost_fn else None
        s = _build_sleeve(symbol, params, initial_cash, timeframe_min=timeframe_min,
                          min_bars=min_bars, cost=cost)
        if s is None:
            try:
                n = len(load_ohlcv(symbol, timeframe_min, prefer_live=False))
                dropped.append((symbol, f"only {n} bars @{timeframe_min}min (< {min_bars})"))
            except FileNotFoundError:
                dropped.append((symbol, f"no cached data @{timeframe_min}min"))
        else:
            kept.append(s)
    return kept, dropped


# ─────────────────────── alignment + equal-risk weighting ────────────────────
def align_returns(sleeves, resolution="D"):
    """Align sleeve return series on their COMMON dates (intersection). Returns
    (common_dates, R) where R[t, i] is sleeve i's return on common_dates[t]. Each
    sleeve return is keyed to the bar it is realised on (dates[1:]).
    `resolution="D"` (default) keys on calendar dates — correct for D1, but it
    COLLIDES for intraday bars (6 H4 bars share a day), so intraday callers must
    pass "s" to align on exact bar timestamps."""
    per = []
    for s in sleeves:
        d = s.dates[1:]                       # rets[k] is realised on dates[k+1]
        per.append({np.datetime64(dd, resolution): r for dd, r in zip(d, s.rets)})
    common = set(per[0])
    for p in per[1:]:
        common &= set(p)
    common_dates = np.array(sorted(common), dtype=f"datetime64[{resolution}]")
    R = np.column_stack([[p[dd] for dd in common_dates] for p in per]) \
        if common_dates.size else np.empty((0, len(sleeves)))
    return common_dates, R


def inverse_vol_weights(R):
    """Naive risk parity: w_i ∝ 1/σ_i, normalised to sum to 1. By construction each
    sleeve's STANDALONE risk contribution w_i·σ_i is identical, so no instrument
    dominates. (σ_i = std of sleeve i's returns over the window.) Documented choice:
    a target-vol scheme w_i = target/σ_i is the same up to the common normaliser."""
    sig = R.std(axis=0)
    sig = np.where(sig == 0, np.nan, sig)
    inv = 1.0 / sig
    inv = np.where(np.isnan(inv), 0.0, inv)
    total = inv.sum()
    return inv / total if total > 0 else np.full(R.shape[1], 1.0 / R.shape[1])


def risk_contributions(R, w):
    """Standalone risk contribution per sleeve = w_i · σ_i (account-ccy-free)."""
    return w * R.std(axis=0)


# ───────────────────────────── combination + metrics ────────────────────────
def combine(R, w, initial_cash):
    """Portfolio return stream and equity curve from weighted sleeve returns."""
    port_rets = R @ w
    equity = initial_cash * np.concatenate([[1.0], np.cumprod(1.0 + port_rets)])
    return port_rets, equity


def series_metrics(rets, equity, n_trades, bars_per_year=BARS_PER_YEAR):
    """Headline metrics from a return stream + its equity curve (equity[0]=start).
    PF here is on the DAILY-return stream (gross up-day P&L / gross down-day P&L) —
    a portfolio-level definition, since a blended portfolio has no single 'trade'.
    Annualised return uses the number of return periods."""
    r = np.asarray(rets, dtype=float)
    n = r.size
    start, end = float(equity[0]), float(equity[-1])
    total_ret = (end / start - 1.0) * 100.0
    ann_ret = ((end / start) ** (bars_per_year / n) - 1.0) * 100.0 if n > 0 and start > 0 else 0.0
    sharpe = float(r.mean() / r.std() * np.sqrt(bars_per_year)) if r.std() > 0 else 0.0
    peak = np.maximum.accumulate(equity)
    dd = float(((equity - peak) / np.where(peak == 0, np.nan, peak)).min() * 100.0)
    gp = float(r[r > 0].sum())
    gl = float(-r[r < 0].sum())
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"total_return_pct": total_ret, "ann_return_pct": ann_ret, "sharpe": sharpe,
            "max_dd_pct": dd, "profit_factor": pf, "n_trades": int(n_trades),
            "final_equity": end, "n_periods": n}


def sleeve_metrics_on(common_dates, R, sleeves, initial_cash,
                      bars_per_year=BARS_PER_YEAR):
    """Per-sleeve metrics computed on the SAME common window (so they are directly
    comparable to the portfolio and to each other)."""
    out = {}
    for i, s in enumerate(sleeves):
        r = R[:, i]
        eq = initial_cash * np.concatenate([[1.0], np.cumprod(1.0 + r)])
        out[s.symbol] = series_metrics(r, eq, s.n_trades, bars_per_year=bars_per_year)
    return out


def correlation_matrix(R):
    """Pairwise Pearson correlation of the sleeve return streams over the window."""
    if R.shape[0] < 2:
        return np.eye(R.shape[1])
    return np.corrcoef(R, rowvar=False)


# ─────────────────────────────── full run ───────────────────────────────────
def run_portfolio(basket=PORTFOLIO_BASKET, params=FIXED_PARAMS, initial_cash=None):
    """Build sleeves, align on the common window, equal-risk weight, combine, and
    package everything the report needs."""
    initial_cash = float(initial_cash or BACKTEST.initial_cash)
    kept, dropped = build_sleeves(basket, params, initial_cash)
    if len(kept) < 2:
        return {"kept": kept, "dropped": dropped, "ok": False}

    common_dates, R = align_returns(kept)
    w = inverse_vol_weights(R)
    port_rets, port_equity = combine(R, w, initial_cash)

    port = series_metrics(port_rets, port_equity,
                          n_trades=sum(s.n_trades for s in kept))
    sleeves_m = sleeve_metrics_on(common_dates, R, kept, initial_cash)
    corr = correlation_matrix(R)
    rc = risk_contributions(R, w)

    # single-EURUSD reference, FULL history, swap-on and swap-off (continuity)
    eur = next((s for s in kept if s.symbol == "EURUSD"), None)
    eur_full = None
    if eur is not None:
        eq = eur.equity
        eur_full = series_metrics(eur.rets, eq, eur.n_trades)

    avg_sleeve_sharpe = float(np.mean([m["sharpe"] for m in sleeves_m.values()]))

    return {
        "ok": True, "kept": kept, "dropped": dropped, "params": dict(params),
        "common_dates": common_dates, "R": R, "weights": w, "risk_contrib": rc,
        "port_rets": port_rets, "port_equity": port_equity, "port": port,
        "sleeves_m": sleeves_m, "corr": corr, "initial_cash": initial_cash,
        "eur_full": eur_full, "avg_sleeve_sharpe": avg_sleeve_sharpe,
        "single_symbol": "EURUSD",
    }


# ───────────────────────────── walk-forward ─────────────────────────────────
def run_portfolio_wf(basket=PORTFOLIO_BASKET, params=FIXED_PARAMS,
                     initial_cash=None, is_bars=750, oos_bars=250,
                     timeframe_min=D1_MIN, min_bars=MIN_BARS, cost_fn=None,
                     resolution="D", bars_per_year=None, sleeves=None):
    """Walk-forward the PORTFOLIO with FIXED params. Per fold: estimate inverse-vol
    weights on the IS window, apply them to the OOS window. OOS segments are chained
    into one pooled OOS portfolio curve — the honest estimate. OOS is strictly AFTER
    IS (no peeking). Because params are fixed and uniform, this tests temporal
    stability + causal risk-weighting, NOT parameter selection. Windows default to
    ~3yr IS / ~1yr OOS (D1), shrunk proportionally if the common window is short.
    Phase-5 knobs (defaults reproduce D1 exactly): timeframe/min-bars/cost stack,
    intraday timestamp alignment, per-timeframe annualisation, and prebuilt
    `sleeves` so a caller can reuse one sleeve build across full-window + WF runs."""
    initial_cash = float(initial_cash or BACKTEST.initial_cash)
    bpy = bars_per_year or _bars_per_year(timeframe_min)
    if sleeves is not None:
        kept, dropped = sleeves, []
    else:
        kept, dropped = build_sleeves(basket, params, initial_cash,
                                      timeframe_min=timeframe_min,
                                      min_bars=min_bars, cost_fn=cost_fn)
    if len(kept) < 2:
        return {"ok": False, "kept": kept, "dropped": dropped}

    common_dates, R = align_returns(kept, resolution=resolution)
    n = R.shape[0]
    if n < is_bars + oos_bars:                  # short history -> scale windows down
        is_bars = max(2, int(n * 0.6))
        oos_bars = max(1, int(n * 0.2))
    step = oos_bars                              # OOS windows tile with no overlap

    running = initial_cash
    oos_segments, oos_ret_chunks, rows = [], [], []
    is_sharpes = []
    start = 0
    while start + is_bars + oos_bars <= n:
        is_R = R[start:start + is_bars]
        oos_R = R[start + is_bars:start + is_bars + oos_bars]
        w = inverse_vol_weights(is_R)                  # causal: IS weights only

        is_rets = is_R @ w
        is_eq = initial_cash * np.concatenate([[1.0], np.cumprod(1.0 + is_rets)])
        is_m = series_metrics(is_rets, is_eq, 0, bars_per_year=bpy)
        is_sharpes.append(is_m["sharpe"])

        oos_rets = oos_R @ w
        seg = running * np.concatenate([[1.0], np.cumprod(1.0 + oos_rets)])
        oos_segments.append(seg if not oos_segments else seg[1:])
        oos_ret_chunks.append(oos_rets)
        running = float(seg[-1])

        oos_m = series_metrics(oos_rets, seg, 0, bars_per_year=bpy)
        rows.append({
            "is_dates": (str(common_dates[start]), str(common_dates[start + is_bars - 1])),
            "oos_dates": (str(common_dates[start + is_bars]),
                          str(common_dates[start + is_bars + oos_bars - 1])),
            "is_sharpe": is_m["sharpe"], "is_ret": is_m["total_return_pct"],
            "oos_ret": oos_m["total_return_pct"],
            "oos_sharpe": oos_m["sharpe"],
        })
        start += step

    if not oos_segments:
        return {"ok": False, "kept": kept, "dropped": dropped,
                "reason": "window too short for any IS/OOS fold"}

    oos_equity = np.concatenate(oos_segments)
    oos_rets = np.concatenate(oos_ret_chunks)
    pooled = series_metrics(oos_rets, oos_equity, 0, bars_per_year=bpy)
    return {"ok": True, "kept": kept, "dropped": dropped, "rows": rows,
            "oos_equity": oos_equity, "pooled": pooled, "n": n, "is_bars": is_bars,
            "oos_bars": oos_bars, "mean_is_sharpe": float(np.mean(is_sharpes)),
            "common_dates": common_dates, "initial_cash": initial_cash}


# ───────────────────────────── reporting ────────────────────────────────────
def _pfx(x):
    return "inf" if x == float("inf") else f"{x:.3f}"


def _date(d):
    return str(np.datetime64(d, "D"))


def _verdict(res, wf):
    """Honest portfolio-vs-single verdict. NOT a forced winner — and NOT a forced
    loser: it states the diversification mechanics precisely, then judges on the
    post-swap, out-of-sample numbers."""
    port, eur = res["port"], res["sleeves_m"][res["single_symbol"]]
    div = port["sharpe"] - res["avg_sleeve_sharpe"]
    dd_gain = port["max_dd_pct"] - eur["max_dd_pct"]        # >0 = shallower than single
    lines = []
    # 1) the mechanical diversification effect (drawdown / vol), stated honestly
    if dd_gain > 0:
        lines.append(f"DIVERSIFICATION DOES what it should mechanically: portfolio maxDD "
                     f"{port['max_dd_pct']:.1f}% is shallower than single-EURUSD "
                     f"{eur['max_dd_pct']:.1f}% ({dd_gain:+.1f} pts), and sleeves are only "
                     f"weakly correlated.")
    else:
        lines.append(f"DIVERSIFICATION did not even reduce drawdown here (portfolio maxDD "
                     f"{port['max_dd_pct']:.1f}% vs single {eur['max_dd_pct']:.1f}%).")
    # 2) but Sharpe scales with the SIGN of the edge — diversification amplifies a
    #    consistent edge, and a negative edge just becomes a more consistent loss
    lines.append(f"Portfolio Sharpe {port['sharpe']:.2f} vs average single-sleeve "
                 f"{res['avg_sleeve_sharpe']:.2f} (Δ {div:+.2f}): risk-parity scales the "
                 f"Sharpe MAGNITUDE up, so when the post-swap edge is negative the loss "
                 f"only becomes more reliable, not smaller.")
    # 3) the actual judgement, on post-swap + out-of-sample numbers
    if port["ann_return_pct"] <= 0:
        lines.append(f"After conservative overnight swap the portfolio is NOT profitable on "
                     f"the common window (ann {port['ann_return_pct']:+.2f}%); financing erases "
                     f"the thin momentum edge. This momentum edge is NOT retail-tradeable as built.")
    elif wf and wf.get("ok") and wf["pooled"]["sharpe"] <= 0:
        lines.append("In-window the portfolio is marginally positive, but the pooled "
                     "OUT-OF-SAMPLE walk-forward Sharpe is <= 0 — it does not survive honestly.")
    else:
        lines.append(f"Portfolio is positive in-window (ann {port['ann_return_pct']:+.2f}%, "
                     f"Sharpe {port['sharpe']:.2f}); the pooled OOS Sharpe "
                     f"{wf['pooled']['sharpe']:.2f} is the figure to trust before believing it.")
    return "  " + "\n  ".join(lines)


def build_markdown(res, wf) -> str:
    port = res["port"]
    eur = res["sleeves_m"][res["single_symbol"]]
    cd = res["common_dates"]
    L = ["# PORTFOLIO.md — Cross-Asset Diversification of the Fixed Momentum Edge\n"]
    L.append(f"- Signal: **{FIXED_STRATEGY}**, ONE fixed param set applied UNIFORMLY to "
             f"every instrument: **lookback={res['params']['lookback']}, "
             f"anchor={res['params']['anchor']}** (the D1 plateau centre). Params are "
             f"NOT fitted per instrument — only diversification is under test.\n")
    L.append(f"- Costs: realistic spread/slippage/commission **+ conservative overnight "
             f"swap** per instrument (a documented approximation — see config.INSTRUMENT_COSTS).\n")
    L.append(f"- Common window (all kept sleeves live): **{_date(cd[0])} → {_date(cd[-1])}**, "
             f"{cd.size} D1 bars.\n")

    L.append("\n## Instruments used vs dropped\n")
    L.append("| sleeve | D1 bars | range | trades | swap %/yr | weight | risk contrib |")
    L.append("|---|--:|---|--:|--:|--:|--:|")
    rc = res["risk_contrib"]
    for i, s in enumerate(res["kept"]):
        L.append(f"| {s.symbol} | {len(s.dates)} | {_date(s.dates[0])}→{_date(s.dates[-1])} "
                 f"| {s.n_trades} | {s.swap_rate_annual*100:.1f} | {res['weights'][i]*100:.1f}% "
                 f"| {rc[i]/np.median(rc):.2f}× med |")
    if res["dropped"]:
        for sym, why in res["dropped"]:
            L.append(f"| ~~{sym}~~ | — | DROPPED: {why} | | | | |")

    L.append("\n## Portfolio vs single-EURUSD (same common window)\n")
    L.append("| metric | PORTFOLIO | single EURUSD | Δ |")
    L.append("|---|--:|--:|--:|")
    def row(label, k, fmt="{:+.2f}"):
        a, b = port[k], eur[k]
        L.append(f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {fmt.format(a-b)} |")
    row("Annualised return %", "ann_return_pct")
    row("Total return %", "total_return_pct")
    L.append(f"| Sharpe (annualised) | {port['sharpe']:.2f} | {eur['sharpe']:.2f} | {port['sharpe']-eur['sharpe']:+.2f} |")
    row("Max drawdown %", "max_dd_pct")
    L.append(f"| Profit factor (daily) | {_pfx(port['profit_factor'])} | {_pfx(eur['profit_factor'])} | |")
    L.append(f"| Trades (total) | {port['n_trades']} | {eur['n_trades']} | |")

    L.append(f"\n**Diversification effect:** portfolio Sharpe **{port['sharpe']:.2f}** vs "
             f"average single-sleeve Sharpe **{res['avg_sleeve_sharpe']:.2f}** "
             f"(Δ {port['sharpe']-res['avg_sleeve_sharpe']:+.2f}).\n")

    if res["eur_full"]:
        ef = res["eur_full"]
        L.append(f"> Continuity: single-EURUSD over its FULL history (swap ON) = "
                 f"{ef['total_return_pct']:+.1f}% total, Sharpe {ef['sharpe']:.2f}, "
                 f"maxDD {ef['max_dd_pct']:.1f}%, {ef['n_trades']} trades. The swap-OFF "
                 f"full-history number is the Phase-3 figure (+47.99%); the gap is the "
                 f"overnight-financing drag alone.\n")

    L.append("\n## Sleeve return-correlation matrix (common window)\n")
    syms = [s.symbol for s in res["kept"]]
    L.append("| | " + " | ".join(syms) + " |")
    L.append("|---" * (len(syms) + 1) + "|")
    corr = res["corr"]
    for i, si in enumerate(syms):
        L.append(f"| **{si}** | " + " | ".join(f"{corr[i,j]:+.2f}" for j in range(len(syms))) + " |")
    avg_off = (corr.sum() - np.trace(corr)) / (corr.size - len(syms)) if len(syms) > 1 else 0.0
    L.append(f"\nMean pairwise (off-diagonal) correlation: **{avg_off:+.2f}** — the lower "
             f"this is, the more genuine diversification the basket carries.\n")

    L.append("\n## Walk-forward: pooled OUT-OF-SAMPLE portfolio (the honest estimate)\n")
    if wf and wf.get("ok"):
        p = wf["pooled"]
        L.append(f"- Window: **{wf['is_bars']} IS / {wf['oos_bars']} OOS** bars, fixed params, "
                 f"inverse-vol weights re-estimated on each IS window and applied to OOS "
                 f"(OOS strictly after IS).\n")
        L.append(f"- Pooled OOS: total **{p['total_return_pct']:+.2f}%**, ann "
                 f"{p['ann_return_pct']:+.2f}%, Sharpe **{p['sharpe']:.2f}**, "
                 f"maxDD {p['max_dd_pct']:.2f}%, PF(daily) {_pfx(p['profit_factor'])}.\n")
        L.append(f"- Mean IS Sharpe {wf['mean_is_sharpe']:.2f} → pooled OOS Sharpe "
                 f"{p['sharpe']:.2f} (degradation {p['sharpe']-wf['mean_is_sharpe']:+.2f}).\n")
        L.append("\n| fold | IS window | OOS window | IS ret% | OOS ret% | OOS Sharpe |")
        L.append("|--:|---|---|--:|--:|--:|")
        for k, r in enumerate(wf["rows"]):
            L.append(f"| {k} | {_date(r['is_dates'][0])}→{_date(r['is_dates'][1])} "
                     f"| {_date(r['oos_dates'][0])}→{_date(r['oos_dates'][1])} "
                     f"| {r['is_ret']:+.2f} | {r['oos_ret']:+.2f} | {r['oos_sharpe']:.2f} |")
    else:
        L.append("- Walk-forward could not run (window too short).\n")

    L.append("\n## Verdict\n")
    L.append(_verdict(res, wf) + "\n")
    L.append("\n## Reading this\n")
    L.append("- Uniform fixed params (no per-instrument fitting) keep the multiple-testing "
             "count low BY DESIGN: only ONE strategy+param config is evaluated OOS here.\n"
             "- Overnight swap is a conservative SYMMETRIC drag (charged on either side). Real "
             "broker swaps are directional (long vs short quoted separately) and occasionally a "
             "credit — e.g. GOLD swap long −90.35 / short +11.15 pts on this account — so the "
             "true cost for a strategy that shorts is somewhat LESS than modelled here. We took "
             "the worst case on purpose: if the edge dies under it, that is a real finding.\n"
             "- Diversification can only help risk-adjusted return if sleeve returns are weakly "
             "correlated. The corr matrix above is the evidence; the pooled OOS curve is the "
             "only number never optimised on.\n")
    return "\n".join(L)


def print_summary(res, wf) -> None:
    port = res["port"]
    eur = res["sleeves_m"][res["single_symbol"]]
    bar = "=" * 78
    cd = res["common_dates"]
    print("\n" + bar)
    print(f"  CROSS-ASSET PORTFOLIO — fixed momentum {res['params']['lookback']}/"
          f"{res['params']['anchor']}  ({len(res['kept'])} sleeves, common window "
          f"{_date(cd[0])}→{_date(cd[-1])}, {cd.size} D1 bars)")
    print(bar)
    print(f"  sleeves used: {', '.join(s.symbol for s in res['kept'])}")
    if res["dropped"]:
        print(f"  dropped     : {', '.join(f'{s} ({w})' for s, w in res['dropped'])}")
    print("  " + "-" * 74)
    print(f"  {'metric':<24}{'PORTFOLIO':>16}{'single EURUSD':>16}{'Δ':>14}")
    print("  " + "-" * 74)
    print(f"  {'Annualised return %':<24}{port['ann_return_pct']:>16.2f}{eur['ann_return_pct']:>16.2f}{port['ann_return_pct']-eur['ann_return_pct']:>+14.2f}")
    print(f"  {'Sharpe (annualised)':<24}{port['sharpe']:>16.2f}{eur['sharpe']:>16.2f}{port['sharpe']-eur['sharpe']:>+14.2f}")
    print(f"  {'Max drawdown %':<24}{port['max_dd_pct']:>16.2f}{eur['max_dd_pct']:>16.2f}{port['max_dd_pct']-eur['max_dd_pct']:>+14.2f}")
    print(f"  {'Profit factor (daily)':<24}{_pfx(port['profit_factor']):>16}{_pfx(eur['profit_factor']):>16}{'':>14}")
    print(f"  {'Trades (total)':<24}{port['n_trades']:>16d}{eur['n_trades']:>16d}")
    print(bar)
    print(f"  DIVERSIFICATION: portfolio Sharpe {port['sharpe']:.2f}  vs  "
          f"avg single-sleeve Sharpe {res['avg_sleeve_sharpe']:.2f}  "
          f"(Δ {port['sharpe']-res['avg_sleeve_sharpe']:+.2f})")
    syms = [s.symbol for s in res["kept"]]
    corr = res["corr"]
    avg_off = (corr.sum() - np.trace(corr)) / (corr.size - len(syms)) if len(syms) > 1 else 0.0
    print(f"  mean pairwise sleeve correlation: {avg_off:+.2f}")
    if wf and wf.get("ok"):
        p = wf["pooled"]
        print("  " + "-" * 74)
        print(f"  POOLED OOS (walk-forward, fixed params, causal IS weights): "
              f"ret {p['total_return_pct']:+.2f}%  Sharpe {p['sharpe']:.2f}  "
              f"maxDD {p['max_dd_pct']:.2f}%")
    print(bar)
    print(_verdict(res, wf))
    print(bar)


def _log_registry(res, wf):
    """Log the portfolio backtest + WF to the results registry. Uniform fixed params
    mean only ONE distinct config is added to the multiple-testing count."""
    from registry import ResultsRegistry, multiple_testing_warning
    from backtest import cost_dict
    port, p = res["port"], (wf["pooled"] if wf and wf.get("ok") else None)
    cd = res["common_dates"]
    syms = [s.symbol for s in res["kept"]]
    params = {**res["params"], "basket": syms, "weighting": "inverse_vol_equal_risk"}
    dm = {"symbol": "PORTFOLIO[" + "+".join(syms) + "]", "timeframe": D1_MIN,
          "data_start": _date(cd[0]), "data_end": _date(cd[-1]), "n_bars": int(cd.size)}
    cd_cost = cost_dict(cost_for("EURUSD"))   # representative sleeve cost (incl swap)
    metrics_is = {"return_pct": round(port["total_return_pct"], 4),
                  "sharpe": round(port["sharpe"], 4), "max_dd_pct": round(port["max_dd_pct"], 4),
                  "profit_factor": None if port["profit_factor"] == float("inf") else round(port["profit_factor"], 4)}
    metrics_oos = None
    if p:
        metrics_oos = {"return_pct": round(p["total_return_pct"], 4), "sharpe": round(p["sharpe"], 4),
                       "max_dd_pct": round(p["max_dd_pct"], 4),
                       "profit_factor": None if p["profit_factor"] == float("inf") else round(p["profit_factor"], 4)}
    reg = ResultsRegistry()
    # The SINGLE uniform config that touched OOS — keeps the multiple-testing count low.
    rh, dup = reg.log_run("portfolio_wf", FIXED_STRATEGY, params, cd_cost, dm,
                          metrics_is=metrics_is, metrics_oos=metrics_oos,
                          oos_configs=[dict(res["params"])],
                          notes=f"portfolio {len(syms)} sleeves; OOS Sharpe "
                                f"{p['sharpe']:.2f}" if p else "portfolio in-window only")
    n, _ = reg.multiple_testing_count()
    reg.close()
    return rh, dup, n, multiple_testing_warning(n)


if __name__ == "__main__":
    res = run_portfolio()
    if not res.get("ok"):
        print("\n  [portfolio] need >= 2 sleeves with sufficient history.")
        print(f"  dropped: {res['dropped']}")
        raise SystemExit(1)
    wf = run_portfolio_wf()
    print_summary(res, wf)

    md = build_markdown(res, wf)
    out = BASE_DIR / "PORTFOLIO.md"
    out.write_text(md)
    print(f"\n  Full report written to {out}")

    try:
        rh, dup, n, warn = _log_registry(res, wf)
        print(f"  logged to results registry (hash {rh}{'  ↻ DUPLICATE' if dup else ''})")
        print("\n  " + warn.replace("\n", "\n  ") + "\n")
    except Exception as e:
        print(f"  [registry] skipped: {e}")
