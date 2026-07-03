"""review.py — the bot's self-critique. Runs after every closed trade.

Finds its own mistakes from DATA, not vibes, and writes them to:
  lessons table   one row per finding, visible on the dashboard
  memory.json     rolling counters the engine reads back at startup
  engine_state    cooldowns / disables that risk.py enforces next cycle

Mistake taxonomy (each tag has a concrete, checkable definition):
  stopped_then_reversed  SL hit, then price reached the original TP within
                         MAX_HOLD_BARS — the idea was right, the stop too tight
  against_htf_trend      entered opposite the H4 EMA20/50 trend at entry time
  high_spread_entry      entry spread was > 1.5x the symbol's median spread
  gap_beyond_stop        realized loss worse than 1.3R — slippage/gap through SL
  chop_timeout           time-stop exit with |R| < 0.2 — entry bought noise
  full_r_win             TP hit as planned (positive reinforcement, tracked too)

Aggregate protections (freqtrade StoplossGuard / MaxDrawdown analogues):
  COOLDOWN_AFTER_LOSSES consecutive losses on a combo -> cooldown COOLDOWN_HOURS
  DISABLE_AFTER_LOSSES_7D losses in 7 days -> combo disabled until the gate
  re-passes it on fresh data
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import config
from .analysis import Bars, regime
from .bridge import Bridge, BridgeError
from .store import Store, utcnow


def _load_memory() -> dict:
    if config.MEMORY_PATH.exists():
        try:
            return json.loads(config.MEMORY_PATH.read_text())
        except (ValueError, OSError):
            pass
    return {"combos": {}, "lessons_total": {}, "updated": None}


def _save_memory(mem: dict) -> None:
    mem["updated"] = utcnow()
    config.MEMORY_PATH.write_text(json.dumps(mem, indent=2))


def _lesson(store: Store, trade: dict, tag: str, lesson: str, detail: dict) -> None:
    store.insert("lessons", {
        "ts": utcnow(), "trade_id": trade["id"], "symbol": trade["symbol"],
        "strategy": trade["strategy"], "tag": tag, "lesson": lesson,
        "detail": detail})
    mem = _load_memory()
    mem["lessons_total"][tag] = mem["lessons_total"].get(tag, 0) + 1
    _save_memory(mem)


def on_trade_closed(store: Store, bridge: Bridge, trade: dict) -> list[str]:
    """Post-mortem for one closed trade row. Returns the tags found."""
    tags: list[str] = []
    ctx = {}
    try:
        ctx = json.loads(trade.get("context") or "{}")
    except ValueError:
        pass
    r = trade.get("r_multiple")
    pnl = trade.get("pnl") or 0.0
    won = pnl > 0

    # -- per-trade findings ------------------------------------------------------
    if trade.get("exit_reason") == "sl":
        try:
            raw = bridge.bars(trade["symbol"], config.TIMEFRAME, config.MAX_HOLD_BARS + 5)
            exit_t = datetime.fromisoformat(trade["exit_time"].replace("Z", "+00:00")).timestamp()
            after = [b for b in raw if b[0] > exit_t - 3 * 3600]  # bars around/after exit
            if after and trade.get("tp"):
                highs = max(b[2] for b in after)
                lows = min(b[3] for b in after)
                reached_tp = (highs >= trade["tp"] if trade["side"] == "buy"
                              else lows <= trade["tp"])
                if reached_tp:
                    tags.append("stopped_then_reversed")
                    _lesson(store, trade, "stopped_then_reversed",
                            "SL hit but price then reached the original TP — "
                            "stop was too tight for current %s volatility"
                            % trade["symbol"],
                            {"tp": trade["tp"], "sl": trade["sl"],
                             "entry_atr": trade.get("entry_atr")})
        except (BridgeError, ValueError, KeyError):
            pass  # no data, no claim — never invent a finding

    htf = ctx.get("htf_trend")
    if htf and not won:
        if (trade["side"] == "buy" and htf == "down") or \
           (trade["side"] == "sell" and htf == "up"):
            tags.append("against_htf_trend")
            _lesson(store, trade, "against_htf_trend",
                    "lost trading %s against the H4 trend (%s)" % (trade["side"], htf),
                    {"htf_trend": htf})

    med_spread = ctx.get("median_spread_points")
    ent_spread = trade.get("entry_spread_points")
    if med_spread and ent_spread and ent_spread > 1.5 * med_spread and not won:
        tags.append("high_spread_entry")
        _lesson(store, trade, "high_spread_entry",
                "entered on %.0f-point spread vs median %.0f — paid up for a loser"
                % (ent_spread, med_spread),
                {"entry_spread": ent_spread, "median": med_spread})

    if r is not None and r < -1.3:
        tags.append("gap_beyond_stop")
        _lesson(store, trade, "gap_beyond_stop",
                "realized %.2fR, worse than the planned -1R — slippage or gap "
                "through the stop" % r, {"r": r})

    if trade.get("exit_reason") in ("time_stop", "friday_flat") and r is not None \
            and abs(r) < 0.2:
        tags.append("chop_timeout")
        _lesson(store, trade, "chop_timeout",
                "time-stop exit at %.2fR — the entry bought noise, not movement" % r,
                {"r": r})

    if trade.get("exit_reason") == "tp":
        tags.append("full_r_win")

    # -- aggregate protections -----------------------------------------------------
    combo = "%s:%s" % (trade["strategy"], trade["symbol"])
    mem = _load_memory()
    c = mem["combos"].setdefault(combo, {"consecutive_losses": 0, "closed": 0})
    c["closed"] += 1
    c["consecutive_losses"] = 0 if won else c["consecutive_losses"] + 1
    _save_memory(mem)

    if c["consecutive_losses"] >= config.COOLDOWN_AFTER_LOSSES:
        until = (datetime.now(timezone.utc)
                 + timedelta(hours=config.COOLDOWN_HOURS)).isoformat()
        store.set_state("cooldown:%s:%s" % (trade["strategy"], trade["symbol"]), until)
        _lesson(store, trade, "cooldown_triggered",
                "%d consecutive losses on %s — cooling down until %s"
                % (c["consecutive_losses"], combo, until),
                {"until": until})

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    losses_7d = store.query(
        "SELECT COUNT(*) AS c FROM trades WHERE strategy=? AND symbol=? "
        "AND status='closed' AND exit_time >= ? AND pnl <= 0",
        (trade["strategy"], trade["symbol"], week_ago))[0]["c"]
    if losses_7d >= config.DISABLE_AFTER_LOSSES_7D:
        store.insert("strategy_status", {
            "ts": utcnow(), "strategy": trade["strategy"], "symbol": trade["symbol"],
            "status": "disabled",
            "reason": "%d losses in 7 days — disabled until the gate re-passes it "
                      "on fresh data" % losses_7d,
            "backtest": {}})
        _lesson(store, trade, "combo_disabled",
                "%s disabled after %d losses in 7 days" % (combo, losses_7d),
                {"losses_7d": losses_7d})
    return tags


def htf_context(bridge: Bridge, symbol: str) -> dict:
    """H4 regime snapshot recorded WITH each entry so the review can later
    judge the entry against the higher timeframe honestly."""
    try:
        raw = bridge.bars(symbol, "H4", 200)
        r = regime(Bars(raw))
        return {"htf_trend": r["trend"], "htf_rsi": round(r["rsi"], 1),
                "htf_vol": r["vol"]}
    except (BridgeError, ValueError):
        return {}


def build_daily_report(store: Store, date: str | None = None) -> dict:
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lo, hi = date + "T00:00:00Z", date + "T23:59:59Z"
    rows = store.query(
        "SELECT pnl FROM trades WHERE status='closed' AND exit_time BETWEEN ? AND ?",
        (lo, hi))
    pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
    eq = store.query(
        "SELECT equity FROM equity_curve WHERE ts BETWEEN ? AND ? ORDER BY id", (lo, hi))
    wins = [p for p in pnls if p > 0]
    report = {
        "date": date, "trades": len(pnls), "wins": len(wins),
        "losses": len(pnls) - len(wins),
        "pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "equity_open": eq[0]["equity"] if eq else None,
        "equity_close": eq[-1]["equity"] if eq else None,
        "best_trade": round(max(pnls), 2) if pnls else None,
        "worst_trade": round(min(pnls), 2) if pnls else None,
    }
    lessons = store.query(
        "SELECT tag, COUNT(*) AS c FROM lessons WHERE ts BETWEEN ? AND ? "
        "GROUP BY tag ORDER BY c DESC", (lo, hi))
    summary = "%s: %s trades, %s wins, pnl %.2f." % (
        date, report["trades"], report["wins"], report["pnl"])
    if lessons:
        summary += " Lessons: " + ", ".join("%s x%s" % (l["tag"], l["c"]) for l in lessons)
    report["summary"] = summary
    store.execute("DELETE FROM daily_reports WHERE date=?", (date,))
    store.insert("daily_reports", report)
    return report


if __name__ == "__main__":
    # synthetic closed-trade post-mortem (no bridge needed for these tags)
    s = Store()
    tid = s.insert("trades", {
        "ticket": 999999901, "symbol": "EURUSD", "strategy": "selftest",
        "side": "buy", "volume": 0.01, "entry_time": utcnow(),
        "entry_price": 1.1, "sl": 1.095, "tp": 1.11, "status": "closed",
        "exit_time": utcnow(), "exit_price": 1.093, "pnl": -7.0,
        "r_multiple": -1.4, "exit_reason": "sl",
        "entry_spread_points": 40, "entry_atr": 0.001,
        "context": json.dumps({"htf_trend": "down", "median_spread_points": 20})})
    trade = s.query("SELECT * FROM trades WHERE id=?", (tid,))[0]

    class _NoBridge:
        def bars(self, *a, **k):
            raise BridgeError("offline selftest")
    tags = on_trade_closed(s, _NoBridge(), trade)
    assert "against_htf_trend" in tags, tags
    assert "high_spread_entry" in tags, tags
    assert "gap_beyond_stop" in tags, tags
    rep = build_daily_report(s)
    assert rep["trades"] >= 1
    # cleanup selftest artifacts
    s.execute("DELETE FROM trades WHERE id=?", (tid,))
    s.execute("DELETE FROM lessons WHERE trade_id=?", (tid,))
    s.execute("DELETE FROM daily_reports WHERE date=?", (rep["date"],))
    build_daily_report(s)  # rebuild the real (empty) report for today
    print("REVIEW SELFTEST OK — tags:", tags)
    print("  report:", rep["summary"])
