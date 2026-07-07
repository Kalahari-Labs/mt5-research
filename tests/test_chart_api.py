"""Price-chart API tests (executor plane, no MT5 required).

/api/chart is the dashboard's only parameterised route. These tests pin its
input validation (nothing unvalidated is ever forwarded to the bridge), the
overlay assembly (positions/trades/pending filtered to the requested symbol)
and the bridge-down path (an error payload, never an exception).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root


def _import_executor():
    """Import executor modules the way they run: with `intel/` on the path.
    Appended (not inserted) so root modules keep precedence."""
    intel_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intel")
    if intel_dir not in sys.path:
        sys.path.append(intel_dir)
    from executor import config, dashboard
    from executor.bridge import BridgeError
    from executor.store import Store
    return config, dashboard, BridgeError, Store


config, dashboard, BridgeError, Store = _import_executor()

SYM = config.SYMBOLS[0]
OTHER = config.SYMBOLS[-1]

# [epoch, open, high, low, close, tick_volume, spread] — bridge_server shape
BARS = [[1751500800 + i * 900, 1.0, 1.2, 0.9, 1.1, 100 + i, 12]
        for i in range(30)]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeBridge:
    def __init__(self, positions=None, bars_error=None, pos_error=None):
        self._positions = positions or []
        self._bars_error = bars_error
        self._pos_error = pos_error
        self.bars_calls = []

    def bars(self, symbol, tf="H1", count=300, start=0):
        if self._bars_error:
            raise BridgeError(self._bars_error)
        self.bars_calls.append((symbol, tf, count))
        return BARS[:count]

    def positions(self):
        if self._pos_error:
            raise BridgeError(self._pos_error)
        return self._positions


def _position(symbol, ticket=1):
    return {"ticket": ticket, "symbol": symbol, "type": 0, "volume": 0.01,
            "price_open": 1.05, "price_current": 1.06, "sl": 1.00, "tp": 1.20,
            "profit": 3.2, "magic": 770001, "comment": "not-for-chart"}


class TestParseChartQuery(unittest.TestCase):
    def test_defaults(self):
        symbol, tf, count = dashboard.parse_chart_query("/api/chart")
        self.assertEqual((symbol, tf, count), (config.SYMBOLS[0], "M15", 180))

    def test_explicit_params_pass_through(self):
        symbol, tf, count = dashboard.parse_chart_query(
            "/api/chart?symbol=%s&tf=H1&count=300" % OTHER)
        self.assertEqual((symbol, tf, count), (OTHER, "H1", 300))

    def test_unknown_symbol_rejected(self):
        with self.assertRaises(ValueError):
            dashboard.parse_chart_query("/api/chart?symbol=DOGEUSD")

    def test_unknown_timeframe_rejected(self):
        # M1 is a real MT5 timeframe but not on the chart whitelist
        with self.assertRaises(ValueError):
            dashboard.parse_chart_query("/api/chart?symbol=%s&tf=M1" % SYM)

    def test_count_clamped_both_ends(self):
        _, _, lo = dashboard.parse_chart_query("/api/chart?count=1")
        _, _, hi = dashboard.parse_chart_query("/api/chart?count=999999")
        self.assertEqual((lo, hi), (20, 500))

    def test_non_integer_count_rejected(self):
        with self.assertRaises(ValueError):
            dashboard.parse_chart_query("/api/chart?count=abc")


class ChartBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "executor.sqlite")

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def _trade(self, symbol, ticket, **over):
        now = datetime.now(timezone.utc)
        row = {"ticket": ticket, "symbol": symbol, "strategy": "trend_pullback",
               "side": "buy", "volume": 0.01,
               "entry_time": _iso(now - timedelta(hours=2)), "entry_price": 1.05,
               "sl": 1.00, "tp": 1.20, "status": "open",
               "context": {"signal": "pullback to EMA in uptrend"}}
        row.update(over)
        return self.store.insert("trades", row)

    def _pending(self, symbol):
        now = datetime.now(timezone.utc)
        return self.store.insert("pending_trades", {
            "symbol": symbol, "strategy": "trend_pullback", "side": "buy",
            "volume": 0.01, "sl": 1.00, "tp": 1.20, "reason": "test proposal",
            "detail": {"stars": 3}, "status": "pending",
            "ts_created": _iso(now),
            "ts_expires": _iso(now + timedelta(minutes=15))})


class TestApiChart(ChartBase):
    def test_happy_path_filters_overlays_to_symbol(self):
        bridge = FakeBridge(positions=[_position(SYM, 1), _position(OTHER, 2)])
        self._trade(SYM, 11)
        self._trade(OTHER, 12)
        self._pending(SYM)
        self._pending(OTHER)
        out = dashboard.api_chart(self.store, bridge, SYM, "M15", 30)
        self.assertNotIn("error", out)
        self.assertEqual(out["bars"], BARS)
        self.assertEqual(bridge.bars_calls, [(SYM, "M15", 30)])
        self.assertEqual([p["ticket"] for p in out["positions"]], [1])
        self.assertEqual([t["ticket"] for t in out["trades"]], [11])
        self.assertEqual([p["symbol"] for p in out["pending"]], [SYM])

    def test_position_payload_is_whitelisted_keys_only(self):
        bridge = FakeBridge(positions=[_position(SYM)])
        out = dashboard.api_chart(self.store, bridge, SYM, "M15", 30)
        self.assertEqual(set(out["positions"][0]),
                         {"ticket", "type", "volume", "price_open",
                          "price_current", "sl", "tp", "profit"})

    def test_bridge_down_returns_error_payload_not_exception(self):
        bridge = FakeBridge(bars_error="connection refused")
        self._trade(SYM, 11)
        out = dashboard.api_chart(self.store, bridge, SYM, "M15", 30)
        self.assertIn("connection refused", out["error"])
        self.assertEqual((out["bars"], out["positions"], out["trades"]),
                         ([], [], []))

    def test_positions_error_degrades_gracefully(self):
        bridge = FakeBridge(pos_error="terminal not logged in")
        self._trade(SYM, 11)
        out = dashboard.api_chart(self.store, bridge, SYM, "M15", 30)
        self.assertNotIn("error", out)
        self.assertEqual(out["positions"], [])
        self.assertEqual([t["ticket"] for t in out["trades"]], [11])


class TestTradesForChart(ChartBase):
    def test_symbol_filter_and_context_included(self):
        self._trade(SYM, 21)
        self._trade(OTHER, 22)
        rows = self.store.trades_for_chart(SYM)
        self.assertEqual([r["ticket"] for r in rows], [21])
        self.assertIn("pullback to EMA", rows[0]["context"])

    def test_limit_returns_newest_first(self):
        for i in range(5):
            self._trade(SYM, 30 + i)
        rows = self.store.trades_for_chart(SYM, limit=3)
        self.assertEqual([r["ticket"] for r in rows], [34, 33, 32])


if __name__ == "__main__":
    unittest.main()
