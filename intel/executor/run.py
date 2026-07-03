"""run.py — ONE command boots the whole executor stack and keeps it alive.

    python3 -m executor.run            # trade mode (demo-gated server-side)
    MI_EXEC_MODE=observe python3 -m executor.run   # full pipeline, no orders

Boot order:
  1. MT5 terminal under Wine (started if missing)
  2. Wine HTTP bridge (started if /health is dead)
  3. engine  (subprocess, restarted with backoff if it dies)
  4. dashboard on http://127.0.0.1:8877 (subprocess, restarted if it dies)

Ctrl-C stops children cleanly. The KILL file (executor/data/KILL) makes the
engine flatten all positions and idle without stopping the processes.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import config
from .bridge import ensure_bridge

CHILDREN: dict[str, subprocess.Popen] = {}
BACKOFF: dict[str, float] = {}
STARTED: dict[str, float] = {}


def log(msg: str) -> None:
    print("[run %s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg),
          flush=True)


def spawn(name: str, module: str) -> None:
    logf = open(config.LOG_DIR / ("%s.log" % name), "ab", buffering=0)
    CHILDREN[name] = subprocess.Popen(
        [sys.executable, "-m", module], cwd=str(config.REPO_DIR),
        stdout=logf, stderr=logf)
    STARTED[name] = time.time()
    log("%s started (pid %s) -> logs/%s.log" % (name, CHILDREN[name].pid, name))


def shutdown(*_a) -> None:
    log("shutting down children (bridge/terminal stay up for other tools)")
    for name, p in CHILDREN.items():
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 10
    for name, p in CHILDREN.items():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    log("ensuring MT5 terminal + Wine bridge ...")
    b = ensure_bridge()
    h = b.health()
    log("bridge ok — account %s (%s), writes %s"
        % (h["account"]["login"],
           "DEMO" if h["account"]["demo"] else "NOT-DEMO",
           "allowed" if h["writes_allowed"] else "REFUSED"))
    if not h["account"]["demo"]:
        log("*** WARNING: this is NOT a demo account. Orders are refused unless "
            "the live triple-gate is explicitly opened. See EXECUTOR.md. ***")
    spawn("engine", "executor.engine")
    spawn("dashboard", "executor.dashboard")
    log("dashboard: http://%s:%s | kill switch: touch %s"
        % (config.DASH_HOST, config.DASH_PORT, config.KILL_SWITCH))
    while True:
        time.sleep(5)
        for name, module in (("engine", "executor.engine"),
                             ("dashboard", "executor.dashboard")):
            p = CHILDREN.get(name)
            if p and p.poll() is None:
                if time.time() - STARTED.get(name, 0) > 120:
                    BACKOFF[name] = 5  # stable for 2 min: forgive past crashes
            elif p:
                wait = min(BACKOFF.get(name, 5) * 2, 300)
                BACKOFF[name] = wait
                log("%s died (rc=%s) — restarting in %ss" % (name, p.returncode, wait))
                time.sleep(wait)
                spawn(name, module)
        if not b.alive():
            log("bridge dead — rebooting it")
            try:
                b = ensure_bridge()
                log("bridge back")
            except Exception as e:
                log("bridge reboot failed (%r); retrying next tick" % e)


if __name__ == "__main__":
    main()
