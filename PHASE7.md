# PHASE7.md — Historical Swap Series (pre-registered instrumentation)

- Generated 2026-07-07T19:38:38Z by phase7.py. Research plane only; intel/executor untouched.

## Why

Every carry number in Phases 4b/5/6 applies the broker's 2026-07-02 swap quote across 15 years of history — PHASE6.md's first documented limitation, and its first listed evidence upgrade is a historical swap series. Brokers do not publish past swap points, so the series cannot be backfilled; it can only be RECORDED from now on. Phase 7 deploys that instrument and locks the design of the future evaluation before its data exists.

## The instrument

- `python3 swapseries.py record` — captures the current quote per symbol from the running MT5 bridge (falling back to the frozen `data/*_swap.json` captures, which land at their TRUE historical date) and appends to `data/swap_history.csv`.
- Idempotency: one row per (symbol, UTC capture date), keep-first, append-only; re-running record is a no-op.
- Causality contract: a quote captured on UTC day D first affects bars dated STRICTLY AFTER D; bars before the first capture use the constant spec (bit-for-bit Phase 4b/5/6 behaviour) and the fallback bar-count is always reported.
- Loader contract: swap_mode==1 (points) only — same refusal as config.load_swap_spec; the recorder stores any mode faithfully, the loader refuses to convert unknowns.
- Cadence: once per UTC day is kept; a cron line like `17 6 * * *  cd /home/flowdaaddy/mt5-research && python3 swapseries.py record` builds the series unattended while the executor keeps the bridge up.

## Recording status

| symbol | captures | first | last | long px/night (latest) | short px/night (latest) |
|---|--:|---|---|--:|--:|
| EURUSD | 2 | 2026-07-02 | 2026-07-07 | -0.00008020 | +0.00001280 |
| GBPUSD | 2 | 2026-07-02 | 2026-07-07 | -0.00003720 | -0.00003820 |
| USDJPY | 2 | 2026-07-02 | 2026-07-07 | +0.00211000 | -0.02959000 |
| AUDUSD | 2 | 2026-07-02 | 2026-07-07 | -0.00001800 | -0.00003100 |
| GOLD | 2 | 2026-07-02 | 2026-07-07 | -0.90350000 | +0.11150000 |

## Pre-registered future evaluation (locked now, runs when the data exists)

- **Trigger:** series spans >= 180 calendar days AND every basket sleeve has >= 120 captures.
- **Configs (exactly two):** carry_momentum A/filter X=0bps lookback=120 anchor=200 (the Phase-6 survivor); ts_momentum lookback=120 anchor=200 (the Phase-4b baseline).
- **Method:** per-bar recorded swaps via swapseries.per_bar_swap into carry_bps_per_year; constant fallback before first capture; same 5 sleeves, common window, inverse-vol weights, 750/250 portfolio walk-forward, directional cost stack as Phase 4/6 — portfolio.run_portfolio_wf REUSED, not copied.
- **Gate:** pooled OOS portfolio Sharpe >= 0.5 net of ALL costs.
- **Multiple testing:** exactly 2 configs added to the counter when the future run happens; 0 during Phase 7.

## What this phase does NOT claim

- No new OOS evaluations were performed; no strategy result changed; the Phase 6 verdict (GATE NOT MET) stands untouched.
- The recorded series starts in 2026-07 — history before that stays under the constant-quote approximation forever, and any future honest-carry backtest must report how many bars used the fallback.
- A quote captured once per day cannot see intraday swap repricing; the series is a daily step function by construction.

## Multiple-testing budget

Phase 7 adds ZERO configs (asserted at runtime: the counter is read before and after the run and must not move).

```
MULTIPLE-TESTING COUNT: 34 distinct strategy+param config(s) have been evaluated against out-of-sample data (deduped across all logged runs).
  ⚠ The more configs you test, the more likely the best OOS result is LUCK.
  Rough intuition: at a 5% false-positive rate, ~1.7 of 34 configs would look like 'winners' by pure chance.
  A survivor needs a MUCH higher bar — or fresh, never-tested data — before it means anything. This is a counter + warning, not a significance test.
```

## Verdict

**VERDICT: instrumentation deployed — swap history recording active (5/5 basket sleeves, 10 rows); NO OOS evaluation performed; multiple-testing count unchanged.**
