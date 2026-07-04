"""core.broker — the broker-independence contract (Phase 2 keystone).

Every path that touches a broker for account state, positions, or order
lifecycle goes through a BrokerAdapter. The executor's `Bridge` already
satisfies this shape; Phase 2 adds `MT5BrokerAdapter` (thin wrapper over the
demo-guarded HTTP bridge) and `PaperBrokerAdapter` (in-memory fills) behind the
SAME contract, so engine logic never learns which broker it is talking to.

Contract only — no behavior, and in particular NO relaxation of the server-side
demo/live gate: an adapter forwards orders, it never decides whether a live
order is permitted. That decision stays in `bridge_server.write_gate`.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class OrderResult(TypedDict, total=False):
    """Normalized order-send result. `ok` is the only guaranteed key; the rest
    are best-effort passthrough from the underlying broker."""
    ok: bool
    retcode: int
    position: int
    price: float
    volume: float
    comment: str
    error: str


@runtime_checkable
class BrokerAdapter(Protocol):
    """Account + position + order lifecycle. Structural: any object exposing
    these methods qualifies (the executor `Bridge` does, as-is)."""

    def account(self) -> dict[str, Any]:
        """Account snapshot (balance, equity, margin, currency, ...)."""
        ...

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Currently open positions, optionally filtered by symbol."""
        ...

    def order(self, symbol: str, side: str, volume: float, sl: float, tp: float,
              comment: str = "", magic: int = 0) -> OrderResult:
        """Send a market order. `sl` and `tp` are MANDATORY protective prices —
        adapters must not send naked orders (the server-side gate refuses them
        anyway)."""
        ...

    def close(self, ticket: int, comment: str = "") -> OrderResult:
        """Close the position identified by `ticket`."""
        ...

    def modify(self, ticket: int, sl: float | None = None,
               tp: float | None = None) -> OrderResult:
        """Modify the stop-loss / take-profit of an open position."""
        ...

    def alive(self) -> bool:
        """True when the adapter can currently reach the broker."""
        ...
