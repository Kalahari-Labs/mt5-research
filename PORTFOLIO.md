# PORTFOLIO.md — Cross-Asset Diversification of the Fixed Momentum Edge

- Signal: **ts_momentum**, ONE fixed param set applied UNIFORMLY to every instrument: **lookback=120, anchor=200** (the D1 plateau centre). Params are NOT fitted per instrument — only diversification is under test.

- Costs: realistic spread/slippage/commission **+ conservative overnight swap** per instrument (a documented approximation — see config.INSTRUMENT_COSTS).

- Common window (all kept sleeves live): **2011-03-22 → 2026-06-29**, 3942 D1 bars.


## Instruments used vs dropped

| sleeve | D1 bars | range | trades | swap %/yr | weight | risk contrib |
|---|--:|---|--:|--:|--:|--:|
| EURUSD | 7177 | 1999-01-04→2026-06-29 | 219 | 2.0 | 24.1% | 1.00× med |
| GBPUSD | 5000 | 2007-05-11→2026-06-29 | 190 | 2.0 | 22.4% | 1.00× med |
| USDJPY | 5000 | 2007-05-11→2026-06-29 | 186 | 1.5 | 20.8% | 1.00× med |
| AUDUSD | 4000 | 2011-03-21→2026-06-29 | 175 | 2.5 | 21.2% | 1.00× med |
| GOLD | 6519 | 2001-06-04→2026-06-29 | 207 | 4.0 | 11.5% | 1.00× med |
| ~~US500Cash~~ | — | DROPPED: only 700 D1 bars (< 2000) | | | | |
| ~~OILCash~~ | — | DROPPED: only 1200 D1 bars (< 2000) | | | | |

## Portfolio vs single-EURUSD (same common window)

| metric | PORTFOLIO | single EURUSD | Δ |
|---|--:|--:|--:|
| Annualised return % | -1.19 | -1.23 | +0.03 |
| Total return % | -16.65 | -17.05 | +0.40 |
| Sharpe (annualised) | -0.20 | -0.13 | -0.07 |
| Max drawdown % | -21.26 | -24.09 | +2.83 |
| Profit factor (daily) | 0.966 | 0.977 | |
| Trades (total) | 977 | 219 | |

**Diversification effect:** portfolio Sharpe **-0.20** vs average single-sleeve Sharpe **-0.12** (Δ -0.08).

> Continuity: single-EURUSD over its FULL history (swap ON) = -6.7% total, Sharpe 0.01, maxDD -29.6%, 219 trades. The swap-OFF full-history number is the Phase-3 figure (+47.99%); the gap is the overnight-financing drag alone.


## Sleeve return-correlation matrix (common window)

| | EURUSD | GBPUSD | USDJPY | AUDUSD | GOLD |
|---|---|---|---|---|---|
| **EURUSD** | +1.00 | +0.46 | +0.17 | +0.26 | +0.15 |
| **GBPUSD** | +0.46 | +1.00 | +0.13 | +0.26 | +0.12 |
| **USDJPY** | +0.17 | +0.13 | +1.00 | +0.12 | +0.18 |
| **AUDUSD** | +0.26 | +0.26 | +0.12 | +1.00 | +0.09 |
| **GOLD** | +0.15 | +0.12 | +0.18 | +0.09 | +1.00 |

Mean pairwise (off-diagonal) correlation: **+0.20** — the lower this is, the more genuine diversification the basket carries.


## Walk-forward: pooled OUT-OF-SAMPLE portfolio (the honest estimate)

- Window: **750 IS / 250 OOS** bars, fixed params, inverse-vol weights re-estimated on each IS window and applied to OOS (OOS strictly after IS).

- Pooled OOS: total **-8.05%**, ann -0.72%, Sharpe **-0.11**, maxDD -18.79%, PF(daily) 0.982.

- Mean IS Sharpe -0.15 → pooled OOS Sharpe -0.11 (degradation +0.04).


| fold | IS window | OOS window | IS ret% | OOS ret% | OOS Sharpe |
|--:|---|---|--:|--:|--:|
| 0 | 2011-03-22→2014-02-12 | 2014-02-13→2015-02-02 | -6.33 | +6.36 | 1.37 |
| 1 | 2012-03-09→2015-02-02 | 2015-02-03→2016-01-21 | +2.17 | -0.08 | 0.01 |
| 2 | 2013-02-26→2016-01-21 | 2016-01-22→2017-01-10 | +8.47 | -5.22 | -1.02 |
| 3 | 2014-02-13→2017-01-10 | 2017-01-11→2017-12-28 | +1.20 | -4.93 | -1.13 |
| 4 | 2015-02-03→2017-12-28 | 2017-12-29→2018-12-17 | -9.14 | -0.21 | -0.01 |
| 5 | 2016-01-22→2018-12-17 | 2018-12-18→2019-12-05 | -9.89 | -1.72 | -0.53 |
| 6 | 2017-01-11→2019-12-05 | 2019-12-06→2020-11-24 | -7.45 | -2.13 | -0.31 |
| 7 | 2017-12-29→2020-11-24 | 2020-11-25→2021-11-12 | -5.25 | -0.50 | -0.11 |
| 8 | 2018-12-18→2021-11-12 | 2021-11-15→2022-11-01 | -5.79 | +12.50 | 1.85 |
| 9 | 2019-12-06→2022-11-01 | 2022-11-02→2023-10-20 | +8.23 | -9.08 | -1.52 |
| 10 | 2020-11-25→2023-10-20 | 2023-10-23→2024-10-09 | -0.11 | -2.29 | -0.50 |
| 11 | 2021-11-15→2024-10-09 | 2024-10-10→2025-09-29 | -1.95 | +0.59 | 0.13 |

## Verdict

  DIVERSIFICATION DOES what it should mechanically: portfolio maxDD -21.3% is shallower than single-EURUSD -24.1% (+2.8 pts), and sleeves are only weakly correlated.
  Portfolio Sharpe -0.20 vs average single-sleeve -0.12 (Δ -0.08): risk-parity scales the Sharpe MAGNITUDE up, so when the post-swap edge is negative the loss only becomes more reliable, not smaller.
  After conservative overnight swap the portfolio is NOT profitable on the common window (ann -1.19%); financing erases the thin momentum edge. This momentum edge is NOT retail-tradeable as built.


## Reading this

- Uniform fixed params (no per-instrument fitting) keep the multiple-testing count low BY DESIGN: only ONE strategy+param config is evaluated OOS here.
- Overnight swap is a conservative SYMMETRIC drag (charged on either side). Real broker swaps are directional (long vs short quoted separately) and occasionally a credit — e.g. GOLD swap long −90.35 / short +11.15 pts on this account — so the true cost for a strategy that shorts is somewhat LESS than modelled here. We took the worst case on purpose: if the edge dies under it, that is a real finding.
- Diversification can only help risk-adjusted return if sleeve returns are weakly correlated. The corr matrix above is the evidence; the pooled OOS curve is the only number never optimised on.
