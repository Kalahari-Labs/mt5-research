# PHASE9.md — Never-Tested-Symbols Holdout (pre-registered)

- Generated 2026-07-07T20:49:51Z by phase9.py. Research plane only; the bridge is read over HTTP; intel/executor untouched.

## The brief (pre-registered BEFORE any holdout bar was fetched)

- Candidates (fixed, chosen blind): NZDUSD, USDCAD, USDCHF, EURGBP, EURJPY, GBPJPY.
- Screen (mechanical): broker serves >= 2000 D1 bars · swap_mode == 1 (points) · never OOS-evaluated before (asserted vs the ever-tested set).
- Exactly two configs: the Phase-6 survivor (carry filter X=0bps, 120/200) and the Phase-4b ts_momentum control.
- Costs: median per-bar broker spread over full fetched D1 history (empirical — Phase 8 showed legacy assumptions understate; understating biases toward passing); slippage 0.3 pips; commission 3.5/lot; directional swap from live spec.
- Gate: pooled OOS portfolio Sharpe >= 0.5 net of ALL costs for the carry config; baseline is control.

## Screen results

| candidate | verdict | detail |
|---|---|---|
| NZDUSD | PASS | 2000 D1 bars, swap_mode 1 |
| USDCAD | fail | fetch failed: ValueError: terminal serves < 2000 D1 bars (deep history not downloaded — open USDCAD's chart in the MT5 terminal once, let it sync, then re-run `python3 phase9.py run`) |
| USDCHF | fail | fetch failed: ValueError: terminal serves < 2000 D1 bars (deep history not downloaded — open USDCHF's chart in the MT5 terminal once, let it sync, then re-run `python3 phase9.py run`) |
| EURGBP | fail | fetch failed: ValueError: terminal serves < 2000 D1 bars (deep history not downloaded — open EURGBP's chart in the MT5 terminal once, let it sync, then re-run `python3 phase9.py run`) |
| EURJPY | fail | fetch failed: ValueError: terminal serves < 2000 D1 bars (deep history not downloaded — open EURJPY's chart in the MT5 terminal once, let it sync, then re-run `python3 phase9.py run`) |
| GBPJPY | fail | fetch failed: ValueError: terminal serves < 2000 D1 bars (deep history not downloaded — open GBPJPY's chart in the MT5 terminal once, let it sync, then re-run `python3 phase9.py run`) |

## What unblocks this phase

The MT5 terminal only downloads deep history for a symbol when its chart has been opened once (that is how the basket symbols got theirs at install time); it refuses API-triggered backfill for never-charted symbols. One-time human step: open a D1 chart for each candidate in the terminal, let it sync (~seconds per symbol), then re-run `python3 phase9.py run`. The brief stays pre-registered and unchanged; nothing about the design may be edited between now and that run.

## Verdict

**VERDICT: fewer than 3 candidates survived the mechanical screen — holdout NOT FEASIBLE on this broker today (terminal serves insufficient history for never-charted symbols); no evaluation performed; multiple-testing count unchanged.**
