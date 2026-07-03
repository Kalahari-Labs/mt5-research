"""Tests for Phase 4: the overnight-swap cost, inverse-vol (equal-risk) sleeve
scaling, portfolio aggregation, and the regression guard that adding all of this
left SMA + single-EURUSD momentum BYTE-FOR-BYTE unchanged (swap defaults to 0).
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CostModel, REALISTIC_COSTS, cost_for, INSTRUMENT_COSTS  # noqa: E402
from backtest import cost_dict                                             # noqa: E402
import portfolio as pf                                                     # noqa: E402


# ─────────────────────────── swap / financing cost ──────────────────────────
class TestSwapCost(unittest.TestCase):
    def test_swap_is_a_positive_drag(self):
        c = CostModel(swap_rate_annual=0.05)               # 5%/yr
        # 1,000,000 notional, 30 nights, 360-day year -> 0.05/360*1e6*30
        self.assertAlmostEqual(c.swap_cost(1_000_000, 30), 0.05 / 360 * 1e6 * 30, places=6)
        self.assertGreater(c.swap_cost(1_000_000, 1), 0.0)

    def test_zero_when_rate_or_nights_zero(self):
        self.assertEqual(REALISTIC_COSTS.swap_cost(1_000_000, 30), 0.0)   # rate 0
        self.assertEqual(CostModel(swap_rate_annual=0.05).swap_cost(1_000_000, 0), 0.0)
        self.assertEqual(CostModel(swap_rate_annual=0.05).swap_cost(0, 30), 0.0)

    def test_cost_dict_hash_stability(self):
        # swap-free cost payload must be UNCHANGED (so prior content hashes hold)...
        self.assertNotIn("swap_rate_annual", cost_dict(REALISTIC_COSTS))
        # ...but a sleeve cost WITH swap records it.
        self.assertIn("swap_rate_annual", cost_dict(cost_for("GOLD")))

    def test_every_basket_instrument_has_a_cost_row(self):
        for sym in pf.PORTFOLIO_BASKET:
            self.assertIn(sym, INSTRUMENT_COSTS, f"{sym} missing a cost spec")
            self.assertGreater(INSTRUMENT_COSTS[sym]["swap_rate_annual"], 0.0)


class TestSwapReducesPnL(unittest.TestCase):
    """Integration: charging swap strictly lowers P&L on a strategy that HOLDS."""

    def test_swap_reduces_final_equity(self):
        try:
            from backtest import run
            noswap = run(strategy_name="ts_momentum", symbol="EURUSD", timeframe_min=1440,
                         cost=cost_for("EURUSD", with_swap=False))
            swap = run(strategy_name="ts_momentum", symbol="EURUSD", timeframe_min=1440,
                       cost=cost_for("EURUSD", with_swap=True))
        except FileNotFoundError:
            self.skipTest("cached EURUSD D1 data not present")
        self.assertLess(swap.final_equity, noswap.final_equity)      # swap is a drag
        self.assertEqual(swap.n_trades, noswap.n_trades)             # costs ≠ signal
        # equity reconciles with pooled trade PnLs even with swap charged per night
        self.assertAlmostEqual(swap.final_equity,
                               swap.initial_cash + swap.trade_pnls.sum(), places=4)


# ─────────────────────────── regression guard ───────────────────────────────
class TestNoRegression(unittest.TestCase):
    """SMA + single-EURUSD momentum reproduce their prior numbers EXACTLY because
    swap defaults to 0 and the swap-free engine path is byte-for-byte unchanged."""

    def test_sma_and_momentum_unchanged(self):
        try:
            from backtest import run
            from config import LEGACY_COSTS
            sma = run(strategy_name="sma_crossover", cost=REALISTIC_COSTS)
            sma_legacy = run(strategy_name="sma_crossover", cost=LEGACY_COSTS)
            mom = run(strategy_name="ts_momentum", symbol="EURUSD", timeframe_min=1440)
        except FileNotFoundError:
            self.skipTest("cached data not present")
        self.assertAlmostEqual(sma.total_return_pct, -13.0427, places=2)
        self.assertEqual(sma.n_trades, 328)
        self.assertAlmostEqual(sma_legacy.total_return_pct, -12.1814, places=2)
        self.assertAlmostEqual(mom.total_return_pct, 47.9931, places=2)
        self.assertEqual(mom.n_trades, 219)
        self.assertAlmostEqual(mom.profit_factor, 1.2956, places=3)


# ─────────────────────── inverse-vol equal-risk weighting ────────────────────
class TestVolScaling(unittest.TestCase):
    def _R(self, sigmas, n=4000, seed=0):
        rng = np.random.default_rng(seed)
        return np.column_stack([rng.normal(0.0, s, size=n) for s in sigmas])

    def test_inverse_vol_equalises_risk(self):
        R = self._R([0.005, 0.02, 0.01, 0.04])             # very different vols
        w = pf.inverse_vol_weights(R)
        self.assertAlmostEqual(float(w.sum()), 1.0, places=9)
        rc = pf.risk_contributions(R, w)
        # equal-risk by construction: every sleeve within 1.5x the median contribution
        self.assertLess(rc.max() / np.median(rc), 1.5)
        self.assertLess(np.median(rc) / rc.min(), 1.5)
        # higher-vol sleeve gets the smaller weight
        self.assertLess(w[3], w[0])

    def test_no_sleeve_dominates(self):
        R = self._R([0.001, 0.05, 0.02, 0.03, 0.008], seed=7)
        w = pf.inverse_vol_weights(R)
        rc = pf.risk_contributions(R, w)
        self.assertTrue(np.all(rc <= 1.5 * np.median(rc)))


# ─────────────────────────── portfolio aggregation ──────────────────────────
class TestAggregation(unittest.TestCase):
    def test_combine_matches_weighted_sum(self):
        rng = np.random.default_rng(1)
        R = rng.normal(0.0, 0.01, size=(500, 3))
        w = np.array([0.2, 0.5, 0.3])
        port_rets, equity = pf.combine(R, w, 10_000.0)
        np.testing.assert_allclose(port_rets, R @ w, rtol=1e-12)
        self.assertEqual(equity.shape[0], R.shape[0] + 1)
        self.assertEqual(equity[0], 10_000.0)
        self.assertAlmostEqual(equity[-1], 10_000.0 * np.prod(1.0 + port_rets), places=6)

    def test_identical_sleeves_reduce_to_single(self):
        rng = np.random.default_rng(2)
        r = rng.normal(0.0003, 0.01, size=400)
        R = np.column_stack([r, r, r])                     # 3 identical sleeves
        w = pf.inverse_vol_weights(R)
        np.testing.assert_allclose(w, np.full(3, 1 / 3), rtol=1e-9)
        port_rets, _ = pf.combine(R, w, 10_000.0)
        np.testing.assert_allclose(port_rets, r, rtol=1e-9)   # blend of clones == clone

    def test_diversification_lowers_vol(self):
        # uncorrelated equal-vol sleeves -> portfolio vol < single-sleeve vol
        rng = np.random.default_rng(3)
        R = rng.normal(0.0, 0.01, size=(5000, 4))
        w = pf.inverse_vol_weights(R)
        port_rets, _ = pf.combine(R, w, 10_000.0)
        self.assertLess(port_rets.std(), R.std(axis=0).mean())

    def test_correlation_matrix_shape_and_diag(self):
        rng = np.random.default_rng(4)
        R = rng.normal(0.0, 0.01, size=(300, 3))
        C = pf.correlation_matrix(R)
        self.assertEqual(C.shape, (3, 3))
        np.testing.assert_allclose(np.diag(C), np.ones(3), atol=1e-9)

    def test_portfolio_wf_oos_strictly_after_is(self):
        # End-to-end on the real cached basket: every fold's OOS window must start
        # strictly after its IS window ends, and pooled OOS metrics must exist.
        wf = pf.run_portfolio_wf()
        if not wf.get("ok"):
            self.skipTest("basket data not present / too short for a fold")
        for r in wf["rows"]:
            self.assertGreater(np.datetime64(r["oos_dates"][0]),
                               np.datetime64(r["is_dates"][1]))
        self.assertIn("sharpe", wf["pooled"])
        self.assertIn("max_dd_pct", wf["pooled"])
        self.assertEqual(wf["oos_equity"].shape[0],
                         sum(1 for _ in wf["rows"]) * wf["oos_bars"] + 1)

    def test_real_portfolio_sleeves_are_risk_equalised(self):
        # Acceptance: on the REAL portfolio no sleeve contributes >~1.5x the median
        # sleeve risk (w_i*sigma_i) — the equal-risk property, on live data.
        res = pf.run_portfolio()
        if not res.get("ok"):
            self.skipTest("basket data not present")
        rc = res["risk_contrib"]
        self.assertLess(rc.max() / np.median(rc), 1.5)

    def test_align_returns_is_intersection(self):
        # two sleeves with overlapping but offset date ranges -> common = overlap
        class S:
            def __init__(self, d, r):
                self.dates = d
                self.rets = r
                self.symbol = "X"
                self.n_trades = 0
        d1 = np.array(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"], dtype="datetime64[D]")
        d2 = np.array(["2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"], dtype="datetime64[D]")
        s1 = S(d1, np.array([0.1, 0.2, 0.3]))    # rets aligned to dates[1:]
        s2 = S(d2, np.array([0.4, 0.5, 0.6]))
        common, R = pf.align_returns([s1, s2])
        # dates[1:] of s1 = 02,03,04 ; of s2 = 03,04,05 ; intersection = 03,04
        self.assertEqual(list(common.astype(str)), ["2020-01-03", "2020-01-04"])
        self.assertEqual(R.shape, (2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
