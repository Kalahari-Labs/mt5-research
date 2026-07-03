"""notify.py — optional phone notifications on entries, exits and halts.

Two keyless-to-cheap channels, both off by default:
  ntfy      set MI_NTFY_TOPIC to a long random topic name and subscribe to it
            in the ntfy app (https://ntfy.sh) — no account needed
  telegram  set MI_TELEGRAM_BOT_TOKEN (from @BotFather) + MI_TELEGRAM_CHAT_ID

Delivery is fire-and-forget on a daemon thread: a dead network can NEVER stall
or crash the trading loop, and a failed send is dropped silently (the journal
in SQLite remains the source of truth, not your phone).
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

from . import config

TIMEOUT = 10


def enabled() -> bool:
    return bool(config.NTFY_TOPIC or
                (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID))


def _post_ntfy(title: str, body: str, priority: str) -> None:
    req = urllib.request.Request(
        "%s/%s" % (config.NTFY_SERVER.rstrip("/"), config.NTFY_TOPIC),
        data=body.encode(),
        headers={"Title": title, "Priority": priority,
                 "User-Agent": "market-intel-executor/1.0"})
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def _post_telegram(title: str, body: str) -> None:
    url = "https://api.telegram.org/bot%s/sendMessage" % config.TELEGRAM_BOT_TOKEN
    data = urllib.parse.urlencode({
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": "%s\n%s" % (title, body)}).encode()
    urllib.request.urlopen(
        urllib.request.Request(url, data=data), timeout=TIMEOUT).read()


def _worker(title: str, body: str, priority: str) -> None:
    if config.NTFY_TOPIC:
        try:
            _post_ntfy(title, body, priority)
        except Exception:
            pass
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            _post_telegram(title, body)
        except Exception:
            pass


def send(title: str, body: str = "", priority: str = "default") -> None:
    """Non-blocking. priority: min|low|default|high|urgent (ntfy scale)."""
    if not enabled():
        return
    threading.Thread(target=_worker, args=(title, body, priority),
                     daemon=True).start()


def trade_opened(symbol: str, strategy: str, side: str, lots: float,
                 price, sl: float, tp: float, ticket) -> None:
    send("ENTER %s %s" % (side.upper(), symbol),
         "%s  %.2f lots @ %s\nSL %.5f  TP %.5f  (#%s)"
         % (strategy, lots, price, sl, tp, ticket), "high")


def trade_closed(symbol: str, strategy: str, pnl: float, reason: str,
                 ticket) -> None:
    send("%s %s %+.2f" % ("WIN" if pnl > 0 else "LOSS", symbol, pnl),
         "%s closed by %s (#%s)" % (strategy, reason, ticket),
         "high" if pnl <= 0 else "default")


def halt(reason: str) -> None:
    send("EXECUTOR HALT", reason, "urgent")


if __name__ == "__main__":
    if not enabled():
        print("notify: no channel configured (set MI_NTFY_TOPIC or "
              "MI_TELEGRAM_BOT_TOKEN + MI_TELEGRAM_CHAT_ID). Selftest passes "
              "trivially: send() is a no-op that cannot block the engine.")
    else:
        import time
        send("market-intel selftest", "if you can read this, notifications work")
        time.sleep(3)  # give the daemon thread a beat before the process exits
        print("notify: test message dispatched (check your phone)")
    print("NOTIFY SELFTEST OK")
