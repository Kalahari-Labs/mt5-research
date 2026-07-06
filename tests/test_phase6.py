"""Tests for Phase 6 carry_momentum: the carry math vs a hand-computed oracle,
truncation invariance on the carry input (no look-ahead), flat-when-carry-adverse,
the composite z-score leg vs a loop oracle, and the guards that the symmetric-swap
path and every existing strategy are untouched.
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies                                                        # noqa: E402
from strategies.base import Strategy                                     # noqa: E402
from strategies.carry_momentum import (CarryMomentum, carry_bps_per_year,  # noqa: E402
                                       expanding_zscore)
from strategies.ts_momentum import TsMomentum                            # noqa: E402
from config import CostModel, load_swap_spec, swap_spec_path             # noqa: E402
from backtest import _simulate                                           # noqa: E402

CM = CarryMomentum()
TS = TsMomentum()


def _walk(n, seed=12345, start=1.10, vol=0.01):
    """A reproducible positive geometric random walk for fixtures."""
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0.0, vol, size=n)))


def weekday_dates(start, n):
    """`n` consecutive WEEKDAY dates from `start` (datetime64[D]) as datetime64[s]."""
    out, d = [], np.datetime64(start, "D")
    while len(out) < n:
        wd = (d.astype(int) + 3) % 7          # 1970-01-01 = Thursday -> Mon=0
        if wd < 5:
            out.append(d)
        d += 1
    return np.array(out, dtype="datetime64[s]")


def ema_oracle(x, span):
    a = 2.0 / (span + 1.0)
    e = float(x[0])
    out = [e]
    for i in range(1, len(x)):
        e = a * x[i] + (1.0 - a) * e
        out.append(e)
    return np.array(out)


# ─────────────────────────── carry math vs hand oracle ───────────────────────
class TestCarryMathOracle(unittest.TestCase):
    """The brief's required guard: bps/yr conversion matches hand arithmetic."""

    def test_scalar_matches_hand_computed(self):
        # EURUSD-like: swap_long -8.02 points × 1e-5 point = -8.02e-5 price/night.
        # At close 1.10: -8.02e-5 × 365 / 1.10 × 1e4 = -266.11818181... bps/yr.
        got = carry_bps_per_year(-8.02e-5, np.array([1.10]), 365.0)
        self.assertAlmostEqual(float(got[0]), -266.1181818181818, places=9)
        # GOLD-like short credit: +11.15 points × 0.01 = +0.1115 price/night.
        # At close 3300: 0.1115 × 365 / 3300 × 1e4 = +123.32575757... bps/yr.
        got = carry_bps_per_year(0.1115, np.array([3300.0]), 365.0)
        self.assertAlmostEqual(float(got[0]), 123.32575757575758, places=9)

    def test_per_bar_close_and_array_swap(self):
        # constant swap, varying close: carry scales as 1/close (hand-computed).
        close = np.array([1.0, 2.0, 4.0])
        got = carry_bps_per_year(-3.65e-4, close, 365.0)
        np.testing.assert_allclose(got, [-1332.25, -666.125, -333.0625], rtol=1e-12)
        # per-bar swap series: element t pairs swap[t] with close[t].
        swap = np.array([-1e-4, 0.0, 2e-4])
        got = carry_bps_per_year(swap, np.ones(3), 365.0)
        np.testing.assert_allclose(got, [-365.0, 0.0, 730.0], rtol=1e-12)

    def test_real_spec_signs(self):
        if not swap_spec_path("EURUSD").exists():
            self.skipTest("no dumped swap spec")
        d = load_swap_spec("EURUSD")
        c_long = carry_bps_per_year(d["swap_long_per_night"], np.array([1.10]))
        c_short = carry_bps_per_year(d["swap_short_per_night"], np.array([1.10]))
        self.assertLess(float(c_long[0]), 0.0)       # long EURUSD costs on this account
        self.assertGreater(float(c_short[0]), 0.0)   # short EURUSD earns a small credit


# ───────────────────────── filter: flat when carry adverse ───────────────────
class TestFlatWhenCarryAdverse(unittest.TestCase):
    def setUp(self):
        self.up = 1.0 * 1.002 ** np.arange(300)      # monotone up: TSMOM long
        self.down = 1.0 * 0.998 ** np.arange(300)    # monotone down: TSMOM short
        self.kw = dict(lookback=10, anchor=20, allow_short=True, use_anchor=True)

    def test_adverse_long_carry_flattens_everything(self):
        ts = TS.generate(self.up, **self.kw).regime
        self.assertGreater(int((ts != 0).sum()), 0)  # the baseline DOES trade
        got = CM.generate(self.up, mode="filter", max_adverse_carry_bps=100.0,
                          swap_long_per_night=-1.0, **self.kw).regime
        self.assertTrue((got == 0).all())            # hugely adverse carry -> flat

    def test_favourable_long_carry_is_a_noop(self):
        ts = TS.generate(self.up, **self.kw).regime
        got = CM.generate(self.up, mode="filter", max_adverse_carry_bps=0.0,
                          swap_long_per_night=+1e-6, **self.kw).regime
        np.testing.assert_array_equal(got, ts)       # credit side never blocked

    def test_short_side_gated_by_swap_short_only(self):
        ts = TS.generate(self.down, **self.kw).regime
        self.assertGreater(int((ts == -1).sum()), 0)
        # poison the LONG side only: shorts must be untouched
        got = CM.generate(self.down, mode="filter", max_adverse_carry_bps=0.0,
                          swap_long_per_night=-999.0, swap_short_per_night=+1e-6,
                          **self.kw).regime
        np.testing.assert_array_equal(got, ts)
        # adverse SHORT carry flattens the shorts
        got = CM.generate(self.down, mode="filter", max_adverse_carry_bps=100.0,
                          swap_short_per_night=-1.0, **self.kw).regime
        self.assertTrue((got == 0).all())

    def test_gate_none_ignores_poisoned_swaps(self):
        close = _walk(260, seed=3)
        ts = TS.generate(close, **self.kw).regime
        got = CM.generate(close, mode="filter", max_adverse_carry_bps=None,
                          swap_long_per_night=-999.0, swap_short_per_night=-999.0,
                          **self.kw).regime
        np.testing.assert_array_equal(got, ts)

    def test_default_params_are_a_structural_noop(self):
        # defaults: zero swap, X=0 -> carry 0 >= -0 -> the exact ts_momentum regime.
        close = _walk(400, seed=4)
        ts = TS.generate(close, lookback=120, anchor=200).regime
        got = CM.generate(close, lookback=120, anchor=200, **{
            k: v for k, v in CM.default_params().items()
            if k not in ("lookback", "anchor")}).regime
        np.testing.assert_array_equal(got, ts)

    def test_threshold_crossing_semantics(self):
        # swap_long = -50e-4/365 makes carry_long(t) = -50/close[t] bps/yr exactly
        # (the 365s cancel). With X=50: flat while close < 1.0, held while > 1.0.
        close = np.linspace(0.4, 2.5, 400)
        got = CM.generate(close, mode="filter", max_adverse_carry_bps=50.0,
                          swap_long_per_night=-50e-4 / 365.0, **self.kw).regime
        warm = 20
        below = (np.arange(400) >= warm + 1) & (close <= 0.99)
        above = (np.arange(400) >= warm + 1) & (close >= 1.01)
        self.assertTrue((got[below] == 0).all())     # carry worse than -50 -> flat
        self.assertTrue((got[above] == 1).all())     # carry better than -50 -> held


# ───────────── truncation invariance on the carry input (no look-ahead) ─────
class TestNoLookAheadCarryInput(unittest.TestCase):
    def _assert_causal(self, close, warm, gen):
        full = gen(len(close)).regime
        for T in range(warm + 3, len(close), 11):
            part = gen(T).regime
            np.testing.assert_array_equal(part, full[:T],
                                          err_msg=f"future leaked into regime[:{T}]")

    def test_filter_causal_with_time_varying_swap_series(self):
        # swap series oscillate so the gate genuinely toggles across the run;
        # truncating close AND the carry input together must not change the past.
        close = _walk(260, seed=7)
        t = np.arange(260)
        sl = -50e-4 / 365.0 * (1.0 + 0.9 * np.sin(t / 5.0))
        ss = -50e-4 / 365.0 * (1.0 + 0.9 * np.cos(t / 7.0))
        kw = dict(lookback=10, anchor=20, mode="filter", max_adverse_carry_bps=50.0)
        self._assert_causal(close, 20, lambda T: CM.generate(
            close[:T], swap_long_per_night=sl[:T], swap_short_per_night=ss[:T], **kw))

    def test_filter_causal_with_scalar_swap(self):
        close = _walk(260, seed=8)
        kw = dict(lookback=10, anchor=20, mode="filter", max_adverse_carry_bps=50.0,
                  swap_long_per_night=-40e-4 / 365.0, swap_short_per_night=+1e-6)
        self._assert_causal(close, 20, lambda T: CM.generate(close[:T], **kw))

    def test_composite_causal(self):
        close = _walk(260, seed=9)
        kw = dict(lookback=10, anchor=20, mode="composite", lam=0.5, carry_z=0.7)
        self._assert_causal(close, 20, lambda T: CM.generate(close[:T], **kw))


# ───────────────────────────── composite leg ────────────────────────────────
class TestCompositeZ(unittest.TestCase):
    def test_expanding_zscore_matches_loop_oracle(self):
        x = _walk(80, seed=11)
        exp = np.zeros(len(x))
        for t in range(len(x)):
            s = np.std(x[:t + 1])
            exp[t] = 0.0 if s == 0 else (x[t] - np.mean(x[:t + 1])) / s
        got = expanding_zscore(x)
        np.testing.assert_allclose(got, exp, rtol=1e-9, atol=1e-12)
        self.assertEqual(got[0], 0.0)                 # single obs -> undefined -> 0

    def test_lam_zero_matches_zscored_momentum_oracle(self):
        # lam=0 must make carry_z irrelevant (poisoned to prove it) and reduce the
        # composite to sign(expanding z of trailing return) + the anchor overlay.
        close = _walk(300, seed=12)
        L, A = 10, 25
        tr = np.array([close[t] / close[t - L] - 1.0 for t in range(L, len(close))])
        z = np.zeros(len(close))
        for i in range(len(tr)):                      # loop oracle, prefix stats
            s = np.std(tr[:i + 1])
            z[L + i] = 0.0 if s == 0 else (tr[i] - np.mean(tr[:i + 1])) / s
        exp = np.zeros(len(close), dtype=int)
        exp[z > 0] = 1
        exp[z < 0] = -1
        e = ema_oracle(close, A)
        exp[(exp == 1) & ~(close > e)] = 0
        exp[(exp == -1) & ~(close < e)] = 0
        exp[:max(L, A)] = 0
        got = CM.generate(close, lookback=L, anchor=A, mode="composite",
                          lam=0.0, carry_z=123.456).regime
        np.testing.assert_array_equal(got, exp)

    def test_huge_positive_carry_z_forces_long_only_tilt(self):
        close = _walk(300, seed=13)
        L, A = 10, 25
        got = CM.generate(close, lookback=L, anchor=A, mode="composite",
                          lam=0.5, carry_z=1e9).regime
        self.assertEqual(int((got == -1).sum()), 0)
        # score > 0 everywhere -> long wherever the anchor overlay allows it
        e = ema_oracle(close, A)
        exp = np.where(close > e, 1, 0).astype(int)
        exp[:max(L, A)] = 0
        np.testing.assert_array_equal(got, exp)

    def test_huge_negative_carry_z_forces_short_only_tilt(self):
        close = _walk(300, seed=14)
        L, A = 10, 25
        got = CM.generate(close, lookback=L, anchor=A, mode="composite",
                          lam=0.5, carry_z=-1e9).regime
        self.assertEqual(int((got == 1).sum()), 0)
        e = ema_oracle(close, A)
        exp = np.where(close < e, -1, 0).astype(int)
        exp[:max(L, A)] = 0
        np.testing.assert_array_equal(got, exp)

    def test_allow_short_false_has_no_shorts(self):
        close = _walk(300, seed=15)
        got = CM.generate(close, lookback=10, anchor=25, mode="composite",
                          lam=0.5, carry_z=-1e9, allow_short=False).regime
        self.assertEqual(int((got == -1).sum()), 0)


# ───────────────────────── contract + registration guards ────────────────────
class TestContractAndRegistration(unittest.TestCase):
    def test_registered_and_existing_strategies_still_present(self):
        names = strategies.all_names()
        for n in ("carry_momentum", "ts_momentum", "sma_crossover", "buy_and_hold"):
            self.assertIn(n, names)
        self.assertIsInstance(strategies.get("carry_momentum"), Strategy)

    def test_param_grid_is_the_preregistered_surface(self):
        g = CM.param_grid()
        self.assertEqual(set(g), {"lookback", "max_adverse_carry_bps"})
        self.assertEqual(tuple(g["max_adverse_carry_bps"]), (0.0, 50.0, 100.0))
        self.assertEqual(tuple(g["lookback"]), (20, 40, 60, 90, 120, 150, 180, 210, 252))

    def test_warmup_ignores_bps_and_swap_magnitudes(self):
        # THE trap: the base-class default warmup takes max over numeric params —
        # a 100 bps tolerance or a big swap quote must never inflate the warmup.
        self.assertEqual(CM.warmup_bars(lookback=120, anchor=200,
                                        max_adverse_carry_bps=100.0,
                                        swap_long_per_night=-500.0,
                                        nights_per_year=365.0), 200)
        self.assertEqual(CM.warmup_bars(lookback=252, anchor=200,
                                        max_adverse_carry_bps=100.0), 252)
        self.assertEqual(CM.warmup_bars(lookback=120, anchor=200,
                                        use_anchor=False,
                                        max_adverse_carry_bps=100.0), 120)

    def test_validate_params(self):
        self.assertTrue(CM.validate_params(lookback=120, max_adverse_carry_bps=0.0))
        self.assertTrue(CM.validate_params(lookback=120))
        self.assertFalse(CM.validate_params(lookback=1, max_adverse_carry_bps=0.0))
        self.assertFalse(CM.validate_params(lookback=120, max_adverse_carry_bps=-5.0))

    def test_wf_combos_are_preregistered_only(self):
        from walkforward import wf_combos
        from config import WALKFORWARD
        got = wf_combos(CM, WALKFORWARD)
        self.assertEqual(got, [{"lookback": 120, "max_adverse_carry_bps": x}
                               for x in (0.0, 50.0, 100.0)])
        self.assertTrue(all(CM.validate_params(**c) for c in got))

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            CM.generate(_walk(60), lookback=5, anchor=8, mode="yolo")


# ───────────────── symmetric-swap path untouched (through the new strategy) ──
class TestSymmetricPathUntouched(unittest.TestCase):
    def test_directional_cost_fields_ignored_under_symmetric_model(self):
        # The new strategy TRADES (favourable carry gate) while the COST model is
        # the Phase-4 symmetric drag; poisoning the directional per-night fields
        # must change nothing because swap_model stays "symmetric".
        times = weekday_dates("2024-01-01", 60)
        close = 1.0 * 1.002 ** np.arange(60)
        params = dict(lookback=5, anchor=8, mode="filter",
                      max_adverse_carry_bps=0.0, swap_long_per_night=+1e-6)
        base = CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                         commission_per_side=0.0, swap_rate_annual=0.02)
        poisoned = CostModel(spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
                             commission_per_side=0.0, swap_rate_annual=0.02,
                             swap_long_per_night=-999.0, swap_short_per_night=-999.0)
        a = _simulate(close, close, times, CM, params, 10_000.0, 1.0, True, base,
                      warmup=0, timeframe_min=1440)
        b = _simulate(close, close, times, CM, params, 10_000.0, 1.0, True, poisoned,
                      warmup=0, timeframe_min=1440)
        self.assertGreater(a.n_trades, 0)            # it really trades
        self.assertEqual(a.final_equity, b.final_equity)


if __name__ == "__main__":
    unittest.main(verbosity=2)
