"""core.risk — the risk-approval contract.

Risk is the only thing allowed to APPROVE an order, and it may override any
strategy. Two shapes are described:

  RiskVerdict  — the result of an approval decision (approved + reason), so the
                 caller always has a logged reason for a rejection.
  RiskManager  — anything that turns (balance, stop distance) into a verdict.

The root `risk.RiskManager` / `risk.RiskDecision` satisfy these as-is. The
executor's veto-based `risk.py` expresses the same intent as raised `Veto`
exceptions; it is adapted toward this contract in a later phase, not rewritten.

Contract only — no behavior, no relaxation of any existing limit.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RiskVerdict(Protocol):
    """A risk decision with an auditable reason. `approved` gates the order;
    `reason` is logged on both approval and rejection (never a silent no)."""

    @property
    def approved(self) -> bool: ...

    @property
    def reason(self) -> str: ...


@runtime_checkable
class RiskManager(Protocol):
    """Turns account state + a proposed stop distance into a RiskVerdict.
    Structural: any object with `evaluate` returning an object that carries
    `approved` and `reason` qualifies."""

    def evaluate(self, balance: float, stop_distance_price: float) -> RiskVerdict:
        """Approve or reject a prospective order. MUST NOT place it — approval
        only. The returned verdict always carries a human-readable reason."""
        ...
