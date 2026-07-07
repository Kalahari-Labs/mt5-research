"""bridge.py — client + launcher for the MT5 HTTP bridge.

Client: thin urllib wrapper over bridge_server.py's endpoints.
Launcher: starts the bridge if /health is dead, waits for it to come up, and
can restart it. The MT5 terminal itself is started too if missing.

Platforms:
  Linux/macOS  terminal + bridge run under Wine (see docs/INSTALL-WINE-MT5.md)
  Windows      terminal + bridge run natively; the same Python that runs the
               engine runs bridge_server.py (pip install MetaTrader5)
  Docker/remote set MI_BRIDGE_SPAWN=0 + MI_BRIDGE_HOST; the engine never tries
               to boot a terminal it can't reach
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

IS_WINDOWS = os.name == "nt"


class BridgeError(RuntimeError):
    pass


class BridgeRefused(BridgeError):
    """Server-side gate refused a write (403). NOT retryable."""


class Bridge:
    def __init__(self, base_url: str = config.BRIDGE_URL, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport -------------------------------------------------------------
    def _get(self, path: str) -> object:
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise BridgeError("GET %s -> %s %s" % (path, e.code, e.read()[:300])) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise BridgeError("GET %s -> %r" % (path, e)) from e

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            payload = e.read()[:500]
            if e.code == 403:
                raise BridgeRefused("write refused: %s" % payload.decode(errors="replace")) from e
            try:
                return json.loads(payload)
            except Exception:
                raise BridgeError("POST %s -> %s %s" % (path, e.code, payload)) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise BridgeError("POST %s -> %r" % (path, e)) from e

    # -- reads -----------------------------------------------------------------
    def health(self) -> dict:
        return self._get("/health")

    def account(self) -> dict:
        return self._get("/account")

    def symbol(self, name: str) -> dict:
        return self._get("/symbol?name=%s" % name)

    def tick(self, symbol: str) -> dict:
        return self._get("/tick?symbol=%s" % symbol)

    def bars(self, symbol: str, tf: str = "H1", count: int = 300, start: int = 0) -> list:
        """[[epoch, o, h, l, c, tick_volume, spread_points], ...] oldest-first."""
        return self._get("/bars?symbol=%s&tf=%s&count=%d&start=%d" % (symbol, tf, count, start))

    def positions(self, symbol: str | None = None) -> list[dict]:
        return self._get("/positions" + ("?symbol=%s" % symbol if symbol else ""))

    def history_deals(self, days: int = 30) -> list[dict]:
        return self._get("/history_deals?days=%d" % days)

    # -- writes (demo-guarded server-side) --------------------------------------
    def order(self, symbol: str, side: str, volume: float, sl: float, tp: float,
              comment: str = "mi-executor", magic: int = config.MAGIC) -> dict:
        return self._post("/order", {"symbol": symbol, "side": side, "volume": volume,
                                     "sl": sl, "tp": tp, "comment": comment, "magic": magic})

    def close(self, ticket: int, comment: str = "mi-executor close",
              volume: float | None = None) -> dict:
        body: dict = {"ticket": ticket, "comment": comment}
        if volume is not None:
            body["volume"] = volume
        return self._post("/close", body)

    def modify(self, ticket: int, sl: float | None = None, tp: float | None = None) -> dict:
        body: dict = {"ticket": ticket}
        if sl is not None:
            body["sl"] = sl
        if tp is not None:
            body["tp"] = tp
        return self._post("/modify", body)

    def alive(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except BridgeError:
            return False

    def reachable(self) -> bool:
        """HTTP answers at all (even ok:false). Distinguishes a half-dead
        bridge — process up, terminal attach lost, fixable via /reinit — from
        no bridge at all, which needs a spawn."""
        try:
            self.health()
            return True
        except BridgeError:
            return False

    def reinit(self) -> dict:
        """Ask a half-dead bridge to re-attach to the terminal. Only a server
        that implements /reinit answers with a `login` key (an old zombie 404s
        into `{"error": ...}`, transport failure yields `{}`) — callers key on
        that to decide between 'terminal problem' and 'replace the process'."""
        try:
            return self._post("/reinit", {})
        except BridgeError:
            return {}


def _wine_path(linux_path: Path) -> str:
    return "Z:" + str(linux_path).replace("/", "\\")


def _wine_env() -> dict:
    env = dict(os.environ)
    env["WINEPREFIX"] = config.WINEPREFIX
    env["WINEDEBUG"] = "-all"
    env["MI_BRIDGE_PORT"] = str(config.BRIDGE_PORT)
    env["MI_MAX_ORDER_VOLUME"] = str(config.MAX_VOLUME)
    if config.WINE_DISPLAY:
        env["DISPLAY"] = config.WINE_DISPLAY
    return env


def terminal_running() -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                             capture_output=True, text=True)
        return "terminal64.exe" in out.stdout
    out = subprocess.run(["pgrep", "-f", "terminal64.exe"], capture_output=True, text=True)
    return out.returncode == 0


def start_terminal() -> None:
    if terminal_running():
        return
    if IS_WINDOWS:
        subprocess.Popen([config.TERMINAL_EXE],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(
            ["wine", config.TERMINAL_EXE], env=_wine_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    time.sleep(25)  # terminal boot + broker login


def start_bridge(log_path: Path | None = None) -> subprocess.Popen:
    log_f = open(log_path or (config.LOG_DIR / "bridge.log"), "ab", buffering=0)
    if IS_WINDOWS:
        # native: the engine's own Python runs the bridge (needs MetaTrader5)
        return subprocess.Popen(
            [sys.executable, str(config.BASE_DIR / "bridge_server.py")],
            env=dict(os.environ, MI_BRIDGE_PORT=str(config.BRIDGE_PORT),
                     MI_MAX_ORDER_VOLUME=str(config.MAX_VOLUME)),
            stdout=log_f, stderr=log_f)
    server_py = _wine_path(config.BASE_DIR / "bridge_server.py")
    return subprocess.Popen(
        ["wine", config.WINE_PYTHON, server_py], env=_wine_env(),
        stdout=log_f, stderr=log_f, start_new_session=True)


def kill_stale_bridge() -> None:
    """Kill a bridge process that answers HTTP but can never recover (it
    predates /reinit) or an orphan squatting the port with a dead terminal
    attach — a fresh spawn cannot bind until it is gone. Matches the exact
    server script in the cmdline, so the terminal, wineserver, and unrelated
    tools are never touched."""
    if IS_WINDOWS:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object {$_.CommandLine -like '*bridge_server.py*'} | "
             "ForEach-Object {Stop-Process -Id $_.ProcessId -Force}"],
            capture_output=True)
    else:
        subprocess.run(["pkill", "-f", r"bridge_server\.py"], capture_output=True)
    time.sleep(2)


def ensure_bridge(timeout_sec: int = 120) -> Bridge:
    """Idempotent: returns a healthy Bridge, booting terminal/bridge if needed.

    Escalation ladder when the bridge answers HTTP but reports ok:false (its
    terminal attach died — happens whenever the terminal re-logs or restarts):
      1. POST /reinit — cheap in-process re-attach.
      2. If the server doesn't know /reinit (pre-self-heal zombie), kill it by
         cmdline match and spawn a fresh one.
      3. If /reinit worked but there is still no account, the terminal itself
         is logged out — replacing the bridge cannot fix that, so say so."""
    b = Bridge()
    if b.alive():
        return b
    if not config.BRIDGE_SPAWN:
        # remote bridge (Docker / another host): wait for it, never spawn
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if b.alive():
                return b
            time.sleep(2)
        raise BridgeError("remote bridge %s not answering within %ss "
                          "(MI_BRIDGE_SPAWN=0 — start it on the bridge host)"
                          % (config.BRIDGE_URL, timeout_sec))
    start_terminal()
    if b.reachable():
        r = b.reinit()
        if b.alive():
            return b
        if "login" in r:  # server understood /reinit — the terminal is the problem
            raise BridgeError("bridge is up but the terminal has no account "
                              "(logged out?): %s" % r)
        kill_stale_bridge()
    start_bridge()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if b.alive():
            return b
        time.sleep(2)
    raise BridgeError("bridge did not come up within %ss (see logs/bridge.log)" % timeout_sec)


if __name__ == "__main__":
    br = ensure_bridge()
    print(json.dumps(br.health(), indent=2))
