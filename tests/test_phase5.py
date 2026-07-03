"""Tests for Phase 4b (directional per-side swap, triple-swap Wednesday) and the
Phase-5 plumbing (H4 timestamp alignment, holding-period measurement).
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CostModel, cost_for, load_swap_spec, swap_spec_path  # noqa: E402
from backtest import _simulate                                          # noqa: E402
import strategies                                                       # noqa: E402
from strategies.base import Strategy, Signals                           # noqa: E402
import portfolio as pf                                                  # noqa: E402

BH = strategies.get("buy_and_hold")


class SellAndHold(Strategy):
    """Always-short mirror of buy_and_hold — proves the SHORT side of the
    directional swap is the one charged."""
    name = "sell_and_hold"

    def generate(self, close, **params) -> Signals:
        n = np.asarray(close).shape[0]
        regime = -np.ones(n, dtype=int)
        entries = np.zeros(n, dtype=int)
        if n:
            entries[0] = -1
        return Signals(regime=regime, entries=entries)


def weekday_dates(start, n):
    """`n` consecutive WEEKDAY dates from `start` (datetime64[D]) as datetime64[s]."""
    out, d = [], np.datetime64(start, "D")
    while len(out) < n:
        wd = (d.astype(int) + 3) % 7          # 1970-01-01 = Thursday -> Mon=0
        if wd < 5:
            out.append(d)
        d += 1
    return np.array(out, dtype="datetime64[s]")


def dcost(long_pn=0.0, short_pn=0.0, triple=2) -> CostModel:
    """Zero trading costs + directional swap only: isolates the financing math."""
    return CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                     commission_per_side=0.0, swap_model="directional",
                     swap_long_per_night=long_pn, swap_short_per_night=short_pn,
                     swap_triple_weekday=triple)


def flat_run(strategy, cost, times, price=1.0, cash=10_000.0):
    """Run on a constant-price series so the ONLY P&L is financing."""
    n = times.shape[0]
    px = np.full(n, price)
    return _simulate(px, px, times, strategy, {}, cash, 1.0, True, cost,
                     warmup=0, timeframe_min=1440)


class TestDirectionalSwapMath(unittest.TestCase):
    """Two full Mon–Fri weeks + a final Monday: position opens at bar 1 (Tue) and is
    carried through 9 bar-transitions whose rollover-night multipliers are
    Tue1 Wed3 Thu1 Fri1(weekend) Mon1 Tue1 Wed3 Thu1 Fri1 = 13 nights total."""

    def setUp(self):
        self.times = weekday_dates("2024-01-01", 11)     # 2024-01-01 was a Monday

    def test_long_cost_charges_13_nights(self):
        r = flat_run(BH, dcost(long_pn=-1e-4), self.times)
        # 10,000 units held × 1e-4 price/night × 13 nights = 13.00 cost
        self.assertAlmostEqual(r.total_swap_cost, 13.0, places=9)
        self.assertAlmostEqual(r.final_equity, 10_000.0 - 13.0, places=9)

    def test_long_credit_is_a_gain(self):
        r = flat_run(BH, dcost(long_pn=+1e-4), self.times)
        self.assertAlmostEqual(r.total_swap_cost, -13.0, places=9)   # negative = credit
        self.assertAlmostEqual(r.final_equity, 10_000.0 + 13.0, places=9)

    def test_short_side_uses_swap_short(self):
        # poison the long side: if the engine picked the wrong side this explodes
        r = flat_run(SellAndHold(), dcost(long_pn=-999.0, short_pn=-1e-4), self.times)
        self.assertAlmostEqual(r.total_swap_cost, 13.0, places=9)

    def test_equity_reconciles_with_trade_pnls_including_credits(self):
        r = flat_run(BH, dcost(long_pn=+1e-4), self.times)
        self.assertAlmostEqual(r.final_equity,
                               r.initial_cash + r.trade_pnls.sum(), places=6)


class TestTripleSwapDay(unittest.TestCase):
    def test_wednesday_charges_3x(self):
        # Mon..Fri, one week: transitions Tue(1) Wed(3) Thu(1) = 5 nights
        times = weekday_dates("2024-01-01", 5)
        r = flat_run(BH, dcost(long_pn=-1e-4, triple=2), times)
        self.assertAlmostEqual(r.total_swap_cost, 5.0, places=9)

    def test_triple_day_is_configurable(self):
        # same week, triple on FRIDAY (not crossed before the run ends) = 3 nights
        times = weekday_dates("2024-01-01", 5)
        r = flat_run(BH, dcost(long_pn=-1e-4, triple=4), times)
        self.assertAlmostEqual(r.total_swap_cost, 3.0, places=9)

    def test_weekend_nights_are_free(self):
        # Fri -> Mon transition: E ∈ {Fri, Sat, Sun} -> only Friday charges (1×)
        times = np.array(["2024-01-05", "2024-01-08"], dtype="datetime64[s]")
        prices = np.full(2, 1.0)
        # hold from bar 1 impossible with 2 bars (opens at bar 1, no later transition)
        # -> use 3 bars: Thu, Fri, Mon; position opens Fri (bar 1), Fri->Mon charges 1
        times = np.array(["2024-01-04", "2024-01-05", "2024-01-08"], dtype="datetime64[s]")
        r = flat_run(BH, dcost(long_pn=-1e-4), times)
        self.assertAlmostEqual(r.total_swap_cost, 1.0, places=9)

    def test_intraday_h4_bars_charge_only_on_midnight(self):
        # Six H4 bars inside Tue 2024-01-02, then Wed 00:00: exactly ONE rollover
        # night (E = Tuesday, 1×) despite 6 bar-transitions.
        stamps = [f"2024-01-02T{h:02d}:00" for h in (0, 4, 8, 12, 16, 20)]
        stamps.append("2024-01-03T00:00")
        times = np.array(stamps, dtype="datetime64[s]")
        r = flat_run(BH, dcost(long_pn=-1e-4), times)
        self.assertAlmostEqual(r.total_swap_cost, 1.0, places=9)


class TestSymmetricPathUntouched(unittest.TestCase):
    def test_directional_fields_ignored_when_model_symmetric(self):
        times = weekday_dates("2024-01-01", 11)
        base = CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                         commission_per_side=0.0, swap_rate_annual=0.02)
        poisoned = CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                             commission_per_side=0.0, swap_rate_annual=0.02,
                             swap_long_per_night=-999.0, swap_short_per_night=-999.0)
        a = flat_run(BH, base, times)
        b = flat_run(BH, poisoned, times)
        self.assertEqual(a.final_equity, b.final_equity)   # byte-identical path


class TestSwapSpecLoading(unittest.TestCase):
    def test_conversion_matches_raw_quote(self):
        if not swap_spec_path("EURUSD").exists():
            self.skipTest("no dumped swap spec")
        d = load_swap_spec("EURUSD")
        raw = d["raw"]
        self.assertAlmostEqual(d["swap_long_per_night"],
                               raw["swap_long"] * raw["point"], places=12)
        self.assertAlmostEqual(d["swap_short_per_night"],
                               raw["swap_short"] * raw["point"], places=12)
        self.assertIn(d["swap_triple_weekday"], range(7))
        # MT5 ENUM_DAY_OF_WEEK Wednesday=3 -> Python Wednesday=2
        if raw["swap_rollover3days"] == 3:
            self.assertEqual(d["swap_triple_weekday"], 2)

    def test_cost_for_directional_builds(self):
        if not swap_spec_path("EURUSD").exists():
            self.skipTest("no dumped swap spec")
        c = cost_for("EURUSD", swap_model="directional")
        self.assertEqual(c.swap_model, "directional")
        self.assertEqual(c.swap_rate_annual, 0.0)          # one financing model at a time
        self.assertEqual(c.spread_pips, cost_for("EURUSD").spread_pips)


class TestHoldingPeriods(unittest.TestCase):
    def test_single_hold_measured_in_bars_and_days(self):
        times = weekday_dates("2024-01-01", 11)
        r = flat_run(BH, dcost(), times)
        self.assertEqual(r.n_trades, 1)
        self.assertEqual(len(r.holding_bars), 1)
        self.assertEqual(r.holding_bars[0], 9.0)           # opened bar 1, closed bar 10
        # Tue 2024-01-02 -> Mon 2024-01-15 = 13 calendar days
        self.assertAlmostEqual(r.holding_days[0], 13.0, places=9)

    def test_holding_arrays_match_trade_count_on_real_data(self):
        try:
            from backtest import run
            r = run(strategy_name="ts_momentum", symbol="EURUSD", timeframe_min=1440)
        except FileNotFoundError:
            self.skipTest("cached EURUSD D1 data not present")
        self.assertEqual(len(r.holding_bars), r.n_trades)
        self.assertEqual(len(r.holding_days), r.n_trades)
        self.assertTrue(np.all(r.holding_bars >= 0))
        self.assertTrue(np.all(r.holding_days >= 0))


class TestH4Alignment(unittest.TestCase):
    class S:
        def __init__(self, d, r):
            self.dates = d
            self.rets = r
            self.symbol = "X"
            self.n_trades = 0

    def test_timestamp_resolution_keeps_intraday_bars(self):
        d1 = np.array(["2020-01-01T00:00", "2020-01-01T04:00", "2020-01-01T08:00",
                       "2020-01-01T12:00"], dtype="datetime64[s]")
        d2 = np.array(["2020-01-01T04:00", "2020-01-01T08:00", "2020-01-01T12:00",
                       "2020-01-01T16:00"], dtype="datetime64[s]")
        s1 = self.S(d1, np.array([0.1, 0.2, 0.3]))         # rets keyed to dates[1:]
        s2 = self.S(d2, np.array([0.4, 0.5, 0.6]))
        common, R = pf.align_returns([s1, s2], resolution="s")
        # dates[1:] of s1 = 04,08,12 ; of s2 = 08,12,16 ; intersection = 08,12
        self.assertEqual(common.shape[0], 2)
        self.assertEqual(R.shape, (2, 2))
        np.testing.assert_allclose(R[:, 0], [0.2, 0.3])
        np.testing.assert_allclose(R[:, 1], [0.4, 0.5])

    def test_day_resolution_collides_on_intraday_bars(self):
        # the documented failure mode day-keys have on H4: 3 bars -> ONE day key
        d1 = np.array(["2020-01-01T00:00", "2020-01-01T04:00", "2020-01-01T08:00",
                       "2020-01-01T12:00"], dtype="datetime64[s]")
        s1 = self.S(d1, np.array([0.1, 0.2, 0.3]))
        common, _ = pf.align_returns([s1, s1], resolution="D")
        self.assertEqual(common.shape[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
