# WALKFORWARD.md — Rolling Out-of-Sample Validation

- Symbol/TF: **EURUSD H24**, 7177 bars
- Window: **750 IS / 250 OOS**, step 250 (rolling, non-anchored)
- Selection: best **profit factor** in-sample, ≥ 8 IS trades to qualify
- Grid: lookback [20, 60, 120, 200, 252] × anchor [100, 200, 300]
- Costs: spread=0.8p, comm/lot=3.5, slip=0.2p, fill=next_open
- Folds: 25 (25 with an eligible param set)


## Per-fold (IS picks → OOS realized)

| # | IS window | best params | IS PF | IS tr | IS ret% | OOS ret% | OOS PF | OOS tr | OOS win% |
|--:|---|---|--:|--:|--:|--:|--:|--:|--:|
| 0 | 1999-01-04→2001-11-16 | 252/100 | 1.837 | 14 | +7.70 | +3.82 | 1.536 | 15 | 26.7 |
| 1 | 1999-12-20→2002-11-01 | 200/300 | 1.327 | 20 | +4.53 | +16.96 | inf | 1 | 100.0 |
| 2 | 2000-12-04→2003-10-17 | 60/300 | 5.406 | 9 | +21.84 | +1.27 | 1.269 | 13 | 30.8 |
| 3 | 2001-11-19→2004-10-01 | 120/300 | 3.882 | 10 | +9.63 | +6.92 | 6.972 | 4 | 75.0 |
| 4 | 2002-11-04→2005-09-16 | 120/300 | 1.050 | 12 | +0.43 | -0.59 | 0.920 | 9 | 33.3 |
| 5 | 2003-10-20→2006-09-01 | 120/200 | 1.196 | 22 | +2.54 | +1.59 | 1.484 | 5 | 40.0 |
| 6 | 2004-10-04→2007-08-20 | 20/300 | 1.518 | 21 | +4.34 | +6.57 | 1.852 | 12 | 41.7 |
| 7 | 2005-09-19→2008-08-06 | 200/100 | 16.628 | 9 | +17.67 | -1.62 | 0.768 | 10 | 30.0 |
| 8 | 2006-09-04→2009-07-24 | 120/200 | 6.599 | 10 | +22.79 | +9.67 | 33.301 | 3 | 66.7 |
| 9 | 2007-08-21→2010-07-13 | 120/100 | 3.767 | 14 | +28.54 | -4.47 | 0.595 | 13 | 15.4 |
| 10 | 2008-08-07→2011-06-29 | 60/200 | 2.987 | 18 | +17.72 | -0.17 | 0.981 | 14 | 28.6 |
| 11 | 2009-07-27→2012-06-14 | 120/300 | 1.357 | 10 | +3.40 | -12.94 | 0.008 | 20 | 10.0 |
| 12 | 2010-07-14→2013-05-31 | 120/100 | 0.672 | 32 | -6.95 | -3.31 | 0.422 | 11 | 27.3 |
| 13 | 2011-06-30→2014-05-19 | 60/100 | 0.863 | 37 | -2.06 | +18.10 | inf | 2 | 100.0 |
| 14 | 2012-06-15→2015-05-01 | 252/100 | 5.917 | 13 | +15.90 | -8.61 | 0.276 | 15 | 13.3 |
| 15 | 2013-06-03→2016-04-18 | 252/300 | 7.995 | 8 | +12.82 | -9.02 | 0.180 | 25 | 20.0 |
| 16 | 2014-05-20→2017-04-03 | 120/100 | 0.666 | 39 | -8.04 | +10.90 | 9.491 | 5 | 40.0 |
| 17 | 2015-05-04→2018-03-18 | 120/300 | 3.680 | 11 | +10.45 | -4.38 | 0.364 | 8 | 25.0 |
| 18 | 2016-04-19→2019-03-04 | 120/100 | 6.047 | 14 | +15.27 | -0.22 | 0.946 | 14 | 21.4 |
| 19 | 2017-04-04→2020-02-13 | 120/200 | 6.778 | 11 | +5.98 | +2.44 | 1.415 | 6 | 16.7 |
| 20 | 2018-03-19→2021-01-26 | 120/300 | 3.092 | 8 | +6.06 | +1.12 | 1.223 | 11 | 36.4 |
| 21 | 2019-03-05→2022-01-06 | 120/300 | 2.679 | 12 | +7.39 | +6.30 | 7.174 | 3 | 33.3 |
| 22 | 2020-02-14→2022-12-19 | 120/100 | 4.629 | 13 | +16.66 | -1.02 | 0.668 | 9 | 44.4 |
| 23 | 2021-01-27→2023-11-30 | 120/200 | 2.963 | 9 | +8.92 | -7.81 | 0.263 | 23 | 21.7 |
| 24 | 2022-01-07→2024-11-12 | 20/100 | 1.080 | 46 | +1.18 | +5.11 | 2.539 | 16 | 43.8 |

## Aggregate: in-sample expectation vs out-of-sample reality

**mean IS** = average across eligible folds (IS windows OVERLAP, so they can only be averaged). **OOS** = trade-**pooled** from the continuous curve (OOS windows are disjoint → pooling is valid and matches the headline). Returns are **annualised** so the ~6-mo IS and ~1-mo OOS horizons compare; expectancy is % of starting equity per trade.

| Metric | mean IS | OOS (pooled) | degradation |
|---|--:|--:|---|
| Return % (ann.) | +2.95 | +1.25 | -1.71 |
| Win rate % | 40.14 | 28.46 | -11.67 |
| Profit factor | 3.784 | 1.181 | -2.603 |
| Expectancy/trade % | +0.8294 | +0.1293 | -0.7001 |
| Max drawdown % | -8.51 | -26.49 | -17.97 |

> ⚠ Statistical trap: the **naive per-fold OOS mean** profit factor is 3.245 and win rate 37.7% — far rosier than the pooled 1.181 / 28.5%. Averaging ratios across short windows is upward-biased; the trade-pooled continuous curve is the figure to trust.

## Headline: the continuous OOS equity curve (the honest number)

Every OOS segment chained end-to-end — this is the only number that was never optimised on:

- Total OOS return: **+34.52%** (start 10,000 → end 13,452)
- OOS profit factor: **1.181**, win rate 28.5%, expectancy/trade +12.93
- OOS max drawdown: **-26.49%**, total OOS trades: 267

## Reading this

- IS→OOS degradation is expected and is the whole point: it quantifies how much the in-sample pick was flattered by fitting.
- For SMA(20/50)-style crossovers, OOS is expected to land **worse than IS and likely still negative**. That is the tool working correctly, not a bug to fix.
- Walk-forward controls *parameter* overfitting only. Trying many strategy ideas re-introduces selection bias across ideas — track how many you tried.
- Set realistic costs to your broker's real figures (see FILL_MODEL.md) before trusting absolute OOS returns.
