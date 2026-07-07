# PHASE6.md — Carry-Aware Momentum (pre-registered)

- Generated 2026-07-06T14:54:27Z by phase6.py. Research plane only; intel/executor untouched.

## The brief (pre-registered in the results registry BEFORE any run)

Phase 4: TSMOM portfolio edge is gross-positive but financing kills it. Phase 5: faster cycling loses (turnover > swap savings). Untested gap: make the SIGNAL swap-aware instead of changing the holding period.

- **A) Carry filter** — hold the TSMOM position only when the directional overnight swap for that side ≥ −X bps/yr, X ∈ {0, 50, 100}; else flat.
- **B) Composite** — score = z(momentum) + λ·z(carry), λ ∈ {0.25, 0.5}; direction = sign(score), same anchor overlay as ts_momentum.
- No other variants. Fixed 120/200, same 5 sleeves, same common window, same inverse-vol weighting, same 750/250 portfolio walk-forward as Phase 4 (portfolio.run_portfolio_wf reused). Costs: Phase-4b directional stack.
- Gate: pooled OOS portfolio Sharpe ≥ 0.5 net of ALL costs.


## Carry inputs (broker swap capture 2026-07-02, marked at last close)

| sleeve | swap long (px/night) | swap short (px/night) | ref close | carry long (bps/yr) | carry short (bps/yr) | net long-favouring | carry_z |
|---|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | -0.000080 | +0.000013 | 1.14 | -256.8 | +41.0 | -148.9 | -0.413 |
| GBPUSD | -0.000037 | -0.000038 | 1.33 | -102.4 | -105.2 | +1.4 | +0.147 |
| USDJPY | +0.002110 | -0.029590 | 161.93 | +47.6 | -667.0 | +357.3 | +1.475 |
| AUDUSD | -0.000009 | -0.000032 | 0.69 | -47.2 | -169.0 | +60.9 | +0.370 |
| GOLD | -0.903500 | +0.111500 | 4015.17 | -821.3 | +101.4 | -461.3 | -1.579 |

Reading: on this account longs bleed on EURUSD/GBPUSD/AUDUSD/GOLD and earn on USDJPY; shorts bleed on GBPUSD/USDJPY/AUDUSD and earn a credit on EURUSD/GOLD. The filter can only remove exposure; the composite tilts it.


## Baseline (Phase-4b: unchanged ts_momentum 120/200, directional swap)

- Common window **2011-03-22 → 2026-06-29**, 3942 D1 bars, 5 sleeves (US500Cash/OILCash excluded a priori — insufficient history, exactly as Phase 4 dropped them).
- Full window: ann **-1.62%**, Sharpe -0.28, maxDD -26.7%, trades 977.
- Pooled WF OOS (750/250): total -14.84%, ann -1.38%, Sharpe **-0.23**, maxDD -25.85%.
- Cross-check vs SHORTHOLDS.md D1 bridge (ann -1.62%, Sharpe -0.28, OOS Sharpe -0.23): reproduced by this pipeline — proves the Phase-4 machinery is unchanged.


## Robustness surface (EURUSD D1, filter variant, directional costs, IN-SAMPLE shape only)
```
  rows = lookback  (top→bottom)   cols = max_adverse_carry_bps  (left→right)
         0.050.0100.0
     20    #   #   #
     40    #   #   #
     60    +   +   +
     90    #   #   #
    120    @   #   #
    150    #   #   #
    180    #   #   #
    210    #   #   #
    252    .   .   .

  legend: @ best   # >=+2%   + 0..2%   - -2..0%   . <-2%   ~ <min-trades   (blank) invalid
```

**VERDICT: PLATEAU — best lookback/max_adverse_carry_bps = 120/0.0 @ +42.61%; 24/27 cells profitable (89%), largest contiguous profitable block = 24 cells.  Broad plateau — more likely a real effect than a fit artefact.**


## Pre-registered configs — full window + pooled OOS (the honest number)

| config | ann% (full) | Sharpe (full) | maxDD% | trades | OOS total% | OOS ann% | OOS Sharpe | OOS maxDD% |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| baseline ts_momentum (4b) | -1.62 | -0.28 | -26.7 | 977 | -14.84 | -1.38 | **-0.23** | -25.85 |
| A/filter X=0bps | +1.65 | 0.39 | -11.7 | 291 | +13.95 | +1.14 | **0.28** | -11.20 |
| A/filter X=50bps | +0.65 | 0.21 | -11.9 | 380 | +1.00 | +0.09 | **0.04** | -11.62 |
| A/filter X=100bps | +0.18 | 0.08 | -9.7 | 483 | -0.61 | -0.05 | **-0.00** | -11.42 |
| B/composite lam=0.25 | -1.24 | -0.22 | -25.1 | 1020 | -13.20 | -1.22 | **-0.21** | -23.91 |
| B/composite lam=0.50 | -0.93 | -0.17 | -24.2 | 964 | -10.12 | -0.92 | **-0.16** | -21.32 |

## Best config in detail — A/filter X=0bps

| fold | IS window | OOS window | IS ret% | OOS ret% | OOS Sharpe |
|--:|---|---|--:|--:|--:|
| 0 | 2011-03-22→2014-02-12 | 2014-02-13→2015-02-02 | +8.45 | +10.22 | 1.82 |
| 1 | 2012-03-09→2015-02-02 | 2015-02-03→2016-01-21 | +17.31 | +5.61 | 0.93 |
| 2 | 2013-02-26→2016-01-21 | 2016-01-22→2017-01-10 | +20.76 | +0.43 | 0.13 |
| 3 | 2014-02-13→2017-01-10 | 2017-01-11→2017-12-28 | +16.85 | -4.89 | -1.34 |
| 4 | 2015-02-03→2017-12-28 | 2017-12-29→2018-12-17 | +1.58 | +2.75 | 0.74 |
| 5 | 2016-01-22→2018-12-17 | 2018-12-18→2019-12-05 | -1.49 | -0.02 | -0.00 |
| 6 | 2017-01-11→2019-12-05 | 2019-12-06→2020-11-24 | -2.50 | -4.18 | -1.86 |
| 7 | 2017-12-29→2020-11-24 | 2020-11-25→2021-11-12 | -2.23 | -1.71 | -0.41 |
| 8 | 2018-12-18→2021-11-12 | 2021-11-15→2022-11-01 | -5.12 | +18.01 | 2.67 |
| 9 | 2019-12-06→2022-11-01 | 2022-11-02→2023-10-20 | +10.28 | -5.65 | -1.23 |
| 10 | 2020-11-25→2023-10-20 | 2023-10-23→2024-10-09 | +8.22 | -3.10 | -1.10 |
| 11 | 2021-11-15→2024-10-09 | 2024-10-10→2025-09-29 | +5.14 | -1.83 | -0.89 |

Mean IS Sharpe 0.37 → pooled OOS Sharpe 0.28.


## Cost decomposition (gross → +trading → +swap = net, common window)


### Baseline ts_momentum (directional)

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | +0.63 | 0.14 | 0.83 | -0.33 | 5.0 | 86% | 219 |
| GBPUSD | -2.00 | 0.18 | 0.81 | -2.99 | 5.0 | 83% | 190 |
| USDJPY | +1.88 | 0.14 | 2.88 | -1.14 | 4.5 | 82% | 186 |
| AUDUSD | -1.51 | 0.34 | 0.80 | -2.65 | 5.0 | 78% | 175 |
| GOLD | +7.11 | 0.27 | 10.01 | -3.17 | 6.0 | 84% | 207 |
| **w-avg** | **+0.53** | **0.21** | **2.23** | **-1.90** | | | |

### Best carry-aware config — A/filter X=0bps

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | +1.08 | 0.08 | -0.18 | +1.19 | 5.0 | 41% | 104 |
| GBPUSD | +0.00 | 0.00 | 0.00 | +0.00 | 0.0 | 0% | 0 |
| USDJPY | +2.80 | 0.08 | -0.35 | +3.07 | 4.0 | 44% | 98 |
| AUDUSD | +0.00 | 0.00 | 0.00 | +0.00 | 0.0 | 0% | 0 |
| GOLD | -0.34 | 0.13 | -0.81 | +0.34 | 6.0 | 20% | 89 |
| **w-avg** | **+1.22** | **0.09** | **-0.41** | **+1.54** | | | |

## Honest reading of the best config (before anyone gets excited)

- **Effectively a 3-sleeve portfolio.** At this tolerance GBPUSD/AUDUSD never trade — both sides are carry-blocked across the whole window — so the diversification Phase 4 was built on is partly gone; what remains is credit-side momentum on EURUSD, USDJPY, GOLD.
- **Fold concentration.** 7 of 12 OOS folds are negative; the single best fold (2021-11-15→2022-11-01, +18.01%) contributes more than the whole pooled total (+13.95%). Remove that one fold and the pooled result is roughly flat-to-negative — one good carry year (the 2022 rate-hike regime) carries the result.
- **Multiple testing.** This is the best of 5 pre-registered configs on top of 29 prior OOS evaluations; at Sharpe 0.28 on ~12 folds it is nowhere near distinguishable from luck. The pre-registered gate (0.5) exists precisely so this number cannot be promoted by enthusiasm.
- What WOULD upgrade the evidence: a fresh never-tested holdout (new broker data as it accrues), a historical swap series instead of the constant-quote approximation, and the demo forward-test protocol in GUIDE.md §6 — none of which this phase authorises.


## Multiple-testing budget

5 new configs evaluated OOS (3 filter + 2 composite); the directional baseline dedupes to Phase 4's config. Registry count is now **34**.

```
MULTIPLE-TESTING COUNT: 34 distinct strategy+param config(s) have been evaluated against out-of-sample data (deduped across all logged runs).
  ⚠ The more configs you test, the more likely the best OOS result is LUCK.
  Rough intuition: at a 5% false-positive rate, ~1.7 of 34 configs would look like 'winners' by pure chance.
  A survivor needs a MUCH higher bar — or fresh, never-tested data — before it means anything. This is a counter + warning, not a significance test.
```


## Documented limitations (read before quoting numbers)

- **Constant swap points across history** (same as Phase 4b/5): today's quote applied to the whole backtest. On old data the per-night charge is a distorted fraction of notional — worst on GOLD's early years. The FILTER inherits this: which side is 'adverse' is set by TODAY's quote, so the filter is effectively a constant side-mask per instrument (modulated only by the price level crossing the threshold).
- **carry_z is a static cross-sectional constant** from one capture date. A live system would recompute it as brokers reprice swaps; no historical swap series exists to backtest that honestly.
- **The composite's momentum leg is demeaned** (that is what a z-score is): sign(z(mom)) ≠ sign(mom), so B is not 'baseline + tilt' — it is a related but distinct momentum definition, pre-registered as such.
- **The surface's carry axis is a structural no-op on EURUSD** (verifier finding): EURUSD's carry asymmetry (long −257, short +41 bps/yr) is extreme enough that X ∈ {0,50,100} never changes which side the filter allows, so the three columns are identical and the 27-cell plateau is really a 9-point lookback sweep tripled. The PLATEAU verdict holds on the 9-point basis (8/9 profitable); the portfolio OOS numbers are unaffected (the other four sleeves' carries do straddle the thresholds).
- Same fill/cost model caveats as every prior phase (FILL_MODEL.md).


## Verdict

**VERDICT: best pre-registered carry-aware config is A/filter X=0bps with net pooled OOS Sharpe 0.28 (ann +1.14%) vs the 0.5 gate — GATE NOT MET.**
