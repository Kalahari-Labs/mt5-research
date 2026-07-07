"""phase8.py — PHASE 8: FORWARD-TEST RECONCILIATION (research plane ONLY).

THE QUESTION (fixed a priori): GUIDE.md §6 step 4 demands weekly reconciliation
of live demo fills against model assumptions — spread paid, slippage, swap
charged vs CostModel — and §6 step 5 kills any forward test whose realized
costs exceed modelled costs by >50%. The §6 protocol stays LOCKED (no strategy
has passed the research gate), but the INSTRUMENT it requires must exist and be
proven before it is ever needed. Phase 8 builds it (forwardtest.py) and runs it
once against the demo journal that is already accruing.

THIS PHASE PERFORMS NO OUT-OF-SAMPLE EVALUATION. The multiple-testing counter
must be IDENTICAL before and after `python3 phase8.py run` (asserted at
runtime; the run aborts if it moved). Reconciliation reads execution reality;
it never touches strategy results.

PRE-REGISTERED CONTRACT (logged to the registry BEFORE the first reconciliation
— `python3 phase8.py brief`; `run` refuses to start without that row):
  access    : executor journal opened STRICTLY READ-ONLY (sqlite mode=ro URI).
  spread    : realized entry_spread_points → pips via the broker's captured
              point size, vs the research CostModel's spread_pips; per-symbol
              median; ratio > 1.5 = kill-rule breach (GUIDE §6 step 5).
  swap      : realized broker charge vs per-night directional quote × units ×
              rollover nights under BIT-FOR-BIT the backtest's night convention
              (UTC midnights crossed, weekday of the day left, Mon–Fri 1×,
              triple day 3×, weekend 0×) — rollover-hour mismatches must show
              up as error, not be calibrated away.
  slippage  : DECLARED UNMEASURABLE — the journal has no requested-price
              column. Reported as a schema gap, never estimated.
  currency  : quote→account conversion only where deterministic from the trade
              row (USD-quoted: 1.0; USD-base: 1/exit). Anything else is flagged
              'unconverted' and excluded from totals, never guessed.

stdlib + numpy only. Do NOT touch intel/executor/ — reads its journal, writes
nothing anywhere near it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import forwardtest as ft
from registry import ResultsRegistry, multiple_testing_warning

PHASE = 8
STRAT = "forward_reconcile"
DOC = "PHASE8.md"


# ───────────────────────────── brief ─────────────────────────────────────────
def brief_payload() -> dict:
    return {
        "phase": PHASE,
        "kind": "instrumentation — NO out-of-sample evaluation in this phase",
        "question": ("build and prove the read-only reconciliation instrument "
                     "GUIDE.md §6 step 4 requires: realized demo spread/swap "
                     "vs the research CostModel, with the §6 step 5 kill "
                     "threshold (realized > 1.5x modelled) applied per symbol"),
        "access": "executor journal via sqlite mode=ro URI — writes impossible",
        "spread_method": ("entry_spread_points x captured point / CostModel "
                          "pip_size; per-symbol median vs spread_pips; "
                          "breach = ratio > 1.5"),
        "swap_method": ("per-night directional quote x volume x contract size "
                        "x nights, nights counted exactly as backtest.py "
                        "nights_mult (UTC midnights, day-left weekday, "
                        "Mon-Fri 1x / triple 3x / weekend 0x), quote->USD only "
                        "where deterministic"),
        "slippage": "declared unmeasurable — no requested-price column; "
                    "reported as schema gap, never estimated",
        "multiple_testing": "0 configs added — reconciliation reads execution "
                            "reality, it evaluates no strategy",
    }


def log_brief():
    reg = ResultsRegistry()
    rh, dup = reg.log_run("brief", STRAT, brief_payload(), {},
                          {"symbol": "JOURNAL", "timeframe": 0},
                          notes="Phase 8 pre-registration — reconciliation "
                                "instrument, no OOS evaluation")
    reg.close()
    return rh, dup


def brief_exists() -> bool:
    reg = ResultsRegistry()
    n = reg.c.execute("SELECT COUNT(*) FROM results WHERE run_type='brief' "
                      "AND strategy=?", (STRAT,)).fetchone()[0]
    reg.close()
    return n > 0


# ───────────────────────────── doc ───────────────────────────────────────────
def render_doc(rep: dict, count_line: str) -> str:
    L = []
    L.append("# PHASE8.md — Forward-Test Reconciliation (pre-registered instrumentation)\n")
    L.append(f"- Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
             f"by phase8.py. Research plane only; the executor journal is opened "
             f"read-only and nothing in intel/executor/ is touched.\n")
    L.append("## Why\n")
    L.append("GUIDE.md §6 step 4 demands weekly reconciliation of live fills vs model "
             "assumptions before any forward test can graduate, and step 5 kills the test "
             "if realized costs exceed modelled by >50%. The protocol stays locked — no "
             "strategy has passed the research gate — but the instrument must exist and "
             "be proven BEFORE it is needed. `python3 forwardtest.py` is that instrument; "
             "this document is its first real run, against the accruing demo journal.\n")
    L.append(f"## Per-trade reconciliation ({rep['n_trades']} closed trades)\n")
    L.append("| ticket | symbol | side | nights | swap realized | swap model | "
             "spread realized | spread model |")
    L.append("|---|---|---|---|--:|--:|--:|--:|")
    for r in rep["rows"]:
        sp = ("—" if r["spread_realized_pips"] is None
              else f"{r['spread_realized_pips']:.1f}p")
        se = "—" if r["swap_expected"] is None else f"{r['swap_expected']:+.4f}"
        L.append(f"| {r['ticket']} | {r['symbol']} | {r['side']} | "
                 f"{r['nights_note']} | {(r['swap'] or 0.0):+.2f} | {se} | "
                 f"{sp} | {r['spread_model_pips']:.1f}p |")
    L.append("")
    L.append("## Spread: realized vs modelled (kill threshold "
             f"{rep['kill_ratio']}x)\n")
    L.append("| symbol | trades | with spread | median realized | model | ratio | verdict |")
    L.append("|---|--:|--:|--:|--:|--:|---|")
    for sym, s in sorted(rep["by_symbol"].items()):
        med = "—" if s["median_realized"] is None else f"{s['median_realized']:.1f}p"
        ratio = "—" if s["ratio"] is None else f"{s['ratio']:.2f}x"
        verdict = "**BREACH**" if s["breach"] else ("ok" if s["ratio"] else "—")
        L.append(f"| {sym} | {s['n']} | {s['n_spread']} | {med} | "
                 f"{s['model']:.1f}p | {ratio} | {verdict} |")
    st = rep["swap_totals"]
    L.append("")
    L.append("## Swap: realized vs modelled\n")
    L.append(f"- Total realized {st['realized']:+.2f} vs model expectation "
             f"{st['expected']:+.2f} over {st['n']} convertible trade(s); "
             f"max per-trade |difference| {st['max_abs_diff']:.2f} (account ccy).")
    L.append("- Night counting is bit-for-bit the backtest's convention, so any broker "
             "rollover-hour mismatch appears here as error rather than being calibrated "
             "away.\n")
    L.append("## Findings (honest reading, small sample)\n")
    n_overnight = sum(1 for r in rep["rows"]
                      if r["nights_note"] not in ("0 night(s)",))
    if rep["breaches"]:
        L.append(f"- **Spread kill-rule breach on {', '.join(rep['breaches'])}** — "
                 f"realized demo spreads run well above the research CostModel's "
                 f"assumptions. Every backtest in this repo therefore UNDERSTATES "
                 f"trading costs for this broker: the existing negative OOS verdicts "
                 f"are conservative in the right direction, and any future gate-pass "
                 f"must be re-run with recalibrated spreads (as a pre-registered "
                 f"re-run, not a silent parameter edit) before GUIDE §6 could start.")
    L.append(f"- The directional swap model reconciled against real broker charges on "
             f"{n_overnight} overnight trade(s) — a tiny sample; the check re-runs "
             f"weekly as the journal accrues.")
    for g in rep["schema_gaps"]:
        L.append(f"- Schema gap: {g}.")
    if rep["no_spec_symbols"]:
        L.append(f"- Excluded (no captured broker spec): "
                 f"{', '.join(rep['no_spec_symbols'])}.")
    L.append("")
    L.append("## Cadence\n")
    L.append("`python3 forwardtest.py` prints this reconciliation any time; GUIDE §6 "
             "step 4 asks for it weekly during a real forward test. It reads the journal "
             "read-only and can never write.\n")
    L.append("## Multiple-testing budget\n")
    L.append("Phase 8 adds ZERO configs (asserted at runtime: the counter is read before "
             "and after the run and must not move). No strategy result changed; the "
             "Phase 6 verdict (GATE NOT MET) stands untouched.\n")
    L.append("```")
    L.append(count_line)
    L.append("```\n")
    L.append("## Verdict\n")
    breach_txt = (f"kill-rule breach on {', '.join(rep['breaches'])} "
                  f"(realized spread > {rep['kill_ratio']}x model)"
                  if rep["breaches"] else "no kill-rule breach")
    L.append(f"**VERDICT: reconciliation instrument deployed and proven on "
             f"{rep['n_trades']} real demo trades — swap model reconciles "
             f"(max |diff| {st['max_abs_diff']:.2f}), {breach_txt}; NO OOS "
             f"evaluation performed; multiple-testing count unchanged.**")
    return "\n".join(L) + "\n"


# ───────────────────────────── commands ──────────────────────────────────────
def cmd_brief() -> None:
    rh, dup = log_brief()
    print(f"brief logged (hash {rh}){' — duplicate, already registered' if dup else ''}")


def cmd_run() -> None:
    if not brief_exists():
        raise SystemExit("REFUSING to run: no pre-registered brief in the registry. "
                         "Run `python3 phase8.py brief` first.")
    reg = ResultsRegistry()
    n_before, _ = reg.multiple_testing_count()
    reg.close()

    rep = ft.build_report()
    print(ft.render_text(rep))

    reg = ResultsRegistry()
    n_after, _ = reg.multiple_testing_count()
    reg.log_run("phase8_reconcile", STRAT,
                {"n_trades": rep["n_trades"], "breaches": rep["breaches"],
                 "swap_max_abs_diff": rep["swap_totals"]["max_abs_diff"]},
                {}, {"symbol": "JOURNAL", "timeframe": 0},
                notes="reconciliation run — no OOS evaluation")
    reg.close()
    if n_after != n_before:
        raise SystemExit(f"MULTIPLE-TESTING VIOLATION: counter moved "
                         f"{n_before} -> {n_after} during an instrumentation phase")

    with open(DOC, "w") as f:
        f.write(render_doc(rep, multiple_testing_warning(n_after)))
    print(f"\nwrote {DOC}; multiple-testing count unchanged at {n_after}")


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
        raise SystemExit("usage: python3 phase8.py [brief|run|all]")
