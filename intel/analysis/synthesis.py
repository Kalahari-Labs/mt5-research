"""analysis/synthesis.py — market-state synthesis (Phase 5).

Reads the outputs of the collector, TA, and sentiment layers and writes one
periodic market_state row per symbol: trend, volatility regime, nearby levels,
sentiment, news pressure, and a DESCRIPTIVE "setup" block — the structure the
analysis sees (level of interest, next opposing level, invalidation level) with
an empirical confidence measured on the same 300-bar window.

This layer knows what is happening. By design it stops at knowing: no field in
market_state is an instruction, nothing reads it to place orders, and the
setup_confidence number is a measured historical frequency, not a promise.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empirical_hit_rate(bars: list[list[float]], direction: str, atr_val: float,
                       horizon: int = 20) -> float:
    """Measured frequency, over this window, that price moved 1x ATR in the
    trend direction before 1x ATR against it, within `horizon` bars. This is a
    description of recent behavior — NOT a forecast, NOT a win rate promise.
    (Parent repo context: 29/29 strategies failed walk-forward; treat any
    number here with that skepticism.)"""
    if atr_val <= 0 or len(bars) < horizon + 10:
        return 0.0
    arr = np.array(bars, dtype=float)
    close = arr[:, 4]
    sign = 1.0 if direction == "long-structure" else -1.0
    wins = total = 0
    for i in range(0, len(close) - horizon):
        fwd = (close[i + 1:i + 1 + horizon] - close[i]) * sign
        hit_for = np.argmax(fwd >= atr_val) if (fwd >= atr_val).any() else None
        hit_against = np.argmax(fwd <= -atr_val) if (fwd <= -atr_val).any() else None
        if hit_for is None and hit_against is None:
            continue
        total += 1
        if hit_against is None or (hit_for is not None and hit_for < hit_against):
            wins += 1
    return round(wins / total, 3) if total else 0.0


def synthesize_symbol(store, symbol: str, bars_by_tf: dict) -> dict | None:
    sigs = store.latest("technical_signals", symbol, 6)
    sig = next((s for s in sigs if s["timeframe"] == "H4"), sigs[0] if sigs else None)
    if sig is None:
        return None
    sent_rows = store.latest("sentiment_scores", symbol, 1)
    sentiment = sent_rows[0]["score"] if sent_rows else None
    news24 = store.query(
        "SELECT COUNT(*) AS n FROM news_events WHERE symbol=? AND ts >= datetime('now','-1 day')",
        (symbol,))[0]["n"]

    trend, sweep = sig["trend"], sig["sweep"]
    support, resistance, atr_val = sig["support"], sig["resistance"], sig["atr"] or 0
    close = sig["close"]

    # Descriptive structure: which side the current trend + sweep pattern points at.
    if trend == "up" or sweep == "low_sweep":
        bias, entry, target = "long-structure", support, resistance
        stop = (support - atr_val) if support else None
    elif trend == "down" or sweep == "high_sweep":
        bias, entry, target = "short-structure", resistance, support
        stop = (resistance + atr_val) if resistance else None
    else:
        bias = "neutral"
        entry = target = stop = None

    conf = 0.0
    bars = bars_by_tf.get("H4") or bars_by_tf.get("D1")
    if bias != "neutral" and isinstance(bars, list):
        conf = empirical_hit_rate(bars, bias, atr_val)

    summary = (f"{symbol}: {trend} trend ({sig['timeframe']}), {sig['volatility_regime']} vol, "
               f"close {close}, S {support} / R {resistance}"
               + (f", {sweep} @ {sig['sweep_level']}" if sweep else "")
               + (f", sentiment {sentiment:+.2f} ({news24} headlines/24h)" if sentiment is not None else ""))

    return {
        "ts": _now(), "symbol": symbol, "source": "intel.analysis.synthesis",
        "trend": trend, "volatility_regime": sig["volatility_regime"],
        "last_close": close, "support": support, "resistance": resistance,
        "sweep": sweep, "sentiment": sentiment, "news_count_24h": news24,
        "setup_bias": bias, "setup_entry": entry, "setup_target": target,
        "setup_stop": stop, "setup_confidence": conf,
        "summary": summary,
        "details": {"rsi": sig["rsi"], "trend_strength": sig["trend_strength"],
                    "signal_tf": sig["timeframe"],
                    "note": "descriptive structure; empirical confidence on recent window; not a recommendation"},
    }


def run(store) -> int:
    from collectors.prices import LIVE_JSON
    payload = json.loads(LIVE_JSON.read_text()) if LIVE_JSON.exists() else {"symbols": {}}
    symbols = list(payload["symbols"].keys()) or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD"]
    n = 0
    for sym in symbols:
        bars_by_tf = payload["symbols"].get(sym, {}).get("bars", {})
        row = synthesize_symbol(store, sym, bars_by_tf)
        if row:
            store.insert("market_state", row)
            n += 1
    return n
