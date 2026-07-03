"""dump_basket.py — pull D1 history for the Phase-4 CROSS-ASSET basket through the
WINE Python that has the MetaTrader5 package, and write it to the Linux project
cache (same CSV + symbol-JSON format as mt5_dump.py).

Unlike mt5_dump.py this does NOT upper-case the symbol — broker symbol names are
CASE-SENSITIVE (e.g. "US500Cash", "OILCash"), so upper-casing them breaks the
symbol_info() lookup. It also makes each symbol VISIBLE (symbol_select) before
copying rates, because cross-asset symbols are hidden from Market Watch by default
on this account. DEMO is HARD-verified before anything is written.

Run from Linux:
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\tools\\dump_basket.py'
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
TF_D1 = mt5.TIMEFRAME_D1
TF_MIN = 1440
MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
# The shared terminal only keeps SHALLOW D1 history on disk for non-active symbols.
# copy_rates_from_pos(0, TARGET) FAILS FAST ("Terminal: Call failed") until the
# bars are downloaded — but it never blocks, so we can POLL it: nudge the download
# with a tiny request, then keep asking for TARGET until the returned count stops
# growing (history fully pulled). NB copy_rates_RANGE was tried and BLOCKS for
# minutes on a deep range, so it is deliberately avoided here.
# KEY behaviour of this terminal: copy_rates_from_pos(0, N) SUCCEEDS when N <= the
# bars already downloaded for that symbol, but FAILS ("Terminal: Call failed") /
# BLOCKS when N exceeds them (it tries to download older history and that stalls).
# So we PROBE DOWNWARD: ask for a deep count, and on failure step down until a
# request succeeds — that captures the deepest history the terminal will actually
# serve, without hanging on an impossible deep download.
PROBE_LADDER = [7000, 5000, 4000, 3000, 2500, 2000, 1500, 1200, 1000, 700, 400]
CALL_TIMEOUT = 8.0    # a single copy_rates can BLOCK while the terminal downloads
                      # history; run it in a daemon thread and abandon it past this,
                      # so one stuck symbol can never hang the whole basket run.


def rates_with_timeout(symbol, count, timeout=CALL_TIMEOUT):
    """copy_rates_from_pos in a daemon thread; return None if it blocks past
    `timeout` (the background download keeps going for the next poll)."""
    box = {}

    def work():
        try:
            box["r"] = mt5.copy_rates_from_pos(symbol, TF_D1, 0, count)
        except Exception as e:                       # pragma: no cover
            box["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    return box.get("r")

# The cross-asset basket: FX majors + gold + a US equity index + WTI oil.
# Names verified against this broker's symbol table (XM demo). EURUSD is
# DELIBERATELY EXCLUDED here: its cached CSV is the Phase-0..3 reference series and
# must stay byte-identical so the SMA + single-EURUSD-momentum regression guard
# (identical content hashes) still holds. The portfolio reuses that existing file.
BASKET = ["GBPUSD", "USDJPY", "AUDUSD", "GOLD", "US500Cash", "OILCash"]


def init() -> None:
    if mt5.initialize(timeout=60000):
        return
    if mt5.initialize(path=TERMINAL, timeout=60000):
        return
    raise SystemExit("[BASKET-FAIL] initialize() failed: %s" % (mt5.last_error(),))


def fetch_deep(symbol: str):
    """Probe DOWNWARD through PROBE_LADDER and return the deepest series the terminal
    will serve. Two passes: a first pass nudges the download, a second pass captures
    the now-deeper result. Returns None if even the shallowest probe fails."""
    rates_with_timeout(symbol, 100, timeout=4.0)     # nudge: kick off the download
    best = None
    for _pass in range(2):
        for count in PROBE_LADDER:
            rates = rates_with_timeout(symbol, count)
            n = 0 if rates is None else len(rates)
            if n > 200:
                if best is None or n > len(best):
                    best = rates
                break                    # got this pass's deepest; restart ladder
        time.sleep(1.0)                  # let any triggered download advance
    return best


def dump_symbol(symbol: str) -> None:
    info = mt5.symbol_info(symbol)
    if info is None:
        print("[SKIP] symbol_info() None for %s" % symbol)
        return
    if not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(1.0)

    rates = fetch_deep(symbol)
    if rates is None or len(rates) == 0:
        print("[SKIP] no rates for %s: %s" % (symbol, mt5.last_error()))
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

    print("[OK] %-10s %5d bars  %s -> %s" % (
        symbol, len(rates),
        datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc).date(),
        datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc).date()))


def main() -> None:
    init()
    acct = mt5.account_info()
    if acct is None:
        raise SystemExit("[BASKET-FAIL] account_info() None: %s" % (mt5.last_error(),))
    mode = MODES.get(acct.trade_mode, "UNKNOWN(%s)" % acct.trade_mode)
    print("account login=%s server=%s %s trade_mode=%s"
          % (acct.login, acct.server, acct.currency, mode))
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("[BASKET-FAIL] trade_mode is %s, not DEMO. Refusing." % mode)

    for symbol in BASKET:
        try:
            dump_symbol(symbol)
        except Exception as e:                       # keep going on a bad symbol
            print("[SKIP] %s raised %s" % (symbol, e))
    mt5.shutdown()


if __name__ == "__main__":
    main()
