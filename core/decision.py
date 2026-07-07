"""core.decision — the central decision contract (Phase 6 output).

The decision engine consumes strategy `Recommendation`s plus market regime,
volatility, spread, news, portfolio exposure, drawdown, session filters, and
account state, and emits exactly one `Decision` per (symbol) evaluation:

    BUY | SELL | WAIT | IGNORE

Every decision carries an explanation and the factors that produced it, so the
"why" of every action AND every non-action is auditable — the same discipline
the executor already applies to its journal (every skip is logged with a reason).

This module defines the value objects only; the engine that produces them lands
in Phase 6. No trading behavior here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Action(str, Enum):
    """The four terminal decisions. WAIT = conditions may change soon (re-evaluate
    next bar); IGNORE = actively rejected now (e.g. a hard veto)."""
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    IGNORE = "IGNORE"


ACTIONABLE = (Action.BUY, Action.SELL)


@dataclass(frozen=True)
class Decision:
    """One explainable decision for one symbol.

    action      BUY | SELL | WAIT | IGNORE
    symbol      the instrument evaluated
    confidence  0.0..1.0 aggregate conviction behind the action
    explanation human-readable summary — ALWAYS present, for actions and non-actions alike
    factors     the inputs that produced it (regime, spread, news, exposure, vetoes, votes, ...)
    strategy    the contributing strategy/strategies, when a single one dominates
    """
    action: Action
    symbol: str
    explanation: str
    confidence: float = 0.0
    factors: dict[str, Any] = field(default_factory=dict)
    strategy: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            raise ValueError("action must be a core.Action, got %r" % (self.action,))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1], got %r" % (self.confidence,))
        if not self.explanation:
            raise ValueError("explanation is mandatory — every decision must be auditable")

    @property
    def is_actionable(self) -> bool:
        """True only for BUY/SELL — the sole actions that reach a broker adapter."""
        return self.action in ACTIONABLE

    def to_dict(self) -> dict[str, Any]:
        """Journal-friendly plain dict (action serialized to its string value)."""
        return {"action": self.action.value, "symbol": self.symbol,
                "confidence": self.confidence, "explanation": self.explanation,
                "factors": self.factors, "strategy": self.strategy}
