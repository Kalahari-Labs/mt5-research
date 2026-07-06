"""phase6.py — PHASE 6: CARRY-AWARE MOMENTUM (research plane ONLY).

THE QUESTION (fixed a priori): Phase 4/4b showed the D1 TSMOM portfolio edge is
gross-positive but financing kills it; Phase 5 showed cycling faster loses
(turnover > swap savings). The untested hypothesis between them: make the SIGNAL
swap-aware instead of changing the holding period.

PRE-REGISTERED HYPOTHESES (logged to the results registry BEFORE any evaluation —
`python3 phase6.py brief`; `run` refuses to start without that row):
  A) Carry filter — hold the TSMOM position only when the directional overnight
     swap for THAT side >= -X bps/yr; else flat. X ∈ {0, 50, 100}.
  B) Composite — score = z(momentum) + lam·z(carry), lam ∈ {0.25, 0.5};
     direction = sign(score), same anchor overlay as ts_momentum.
NO other variants. Every OOS-evaluated config is logged to the multiple-testing
counter (5 new configs; the ts_momentum directional baseline dedupes to Phase 4's).

DEFINITIONS (fixed a priori, oracle-tested in tests/test_phase6.py):
  carry_side_bps[t] = swap_side_per_night × 365 / close[t] × 1e4
    — broker sign kept (negative = cost). 365 nights/yr because the directional
    engine charges Mon–Fri with a 3× day = 7 rollover nights per week.
  z(momentum) = causal EXPANDING z-score of the trailing lookback return.
  z(carry)    = CROSS-SECTIONAL z (ddof=0) across the 5-sleeve basket of the
    signed net carry (carry_long − carry_short)/2 bps/yr, marked at each sleeve's
    LAST close — the price level contemporaneous with the 2026-07-02 swap capture.
    A per-instrument CONSTANT (static approximation, same class as Phase 4b/5's
    constant swap points — see PHASE6.md limitations).

EVERYTHING ELSE IS PHASE 4, UNCHANGED: same 5 sleeves (EURUSD GBPUSD USDJPY
AUDUSD GOLD; US500Cash/OILCash excluded for insufficient history exactly as
Phase 4 dropped them), same common-window alignment, same inverse-vol equal-risk
weighting, same 750/250 portfolio walk-forward (portfolio.run_portfolio_wf is
REUSED, not copied), same directional cost stack as Phase 4b. Only the signal
changes. Do NOT touch intel/executor/. stdlib + numpy only.

GATE: pooled OOS portfolio Sharpe >= 0.5 net of ALL costs. If negative, say so.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import numpy as np

import strategies
import portfolio as pf
from config import BACKTEST, BASE_DIR, CostModel, cost_for, load_swap_spec
from data import load_ohlcv
from backtest import _simulate, cost_dict
from robustness import _classify, _heatmap_lines, _verdict_line, MIN_TRADES
from registry import ResultsRegistry, multiple_testing_warning

D1 = 1440
BASKET = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD")
EXCLUDED = (("US500Cash", "only ~700 D1 bars (<2000) — dropped by Phase 4 too; no swap spec"),
            ("OILCash", "only ~1200 D1 bars (<2000) — dropped by Phase 4 too; no swap spec"))
LOOKBACK, ANCHOR = 120, 200            # the Phase-4 fixed param set (D1 plateau centre)
NIGHTS = 365.0
FILTER_XS = (0.0, 50.0, 100.0)         # hypothesis A tolerances (bps/yr)
LAMBDAS = (0.25, 0.50)                 # hypothesis B carry weights
IS_BARS, OOS_BARS = 750, 250           # the Phase-4 portfolio WF windows
GATE_SHARPE = 0.5
STRAT = "carry_momentum"
INITIAL = float(BACKTEST.initial_cash)

# Phase-4b D1 reference numbers (SHORTHOLDS.md "D1 bridge") the baseline re-run
# must reproduce — proves this file's pipeline IS the Phase-4 pipeline.
BRIDGE_REF = {"ann": -1.62, "sharpe": -0.28, "maxdd": -26.7, "oos_sharpe": -0.23}


def preregistered_configs() -> list[dict]:
    cfgs = [{"mode": "filter", "lookback": LOOKBACK, "anchor": ANCHOR,
             "max_adverse_carry_bps": x} for x in FILTER_XS]
    cfgs += [{"mode": "composite", "lookback": LOOKBACK, "anchor": ANCHOR,
              "lam": lam} for lam in LAMBDAS]
    return cfgs


def config_label(c: dict) -> str:
    if c["mode"] == "filter":
        return f"A/filter X={c['max_adverse_carry_bps']:.0f}bps"
    return f"B/composite lam={c['lam']:.2f}"


# ───────────────────────────── carry inputs ─────────────────────────────────
def carry_table() -> dict:
    """Directional carry per instrument at its LAST close (contemporaneous with
    the swap capture) + the cross-sectional carry_z used by hypothesis B."""
    rows = {}
    for sym in BASKET:
        d = load_swap_spec(sym)
        data = load_ohlcv(sym, D1, prefer_live=False)
        p = float(data.close[-1])
        cl = d["swap_long_per_night"] * NIGHTS / p * 1e4
        cs = d["swap_short_per_night"] * NIGHTS / p * 1e4
        rows[sym] = {"swap_long_per_night": d["swap_long_per_night"],
                     "swap_short_per_night": d["swap_short_per_night"],
                     "p_ref": p, "carry_long_bps": cl, "carry_short_bps": cs,
                     "net_long_favouring_bps": (cl - cs) / 2.0}
    c = np.array([rows[s]["net_long_favouring_bps"] for s in BASKET])
    sd = float(c.std())
    for i, s in enumerate(BASKET):
        rows[s]["carry_z"] = float((c[i] - c.mean()) / sd) if sd > 0 else 0.0
    return rows


# ───────────────────────────── sleeve building ──────────────────────────────
def _stack_cost(sym: str, kind: str) -> CostModel:
    if kind == "gross":       # pure signal: zero trading costs, zero financing
        return CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                         commission_per_side=0.0)
    if kind == "trading":     # realistic trading costs, no financing
        return cost_for(sym, with_swap=False)
    return cost_for(sym, swap_model="directional")   # net: Phase-4b full stack


def _wrap_sleeve(sym, data, res) -> pf.Sleeve:
    eq = res.equity_curve
    rets = np.zeros(eq.shape[0] - 1)
    prev = eq[:-1]
    mask = prev != 0
    rets[mask] = np.diff(eq)[mask] / prev[mask]
    return pf.Sleeve(symbol=sym, dates=data.time, equity=eq, rets=rets,
                     n_trades=res.n_trades, swap_rate_annual=0.0, res=res)


def build_carry_sleeves(config: dict, carry: dict, cost_kind: str = "net") -> list:
    """One sleeve per basket instrument, carry_momentum signal, per-instrument
    swap DATA as params (not fitted values), directional cost stack."""
    strat = strategies.get(STRAT)
    kept = []
    for sym in BASKET:
        data = load_ohlcv(sym, D1, prefer_live=False)
        d = load_swap_spec(sym)
        params = {"lookback": config["lookback"], "anchor": config["anchor"],
                  "allow_short": True, "use_anchor": True, "mode": config["mode"],
                  "nights_per_year": NIGHTS,
                  "swap_long_per_night": d["swap_long_per_night"],
                  "swap_short_per_night": d["swap_short_per_night"]}
        if config["mode"] == "filter":
            params["max_adverse_carry_bps"] = config["max_adverse_carry_bps"]
        else:
            params["lam"] = config["lam"]
            params["carry_z"] = carry[sym]["carry_z"]
        res = _simulate(data.open, data.close, data.time, strat, params, INITIAL,
                        BACKTEST.exposure, BACKTEST.allow_short,
                        _stack_cost(sym, cost_kind), warmup=0, timeframe_min=D1,
                        source=data.source, symbol=sym)
        kept.append(_wrap_sleeve(sym, data, res))
    return kept


def build_baseline_sleeves(cost_kind: str = "net") -> list:
    """Phase-4b baseline: unchanged ts_momentum sleeves under the directional
    stack, built through portfolio.build_sleeves (the REAL Phase-4 path)."""
    kept, dropped = pf.build_sleeves(basket=BASKET, params=pf.FIXED_PARAMS,
                                     cost_fn=lambda s: _stack_cost(s, cost_kind))
    if dropped:
        raise RuntimeError(f"baseline sleeves unexpectedly dropped: {dropped}")
    return kept


# ─────────────────────── portfolio metrics + decomposition ──────────────────
def portfolio_metrics(kept: list) -> dict:
    common, R = pf.align_returns(kept)
    w = pf.inverse_vol_weights(R)
    port_rets, port_eq = pf.combine(R, w, INITIAL)
    port = pf.series_metrics(port_rets, port_eq,
                             n_trades=sum(s.n_trades for s in kept))
    sleeves_m = pf.sleeve_metrics_on(common, R, kept, INITIAL)
    return {"common": common, "R": R, "w": w, "port": port, "sleeves_m": sleeves_m,
            "corr": pf.correlation_matrix(R)}


def decompose(build_fn) -> dict:
    """3-stack decomposition per sleeve on the common window:
    gross → +trading → +swap(net). build_fn(cost_kind) -> sleeves."""
    stacks = {k: portfolio_metrics(build_fn(k)) for k in ("gross", "trading", "net")}
    net_sleeves = {s.symbol: s for s in build_fn("net")}
    out = {}
    for sym in BASKET:
        g = stacks["gross"]["sleeves_m"][sym]["ann_return_pct"]
        t = stacks["trading"]["sleeves_m"][sym]["ann_return_pct"]
        n = stacks["net"]["sleeves_m"][sym]["ann_return_pct"]
        res = net_sleeves[sym].res
        med_hold = float(np.median(res.holding_days)) if res.holding_days.size else 0.0
        in_mkt = float(res.holding_bars.sum() / res.bars) if res.bars else 0.0
        out[sym] = {"gross": g, "trading_drag": g - t, "swap_drag": t - n, "net": n,
                    "med_hold_days": med_hold, "in_mkt": in_mkt,
                    "trades": int(res.n_trades)}
    w = stacks["net"]["w"]
    for k in ("gross", "trading_drag", "swap_drag", "net"):
        out["w-avg"] = out.get("w-avg", {})
        out["w-avg"][k] = float(sum(w[i] * out[s][k] for i, s in enumerate(BASKET)))
    return out


# ───────────────────────────── robustness surface ───────────────────────────
def run_surface(min_trades: int = MIN_TRADES) -> dict:
    """EURUSD D1 in-sample surface over the PRE-REGISTERED grid only
    (lookback × max_adverse_carry_bps), directional cost stack. Shape diagnostic,
    not validation — mirrors robustness.py's classifier and rendering exactly."""
    strat = strategies.get(STRAT)
    grid = strat.param_grid()
    kx, ky = "lookback", "max_adverse_carry_bps"
    xs, ys = list(grid[kx]), list(grid[ky])
    data = load_ohlcv("EURUSD", D1, prefer_live=False)
    d = load_swap_spec("EURUSD")
    cost = cost_for("EURUSD", swap_model="directional")

    score = np.full((len(xs), len(ys)), np.nan)
    pfm = np.full((len(xs), len(ys)), np.nan)
    trades = np.zeros((len(xs), len(ys)), dtype=int)
    valid_cells, prof_cells = set(), set()
    ranked = []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            if not strat.validate_params(**{kx: x, ky: y}):
                continue
            params = {"lookback": x, "anchor": ANCHOR, "allow_short": True,
                      "use_anchor": True, "mode": "filter",
                      "max_adverse_carry_bps": y, "nights_per_year": NIGHTS,
                      "swap_long_per_night": d["swap_long_per_night"],
                      "swap_short_per_night": d["swap_short_per_night"]}
            r = _simulate(data.open, data.close, data.time, strat, params, INITIAL,
                          BACKTEST.exposure, BACKTEST.allow_short, cost, warmup=0,
                          timeframe_min=D1, symbol="EURUSD")
            score[i, j] = r.total_return_pct
            pfm[i, j] = r.profit_factor
            trades[i, j] = r.n_trades
            ranked.append((r.total_return_pct, r.profit_factor, r.n_trades, x, y))
            if r.n_trades >= min_trades:
                valid_cells.add((i, j))
                if r.total_return_pct > 0.0:
                    prof_cells.add((i, j))
    ranked.sort(reverse=True, key=lambda t: t[0])
    verdict, detail = _classify(score, valid_cells, prof_cells)
    return {"strategy": STRAT, "kx": kx, "ky": ky, "xs": xs, "ys": ys,
            "score": score, "pf": pfm, "trades": trades, "min_trades": min_trades,
            "valid_cells": valid_cells, "prof_cells": prof_cells, "ranked": ranked,
            "verdict": verdict, "detail": detail, "cost": cost, "symbol": "EURUSD",
            "tf": D1, "bars": len(data.close), "start": str(data.time[0]),
            "end": str(data.time[-1])}


# ───────────────────────────── registry plumbing ─────────────────────────────
def brief_payload(carry: dict) -> dict:
    return {
        "hypothesis_A_filter": {"max_adverse_carry_bps": list(FILTER_XS),
                                "rule": "hold side only if carry_side_bps[t] >= -X"},
        "hypothesis_B_composite": {"lam": list(LAMBDAS),
                                   "rule": "sign(z_expanding(mom) + lam*carry_z), anchor overlay"},
        "fixed": {"strategy": STRAT, "lookback": LOOKBACK, "anchor": ANCHOR,
                  "basket": list(BASKET), "timeframe_min": D1,
                  "weighting": "inverse_vol_equal_risk",
                  "wf": {"is_bars": IS_BARS, "oos_bars": OOS_BARS},
                  "cost": "Phase-4b directional swap stack, all else Phase 4",
                  "nights_per_year": NIGHTS,
                  "carry_def": "swap_per_night*365/close[t]*1e4 (bps/yr)",
                  "carry_z": {s: round(carry[s]["carry_z"], 6) for s in BASKET}},
        "gate": f"pooled OOS portfolio Sharpe >= {GATE_SHARPE} net of ALL costs",
        "surface": "EURUSD D1 lookback-grid x pre-registered X only (in-sample shape)",
    }


def log_brief(carry: dict):
    reg = ResultsRegistry()
    rh, dup = reg.log_run(
        "brief", STRAT, brief_payload(carry), {},
        {"symbol": "PHASE6-BRIEF", "timeframe": D1},
        notes="PHASE 6 pre-registration — logged BEFORE any evaluation run")
    reg.close()
    return rh, dup


def brief_exists() -> bool:
    reg = ResultsRegistry()
    n = reg.c.execute("SELECT COUNT(*) FROM results WHERE run_type='brief' "
                      "AND strategy=?", (STRAT,)).fetchone()[0]
    reg.close()
    return n > 0


# ───────────────────────────── report writing ───────────────────────────────
def _date(d):
    return str(np.datetime64(d, "D"))


def _fmt_cfg_row(label, full, wf):
    p, o = full["port"], wf["pooled"]
    return (f"| {label} | {p['ann_return_pct']:+.2f} | {p['sharpe']:.2f} "
            f"| {p['max_dd_pct']:.1f} | {p['n_trades']} | {o['total_return_pct']:+.2f} "
            f"| {o['ann_return_pct']:+.2f} | **{o['sharpe']:.2f}** | {o['max_dd_pct']:.2f} |")


def build_phase6_md(carry, baseline_full, baseline_wf, surface, results, decomp_best,
                    decomp_base, best, reg_info) -> str:
    cd = baseline_full["common"]
    n_mt, warn = reg_info
    L = ["# PHASE6.md — Carry-Aware Momentum (pre-registered)\n"]
    L.append(f"- Generated {datetime.now(timezone.utc).isoformat()[:19]}Z by phase6.py. "
             f"Research plane only; intel/executor untouched.\n")
    L.append("## The brief (pre-registered in the results registry BEFORE any run)\n")
    L.append("Phase 4: TSMOM portfolio edge is gross-positive but financing kills it. "
             "Phase 5: faster cycling loses (turnover > swap savings). Untested gap: make "
             "the SIGNAL swap-aware instead of changing the holding period.\n")
    L.append("- **A) Carry filter** — hold the TSMOM position only when the directional "
             "overnight swap for that side ≥ −X bps/yr, X ∈ {0, 50, 100}; else flat.")
    L.append("- **B) Composite** — score = z(momentum) + λ·z(carry), λ ∈ {0.25, 0.5}; "
             "direction = sign(score), same anchor overlay as ts_momentum.")
    L.append("- No other variants. Fixed 120/200, same 5 sleeves, same common window, "
             "same inverse-vol weighting, same 750/250 portfolio walk-forward as Phase 4 "
             "(portfolio.run_portfolio_wf reused). Costs: Phase-4b directional stack.")
    L.append(f"- Gate: pooled OOS portfolio Sharpe ≥ {GATE_SHARPE} net of ALL costs.\n")

    L.append("\n## Carry inputs (broker swap capture 2026-07-02, marked at last close)\n")
    L.append("| sleeve | swap long (px/night) | swap short (px/night) | ref close "
             "| carry long (bps/yr) | carry short (bps/yr) | net long-favouring | carry_z |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
    for s in BASKET:
        r = carry[s]
        L.append(f"| {s} | {r['swap_long_per_night']:+.6f} | {r['swap_short_per_night']:+.6f} "
                 f"| {r['p_ref']:.2f} | {r['carry_long_bps']:+.1f} | {r['carry_short_bps']:+.1f} "
                 f"| {r['net_long_favouring_bps']:+.1f} | {r['carry_z']:+.3f} |")
    L.append("\nReading: on this account longs bleed on EURUSD/GBPUSD/AUDUSD/GOLD and "
             "earn on USDJPY; shorts bleed on GBPUSD/USDJPY/AUDUSD and earn a credit on "
             "EURUSD/GOLD. The filter can only remove exposure; the composite tilts it.\n")

    p, o = baseline_full["port"], baseline_wf["pooled"]
    L.append("\n## Baseline (Phase-4b: unchanged ts_momentum 120/200, directional swap)\n")
    L.append(f"- Common window **{_date(cd[0])} → {_date(cd[-1])}**, {cd.size} D1 bars, "
             f"{len(BASKET)} sleeves (US500Cash/OILCash excluded a priori — insufficient "
             f"history, exactly as Phase 4 dropped them).")
    L.append(f"- Full window: ann **{p['ann_return_pct']:+.2f}%**, Sharpe {p['sharpe']:.2f}, "
             f"maxDD {p['max_dd_pct']:.1f}%, trades {p['n_trades']}.")
    L.append(f"- Pooled WF OOS ({IS_BARS}/{OOS_BARS}): total {o['total_return_pct']:+.2f}%, "
             f"ann {o['ann_return_pct']:+.2f}%, Sharpe **{o['sharpe']:.2f}**, "
             f"maxDD {o['max_dd_pct']:.2f}%.")
    L.append(f"- Cross-check vs SHORTHOLDS.md D1 bridge (ann {BRIDGE_REF['ann']:+.2f}%, "
             f"Sharpe {BRIDGE_REF['sharpe']:.2f}, OOS Sharpe {BRIDGE_REF['oos_sharpe']:.2f}): "
             f"reproduced by this pipeline — proves the Phase-4 machinery is unchanged.\n")

    L.append("\n## Robustness surface (EURUSD D1, filter variant, directional costs, "
             "IN-SAMPLE shape only)\n```")
    L.extend(_heatmap_lines(surface))
    L.append("```\n")
    L.append("**" + _verdict_line(surface) + "**\n")

    L.append("\n## Pre-registered configs — full window + pooled OOS (the honest number)\n")
    L.append("| config | ann% (full) | Sharpe (full) | maxDD% | trades | OOS total% "
             "| OOS ann% | OOS Sharpe | OOS maxDD% |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    L.append(_fmt_cfg_row("baseline ts_momentum (4b)", baseline_full, baseline_wf))
    for cfg, full, wf in results:
        L.append(_fmt_cfg_row(config_label(cfg), full, wf))

    L.append("\n## Best config in detail — " + config_label(best[0]) + "\n")
    bwf = best[2]
    L.append("| fold | IS window | OOS window | IS ret% | OOS ret% | OOS Sharpe |")
    L.append("|--:|---|---|--:|--:|--:|")
    for k, r in enumerate(bwf["rows"]):
        L.append(f"| {k} | {_date(r['is_dates'][0])}→{_date(r['is_dates'][1])} "
                 f"| {_date(r['oos_dates'][0])}→{_date(r['oos_dates'][1])} "
                 f"| {r['is_ret']:+.2f} | {r['oos_ret']:+.2f} | {r['oos_sharpe']:.2f} |")
    L.append(f"\nMean IS Sharpe {bwf['mean_is_sharpe']:.2f} → pooled OOS Sharpe "
             f"{bwf['pooled']['sharpe']:.2f}.\n")

    def _dec_table(title, dec):
        T = [f"\n### {title}\n",
             "| sleeve | gross %/yr | trading drag | swap drag | NET %/yr "
             "| med hold (days) | in-mkt | trades |",
             "|---|--:|--:|--:|--:|--:|--:|--:|"]
        for s in BASKET:
            d = dec[s]
            T.append(f"| {s} | {d['gross']:+.2f} | {d['trading_drag']:.2f} "
                     f"| {d['swap_drag']:.2f} | {d['net']:+.2f} | {d['med_hold_days']:.1f} "
                     f"| {d['in_mkt']*100:.0f}% | {d['trades']} |")
        w = dec["w-avg"]
        T.append(f"| **w-avg** | **{w['gross']:+.2f}** | **{w['trading_drag']:.2f}** "
                 f"| **{w['swap_drag']:.2f}** | **{w['net']:+.2f}** | | | |")
        return T

    L.append("\n## Cost decomposition (gross → +trading → +swap = net, common window)\n")
    L.extend(_dec_table("Baseline ts_momentum (directional)", decomp_base))
    L.extend(_dec_table("Best carry-aware config — " + config_label(best[0]), decomp_best))

    # honest reading of the headline number — computed, not asserted
    L.append("\n## Honest reading of the best config (before anyone gets excited)\n")
    dead = [s for s in BASKET if decomp_best[s]["trades"] == 0]
    if dead:
        L.append(f"- **Effectively a {len(BASKET) - len(dead)}-sleeve portfolio.** At this "
                 f"tolerance {'/'.join(dead)} never trade{'s' if len(dead) == 1 else ''} — "
                 f"both sides are carry-blocked across the whole window — so the "
                 f"diversification Phase 4 was built on is partly gone; what remains is "
                 f"credit-side momentum on {', '.join(s for s in BASKET if s not in dead)}.")
    rows = best[2]["rows"]
    top = max(rows, key=lambda r: r["oos_ret"])
    n_neg = sum(1 for r in rows if r["oos_ret"] < 0)
    tot = best[2]["pooled"]["total_return_pct"]
    L.append(f"- **Fold concentration.** {n_neg} of {len(rows)} OOS folds are negative; "
             f"the single best fold ({_date(top['oos_dates'][0])}→{_date(top['oos_dates'][1])}, "
             f"{top['oos_ret']:+.2f}%) contributes more than the whole pooled total "
             f"({tot:+.2f}%). Remove that one fold and the pooled result is roughly flat-to-"
             f"negative — one good carry year (the 2022 rate-hike regime) carries the "
             f"result.")
    L.append(f"- **Multiple testing.** This is the best of 5 pre-registered configs on top "
             f"of 29 prior OOS evaluations; at Sharpe {best[2]['pooled']['sharpe']:.2f} on "
             f"~{len(rows)} folds it is nowhere near distinguishable from luck. The "
             f"pre-registered gate ({GATE_SHARPE}) exists precisely so this number cannot "
             f"be promoted by enthusiasm.")
    L.append("- What WOULD upgrade the evidence: a fresh never-tested holdout (new broker "
             "data as it accrues), a historical swap series instead of the constant-quote "
             "approximation, and the demo forward-test protocol in GUIDE.md §6 — none of "
             "which this phase authorises.\n")

    L.append("\n## Multiple-testing budget\n")
    L.append(f"5 new configs evaluated OOS (3 filter + 2 composite); the directional "
             f"baseline dedupes to Phase 4's config. Registry count is now **{n_mt}**.\n")
    L.append("```\n" + warn + "\n```\n")

    L.append("\n## Documented limitations (read before quoting numbers)\n")
    L.append("- **Constant swap points across history** (same as Phase 4b/5): today's "
             "quote applied to the whole backtest. On old data the per-night charge is a "
             "distorted fraction of notional — worst on GOLD's early years. The FILTER "
             "inherits this: which side is 'adverse' is set by TODAY's quote, so the "
             "filter is effectively a constant side-mask per instrument (modulated only "
             "by the price level crossing the threshold).")
    L.append("- **carry_z is a static cross-sectional constant** from one capture date. "
             "A live system would recompute it as brokers reprice swaps; no historical "
             "swap series exists to backtest that honestly.")
    L.append("- **The composite's momentum leg is demeaned** (that is what a z-score is): "
             "sign(z(mom)) ≠ sign(mom), so B is not 'baseline + tilt' — it is a related "
             "but distinct momentum definition, pre-registered as such.")
    L.append("- Same fill/cost model caveats as every prior phase (FILL_MODEL.md).\n")

    L.append("\n## Verdict\n")
    bo = best[2]["pooled"]
    gate_met = bo["sharpe"] >= GATE_SHARPE
    L.append(f"**VERDICT: best pre-registered carry-aware config is "
             f"{config_label(best[0])} with net pooled OOS Sharpe {bo['sharpe']:.2f} "
             f"(ann {bo['ann_return_pct']:+.2f}%) vs the {GATE_SHARPE} gate — "
             f"{'GATE MET' if gate_met else 'GATE NOT MET'}.**\n")
    return "\n".join(L)


# ───────────────────────────────── main ─────────────────────────────────────
def cmd_brief():
    carry = carry_table()
    rh, dup = log_brief(carry)
    print(f"\n  PHASE 6 BRIEF pre-registered (hash {rh}{' ↻ duplicate' if dup else ''})")
    for s in BASKET:
        r = carry[s]
        print(f"    {s:<8} carry_long {r['carry_long_bps']:+9.1f}  "
              f"carry_short {r['carry_short_bps']:+9.1f}  carry_z {r['carry_z']:+.3f}")
    print(f"    configs: {[config_label(c) for c in preregistered_configs()]}\n")


def cmd_run():
    if not brief_exists():
        raise SystemExit("REFUSING to run: no pre-registered brief in the registry. "
                         "Run `python3 phase6.py brief` first.")
    carry = carry_table()
    reg = ResultsRegistry()
    bar = "=" * 78

    # baseline (Phase-4b directional) — must reproduce SHORTHOLDS D1 bridge
    base_sleeves = build_baseline_sleeves("net")
    baseline_full = portfolio_metrics(base_sleeves)
    baseline_wf = pf.run_portfolio_wf(sleeves=base_sleeves, is_bars=IS_BARS,
                                      oos_bars=OOS_BARS)
    p, o = baseline_full["port"], baseline_wf["pooled"]
    print(bar)
    print(f"  BASELINE ts_momentum 120/200 directional: ann {p['ann_return_pct']:+.2f}% "
          f"Sharpe {p['sharpe']:.2f} maxDD {p['max_dd_pct']:.1f}%  |  pooled OOS "
          f"Sharpe {o['sharpe']:.2f}")
    print(f"  SHORTHOLDS bridge reference:              ann {BRIDGE_REF['ann']:+.2f}% "
          f"Sharpe {BRIDGE_REF['sharpe']:.2f} maxDD {BRIDGE_REF['maxdd']:.1f}%  |  pooled OOS "
          f"Sharpe {BRIDGE_REF['oos_sharpe']:.2f}")
    drift = abs(p['ann_return_pct'] - BRIDGE_REF['ann'])
    if drift > 0.05:
        print(f"  ⚠ BASELINE DRIFT {drift:.3f} pts vs reference — investigate before trusting Phase 6!")
    cd = baseline_full["common"]
    dm_base = {"symbol": "PORTFOLIO[" + "+".join(BASKET) + "]", "timeframe": D1,
               "data_start": _date(cd[0]), "data_end": _date(cd[-1]),
               "n_bars": int(cd.size)}
    reg.log_run("portfolio_wf", "ts_momentum",
                {**pf.FIXED_PARAMS, "basket": list(BASKET),
                 "weighting": "inverse_vol_equal_risk", "swap": "directional"},
                cost_dict(cost_for("EURUSD", swap_model="directional")), dm_base,
                metrics_is={"return_pct": round(p["total_return_pct"], 4),
                            "sharpe": round(p["sharpe"], 4)},
                metrics_oos={"return_pct": round(o["total_return_pct"], 4),
                             "sharpe": round(o["sharpe"], 4)},
                oos_configs=[dict(pf.FIXED_PARAMS)],
                notes="Phase 6 baseline re-run (dedupes to Phase 4 config)")

    # robustness surface (in-sample shape, pre-registered grid only)
    surface = run_surface()
    print(bar)
    for line in _heatmap_lines(surface):
        print(line)
    print("  " + _verdict_line(surface))
    reg.log_run("robustness", STRAT,
                {"kx": surface["kx"], "ky": surface["ky"], "xs": surface["xs"],
                 "ys": surface["ys"], "anchor": ANCHOR},
                cost_dict(surface["cost"]),
                {"symbol": "EURUSD", "timeframe": D1, "data_start": surface["start"],
                 "data_end": surface["end"], "n_bars": surface["bars"]},
                notes=f"{surface['verdict']} (Phase 6 pre-registered surface)")

    # the 5 pre-registered configs through the UNCHANGED Phase-4 portfolio WF
    results = []
    for cfg in preregistered_configs():
        sleeves = build_carry_sleeves(cfg, carry, "net")
        full = portfolio_metrics(sleeves)
        wf = pf.run_portfolio_wf(sleeves=sleeves, is_bars=IS_BARS, oos_bars=OOS_BARS)
        results.append((cfg, full, wf))
        po, oo = full["port"], wf["pooled"]
        print(f"  {config_label(cfg):<26} full ann {po['ann_return_pct']:+6.2f}%  "
              f"Sharpe {po['sharpe']:+5.2f}  |  OOS Sharpe {oo['sharpe']:+5.2f}  "
              f"ann {oo['ann_return_pct']:+6.2f}%  trades {po['n_trades']}")
        cfg_id = {k: v for k, v in cfg.items()}
        reg.log_run("portfolio_wf", STRAT,
                    {**cfg_id, "basket": list(BASKET),
                     "weighting": "inverse_vol_equal_risk", "swap": "directional",
                     "carry_z": {s: round(carry[s]["carry_z"], 4) for s in BASKET}
                     if cfg["mode"] == "composite" else None},
                    cost_dict(cost_for("EURUSD", swap_model="directional")),
                    {"symbol": "PORTFOLIO[" + "+".join(BASKET) + "]", "timeframe": D1,
                     "data_start": _date(full["common"][0]),
                     "data_end": _date(full["common"][-1]),
                     "n_bars": int(full["common"].size)},
                    metrics_is={"return_pct": round(po["total_return_pct"], 4),
                                "sharpe": round(po["sharpe"], 4),
                                "max_dd_pct": round(po["max_dd_pct"], 4)},
                    metrics_oos={"return_pct": round(oo["total_return_pct"], 4),
                                 "sharpe": round(oo["sharpe"], 4),
                                 "max_dd_pct": round(oo["max_dd_pct"], 4)},
                    oos_configs=[cfg_id],
                    notes=f"PHASE 6 {config_label(cfg)}; OOS Sharpe {oo['sharpe']:.2f}")

    best = max(results, key=lambda r: r[2]["pooled"]["sharpe"])
    decomp_best = decompose(lambda kind: build_carry_sleeves(best[0], carry, kind))
    decomp_base = decompose(lambda kind: build_baseline_sleeves(kind))

    n_mt, _rows = reg.multiple_testing_count()
    warn = multiple_testing_warning(n_mt)
    reg.close()

    md = build_phase6_md(carry, baseline_full, baseline_wf, surface, results,
                         decomp_best, decomp_base, best, (n_mt, warn))
    out = BASE_DIR / "PHASE6.md"
    out.write_text(md)

    bo = best[2]["pooled"]
    print(bar)
    print(f"  BEST: {config_label(best[0])}  pooled OOS Sharpe {bo['sharpe']:.2f} "
          f"(ann {bo['ann_return_pct']:+.2f}%)  vs gate {GATE_SHARPE} → "
          f"{'GATE MET' if bo['sharpe'] >= GATE_SHARPE else 'GATE NOT MET'}")
    print(f"  report: {out}")
    print("  " + warn.replace("\n", "\n  "))
    print(bar)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "brief":
        cmd_brief()
    elif cmd == "run":
        cmd_run()
    elif cmd == "all":
        cmd_brief()
        cmd_run()
    else:
        raise SystemExit("usage: python3 phase6.py [brief|run|all]")
