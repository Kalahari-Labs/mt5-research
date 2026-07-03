"""Tests for the results registry: log/list round-trip, duplicate detection, and
the multiple-testing counter dedup. Uses a temp DB (never touches the real one).
Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry import ResultsRegistry   # noqa: E402

DM = {"symbol": "EURUSD", "timeframe": 60, "data_start": "2024-01-01",
      "data_end": "2026-06-25", "n_bars": 15000}


class TestRegistry(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.reg = ResultsRegistry(self.path)

    def tearDown(self):
        self.reg.close()
        os.unlink(self.path)

    def test_log_and_list_roundtrip(self):
        h, dup = self.reg.log_run("backtest", "sma_crossover", {"fast": 20, "slow": 50},
                                  {"spread_pips": 0.8}, DM,
                                  metrics_is={"return_pct": -13.04})
        self.assertFalse(dup)
        rows = self.reg.list_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "backtest")        # run_type
        self.assertEqual(rows[0][2], "sma_crossover")   # strategy

    def test_identical_run_detected_as_duplicate(self):
        a, dup1 = self.reg.log_run("backtest", "sma_crossover", {"fast": 20, "slow": 50},
                                   {"spread_pips": 0.8}, DM, metrics_is={"return_pct": -13.04})
        b, dup2 = self.reg.log_run("backtest", "sma_crossover", {"fast": 20, "slow": 50},
                                   {"spread_pips": 0.8}, DM, metrics_is={"return_pct": -13.04})
        self.assertEqual(a, b)         # same content hash
        self.assertFalse(dup1)
        self.assertTrue(dup2)          # second time flagged as a repeat

    def test_different_params_not_duplicate(self):
        a, _ = self.reg.log_run("backtest", "sma_crossover", {"fast": 20, "slow": 50},
                                {}, DM)
        b, dup = self.reg.log_run("backtest", "sma_crossover", {"fast": 10, "slow": 30},
                                  {}, DM)
        self.assertNotEqual(a, b)
        self.assertFalse(dup)

    def test_multiple_testing_count_dedups(self):
        cfgs = [{"fast": 20, "slow": 50}, {"fast": 10, "slow": 30}, {"fast": 20, "slow": 50}]
        self.reg.log_run("walkforward", "sma_crossover", {"grid": "a"}, {}, DM,
                         metrics_oos={"return_pct": -0.74}, oos_configs=cfgs)
        n, rows = self.reg.multiple_testing_count()
        self.assertEqual(n, 2)                      # 3 configs, 1 dup → 2 distinct
        self.assertEqual(len(rows), 2)

    def test_multiple_testing_count_accumulates_across_runs(self):
        self.reg.log_run("walkforward", "sma_crossover", {"grid": "a"}, {}, DM,
                         oos_configs=[{"fast": 20, "slow": 50}, {"fast": 10, "slow": 30}])
        # Re-logging the same configs must NOT inflate the count.
        self.reg.log_run("walkforward", "sma_crossover", {"grid": "b"}, {}, DM,
                         oos_configs=[{"fast": 20, "slow": 50}, {"fast": 10, "slow": 30}])
        self.assertEqual(self.reg.multiple_testing_count()[0], 2)
        # A genuinely new config adds exactly one.
        self.reg.log_run("walkforward", "sma_crossover", {"grid": "c"}, {}, DM,
                         oos_configs=[{"fast": 5, "slow": 200}])
        self.assertEqual(self.reg.multiple_testing_count()[0], 3)

    def test_backtest_run_does_not_add_oos_configs(self):
        self.reg.log_run("backtest", "sma_crossover", {"fast": 20, "slow": 50}, {}, DM,
                         metrics_is={"return_pct": -13.0})
        self.assertEqual(self.reg.multiple_testing_count()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
