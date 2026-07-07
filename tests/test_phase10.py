"""Tests for Phase 10's synthesis pieces: verbatim verdict extraction, the
registry summary on an isolated temp registry, forward-test doc parsing, the
swap-series trigger arithmetic, and the counter-pinned discipline invariants.
Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase10                                                         # noqa: E402
import swapseries as ss                                                # noqa: E402
from registry import ResultsRegistry                                   # noqa: E402


class TestExtractVerdict(unittest.TestCase):
    def test_takes_last_verbatim_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "X.md")
            with open(p, "w") as f:
                f.write("# doc\n**VERDICT: first.**\nmore\n"
                        "**VERDICT: the real one — GATE NOT MET.**\n")
            self.assertEqual(phase10.extract_verdict(p),
                             "the real one — GATE NOT MET.")

    def test_missing_doc_and_missing_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(phase10.extract_verdict(os.path.join(d, "no.md")))
            p = os.path.join(d, "Y.md")
            with open(p, "w") as f:
                f.write("# doc with no verdict line\n")
            self.assertIsNone(phase10.extract_verdict(p))


class TestRegistrySummary(unittest.TestCase):
    def test_summary_on_isolated_registry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reg.sqlite")
            reg = ResultsRegistry(path)
            reg.log_run("brief", "s1", {"a": 1}, {}, {"symbol": "X"})
            reg.log_run("walkforward", "s1", {"a": 1}, {}, {"symbol": "X"},
                        oos_configs=[{"a": 1}])
            reg.log_run("walkforward", "s2", {"b": 2}, {}, {"symbol": "Y"},
                        oos_configs=[{"b": 2}])
            reg.close()
            s = phase10.registry_summary(path)
        self.assertEqual(s["n_total"], 3)
        self.assertEqual(s["by_type"]["walkforward"], 2)
        self.assertEqual(s["n_mt"], 2)
        self.assertEqual(s["briefs"], [("s1", s["briefs"][0][1])])
        self.assertEqual(s["oos_strategies"], ["s1", "s2"])
        self.assertIn("2 distinct", s["warning"])


class TestForwardTestStatus(unittest.TestCase):
    def test_parses_day_equity_return(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "forward-test.md")
            with open(p, "w") as f:
                f.write("| Started | 2026-07-03 (day **4** of 90) |\n"
                        "| Current equity | 9541.29 |\n"
                        "| Return since start | -0.07% |\n")
            s = phase10.forward_test_status(p)
        self.assertTrue(s["exists"])
        self.assertEqual((s["day"], s["of"]), (4, 90))
        self.assertAlmostEqual(s["equity"], 9541.29)
        self.assertAlmostEqual(s["ret"], -0.07)

    def test_missing_doc(self):
        s = phase10.forward_test_status("/nonexistent/fwd.md")
        self.assertFalse(s["exists"])


class TestSwapSeriesStatus(unittest.TestCase):
    def _seed(self, d, dates, syms=phase10.BASKET):
        for date in dates:
            rows = [ss.row_from_spec(
                {"swap_long": -8.0, "swap_short": 1.0, "swap_mode": 1,
                 "swap_rollover3days": 3, "point": 1e-05, "digits": 5,
                 "trade_contract_size": 1e5}, sym, source="file",
                captured_utc=f"{date}T10:00:00+00:00") for sym in syms]
            ss.record(rows, data_dir=d)

    def test_trigger_not_met_on_short_span(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, ["2026-07-02", "2026-07-07"])
            s = phase10.swap_series_status(data_dir=d)
        self.assertEqual(s["span_days"], 5)
        self.assertEqual(s["min_captures"], 2)
        self.assertFalse(s["trigger_met"])
        self.assertEqual(s["n_rows"], 10)

    def test_trigger_arithmetic(self):
        # 181-day span x 120 captures per sleeve -> met
        with tempfile.TemporaryDirectory() as d:
            import numpy as np
            d0 = np.datetime64("2026-01-01")
            dates = [str(d0 + int(round(i * 181 / 119))) for i in range(120)]
            self._seed(d, dates)
            s = phase10.swap_series_status(data_dir=d)
        self.assertGreaterEqual(s["span_days"], 180)
        self.assertGreaterEqual(s["min_captures"], 120)
        self.assertTrue(s["trigger_met"])


class TestDiscipline(unittest.TestCase):
    def test_brief_declares_synthesis_only(self):
        b = phase10.brief_payload()
        self.assertIn("NO out-of-sample evaluation", b["kind"])
        self.assertTrue(any("VERBATIM" in r for r in b["rules"]))
        self.assertTrue(any("pinned" in r for r in b["rules"]))

    def test_render_includes_warning_verbatim_and_verdict(self):
        regsum = {"n_total": 3, "by_type": {"brief": 3}, "n_mt": 34,
                  "briefs": [("s1", "2026-07-07T00:00:00")],
                  "oos_strategies": ["s1"],
                  "warning": "MULTIPLE-TESTING COUNT: 34 distinct ..."}
        doc = phase10.render_doc(
            [("PHASE6.md", "GATE NOT MET."), ("PHASE7.md", None)], regsum,
            {"exists": False, "day": None, "of": None, "equity": None,
             "ret": None},
            {"per_sym": {"EURUSD": 2}, "n_rows": 2, "span_days": 5,
             "min_captures": 2, "trigger_met": False},
            {"n_trades": 6, "breaches": ["EURUSD", "GOLD"],
             "swap_totals": {"max_abs_diff": 0.0035}})
        self.assertIn("MULTIPLE-TESTING COUNT: 34 distinct ...", doc)
        self.assertIn("GATE NOT MET.", doc)
        self.assertIn("_no verdict line found_", doc)
        self.assertIn("**VERDICT: research review regenerated", doc)
        self.assertIn("EURUSD, GOLD", doc)


if __name__ == "__main__":
    unittest.main()
