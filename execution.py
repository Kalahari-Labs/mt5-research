"""execution.py — DEMO-only order execution. OFF by default, dry-run by default.

A signal is sized/approved by risk.py and only sent to MT5 when ALL hold:
  * EXECUTION_ENABLED is true,
  * DRY_RUN is false,
  * the connected account's trade_mode == DEMO (0).

On a non-DEMO (live) account it HARD-REFUSES — no order is even constructed,
regardless of the flags. Anything that is not a real demo send logs the intended
order and sends nothing. There is no code path here that can place a real-money
order. Account reads and order sends are injected, so the safety logic is fully
unit-testable without a terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from config import EXECUTION, STRATEGY

# ENUM_ACCOUNT_TRADE_MODE, verified empirically on XMGlobal-MT5:
# DEMO=0, CONTEST=1, REAL=2.
ACCOUNT_TRADE_MODE_DEMO = 0
# order_send success retcode (TRADE_RETCODE_DONE). Anything else numeric is a
# broker rejection — found the hard way: XM rejected a hardcoded IOC filling mode
# with 10030 and the old code still reported SENT because it never looked.
TRADE_RETCODE_DONE = 10009
_RETCODE_UNSUPPORTED_FILLING = 10030
# SYMBOL_FILLING_* bitmask flags on symbol_info().filling_mode
_FILLING_FOK, _FILLING_IOC = 1, 2


def _result_ok(res):
    """Verify an order_send result. A numeric retcode != DONE is a broker
    rejection; fakes/objects without a numeric retcode are treated as OK so
    injected test senders keep working."""
    if res is None:
        return False, "order_send returned None"
    rc = res.get("retcode") if isinstance(res, dict) else getattr(res, "retcode", None)
    if isinstance(rc, int) and rc != TRADE_RETCODE_DONE:
        cm = res.get("comment") if isinstance(res, dict) else getattr(res, "comment", "")
        return False, f"broker rejected: retcode={rc} {cm or ''}".strip()
    return True, ""


def _filling_candidates(mt5, symbol):
    """Filling modes to try, best-supported first, from the symbol's own flags —
    brokers differ (XM EURUSD advertises FOK only) and 10030 means 'unsupported
    filling mode', so never hardcode one."""
    info = mt5.symbol_info(symbol)
    flags = getattr(info, "filling_mode", 0) if info else 0
    out = []
    if flags & _FILLING_IOC:
        out.append(mt5.ORDER_FILLING_IOC)
    if flags & _FILLING_FOK:
        out.append(mt5.ORDER_FILLING_FOK)
    out.append(mt5.ORDER_FILLING_RETURN)
    return out


@dataclass
class AccountInfo:
    login: int
    trade_mode: int
    balance: float
    server: str = ""


@dataclass
class ExecutionResult:
    status: str          # REFUSED_LIVE | REJECTED_RISK | DISABLED | DRY_RUN | SENT | ERROR
    sent: bool
    reason: str
    volume: float = 0.0
    order: Optional[dict] = None


def _default_account_provider():
    try:
        import MetaTrader5 as mt5
    except Exception:
        return None
    if not mt5.initialize(timeout=60000):
        return None
    a = mt5.account_info()
    if a is None:
        return None
    return AccountInfo(login=a.login, trade_mode=a.trade_mode,
                       balance=a.balance, server=a.server)


def _default_order_sender(order: dict, acct: AccountInfo):
    """Send a market order on MT5 — with a SECOND demo guard at send time. Tries
    the symbol's advertised filling modes in order, retrying only on retcode
    10030 (unsupported filling mode); every other retcode is returned to the
    Executor, which verifies it against TRADE_RETCODE_DONE."""
    import MetaTrader5 as mt5
    a = mt5.account_info()
    if a is None or a.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("ABORT: account is not DEMO at send time — refusing.")
    tick = mt5.symbol_info_tick(order["symbol"])
    is_buy = order["side"] == "buy"
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": order["symbol"],
        "volume": float(order["volume"]),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if is_buy else tick.bid,
        "deviation": order["deviation"],
        "magic": order["magic"],
        "type_time": mt5.ORDER_TIME_GTC,
        "comment": "mt5-research demo",
    }
    res = None
    for filling in _filling_candidates(mt5, order["symbol"]):
        res = mt5.order_send({**req, "type_filling": filling})
        if res is None or res.retcode != _RETCODE_UNSUPPORTED_FILLING:
            return res
    return res


class Executor:
    def __init__(self, risk_manager, journal=None,
                 account_provider: Optional[Callable] = None,
                 order_sender: Optional[Callable] = None,
                 config=EXECUTION):
        self.risk = risk_manager
        self.journal = journal
        self.config = config
        self._account_provider = account_provider or _default_account_provider
        self._order_sender = order_sender or _default_order_sender

    def _log(self, **kw):
        if self.journal:
            try:
                self.journal.record(**kw)
            except Exception:
                pass

    def submit(self, side: str, balance: float, stop_distance_price: float,
               price=None, symbol=None) -> ExecutionResult:
        symbol = symbol or STRATEGY.symbol
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        # 1) Risk gate — the ONLY approver.
        decision = self.risk.evaluate(balance, stop_distance_price)
        if not decision.approved:
            self._log(event="order", symbol=symbol, signal=side,
                      decision="REJECTED_RISK", reason=decision.reason, price=price)
            return ExecutionResult("REJECTED_RISK", False, decision.reason)

        # 2) HARD demo guard — read the account and refuse anything non-DEMO,
        #    BEFORE building an order, regardless of the dry-run/enabled flags.
        acct = self._account_provider()
        if acct is None:
            reason = "REFUSE: could not read account_info to verify DEMO mode"
            self._log(event="order", symbol=symbol, signal=side,
                      decision="REFUSED_LIVE", reason=reason, price=price)
            return ExecutionResult("REFUSED_LIVE", False, reason)
        if acct.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
            reason = (f"REFUSE: account #{acct.login} trade_mode={acct.trade_mode} "
                      f"is NOT DEMO — no order constructed")
            self._log(event="order", symbol=symbol, signal=side,
                      decision="REFUSED_LIVE", reason=reason, price=price)
            return ExecutionResult("REFUSED_LIVE", False, reason)

        order = {"symbol": symbol, "side": side, "volume": decision.volume,
                 "price": price, "magic": self.config.magic,
                 "deviation": self.config.deviation,
                 "stop_distance": stop_distance_price}

        # 3) Disabled or dry-run -> log intent, send nothing (the DEFAULT path).
        if not self.config.execution_enabled:
            self._log(event="order", symbol=symbol, signal=side, decision="DISABLED",
                      reason="EXECUTION_ENABLED=false; intent logged only",
                      volume=decision.volume, price=price)
            return ExecutionResult("DISABLED", False,
                                   "execution disabled", decision.volume, order)
        if self.config.dry_run:
            self._log(event="order", symbol=symbol, signal=side, decision="DRY_RUN",
                      reason="DRY_RUN=true; intent logged only",
                      volume=decision.volume, price=price)
            return ExecutionResult("DRY_RUN", False, "dry run", decision.volume, order)

        # 4) Real demo send (guarded above, and again inside the sender).
        try:
            res = self._order_sender(order, acct)
        except Exception as e:
            self._log(event="order", symbol=symbol, signal=side, decision="ERROR",
                      reason=f"send failed: {e}", volume=decision.volume, price=price)
            return ExecutionResult("ERROR", False, str(e), decision.volume, order)

        # 5) VERIFY the broker actually accepted it — order_send does not raise on
        # rejection, it returns a retcode. SENT without this check is a lie.
        ok, why = _result_ok(res)
        if not ok:
            self._log(event="order", symbol=symbol, signal=side,
                      decision="REJECTED_BROKER", reason=why,
                      volume=decision.volume, price=price)
            return ExecutionResult("REJECTED_BROKER", False, why, decision.volume, order)

        self.risk.register_open()
        self._log(event="fill", symbol=symbol, signal=side, decision="SENT",
                  reason="demo order sent", volume=decision.volume, price=price)
        return ExecutionResult("SENT", True, "demo order sent", decision.volume, order)
