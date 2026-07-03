"""mt5_pull.py — Wine-side live puller (READ-ONLY).

Runs under the WINE Python that has the MetaTrader5 package. Pulls the last N
bars + current tick for every basket symbol and writes ONE JSON file to the
Linux side. HARD-verifies DEMO before touching anything. Uses only read calls:
initialize / account_info / symbol_select / symbol_info_tick / copy_rates_from_pos.
No order_* function is imported, referenced, or reachable from this file.

Run from Linux (the collector loop does this for you):
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\intel\\collectors\\mt5_pull.py'
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

OUT_PATH = r"Z:\home\flowdaaddy\mt5-research\intel\data\mt5_live.json"
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD", "US500Cash", "OILCash"]
TIMEFRAMES = {"H1": (None, 60), "H4": (None, 240), "D1": (None, 1440)}
BARS = 300  # enough for 200-EMA + level detection, shallow enough to never stall


def die(msg):
    print("[PULL-FAIL]", msg)
    try:
        mt5.shutdown()
    except Exception:
        pass
    sys.exit(1)


def main():
    if not (mt5.initialize(timeout=60000) or mt5.initialize(path=TERMINAL, timeout=60000)):
        die("initialize() failed: %s" % (mt5.last_error(),))
    TIMEFRAMES["H1"] = (mt5.TIMEFRAME_H1, 60)
    TIMEFRAMES["H4"] = (mt5.TIMEFRAME_H4, 240)
    TIMEFRAMES["D1"] = (mt5.TIMEFRAME_D1, 1440)

    acct = mt5.account_info()
    if acct is None:
        die("account_info() returned None")
    if acct.trade_mode != 0:  # 0 = ACCOUNT_TRADE_MODE_DEMO
        die("account is NOT demo (trade_mode=%s) — refusing to run" % acct.trade_mode)

    out = {"pulled_at": datetime.now(timezone.utc).isoformat(),
           "account": {"login": acct.login, "server": acct.server, "demo": True},
           "symbols": {}}

    for sym in SYMBOLS:
        entry = {"tick": None, "bars": {}}
        try:
            mt5.symbol_select(sym, True)
            time.sleep(0.2)
            tick = mt5.symbol_info_tick(sym)
            if tick:
                entry["tick"] = {"time": tick.time, "bid": tick.bid, "ask": tick.ask,
                                 "last": tick.last, "volume": tick.volume}
            for tf_name, (tf_const, _) in TIMEFRAMES.items():
                rates = mt5.copy_rates_from_pos(sym, tf_const, 0, BARS)
                if rates is None or len(rates) == 0:
                    entry["bars"][tf_name] = {"error": str(mt5.last_error())}
                    continue
                entry["bars"][tf_name] = [
                    [int(r["time"]), float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["tick_volume"])]
                    for r in rates]
        except Exception as e:  # one bad symbol must not kill the pull
            entry["error"] = repr(e)
        out["symbols"][sym] = entry

    with open(OUT_PATH, "w") as f:
        json.dump(out, f)
    ok = sum(1 for s in out["symbols"].values() if s.get("bars"))
    print("[PULL-OK] %d/%d symbols -> mt5_live.json" % (ok, len(SYMBOLS)))
    mt5.shutdown()


if __name__ == "__main__":
    main()
