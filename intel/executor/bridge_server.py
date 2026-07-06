"""bridge_server.py — Wine-side MT5 HTTP bridge (runs under Windows Python).

The ONLY module in this repo that imports order_send. It exposes MT5 over
HTTP on 127.0.0.1 so the Linux-side engine gets millisecond round-trips
instead of a multi-second Wine boot per call.

SAFETY MODEL (server-side, cannot be bypassed by any client):
  1. On startup: refuse to serve unless account_info().trade_mode == DEMO,
     unless the live-unlock triple gate below is fully open.
  2. Before EVERY write (/order /close /modify): re-fetch account_info and
     re-verify the gate. A mid-session account switch to REAL kills writes.
  3. Every /order MUST carry both sl and tp. Naked orders are refused.
  4. Volume is clamped server-side to MAX_ORDER_VOLUME lots.
  5. Live unlock (all three or no writes on a REAL account):
       env  MI_ALLOW_LIVE=1
       file ALLOW_LIVE (next to this script) containing the exact login
       the account login in ALLOW_LIVE matching account_info().login

Run from Linux (executor/bridge.py does this for you):
  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all \
    wine 'C:\\Program Files\\Python312\\python.exe' \
    'Z:\\home\\flowdaaddy\\mt5-research\\intel\\executor\\bridge_server.py'
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import MetaTrader5 as mt5

# Default loopback-only: nothing off this machine can reach the order endpoint.
# Docker engine setups need MI_BRIDGE_BIND=172.17.0.1 (docker0) or 0.0.0.0 —
# widen it ONLY if you understand who else can then reach this port.
HOST = os.environ.get("MI_BRIDGE_BIND", "127.0.0.1")
PORT = int(os.environ.get("MI_BRIDGE_PORT", "8787"))
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
MAX_ORDER_VOLUME = float(os.environ.get("MI_MAX_ORDER_VOLUME", "0.50"))
ALLOW_LIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ALLOW_LIVE")

TIMEFRAMES = {}  # filled after initialize()


def log(msg: str) -> None:
    print("[bridge %s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


_LAST_REATTACH = 0.0
REATTACH_MIN_SEC = 30.0


def init_mt5() -> bool:
    """Attach to the terminal. Safe to call repeatedly; True on success."""
    if not (mt5.initialize(timeout=60000) or mt5.initialize(path=TERMINAL, timeout=60000)):
        log("initialize() failed: %s" % (mt5.last_error(),))
        return False
    TIMEFRAMES.update({
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    })
    return True


def ensure_mt5(force: bool = False) -> None:
    """Self-heal a dead terminal attach. The MetaTrader5 IPC pipe silently dies
    whenever the terminal restarts or re-logs in (e.g. switching accounts in
    the UI); without this the bridge serves account=None forever — writes stay
    refused and the dashboard goes stale until a human restarts the process.
    Rate-limited so a genuinely logged-out terminal doesn't spin."""
    global _LAST_REATTACH
    if mt5.account_info() is not None:
        return
    now = time.monotonic()
    if not force and now - _LAST_REATTACH < REATTACH_MIN_SEC:
        return
    _LAST_REATTACH = now
    log("account_info() is None — re-attaching to terminal")
    mt5.shutdown()
    if init_mt5():
        a = mt5.account_info()
        log("re-attach: %s" % ("login %s @ %s" % (a.login, a.server)
                               if a else "attached, terminal not logged in"))


def account() -> dict | None:
    a = mt5.account_info()
    return a._asdict() if a else None


def write_gate() -> tuple[bool, str]:
    """(allowed, reason). Re-checked before EVERY write. DEMO passes; REAL
    passes only through the triple gate."""
    a = mt5.account_info()
    if a is None:
        return False, "account_info() is None - terminal not logged in"
    if a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO:
        return True, "demo"
    if os.environ.get("MI_ALLOW_LIVE") != "1":
        return False, "REAL account and MI_ALLOW_LIVE!=1 - writes refused"
    if not os.path.exists(ALLOW_LIVE_FILE):
        return False, "REAL account and no ALLOW_LIVE file - writes refused"
    try:
        unlocked_login = open(ALLOW_LIVE_FILE).read().strip()
    except OSError as e:
        return False, "ALLOW_LIVE unreadable: %r" % e
    if unlocked_login != str(a.login):
        return False, "ALLOW_LIVE login %s != account %s - writes refused" % (unlocked_login, a.login)
    return True, "LIVE UNLOCKED for %s" % a.login


def filling_modes(sym: str):
    info = mt5.symbol_info(sym)
    fm = getattr(info, "filling_mode", 0)
    ordered = []
    if fm & 1:
        ordered.append(mt5.ORDER_FILLING_FOK)
    if fm & 2:
        ordered.append(mt5.ORDER_FILLING_IOC)
    for m in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        if m not in ordered:
            ordered.append(m)
    return ordered


def send_with_filling_fallback(req_base: dict) -> dict:
    """order_send with the 10030 (unsupported filling) fallback proven on XM."""
    last = None
    for fmode in filling_modes(req_base["symbol"]):
        req = dict(req_base, type_filling=fmode)
        res = mt5.order_send(req)
        last = res
        if res is None:
            continue
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            return {"ok": True, "retcode": res.retcode, "order": res.order,
                    "deal": res.deal, "price": res.price, "volume": res.volume,
                    "comment": res.comment}
        if res.retcode != 10030:
            break
    return {"ok": False,
            "retcode": getattr(last, "retcode", None),
            "comment": getattr(last, "comment", str(mt5.last_error()))}


def do_order(body: dict) -> tuple[int, dict]:
    ok, reason = write_gate()
    if not ok:
        return 403, {"ok": False, "error": reason}
    for field in ("symbol", "side", "volume", "sl", "tp"):
        if field not in body:
            return 400, {"ok": False, "error": "missing required field: %s (sl+tp are MANDATORY)" % field}
    sym = str(body["symbol"])
    side = str(body["side"]).lower()
    if side not in ("buy", "sell"):
        return 400, {"ok": False, "error": "side must be buy|sell"}
    vol = float(body["volume"])
    if vol <= 0:
        return 400, {"ok": False, "error": "volume must be > 0"}
    if vol > MAX_ORDER_VOLUME:
        return 400, {"ok": False, "error": "volume %s exceeds server cap %s" % (vol, MAX_ORDER_VOLUME)}
    sl, tp = float(body["sl"]), float(body["tp"])
    if sl <= 0 or tp <= 0:
        return 400, {"ok": False, "error": "sl and tp must be actual price levels"}
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return 502, {"ok": False, "error": "no tick for %s" % sym}
    price = tick.ask if side == "buy" else tick.bid
    # sanity: stop must be on the protective side
    if side == "buy" and not (sl < price < tp):
        return 400, {"ok": False, "error": "buy requires sl < price(%s) < tp" % price}
    if side == "sell" and not (tp < price < sl):
        return 400, {"ok": False, "error": "sell requires tp < price(%s) < sl" % price}
    res = send_with_filling_fallback({
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": vol,
        "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": int(body.get("deviation", 20)),
        "magic": int(body.get("magic", 770001)),
        "comment": str(body.get("comment", "mi-executor"))[:31],
        "type_time": mt5.ORDER_TIME_GTC,
    })
    if res["ok"]:
        # resolve the actual position ticket for the caller
        time.sleep(0.2)
        for p in (mt5.positions_get(symbol=sym) or []):
            if p.magic == int(body.get("magic", 770001)) and p.ticket == res["order"]:
                res["position"] = p.ticket
        res.setdefault("position", res["order"])
        log("ORDER DONE %s %s %s sl=%s tp=%s -> pos %s @ %s (gate: %s)"
            % (side, vol, sym, sl, tp, res["position"], res["price"], reason))
    else:
        log("ORDER FAIL %s %s %s retcode=%s %s" % (side, vol, sym, res["retcode"], res["comment"]))
    return (200 if res["ok"] else 502), res


def do_close(body: dict) -> tuple[int, dict]:
    ok, reason = write_gate()
    if not ok:
        return 403, {"ok": False, "error": reason}
    ticket = int(body.get("ticket", 0))
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return 200, {"ok": True, "already_flat": True}
    p = pos[0]
    tick = mt5.symbol_info_tick(p.symbol)
    res = send_with_filling_fallback({
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": float(body.get("volume", p.volume)),
        "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
        "position": ticket,
        "price": tick.bid if p.type == 0 else tick.ask,
        "deviation": int(body.get("deviation", 20)),
        "magic": p.magic,
        "comment": str(body.get("comment", "mi-executor close"))[:31],
        "type_time": mt5.ORDER_TIME_GTC,
    })
    log("CLOSE %s -> %s" % (ticket, res))
    return (200 if res["ok"] else 502), res


def do_modify(body: dict) -> tuple[int, dict]:
    ok, reason = write_gate()
    if not ok:
        return 403, {"ok": False, "error": reason}
    ticket = int(body.get("ticket", 0))
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return 404, {"ok": False, "error": "position %s not found" % ticket}
    p = pos[0]
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": p.symbol,
        "position": ticket,
        "sl": float(body.get("sl", p.sl)),
        "tp": float(body.get("tp", p.tp)),
    }
    res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return 200, {"ok": True, "retcode": res.retcode}
    return 502, {"ok": False, "retcode": getattr(res, "retcode", None),
                 "comment": getattr(res, "comment", str(mt5.last_error()))}


def get_bars(q: dict) -> tuple[int, object]:
    sym = q.get("symbol", [""])[0]
    tf = q.get("tf", ["H1"])[0]
    count = min(int(q.get("count", ["300"])[0]), 20000)
    start = int(q.get("start", ["0"])[0])
    if tf not in TIMEFRAMES:
        return 400, {"ok": False, "error": "tf must be one of %s" % sorted(TIMEFRAMES)}
    mt5.symbol_select(sym, True)
    rates = mt5.copy_rates_from_pos(sym, TIMEFRAMES[tf], start, count)
    if rates is None:
        return 502, {"ok": False, "error": str(mt5.last_error())}
    return 200, [[int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                  float(r["close"]), float(r["tick_volume"]), float(r["spread"])]
                 for r in rates]


def get_history(q: dict) -> tuple[int, object]:
    days = min(int(q.get("days", ["30"])[0]), 3650)
    frm = datetime.now(timezone.utc) - timedelta(days=days)
    deals = mt5.history_deals_get(frm, datetime.now(timezone.utc) + timedelta(days=1))
    if deals is None:
        return 200, []
    return 200, [d._asdict() for d in deals]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet; we log writes explicitly
        pass

    def _send(self, code: int, payload) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            ensure_mt5()
            if u.path == "/health":
                a = account()
                gate_ok, gate_reason = write_gate()
                t = mt5.terminal_info()
                self._send(200, {
                    "ok": a is not None, "ts": datetime.now(timezone.utc).isoformat(),
                    "account": {"login": a and a["login"], "server": a and a["server"],
                                "trade_mode": a and a["trade_mode"],
                                "demo": bool(a and a["trade_mode"] == 0)},
                    "writes_allowed": gate_ok, "gate": gate_reason,
                    "terminal_connected": bool(t and t.connected),
                    "max_order_volume": MAX_ORDER_VOLUME,
                })
            elif u.path == "/account":
                self._send(200, account() or {})
            elif u.path == "/symbol":
                sym = q.get("name", [""])[0]
                mt5.symbol_select(sym, True)
                info = mt5.symbol_info(sym)
                self._send(200, info._asdict() if info else {"error": "unknown symbol %s" % sym})
            elif u.path == "/tick":
                sym = q.get("symbol", [""])[0]
                tick = mt5.symbol_info_tick(sym)
                self._send(200, {"time": tick.time, "bid": tick.bid, "ask": tick.ask,
                                 "last": tick.last} if tick else {"error": "no tick"})
            elif u.path == "/bars":
                code, payload = get_bars(q)
                self._send(code, payload)
            elif u.path == "/positions":
                sym = q.get("symbol", [None])[0]
                pos = mt5.positions_get(symbol=sym) if sym else mt5.positions_get()
                self._send(200, [p._asdict() for p in (pos or [])])
            elif u.path == "/history_deals":
                code, payload = get_history(q)
                self._send(code, payload)
            else:
                self._send(404, {"error": "unknown path %s" % u.path})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            ensure_mt5()
            if self.path == "/order":
                code, payload = do_order(body)
            elif self.path == "/close":
                code, payload = do_close(body)
            elif self.path == "/modify":
                code, payload = do_modify(body)
            elif self.path == "/reinit":
                # Maintenance (loopback-only server): force a fresh terminal
                # attach. Read-side action — the write gate is untouched.
                ensure_mt5(force=True)
                a = account()
                code, payload = 200, {"ok": a is not None,
                                      "login": a and a["login"],
                                      "server": a and a["server"]}
            else:
                code, payload = 404, {"error": "unknown path %s" % self.path}
            self._send(code, payload)
        except Exception as e:
            self._send(500, {"error": repr(e)})


def main() -> None:
    # Serve even when the terminal is still booting or mid-login: ensure_mt5()
    # re-attaches on demand, /health reports the truth, and write_gate fails
    # closed meanwhile. A crash-exit here just made the supervisor loop.
    if not init_mt5():
        log("terminal attach failed at boot — serving anyway; re-attach on demand")
    a = mt5.account_info()
    if a is None:
        log("terminal not logged in (yet): read-only until it is; writes fail closed")
    else:
        gate_ok, gate_reason = write_gate()
        mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(a.trade_mode, str(a.trade_mode))
        log("account %s @ %s mode=%s balance=%.2f %s | writes: %s (%s)"
            % (a.login, a.server, mode, a.balance, a.currency,
               "ALLOWED" if gate_ok else "REFUSED", gate_reason))
        if a.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not gate_ok:
            log("REAL account without live unlock: serving READ-ONLY endpoints.")
    srv = HTTPServer((HOST, PORT), Handler)  # single-threaded: serializes MT5 calls
    log("serving on http://%s:%s" % (HOST, PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
