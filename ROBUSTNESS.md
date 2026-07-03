# ROBUSTNESS.md — Parameter-Robustness Surface

- Strategy: **ts_momentum**, EURUSD H24, 7177 bars (1999-01-04→2026-06-29)
- Metric per cell: **total return %**, realistic costs, IN-SAMPLE over all data
- Grid: lookback [20, 40, 60, 90, 120, 150, 180, 210, 252] × anchor [50, 100, 150, 200, 250, 300]
- Trust threshold: ≥ 20 trades per cell

## Surface (text heatmap)
```
  rows = lookback  (top→bottom)   cols = anchor  (left→right)
          50 100 150 200 250 300
     20    #   #   #   #   #   #
     40    #   #   #   #   #   #
     60    #   #   #   #   #   #
     90    #   #   #   #   @   #
    120    #   #   #   #   #   #
    150    #   #   #   #   #   #
    180    #   #   #   #   #   #
    210    #   #   #   #   #   #
    252    +   #   #   #   #   #

  legend: @ best   # >=+2%   + 0..2%   - -2..0%   . <-2%   ~ <min-trades   (blank) invalid
```

## Top combos

| rank | lookback/anchor | return % | PF | trades |
|--:|---|--:|--:|--:|
| 1 | 90/250 | +61.74 | 1.307 | 234 |
| 2 | 120/250 | +56.10 | 1.359 | 200 |
| 3 | 90/150 | +52.29 | 1.249 | 284 |
| 4 | 120/150 | +50.83 | 1.283 | 250 |
| 5 | 90/200 | +50.46 | 1.256 | 251 |
| 6 | 180/250 | +48.09 | 1.307 | 212 |
| 7 | 120/200 | +47.99 | 1.296 | 219 |
| 8 | 210/250 | +44.38 | 1.272 | 202 |
| 9 | 40/250 | +44.31 | 1.193 | 335 |
| 10 | 90/100 | +44.28 | 1.201 | 330 |

## Verdict

**VERDICT: PLATEAU — best lookback/anchor = 90/250 @ +61.74%; 54/54 cells profitable (100%), largest contiguous profitable block = 54 cells.  Broad plateau — more likely a real effect than a fit artefact.**

## Reading this

- A **SPIKE** means the strategy only 'works' at one lucky parameter setting and loses at its neighbours — almost always curve-fitting. Do NOT trust it.
- A **PLATEAU** (broad contiguous profitable block) is necessary-but-not-sufficient evidence of a real effect; still confirm out-of-sample (walkforward.py).
- **NO EDGE** is the honest, common result — most simple ideas have none after costs.
- This sweep is in-sample over the whole set; it measures parameter sensitivity, not out-of-sample performance.
