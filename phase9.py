"""phase9.py — PHASE 9: NEVER-TESTED-SYMBOLS HOLDOUT (research plane ONLY).

THE QUESTION (fixed a priori): PHASE6.md says what would upgrade its evidence —
"a fresh never-tested holdout". Only ~1 week of new TIME has accrued since the
data window closed, but fresh data also exists in the CROSS-SECTION: liquid
symbols this repo has never evaluated against out-of-sample data. Phase 9 takes
the ONE surviving Phase-6 config and its Phase-4b control to that holdout,
exactly as pre-registered, pass or fail.

PRE-REGISTERED DESIGN (logged BEFORE any holdout bar is fetched —
`python3 phase9.py brief`; `run` refuses without it):

  candidates : NZDUSD USDCAD USDCHF EURGBP EURJPY GBPJPY — fixed list, chosen
               blind for liquidity; none has ever appeared in any OOS
               evaluation (asserted against the ever-tested set at runtime).
  screen     : MECHANICAL, no discretion — (1) broker serves >= 2000 D1 bars
               (portfolio.MIN_BARS, the same rule that dropped US500Cash/
               OILCash in Phase 4); (2) swap_mode == 1 (points — the only mode
               the engine implements); (3) never OOS-tested. Survivors form
               the holdout basket; fewer than 3 survivors = NOT FEASIBLE
               verdict, no evaluation.
  configs    : EXACTLY TWO, no variants, params frozen from Phases 4/6:
               (1) carry_momentum A/filter X=0bps, lookback 120 / anchor 200
                   (the Phase-6 survivor);
               (2) ts_momentum 120/200 (the Phase-4b baseline, as control).
  costs      : mechanical from the broker's own data, no tuned numbers:
               pip_size = 10 x point; spread_pips = median of the broker's
               per-bar spread over the ENTIRE fetched D1 history (empirical —
               Phase 8 proved the legacy assumptions understate real spreads,
               and understating costs would bias TOWARD passing the gate);
               slippage 0.3 pips + commission 3.5/lot (the basket's FX class);
               contract size + directional per-side swap from the live broker
               spec (constant-quote carry, same documented limitation as
               Phase 6). swap capture is also recorded into the Phase-7
               series (data/swap_history.csv).
  portfolio  : inverse-vol equal-risk weights, 750/250 walk-forward —
               portfolio.run_portfolio_wf REUSED, not copied (windows shrink
               proportionally on short common windows, its documented rule).
  gate       : pooled OOS portfolio Sharpe >= 0.5 net of ALL costs, for the
               carry config. The baseline is the control, not a second chance.
  counting   : exactly 2 configs enter the multiple-testing counter (asserted
               at runtime: after - before == 2).

stdlib + numpy only. Do NOT touch intel/executor/ (the bridge is read over
HTTP, same as any client).
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import portfolio as pf
import strategies
import swapseries as ss
from backtest import _simulate, cost_dict
from config import BACKTEST, DATA_DIR, CostModel
from data import load_ohlcv
from phase6 import (BASKET as PHASE6_BASKET, D1, NIGHTS, IS_BARS, OOS_BARS,
                    LOOKBACK, ANCHOR, GATE_SHARPE, _wrap_sleeve, _date,
                    portfolio_metrics)
from registry import ResultsRegistry, multiple_testing_warning

PHASE = 9
DOC = "PHASE9.md"
CANDIDATES = ("NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURJPY", "GBPJPY")
EVER_TESTED = tuple(PHASE6_BASKET)      # every symbol any OOS run ever used
MIN_SLEEVES = 3
FILTER_X = 0.0
SLIPPAGE_PIPS = 0.3                     # the basket's FX class (config.py)
COMMISSION_PER_LOT = 3.5
BRIEF_STRAT = "holdout9"
INITIAL = float(BACKTEST.initial_cash)


# ───────────────────────────── brief ─────────────────────────────────────────
def brief_payload() -> dict:
    return {
        "phase": PHASE,
        "kind": "cross-sectional holdout — fresh, never-tested symbols",
        "candidates": list(CANDIDATES),
        "screen": [f"broker serves >= {pf.MIN_BARS} D1 bars",
                   "swap_mode == 1 (points)",
                   "never OOS-evaluated before (asserted vs the ever-tested set)",
                   f"fewer than {MIN_SLEEVES} survivors -> NOT FEASIBLE, no evaluation"],
        "configs": [
            {"strategy": "carry_momentum", "mode": "filter",
             "max_adverse_carry_bps": FILTER_X, "lookback": LOOKBACK,
             "anchor": ANCHOR, "why": "the ONE Phase-6 survivor"},
            {"strategy": "ts_momentum", "lookback": LOOKBACK, "anchor": ANCHOR,
             "why": "Phase-4b baseline, control"},
        ],
        "costs": {"pip_size": "10 x broker point",
                  "spread_pips": "median per-bar broker spread over full fetched "
                                 "D1 history (empirical — Phase 8 showed legacy "
                                 "assumptions understate; understating biases "
                                 "toward passing)",
                  "slippage_pips": SLIPPAGE_PIPS,
                  "commission_per_lot": COMMISSION_PER_LOT,
                  "swap": "directional per-side from live broker spec, constant "
                          "quote (Phase-6 limitation inherited + documented)"},
        "portfolio": {"weighting": "inverse_vol_equal_risk",
                      "wf": {"is_bars": IS_BARS, "oos_bars": OOS_BARS},
                      "engine": "portfolio.run_portfolio_wf REUSED"},
        "gate": f"pooled OOS portfolio Sharpe >= {GATE_SHARPE} net of ALL costs "
                f"for the carry config; baseline is control",
        "multiple_testing": "exactly +2 configs (asserted at runtime)",
    }


def log_brief():
    reg = ResultsRegistry()
    rh, dup = reg.log_run("brief", BRIEF_STRAT, brief_payload(), {},
                          {"symbol": "PHASE9-BRIEF", "timeframe": D1},
                          notes="PHASE 9 pre-registration — logged BEFORE any "
                                "holdout bar was fetched")
    reg.close()
    return rh, dup


def brief_exists() -> bool:
    reg = ResultsRegistry()
    n = reg.c.execute("SELECT COUNT(*) FROM results WHERE run_type='brief' "
                      "AND strategy=?", (BRIEF_STRAT,)).fetchone()[0]
    reg.close()
    return n > 0


# ───────────────────────────── data acquisition ──────────────────────────────
def _get_json(url: str, timeout: float = 20.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def write_bars_csv(path, bars) -> int:
    """Bars from the bridge ([epoch, o, h, l, c, tick_volume, spread_points])
    into the exact CSV shape data._load_csv reads (mt5_dump.py's format)."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close",
                    "tick_volume", "spread_points"])
        for b in bars:
            ts = datetime.fromtimestamp(int(b[0]), tz=timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"),
                        b[1], b[2], b[3], b[4], b[5], b[6]])
    return len(bars)


def fetch_symbol(sym: str, base_url: str | None = None) -> dict:
    """Pull D1 history + live spec for one candidate from the bridge and land
    them in the research data cache in the formats the loaders already read.
    Returns {bars, median_spread_points, spec} — errors raise, the caller
    reports them as screen failures."""
    base = (base_url or ss.DEFAULT_BRIDGE).rstrip("/")
    spec = _get_json(f"{base}/symbol?name={sym}")
    if "error" in spec:
        raise ValueError(spec["error"])
    # MT5 returns None (bridge: 502) when `count` exceeds the local history it
    # holds, and it does NOT backfill never-charted symbols on API demand — that
    # download only happens when the symbol's chart is opened in the terminal
    # once. So: one gentle descending ladder, no retries (the bridge is single-
    # threaded and also serves the live engine); the deepest count that answers
    # is what the terminal truly has.
    bars = None
    for count in (8000, 5000, 3000, pf.MIN_BARS):
        try:
            got = _get_json(f"{base}/bars?symbol={sym}&tf=D1&count={count}")
        except urllib.error.HTTPError:
            continue
        if isinstance(got, list):
            bars = got
            break
    if bars is None:
        raise ValueError(
            f"terminal serves < {pf.MIN_BARS} D1 bars (deep history not "
            f"downloaded — open {sym}'s chart in the MT5 terminal once, let it "
            f"sync, then re-run `python3 phase9.py run`)")
    write_bars_csv(Path(DATA_DIR) / f"{sym}_1440.csv", bars)
    keep = ("name", "digits", "point", "volume_min", "volume_max", "volume_step",
            "trade_contract_size", "trade_tick_value", "trade_tick_size",
            "currency_profit", "currency_margin", "description")
    (Path(DATA_DIR) / f"{sym}_symbol.json").write_text(
        json.dumps({k: spec.get(k) for k in keep}, indent=2))
    cap = {"name": sym, "swap_long": spec["swap_long"],
           "swap_short": spec["swap_short"], "swap_mode": spec["swap_mode"],
           "swap_rollover3days": spec["swap_rollover3days"],
           "point": spec["point"], "digits": spec["digits"],
           "trade_contract_size": spec["trade_contract_size"],
           "captured_utc": datetime.now(timezone.utc).isoformat()}
    (Path(DATA_DIR) / f"{sym}_swap.json").write_text(json.dumps(cap, indent=2))
    ss.record([ss.row_from_spec(spec, sym, source="bridge",
                                ref_price=spec.get("bid"))])
    return {"bars": bars, "spec": spec,
            "median_spread_points": float(np.median([b[6] for b in bars]))}


# ───────────────────────────── screen + costs ────────────────────────────────
def screen(sym: str, fetched: dict | None, err: str | None):
    """(passed, reason) — mechanical, pre-registered, no discretion."""
    if sym in EVER_TESTED:
        return False, "already OOS-evaluated — not a holdout symbol"
    if err is not None:
        return False, f"fetch failed: {err}"
    n = len(fetched["bars"])
    if n < pf.MIN_BARS:
        return False, f"only {n} D1 bars (< {pf.MIN_BARS})"
    if fetched["spec"].get("swap_mode") != 1:
        return False, f"swap_mode={fetched['spec'].get('swap_mode')} (need 1)"
    return True, f"{n} D1 bars, swap_mode 1"


def holdout_cost(sym: str, fetched: dict) -> CostModel:
    """The pre-registered mechanical cost rule. No tuned numbers anywhere."""
    spec = fetched["spec"]
    point = float(spec["point"])
    pip = 10.0 * point
    from config import load_swap_spec
    d = load_swap_spec(sym)                       # reads the json we just wrote
    return CostModel(
        pip_size=pip,
        spread_pips=fetched["median_spread_points"] * point / pip,
        slippage_pips=SLIPPAGE_PIPS,
        commission_per_lot=COMMISSION_PER_LOT,
        contract_size=float(spec["trade_contract_size"]),
        fill_timing="next_open", commission_per_side=0.0,
        swap_rate_annual=0.0, swap_model="directional",
        swap_long_per_night=d["swap_long_per_night"],
        swap_short_per_night=d["swap_short_per_night"],
        swap_triple_weekday=d["swap_triple_weekday"])


# ───────────────────────────── sleeves ───────────────────────────────────────
def build_sleeves(cfg_name: str, basket, costs) -> list:
    """Holdout sleeves through the UNCHANGED _simulate engine. cfg_name is one
    of the two pre-registered configs; anything else is refused."""
    from config import load_swap_spec
    if cfg_name not in ("carry_filter", "ts_baseline"):
        raise ValueError(f"not a pre-registered config: {cfg_name}")
    kept = []
    for sym in basket:
        data = load_ohlcv(sym, D1, prefer_live=False)
        if cfg_name == "carry_filter":
            strat = strategies.get("carry_momentum")
            d = load_swap_spec(sym)
            params = {"lookback": LOOKBACK, "anchor": ANCHOR, "allow_short": True,
                      "use_anchor": True, "mode": "filter",
                      "max_adverse_carry_bps": FILTER_X,
                      "nights_per_year": NIGHTS,
                      "swap_long_per_night": d["swap_long_per_night"],
                      "swap_short_per_night": d["swap_short_per_night"]}
        elif cfg_name == "ts_baseline":
            strat = strategies.get("ts_momentum")
            params = dict(pf.FIXED_PARAMS)
        else:
            raise ValueError(f"not a pre-registered config: {cfg_name}")
        res = _simulate(data.open, data.close, data.time, strat, params, INITIAL,
                        BACKTEST.exposure, BACKTEST.allow_short, costs[sym],
                        warmup=0, timeframe_min=D1, source=data.source,
                        symbol=sym)
        kept.append(_wrap_sleeve(sym, data, res))
    return kept


# ───────────────────────────── doc ───────────────────────────────────────────
def render_doc(screen_rows, basket, costs_info, results, warn,
               not_feasible=False) -> str:
    b = brief_payload()
    L = ["# PHASE9.md — Never-Tested-Symbols Holdout (pre-registered)\n"]
    L.append(f"- Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
             f"by phase9.py. Research plane only; the bridge is read over HTTP; "
             f"intel/executor untouched.\n")
    L.append("## The brief (pre-registered BEFORE any holdout bar was fetched)\n")
    L.append(f"- Candidates (fixed, chosen blind): {', '.join(CANDIDATES)}.")
    L.append(f"- Screen (mechanical): {' · '.join(b['screen'][:3])}.")
    L.append(f"- Exactly two configs: the Phase-6 survivor (carry filter X=0bps, "
             f"{LOOKBACK}/{ANCHOR}) and the Phase-4b ts_momentum control.")
    L.append(f"- Costs: {b['costs']['spread_pips']}; slippage {SLIPPAGE_PIPS} pips; "
             f"commission {COMMISSION_PER_LOT}/lot; directional swap from live spec.")
    L.append(f"- Gate: {b['gate']}.\n")
    L.append("## Screen results\n")
    L.append("| candidate | verdict | detail |")
    L.append("|---|---|---|")
    for sym, ok, reason in screen_rows:
        L.append(f"| {sym} | {'PASS' if ok else 'fail'} | {reason} |")
    L.append("")
    if not_feasible:
        L.append("## What unblocks this phase\n")
        L.append("The MT5 terminal only downloads deep history for a symbol when "
                 "its chart has been opened once (that is how the basket symbols "
                 "got theirs at install time); it refuses API-triggered backfill "
                 "for never-charted symbols. One-time human step: open a D1 "
                 "chart for each candidate in the terminal, let it sync (~seconds "
                 "per symbol), then re-run `python3 phase9.py run`. The brief "
                 "stays pre-registered and unchanged; nothing about the design "
                 "may be edited between now and that run.\n")
        L.append("## Verdict\n")
        L.append(f"**VERDICT: fewer than {MIN_SLEEVES} candidates survived the "
                 f"mechanical screen — holdout NOT FEASIBLE on this broker "
                 f"today (terminal serves insufficient history for never-"
                 f"charted symbols); no evaluation performed; multiple-testing "
                 f"count unchanged.**")
        return "\n".join(L) + "\n"
    L.append(f"## Holdout basket + empirical costs ({len(basket)} sleeves)\n")
    L.append("| sleeve | D1 bars | median spread (pips) | swap long (px/night) | "
             "swap short (px/night) |")
    L.append("|---|--:|--:|--:|--:|")
    for sym in basket:
        ci = costs_info[sym]
        L.append(f"| {sym} | {ci['bars']} | {ci['spread_pips']:.1f} | "
                 f"{ci['swap_long']:+.6f} | {ci['swap_short']:+.6f} |")
    L.append("")
    L.append("## Results — full window + pooled OOS (the honest number)\n")
    L.append("| config | ann% (full) | Sharpe (full) | maxDD% | trades | "
             "OOS total% | OOS ann% | OOS Sharpe | OOS maxDD% |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for label, full, wf in results:
        p, o = full["port"], wf["pooled"]
        L.append(f"| {label} | {p['ann_return_pct']:+.2f} | {p['sharpe']:.2f} | "
                 f"{p['max_dd_pct']:.1f} | {p['n_trades']} | "
                 f"{o['total_return_pct']:+.2f} | {o['ann_return_pct']:+.2f} | "
                 f"**{o['sharpe']:.2f}** | {o['max_dd_pct']:.2f} |")
    L.append("")
    carry_label, carry_full, carry_wf = results[0]
    L.append(f"## OOS folds — {carry_label}\n")
    L.append("| fold | IS window | OOS window | IS ret% | OOS ret% | OOS Sharpe |")
    L.append("|--:|---|---|--:|--:|--:|")
    for i, r in enumerate(carry_wf["rows"], 1):
        L.append(f"| {i} | {_date(r['is_dates'][0])}→{_date(r['is_dates'][1])} "
                 f"| {_date(r['oos_dates'][0])}→{_date(r['oos_dates'][1])} "
                 f"| {r['is_ret']:+.2f} | {r['oos_ret']:+.2f} "
                 f"| {r['oos_sharpe']:.2f} |")
    L.append("")
    L.append("## Documented limitations (read before quoting numbers)\n")
    L.append("- **Constant swap quote across history** — same class of "
             "approximation as Phases 4b/5/6; the Phase-7 series will fix this "
             "for future runs, not for this one.")
    L.append("- **Empirical spreads here vs assumed spreads in Phases 4-6**: the "
             "holdout uses the broker's own median per-bar spread (Phase 8 showed "
             "the legacy assumptions understate real costs ~2x). Holdout costs "
             "are therefore HARSHER and not directly comparable to basket "
             "numbers — by design; understating costs would bias toward passing.")
    L.append("- Single broker, single capture date for swaps; same fill-model "
             "caveats as every prior phase (FILL_MODEL.md).\n")
    L.append("## Multiple-testing budget\n")
    L.append("Exactly 2 configs added (pre-registered; asserted at runtime).\n")
    L.append("```")
    L.append(warn)
    L.append("```\n")
    L.append("## Verdict\n")
    o = carry_wf["pooled"]
    met = o["sharpe"] >= GATE_SHARPE
    L.append(f"**VERDICT: the Phase-6 survivor (carry filter X=0bps) on the "
             f"never-tested holdout basket [{'+'.join(basket)}] scores pooled OOS "
             f"Sharpe {o['sharpe']:.2f} (ann {o['ann_return_pct']:+.2f}%) vs the "
             f"{GATE_SHARPE} gate — GATE {'MET' if met else 'NOT MET'}.**")
    return "\n".join(L) + "\n"


# ───────────────────────────── commands ──────────────────────────────────────
def cmd_brief() -> None:
    rh, dup = log_brief()
    print(f"brief logged (hash {rh}){' — duplicate, already registered' if dup else ''}")


def cmd_run() -> None:
    if not brief_exists():
        raise SystemExit("REFUSING to run: no pre-registered brief in the registry. "
                         "Run `python3 phase9.py brief` first.")
    reg = ResultsRegistry()
    n_before, _ = reg.multiple_testing_count()
    reg.close()
    bar = "=" * 78

    # fetch + mechanical screen
    screen_rows, fetched = [], {}
    for sym in CANDIDATES:
        got, err = None, None
        try:
            got = fetch_symbol(sym)
        except Exception as e:  # noqa: BLE001 — a failed fetch is a screen fail
            err = f"{e.__class__.__name__}: {e}"
        ok, reason = screen(sym, got, err)
        screen_rows.append((sym, ok, reason))
        if ok:
            fetched[sym] = got
        print(f"  screen {sym:<8} {'PASS' if ok else 'fail':<5} {reason}")
    basket = tuple(s for s, ok, _ in screen_rows if ok)

    if len(basket) < MIN_SLEEVES:
        doc = render_doc(screen_rows, basket, {}, [], "", not_feasible=True)
        with open(DOC, "w") as f:
            f.write(doc)
        reg = ResultsRegistry()
        reg.log_run("phase9_screen", BRIEF_STRAT,
                    {"survivors": list(basket)}, {},
                    {"symbol": "PHASE9", "timeframe": D1},
                    notes="holdout NOT FEASIBLE — screen left < 3 sleeves")
        reg.close()
        print(f"NOT FEASIBLE: {len(basket)} survivor(s) < {MIN_SLEEVES}; "
              f"wrote {DOC}")
        return

    costs = {s: holdout_cost(s, fetched[s]) for s in basket}
    costs_info = {s: {"bars": len(fetched[s]["bars"]),
                      "spread_pips": costs[s].spread_pips,
                      "swap_long": costs[s].swap_long_per_night,
                      "swap_short": costs[s].swap_short_per_night}
                  for s in basket}

    # the two pre-registered configs — nothing else
    reg = ResultsRegistry()
    results = []
    for cfg_name, strat_name, label, cfg_id in (
            ("carry_filter", "carry_momentum",
             f"carry filter X=0bps ({LOOKBACK}/{ANCHOR})",
             {"mode": "filter", "max_adverse_carry_bps": FILTER_X,
              "lookback": LOOKBACK, "anchor": ANCHOR,
              "basket": "HOLDOUT9[" + "+".join(basket) + "]"}),
            ("ts_baseline", "ts_momentum",
             f"ts_momentum baseline ({LOOKBACK}/{ANCHOR})",
             {**pf.FIXED_PARAMS,
              "basket": "HOLDOUT9[" + "+".join(basket) + "]"})):
        sleeves = build_sleeves(cfg_name, basket, costs)
        full = portfolio_metrics(sleeves)
        wf = pf.run_portfolio_wf(sleeves=sleeves, is_bars=IS_BARS,
                                 oos_bars=OOS_BARS)
        if not wf.get("ok"):
            raise SystemExit(f"walk-forward failed for {label}: "
                             f"{wf.get('reason')}")
        results.append((label, full, wf))
        p, o = full["port"], wf["pooled"]
        print(bar)
        print(f"  {label:<34} full ann {p['ann_return_pct']:+6.2f}%  Sharpe "
              f"{p['sharpe']:+5.2f}  |  OOS Sharpe {o['sharpe']:+5.2f}  "
              f"ann {o['ann_return_pct']:+6.2f}%  trades {p['n_trades']}")
        cd = full["common"]
        reg.log_run("portfolio_wf", strat_name, cfg_id,
                    cost_dict(costs[basket[0]]),
                    {"symbol": "HOLDOUT9[" + "+".join(basket) + "]",
                     "timeframe": D1, "data_start": _date(cd[0]),
                     "data_end": _date(cd[-1]), "n_bars": int(cd.size)},
                    metrics_is={"return_pct": round(p["total_return_pct"], 4),
                                "sharpe": round(p["sharpe"], 4),
                                "max_dd_pct": round(p["max_dd_pct"], 4)},
                    metrics_oos={"return_pct": round(o["total_return_pct"], 4),
                                 "sharpe": round(o["sharpe"], 4),
                                 "max_dd_pct": round(o["max_dd_pct"], 4)},
                    oos_configs=[cfg_id],
                    notes="Phase 9 never-tested-symbols holdout")
    n_after, _ = reg.multiple_testing_count()
    warn = multiple_testing_warning(n_after)
    reg.close()
    if n_after - n_before != 2:
        raise SystemExit(f"MULTIPLE-TESTING VIOLATION: counter moved "
                         f"{n_before} -> {n_after}, pre-registered +2 exactly")

    with open(DOC, "w") as f:
        f.write(render_doc(screen_rows, basket, costs_info, results, warn))
    o = results[0][2]["pooled"]
    print(bar)
    print(f"  VERDICT: holdout pooled OOS Sharpe {o['sharpe']:.2f} vs gate "
          f"{GATE_SHARPE} — GATE {'MET' if o['sharpe'] >= GATE_SHARPE else 'NOT MET'}")
    print(f"  wrote {DOC}; multiple-testing count {n_before} -> {n_after}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "brief":
        cmd_brief()
    elif cmd == "run":
        cmd_run()
    elif cmd == "all":
        cmd_brief()
        cmd_run()
    else:
        raise SystemExit("usage: python3 phase9.py [brief|run|all]")
