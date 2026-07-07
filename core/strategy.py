"""core.strategy — the strategy plugin contract and its recommendation output.

Strategies NEVER execute trades. They analyze market data and return a
`Recommendation` (side + confidence + reasoning + metadata). The decision engine
(Phase 6) is the only thing that turns recommendations into actions, and the
broker adapter (Phase 2) is the only thing that places them.

Two strategy families exist today and both are legitimate `Strategy` plugins
(they carry a stable `name`):
  * root `strategies.base.Strategy.generate(close, **params) -> Signals`  (vectorised research)
  * executor `strategies.Strategy.decide(bars, i) -> Signal`              (per-bar live)

`Recommendation` is the UNIFIED output both families migrate toward in Phase 5.
It is defined here now so the decision engine and tests can rely on one shape;
no existing strategy is forced to change in Phase 1.

Contract + one value object — still no trading behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

SIDES = ("buy", "sell")


@dataclass(frozen=True)
class Recommendation:
    """A strategy's advisory output for one symbol at one moment.

    side        "buy" | "sell"
    confidence  0.0..1.0 — the strategy's own conviction, NOT a probability of profit
    reasoning   human-readable why, always present (auditable)
    metadata    free-form extras (indicator values, tags, regime, ...)
    sl / tp     optional protective prices when the strategy has structure for them
    """
    side: str
    confidence: float
    reasoning: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sl: float | None = None
    tp: float | None = None

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError("side must be one of %s, got %r" % (SIDES, self.side))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1], got %r" % (self.confidence,))
        if not self.reasoning:
            raise ValueError("reasoning is mandatory — a recommendation must be explainable")


@runtime_checkable
class Strategy(Protocol):
    """Plugin identity. Structural: any object carrying a `name` qualifies, so
    both existing strategy families conform as-is. Phase 5 adds a uniform
    `recommend(...) -> Recommendation` method across implementations."""

    name: str
