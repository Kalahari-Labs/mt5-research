"""Tests for Phase 7 swapseries: the recorder's keep-first idempotency, the
loader's unit conversion + swap_mode refusal, the strictly-before-day causality
of per_bar_swap vs a loop oracle, and the bridge back to Phase 6 — a single
capture predating every bar must reproduce the constant-quote carry exactly.
Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swapseries as ss                                               # noqa: E402
from strategies.carry_momentum import carry_bps_per_year              # noqa: E402


def spec(swap_long=-8.02, swap_short=1.28, swap_mode=1, point=1e-05, **kw):
    d = {"swap_long": swap_long, "swap_short": swap_short, "swap_mode": swap_mode,
         "swap_rollover3days": 3, "point": point, "digits": 5,
         "trade_contract_size": 100000.0}
    d.update(kw)
    return d


def days(start, n):
    """n consecutive datetime64[D] days from `start` (calendar days — the
    causality contract is about dates, not trading sessions)."""
    d0 = np.datetime64(start, "D")
    return d0 + np.arange(n)


def per_bar_oracle(bar_days, cap_dates, values, fallback):
    """Loop oracle for the strictly-before-day rule."""
    out, nf = [], 0
    for b in bar_days:
        best = None
        for cd, v in zip(cap_dates, values):
            if cd < b:
                best = v
        if best is None:
            out.append(float(fallback))
            nf += 1
        else:
            out.append(float(best))
    return np.array(out), nf


class TestRowFromSpec(unittest.TestCase):
    def test_builds_row_and_derives_date(self):
        r = ss.row_from_spec(spec(), "EURUSD", source="bridge",
                             captured_utc="2026-07-02T17:37:20+00:00",
                             ref_price=1.14)
        self.assertEqual(r["symbol"], "EURUSD")
        self.assertEqual(r["date_utc"], "2026-07-02")
        self.assertEqual(r["swap_long"], -8.02)
        self.assertEqual(r["source"], "bridge")
        self.assertEqual(r["ref_price"], 1.14)
        self.assertEqual(set(r), set(ss.FIELDS))

    def test_refuses_partial_quote(self):
        broken = spec()
        del broken["swap_long"]
        with self.assertRaises(ValueError):
            ss.row_from_spec(broken, "EURUSD", source="bridge")

    def test_refuses_none_field(self):
        with self.assertRaises(ValueError):
            ss.row_from_spec(spec(point=None), "EURUSD", source="file")


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, date="2026-07-02", sym="EURUSD", **kw):
        return [ss.row_from_spec(spec(**kw), sym, source="file",
                                 captured_utc=f"{date}T17:37:20+00:00")]

    def test_keep_first_idempotency(self):
        r = self.rows()
        self.assertEqual(ss.record(r, data_dir=self.dir), (1, 0))
        self.assertEqual(ss.record(r, data_dir=self.dir), (0, 1))
        # same (symbol, date) with a DIFFERENT quote is still skipped: keep-first
        other = self.rows(swap_long=-99.0)
        self.assertEqual(ss.record(other, data_dir=self.dir), (0, 1))
        s = ss.load_series("EURUSD", data_dir=self.dir)
        self.assertEqual(s["n"], 1)
        self.assertAlmostEqual(s["swap_long_per_night"][0], -8.02 * 1e-05)

    def test_new_dates_and_symbols_append(self):
        ss.record(self.rows("2026-07-02"), data_dir=self.dir)
        self.assertEqual(ss.record(self.rows("2026-07-03"), data_dir=self.dir), (1, 0))
        self.assertEqual(ss.record(self.rows("2026-07-03", sym="GOLD"),
                                   data_dir=self.dir), (1, 0))
        self.assertEqual(ss.load_series("EURUSD", data_dir=self.dir)["n"], 2)
        self.assertEqual(ss.load_series("GOLD", data_dir=self.dir)["n"], 1)


class TestLoadSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_none_when_absent(self):
        self.assertIsNone(ss.load_series("EURUSD", data_dir=self.dir))
        ss.record([ss.row_from_spec(spec(), "GOLD", source="file",
                                    captured_utc="2026-07-02T00:00:00+00:00")],
                  data_dir=self.dir)
        self.assertIsNone(ss.load_series("EURUSD", data_dir=self.dir))

    def test_sorted_and_engine_units(self):
        # recorded out of order on purpose — the loader must sort by date
        rows = [ss.row_from_spec(spec(swap_long=-9.0, swap_short=2.0), "EURUSD",
                                 source="file",
                                 captured_utc="2026-07-05T10:00:00+00:00"),
                ss.row_from_spec(spec(swap_long=-8.0, swap_short=1.0), "EURUSD",
                                 source="file",
                                 captured_utc="2026-07-02T10:00:00+00:00")]
        ss.record(rows, data_dir=self.dir)
        s = ss.load_series("EURUSD", data_dir=self.dir)
        self.assertEqual(list(s["dates"].astype(str)), ["2026-07-02", "2026-07-05"])
        np.testing.assert_allclose(s["swap_long_per_night"],
                                   [-8.0 * 1e-05, -9.0 * 1e-05])
        np.testing.assert_allclose(s["swap_short_per_night"],
                                   [1.0 * 1e-05, 2.0 * 1e-05])

    def test_refuses_unknown_swap_mode(self):
        ss.record([ss.row_from_spec(spec(swap_mode=2), "EURUSD", source="file",
                                    captured_utc="2026-07-02T00:00:00+00:00")],
                  data_dir=self.dir)
        with self.assertRaises(ValueError):
            ss.load_series("EURUSD", data_dir=self.dir)


class TestPerBarSwap(unittest.TestCase):
    def test_strictly_before_day(self):
        bars = days("2026-07-01", 5)                    # 07-01 .. 07-05
        caps = np.array(["2026-07-03"], dtype="datetime64[D]")
        out, nf = ss.per_bar_swap(bars, caps, [-5.0], fallback=-1.0)
        # capture on 07-03 must NOT be visible on 07-03 itself, only from 07-04
        np.testing.assert_allclose(out, [-1.0, -1.0, -1.0, -5.0, -5.0])
        self.assertEqual(nf, 3)

    def test_multi_regime_vs_loop_oracle(self):
        bars = days("2026-01-01", 40)
        caps = np.array(["2026-01-05", "2026-01-12", "2026-02-01"],
                        dtype="datetime64[D]")
        vals = [-1.5, 2.25, -0.75]
        out, nf = ss.per_bar_swap(bars, caps, vals, fallback=0.5)
        exp, enf = per_bar_oracle(bars, caps, vals, 0.5)
        np.testing.assert_allclose(out, exp)
        self.assertEqual(nf, enf)

    def test_single_early_capture_reproduces_constant_carry(self):
        """The bridge back to Phase 6: one capture predating every bar must be
        bit-for-bit the constant-quote behaviour, including through
        carry_bps_per_year (which already accepts per-bar arrays)."""
        bars = days("2026-07-01", 10)
        const = -8.02 * 1e-05
        out, nf = ss.per_bar_swap(bars, np.array(["2026-06-30"],
                                                 dtype="datetime64[D]"),
                                  [const], fallback=999.0)
        self.assertEqual(nf, 0)
        np.testing.assert_array_equal(out, np.full(10, const))
        close = np.linspace(1.10, 1.15, 10)
        np.testing.assert_array_equal(carry_bps_per_year(out, close),
                                      carry_bps_per_year(const, close))

    def test_all_fallback_before_first_capture(self):
        bars = days("2026-07-01", 4)
        out, nf = ss.per_bar_swap(bars, np.array(["2026-08-01"],
                                                 dtype="datetime64[D]"),
                                  [-5.0], fallback=-2.5)
        np.testing.assert_allclose(out, np.full(4, -2.5))
        self.assertEqual(nf, 4)

    def test_rejects_unsorted_and_mismatched(self):
        bars = days("2026-07-01", 3)
        with self.assertRaises(ValueError):
            ss.per_bar_swap(bars, np.array(["2026-07-05", "2026-07-02"],
                                           dtype="datetime64[D]"), [1.0, 2.0], 0.0)
        with self.assertRaises(ValueError):
            ss.per_bar_swap(bars, np.array(["2026-07-02"],
                                           dtype="datetime64[D]"), [1.0, 2.0], 0.0)


class TestCaptureFiles(unittest.TestCase):
    def test_lands_at_true_historical_date_and_autodiscovers(self):
        with tempfile.TemporaryDirectory() as d:
            for sym, sl in (("EURUSD", -8.02), ("GOLD", -90.35)):
                (spec_d := spec(swap_long=sl))["name"] = sym
                spec_d["captured_utc"] = "2026-07-02T17:37:20.489389+00:00"
                with open(os.path.join(d, f"{sym}_swap.json"), "w") as f:
                    json.dump(spec_d, f)
            rows = ss.capture_files(data_dir=d)              # symbols=None → glob
            self.assertEqual(sorted(r["symbol"] for r in rows), ["EURUSD", "GOLD"])
            for r in rows:
                self.assertEqual(r["date_utc"], "2026-07-02")   # NOT today
                self.assertEqual(r["source"], "file")
            ss.record(rows, data_dir=d)
            s = ss.load_series("GOLD", data_dir=d)
            self.assertEqual(str(s["dates"][0]), "2026-07-02")
            self.assertAlmostEqual(s["swap_long_per_night"][0], -90.35 * 1e-05)


class TestBridgePayloadParse(unittest.TestCase):
    def test_bridge_shape_parses_without_network(self):
        payload = spec()                       # symbol_info._asdict() superset
        payload.update({"name": "EURUSD", "bid": 1.14111, "ask": 1.14120})
        r = ss.row_from_spec(payload, "EURUSD", source="bridge",
                             ref_price=payload["bid"])
        self.assertEqual(r["ref_price"], 1.14111)
        self.assertEqual(r["date_utc"], r["captured_utc"][:10])


if __name__ == "__main__":
    unittest.main()
