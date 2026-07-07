"""carry_momentum.py — Phase 6: carry-AWARE time-series momentum. Same Strategy
contract as everything else; the ONLY thing that changes vs ts_momentum is the
SIGNAL's awareness of directional overnight financing (the broker's real
swap_long / swap_short quotes). The engine, costs, portfolio and walk-forward
machinery are untouched; ts_momentum's own components are REUSED, not copied.

WHY (pre-registered hypothesis — see PHASE6.md for the full brief):
Phase 4/4b: the D1 TSMOM portfolio edge is gross-positive but financing kills it.
Phase 5: cycling faster loses (turnover grows faster than swap savings) because
this signal family is ~85% in-market at every horizon. The untested gap between
them: keep the holding period, make the SIGNAL swap-aware — refuse to hold the
side of a market whose financing bleeds more than a pre-registered tolerance
(A), or tilt the direction toward the side financing favours (B).

TWO PRE-REGISTERED VARIANTS (no others):
  A) mode="filter" — generate the EXACT ts_momentum regime (same lookback /
     anchor machinery; byte-equal when the gate is off), then FLATTEN any bar
     whose held-side financing is worse than -X bps/yr:
        hold long  at t only if carry_long_bps[t]  >= -X
        hold short at t only if carry_short_bps[t] >= -X
     X = max_adverse_carry_bps ∈ {0, 50, 100}.
  B) mode="composite" — score[t] = z(momentum)[t] + lam * carry_z, direction =
     sign(score), then the SAME anchor trend-confirmation overlay as
     ts_momentum. z(momentum) is the CAUSAL EXPANDING z-score of the trailing
     lookback return (population std; 0 while undefined). carry_z is a
     per-instrument CONSTANT supplied by the caller: the cross-sectional
     z-score (across the Phase-4 basket) of the instrument's signed net carry
     (long-favouring, bps/yr) — computed OUTSIDE the strategy because a
     single-sleeve strategy cannot see the basket. lam ∈ {0.25, 0.5}.

CARRY MATH (oracle-tested):
    carry_bps_per_year(swap_per_night, close, nights_per_year=365)
        = swap_per_night * nights_per_year / close * 10_000
swap_per_night is the broker's REAL quote in PRICE units per unit per night
(config.load_swap_spec: swap points × point size; negative = cost, positive =
credit). 365 nights/yr because the directional engine charges Mon–Fri with a 3×
day = 7 rollover nights per calendar week. Dividing by close[t] makes it the
honest INSTANTANEOUS financing rate on notional at bar t — a per-bar, causal
series. The swap QUOTE itself is today's, applied across history: the same
documented approximation as Phase 4b/5 (see SHORTHOLDS.md limitations).

NO LOOK-AHEAD: every component (the ts_momentum regime, the expanding z, the
per-bar carry) uses only close[0..t] and the swap input up to t. The unit tests
prove truncation invariance on BOTH the close series and the carry input.

NO I/O here: swap quotes enter as explicit params — scalars in production
(phase6.py passes each sleeve's spec), arrays allowed for tests and for a future
historical swap series. Defaults (zero swap, X=0) make the gate a structural
no-op, so carry_momentum with defaults reproduces ts_momentum exactly.
"""
from __future__ import annotations

import numpy as np

from config import STRATEGY
from .base import Strategy, Signals
from .ts_momentum import TsMomentum, ema, trailing_return, _entries_from_regime


def carry_bps_per_year(swap_per_night, close, nights_per_year: float = 365.0) -> np.ndarray:
    """Directional financing rate in bps/yr for holding ONE unit of the base
    asset, marked against the concurrent close. Broker sign convention kept:
    negative = cost, positive = credit. `swap_per_night` may be a scalar
    (production: today's quote applied across history) or a per-bar array
    (tests / future historical swap series). Causal: element t uses close[t]."""
    close = np.asarray(close, dtype=float)
    s = np.broadcast_to(np.asarray(swap_per_night, dtype=float), close.shape)
    return s * float(nights_per_year) / close * 1e4


def expanding_zscore(values) -> np.ndarray:
    """Causal expanding z-score: z[t] = (x[t] − mean(x[0..t])) / std(x[0..t]),
    population std (ddof=0); 0.0 wherever the std is 0 (including t=0). z[t]
    uses NOTHING after t — vectorised with cumulative sums, loop-oracle-tested."""
    x = np.asarray(values, dtype=float)
    n = x.shape[0]
    if n == 0:
        return np.empty(0)
    k = np.arange(1.0, n + 1.0)
    mean = np.cumsum(x) / k
    var = np.cumsum(x * x) / k - mean * mean      # may dip <0 by float error
    std = np.sqrt(np.clip(var, 0.0, None))
    z = np.zeros(n)
    ok = std > 0
    z[ok] = (x[ok] - mean[ok]) / std[ok]
    return z


class CarryMomentum(Strategy):
    name = "carry_momentum"

    def default_params(self) -> dict:
        return {"lookback": STRATEGY.mom_lookback, "anchor": STRATEGY.mom_anchor,
                "allow_short": STRATEGY.mom_allow_short,
                "use_anchor": STRATEGY.mom_use_anchor,
                "mode": "filter", "max_adverse_carry_bps": 0.0,
                "swap_long_per_night": 0.0, "swap_short_per_night": 0.0,
                "nights_per_year": 365.0, "lam": 0.0, "carry_z": 0.0}

    def param_grid(self) -> dict:
        # 2-param robustness surface (robustness.py requires exactly 2 keys):
        # the Phase-3 TSMOM horizon grid × the PRE-REGISTERED carry tolerances.
        # anchor stays at its default (200) exactly as Phase 3 fixed non-surface
        # params. NOT a licence to fish: the tolerances are the brief's {0,50,100}.
        return {"lookback": (20, 40, 60, 90, 120, 150, 180, 210, 252),
                "max_adverse_carry_bps": (0.0, 50.0, 100.0)}

    def wf_grid(self) -> dict:
        # A walk-forward may search ONLY the pre-registered filter tolerances at
        # the fixed Phase-4 horizon — anything wider would be parameter fishing.
        return {"lookback": (120,),
                "max_adverse_carry_bps": (0.0, 50.0, 100.0)}

    def validate_params(self, lookback, max_adverse_carry_bps=None, **_) -> bool:
        ok = int(lookback) >= 2
        if max_adverse_carry_bps is not None:
            ok = ok and float(max_adverse_carry_bps) >= 0.0
        return ok

    def warmup_bars(self, **params) -> int:
        # NOT the base-class default: bps tolerances and swap quotes are
        # magnitudes, not window lengths — they must never inflate the warmup.
        lb = int(params.get("lookback", STRATEGY.mom_lookback))
        an = int(params.get("anchor", STRATEGY.mom_anchor))
        use_anchor = params.get("use_anchor", STRATEGY.mom_use_anchor)
        return max(lb, an) if use_anchor else lb

    def generate(self, close, lookback=None, anchor=None, allow_short=True,
                 use_anchor=True, mode="filter",
                 swap_long_per_night=0.0, swap_short_per_night=0.0,
                 nights_per_year=365.0, max_adverse_carry_bps=None,
                 lam=0.0, carry_z=0.0) -> Signals:
        close = np.asarray(close, dtype=float)
        n = close.shape[0]
        lookback = int(STRATEGY.mom_lookback if lookback is None else lookback)
        anchor = int(STRATEGY.mom_anchor if anchor is None else anchor)
        warmup = max(lookback, anchor) if use_anchor else lookback
        if n <= warmup:
            zero = np.zeros(n, dtype=int)
            return Signals(regime=zero, entries=zero.copy())

        if mode == "filter":
            # A) the EXACT ts_momentum regime (reused, not re-implemented) …
            regime = TsMomentum().generate(
                close, lookback=lookback, anchor=anchor,
                allow_short=allow_short, use_anchor=use_anchor).regime.copy()
            # … then flatten any bar whose held side bleeds beyond the tolerance.
            if max_adverse_carry_bps is not None:
                thr = -float(max_adverse_carry_bps)
                c_long = carry_bps_per_year(swap_long_per_night, close, nights_per_year)
                c_short = carry_bps_per_year(swap_short_per_night, close, nights_per_year)
                regime[(regime == 1) & (c_long < thr)] = 0
                regime[(regime == -1) & (c_short < thr)] = 0
        elif mode == "composite":
            # B) momentum leg: causal expanding z of the trailing return.
            tr = trailing_return(close, lookback)
            z = np.zeros(n)
            z[lookback:] = expanding_zscore(tr[lookback:])
            score = z + float(lam) * float(carry_z)
            regime = np.zeros(n, dtype=int)
            regime[score > 0] = 1
            regime[score < 0] = -1 if allow_short else 0
            # same trend-confirmation overlay as ts_momentum
            if use_anchor:
                anchor_ema = ema(close, anchor)
                regime[(regime == 1) & ~(close > anchor_ema)] = 0
                regime[(regime == -1) & ~(close < anchor_ema)] = 0
            regime[:warmup] = 0
        else:
            raise ValueError(f"unknown mode '{mode}' (use 'filter' or 'composite')")

        return Signals(regime=regime, entries=_entries_from_regime(regime))
