"""phase7.py — PHASE 7: HISTORICAL SWAP SERIES (research plane ONLY).

THE QUESTION (fixed a priori): PHASE6.md's first documented limitation is that
every carry number in Phases 4b/5/6 marks TODAY's broker swap quote across the
whole backtest history, and its first listed evidence upgrade is "a historical
swap series instead of the constant-quote approximation". No such series exists
anywhere and it cannot be backfilled. Phase 7 therefore deploys the INSTRUMENT:
record the live quote daily (swapseries.py), define the causal per-bar lookup,
and PRE-REGISTER the future evaluation that will use the series once it is long
enough — so that when the honest carry backtest finally runs, its design was
locked years before its data existed.

THIS PHASE PERFORMS NO OUT-OF-SAMPLE EVALUATION. The multiple-testing counter
must be IDENTICAL before and after `python3 phase7.py run` (asserted at runtime;
the run aborts if it moved).

PRE-REGISTERED FUTURE EVALUATION (logged to the registry BEFORE any recording —
`python3 phase7.py brief`; `run` refuses to start without that row):
  trigger  : recorded series spans >= 180 calendar days AND every basket sleeve
             has >= 120 daily captures.
  test     : re-run exactly TWO configs with per-bar recorded swaps replacing
             the constant (constant fallback before the first capture, strictly-
             before-day causality): (1) the Phase-6 survivor carry_momentum
             A/filter X=0bps 120/200, (2) the Phase-4b ts_momentum 120/200
             baseline. Same 5 sleeves, same common window, same inverse-vol
             weights, same 750/250 portfolio walk-forward, same directional
             cost stack. Nothing else.
  gate     : pooled OOS portfolio Sharpe >= 0.5 net of ALL costs.
  counting : exactly 2 configs enter the multiple-testing counter WHEN THAT RUN
             HAPPENS; zero enter during Phase 7 itself.

stdlib + numpy only. Do NOT touch intel/executor/.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import swapseries as ss
from registry import ResultsRegistry, multiple_testing_warning

PHASE = 7
STRAT = "swap_series"
DOC = "PHASE7.md"
TRIGGER_DAYS = 180
TRIGGER_CAPTURES = 120
BASKET = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD")


# ───────────────────────────── brief ─────────────────────────────────────────
def brief_payload() -> dict:
    return {
        "phase": PHASE,
        "kind": "instrumentation — NO out-of-sample evaluation in this phase",
        "question": ("record the broker's real swap quotes over time so a future "
                     "phase can replace the constant-quote carry approximation "
                     "(PHASE6.md limitation #1) with quotes that were actually "
                     "live at each point in history"),
        "schema": list(ss.FIELDS),
        "idempotency": ("one row per (symbol, UTC capture date), keep-first, "
                        "append-only; re-running record is a no-op"),
        "causality": ("a quote captured on UTC day D first affects bars dated "
                      "STRICTLY AFTER D; bars before the first capture use the "
                      "constant spec (bit-for-bit Phase 4b/5/6 behaviour) and "
                      "the fallback bar-count is always reported"),
        "loader_contract": ("swap_mode==1 (points) only — same refusal as "
                            "config.load_swap_spec; the recorder stores any mode "
                            "faithfully, the loader refuses to convert unknowns"),
        "future_evaluation": {
            "trigger": (f"series spans >= {TRIGGER_DAYS} calendar days AND every "
                        f"basket sleeve has >= {TRIGGER_CAPTURES} captures"),
            "configs": ["carry_momentum A/filter X=0bps lookback=120 anchor=200 "
                        "(the Phase-6 survivor)",
                        "ts_momentum lookback=120 anchor=200 (the Phase-4b "
                        "baseline)"],
            "method": ("per-bar recorded swaps via swapseries.per_bar_swap into "
                       "carry_bps_per_year; constant fallback before first "
                       "capture; same 5 sleeves, common window, inverse-vol "
                       "weights, 750/250 portfolio walk-forward, directional "
                       "cost stack as Phase 4/6 — portfolio.run_portfolio_wf "
                       "REUSED, not copied"),
            "gate": "pooled OOS portfolio Sharpe >= 0.5 net of ALL costs",
            "multiple_testing": ("exactly 2 configs added to the counter when "
                                 "the future run happens; 0 during Phase 7"),
        },
    }


def log_brief():
    reg = ResultsRegistry()
    rh, dup = reg.log_run("brief", STRAT, brief_payload(), {},
                          {"symbol": "PORTFOLIO", "timeframe": 1440},
                          notes="Phase 7 pre-registration — instrumentation, "
                                "no OOS evaluation")
    reg.close()
    return rh, dup


def brief_exists() -> bool:
    reg = ResultsRegistry()
    n = reg.c.execute("SELECT COUNT(*) FROM results WHERE run_type='brief' "
                      "AND strategy=?", (STRAT,)).fetchone()[0]
    reg.close()
    return n > 0


# ───────────────────────────── doc ───────────────────────────────────────────
def render_doc(status: dict, count_line: str) -> str:
    b = brief_payload()
    L = []
    L.append("# PHASE7.md — Historical Swap Series (pre-registered instrumentation)\n")
    L.append(f"- Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
             f"by phase7.py. Research plane only; intel/executor untouched.\n")
    L.append("## Why\n")
    L.append("Every carry number in Phases 4b/5/6 applies the broker's 2026-07-02 swap "
             "quote across 15 years of history — PHASE6.md's first documented limitation, "
             "and its first listed evidence upgrade is a historical swap series. Brokers "
             "do not publish past swap points, so the series cannot be backfilled; it can "
             "only be RECORDED from now on. Phase 7 deploys that instrument and locks the "
             "design of the future evaluation before its data exists.\n")
    L.append("## The instrument\n")
    L.append("- `python3 swapseries.py record` — captures the current quote per symbol "
             "from the running MT5 bridge (falling back to the frozen `data/*_swap.json` "
             "captures, which land at their TRUE historical date) and appends to "
             "`data/swap_history.csv`.")
    L.append(f"- Idempotency: {b['idempotency']}.")
    L.append(f"- Causality contract: {b['causality']}.")
    L.append(f"- Loader contract: {b['loader_contract']}.")
    L.append("- Cadence: once per UTC day is kept; a cron line like "
             "`17 6 * * *  cd " + str(ss.DATA_DIR.parent) + " && python3 swapseries.py record` "
             "builds the series unattended while the executor keeps the bridge up.\n")
    L.append("## Recording status\n")
    L.append("| symbol | captures | first | last | long px/night (latest) | short px/night (latest) |")
    L.append("|---|--:|---|---|--:|--:|")
    for sym in BASKET:
        s = status.get(sym)
        if s is None:
            L.append(f"| {sym} | 0 | — | — | — | — |")
        else:
            L.append(f"| {sym} | {s['n']} | {s['dates'][0]} | {s['dates'][-1]} | "
                     f"{s['swap_long_per_night'][-1]:+.8f} | "
                     f"{s['swap_short_per_night'][-1]:+.8f} |")
    L.append("")
    L.append("## Pre-registered future evaluation (locked now, runs when the data exists)\n")
    fe = b["future_evaluation"]
    L.append(f"- **Trigger:** {fe['trigger']}.")
    L.append(f"- **Configs (exactly two):** {fe['configs'][0]}; {fe['configs'][1]}.")
    L.append(f"- **Method:** {fe['method']}.")
    L.append(f"- **Gate:** {fe['gate']}.")
    L.append(f"- **Multiple testing:** {fe['multiple_testing']}.\n")
    L.append("## What this phase does NOT claim\n")
    L.append("- No new OOS evaluations were performed; no strategy result changed; the "
             "Phase 6 verdict (GATE NOT MET) stands untouched.")
    L.append("- The recorded series starts in 2026-07 — history before that stays under "
             "the constant-quote approximation forever, and any future honest-carry "
             "backtest must report how many bars used the fallback.")
    L.append("- A quote captured once per day cannot see intraday swap repricing; the "
             "series is a daily step function by construction.\n")
    L.append("## Multiple-testing budget\n")
    L.append("Phase 7 adds ZERO configs (asserted at runtime: the counter is read before "
             "and after the run and must not move).\n")
    L.append("```")
    L.append(count_line)
    L.append("```\n")
    L.append("## Verdict\n")
    n_syms = sum(1 for s in BASKET if status.get(s))
    total = sum(status[s]["n"] for s in BASKET if status.get(s))
    L.append(f"**VERDICT: instrumentation deployed — swap history recording active "
             f"({n_syms}/{len(BASKET)} basket sleeves, {total} rows); NO OOS evaluation "
             f"performed; multiple-testing count unchanged.**")
    return "\n".join(L) + "\n"


# ───────────────────────────── commands ──────────────────────────────────────
def cmd_brief() -> None:
    rh, dup = log_brief()
    print(f"brief logged (hash {rh}){' — duplicate, already registered' if dup else ''}")


def cmd_run() -> None:
    if not brief_exists():
        raise SystemExit("REFUSING to run: no pre-registered brief in the registry. "
                         "Run `python3 phase7.py brief` first.")
    reg = ResultsRegistry()
    n_before, _ = reg.multiple_testing_count()
    reg.close()

    # 1) record: frozen file captures (true historical date) + fresh bridge quote
    file_rows = ss.capture_files()
    syms = sorted({r["symbol"] for r in file_rows})
    bridge_rows, errors = ss.capture_bridge(syms)
    for e in errors:
        print(f"  bridge: {e}")
    a, s = ss.record(file_rows + bridge_rows)
    print(f"recorded: +{a} rows, {s} already present")

    # 2) idempotency self-check: an immediate re-record must be a pure no-op
    a2, _ = ss.record(file_rows + bridge_rows)
    if a2 != 0:
        raise SystemExit(f"IDEMPOTENCY VIOLATION: re-record added {a2} rows")

    # 3) per-symbol status for the doc
    status = {sym: ss.load_series(sym) for sym in BASKET}

    # 4) the counter must not have moved — this phase evaluates nothing
    reg = ResultsRegistry()
    n_after, _ = reg.multiple_testing_count()
    reg.log_run("phase7_record", STRAT,
                {sym: (status[sym]["n"] if status[sym] else 0) for sym in BASKET},
                {}, {"symbol": "PORTFOLIO", "timeframe": 1440},
                notes="swap history recording run — no OOS evaluation")
    reg.close()
    if n_after != n_before:
        raise SystemExit(f"MULTIPLE-TESTING VIOLATION: counter moved "
                         f"{n_before} -> {n_after} during an instrumentation phase")

    doc = render_doc(status, multiple_testing_warning(n_after))
    with open(DOC, "w") as f:
        f.write(doc)
    print(f"wrote {DOC}; multiple-testing count unchanged at {n_after}")


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
        raise SystemExit("usage: python3 phase7.py [brief|run|all]")
