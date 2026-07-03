# SHORTHOLDS.md — Phase 5: Short-Hold Momentum on H4

**Question (fixed a priori):** does the ratio of gross edge to financing cost improve as hold time shrinks — and enough to clear ALL costs? A clean negative is a complete result.

**Cost model:** full realistic trading stack per trade + Phase-4b DIRECTIONAL swap (real broker swap_long/swap_short per instrument, triple-swap **Wednesday**, weekend nights free). Stricter than Phase 4, not looser.


## Per-instrument H4 depth (blocker check: ≥2,000 bars, ~5yr common window)

| sleeve | H4 bars | range |
|---|--:|---|
| EURUSD | 20000 | 2013-08-21 → 2026-07-02 |
| GBPUSD | 20000 | 2013-08-21 → 2026-07-02 |
| USDJPY | 20000 | 2013-08-21 → 2026-07-02 |
| AUDUSD | 20000 | 2013-08-19 → 2026-07-02 |
| GOLD | 20000 | 2013-07-09 → 2026-07-02 |

All five sleeves returned the probe-ladder maximum (20,000 bars ≈ 12.8 years) — far beyond the 2,000-bar / 5-year gate. **No data blocker.**


## D1 bridge reference (Phase 4 re-run under the DIRECTIONAL swap model)

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | +1.56 | 0.13 | 0.94 | +0.49 | 5.0 | 86% | 219 |
| GBPUSD | -1.02 | 0.17 | 0.75 | -1.95 | 5.0 | 83% | 190 |
| USDJPY | +1.17 | 0.15 | 3.51 | -2.49 | 4.5 | 82% | 186 |
| AUDUSD | -1.60 | 0.34 | 0.79 | -2.73 | 5.0 | 78% | 175 |
| GOLD | +7.85 | 0.41 | 24.97 | -17.53 | 6.0 | 84% | 207 |
| **w-avg** | **+0.89** | **0.22** | **3.97** | **-3.30** | | | |

D1 portfolio (directional swap): ann **-1.62%**, Sharpe -0.28, maxDD -26.7%; pooled WF OOS Sharpe -0.23.


## Kill criterion (evaluated on C3, the bridge case, BEFORE C1/C2)

- Swap savings from H4's shorter holds: **+1.80 %/yr**
- EXTRA execution cost from H4 turnover: **+0.97 %/yr**
- Margin (savings − extra cost): **+0.82 %/yr** → survives to C1/C2


## C1 (30/50 H4) — lookback 30 / anchor 50 @H4

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold bars | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | -1.74 | 1.92 | 0.84 | -4.50 | 4 | 0.8 | 86% | 1507 |
| GBPUSD | -0.94 | 2.20 | 0.85 | -3.99 | 4 | 0.7 | 86% | 1544 |
| USDJPY | +2.19 | 1.59 | 3.00 | -2.40 | 3 | 0.7 | 85% | 1493 |
| AUDUSD | -4.41 | 3.80 | 0.80 | -9.01 | 4 | 0.7 | 85% | 1639 |
| GOLD | +6.22 | 3.08 | 8.48 | -5.33 | 4 | 0.7 | 86% | 1465 |
| **w-avg** | **-0.21** | **2.40** | **2.26** | **-4.87** | 4 | 0.7 | | |

| metric | PORTFOLIO | best sleeve (USDJPY) |
|---|--:|--:|
| Annualised return % | -4.66 | -2.61 |
| Sharpe | -0.86 | -0.29 |
| Max drawdown % | -47.63 | -32.15 |
| Profit factor (per-bar) | 0.936 | 0.977 |
| Trades (total) | 7648 | 1493 |

Pooled walk-forward OOS (4500 IS / 1500 OOS H4 bars, causal inverse-vol weights, 10 folds): total **-38.93%**, ann -5.00%, Sharpe **-0.92**, maxDD -41.04%.


**Robustness verdict (EURUSD H4 surface, full costs): NO-EDGE (by neighbours)** — lookback 30 is not a canonical grid column; its bracketing cells at anchor 50 both lose badly (20/50: −49.85%, 40/50: −55.02%), so the C1 cell sits inside a losing region.


## C2 (60/100 H4) — lookback 60 / anchor 100 @H4

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold bars | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | -2.51 | 1.43 | 0.81 | -4.76 | 4 | 0.7 | 84% | 1139 |
| GBPUSD | -2.33 | 1.49 | 0.83 | -4.66 | 4 | 0.7 | 85% | 1059 |
| USDJPY | +2.22 | 1.13 | 2.90 | -1.81 | 4 | 0.8 | 85% | 1050 |
| AUDUSD | -2.31 | 2.79 | 0.83 | -5.93 | 3 | 0.7 | 85% | 1167 |
| GOLD | +1.12 | 2.13 | 8.13 | -9.13 | 4 | 0.7 | 85% | 1077 |
| **w-avg** | **-0.93** | **1.73** | **2.21** | **-4.86** | 4 | 0.7 | | |

| metric | PORTFOLIO | best sleeve (USDJPY) |
|---|--:|--:|
| Annualised return % | -4.63 | -2.05 |
| Sharpe | -0.86 | -0.21 |
| Max drawdown % | -47.39 | -41.37 |
| Profit factor (per-bar) | 0.936 | 0.982 |
| Trades (total) | 5492 | 1050 |

Pooled walk-forward OOS (4500 IS / 1500 OOS H4 bars, causal inverse-vol weights, 10 folds): total **-36.13%**, ann -4.56%, Sharpe **-0.84**, maxDD -37.40%.


**Robustness verdict (EURUSD H4 surface, full costs): NO-EDGE** — cell itself loses (-46.47%)


## C3 (bridge, 120/200 H4) — lookback 120 / anchor 200 @H4

| sleeve | gross %/yr | trading drag | swap drag | NET %/yr | med hold bars | med hold (days) | in-mkt | trades |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| EURUSD | -1.83 | 0.94 | 0.80 | -3.58 | 5 | 1.0 | 85% | 738 |
| GBPUSD | -1.33 | 1.01 | 0.85 | -3.18 | 5 | 1.0 | 85% | 705 |
| USDJPY | +1.04 | 0.86 | 2.86 | -2.68 | 4 | 0.8 | 85% | 800 |
| AUDUSD | -1.68 | 1.92 | 0.85 | -4.45 | 3 | 0.7 | 85% | 794 |
| GOLD | +0.95 | 1.50 | 8.05 | -8.60 | 4 | 0.7 | 84% | 756 |
| **w-avg** | **-0.71** | **1.19** | **2.17** | **-4.08** | 4 | 0.8 | | |

| metric | PORTFOLIO | best sleeve (USDJPY) |
|---|--:|--:|
| Annualised return % | -3.85 | -2.84 |
| Sharpe | -0.70 | -0.32 |
| Max drawdown % | -43.93 | -47.83 |
| Profit factor (per-bar) | 0.948 | 0.974 |
| Trades (total) | 3793 | 800 |

Pooled walk-forward OOS (4500 IS / 1500 OOS H4 bars, causal inverse-vol weights, 10 folds): total **-32.79%**, ann -4.05%, Sharpe **-0.73**, maxDD -33.71%.


**Robustness verdict (EURUSD H4 surface, full costs): NO-EDGE** — cell itself loses (-37.30%)


## Mechanism check (was the hypothesis even mechanically possible?)

Time-series momentum is in the market almost continuously (long OR short whenever the anchor filter agrees) — so financing accrues per NIGHT IN THE MARKET, not per trade. Shorter lookbacks shorten the per-trade hold but barely change nights-in-market per year; the in-mkt column above is the evidence. Swap can only shrink via (a) more flat time from the anchor filter, or (b) the directional model paying credits on the side held. Neither is a 'shorter holds pay less swap' effect — the hypothesis's core mechanism does not exist for an always-in-market signal family.


## Documented limitations (read before quoting numbers)

- **Constant swap points across history.** The directional model applies TODAY's
  broker quote (price units per night) to the whole backtest. Swap points scale
  with rates and price levels, so on old data the charge is distorted — most
  visibly GOLD on the D1 reference (window starts 2011 when gold traded far below
  today's level, making a fixed dollar-per-night charge a much larger fraction of
  notional → the 24.97%/yr D1 GOLD swap drag is overstated for the early years).
  The H4 windows start 2013 and are less affected. Directionally the conclusion is
  robust: even with swap set to ZERO (the gross column), H4 configs barely beat —
  or lose to — their own trading costs.
- **Part of the "swap savings" in the kill check is window/price-level effect**,
  not genuine hold-shortening: the D1 reference runs 2011→2026, the H4 runs
  2013→2026, and GOLD dominates the difference. The mechanism check above is the
  cleaner evidence: time-in-market stays ~85% at every horizon, so real financing
  savings from faster cycling are structurally near zero for this signal family.
- **Median holds are short even on D1** (3–6 days): the sign-flip signal whipsaws
  around zero, so the median trade is chop while the tail carries the edge. The
  hypothesis's premise ("weeks-long holds" on D1) was true for the mean, not the
  median.

## Verdict

**Gate NOT met** (portfolio Sharpe ~0.5+ net of ALL costs on pooled OOS required). Per the brief: log it, stop. No live wiring, no n8n, no dashboard. Short-hold momentum on H4 does NOT outrun financing — turnover costs scale faster than swap savings, exactly the failure mode the brief flagged as the hypothesis's known enemy.

Every H4 config is net-negative on every sleeve; pooled OOS Sharpe −0.73/−0.84/−0.92 (C3/C2/C1); gross w-avg edge is already ≤ 0 on H4 before a single cost is charged. The ratio the brief asked about moves the WRONG way: shortening the horizon shrank the gross edge (−0.7 to −0.9%/yr w-avg vs +0.9%/yr on D1) while multiplying turnover cost (0.22 → 1.2–2.4%/yr). Multiple-testing count: 29, as budgeted (26 + 3 H4 configs; the D1 directional re-run dedupes to Phase 4's config).
