"""Unit tests for risk.py — position sizing, max-risk cap, daily-loss cap, kill
switch. Pure arithmetic, no broker. Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RiskConfig          # noqa: E402
from risk import RiskManager, SymbolSpec  # noqa: E402

# Real EURUSD specs: 1 tick (1e-5) = $1.00 per 1.0 lot.
# => money_per_lot at a 0.0050 (50-pip) stop = 0.0050/1e-5 * 1.0 = $500/lot.
EURUSD = SymbolSpec(tick_value=1.0, tick_size=1e-05,
                    volume_min=0.01, volume_max=50.0, volume_step=0.01)


def mk(risk_pct=1.0, daily=3.0, maxpos=1, balance=10_000.0):
    cfg = RiskConfig(risk_per_trade_pct=risk_pct, max_daily_loss_pct=daily,
                     max_open_positions=maxpos)
    return RiskManager(cfg, EURUSD, balance)


class TestPositionSizing(unittest.TestCase):
    def test_basic_size_and_risk(self):
        rm = mk(balance=10_000)
        # budget $100, $500/lot -> 0.20 lots, risk exactly $100.
        self.assertAlmostEqual(rm.position_size(10_000, 0.0050), 0.20, places=6)
        d = rm.evaluate(10_000, 0.0050)
        self.assertTrue(d.approved)
        self.assertAlmostEqual(d.volume, 0.20, places=6)
        self.assertAlmostEqual(d.risk_amount, 100.0, places=4)

    def test_rounds_down_to_lot_step(self):
        rm = mk(balance=10_000)
        # $100 budget, $370/lot -> raw 0.2703 -> floored to step 0.01 -> 0.27.
        self.assertAlmostEqual(rm.position_size(10_000, 0.0037), 0.27, places=6)

    def test_wider_stop_smaller_size(self):
        rm = mk(balance=10_000)
        wide = rm.position_size(10_000, 0.0100)   # $1000/lot -> 0.10
        narrow = rm.position_size(10_000, 0.0050)  # $500/lot  -> 0.20
        self.assertLess(wide, narrow)
        self.assertAlmostEqual(wide, 0.10, places=6)


class TestMaxRiskCap(unittest.TestCase):
    def test_min_lot_exceeds_budget_is_rejected(self):
        # balance $400 -> budget $4; smallest lot 0.01 risks $5 at a 0.0050 stop.
        rm = mk(balance=400)
        d = rm.evaluate(400, 0.0050)
        self.assertFalse(d.approved)
        self.assertIn("min lot", d.reason)

    def test_sized_risk_never_exceeds_budget(self):
        rm = mk(balance=10_000)
        d = rm.evaluate(10_000, 0.0050)
        self.assertLessEqual(d.risk_amount, 10_000 * 0.01 + 1e-6)

    def test_zero_stop_rejected(self):
        rm = mk(balance=10_000)
        self.assertFalse(rm.evaluate(10_000, 0.0).approved)


class TestDailyLossCap(unittest.TestCase):
    def test_hitting_cap_trips_killswitch_and_rejects(self):
        rm = mk(balance=10_000, daily=3.0)   # cap = $300
        rm.register_close(-300.0)            # daily loss reaches the cap
        self.assertTrue(rm.kill_switch)
        d = rm.evaluate(10_000, 0.0050)
        self.assertFalse(d.approved)
        self.assertIn("daily loss", d.reason)

    def test_below_cap_still_approves(self):
        rm = mk(balance=10_000, daily=3.0)
        rm.register_close(-100.0)            # within cap, position now flat
        self.assertFalse(rm.kill_switch)
        self.assertTrue(rm.evaluate(10_000, 0.0050).approved)

    def test_trade_that_would_breach_cap_is_rejected(self):
        # 2% risk/trade, 3% daily cap, already -2% today: next 2% would breach.
        rm = mk(risk_pct=2.0, daily=3.0, balance=10_000)  # budget $200, cap $300
        rm.register_close(-200.0)            # daily loss $200, still < cap
        self.assertFalse(rm.kill_switch)
        d = rm.evaluate(10_000, 0.0050)      # would add $200 risk -> $400 > $300
        self.assertFalse(d.approved)
        self.assertIn("daily loss cap", d.reason)


class TestMaxOpenPositions(unittest.TestCase):
    def test_open_cap_blocks_new(self):
        rm = mk(maxpos=1)
        rm.register_open()
        self.assertFalse(rm.evaluate(10_000, 0.0050).approved)


class TestKillSwitch(unittest.TestCase):
    def test_manual_kill_rejects(self):
        rm = mk()
        rm.trip_kill_switch()
        d = rm.evaluate(10_000, 0.0050)
        self.assertFalse(d.approved)
        self.assertIn("kill switch", d.reason)

    def test_reset_day_clears_state(self):
        rm = mk()
        rm.register_close(-500.0)
        rm.trip_kill_switch()
        rm.reset_day(10_000)
        self.assertFalse(rm.kill_switch)
        self.assertTrue(rm.evaluate(10_000, 0.0050).approved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
