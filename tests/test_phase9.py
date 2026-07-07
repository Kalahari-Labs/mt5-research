"""Tests for Phase 9's mechanical pieces (no network): the CSV writer's
format contract with data._load_csv, the pre-registered screen rules, the
mechanical cost construction, and the brief's discipline invariants.
Run: python -m unittest discover -s tests
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase9                                                          # noqa: E402
import portfolio as pf                                                 # noqa: E402


def bars(n, spread=19.0, t0=1_700_000_000):
    return [[t0 + i * 86400, 1.1, 1.11, 1.09, 1.10, 1000.0, spread]
            for i in range(n)]


def fetched(n=2500, spread=19.0, swap_mode=1, point=1e-05, contract=100000.0):
    return {"bars": bars(n, spread),
            "spec": {"swap_mode": swap_mode, "point": point,
                     "trade_contract_size": contract},
            "median_spread_points": float(spread)}


class TestWriteBarsCsv(unittest.TestCase):
    def test_format_matches_loader_contract(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "X_1440.csv")
            n = phase9.write_bars_csv(path, bars(3))
            self.assertEqual(n, 3)
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            # exactly the columns data._load_csv reads (+ spread extra)
            for col in ("time", "open", "high", "low", "close", "tick_volume"):
                self.assertIn(col, rows[0])
            self.assertRegex(rows[0]["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertEqual(float(rows[0]["close"]), 1.10)
            self.assertLess(rows[0]["time"], rows[1]["time"])   # ascending


class TestScreen(unittest.TestCase):
    def test_never_tested_guard_rejects_basket(self):
        for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD"):
            ok, reason = phase9.screen(sym, fetched(), None)
            self.assertFalse(ok)
            self.assertIn("already OOS-evaluated", reason)

    def test_fetch_error_fails(self):
        ok, reason = phase9.screen("NZDUSD", None, "URLError: down")
        self.assertFalse(ok)
        self.assertIn("fetch failed", reason)

    def test_short_history_fails(self):
        ok, reason = phase9.screen("NZDUSD", fetched(n=pf.MIN_BARS - 1), None)
        self.assertFalse(ok)
        self.assertIn(f"< {pf.MIN_BARS}", reason)

    def test_bad_swap_mode_fails(self):
        ok, reason = phase9.screen("NZDUSD", fetched(swap_mode=2), None)
        self.assertFalse(ok)
        self.assertIn("swap_mode=2", reason)

    def test_good_candidate_passes(self):
        ok, reason = phase9.screen("NZDUSD", fetched(), None)
        self.assertTrue(ok)
        self.assertIn("2500 D1 bars", reason)


class TestHoldoutCost(unittest.TestCase):
    def test_mechanical_cost_rule(self):
        with tempfile.TemporaryDirectory():
            pass
        # write the swap json holdout_cost reads via config.load_swap_spec
        import json
        from config import swap_spec_path
        sym = "ZZTEST"
        path = swap_spec_path(sym)
        path.write_text(json.dumps({
            "name": sym, "swap_long": -8.0, "swap_short": 1.0, "swap_mode": 1,
            "swap_rollover3days": 3, "point": 1e-05, "digits": 5,
            "trade_contract_size": 100000.0}))
        try:
            cm = phase9.holdout_cost(sym, fetched(spread=19.0))
            self.assertAlmostEqual(cm.pip_size, 1e-4)          # 10 x point
            self.assertAlmostEqual(cm.spread_pips, 1.9)        # 19 pts -> pips
            self.assertAlmostEqual(cm.slippage_pips, 0.3)
            self.assertAlmostEqual(cm.commission_per_lot, 3.5)
            self.assertAlmostEqual(cm.contract_size, 100000.0)
            self.assertEqual(cm.swap_model, "directional")
            self.assertAlmostEqual(cm.swap_long_per_night, -8.0 * 1e-05)
            self.assertAlmostEqual(cm.swap_short_per_night, 1.0 * 1e-05)
            self.assertEqual(cm.swap_rate_annual, 0.0)         # one model at a time
        finally:
            path.unlink()


class TestBriefDiscipline(unittest.TestCase):
    def test_exactly_two_configs_and_gate(self):
        b = phase9.brief_payload()
        self.assertEqual(len(b["configs"]), 2)
        self.assertEqual(b["configs"][0]["strategy"], "carry_momentum")
        self.assertEqual(b["configs"][0]["max_adverse_carry_bps"], 0.0)
        self.assertEqual(b["configs"][1]["strategy"], "ts_momentum")
        self.assertIn("0.5", b["gate"])
        self.assertIn("+2", b["multiple_testing"])

    def test_candidates_disjoint_from_ever_tested(self):
        self.assertFalse(set(phase9.CANDIDATES) & set(phase9.EVER_TESTED))

    def test_unregistered_config_refused(self):
        with self.assertRaises(ValueError):
            phase9.build_sleeves("sneaky_third_config", (), {})


if __name__ == "__main__":
    unittest.main()
