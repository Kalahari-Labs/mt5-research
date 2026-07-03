"""robustness.py — parameter-robustness surface.

Instead of crowning the single best param combo (the classic overfit), this sweeps
a strategy's whole param grid over the full history (realistic costs) and reports
the SHAPE of the result surface:

  PLATEAU  — the profitable region is a broad, contiguous block of neighbouring
             params. More likely a real effect: small param changes don't break it.
  SPIKE    — the only profit is an isolated cell (or a few scattered ones), with
             losing neighbours. CURVE-FIT WARNING: it survives one lucky setting.
  NO EDGE  — nothing is profitable even in-sample over the whole set.

This is an IN-SAMPLE sensitivity scan over ALL data (no hold-out) — its job is the
shape, not validation. The walk-forward (walkforward.py) is the out-of-sample test.
A spike here is a red flag regardless of what any single combo's number says.
"""
from __future__ import annotations

import math

import numpy as np

import strategies
from config import STRATEGY, BACKTEST, REALISTIC_COSTS, BASE_DIR
from data import load_ohlcv
from backtest import _simulate, _pf, metrics_dict, cost_dict

MIN_TRADES = 20          # a cell needs at least this many trades to be trusted
PROFIT_EPS = 0.0         # "profitable" = total_return_pct > this


def _connected_components(cell_set):
    """4-neighbour connected components over a set of (i, j) grid cells."""
    seen, comps = set(), []
    for cell in cell_set:
        if cell in seen:
            continue
        stack, comp = [cell], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            i, j = x
            for nb in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if nb in cell_set and nb not in seen:
                    stack.append(nb)
        comps.append(comp)
    return comps


def run_robustness(strategy_name=None, cost=REALISTIC_COSTS, min_trades=MIN_TRADES):
    strat = strategies.get(strategy_name or STRATEGY.name)
    grid = strat.param_grid()
    keys = list(grid.keys())
    if len(keys) != 2:
        raise ValueError(f"robustness surface needs a 2-param grid; '{strat.name}' "
                         f"has {len(keys)} ({keys}). Add a 2-param strategy or grid.")
    kx, ky = keys                      # e.g. 'fast', 'slow'
    xs, ys = list(grid[kx]), list(grid[ky])

    data = load_ohlcv(STRATEGY.symbol, STRATEGY.timeframe_min)

    score = np.full((len(xs), len(ys)), np.nan)     # total return %
    pf = np.full((len(xs), len(ys)), np.nan)
    trades = np.zeros((len(xs), len(ys)), dtype=int)
    valid_cells, prof_cells = set(), set()
    ranked = []

    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            if not strat.validate_params(**{kx: x, ky: y}):
                continue
            r = _simulate(data.open, data.close, data.time, strat, {kx: x, ky: y},
                          BACKTEST.initial_cash, BACKTEST.exposure,
                          BACKTEST.allow_short, cost, warmup=0,
                          timeframe_min=STRATEGY.timeframe_min, symbol=STRATEGY.symbol)
            score[i, j] = r.total_return_pct
            pf[i, j] = r.profit_factor
            trades[i, j] = r.n_trades
            ranked.append((r.total_return_pct, r.profit_factor, r.n_trades, x, y))
            if r.n_trades >= min_trades:
                valid_cells.add((i, j))
                if r.total_return_pct > PROFIT_EPS:
                    prof_cells.add((i, j))

    ranked.sort(reverse=True, key=lambda t: t[0])

    # ---- verdict ----
    verdict, detail = _classify(score, valid_cells, prof_cells)

    return {"strategy": strat.name, "kx": kx, "ky": ky, "xs": xs, "ys": ys,
            "score": score, "pf": pf, "trades": trades, "min_trades": min_trades,
            "valid_cells": valid_cells, "prof_cells": prof_cells, "ranked": ranked,
            "verdict": verdict, "detail": detail, "cost": cost,
            "symbol": STRATEGY.symbol, "tf": STRATEGY.timeframe_min,
            "bars": len(data.close), "start": str(data.time[0]), "end": str(data.time[-1])}


def _classify(score, valid_cells, prof_cells):
    n_valid = len(valid_cells)
    if n_valid == 0:
        return "NO EDGE", {"reason": "no param combo produced enough trades to trust"}

    best_ij = max(valid_cells, key=lambda ij: score[ij])
    best_score = float(score[best_ij])
    frac_prof = len(prof_cells) / n_valid

    if best_score <= PROFIT_EPS or not prof_cells:
        return "NO EDGE", {"reason": "nothing profitable in-sample over the full set",
                           "best_score": best_score, "frac_prof": frac_prof,
                           "n_valid": n_valid, "best_ij": best_ij}

    comps = _connected_components(prof_cells)
    largest = max(comps, key=len)
    largest_size = len(largest)
    best_in_largest = best_ij in largest
    plateau_size = max(3, math.ceil(0.20 * n_valid))

    if best_in_largest and largest_size >= plateau_size and frac_prof >= 0.25:
        verdict = "PLATEAU"
    else:
        verdict = "SPIKE"

    return verdict, {"best_score": best_score, "frac_prof": frac_prof,
                     "n_valid": n_valid, "best_ij": best_ij,
                     "largest_component": largest_size, "plateau_size_threshold": plateau_size,
                     "best_in_largest": best_in_largest, "n_profitable": len(prof_cells)}


# ---- rendering ----
def _cell_char(res, i, j):
    if (i, j) == res["detail"].get("best_ij"):
        return "@"
    s = res["score"][i, j]
    if np.isnan(s):
        return " "                      # invalid (fast>=slow)
    if res["trades"][i, j] < res["min_trades"]:
        return "~"                      # too few trades to trust
    if s >= 2:
        return "#"
    if s > 0:
        return "+"
    if s > -2:
        return "-"
    return "."


def _heatmap_lines(res):
    xs, ys = res["xs"], res["ys"]
    L = [f"  rows = {res['kx']}  (top→bottom)   cols = {res['ky']}  (left→right)"]
    header = "        " + "".join(f"{y:>4}" for y in ys)
    L.append(header)
    for i, x in enumerate(xs):
        row = "".join(f"   {_cell_char(res, i, j)}" for j in range(len(ys)))
        L.append(f"  {x:>5} {row}")
    L.append("")
    L.append("  legend: @ best   # >=+2%   + 0..2%   - -2..0%   . <-2%   ~ <min-trades   (blank) invalid")
    return L


def _verdict_line(res):
    v, d = res["verdict"], res["detail"]
    if v == "NO EDGE":
        return f"VERDICT: NO EDGE — {d.get('reason','')}."
    bx = res["xs"][d["best_ij"][0]]
    by = res["ys"][d["best_ij"][1]]
    base = (f"VERDICT: {v} — best {res['kx']}/{res['ky']} = {bx}/{by} "
            f"@ {d['best_score']:+.2f}%; {d['n_profitable']}/{d['n_valid']} cells profitable "
            f"({d['frac_prof']*100:.0f}%), largest contiguous profitable block = "
            f"{d['largest_component']} cells.")
    if v == "SPIKE":
        base += "  ⚠ CURVE-FIT WARNING: profit is isolated, not a broad plateau."
    else:
        base += "  Broad plateau — more likely a real effect than a fit artefact."
    return base


def print_robustness(res):
    bar = "=" * 72
    print("\n" + bar)
    print(f"  PARAMETER ROBUSTNESS — {res['strategy']}  {res['symbol']} "
          f"H{res['tf'] // 60}  ({res['bars']} bars, realistic costs)")
    print(f"  metric per cell = total return %   (in-sample over ALL data; not OOS)")
    print(bar)
    for line in _heatmap_lines(res):
        print(line)
    print(bar)
    print("  Top combos:")
    print(f"  {'rank':<5}{res['kx']+'/'+res['ky']:<10}{'return%':>10}{'PF':>8}{'trades':>8}")
    for n, (ret, p, tr, x, y) in enumerate(res["ranked"][:8], 1):
        print(f"  {n:<5}{f'{x}/{y}':<10}{ret:>+10.2f}{_pf(p):>8}{tr:>8}")
    print(bar)
    print("  " + _verdict_line(res))
    print(bar)


def build_markdown(res) -> str:
    L = ["# ROBUSTNESS.md — Parameter-Robustness Surface\n",
         f"- Strategy: **{res['strategy']}**, {res['symbol']} H{res['tf'] // 60}, "
         f"{res['bars']} bars ({res['start'][:10]}→{res['end'][:10]})",
         f"- Metric per cell: **total return %**, realistic costs, IN-SAMPLE over all data",
         f"- Grid: {res['kx']} {res['xs']} × {res['ky']} {res['ys']}",
         f"- Trust threshold: ≥ {res['min_trades']} trades per cell\n",
         "## Surface (text heatmap)\n```",
         *(_heatmap_lines(res)),
         "```\n",
         "## Top combos\n",
         f"| rank | {res['kx']}/{res['ky']} | return % | PF | trades |",
         "|--:|---|--:|--:|--:|"]
    for n, (ret, p, tr, x, y) in enumerate(res["ranked"][:10], 1):
        L.append(f"| {n} | {x}/{y} | {ret:+.2f} | {_pf(p)} | {tr} |")
    L.append("\n## Verdict\n")
    L.append("**" + _verdict_line(res) + "**\n")
    L.append("## Reading this\n")
    L.append("- A **SPIKE** means the strategy only 'works' at one lucky parameter setting and "
             "loses at its neighbours — almost always curve-fitting. Do NOT trust it.\n"
             "- A **PLATEAU** (broad contiguous profitable block) is necessary-but-not-sufficient "
             "evidence of a real effect; still confirm out-of-sample (walkforward.py).\n"
             "- **NO EDGE** is the honest, common result — most simple ideas have none after costs.\n"
             "- This sweep is in-sample over the whole set; it measures parameter sensitivity, not "
             "out-of-sample performance.\n")
    return "\n".join(L)


if __name__ == "__main__":
    res = run_robustness()
    print_robustness(res)
    out = BASE_DIR / "ROBUSTNESS.md"
    out.write_text(build_markdown(res))
    print(f"\n  Full surface written to {out}")

    try:
        from registry import ResultsRegistry
        d = res["detail"]
        best = (f"{res['xs'][d['best_ij'][0]]}/{res['ys'][d['best_ij'][1]]}"
                if "best_ij" in d else "n/a")
        reg = ResultsRegistry()
        dm = {"symbol": res["symbol"], "timeframe": res["tf"],
              "data_start": res["start"], "data_end": res["end"], "n_bars": res["bars"]}
        grid_spec = {"kx": res["kx"], "ky": res["ky"], "xs": res["xs"], "ys": res["ys"]}
        rh, dup = reg.log_run("robustness", res["strategy"], grid_spec,
                              cost_dict(res["cost"]), dm,
                              notes=f"{res['verdict']} best {best}")
        reg.close()
        print(f"  logged to results registry (hash {rh}"
              f"{'  ↻ DUPLICATE of a prior run' if dup else ''})\n")
    except Exception as e:
        print(f"  [registry] skipped: {e}")
