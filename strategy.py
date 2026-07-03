"""strategy.py — DEPRECATED compatibility shim.

The strategy logic now lives in the `strategies/` package behind the Strategy
contract. This module re-exports the SMA pieces so any older import keeps working.
Prefer:  `import strategies; strat = strategies.get("sma_crossover")`.
"""
from __future__ import annotations

from strategies.base import Signals
from strategies.sma_crossover import SmaCrossover, sma

_SMA = SmaCrossover()


def generate_signals(close, fast_period, slow_period) -> Signals:
    """Back-compat wrapper around the registered SMA crossover strategy."""
    return _SMA.generate(close, fast=fast_period, slow=slow_period)
