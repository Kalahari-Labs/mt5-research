"""news_calendar.py — high-impact economic calendar (ForexFactory weekly JSON).

Free keyless feed, refreshed every CALENDAR_REFRESH_HOURS. Events land in the
calendar_events table; risk.py enforces a +/- NEWS_BLACKOUT_MIN minute entry
blackout per affected currency. If the feed is down we fail SAFE for data
(keep last known events) and journal the failure — we never invent events.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config
from .store import Store, utcnow


def fetch_week() -> list[dict]:
    req = urllib.request.Request(
        config.FF_CALENDAR_URL, headers={"User-Agent": "market-intel-executor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def refresh(store: Store) -> int:
    """Pull the weekly feed; store HIGH-impact events. Returns rows added."""
    last = store.get_state("calendar_last_refresh")
    if last:
        then = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
        if (time.time() - then) < config.CALENDAR_REFRESH_HOURS * 3600:
            return 0
    try:
        events = fetch_week()
    except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError) as e:
        # data hiccup, not a halt — the engine keeps trading on last known events
        store.decide("manage", "calendar refresh failed (keeping last known): %r" % e)
        store.set_state("calendar_last_error", {"ts": utcnow(), "error": repr(e)})
        return 0
    n = 0
    for ev in events:
        if str(ev.get("impact", "")).lower() != "high":
            continue
        # feed dates are ISO with offset, e.g. "2026-07-03T08:30:00-04:00"
        try:
            ts = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        if store.insert_calendar_event(
                ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ev.get("country", ""), ev.get("title", "")):
            n += 1
    store.set_state("calendar_last_refresh", utcnow())
    return n


def blackout(store: Store, symbol: str, now_utc: datetime | None = None) -> str | None:
    """Return the blocking event title if `symbol` is inside a news blackout."""
    currencies = config.currencies_for(symbol)
    if not currencies:
        return None
    now = now_utc or datetime.now(timezone.utc)
    pad = config.NEWS_BLACKOUT_MIN * 60
    lo = datetime.fromtimestamp(now.timestamp() - pad, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = datetime.fromtimestamp(now.timestamp() + pad, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = store.events_in_window(currencies, lo, hi)
    if rows:
        r = rows[0]
        return "%s %s @ %s" % (r["currency"], r["title"], r["ts_event"])
    return None


if __name__ == "__main__":
    s = Store()
    added = refresh(s)
    total = s.query("SELECT COUNT(*) AS c FROM calendar_events")[0]["c"]
    nxt = s.query("SELECT * FROM calendar_events WHERE ts_event > ? ORDER BY ts_event LIMIT 5",
                  (utcnow(),))
    print("CALENDAR OK — added %s, total %s high-impact events" % (added, total))
    for r in nxt:
        print("  next:", r["ts_event"], r["currency"], r["title"])
    for sym in config.SYMBOLS:
        print("  blackout %-8s ->" % sym, blackout(s, sym))
