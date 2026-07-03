"""Unit tests for the explicit cost model and its effect on the backtest.
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CostModel, REALISTIC_COSTS, LEGACY_COSTS   # noqa: E402

ZERO = CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                 commission_per_side=0.0)


class TestCostModelMath(unittest.TestCase):
    def test_buy_pays_more_sell_receives_less(self):
        c = CostModel(spread_pips=1.0, slippage_pips=0.0, pip_size=0.0001)
        # half-spread = 0.5 pip = 0.00005
        self.assertAlmostEqual(c.fill_price(1.10000, True), 1.10005, places=7)
        self.assertAlmostEqual(c.fill_price(1.10000, False), 1.09995, places=7)

    def test_spread_plus_slippage_adjustment(self):
        c = CostModel(spread_pips=0.8, slippage_pips=0.2, pip_size=0.0001)
        # adj = half-spread(0.4p) + slip(0.2p) = 0.6 pip = 0.00006
        self.assertAlmostEqual(c.fill_price(1.10000, True), 1.10006, places=7)
        self.assertAlmostEqual(c.fill_price(1.10000, False), 1.09994, places=7)

    def test_realistic_commission_is_per_lot(self):
        # REALISTIC: commission_per_side(fraction)=0, per_lot=3.5
        self.assertAlmostEqual(REALISTIC_COSTS.commission(notional=10_000, lots=0.10),
                               0.35, places=6)

    def test_legacy_commission_is_notional_fraction(self):
        # LEGACY: per_lot=0, commission_per_side≈0.00007 of notional
        self.assertAlmostEqual(LEGACY_COSTS.commission(notional=10_000, lots=0.10),
                               10_000 * LEGACY_COSTS.commission_per_side, places=6)
        self.assertGreater(LEGACY_COSTS.commission(10_000, 0.10), 0.0)

    def test_zero_cost_model_is_free(self):
        self.assertEqual(ZERO.fill_price(1.2345, True), 1.2345)
        self.assertEqual(ZERO.commission(10_000, 5.0), 0.0)


class TestCostEffectOnBacktest(unittest.TestCase):
    """Integration: more cost => strictly lower return on the real data."""

    @classmethod
    def setUpClass(cls):
        try:
            from backtest import run
            cls.zero = run(cost=ZERO)
            cls.legacy = run(cost=LEGACY_COSTS)
            cls.realistic = run(cost=REALISTIC_COSTS)
        except FileNotFoundError:
            raise unittest.SkipTest("cached data CSV not present")

    def test_costs_reduce_return(self):
        self.assertLess(self.realistic.total_return_pct, self.zero.total_return_pct)
        self.assertLess(self.legacy.total_return_pct, self.zero.total_return_pct)
        self.assertLess(self.realistic.final_equity, self.zero.final_equity)

    def test_realistic_is_more_conservative_than_legacy(self):
        # realistic (spread+slip+per-lot) costs more round-turn than the legacy proxy
        self.assertLess(self.realistic.total_return_pct, self.legacy.total_return_pct)

    def test_legacy_reproduces_phase0_number(self):
        # Refactor guard: LEGACY_COSTS must still reproduce the original -12.18%.
        self.assertAlmostEqual(self.legacy.total_return_pct, -12.18, delta=0.5)

    def test_trade_count_unchanged_by_costs(self):
        # Costs change PnL, not which bars the signal fires on.
        self.assertEqual(self.zero.n_trades, self.realistic.n_trades)


if __name__ == "__main__":
    unittest.main(verbosity=2)
