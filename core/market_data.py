"""core.market_data — the market-data read contract.

A MarketDataProvider serves price bars and the current tick for a symbol. The
executor's `Bridge` satisfies this today; a paper/simulation provider or a
second broker's feed can satisfy it tomorrow without any consumer changing.

Contract only — no behavior. Bar rows follow the repo-wide convention
`[epoch, open, high, low, close, tick_volume, spread_points]`, oldest-first.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    """Read-only market data. Structural: any object with these methods qualifies."""

    def bars(self, symbol: str, tf: str = "H1", count: int = 300,
             start: int = 0) -> Sequence[Sequence[float]]:
        """`count` closed bars for `symbol` at timeframe `tf`, oldest-first,
        each `[epoch, o, h, l, c, tick_volume, spread_points]`."""
        ...

    def tick(self, symbol: str) -> dict[str, Any]:
        """Latest tick, e.g. `{"time", "bid", "ask", "last"}`."""
        ...
