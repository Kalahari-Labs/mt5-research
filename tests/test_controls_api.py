"""Dashboard manual-control tests (executor plane, no MT5 required).

The dashboard's human overrides — pause/resume, kill switch, close position,
manual trade ticket — must all be journaled, validated before anything
reaches the bridge, and unable to bypass the discipline the engine lives by
(whitelisted symbols, capped size, mandatory SL/TP). These tests pin that.
"""
import os
import sys
import tempfile
import unittest
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


class FakeBridge:
    def __init__(self, positions=None, fail=None):
        self._positions = positions or []
        self._fail = fail          # BridgeError message raised by any write
        self.orders = []
        self.closes = []

    def positions(self):
        return self._positions

    def order(self, symbol, side, volume, sl, tp, comment="", magic=0):
        if self._fail:
            raise BridgeError(self._fail)
        self.orders.append((symbol, side, volume, sl, tp, comment))
        return {"ok": True, "ticket": 424242}

    def close(self, ticket, comment="", volume=None):
        if self._fail:
            raise BridgeError(self._fail)
        self.closes.append((ticket, comment))
        return {"ok": True}


class ControlsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "executor.sqlite")
        self._orig_kill = config.KILL_SWITCH
        config.KILL_SWITCH = Path(self.tmp.name) / "KILL"

    def tearDown(self):
        config.KILL_SWITCH = self._orig_kill
        self.store.conn.close()
        self.tmp.cleanup()

    def _decisions(self):
        return self.store.query("SELECT action, reason, symbol FROM decisions ORDER BY id")


class TestControl(ControlsBase):
    def test_halt_sets_state_and_journals(self):
        out = dashboard.api_control(self.store, {"action": "halt"})
        self.assertTrue(out["ok"])
        self.assertIn("dashboard", self.store.get_state("manual_halt"))
        self.assertEqual(self._decisions()[0]["action"], "halt")

    def test_resume_clears_halt(self):
        dashboard.api_control(self.store, {"action": "halt"})
        out = dashboard.api_control(self.store, {"action": "resume"})
        self.assertTrue(out["ok"])
        self.assertIsNone(self.store.get_state("manual_halt"))

    def test_kill_touches_file_and_clear_removes_it(self):
        self.assertTrue(dashboard.api_control(self.store, {"action": "kill"})["ok"])
        self.assertTrue(config.KILL_SWITCH.exists())
        self.assertTrue(dashboard.api_control(self.store, {"action": "clear_kill"})["ok"])
        self.assertFalse(config.KILL_SWITCH.exists())

    def test_unknown_action_rejected_and_not_journaled(self):
        out = dashboard.api_control(self.store, {"action": "moon"})
        self.assertFalse(out["ok"])
        self.assertEqual(self._decisions(), [])


class TestClosePosition(ControlsBase):
    POS = {"ticket": 7, "symbol": SYM, "type": 0, "volume": 0.01}

    def test_close_open_position_journals_exit(self):
        bridge = FakeBridge(positions=[self.POS])
        out = dashboard.api_close_position(self.store, bridge, {"ticket": 7})
        self.assertTrue(out["ok"])
        self.assertEqual(bridge.closes, [(7, "mi-dashboard manual close")])
        d = self._decisions()[0]
        self.assertEqual((d["action"], d["symbol"]), ("exit", SYM))

    def test_unknown_ticket_never_reaches_bridge(self):
        bridge = FakeBridge(positions=[self.POS])
        out = dashboard.api_close_position(self.store, bridge, {"ticket": 999})
        self.assertFalse(out["ok"])
        self.assertEqual(bridge.closes, [])

    def test_non_integer_ticket_rejected(self):
        out = dashboard.api_close_position(self.store, FakeBridge(), {"ticket": "abc"})
        self.assertFalse(out["ok"])

    def test_bridge_error_surfaces_not_raises(self):
        bridge = FakeBridge(positions=[self.POS], fail="writes refused: not a demo account")
        out = dashboard.api_close_position(self.store, bridge, {"ticket": 7})
        self.assertFalse(out["ok"])
        self.assertIn("writes refused", out["error"])


class TestManualOrder(ControlsBase):
    def _order(self, **over):
        body = {"symbol": SYM, "side": "buy", "volume": 0.02,
                "sl": 1.05, "tp": 1.15}
        body.update(over)
        return body

    def test_happy_path_orders_and_journals(self):
        bridge = FakeBridge()
        out = dashboard.api_manual_order(self.store, bridge, self._order())
        self.assertTrue(out["ok"])
        self.assertEqual(bridge.orders,
                         [(SYM, "buy", 0.02, 1.05, 1.15, "mi-dashboard manual")])
        d = self._decisions()[0]
        self.assertEqual((d["action"], d["symbol"]), ("enter", SYM))

    def test_unknown_symbol_never_reaches_bridge(self):
        bridge = FakeBridge()
        out = dashboard.api_manual_order(self.store, bridge,
                                         self._order(symbol="DOGEUSD"))
        self.assertFalse(out["ok"])
        self.assertEqual(bridge.orders, [])

    def test_bad_side_rejected(self):
        out = dashboard.api_manual_order(self.store, FakeBridge(),
                                         self._order(side="yolo"))
        self.assertFalse(out["ok"])

    def test_volume_cap_enforced_both_ends(self):
        for vol in (0.005, dashboard.MANUAL_MAX_LOTS + 0.01):
            out = dashboard.api_manual_order(self.store, FakeBridge(),
                                             self._order(volume=vol))
            self.assertFalse(out["ok"], "volume %s must be rejected" % vol)

    def test_missing_or_zero_stop_rejected(self):
        for bad in ({"sl": None}, {"sl": 0}, {"tp": 0}, {"tp": "x"}):
            out = dashboard.api_manual_order(self.store, FakeBridge(),
                                             self._order(**bad))
            self.assertFalse(out["ok"], "must reject %s" % bad)

    def test_bridge_refusal_surfaces_not_raises(self):
        bridge = FakeBridge(fail="market closed")
        out = dashboard.api_manual_order(self.store, bridge, self._order())
        self.assertFalse(out["ok"])
        self.assertIn("market closed", out["error"])


if __name__ == "__main__":
    unittest.main()
