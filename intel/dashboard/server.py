"""dashboard/server.py — read-only web dashboard (Phase 6).

Stdlib http.server serving:
  /                    the single-page dashboard (index.html)
  /api/overview        one JSON blob: prices, market state, signals, news,
                       sentiment, system health, research-registry verdicts

Strictly read-only: it queries the intel store and the parent repo's research
journal. There is no order-entry endpoint, no POST handler, no path to a broker.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
from store import get_store  # noqa: E402

JOURNAL = BASE_DIR.parent.parent / "data" / "journal.sqlite"
PORT = 8899
START = time.time()


def registry_results(limit: int = 30) -> list[dict]:
    if not JOURNAL.exists():
        return []
    conn = sqlite3.connect(f"file:{JOURNAL}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT ts, run_type, strategy, symbol, timeframe, metrics_is, metrics_oos, notes "
        "FROM results ORDER BY id DESC LIMIT ?", (limit,))]
    conn.close()
    for r in rows:
        for k in ("metrics_is", "metrics_oos"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except Exception:
                    pass
    return rows


def overview() -> dict:
    s = get_store()
    prices = s.query(
        "SELECT symbol, source, close AS price, volume, bar_time, ts FROM price_snapshots "
        "WHERE timeframe='tick' AND id IN "
        "(SELECT MAX(id) FROM price_snapshots WHERE timeframe='tick' GROUP BY symbol) "
        "ORDER BY symbol")
    state = s.query(
        "SELECT * FROM market_state WHERE id IN "
        "(SELECT MAX(id) FROM market_state GROUP BY symbol) ORDER BY symbol")
    signals = s.query(
        "SELECT ts, symbol, timeframe, trend, trend_strength, volatility_regime, rsi, atr, "
        "support, resistance, sweep, sweep_level, close FROM technical_signals "
        "WHERE id IN (SELECT MAX(id) FROM technical_signals GROUP BY symbol, timeframe) "
        "ORDER BY symbol, timeframe")
    news = s.query(
        "SELECT ts, symbol, source, published, title, url FROM news_events "
        "GROUP BY title ORDER BY id DESC LIMIT 40")
    sentiment = s.query(
        "SELECT symbol, score, n_items, ts FROM sentiment_scores WHERE id IN "
        "(SELECT MAX(id) FROM sentiment_scores GROUP BY symbol) ORDER BY symbol")
    health = s.query(
        "SELECT source, status, cycle, latency_ms, message, ts FROM system_health "
        "ORDER BY id DESC LIMIT 25")
    counts = s.query(
        "SELECT (SELECT COUNT(*) FROM price_snapshots) AS prices, "
        "(SELECT COUNT(*) FROM technical_signals) AS signals, "
        "(SELECT COUNT(*) FROM news_events) AS news, "
        "(SELECT COUNT(*) FROM market_state) AS states")[0]
    for row in state:
        if row.get("details"):
            try:
                row["details"] = json.loads(row["details"])
            except Exception:
                pass
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "server_uptime_s": int(time.time() - START),
            "prices": prices, "market_state": state, "signals": signals,
            "news": news, "sentiment": sentiment, "health": health,
            "row_counts": counts, "registry": registry_results()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/overview"):
            body = json.dumps(overview(), default=str).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = (BASE_DIR / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet access log
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"market-intel dashboard (read-only) on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
