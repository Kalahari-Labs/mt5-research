"""config.py — every executor knob in one place, env-overridable via .env.

Nothing here talks to the network. All safety-relevant limits are ALSO
enforced server-side in bridge_server.py; these are the engine-side copies.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # .../intel/executor
REPO_DIR = BASE_DIR.parent                          # .../intel
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "executor.sqlite"
MEMORY_PATH = DATA_DIR / "memory.json"
KILL_SWITCH = DATA_DIR / "KILL"                     # touch this file -> engine flattens & halts
LOG_DIR = REPO_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _load_env() -> None:
    for env_path in (BASE_DIR / ".env", REPO_DIR / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()


def _get(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return cast(raw)
    except (TypeError, ValueError):
        return default


# ---- bridge (Wine <-> Linux, or native Windows) -------------------------------
BRIDGE_HOST = _get("MI_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = _get("MI_BRIDGE_PORT", 8787, int)
BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
# set MI_BRIDGE_SPAWN=0 when the bridge runs elsewhere (Docker, another host,
# native Windows service) so the engine never tries to boot Wine itself
BRIDGE_SPAWN = _get("MI_BRIDGE_SPAWN", BRIDGE_HOST in ("127.0.0.1", "localhost"), bool)
WINEPREFIX = _get("MI_WINEPREFIX", str(Path.home() / ".mt5"))
WINE_PYTHON = _get("MI_WINE_PYTHON", r"C:\Program Files\Python312\python.exe")
TERMINAL_EXE = _get("MI_TERMINAL_EXE", r"C:\Program Files\MetaTrader 5\terminal64.exe")

# ---- universe / cadence -----------------------------------------------------
SYMBOLS = [s.strip() for s in _get("MI_SYMBOLS", "EURUSD,GBPUSD,USDJPY,GOLD").split(",") if s.strip()]
TIMEFRAME = _get("MI_TIMEFRAME", "H1")              # decision timeframe for all strategies
CYCLE_SEC = _get("MI_CYCLE_SEC", 30, int)           # engine wake-up cadence
BARS_LIVE = _get("MI_BARS_LIVE", 400, int)          # bars fetched per decision
BARS_BACKTEST = _get("MI_BARS_BACKTEST", 5000, int) # bars fetched for the gate

# ---- execution mode ---------------------------------------------------------
# observe: full pipeline, journals every decision, sends NO orders.
# trade:   sends orders for gate-passed combos (demo-guarded server-side anyway).
EXEC_MODE = _get("MI_EXEC_MODE", "trade")
HITL_MODE = _get("MI_HITL_MODE", False, bool)
HITL_TTL_MIN = _get("MI_HITL_TTL_MIN", 15, int)     # minutes a proposal stays approvable
# Manual trade ticket on the dashboard. OFF by default: the sanctioned human
# role is approving/denying the bot's proposals, not entering trades by hand.
MANUAL_TICKET = _get("MI_MANUAL_TICKET", False, bool)
ALLOW_LIVE = _get("MI_ALLOW_LIVE", False, bool)
MAGIC = _get("MI_MAGIC", 770001, int)               # tags every executor order

# ---- risk (engine-side; bridge enforces its own caps too) -------------------
RISK_PER_TRADE_PCT = _get("MI_RISK_PER_TRADE_PCT", 0.5, float)   # % equity risked per trade
MAX_DAILY_LOSS_PCT = _get("MI_MAX_DAILY_LOSS_PCT", 2.0, float)   # halt for the day beyond this
MAX_DRAWDOWN_PCT = _get("MI_MAX_DRAWDOWN_PCT", 10.0, float)      # halt entirely beyond this
MAX_OPEN_POSITIONS = _get("MI_MAX_OPEN_POSITIONS", 2, int)
MAX_VOLUME = _get("MI_MAX_ORDER_VOLUME", 0.50, float)            # lots, hard cap
MIN_VOLUME = 0.01
MAX_SPREAD_ATR_FRAC = _get("MI_MAX_SPREAD_ATR_FRAC", 0.15, float)  # skip if spread > 15% of ATR
COOLDOWN_AFTER_LOSSES = _get("MI_COOLDOWN_AFTER_LOSSES", 3, int)   # consecutive losses -> cooldown
COOLDOWN_HOURS = _get("MI_COOLDOWN_HOURS", 24, int)
DISABLE_AFTER_LOSSES_7D = _get("MI_DISABLE_AFTER_LOSSES_7D", 5, int)
NEWS_BLACKOUT_MIN = _get("MI_NEWS_BLACKOUT_MIN", 30, int)        # +/- minutes around high-impact
MAX_HOLD_BARS = _get("MI_MAX_HOLD_BARS", 48, int)                # time-stop: swap drag killed the
FRIDAY_FLAT_HOUR_UTC = _get("MI_FRIDAY_FLAT_HOUR_UTC", 19, int)  # research edge; cap the hold
TRAILING_STOP_ATR_MULT = _get("MI_TRAILING_STOP_ATR_MULT", 2.0, float) # 0 to disable
PARTIAL_EXIT_R_MULT = _get("MI_PARTIAL_EXIT_R_MULT", 1.5, float)       # 0 to disable; close 50% at this R

# ---- costs (backtest side; live costs come from the broker's own deals) -------
COMMISSION_PER_LOT = _get("MI_COMMISSION_PER_LOT", 0.0, float)   # account ccy per 1.0 lot PER SIDE

# ---- backtest gate ----------------------------------------------------------
GATE_MIN_TRADES = _get("MI_GATE_MIN_TRADES", 25, int)
GATE_MIN_PF_OOS = _get("MI_GATE_MIN_PF_OOS", 1.05, float)
GATE_MIN_EXPECTANCY_R = _get("MI_GATE_MIN_EXPECTANCY_R", 0.02, float)
GATE_MAX_DD_PCT = _get("MI_GATE_MAX_DD_PCT", 25.0, float)
GATE_OOS_FRAC = _get("MI_GATE_OOS_FRAC", 0.30, float)            # last 30% of bars held out
GATE_REFRESH_HOURS = _get("MI_GATE_REFRESH_HOURS", 24, int)
# stability check: split the full window into N sequential segments and require
# profit factor >= 1.0 in at least MIN_OK of them — kills one-lucky-streak edges
GATE_STABILITY_SEGMENTS = _get("MI_GATE_STABILITY_SEGMENTS", 3, int)
GATE_STABILITY_MIN_OK = _get("MI_GATE_STABILITY_MIN_OK", 2, int)

# ---- notifications (optional; phone buzz on entries/exits/halts) --------------
# ntfy: pick a secret topic name, subscribe in the ntfy app, set MI_NTFY_TOPIC.
# telegram: create a bot with @BotFather, set token + your chat id.
NTFY_TOPIC = _get("MI_NTFY_TOPIC", "")
NTFY_SERVER = _get("MI_NTFY_SERVER", "https://ntfy.sh")
TELEGRAM_BOT_TOKEN = _get("MI_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get("MI_TELEGRAM_CHAT_ID", "")

# ---- dashboard ---------------------------------------------------------------
DASH_HOST = _get("MI_DASH_HOST", "127.0.0.1")
DASH_PORT = _get("MI_DASH_PORT", 8877, int)

# ---- news calendar ------------------------------------------------------------
FF_CALENDAR_URL = _get("MI_FF_CALENDAR_URL",
                       "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
CALENDAR_REFRESH_HOURS = _get("MI_CALENDAR_REFRESH_HOURS", 6, int)

# map symbols -> currencies whose high-impact events blank them out
SYMBOL_CURRENCIES = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"), "GOLD": ("USD",), "XAUUSD": ("USD",),
    "US500Cash": ("USD",), "OILCash": ("USD",),
}

_ISO_CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY",
            "SEK", "NOK", "MXN", "ZAR", "TRY", "PLN", "HUF", "CZK", "SGD", "HKD"}


def currencies_for(symbol: str) -> tuple[str, ...]:
    """Currencies whose high-impact news should blank out `symbol`.

    Explicit map first; otherwise FX pairs like 'EURUSD'/'EURUSDm' are parsed
    from the name; anything else (index/commodity CFDs) falls back to USD so a
    user-added symbol is never silently exempt from the news blackout.
    """
    if symbol in SYMBOL_CURRENCIES:
        return SYMBOL_CURRENCIES[symbol]
    s = "".join(c for c in symbol.upper() if c.isalpha())
    if len(s) >= 6 and s[:3] in _ISO_CCY and s[3:6] in _ISO_CCY:
        return (s[:3], s[3:6])
    return ("USD",)
