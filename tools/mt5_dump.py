"""One-shot REAL historical-data dumper — runs under WINE Python (the one that
has the MetaTrader5 package). It launches the MT5 terminal, HARD-verifies the
account is DEMO, then writes to the Linux-side project data cache via the Z:
drive:

  data/<SYMBOL>_<TFMIN>.csv   real OHLCV history
  data/<SYMBOL>_symbol.json   real contract specs (tick value/size, lot step...)
  data/account.json           account login/server/currency/balance/trade_mode

This is the bridge that gets REAL broker data onto this Linux box, where the
official MetaTrader5 package cannot import. The Linux backtest never talks to
MT5 directly; it reads the CSV this script produces.

Run from Linux:
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\tools\\mt5_dump.py' EURUSD 60 15000
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

OUT_DIR = r"Z:\home\flowdaaddy\mt5-research\data"
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"

TF_MAP = {
    1: mt5.TIMEFRAME_M1, 5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15,
    30: mt5.TIMEFRAME_M30, 60: mt5.TIMEFRAME_H1, 240: mt5.TIMEFRAME_H4,
    1440: mt5.TIMEFRAME_D1, 10080: mt5.TIMEFRAME_W1, 43200: mt5.TIMEFRAME_MN1,
}
MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}  # ENUM_ACCOUNT_TRADE_MODE (verified)


def die(msg: str) -> None:
    print("[DUMP-FAIL]", msg)
    try:
        mt5.shutdown()
    except Exception:
        pass
    sys.exit(1)


def init() -> None:
    # mt5.initialize() auto-launches the installed terminal and logs into the
    # last-used account.
    if mt5.initialize(timeout=60000):
        return
    if mt5.initialize(path=TERMINAL, timeout=60000):
        return
    die("initialize() failed: %s — is the terminal installed & logged in?"
        % (mt5.last_error(),))


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "EURUSD").upper()
    tf_min = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 15000
    tf = TF_MAP.get(tf_min)
    if tf is None:
        die("bad timeframe %s; valid: %s" % (tf_min, sorted(TF_MAP)))

    init()

    acct = mt5.account_info()
    if acct is None:
        die("account_info() is None — terminal not logged into an account. %s"
            % (mt5.last_error(),))
    mode = MODES.get(acct.trade_mode, "UNKNOWN(%s)" % acct.trade_mode)
    print("account login=%s server=%s balance=%s %s trade_mode=%s"
          % (acct.login, acct.server, acct.balance, acct.currency, mode))
    # Not an order path, but keep the DEMO discipline everywhere.
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        die("trade_mode is %s, not DEMO. Refusing even to dump data." % mode)

    info = mt5.symbol_info(symbol)
    if info is None:
        die("symbol_info() None for %s" % symbol)
    if not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(0.5)

    # First copy_rates can return few bars while the terminal back-fills history;
    # retry until we have a healthy sample.
    rates = None
    for _ in range(8):
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, limit)
        if rates is not None and len(rates) >= min(limit, 3000):
            break
        time.sleep(2)
    if rates is None or len(rates) == 0:
        die("no rates for %s TF%s: %s" % (symbol, tf_min, mt5.last_error()))

    csv_path = OUT_DIR + "\\%s_%s.csv" % (symbol, tf_min)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close",
                    "tick_volume", "spread", "real_volume"])
        for b in rates:
            ts = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), b["open"], b["high"],
                        b["low"], b["close"], int(b["tick_volume"]),
                        int(b["spread"]), int(b["real_volume"])])

    with open(OUT_DIR + "\\%s_symbol.json" % symbol, "w") as f:
        json.dump({
            "name": info.name, "digits": info.digits, "point": info.point,
            "volume_min": info.volume_min, "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_contract_size": info.trade_contract_size,
            "trade_tick_value": info.trade_tick_value,
            "trade_tick_size": info.trade_tick_size,
            "currency_profit": info.currency_profit,
            "currency_margin": info.currency_margin,
            "description": info.description,
        }, f, indent=2)

    with open(OUT_DIR + "\\account.json", "w") as f:
        json.dump({
            "login": acct.login, "server": acct.server, "currency": acct.currency,
            "balance": acct.balance, "leverage": acct.leverage, "trade_mode": mode,
        }, f, indent=2)

    print("[DUMP-OK] %s bars -> %s" % (len(rates), csv_path))
    print("[DUMP-OK] first=%s last=%s" % (
        datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc),
        datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc)))
    mt5.shutdown()


if __name__ == "__main__":
    main()
