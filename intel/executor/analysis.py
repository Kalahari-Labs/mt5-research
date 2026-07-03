"""analysis.py — causal indicator engine shared by BACKTEST and LIVE paths.

Every function returns arrays where index i uses ONLY data up to and including
bar i (no look-ahead by construction). Strategies read these arrays at a single
index; the backtester replays the same reads bar by bar — that is the
backtest/live parity guarantee.

Bars format everywhere: [[epoch, o, h, l, c, tick_volume, spread_points], ...]
oldest-first, as served by bridge /bars.
"""
from __future__ import annotations

import numpy as np


class Bars:
    """Column view over the bridge bar format + cached indicators."""

    def __init__(self, raw: list[list[float]]):
        if not raw or len(raw) < 60:
            raise ValueError("need >= 60 bars, got %s" % (len(raw) if raw else 0))
        a = np.asarray(raw, dtype=float)
        self.time = a[:, 0].astype(np.int64)
        self.open = a[:, 1]
        self.high = a[:, 2]
        self.low = a[:, 3]
        self.close = a[:, 4]
        self.volume = a[:, 5]
        self.spread_points = a[:, 6] if a.shape[1] > 6 else np.zeros(len(a))
        self.n = len(a)
        self._cache: dict = {}

    def cached(self, key: str, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # -- cached standard indicators ---------------------------------------------
    def ema(self, period: int) -> np.ndarray:
        return self.cached(f"ema{period}", lambda: ema(self.close, period))

    def sma(self, period: int) -> np.ndarray:
        return self.cached(f"sma{period}", lambda: sma(self.close, period))

    def rsi(self, period: int = 14) -> np.ndarray:
        return self.cached(f"rsi{period}", lambda: rsi_series(self.close, period))

    def atr(self, period: int = 14) -> np.ndarray:
        return self.cached(f"atr{period}", lambda: atr_series(self.high, self.low, self.close, period))

    def bollinger(self, period: int = 20, k: float = 2.0):
        def _bb():
            mid = sma(self.close, period)
            sd = rolling_std(self.close, period)
            return mid, mid + k * sd, mid - k * sd
        return self.cached(f"bb{period}_{k}", _bb)

    def donchian(self, period: int = 20):
        def _dc():
            hi = rolling_max(self.high, period)
            lo = rolling_min(self.low, period)
            return hi, lo
        return self.cached(f"dc{period}", _dc)

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9):
        """(macd_line, signal_line, histogram) — all causal."""
        def _macd():
            line = ema(self.close, fast) - ema(self.close, slow)
            sig = ema(line, signal)
            return line, sig, line - sig
        return self.cached(f"macd{fast}_{slow}_{signal}", _macd)

    def hour(self) -> np.ndarray:
        """Bar-open hour in BROKER SERVER time (what MT5 stamps bars with)."""
        return self.cached("hour", lambda: ((self.time % 86400) // 3600).astype(int))


# ---- primitive causal indicators (index i sees bars <= i) ----------------------

def ema(x: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def sma(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    out[:period - 1] = out[period - 1] if len(x) >= period else np.nan
    return out


def rolling_std(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(period - 1, len(x)):
        out[i] = x[i - period + 1:i + 1].std()
    out[:period - 1] = out[period - 1] if len(x) >= period else np.nan
    return out


def rolling_max(x: np.ndarray, period: int) -> np.ndarray:
    out = np.empty(len(x))
    for i in range(len(x)):
        out[i] = x[max(0, i - period + 1):i + 1].max()
    return out


def rolling_min(x: np.ndarray, period: int) -> np.ndarray:
    out = np.empty(len(x))
    for i in range(len(x)):
        out[i] = x[max(0, i - period + 1):i + 1].min()
    return out


def rsi_series(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI as a full causal series."""
    n = len(close)
    out = np.full(n, 50.0)
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    if n <= period:
        return out
    avg_g, avg_l = gain[:period].mean(), loss[:period].mean()
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period, len(delta)):
        avg_g = (avg_g * (period - 1) + gain[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i]) / period
        out[i + 1] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 14) -> np.ndarray:
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    out = np.empty(len(close), dtype=float)
    seed = tr[:period].mean() if len(tr) >= period else (high - low).mean()
    out[:period + 1] = seed
    for i in range(period, len(tr)):
        out[i + 1] = (out[i] * (period - 1) + tr[i]) / period
    return out


# ---- market view (regime snapshot for journaling + filters) --------------------

def regime(bars: Bars, i: int | None = None) -> dict:
    """Descriptive regime at bar i (default: last bar). Same fields the intel
    plane computes, evaluated causally so the backtester can use it too."""
    i = bars.n - 1 if i is None else i
    e20, e50 = bars.ema(20), bars.ema(50)
    a = bars.atr(14)
    cur_atr = float(a[i])
    gap = float(e20[i] - e50[i])
    trend = "up" if gap > 0.1 * cur_atr else "down" if gap < -0.1 * cur_atr else "flat"
    lo = max(0, i - 200)
    hist = a[lo:i + 1]
    lo_q, hi_q = np.quantile(hist, 0.25), np.quantile(hist, 0.75)
    vol = "high" if cur_atr > hi_q else "low" if cur_atr < lo_q else "normal"
    return {"trend": trend, "vol": vol, "atr": cur_atr,
            "rsi": float(bars.rsi(14)[i]), "ema20": float(e20[i]),
            "ema50": float(e50[i]), "close": float(bars.close[i]),
            "trend_strength": min(1.0, abs(gap) / cur_atr) if cur_atr > 0 else 0.0}


if __name__ == "__main__":
    # deterministic self-test: rising series -> up trend, RSI > 50
    rng = np.random.default_rng(7)
    n = 300
    px = np.cumsum(rng.normal(0.3, 1.0, n)) + 100
    raw = [[i * 3600, px[i], px[i] + 0.5, px[i] - 0.5, px[i] + 0.1, 100, 10]
           for i in range(n)]
    b = Bars(raw)
    r = regime(b)
    assert r["trend"] == "up", r
    assert r["rsi"] > 50, r
    assert r["atr"] > 0, r
    hi, lo_ = b.donchian(20)
    assert hi[-1] >= b.high[-20:].max() - 1e-9
    mid, up, low_ = b.bollinger(20, 2.0)
    assert low_[-1] < mid[-1] < up[-1]
    # causality check: value at i must not change when future bars are appended
    b2 = Bars(raw[:200])
    assert abs(b2.ema(20)[199] - b.ema(20)[199]) < 1e-12
    assert abs(b2.rsi(14)[199] - b.rsi(14)[199]) < 1e-12
    assert abs(b2.atr(14)[199] - b.atr(14)[199]) < 1e-12
    assert abs(b2.macd()[2][199] - b.macd()[2][199]) < 1e-12
    assert (b.hour() == ((b.time % 86400) // 3600)).all()
    print("ANALYSIS SELFTEST OK", {k: round(v, 3) if isinstance(v, float) else v
                                   for k, v in r.items()})
