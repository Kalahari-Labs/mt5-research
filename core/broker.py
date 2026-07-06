"""core.broker — the broker-independence contract (Phase 2 keystone).

Every path that touches a broker for account state, positions, or order
lifecycle goes through a BrokerAdapter. The executor's `Bridge` already
satisfies this shape; Phase 2 adds `MT5BrokerAdapter` (thin wrapper over the
demo-guarded HTTP bridge) and `PaperBrokerAdapter` (in-memory fills) behind the
SAME contract, so engine logic never learns which broker it is talking to.

Contract only — no behavior, and in particular NO relaxation of the server-side
demo/live gate: an adapter forwards orders, it never decides whether a live
order is permitted. That decision stays in `bridge_server.write_gate`.

The shapes here describe EXACTLY what the executor engine relies on today (it is
the reference caller). A Phase-2 adapter that omits any of these — a paper
broker that forgets partial closes, or a feed that cannot report deal history —
is caught by `tests/test_core_contracts.py`, not discovered in production.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class OrderResult(TypedDict, total=False):
    """Normalized order-send result. `ok` is the only guaranteed key; the rest
    are best-effort passthrough from the underlying broker. The engine reads
    `ok`, `position`, `price`, `volume`, `retcode`, and `comment`."""
    ok: bool
    retcode: int
    position: int
    price: float
    volume: float
    comment: str
    error: str


class DealRow(TypedDict, total=False):
    """One row of broker deal history — the raw material for reconciling a
    position the broker closed on its own (SL/TP hit) since the last cycle.

    `position_id` links the deal back to the position it opened/closed;
    `entry == 1` marks a CLOSING deal (0 = opening); `reason` is the broker's
    close-reason code (SL/TP/expert/manual). The executor sums `profit`, `swap`,
    and `commission` across the closing deals and takes `price`/`time` from the
    last one. Any adapter serving `history_deals` must populate these keys."""
    ticket: int
    position_id: int
    entry: int
    reason: int
    time: float
    price: float
    profit: float
    swap: float
    commission: float
    symbol: str
    volume: float


class AccountHealth(TypedDict, total=False):
    """Account identity carried inside a HealthReport."""
    login: int
    demo: bool
    server: str


class HealthReport(TypedDict, total=False):
    """Adapter self-report. The engine gates its startup banner on `account`
    and `writes_allowed`; the dashboard shows `demo`, `writes_allowed`, `gate`.
    A paper/second-broker adapter synthesizes these rather than leaving the
    engine to guess — that is the whole point of the contract."""
    ok: bool
    writes_allowed: bool
    gate: str
    account: AccountHealth


@runtime_checkable
class BrokerAdapter(Protocol):
    """Account + position + order lifecycle + reconciliation + health. Structural:
    any object exposing these methods qualifies (the executor `Bridge` does,
    as-is). This is the full surface the engine drives — nothing it calls on the
    bridge lives outside this contract."""

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

    def close(self, ticket: int, comment: str = "",
              volume: float | None = None) -> OrderResult:
        """Close the position identified by `ticket`. `volume` closes only part
        of the position (partial take-profit); omit it to close the whole
        position. An adapter that cannot partial-close must still accept the
        parameter and either honor it or reject the request explicitly."""
        ...

    def modify(self, ticket: int, sl: float | None = None,
               tp: float | None = None) -> OrderResult:
        """Modify the stop-loss / take-profit of an open position."""
        ...

    def history_deals(self, days: int = 30) -> list[DealRow]:
        """Closed-deal history over the last `days`, newest window, used to
        reconcile positions the broker closed without the engine's knowledge.
        See `DealRow` for the fields callers depend on."""
        ...

    def health(self) -> HealthReport:
        """Liveness + gate status. `writes_allowed` reflects the server-side
        demo/live gate; the adapter reports it, it never sets it."""
        ...

    def alive(self) -> bool:
        """True when the adapter can currently reach the broker."""
        ...
