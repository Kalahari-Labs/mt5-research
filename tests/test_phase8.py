"""Tests for Phase 8 forwardtest: the night-counting convention vs a loop
oracle mirroring backtest.py's nights_mult, deterministic currency conversion,
expected-swap math, spread pips conversion, strict read-only journal access,
and the end-to-end report against a hand-computed synthetic journal.
Run: python -m unittest discover -s tests
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forwardtest as ft                                              # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────
def make_spec(swap_long=-8.02e-05, swap_short=1.28e-05, triple=2,
              contract=100000.0, point=1e-05):
    """Shape-compatible with config.load_swap_spec's return."""
    return {"swap_long_per_night": swap_long, "swap_short_per_night": swap_short,
            "swap_triple_weekday": triple,
            "raw": {"trade_contract_size": contract, "point": point}}


def make_cost(spread_pips=0.8, pip_size=1e-4):
    return SimpleNamespace(spread_pips=spread_pips, pip_size=pip_size)


TRADE_COLS = ("ticket", "symbol", "strategy", "side", "volume", "entry_time",
              "entry_price", "sl", "tp", "exit_time", "exit_price", "pnl",
              "swap", "commission", "r_multiple", "exit_reason", "status",
              "entry_spread_points", "entry_atr", "context", "timeframe",
              "partial_closed")


def make_journal(path, trades):
    c = sqlite3.connect(path)
    c.execute(f"CREATE TABLE trades ({','.join(TRADE_COLS)})")
    for t in trades:
        row = {k: None for k in TRADE_COLS}
        row.update({"status": "closed", "swap": 0.0, "commission": 0.0}, **t)
        c.execute(f"INSERT INTO trades ({','.join(row)}) VALUES "
                  f"({','.join('?' * len(row))})", list(row.values()))
    c.commit()
    c.close()


def trade(ticket=1, symbol="EURUSD", side="buy", volume=0.01,
          entry_time="2026-07-03T07:00:00Z", exit_time="2026-07-03T10:00:00Z",
          exit_price=1.14, **kw):
    return dict(ticket=ticket, symbol=symbol, strategy="t", side=side,
                volume=volume, entry_time=entry_time, entry_price=1.14,
                exit_time=exit_time, exit_price=exit_price, pnl=0.0, **kw)


def nights_oracle(entry, exit_, triple):
    """Literal transcription of backtest.py's nights_mult inner loop."""
    e = np.datetime64(entry.replace("Z", "")[:19], "s")
    x = np.datetime64(exit_.replace("Z", "")[:19], "s")
    d0 = e.astype("datetime64[D]").astype(np.int64)
    d1 = x.astype("datetime64[D]").astype(np.int64)
    n = 0.0
    for d in range(d0, d1):
        wd = (d + 3) % 7
        if wd < 5:
            n += 3.0 if wd == triple else 1.0
    return n


class TestNightsBetween(unittest.TestCase):
    # 2026-07-06 = Monday, 2026-07-08 = Wednesday, 2026-07-10 = Friday
    def test_intraday_is_zero(self):
        self.assertEqual(ft.nights_between("2026-07-06T07:00:00Z",
                                           "2026-07-06T23:59:59Z", 2), 0.0)

    def test_single_weeknight(self):
        self.assertEqual(ft.nights_between("2026-07-06T20:05:42Z",
                                           "2026-07-07T04:16:34Z", 2), 1.0)

    def test_triple_day(self):
        self.assertEqual(ft.nights_between("2026-07-08T10:00:00Z",
                                           "2026-07-09T10:00:00Z", 2), 3.0)

    def test_weekend_charges_nothing(self):
        # Friday 10:00 -> Monday 10:00: Fri 1x, Sat 0, Sun 0
        self.assertEqual(ft.nights_between("2026-07-10T10:00:00Z",
                                           "2026-07-13T10:00:00Z", 2), 1.0)

    def test_full_week_vs_oracle(self):
        e, x = "2026-07-06T09:00:00Z", "2026-07-13T09:00:00Z"
        for trip in range(5):
            self.assertEqual(ft.nights_between(e, x, trip),
                             nights_oracle(e, x, trip),
                             msg=f"triple={trip}")
        self.assertEqual(ft.nights_between(e, x, 2), 7.0)   # 1+1+3+1+1


class TestQuoteToAccountFx(unittest.TestCase):
    def test_usd_quoted_is_identity(self):
        for sym in ("EURUSD", "GBPUSD", "AUDUSD", "GOLD"):
            self.assertEqual(ft.quote_to_account_fx(sym, 123.4), 1.0)

    def test_usd_base_inverts_exit(self):
        self.assertAlmostEqual(ft.quote_to_account_fx("USDJPY", 160.0), 1 / 160.0)

    def test_unknown_cross_is_none(self):
        self.assertIsNone(ft.quote_to_account_fx("EURJPY", 170.0))


class TestExpectedSwap(unittest.TestCase):
    def test_buy_uses_long_and_scales(self):
        # GOLD-like: -0.9035 px/night, contract 100, 0.01 lots -> 1 unit, 1 night
        spec = make_spec(swap_long=-0.9035, swap_short=0.1115,
                         contract=100.0, point=0.01)
        t = trade(symbol="GOLD", side="buy", volume=0.01,
                  entry_time="2026-07-06T20:05:42Z",
                  exit_time="2026-07-07T04:16:34Z", exit_price=4136.54)
        v, note = ft.expected_swap_ccy(t, spec)
        self.assertAlmostEqual(v, -0.9035)
        self.assertEqual(note, "1 night(s)")

    def test_sell_uses_short(self):
        spec = make_spec(swap_long=-0.9035, swap_short=0.1115,
                         contract=100.0, point=0.01)
        t = trade(symbol="GOLD", side="sell", volume=0.02,
                  entry_time="2026-07-06T20:00:00Z",
                  exit_time="2026-07-07T04:00:00Z", exit_price=4136.54)
        v, _ = ft.expected_swap_ccy(t, spec)
        self.assertAlmostEqual(v, 0.1115 * 2.0)      # 0.02 lots x 100 = 2 units

    def test_usdjpy_converts_to_usd(self):
        spec = make_spec(swap_long=0.00211, swap_short=-0.02959)
        t = trade(symbol="USDJPY", side="buy", volume=0.01,
                  entry_time="2026-07-06T20:00:00Z",
                  exit_time="2026-07-07T04:00:00Z", exit_price=160.0)
        v, _ = ft.expected_swap_ccy(t, spec)
        self.assertAlmostEqual(v, 0.00211 * 1000.0 / 160.0)

    def test_unconvertible_is_none(self):
        t = trade(symbol="EURJPY", side="buy",
                  entry_time="2026-07-06T20:00:00Z",
                  exit_time="2026-07-07T04:00:00Z", exit_price=170.0)
        v, note = ft.expected_swap_ccy(t, make_spec())
        self.assertIsNone(v)
        self.assertIn("unconverted", note)


class TestSpreadPips(unittest.TestCase):
    def test_points_to_pips(self):
        t = trade(entry_spread_points=19.0)
        self.assertAlmostEqual(
            ft.realized_spread_pips(t, make_spec(), make_cost()), 1.9)

    def test_missing_is_none(self):
        self.assertIsNone(
            ft.realized_spread_pips(trade(), make_spec(), make_cost()))


class TestLoadTrades(unittest.TestCase):
    def test_missing_journal_refuses_loudly(self):
        with self.assertRaises(FileNotFoundError):
            ft.load_trades("/nonexistent/nowhere.sqlite")

    def test_reads_closed_only_and_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "executor.sqlite")
            make_journal(db, [trade(ticket=1),
                              dict(trade(ticket=2), status="open")])
            rows = ft.load_trades(db)
            self.assertEqual([r["ticket"] for r in rows], [1])
            ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO trades (ticket,status) VALUES (9,'closed')")
            ro.close()


class TestBuildReport(unittest.TestCase):
    def test_end_to_end_hand_computed(self):
        specs = {"EURUSD": make_spec(),
                 "GOLD": make_spec(swap_long=-0.9035, swap_short=0.1115,
                                   contract=100.0, point=0.01)}
        costs = {"EURUSD": make_cost(0.8, 1e-4), "GOLD": make_cost(25.0, 0.01)}
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "executor.sqlite")
            make_journal(db, [
                trade(ticket=1, entry_spread_points=19.0),                # 1.9p
                trade(ticket=2, entry_spread_points=9.0),                 # 0.9p
                trade(ticket=3, symbol="GOLD", entry_spread_points=52.0,  # 52p
                      side="buy", entry_time="2026-07-06T20:05:42Z",
                      exit_time="2026-07-07T04:16:34Z", exit_price=4136.54,
                      swap=-0.90),
                trade(ticket=4, symbol="MYSTERY", entry_spread_points=5.0),
            ])
            rep = ft.build_report(db, spec_fn=specs.get, cost_fn=costs.get)
        self.assertEqual(rep["n_trades"], 4)
        self.assertEqual(rep["no_spec_symbols"], ["MYSTERY"])
        eu = rep["by_symbol"]["EURUSD"]
        self.assertAlmostEqual(eu["median_realized"], 1.4)   # median(1.9, 0.9)
        self.assertAlmostEqual(eu["ratio"], 1.75)
        self.assertTrue(eu["breach"])                        # 1.75 > 1.5
        gd = rep["by_symbol"]["GOLD"]
        self.assertAlmostEqual(gd["ratio"], 52.0 / 25.0)
        self.assertTrue(gd["breach"])
        self.assertEqual(rep["breaches"], ["EURUSD", "GOLD"])
        st = rep["swap_totals"]
        self.assertEqual(st["n"], 3)                         # MYSTERY excluded
        self.assertAlmostEqual(st["realized"], -0.90)
        self.assertAlmostEqual(st["expected"], -0.9035)
        self.assertAlmostEqual(st["max_abs_diff"], 0.0035)
        self.assertTrue(any("slippage" in g for g in rep["schema_gaps"]))

    def test_no_breach_below_threshold(self):
        specs = {"EURUSD": make_spec()}
        costs = {"EURUSD": make_cost(0.8, 1e-4)}
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "executor.sqlite")
            make_journal(db, [trade(ticket=1, entry_spread_points=10.0)])  # 1.0p
            rep = ft.build_report(db, spec_fn=specs.get, cost_fn=costs.get)
        self.assertAlmostEqual(rep["by_symbol"]["EURUSD"]["ratio"], 1.25)
        self.assertEqual(rep["breaches"], [])


if __name__ == "__main__":
    unittest.main()
