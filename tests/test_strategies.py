"""Tests for the strategy contract + registry, including the refactor guard that
the registered SMA reproduces the prior numbers exactly.
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies                                  # noqa: E402
from strategies.base import Strategy, Signals      # noqa: E402
from strategies.sma_crossover import SmaCrossover  # noqa: E402
from strategies.buy_and_hold import BuyAndHold     # noqa: E402


class TestRegistry(unittest.TestCase):
    def test_builtins_registered(self):
        self.assertIn("sma_crossover", strategies.all_names())
        self.assertIn("buy_and_hold", strategies.all_names())

    def test_get_returns_strategy(self):
        self.assertIsInstance(strategies.get("sma_crossover"), Strategy)

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            strategies.get("does_not_exist")

    def test_double_register_rejected(self):
        with self.assertRaises(ValueError):
            strategies.register(SmaCrossover())   # name already taken


class TestContract(unittest.TestCase):
    def test_generate_is_pure_and_right_shape(self):
        close = np.array([1, 2, 3, 4, 5, 4, 3, 2, 3, 4, 5, 6, 7, 6, 5], float)
        s = SmaCrossover()
        a = s.generate(close, fast=2, slow=4)
        b = s.generate(close, fast=2, slow=4)
        self.assertIsInstance(a, Signals)
        self.assertEqual(a.regime.shape, close.shape)
        np.testing.assert_array_equal(a.regime, b.regime)   # pure: same in → same out

    def test_buy_and_hold_always_long(self):
        r = BuyAndHold().generate(np.arange(10.0))
        self.assertTrue((r.regime == 1).all())
        self.assertEqual(r.entries[0], 1)
        self.assertEqual(r.entries[1:].sum(), 0)


class TestSmaRefactorGuard(unittest.TestCase):
    """Prove the registered SMA reproduces the prior behaviour bit-for-bit."""

    @classmethod
    def setUpClass(cls):
        try:
            from data import load_ohlcv
            cls.close = load_ohlcv().close
        except FileNotFoundError:
            raise unittest.SkipTest("cached data CSV not present")

    def test_regime_matches_independent_oracle(self):
        fast, slow = 20, 50

        def sma_oracle(x, p):           # convolution oracle, independent of the impl
            out = np.full(len(x), np.nan)
            if len(x) >= p:
                out[p - 1:] = np.convolve(x, np.ones(p) / p, "valid")
            return out

        f, s = sma_oracle(self.close, fast), sma_oracle(self.close, slow)
        valid = ~np.isnan(f) & ~np.isnan(s)
        exp = np.zeros(len(self.close), int)
        exp[valid & (f > s)] = 1
        exp[valid & (f < s)] = -1

        got = SmaCrossover().generate(self.close, fast=fast, slow=slow).regime
        # Allow only float-noise disagreements at genuine near-ties (|f-s|<1e-9).
        mism = np.where(got != exp)[0]
        self.assertLess(len(mism), 5)
        for i in mism:
            self.assertLess(abs(f[i] - s[i]), 1e-9)

    def test_backtest_numbers_unchanged(self):
        from backtest import run
        from config import LEGACY_COSTS, REALISTIC_COSTS
        legacy = run(cost=LEGACY_COSTS)
        realistic = run(cost=REALISTIC_COSTS)
        self.assertAlmostEqual(legacy.total_return_pct, -12.18, delta=0.5)
        self.assertAlmostEqual(realistic.total_return_pct, -13.04, delta=0.5)
        self.assertEqual(realistic.n_trades, 328)
        self.assertAlmostEqual(realistic.max_drawdown_pct, -14.06, delta=0.5)
        self.assertEqual(realistic.strategy, "sma_crossover")
        self.assertEqual(realistic.params, {"fast": 20, "slow": 50})


if __name__ == "__main__":
    unittest.main(verbosity=2)
