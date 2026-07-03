"""strategies/ — strategy registry.

Adding a strategy is exactly: ONE new file in this package implementing the
`Strategy` contract, and ONE `register(...)` line at the bottom of this file.
No engine, backtest, or walk-forward changes are required.
"""
from __future__ import annotations

from .base import Strategy, Signals
from .sma_crossover import SmaCrossover
from .buy_and_hold import BuyAndHold
from .ts_momentum import TsMomentum

_REGISTRY: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    if strategy.name in _REGISTRY:
        raise ValueError(f"strategy '{strategy.name}' is already registered")
    _REGISTRY[strategy.name] = strategy
    return strategy


def get(name: str) -> Strategy:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy '{name}'; registered: {all_names()}")


def all_names() -> list[str]:
    return sorted(_REGISTRY)


# --- registered strategies (one line each) ---
register(SmaCrossover())
register(BuyAndHold())
register(TsMomentum())
