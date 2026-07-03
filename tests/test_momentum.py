"""Tests for ts_momentum: the signal vs an independent numpy oracle, the no-look-
ahead (truncation-invariance) guarantee, the trend + volatility filters, the config
knobs, and the guard that adding it left SMA's walk-forward search byte-for-byte
unchanged. Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies                                   # noqa: E402
from strategies.base import Strategy                # noqa: E402
from strategies.ts_momentum import TsMomentum, ema  # noqa: E402


def _walk(n, seed=12345, start=1.10, vol=0.01):
    """A reproducible positive geometric random walk for fixtures."""
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0.0, vol, size=n)))


# --- independent oracles (deliberately different code from the strategy) ---
def core_oracle(close, L, allow_short=True):
    """sign of the trailing L-period return, computed with an explicit loop."""
    n = len(close)
    reg = np.zeros(n, dtype=int)
    for t in range(L, n):
        r = close[t] / close[t - L] - 1.0
        if r > 0:
            reg[t] = 1
        elif r < 0:
            reg[t] = -1 if allow_short else 0
    return reg


def ema_oracle(x, span):
    a = 2.0 / (span + 1.0)
    e = float(x[0])
    out = [e]
    for i in range(1, len(x)):
        e = a * x[i] + (1.0 - a) * e
        out.append(e)
    return np.array(out)


class TestRegistered(unittest.TestCase):
    def test_momentum_registered_and_sma_untouched(self):
        names = strategies.all_names()
        self.assertIn("ts_momentum", names)
        self.assertIn("sma_crossover", names)          # SMA still registered
        self.assertIsInstance(strategies.get("ts_momentum"), Strategy)


class TestCoreSignalOracle(unittest.TestCase):
    """The brief's required guard: the momentum signal matches an independent oracle."""

    def test_core_matches_oracle_no_anchor(self):
        close = _walk(120, seed=1)
        L = 9
        got = TsMomentum().generate(close, lookback=L, anchor=3, allow_short=True,
                                    use_anchor=False).regime
        exp = core_oracle(close, L, allow_short=True)
        np.testing.assert_array_equal(got, exp)

    def test_allow_short_false_has_no_shorts(self):
        close = _walk(120, seed=2)
        L = 9
        got = TsMomentum().generate(close, lookback=L, anchor=3, allow_short=False,
                                    use_anchor=False).regime
        self.assertEqual(int((got == -1).sum()), 0)
        np.testing.assert_array_equal(got, core_oracle(close, L, allow_short=False))

    def test_trend_filter_matches_oracle(self):
        close = _walk(300, seed=3)
        L, A = 10, 25
        exp = core_oracle(close, L, allow_short=True).astype(int)
        e = ema_oracle(close, A)
        exp[(exp == 1) & ~(close > e)] = 0
        exp[(exp == -1) & ~(close < e)] = 0
        exp[:max(L, A)] = 0
        got = TsMomentum().generate(close, lookback=L, anchor=A, allow_short=True,
                                    use_anchor=True).regime
        np.testing.assert_array_equal(got, exp)

    def test_filter_reduces_or_keeps_exposure(self):
        # the trend filter can only cancel signals, never create new ones.
        close = _walk(300, seed=4)
        raw = TsMomentum().generate(close, lookback=12, anchor=30, use_anchor=False).regime
        filt = TsMomentum().generate(close, lookback=12, anchor=30, use_anchor=True).regime
        self.assertLessEqual(int((filt != 0).sum()), int((raw != 0).sum()))


class TestNoLookAhead(unittest.TestCase):
    """Truncation invariance == proof regime[t] never depends on the future."""

    def _assert_causal(self, **params):
        close = _walk(260, seed=7)
        full = TsMomentum().generate(close, **params).regime
        warm = TsMomentum().warmup_bars(**params)
        for T in range(warm + 3, len(close), 11):
            part = TsMomentum().generate(close[:T], **params).regime
            np.testing.assert_array_equal(part, full[:T],
                                          err_msg=f"future leaked into regime[:{T}]")

    def test_causal_core_and_anchor(self):
        self._assert_causal(lookback=10, anchor=20, use_anchor=True)

    def test_causal_with_vol_filter(self):
        self._assert_causal(lookback=10, anchor=12, use_anchor=True,
                            vol_filter=True, vol_lookback=5, vol_window=40,
                            vol_max_pct=0.80)


class TestVolFilter(unittest.TestCase):
    def setUp(self):
        # alternating calm / choppy segments => clearly varying realized vol.
        rng = np.random.default_rng(99)
        seg = []
        for k in range(8):
            v = 0.002 if k % 2 == 0 else 0.02
            seg.append(rng.normal(0.0006, v, size=40))
        self.close = 1.10 * np.exp(np.cumsum(np.concatenate(seg)))
        self.kw = dict(lookback=10, anchor=12, use_anchor=True,
                       vol_lookback=5, vol_window=50)

    def _entries(self, regime):
        prev = np.roll(regime, 1)
        prev[0] = 0
        return int(((regime != prev) & (regime != 0)).sum())

    def test_off_by_default(self):
        self.assertFalse(TsMomentum().default_params()["vol_filter"])

    def test_full_percentile_is_noop(self):
        off = TsMomentum().generate(self.close, vol_filter=False, **self.kw).regime
        p100 = TsMomentum().generate(self.close, vol_filter=True, vol_max_pct=1.0,
                                     **self.kw).regime
        np.testing.assert_array_equal(off, p100)

    def test_strict_percentile_blocks_entries(self):
        off = TsMomentum().generate(self.close, vol_filter=False, **self.kw).regime
        p0 = TsMomentum().generate(self.close, vol_filter=True, vol_max_pct=0.0,
                                   **self.kw).regime
        self.assertLessEqual(self._entries(p0), self._entries(off))
        self.assertTrue((p0 != off).any())   # at least one entry got blocked


class TestConfigAndContract(unittest.TestCase):
    def test_param_grid_is_2d_for_robustness(self):
        g = TsMomentum().param_grid()
        self.assertEqual(set(g), {"lookback", "anchor"})   # robustness.py needs exactly 2

    def test_default_lookback_is_a_long_horizon(self):
        # documented: D1 default lookback sits in the ~100–250 bar TSMOM window.
        lb = TsMomentum().default_params()["lookback"]
        self.assertGreaterEqual(lb, 100)
        self.assertLessEqual(lb, 250)

    def test_validate_params(self):
        m = TsMomentum()
        self.assertTrue(m.validate_params(lookback=120, anchor=200))
        self.assertFalse(m.validate_params(lookback=1, anchor=200))
        self.assertFalse(m.validate_params(lookback=120, anchor=1))

    def test_warmup_bars(self):
        m = TsMomentum()
        self.assertEqual(m.warmup_bars(lookback=120, anchor=200), 200)
        self.assertEqual(m.warmup_bars(lookback=300, anchor=200), 300)
        self.assertEqual(m.warmup_bars(lookback=120, anchor=200, use_anchor=False), 120)

    def test_ema_matches_recursive_oracle(self):
        x = _walk(50, seed=5)
        np.testing.assert_allclose(ema(x, 10), ema_oracle(x, 10), rtol=1e-12, atol=1e-12)


class TestEngineSmoke(unittest.TestCase):
    """ts_momentum runs end-to-end through the UNCHANGED engine."""

    def test_runs_through_backtest(self):
        try:
            from backtest import run
        except Exception as e:                       # pragma: no cover
            self.skipTest(f"backtest import failed: {e}")
        try:
            r = run(strategy_name="ts_momentum")     # default symbol/TF cache
        except FileNotFoundError:
            self.skipTest("cached data CSV not present")
        self.assertEqual(r.strategy, "ts_momentum")
        self.assertTrue(np.isfinite(r.final_equity))
        self.assertGreaterEqual(r.n_trades, 0)


class TestWalkForwardGeneralisation(unittest.TestCase):
    """Adding momentum generalised the WF harness; SMA's search must be unchanged."""

    def test_sma_wf_combos_unchanged(self):
        from walkforward import wf_combos, _eligible_combos
        from config import WALKFORWARD
        sma = strategies.get("sma_crossover")
        expected = [{"fast": f, "slow": s}
                    for f, s in _eligible_combos(WALKFORWARD.fast_grid, WALKFORWARD.slow_grid)]
        self.assertEqual(wf_combos(sma, WALKFORWARD), expected)   # same combos AND order

    def test_momentum_wf_combos_are_validated_crossproduct(self):
        from walkforward import wf_combos
        from config import WALKFORWARD
        mom = strategies.get("ts_momentum")
        g = mom.wf_grid()
        expected = [{"lookback": lb, "anchor": an}
                    for lb in g["lookback"] for an in g["anchor"]]
        got = wf_combos(mom, WALKFORWARD)
        self.assertEqual(got, expected)
        self.assertTrue(all(mom.validate_params(**c) for c in got))


if __name__ == "__main__":
    unittest.main(verbosity=2)
