"""Unit tests for execution.py — proves the demo-only / dry-run guarantees:
no real-money order path, refuses live accounts, defaults to not sending.
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RiskConfig, ExecutionConfig          # noqa: E402
from risk import RiskManager, SymbolSpec                 # noqa: E402
from execution import Executor, AccountInfo              # noqa: E402

EURUSD = SymbolSpec(1.0, 1e-05, 0.01, 50.0, 0.01)
DEMO = AccountInfo(login=1, trade_mode=0, balance=10_000, server="demo")
LIVE = AccountInfo(login=2, trade_mode=2, balance=10_000, server="live")  # REAL=2


def fresh_risk():
    return RiskManager(RiskConfig(1.0, 3.0, 1), EURUSD, 10_000)


class SpySender:
    def __init__(self):
        self.calls = []

    def __call__(self, order, acct):
        self.calls.append((order, acct))
        return {"retcode": "DONE"}


class RejectingSender:
    """Simulates a broker rejection: order_send returns a result whose retcode is
    numeric and != TRADE_RETCODE_DONE (the bug the live plumbing test caught —
    XM rejected an unsupported filling mode with 10030 and the old Executor still
    said SENT)."""
    def __init__(self, retcode=10030, comment="Unsupported filling mode"):
        self.calls = []
        self.retcode = retcode
        self.comment = comment

    def __call__(self, order, acct):
        self.calls.append((order, acct))
        return {"retcode": self.retcode, "comment": self.comment}


class NoneSender:
    def __call__(self, order, acct):
        return None


class TestBrokerVerification(unittest.TestCase):
    def _enabled(self, sender):
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        return Executor(fresh_risk(), account_provider=lambda: DEMO,
                        order_sender=sender, config=cfg)

    def test_numeric_rejection_is_not_sent(self):
        risk = fresh_risk()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        ex = Executor(risk, account_provider=lambda: DEMO,
                      order_sender=RejectingSender(), config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REJECTED_BROKER")
        self.assertFalse(res.sent)
        self.assertIn("10030", res.reason)
        self.assertEqual(risk.open_positions, 0)     # no phantom open registered

    def test_none_result_is_not_sent(self):
        res = self._enabled(NoneSender()).submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REJECTED_BROKER")
        self.assertFalse(res.sent)

    def test_numeric_done_retcode_is_sent(self):
        class DoneSender(RejectingSender):
            def __init__(self):
                super().__init__(retcode=10009, comment="Request completed")
        res = self._enabled(DoneSender()).submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "SENT")
        self.assertTrue(res.sent)


class TestExecutionSafety(unittest.TestCase):
    def test_default_config_sends_nothing(self):
        sender = SpySender()
        ex = Executor(fresh_risk(), account_provider=lambda: DEMO,
                      order_sender=sender, config=ExecutionConfig())  # defaults
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertFalse(res.sent)
        self.assertEqual(len(sender.calls), 0)
        self.assertIn(res.status, ("DISABLED", "DRY_RUN"))

    def test_refuses_live_even_when_fully_enabled(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        ex = Executor(fresh_risk(), account_provider=lambda: LIVE,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REFUSED_LIVE")
        self.assertFalse(res.sent)
        self.assertEqual(len(sender.calls), 0)

    def test_refuses_live_in_dry_run_too(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=False, dry_run=True)
        ex = Executor(fresh_risk(), account_provider=lambda: LIVE,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REFUSED_LIVE")
        self.assertEqual(len(sender.calls), 0)

    def test_unreadable_account_refuses(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        ex = Executor(fresh_risk(), account_provider=lambda: None,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REFUSED_LIVE")
        self.assertEqual(len(sender.calls), 0)

    def test_demo_enabled_not_dryrun_sends_once(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        ex = Executor(fresh_risk(), account_provider=lambda: DEMO,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "SENT")
        self.assertTrue(res.sent)
        self.assertEqual(len(sender.calls), 1)

    def test_demo_dryrun_logs_but_sends_nothing(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=True)
        ex = Executor(fresh_risk(), account_provider=lambda: DEMO,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "DRY_RUN")
        self.assertEqual(len(sender.calls), 0)
        self.assertIsNotNone(res.order)        # intent captured
        self.assertAlmostEqual(res.order["volume"], 0.20, places=6)

    def test_risk_rejection_blocks_send(self):
        sender = SpySender()
        cfg = ExecutionConfig(execution_enabled=True, dry_run=False)
        risk = fresh_risk()
        risk.trip_kill_switch()
        ex = Executor(risk, account_provider=lambda: DEMO,
                      order_sender=sender, config=cfg)
        res = ex.submit("buy", 10_000, 0.0050)
        self.assertEqual(res.status, "REJECTED_RISK")
        self.assertEqual(len(sender.calls), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
