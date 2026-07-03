"""collectors/prices.py — market-data collection (READ-ONLY).

Two sources per cycle:
  1. MT5 via the Wine bridge: runs mt5_pull.py (read-only calls), ingests the
     JSON it writes into price_snapshots.
  2. CoinGecko free keyless API: BTC/ETH spot -> price_snapshots as 'tick' rows.
     Free tier: ~30 req/min without a key; we make 1 request per cycle.

Every failure is caught, logged, and reported through system_health — one bad
source never kills the loop.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_JSON = BASE_DIR / "data" / "mt5_live.json"
WINE_PY = r"C:\Program Files\Python312\python.exe"
PULL_SCRIPT = r"Z:\home\flowdaaddy\mt5-research\intel\collectors\mt5_pull.py"

COINGECKO_URL = ("https://api.coingecko.com/api/v3/simple/price"
                 "?ids=bitcoin,ethereum&vs_currencies=usd"
                 "&include_24hr_vol=true&include_last_updated_at=true")
CG_SYMBOLS = {"bitcoin": "BTCUSD", "ethereum": "ETHUSD"}


def _iso(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_mt5(timeout: int = 240) -> bool:
    """Run the Wine-side read-only puller. Returns True if it wrote fresh JSON."""
    env = dict(os.environ, WINEPREFIX=str(Path.home() / ".mt5"), WINEDEBUG="-all")
    proc = subprocess.run(
        ["wine", WINE_PY, PULL_SCRIPT],
        capture_output=True, text=True, timeout=timeout, env=env)
    return "[PULL-OK]" in (proc.stdout or "")


def ingest_mt5(store) -> int:
    """Load mt5_live.json into price_snapshots. Returns rows actually inserted."""
    payload = json.loads(LIVE_JSON.read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    for sym, entry in payload.get("symbols", {}).items():
        tick = entry.get("tick")
        if tick and tick.get("bid"):
            mid = (tick["bid"] + tick["ask"]) / 2
            store.insert("price_snapshots", {
                "ts": now, "symbol": sym, "source": "mt5_bridge", "timeframe": "tick",
                "bar_time": _iso(tick["time"]), "open": mid, "high": tick["ask"],
                "low": tick["bid"], "close": mid, "volume": tick.get("volume") or 0})
            inserted += 1
        for tf, bars in (entry.get("bars") or {}).items():
            if isinstance(bars, dict):  # {"error": ...}
                continue
            rows = [{"ts": now, "symbol": sym, "source": "mt5_bridge", "timeframe": tf,
                     "bar_time": _iso(b[0]), "open": b[1], "high": b[2], "low": b[3],
                     "close": b[4], "volume": b[5]} for b in bars[-30:]]
            inserted += store.insert_many("price_snapshots", rows)
    return inserted


def load_live_bars(symbol: str, timeframe: str) -> list[list[float]] | None:
    """Full 300-bar window from the latest bridge pull (for the analysis layer)."""
    if not LIVE_JSON.exists():
        return None
    payload = json.loads(LIVE_JSON.read_text())
    bars = payload.get("symbols", {}).get(symbol, {}).get("bars", {}).get(timeframe)
    return bars if isinstance(bars, list) else None


def pull_coingecko(store) -> int:
    req = urllib.request.Request(COINGECKO_URL, headers={"User-Agent": "market-intel/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for coin, sym in CG_SYMBOLS.items():
        if coin not in data:
            continue
        px = float(data[coin]["usd"])
        store.insert("price_snapshots", {
            "ts": now, "symbol": sym, "source": "coingecko", "timeframe": "tick",
            "bar_time": _iso(data[coin].get("last_updated_at", time.time())),
            "open": px, "high": px, "low": px, "close": px,
            "volume": float(data[coin].get("usd_24h_vol") or 0)})
        n += 1
    return n
