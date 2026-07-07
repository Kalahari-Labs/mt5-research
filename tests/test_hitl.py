"""Human-in-the-loop (HITL) approval tests (executor plane, no MT5 required).

The HITL contract: a proposed trade may only reach the bridge while a human
approval AND an unexpired quote coexist. These tests pin the enforcement
primitives in store.py — the expiry sweep (including the approve-after-expiry
race) and the acted-on guard that stops expired/executed proposals from being
resurrected through the dashboard's /api/act endpoint.
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
    from executor import config
    from executor.store import Store
    return config, Store


config, Store = _import_executor()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _proposal(store, minutes_to_expiry: float, status: str = "pending") -> int:
    now = datetime.now(timezone.utc)
    return store.insert("pending_trades", {
        "symbol": "EURUSD", "strategy": "trend_pullback", "side": "buy",
        "volume": 0.01, "sl": 1.0900, "tp": 1.1100, "reason": "test proposal",
        "detail": {"tags": ["with-trend"], "timeframe": "H1"},
        "status": status, "ts_created": _iso(now),
        "ts_expires": _iso(now + timedelta(minutes=minutes_to_expiry))})


class HitlBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "executor.sqlite")

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def _status(self, pid: int) -> str:
        return self.store.query(
            "SELECT status FROM pending_trades WHERE id=?", (pid,))[0]["status"]


class TestProposalRoundTrip(HitlBase):
    def test_proposal_row_survives_round_trip(self):
        pid = _proposal(self.store, minutes_to_expiry=15)
        rows = self.store.pending_trades("pending")
        self.assertEqual([r["id"] for r in rows], [pid])
        row = rows[0]
        self.assertEqual((row["symbol"], row["side"], row["volume"]),
                         ("EURUSD", "buy", 0.01))
        self.assertEqual((row["sl"], row["tp"]), (1.0900, 1.1100))


class TestExpirySweep(HitlBase):
    def test_past_expiry_pending_is_swept(self):
        pid = _proposal(self.store, minutes_to_expiry=-1)
        self.assertEqual(self.store.expire_stale_pending(), 1)
        self.assertEqual(self._status(pid), "expired")
        self.assertEqual(self.store.pending_trades("pending"), [])

    def test_approved_after_expiry_is_swept_not_executed(self):
        # the race: human clicks APPROVE on a row already past ts_expires but
        # not yet swept — the sweep must still catch it before the engine
        # fetches 'approved' rows
        pid = _proposal(self.store, minutes_to_expiry=-1, status="approved")
        self.assertEqual(self.store.expire_stale_pending(), 1)
        self.assertEqual(self._status(pid), "expired")
        self.assertEqual(self.store.pending_trades("approved"), [])

    def test_future_expiry_is_untouched(self):
        pid = _proposal(self.store, minutes_to_expiry=15)
        self.assertEqual(self.store.expire_stale_pending(), 0)
        self.assertEqual(self._status(pid), "pending")

    def test_terminal_states_are_never_reswept(self):
        for status in ("executed", "denied", "expired"):
            pid = _proposal(self.store, minutes_to_expiry=-1, status=status)
            self.store.expire_stale_pending()
            self.assertEqual(self._status(pid), status)


class TestActOnPending(HitlBase):
    def test_approve_flips_pending(self):
        pid = _proposal(self.store, minutes_to_expiry=15)
        self.assertTrue(self.store.act_on_pending(pid, "approve"))
        self.assertEqual(self._status(pid), "approved")

    def test_deny_flips_pending(self):
        pid = _proposal(self.store, minutes_to_expiry=15)
        self.assertTrue(self.store.act_on_pending(pid, "deny"))
        self.assertEqual(self._status(pid), "denied")

    def test_expired_row_cannot_be_resurrected(self):
        pid = _proposal(self.store, minutes_to_expiry=-1)
        self.store.expire_stale_pending()
        self.assertFalse(self.store.act_on_pending(pid, "approve"))
        self.assertEqual(self._status(pid), "expired")

    def test_executed_row_cannot_be_reacted_on(self):
        pid = _proposal(self.store, minutes_to_expiry=15, status="executed")
        self.assertFalse(self.store.act_on_pending(pid, "approve"))
        self.assertFalse(self.store.act_on_pending(pid, "deny"))
        self.assertEqual(self._status(pid), "executed")

    def test_unknown_action_is_rejected(self):
        pid = _proposal(self.store, minutes_to_expiry=15)
        self.assertFalse(self.store.act_on_pending(pid, "execute"))
        self.assertFalse(self.store.act_on_pending(pid, ""))
        self.assertEqual(self._status(pid), "pending")


class TestHitlConfig(unittest.TestCase):
    def test_ttl_is_a_usable_human_scale_window(self):
        # a phone-notification workflow needs minutes, not the engine cycle;
        # guards against regressing to the old CYCLE_SEC*2 (=60s) lifetime
        self.assertIsInstance(config.HITL_TTL_MIN, int)
        self.assertGreaterEqual(config.HITL_TTL_MIN, 1)


if __name__ == "__main__":
    unittest.main()
