"""buy_and_hold.py — trivial placeholder strategy (demonstrates that adding a
strategy is ONE file + ONE registry line). Always long, no params. NOT a real
research subject; SMA stays the active test subject.
"""
from __future__ import annotations

import numpy as np

from .base import Strategy, Signals


class BuyAndHold(Strategy):
    name = "buy_and_hold"

    def generate(self, close, **params) -> Signals:
        n = np.asarray(close).shape[0]
        regime = np.ones(n, dtype=int)        # always long
        entries = np.zeros(n, dtype=int)
        if n:
            entries[0] = 1                     # single entry on the first bar
        return Signals(regime=regime, entries=entries)
