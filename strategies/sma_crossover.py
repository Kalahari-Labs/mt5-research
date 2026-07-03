"""sma_crossover.py — the first registered strategy: SMA fast/slow crossover.

This is the EXACT signal logic from the original strategy.py (numpy rolling mean,
+1 when fast>slow, -1 when fast<slow), moved behind the Strategy contract so the
engine is strategy-agnostic. The refactor guard test asserts it reproduces the
prior numbers bit-for-bit.

(`pandas-ta` would compute the same SMA; a rolling mean is exact either way.)
"""
from __future__ import annotations

import numpy as np

from config import STRATEGY
from .base import Strategy, Signals


def sma(values, period: int) -> np.ndarray:
    """Simple moving average; NaN during warm-up. No look-ahead: value at i uses
    only values[i-period+1 .. i]."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if period <= 0 or values.shape[0] < period:
        return out
    csum = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


class SmaCrossover(Strategy):
    name = "sma_crossover"

    def default_params(self) -> dict:
        return {"fast": STRATEGY.fast_period, "slow": STRATEGY.slow_period}

    def param_grid(self) -> dict:
        # Sweep used by robustness.py. fast<slow combos only (validate_params).
        return {"fast": (5, 8, 10, 12, 15, 20, 25, 30, 40, 50),
                "slow": (20, 30, 40, 50, 60, 80, 100, 150, 200)}

    def validate_params(self, fast, slow) -> bool:
        return 0 < int(fast) < int(slow)

    def generate(self, close, fast, slow) -> Signals:
        close = np.asarray(close, dtype=float)
        f = sma(close, int(fast))
        s = sma(close, int(slow))

        valid = ~np.isnan(f) & ~np.isnan(s)
        regime = np.zeros(close.shape, dtype=int)
        regime[valid & (f > s)] = 1
        regime[valid & (f < s)] = -1

        prev = np.roll(regime, 1)
        prev[0] = 0
        flipped = regime != prev
        entries = np.zeros(close.shape, dtype=int)
        entries[flipped & (regime == 1)] = 1
        entries[flipped & (regime == -1)] = -1
        return Signals(regime=regime, entries=entries)
