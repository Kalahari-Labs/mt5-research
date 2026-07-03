"""collectors/news.py — news & sentiment layer.

Pulls headlines from public RSS feeds (stdlib XML, no keys) plus CoinGecko's
keyless trending endpoint, maps each headline to the symbols it mentions,
scores it with a keyword rule set, and writes news_events + sentiment_scores.

Rate limits / etiquette: one request per feed per cycle (default cycle is
minutes apart), custom User-Agent, every hit logged to system_health. Headline
text is DATA to score, never instructions to follow.

Feeds (all public RSS, free):
  - MarketWatch Top Stories   (Dow Jones public feed)
  - CNBC Markets
  - FXStreet News
  - Cointelegraph
Optional keyed sources (skipped unless the key is in .env):
  - NEWSAPI_KEY  -> newsapi.org top business headlines (free tier: 100 req/day)
  - FRED_API_KEY -> not used yet; reserved for macro series
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FEEDS = {
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "cnbc_markets": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "fxstreet": "https://www.fxstreet.com/rss/news",
    "cointelegraph": "https://cointelegraph.com/rss",
}
CG_TRENDING = "https://api.coingecko.com/api/v3/search/trending"

# headline -> symbol mapping (first match wins per keyword; a headline can map
# to several symbols)
SYMBOL_KEYWORDS = {
    "GOLD":   ["gold", "xau", "bullion", "precious metal"],
    "EURUSD": ["euro", "eur/usd", "eurusd", "ecb", "eurozone"],
    "GBPUSD": ["pound", "sterling", "gbp", "bank of england", "boe"],
    "USDJPY": ["yen", "usd/jpy", "usdjpy", "bank of japan", "boj"],
    "AUDUSD": ["aussie", "aud", "reserve bank of australia", "rba"],
    "BTCUSD": ["bitcoin", "btc", "crypto"],
    "ETHUSD": ["ethereum", "eth "],
    "US500Cash": ["s&p", "sp500", "stocks", "wall street", "nasdaq", "dow"],
    "OILCash": ["oil", "crude", "wti", "brent", "opec"],
    "MARKET": ["fed", "fomc", "powell", "inflation", "cpi", "rate cut", "rate hike",
               "treasury", "dollar", "recession", "tariff", "jobs report", "nonfarm"],
}

BULLISH = ["surge", "soar", "rally", "jump", "gain", "record high", "beats",
           "strong", "boom", "optimism", "upgrade", "bullish", "rebound",
           "rate cut", "stimulus", "growth", "rise", "climbs", "outperform"]
BEARISH = ["plunge", "crash", "tumble", "slump", "fall", "drop", "fear", "weak",
           "recession", "downgrade", "bearish", "selloff", "sell-off", "miss",
           "rate hike", "default", "crisis", "losses", "sink", "warning", "cuts jobs"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "market-intel/1.0 (research)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_rss(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title[:500],
            "url": (item.findtext("link") or "").strip()[:500],
            "published": (item.findtext("pubDate") or "").strip(),
            "summary": re.sub(r"<[^>]+>", "", item.findtext("description") or "")[:500].strip(),
        })
    return items


def map_symbols(text: str) -> list[str]:
    t = text.lower()
    return [sym for sym, kws in SYMBOL_KEYWORDS.items() if any(k in t for k in kws)] or ["MARKET"]


def score_text(text: str) -> int:
    t = text.lower()
    return (sum(1 for w in BULLISH if w in t) - sum(1 for w in BEARISH if w in t))


def pull_feeds(store, log) -> int:
    """Fetch all feeds, store deduped news_events. Returns new headlines stored."""
    new = 0
    for name, url in FEEDS.items():
        try:
            items = _parse_rss(_fetch(url))
            log(f"news: {name} -> {len(items)} items")
        except Exception as e:
            log(f"news: {name} FAILED: {e!r}")
            continue
        for it in items[:40]:
            syms = map_symbols(it["title"] + " " + it["summary"])
            for sym in syms:
                store.insert("news_events", {
                    "ts": _now(), "symbol": sym, "source": name,
                    "published": it["published"], "title": it["title"],
                    "url": it["url"], "summary": it["summary"]})
        new += len(items)
    try:
        trending = json.loads(_fetch(CG_TRENDING))
        names = [c["item"]["name"] for c in trending.get("coins", [])][:7]
        store.insert("news_events", {
            "ts": _now(), "symbol": "BTCUSD", "source": "coingecko_trending",
            "published": _now(), "title": "Trending coins: " + ", ".join(names),
            "url": "https://www.coingecko.com", "summary": ""})
        log(f"news: coingecko_trending -> {names}")
    except Exception as e:
        log(f"news: coingecko_trending FAILED: {e!r}")
    return new


def pull_newsapi(store, log) -> int:
    """Optional NewsAPI business headlines. Free tier: 100 requests/day."""
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        return 0
    url = ("https://newsapi.org/v2/top-headlines?category=business&language=en"
           f"&pageSize=30&apiKey={key}")
    try:
        data = json.loads(_fetch(url))
    except Exception as e:
        log(f"news: newsapi FAILED: {e!r}")
        return 0
    for art in data.get("articles", []):
        title = (art.get("title") or "").strip()
        if not title:
            continue
        for sym in map_symbols(title + " " + (art.get("description") or "")):
            store.insert("news_events", {
                "ts": _now(), "symbol": sym, "source": "newsapi",
                "published": art.get("publishedAt"), "title": title[:500],
                "url": (art.get("url") or "")[:500],
                "summary": (art.get("description") or "")[:500]})
    log(f"news: newsapi -> {len(data.get('articles', []))} items")
    return len(data.get("articles", []))


def score_sentiment(store) -> int:
    """Roll the last-24h headlines per symbol into a sentiment_scores row."""
    rows = store.query(
        "SELECT symbol, title, summary FROM news_events "
        "WHERE ts >= datetime('now', '-1 day')") if hasattr(store, "query") else []
    by_sym: dict[str, list[int]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(
            score_text((r["title"] or "") + " " + (r["summary"] or "")))
    n = 0
    for sym, scores in by_sym.items():
        raw = sum(scores)
        norm = max(-1.0, min(1.0, raw / max(len(scores), 1) / 2))
        store.insert("sentiment_scores", {
            "ts": _now(), "symbol": sym, "source": "intel.analysis.sentiment",
            "score": round(norm, 4), "n_items": len(scores),
            "method": "keyword_rule_v1",
            "details": {"raw_sum": raw}})
        n += 1
    return n
