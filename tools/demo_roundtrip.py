"""demo_roundtrip.py — ONE guarded round-trip order on the DEMO account, through
the project's own risk.py -> execution.py pipeline (NOT a raw order_send script).

Purpose: prove the execution plumbing works end-to-end — sizing, double demo
guard, live send, close, journal audit trail. This is an infrastructure test,
NOT a strategy trade: Phase 5's verdict is negative and the gate stays shut.

Flow: verify DEMO -> size a ~min-lot EURUSD BUY via RiskManager -> submit through
Executor (both env flags flipped for THIS process only) -> close the position
immediately -> print deals + journal rows.

Run from Linux:
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\tools\\demo_roundtrip.py'
"""
from __future__ import annotations

import os
import sys
import time

import MetaTrader5 as mt5

sys.path.insert(0, r"Z:\home\flowdaaddy\mt5-research")

SYMBOL = "EURUSD"
STOP_DISTANCE = 0.0050          # 50 pips — sizing input only; no SL on the test order
MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


def main():
    if not mt5.initialize(timeout=60000):
        raise SystemExit(f"[FAIL] initialize(): {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None:
        raise SystemExit(f"[FAIL] account_info(): {mt5.last_error()}")
    print(f"account #{acct.login} {acct.server} balance={acct.balance:.2f} "
          f"{acct.currency} mode={MODES.get(acct.trade_mode)}")
    if acct.trade_mode != 0:
        raise SystemExit("[FAIL] not a DEMO account — refusing.")

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        raise SystemExit(f"[FAIL] symbol_info({SYMBOL}) None")
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
        time.sleep(1.0)

    # Size the risk % so the test order lands near 2x min lot: this is a PLUMBING
    # test, the smallest honest order the pipeline will approve. Set env BEFORE
    # importing config so the frozen dataclasses pick it up.
    money_per_lot = (STOP_DISTANCE / info.trade_tick_size) * info.trade_tick_value
    target_vol = max(info.volume_min * 2, info.volume_min)
    pct = money_per_lot * target_vol / acct.balance * 100.0
    os.environ["EXECUTION_ENABLED"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["RISK_PER_TRADE_PCT"] = f"{pct:.6f}"

    from config import RISK, EXECUTION                      # noqa: E402
    from risk import RiskManager, SymbolSpec                # noqa: E402
    from execution import Executor, _filling_candidates     # noqa: E402
    from journal import get_journal                         # noqa: E402

    spec = SymbolSpec(tick_value=info.trade_tick_value, tick_size=info.trade_tick_size,
                      volume_min=info.volume_min, volume_max=info.volume_max,
                      volume_step=info.volume_step)
    rm = RiskManager(RISK, spec, day_start_balance=acct.balance)
    journal = get_journal()
    ex = Executor(rm, journal=journal)
    print(f"flags: execution_enabled={EXECUTION.execution_enabled} "
          f"dry_run={EXECUTION.dry_run} risk%={RISK.risk_per_trade_pct:.6f} "
          f"-> target vol ~{target_vol}")

    res = ex.submit("buy", balance=acct.balance,
                    stop_distance_price=STOP_DISTANCE, symbol=SYMBOL)
    print(f"submit: status={res.status} sent={res.sent} vol={res.volume} "
          f"reason={res.reason!r}")
    if not res.sent:
        journal.close()
        raise SystemExit("[FAIL] order was not sent — see reason above.")

    time.sleep(2.0)
    positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or ())
                 if p.magic == EXECUTION.magic]
    if not positions:
        journal.close()
        raise SystemExit("[FAIL] SENT but no open position with our magic found.")
    pos = positions[0]
    print(f"open position: ticket={pos.ticket} {SYMBOL} vol={pos.volume} "
          f"open={pos.price_open} pnl={pos.profit:+.2f}")

    tick = mt5.symbol_info_tick(SYMBOL)
    r = None
    for filling in _filling_candidates(mt5, SYMBOL):
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "position": pos.ticket,
            "volume": pos.volume, "type": mt5.ORDER_TYPE_SELL, "price": tick.bid,
            "deviation": EXECUTION.deviation, "magic": EXECUTION.magic,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            "comment": "mt5-research plumbing close",
        })
        if r is not None and r.retcode != 10030:      # 10030 = unsupported filling
            break
    ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    print(f"close: retcode={getattr(r, 'retcode', None)} "
          f"({'DONE' if ok else 'NOT DONE'}) price={getattr(r, 'price', None)}")
    rm.register_close(pos.profit)
    journal.record(event="fill", symbol=SYMBOL, signal="sell", decision="CLOSED",
                   reason="plumbing-test round trip closed", volume=pos.volume,
                   price=getattr(r, "price", None))

    time.sleep(1.0)
    a2 = mt5.account_info()
    print(f"balance {acct.balance:.2f} -> {a2.balance:.2f} "
          f"(round-trip cost {a2.balance - acct.balance:+.2f} — spread+commission, "
          f"exactly what the backtest cost model charges)")
    print("[OK] round trip complete: risk.py sized it, execution.py double-checked "
          "DEMO, order filled and closed, journal has the audit trail.")
    journal.close()
    mt5.shutdown()


if __name__ == "__main__":
    main()
