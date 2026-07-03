"""runner.py — the 24/7 loop (Phases 2-5 orchestrated, Phase 7 resilience).

Each cycle (INTEL_CYCLE_SEC, default 300s):
  1. Wine-bridge MT5 pull (read-only)  -> price_snapshots
  2. CoinGecko crypto spot             -> price_snapshots
  3. Technical analysis                -> technical_signals
  4. Market-state synthesis            -> market_state
Every NEWS_EVERY_N cycles (default 3, ≈15 min — respectful to the feeds):
  5. RSS/news pull + sentiment scoring -> news_events, sentiment_scores

Every step is individually try/except'd: one bad source logs to system_health
and the loop continues. A heartbeat row is written every cycle. After
ALERT_AFTER consecutive failures of the same step, an ALERT line is logged
(and POSTed to INTEL_ALERT_WEBHOOK if set in .env).

READ-ONLY BOUNDARY: this process observes and records. It imports nothing from
execution.py/risk.py and calls no order function anywhere.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from store import get_store, heartbeat, utcnow  # noqa: E402
from collectors import prices, news  # noqa: E402
from analysis import ta, synthesis  # noqa: E402

CYCLE_SEC = int(os.environ.get("INTEL_CYCLE_SEC", "300"))
NEWS_EVERY_N = int(os.environ.get("INTEL_NEWS_EVERY_N", "3"))
ALERT_AFTER = int(os.environ.get("INTEL_ALERT_AFTER", "3"))
WEBHOOK = os.environ.get("INTEL_ALERT_WEBHOOK", "")
LOG_PATH = BASE_DIR / "logs" / "runner.log"

_fail_streak: dict[str, int] = {}


def log(msg: str) -> None:
    line = f"{utcnow()} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def alert(source: str, message: str) -> None:
    log(f"ALERT [{source}] {message}")
    if WEBHOOK:
        try:
            req = urllib.request.Request(
                WEBHOOK, data=json.dumps({"text": f"[market-intel] {source}: {message}"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"ALERT webhook failed: {e!r}")


def step(store, name: str, fn) -> object | None:
    """Run one pipeline step; isolate failures; escalate repeated ones."""
    t0 = time.time()
    try:
        result = fn()
        _fail_streak[name] = 0
        heartbeat(store, name, "ok", latency_ms=round((time.time() - t0) * 1000, 1),
                  message=str(result))
        return result
    except Exception as e:
        _fail_streak[name] = _fail_streak.get(name, 0) + 1
        heartbeat(store, name, "error", message=repr(e)[:300])
        log(f"STEP FAIL [{name}] ({_fail_streak[name]}x): {e!r}")
        log(traceback.format_exc(limit=3))
        if _fail_streak[name] >= ALERT_AFTER:
            alert(name, f"{_fail_streak[name]} consecutive failures: {e!r}")
        return None


def run_forever() -> None:
    store = get_store()
    log(f"runner starting: backend={store.name} cycle={CYCLE_SEC}s news_every={NEWS_EVERY_N}")
    cycle = 0
    while True:
        cycle += 1
        t0 = time.time()
        pulled = step(store, "collector.mt5_pull", prices.pull_mt5)
        if pulled:
            step(store, "collector.mt5_ingest", lambda: prices.ingest_mt5(store))
        step(store, "collector.coingecko", lambda: prices.pull_coingecko(store))
        step(store, "analysis.ta", lambda: ta.run(store))
        step(store, "analysis.synthesis", lambda: synthesis.run(store))
        if cycle % NEWS_EVERY_N == 1:
            step(store, "collector.news", lambda: news.pull_feeds(store, log))
            step(store, "collector.newsapi", lambda: news.pull_newsapi(store, log))
            step(store, "analysis.sentiment", lambda: news.score_sentiment(store))
        heartbeat(store, "runner", "ok", cycle=cycle,
                  latency_ms=round((time.time() - t0) * 1000, 1),
                  message=f"cycle {cycle} complete")
        log(f"cycle {cycle} done in {time.time() - t0:.1f}s; sleeping {CYCLE_SEC}s")
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    if "--once" in sys.argv:
        CYCLE_SEC = 0
        store = get_store()
        cycle_backup = run_forever  # not used; run a single pass inline instead
        # single pass for testing
        pulled = step(store, "collector.mt5_pull", prices.pull_mt5)
        if pulled:
            step(store, "collector.mt5_ingest", lambda: prices.ingest_mt5(store))
        step(store, "collector.coingecko", lambda: prices.pull_coingecko(store))
        step(store, "analysis.ta", lambda: ta.run(store))
        step(store, "analysis.synthesis", lambda: synthesis.run(store))
        step(store, "collector.news", lambda: news.pull_feeds(store, log))
        step(store, "analysis.sentiment", lambda: news.score_sentiment(store))
        heartbeat(store, "runner", "ok", cycle=0, message="--once pass complete")
        log("single pass complete")
    else:
        run_forever()
