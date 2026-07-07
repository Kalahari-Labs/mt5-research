"""onboard.py — guided setup checker. Run this FIRST on any new machine.

    python3 -m executor.onboard          # check everything, print what's missing
    python3 -m executor.onboard --gate   # also run the backtest gate at the end

Every check is a real probe (files, processes, HTTP, broker data) — nothing is
assumed. Exit code 0 = ready to run ./start.sh observe.
"""
from __future__ import annotations

import os
import shutil
import sys
import urllib.request
from pathlib import Path

from . import config

OK, BAD, WARN = "\033[32m  OK \033[0m", "\033[31mFAIL \033[0m", "\033[33mWARN \033[0m"
failures = 0


def check(label: str, ok: bool, fix: str = "", warn_only: bool = False) -> bool:
    global failures
    mark = OK if ok else (WARN if warn_only else BAD)
    print("%s %s" % (mark, label))
    if not ok:
        if fix:
            print("       -> %s" % fix)
        if not warn_only:
            failures += 1
    return ok


def wine_path(win_path: str) -> Path:
    """C:\\... inside the prefix -> Linux path."""
    return Path(config.WINEPREFIX) / "drive_c" / win_path.replace("C:\\", "").replace("\\", "/")


def main() -> int:
    from .bridge import IS_WINDOWS, terminal_running

    print("== market-intel executor onboarding ==\n")
    print("-- host (%s) --" % ("Windows native" if IS_WINDOWS
                               else "Linux/macOS via Wine"
                               if config.BRIDGE_SPAWN else "remote bridge"))
    check("Python %s.%s (need 3.10+)" % sys.version_info[:2], sys.version_info >= (3, 10),
          "install python3.10+")
    try:
        import numpy  # noqa: F401
        check("numpy importable", True)
    except ImportError:
        check("numpy importable", False,
              "pip install numpy (or apt install python3-numpy)")

    if IS_WINDOWS:
        try:
            import MetaTrader5  # noqa: F401
            check("MetaTrader5 package importable", True)
        except ImportError:
            check("MetaTrader5 package importable", False,
                  "pip install MetaTrader5 (native Windows bridge needs it)")
        check("MT5 terminal at %s" % config.TERMINAL_EXE,
              Path(config.TERMINAL_EXE).exists(),
              "install MetaTrader 5 from your broker, or set MI_TERMINAL_EXE")
        check("MT5 terminal process running", terminal_running(),
              "start it and LOG INTO A DEMO ACCOUNT + enable AutoTrading (Ctrl+E)",
              warn_only=True)
    elif config.BRIDGE_SPAWN:
        check("wine on PATH", shutil.which("wine") is not None,
              "install wine (winehq stable); the MT5 terminal and bridge run under it")
        print("\n-- Wine prefix: %s --" % config.WINEPREFIX)
        check("prefix exists", Path(config.WINEPREFIX).is_dir(),
              "create it: WINEPREFIX=%s winecfg" % config.WINEPREFIX)
        py = wine_path(config.WINE_PYTHON)
        check("Windows Python at %s" % config.WINE_PYTHON, py.exists(),
              "install Windows Python 3.12 in the prefix, then: wine pip install MetaTrader5")
        term = wine_path(config.TERMINAL_EXE)
        check("MT5 terminal at %s" % config.TERMINAL_EXE, term.exists(),
              "install MetaTrader 5 from your broker inside the prefix")
        check("MT5 terminal process running", terminal_running(),
              "start it and LOG INTO A DEMO ACCOUNT + enable AutoTrading (Ctrl+E)",
              warn_only=True)
    else:
        print("       MI_BRIDGE_SPAWN=0 — expecting a bridge already running at %s"
              % config.BRIDGE_URL)
    running = terminal_running() if config.BRIDGE_SPAWN else True

    print("\n-- bridge --")
    from .bridge import Bridge, ensure_bridge, BridgeError
    b = Bridge(timeout=8)
    alive = b.alive()
    if not alive and running:
        print("       bridge not up; trying to boot it (takes ~20s) ...")
        try:
            b = ensure_bridge(timeout_sec=60)
            alive = True
        except BridgeError as e:
            print("       boot failed: %s" % e)
    if check("bridge answering on %s" % config.BRIDGE_URL, alive,
             "check logs/bridge.log; is the terminal logged in?"):
        h = b.health()
        check("account is DEMO (login %s @ %s)" % (h["account"]["login"], h["account"]["server"]),
              bool(h["account"]["demo"]),
              "log the terminal into a DEMO account — writes are refused otherwise")
        check("writes allowed by server-side gate (%s)" % h["gate"], h["writes_allowed"],
              "see EXECUTOR.md safety model", warn_only=True)
        print("\n-- symbols (%s) --" % ",".join(config.SYMBOLS))
        for sym in config.SYMBOLS:
            try:
                info = b.symbol(sym)
                ok = "error" not in info
                check("%-10s digits=%s spread=%s pts" %
                      (sym, info.get("digits"), info.get("spread")), ok,
                      "adjust MI_SYMBOLS in .env to your broker's symbol names")
            except BridgeError as e:
                check(sym, False, "bridge error: %s" % e)

    print("\n-- news calendar --")
    try:
        req = urllib.request.Request(config.FF_CALENDAR_URL,
                                     headers={"User-Agent": "market-intel/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    check("ForexFactory weekly feed reachable", ok,
          "no calendar = no news blackout; engine still runs", warn_only=True)

    print("\n-- config --")
    env = config.REPO_DIR / ".env"
    check(".env present", env.exists(), "cp .env.example .env and review the knobs",
          warn_only=True)
    print("       mode=%s risk/trade=%s%% daily-stop=%s%% max-DD=%s%% max-vol=%s lots"
          % (config.EXEC_MODE, config.RISK_PER_TRADE_PCT, config.MAX_DAILY_LOSS_PCT,
             config.MAX_DRAWDOWN_PCT, config.MAX_VOLUME))
    check("Supabase (optional — bring your own project for the intel/ plane)",
          bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")),
          "not set — fine, falls back to local sqlite. To use your own: fill "
          "SUPABASE_URL + SUPABASE_SERVICE_KEY in intel/.env (run migrations/0001_init.sql there first)",
          warn_only=True)

    if "--gate" in sys.argv and failures == 0:
        print("\n-- backtest gate (5000 bars per combo from YOUR broker) --")
        from .gate import run_gate
        from .store import Store
        run_gate(Store(), b)

    print()
    if failures:
        print("NOT READY — fix the FAIL lines above, then re-run.")
        return 1
    print("READY. Next steps:")
    print("  python3 -m executor.gate     # see what earns the right to trade here")
    print("  ./start.sh observe           # watch the decision feed, zero orders")
    print("  ./start.sh                   # autonomous (demo-gated server-side)")
    print("  dashboard: http://%s:%s | kill: touch %s"
          % (config.DASH_HOST, config.DASH_PORT, config.KILL_SWITCH))
    print("\nRead executor/EXECUTOR.md — especially the risk disclosure. Demo first,")
    print("always. Live stays locked unless you deliberately open the triple gate.")
    return 0


def cli() -> None:
    """console_scripts entry point (mi-onboard)."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
