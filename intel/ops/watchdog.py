"""ops/watchdog.py — external stale-heartbeat check (runs from a systemd timer).

The runner alerts on its own step failures, but it can't alert if it is dead.
This script checks the age of the newest 'runner' heartbeat; if it's older
than INTEL_STALE_MIN (default 20 min) it logs an ALERT to logs/watchdog.log
and fires INTEL_ALERT_WEBHOOK if configured.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from store import get_store, utcnow  # noqa: E402

STALE_MIN = int(os.environ.get("INTEL_STALE_MIN", "20"))
LOG = BASE_DIR / "logs" / "watchdog.log"


def main() -> None:
    rows = get_store().query(
        "SELECT ts FROM system_health WHERE source='runner' ORDER BY id DESC LIMIT 1")
    if rows:
        last = datetime.strptime(rows[0]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    else:
        age_min = float("inf")
    if age_min <= STALE_MIN:
        line = f"{utcnow()} OK heartbeat age {age_min:.1f}m"
    else:
        line = f"{utcnow()} ALERT runner heartbeat stale ({age_min:.1f}m > {STALE_MIN}m)"
        hook = os.environ.get("INTEL_ALERT_WEBHOOK", "")
        if hook:
            try:
                req = urllib.request.Request(
                    hook, data=json.dumps({"text": f"[market-intel] {line}"}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                line += f" (webhook failed: {e!r})"
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
