# PHASE8.md — Forward-Test Reconciliation (pre-registered instrumentation)

- Generated 2026-07-07T19:48:06Z by phase8.py. Research plane only; the executor journal is opened read-only and nothing in intel/executor/ is touched.

## Why

GUIDE.md §6 step 4 demands weekly reconciliation of live fills vs model assumptions before any forward test can graduate, and step 5 kills the test if realized costs exceed modelled by >50%. The protocol stays locked — no strategy has passed the research gate — but the instrument must exist and be proven BEFORE it is needed. `python3 forwardtest.py` is that instrument; this document is its first real run, against the accruing demo journal.

## Per-trade reconciliation (6 closed trades)

| ticket | symbol | side | nights | swap realized | swap model | spread realized | spread model |
|---|---|---|---|--:|--:|--:|--:|
| 795321849 | EURUSD | buy | 0 night(s) | +0.00 | -0.0000 | 1.9p | 0.8p |
| 795518308 | GOLD | buy | 0 night(s) | +0.00 | -0.0000 | 56.0p | 25.0p |
| 795659994 | EURUSD | buy | 0 night(s) | +0.00 | -0.0000 | — | 0.8p |
| 797196476 | GOLD | buy | 1 night(s) | -0.90 | -0.9035 | 52.0p | 25.0p |
| 797310893 | GOLD | buy | 0 night(s) | +0.00 | -0.0000 | 55.0p | 25.0p |
| 797650937 | GOLD | sell | 0 night(s) | +0.00 | +0.0000 | 51.0p | 25.0p |

## Spread: realized vs modelled (kill threshold 1.5x)

| symbol | trades | with spread | median realized | model | ratio | verdict |
|---|--:|--:|--:|--:|--:|---|
| EURUSD | 2 | 1 | 1.9p | 0.8p | 2.37x | **BREACH** |
| GOLD | 4 | 4 | 53.5p | 25.0p | 2.14x | **BREACH** |

## Swap: realized vs modelled

- Total realized -0.90 vs model expectation -0.90 over 6 convertible trade(s); max per-trade |difference| 0.00 (account ccy).
- Night counting is bit-for-bit the backtest's convention, so any broker rollover-hour mismatch appears here as error rather than being calibrated away.

## Findings (honest reading, small sample)

- **Spread kill-rule breach on EURUSD, GOLD** — realized demo spreads run well above the research CostModel's assumptions. Every backtest in this repo therefore UNDERSTATES trading costs for this broker: the existing negative OOS verdicts are conservative in the right direction, and any future gate-pass must be re-run with recalibrated spreads (as a pre-registered re-run, not a silent parameter edit) before GUIDE §6 could start.
- The directional swap model reconciled against real broker charges on 1 overnight trade(s) — a tiny sample; the check re-runs weekly as the journal accrues.
- Schema gap: slippage: journal records fill price only (no requested-price column) — unmeasurable until the executor journals it.

## Cadence

`python3 forwardtest.py` prints this reconciliation any time; GUIDE §6 step 4 asks for it weekly during a real forward test. It reads the journal read-only and can never write.

## Multiple-testing budget

Phase 8 adds ZERO configs (asserted at runtime: the counter is read before and after the run and must not move). No strategy result changed; the Phase 6 verdict (GATE NOT MET) stands untouched.

```
MULTIPLE-TESTING COUNT: 34 distinct strategy+param config(s) have been evaluated against out-of-sample data (deduped across all logged runs).
  ⚠ The more configs you test, the more likely the best OOS result is LUCK.
  Rough intuition: at a 5% false-positive rate, ~1.7 of 34 configs would look like 'winners' by pure chance.
  A survivor needs a MUCH higher bar — or fresh, never-tested data — before it means anything. This is a counter + warning, not a significance test.
```

## Verdict

**VERDICT: reconciliation instrument deployed and proven on 6 real demo trades — swap model reconciles (max |diff| 0.00), kill-rule breach on EURUSD, GOLD (realized spread > 1.5x model); NO OOS evaluation performed; multiple-testing count unchanged.**
