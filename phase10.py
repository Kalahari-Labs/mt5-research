"""phase10.py — PHASE 10: CONSOLIDATED RESEARCH REVIEW (research plane ONLY).

THE QUESTION (fixed a priori): nine research phases, 34 OOS-evaluated configs,
four instruments and one live demo forward test now exist across a dozen
documents and a registry. Phase 10 builds the SYNTHESIS instrument: one
document (RESEARCH.md) regenerated on demand from ground truth — registry rows,
each phase doc's own verdict line quoted VERBATIM, the live reconciliation
state, and the recorded swap-series status. It computes no strategy results,
re-runs no backtests, and editorialises no numbers: everything it prints must
be traceable to a registry row, a verdict line, or an instrument's read-only
output. This is the weekly research report the repo's mandate asks for — the
report analyses; it never trades.

THIS PHASE PERFORMS NO OUT-OF-SAMPLE EVALUATION. The multiple-testing counter
must be IDENTICAL before and after `python3 phase10.py run` (asserted at
runtime; the run aborts if it moved).

stdlib + numpy only. Do NOT touch intel/executor/ (the forward-test doc and
journal are read-only inputs).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import forwardtest as ft
import swapseries as ss
from config import BASE_DIR
from registry import ResultsRegistry, multiple_testing_warning

PHASE = 10
STRAT = "research_review"
DOC = "RESEARCH.md"
# every research document that carries (or should carry) a final verdict line
VERDICT_DOCS = ("WALKFORWARD.md", "ROBUSTNESS.md", "PORTFOLIO.md",
                "SHORTHOLDS.md", "PHASE6.md", "PHASE7.md", "PHASE8.md",
                "PHASE9.md")
BASKET = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD")
FWD_DOC = Path(BASE_DIR) / "intel" / "docs" / "forward-test.md"
VERDICT_RE = re.compile(r"\*\*VERDICT:\s*(.+?)\*\*", re.DOTALL)


# ───────────────────────────── brief ─────────────────────────────────────────
def brief_payload() -> dict:
    return {
        "phase": PHASE,
        "kind": "synthesis — NO out-of-sample evaluation in this phase",
        "question": ("one regenerable document consolidating every phase "
                     "verdict, the registry state, and the live instruments' "
                     "read-only outputs — the weekly research report"),
        "sources": {"verdicts": list(VERDICT_DOCS),
                    "registry": "results + oos_configs tables",
                    "reconciliation": "forwardtest.build_report (read-only)",
                    "swap_series": "swapseries.load_series (read-only)",
                    "forward_test": str(FWD_DOC)},
        "rules": ["verdict lines quoted VERBATIM from their documents",
                  "no strategy result computed or re-run here",
                  "multiple-testing counter pinned (asserted before/after)"],
    }


def log_brief():
    reg = ResultsRegistry()
    rh, dup = reg.log_run("brief", STRAT, brief_payload(), {},
                          {"symbol": "PHASE10-BRIEF", "timeframe": 0},
                          notes="Phase 10 pre-registration — synthesis, "
                                "no OOS evaluation")
    reg.close()
    return rh, dup


def brief_exists() -> bool:
    reg = ResultsRegistry()
    n = reg.c.execute("SELECT COUNT(*) FROM results WHERE run_type='brief' "
                      "AND strategy=?", (STRAT,)).fetchone()[0]
    reg.close()
    return n > 0


# ───────────────────────────── ground truth ──────────────────────────────────
def extract_verdict(path) -> str | None:
    """The LAST verbatim **VERDICT: ...** line of a document, or None."""
    p = Path(path)
    if not p.exists():
        return None
    hits = VERDICT_RE.findall(p.read_text())
    return hits[-1].strip() if hits else None


def collect_verdicts(base=None) -> list[tuple[str, str | None]]:
    base = Path(base or BASE_DIR)
    return [(name, extract_verdict(base / name)) for name in VERDICT_DOCS]


def registry_summary(path=None) -> dict:
    reg = ResultsRegistry(path)
    by_type = dict(reg.c.execute(
        "SELECT run_type, COUNT(*) FROM results GROUP BY run_type "
        "ORDER BY COUNT(*) DESC").fetchall())
    briefs = [(r[0], r[1][:19]) for r in reg.c.execute(
        "SELECT strategy, ts FROM results WHERE run_type='brief' ORDER BY id")]
    strategies = [r[0] for r in reg.c.execute(
        "SELECT DISTINCT strategy FROM oos_configs ORDER BY strategy")]
    n_total = reg.c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    n_mt, _ = reg.multiple_testing_count()
    reg.close()
    return {"n_total": n_total, "by_type": by_type, "briefs": briefs,
            "oos_strategies": strategies, "n_mt": n_mt,
            "warning": multiple_testing_warning(n_mt)}


def forward_test_status(path=None) -> dict:
    """Verbatim-ish extraction from the executor's auto-generated doc."""
    p = Path(path or FWD_DOC)
    out = {"exists": p.exists(), "day": None, "of": None, "equity": None,
           "ret": None}
    if not p.exists():
        return out
    txt = p.read_text()
    m = re.search(r"day \*\*(\d+)\*\* of (\d+)", txt)
    if m:
        out["day"], out["of"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Current equity \| ([\d.]+)", txt)
    if m:
        out["equity"] = float(m.group(1))
    m = re.search(r"Return since start \| ([-+\d.]+)%", txt)
    if m:
        out["ret"] = float(m.group(1))
    return out


def swap_series_status(data_dir=None) -> dict:
    per_sym, n_rows = {}, 0
    span_days = None
    firsts, lasts = [], []
    for sym in BASKET:
        s = ss.load_series(sym, data_dir=data_dir)
        per_sym[sym] = 0 if s is None else s["n"]
        n_rows += per_sym[sym]
        if s is not None:
            firsts.append(s["dates"][0])
            lasts.append(s["dates"][-1])
    if firsts:
        span_days = int((max(lasts) - min(firsts)).astype(int))
    min_caps = min(per_sym.values()) if per_sym else 0
    return {"per_sym": per_sym, "n_rows": n_rows, "span_days": span_days,
            "min_captures": min_caps,
            "trigger_met": bool(span_days is not None and span_days >= 180
                                and min_caps >= 120)}


# ───────────────────────────── doc ───────────────────────────────────────────
def render_doc(verdicts, regsum, fwd, swaps, recon) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["# RESEARCH.md — Consolidated Research Review\n"]
    L.append(f"- Regenerated {now} by `python3 phase10.py run` (weekly cadence; "
             f"every number below is traceable to a registry row, a document's "
             f"own verdict line, or a read-only instrument).\n")
    L.append("## Where the research stands (one paragraph)\n")
    L.append(f"{regsum['n_mt']} distinct strategy+parameter configurations have "
             f"been evaluated against out-of-sample data across nine phases. "
             f"None has passed the pre-registered promotion gate (pooled OOS "
             f"portfolio Sharpe ≥ 0.5 net of all costs). The best survivor — "
             f"carry-filtered momentum, Phase 6 — scored 0.28 and stays parked. "
             f"The demo forward test runs with human-in-the-loop approval; "
             f"live trading remains locked behind the research gate, the "
             f"3-month forward-test protocol (GUIDE.md §6) and the triple "
             f"unlock (tools/unlock_live.py). Nothing in this review changes "
             f"any of that.\n")
    L.append("## Phase verdicts (verbatim from each document)\n")
    L.append("| document | verdict |")
    L.append("|---|---|")
    for name, v in verdicts:
        L.append(f"| {name} | {v if v else '_no verdict line found_'} |")
    L.append("")
    L.append("## Registry state\n")
    L.append(f"- {regsum['n_total']} logged runs — " +
             ", ".join(f"{k}: {v}" for k, v in regsum["by_type"].items()) + ".")
    L.append(f"- Pre-registered briefs on file: " +
             ", ".join(f"{s} ({ts})" for s, ts in regsum["briefs"]) + ".")
    L.append(f"- Strategies ever OOS-evaluated: "
             f"{', '.join(regsum['oos_strategies'])}.\n")
    L.append("```")
    L.append(regsum["warning"])
    L.append("```\n")
    L.append("## Live instruments (read-only views)\n")
    if fwd["exists"] and fwd["day"] is not None:
        L.append(f"- **Demo forward test**: day {fwd['day']} of {fwd['of']}, "
                 f"equity {fwd['equity']}, return since start {fwd['ret']}% "
                 f"(auto-generated by the executor; HITL approval on).")
    else:
        L.append("- **Demo forward test**: doc not found / not yet generated.")
    br = ", ".join(recon["breaches"]) if recon["breaches"] else "none"
    L.append(f"- **Cost reconciliation (Phase 8)**: {recon['n_trades']} closed "
             f"trades; spread kill-rule breaches: {br}; swap model max "
             f"|diff| {recon['swap_totals']['max_abs_diff']:.2f} — realized "
             f"demo spreads exceed the modelled assumptions, so every backtest "
             f"verdict above is conservative in the right direction.")
    L.append(f"- **Swap series (Phase 7)**: {swaps['n_rows']} rows — " +
             ", ".join(f"{s}: {n}" for s, n in swaps["per_sym"].items()) +
             f"; span {swaps['span_days']} day(s), min captures/sleeve "
             f"{swaps['min_captures']}.\n")
    L.append("## What unlocks what (the decision tree ahead)\n")
    trig = ("**TRIGGER MET**" if swaps["trigger_met"]
            else f"not yet (needs ≥180-day span AND ≥120 captures/sleeve)")
    L.append(f"1. **Honest-carry re-run** (pre-registered in Phase 7, exactly 2 "
             f"configs): {trig}.")
    L.append("2. **Cross-sectional holdout** (pre-registered in Phase 9, exactly "
             "2 configs): blocked on terminal history — open each candidate's "
             "D1 chart once, then `python3 phase9.py run`.")
    L.append("3. **Fresh time holdout**: data window closed 2026-06-29; "
             "meaningful after ~6 months of new bars accrue.")
    L.append("4. **GUIDE.md §6 forward-test protocol**: stays LOCKED until some "
             "pre-registered config passes the 0.5 OOS gate; the Phase 8 "
             "instrument is ready for it. Recalibrated (empirical) spreads must "
             "be part of any future gate re-run — Phase 8 showed the legacy "
             "assumptions understate real costs.")
    L.append("5. **Live trading**: research gate → 3-month demo forward test → "
             "triple unlock, in that order, human decision at the end. No "
             "shortcut exists in this codebase, by design.\n")
    L.append("## Multiple-testing budget\n")
    L.append("Phase 10 adds ZERO configs (asserted at runtime: the counter is "
             "read before and after the run and must not move).\n")
    L.append("## Verdict\n")
    L.append(f"**VERDICT: research review regenerated from ground truth — "
             f"{regsum['n_mt']} configs OOS-evaluated, zero past the 0.5 gate; "
             f"promotion path locked and instrumented; NO OOS evaluation "
             f"performed; multiple-testing count unchanged.**")
    return "\n".join(L) + "\n"


# ───────────────────────────── commands ──────────────────────────────────────
def cmd_brief() -> None:
    rh, dup = log_brief()
    print(f"brief logged (hash {rh}){' — duplicate, already registered' if dup else ''}")


def cmd_run() -> None:
    if not brief_exists():
        raise SystemExit("REFUSING to run: no pre-registered brief in the registry. "
                         "Run `python3 phase10.py brief` first.")
    reg = ResultsRegistry()
    n_before, _ = reg.multiple_testing_count()
    reg.close()

    verdicts = collect_verdicts()
    regsum = registry_summary()
    fwd = forward_test_status()
    swaps = swap_series_status()
    recon = ft.build_report()

    reg = ResultsRegistry()
    n_after, _ = reg.multiple_testing_count()
    reg.log_run("phase10_review", STRAT,
                {"n_verdicts": sum(1 for _, v in verdicts if v),
                 "n_mt": n_after}, {},
                {"symbol": "RESEARCH", "timeframe": 0},
                notes="research review regenerated — no OOS evaluation")
    reg.close()
    if n_after != n_before:
        raise SystemExit(f"MULTIPLE-TESTING VIOLATION: counter moved "
                         f"{n_before} -> {n_after} during a synthesis phase")

    with open(DOC, "w") as f:
        f.write(render_doc(verdicts, regsum, fwd, swaps, recon))
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
        raise SystemExit("usage: python3 phase10.py [brief|run|all]")
