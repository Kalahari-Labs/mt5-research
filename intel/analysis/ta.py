"""analysis/ta.py — DESCRIPTIVE technical-analysis layer.

Computes what the market IS DOING: trend direction/strength, volatility regime,
key support/resistance from swing-point clustering, and liquidity sweeps
(equal highs/lows taken out then rejected). Writes structured numbers and
labels to technical_signals.

This layer is DESCRIPTIVE, not PRESCRIPTIVE — there are deliberately no
buy/sell/recommendation fields anywhere in it, and nothing downstream may add
them. See SHORTHOLDS.md in the parent repo: 29/29 tested strategies failed
walk-forward validation; describing structure is all this system is allowed
to do.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np


def ema(x: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(close: np.ndarray, period: int = 14) -> float:
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    if len(gain) < period:
        return 50.0
    avg_g, avg_l = gain[:period].mean(), loss[:period].mean()
    for i in range(period, len(gain)):
        avg_g = (avg_g * (period - 1) + gain[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    out = np.empty(len(close), dtype=float)
    out[:period + 1] = tr[:period].mean() if len(tr) >= period else (high - low).mean()
    for i in range(period, len(tr)):
        out[i + 1] = (out[i] * (period - 1) + tr[i]) / period
    return out


def swing_points(high: np.ndarray, low: np.ndarray, k: int = 3):
    """Indices of local swing highs/lows (strict fractal with k bars each side)."""
    sh, sl = [], []
    for i in range(k, len(high) - k):
        if high[i] == high[i - k:i + k + 1].max():
            sh.append(i)
        if low[i] == low[i - k:i + k + 1].min():
            sl.append(i)
    return sh, sl


def cluster_levels(prices: list[float], tol: float) -> list[dict]:
    """Group nearby swing prices into levels; more touches = stronger level."""
    levels: list[dict] = []
    for p in sorted(prices):
        if levels and abs(p - levels[-1]["price"]) <= tol:
            lv = levels[-1]
            lv["touches"] += 1
            lv["price"] = lv["price"] + (p - lv["price"]) / lv["touches"]
        else:
            levels.append({"price": p, "touches": 1})
    return [lv for lv in levels if lv["touches"] >= 2] or levels


def detect_sweep(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 tol: float, lookback: int = 40):
    """Liquidity sweep: equal highs (lows) within tol, later swept by a wick that
    CLOSES back below (above) the level within 3 bars — swept then reversed."""
    n = len(close)
    start = max(0, n - lookback)
    sh, sl = swing_points(high[start:], low[start:], k=2)
    sh = [i + start for i in sh]
    sl = [i + start for i in sl]

    for idxs, arr, kind in ((sh, high, "high_sweep"), (sl, low, "low_sweep")):
        eq_pairs = [(a, b) for i, a in enumerate(idxs) for b in idxs[i + 1:]
                    if abs(arr[a] - arr[b]) <= tol]
        for a, b in eq_pairs:
            level = (arr[a] + arr[b]) / 2
            for j in range(b + 1, n):
                if kind == "high_sweep" and high[j] > level + tol * 0.5:
                    reversed_ = any(close[m] < level for m in range(j, min(j + 3, n)))
                    if reversed_ and j >= n - 10:  # only report recent sweeps
                        return kind, float(level)
                    break
                if kind == "low_sweep" and low[j] < level - tol * 0.5:
                    reversed_ = any(close[m] > level for m in range(j, min(j + 3, n)))
                    if reversed_ and j >= n - 10:
                        return kind, float(level)
                    break
    return None, None


def analyze(symbol: str, timeframe: str, bars: list[list[float]]) -> dict | None:
    """bars: [[epoch, o, h, l, c, v], ...] oldest-first. Returns a technical_signals row."""
    if not bars or len(bars) < 60:
        return None
    arr = np.array(bars, dtype=float)
    high, low, close = arr[:, 2], arr[:, 3], arr[:, 4]
    last = float(close[-1])

    a = atr(high, low, close)
    cur_atr = float(a[-1])
    e20, e50 = ema(close, 20), ema(close, 50)
    gap = float(e20[-1] - e50[-1])
    strength = min(1.0, abs(gap) / cur_atr) if cur_atr > 0 else 0.0
    trend = "up" if gap > 0.1 * cur_atr else "down" if gap < -0.1 * cur_atr else "flat"

    # volatility regime: current ATR vs its own trailing distribution
    hist = a[len(a) // 2:]
    lo_q, hi_q = np.quantile(hist, 0.25), np.quantile(hist, 0.75)
    vol_regime = "high" if cur_atr > hi_q else "low" if cur_atr < lo_q else "normal"

    sh, sl = swing_points(high, low)
    tol = cur_atr * 0.25 if cur_atr > 0 else last * 1e-4
    levels = cluster_levels([float(high[i]) for i in sh] + [float(low[i]) for i in sl], tol)
    below = [lv["price"] for lv in levels if lv["price"] < last]
    above = [lv["price"] for lv in levels if lv["price"] > last]
    support = max(below) if below else None
    resistance = min(above) if above else None

    sweep, sweep_level = detect_sweep(high, low, close, tol)

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": symbol, "source": "intel.analysis.ta", "timeframe": timeframe,
        "trend": trend, "trend_strength": round(strength, 4),
        "volatility_regime": vol_regime, "atr": round(cur_atr, 6),
        "rsi": round(rsi(close), 2),
        "support": support, "resistance": resistance,
        "sweep": sweep, "sweep_level": sweep_level,
        "close": last,
        "details": {"levels": [{"price": round(lv["price"], 6), "touches": lv["touches"]}
                                for lv in levels[:12]],
                    "ema20": round(float(e20[-1]), 6), "ema50": round(float(e50[-1]), 6)},
    }


def run(store) -> int:
    """Analyze every symbol/timeframe in the latest bridge pull. Returns rows written."""
    from collectors.prices import load_live_bars, LIVE_JSON
    if not LIVE_JSON.exists():
        return 0
    payload = json.loads(LIVE_JSON.read_text())
    n = 0
    for sym, entry in payload.get("symbols", {}).items():
        for tf in ("H1", "H4", "D1"):
            bars = entry.get("bars", {}).get(tf)
            if not isinstance(bars, list):
                continue
            row = analyze(sym, tf, bars)
            if row:
                store.insert("technical_signals", row)
                n += 1
    return n
