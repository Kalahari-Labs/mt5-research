"""dump_h4.py — pull H4 history + REAL swap specs for the Phase-5 basket through
the WINE Python that has the MetaTrader5 package.

Two jobs, one DEMO-verified session:
  1. {sym}_240.csv       — deepest H4 series the terminal will serve (probe-down,
                           same approach as dump_basket.py; copy_rates blocks/fails
                           on counts deeper than what the terminal has downloaded).
  2. {sym}_swap.json     — swap_long / swap_short / swap_mode / swap_rollover3days
                           + point, captured for the Phase-4b DIRECTIONAL swap model.
                           Written to NEW files so the existing {sym}_symbol.json
                           (hashed by prior phases) stay byte-identical.

EURUSD IS included here: EURUSD_240.csv is a NEW cache file; the protected Phase-0..3
reference series are EURUSD_60.csv / EURUSD_1440.csv, which this never touches.

Run from Linux:
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\tools\\dump_h4.py'
"""
from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

OUT_DIR = r"Z:\home\flowdaaddy\mt5-research\data"
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
TF_H4 = mt5.TIMEFRAME_H4
TF_MIN = 240
MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
# H4 runs ~1,560 bars/yr, so ~5 years needs ~7,800 bars — the ladder tops well
# above that in case the terminal serves deeper history.
PROBE_LADDER = [20000, 15000, 12000, 10000, 8000, 7000, 6000, 5000,
                4000, 3000, 2500, 2000, 1500, 1200, 1000, 700, 400]
CALL_TIMEOUT = 8.0

BASKET = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD"]


def rates_with_timeout(symbol, count, timeout=CALL_TIMEOUT):
    """copy_rates_from_pos in a daemon thread; None if it blocks past `timeout`
    (the background download keeps going for the next poll)."""
    box = {}

    def work():
        try:
            box["r"] = mt5.copy_rates_from_pos(symbol, TF_H4, 0, count)
        except Exception as e:                       # pragma: no cover
            box["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    return box.get("r")


def fetch_deep(symbol: str):
    """Probe DOWNWARD through PROBE_LADDER; two passes (nudge, then capture)."""
    rates_with_timeout(symbol, 100, timeout=4.0)     # nudge: kick off the download
    best = None
    for _pass in range(3):                           # H4 downloads are bigger: 3 passes
        for count in PROBE_LADDER:
            rates = rates_with_timeout(symbol, count)
            n = 0 if rates is None else len(rates)
            if n > 200:
                if best is None or n > len(best):
                    best = rates
                break
        time.sleep(1.5)
    return best


def dump_symbol(symbol: str) -> None:
    info = mt5.symbol_info(symbol)
    if info is None:
        print("[SKIP] symbol_info() None for %s" % symbol)
        return
    if not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(1.0)
        info = mt5.symbol_info(symbol)

    # --- swap spec first (cheap, independent of history depth) ---------------
    with open(OUT_DIR + "\\%s_swap.json" % symbol, "w") as f:
        json.dump({
            "name": info.name,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            # ENUM_SYMBOL_SWAP_MODE: 0 disabled, 1 points, 2 base ccy, 3 margin ccy,
            # 4 deposit ccy, 5/6 interest, 7/8 reopen
            "swap_mode": info.swap_mode,
            # ENUM_DAY_OF_WEEK: 0 Sunday ... 3 Wednesday ... 6 Saturday
            "swap_rollover3days": info.swap_rollover3days,
            "point": info.point,
            "digits": info.digits,
            "trade_contract_size": info.trade_contract_size,
            "captured_utc": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)
    print("[SWAP] %-8s long=%.2f short=%.2f mode=%d roll3day=%d point=%g"
          % (symbol, info.swap_long, info.swap_short, info.swap_mode,
             info.swap_rollover3days, info.point))

    # --- H4 history -----------------------------------------------------------
    rates = fetch_deep(symbol)
    if rates is None or len(rates) == 0:
        print("[SKIP] no H4 rates for %s: %s" % (symbol, mt5.last_error()))
        return

    csv_path = OUT_DIR + "\\%s_%s.csv" % (symbol, TF_MIN)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close",
                    "tick_volume", "spread", "real_volume"])
        for b in rates:
            ts = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), b["open"], b["high"],
                        b["low"], b["close"], int(b["tick_volume"]),
                        int(b["spread"]), int(b["real_volume"])])

    print("[OK] %-10s %6d H4 bars  %s -> %s" % (
        symbol, len(rates),
        datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc).date(),
        datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc).date()))


def main() -> None:
    if not (mt5.initialize(timeout=60000) or mt5.initialize(path=TERMINAL, timeout=60000)):
        raise SystemExit("[H4-FAIL] initialize() failed: %s" % (mt5.last_error(),))
    acct = mt5.account_info()
    if acct is None:
        raise SystemExit("[H4-FAIL] account_info() None: %s" % (mt5.last_error(),))
    mode = MODES.get(acct.trade_mode, "UNKNOWN(%s)" % acct.trade_mode)
    print("account login=%s server=%s %s trade_mode=%s"
          % (acct.login, acct.server, acct.currency, mode))
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("[H4-FAIL] trade_mode is %s, not DEMO. Refusing." % mode)

    for symbol in BASKET:
        try:
            dump_symbol(symbol)
        except Exception as e:                       # keep going on a bad symbol
            print("[SKIP] %s raised %s" % (symbol, e))
    mt5.shutdown()


if __name__ == "__main__":
    main()
