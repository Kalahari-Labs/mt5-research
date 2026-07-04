"""core — shared, dependency-free contracts for the Kalahari Labs platform.

This package contains INTERFACES ONLY. It imports nothing outside the Python
standard library and defines no trading behavior, so importing it can never
change runtime behavior or add a dependency. The three existing subsystems
(root research, intel plane, intel/executor) converge on these contracts
incrementally; new code (broker adapters, the decision engine) is written
against them directly.

See docs/ARCHITECTURE.md §5 for the migration policy. The behavioral contracts
are `@runtime_checkable` Protocols, so existing classes satisfy them AS-IS —
`tests/test_core_contracts.py` fails CI if any implementation ever drifts.
"""
from __future__ import annotations

from .broker import BrokerAdapter, OrderResult
from .decision import Action, Decision
from .market_data import MarketDataProvider
from .risk import RiskManager, RiskVerdict
from .strategy import Recommendation, Strategy

__all__ = [
    "MarketDataProvider",
    "BrokerAdapter",
    "OrderResult",
    "RiskManager",
    "RiskVerdict",
    "Strategy",
    "Recommendation",
    "Action",
    "Decision",
]
