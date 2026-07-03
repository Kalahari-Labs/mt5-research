"""ts_momentum.py — time-series (trend-following) momentum, Moskowitz-Ooi-Pedersen
style, behind the SAME Strategy contract as SMA. One file + one registry line; the
engine, the SMA strategy, and the cost/walk-forward machinery are untouched.

CORE SIGNAL (time-series momentum):
    trailing_return[t] = close[t] / close[t - LOOKBACK] - 1
    position = sign(trailing_return):  +1 (long) when > 0,  -1 (short) when < 0.
A positive trailing return predicts a positive forward return — the documented
TSMOM effect on the 1–12 month horizon across asset classes. `allow_short=False`
makes the < 0 case flat instead of short. Default LOOKBACK = 120 D1 bars ≈ 6 months
(config `MOM_LOOKBACK`): a genuinely long horizon for the timeframe, where the edge
is documented (on H1 the same bar count is only ~5 days, which is one reason a fast
crossover on H1 is mostly noise).

TREND-CONFIRMATION FILTER (ON by default, `use_anchor`):
Take a momentum signal only when price is on the correct side of a long EMA anchor
(length ANCHOR, default 200) — long only if close > EMA, short only if close < EMA;
otherwise flat. This suppresses momentum signals that fight the long-term trend
(chop), the usual failure mode of a raw sign-of-return rule.

VOLATILITY FILTER (OFF by default, `vol_filter`):
When on, skip NEW entries whose recent realized volatility sits above an extreme
TRAILING percentile (entry filter ONLY — it never resizes a position; vol-targeted
sizing is a deliberately separate, later enhancement). Continuations and exits are
never blocked. Default OFF to keep the primary hypothesis test clean (fewer
researcher degrees of freedom); the documented defaults are vol_lookback=20,
vol_window=252, vol_max_pct=0.90 (config `MOM_VOL_*`).

NO LOOK-AHEAD: every component (trailing return, recursive EMA, trailing realized
vol + its rolling percentile) uses only close[0..t], so regime[t] is fixed the
instant bar t closes. The engine then fills at the NEXT bar's open. The unit tests
include a truncation-invariance check proving regime[:t] never depends on the future.

(`pandas-ta`/`pandas` would compute the same EMA/returns/rolling-quantile; numpy is
exact here. See requirements.txt for the canonical libs.)
"""
from __future__ import annotations

import numpy as np

from config import STRATEGY
from .base import Strategy, Signals


def ema(values, span: int) -> np.ndarray:
    """Exponential moving average, alpha = 2/(span+1), seeded with the first value.
    Causal: ema[t] depends only on values[0..t]. (canonical: pandas
    `Series.ewm(span=span, adjust=False).mean()`.)"""
    values = np.asarray(values, dtype=float)
    n = values.shape[0]
    out = np.empty(n)
    if n == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    e = values[0]
    out[0] = e
    for i in range(1, n):
        e = alpha * values[i] + (1.0 - alpha) * e
        out[i] = e
    return out


def trailing_return(close, lookback: int) -> np.ndarray:
    """close[t]/close[t-lookback] - 1; NaN for the first `lookback` bars. Causal."""
    close = np.asarray(close, dtype=float)
    n = close.shape[0]
    out = np.full(n, np.nan)
    if 0 < lookback < n:
        out[lookback:] = close[lookback:] / close[:-lookback] - 1.0
    return out


def realized_vol(close, lookback: int) -> np.ndarray:
    """Trailing realized volatility = std of the last `lookback` log-returns. rv[t]
    uses only returns up to t (causal); NaN until `lookback` returns exist."""
    close = np.asarray(close, dtype=float)
    n = close.shape[0]
    rv = np.full(n, np.nan)
    if n < 2 or lookback < 2:
        return rv
    rets = np.full(n, np.nan)
    rets[1:] = np.log(close[1:] / close[:-1])
    for t in range(lookback, n):
        rv[t] = np.std(rets[t - lookback + 1:t + 1])
    return rv


def _entries_from_regime(regime: np.ndarray) -> np.ndarray:
    """+1/-1 on the bar a new non-zero regime begins, else 0 (same convention as
    sma_crossover, so the engine sees identical event semantics)."""
    prev = np.roll(regime, 1)
    prev[0] = 0
    flipped = regime != prev
    entries = np.zeros(regime.shape, dtype=int)
    entries[flipped & (regime == 1)] = 1
    entries[flipped & (regime == -1)] = -1
    return entries


class TsMomentum(Strategy):
    name = "ts_momentum"

    def default_params(self) -> dict:
        return {"lookback": STRATEGY.mom_lookback, "anchor": STRATEGY.mom_anchor,
                "allow_short": STRATEGY.mom_allow_short,
                "use_anchor": STRATEGY.mom_use_anchor,
                "vol_filter": STRATEGY.mom_vol_filter,
                "vol_lookback": STRATEGY.mom_vol_lookback,
                "vol_window": STRATEGY.mom_vol_window,
                "vol_max_pct": STRATEGY.mom_vol_max_pct}

    def param_grid(self) -> dict:
        # 2-param robustness surface: TSMOM horizon × trend-anchor length. Lookbacks
        # span ~1 month (20) to ~12 months (252) of D1 bars — the documented TSMOM
        # range; anchors span 50–300. (robustness.py requires exactly 2 grid keys.)
        return {"lookback": (20, 40, 60, 90, 120, 150, 180, 210, 252),
                "anchor": (50, 100, 150, 200, 250, 300)}

    def wf_grid(self) -> dict:
        # Smaller grid searched per walk-forward IS window — limits per-fold multiple
        # testing & runtime, same spirit as SMA's separate (smaller) WALKFORWARD grid.
        return {"lookback": (20, 60, 120, 200, 252),
                "anchor": (100, 200, 300)}

    def validate_params(self, lookback, anchor, **_) -> bool:
        # lookback and anchor are independent dimensions (horizon vs trend filter);
        # the only constraint is that both are usable window lengths.
        return int(lookback) >= 2 and int(anchor) >= 2

    def warmup_bars(self, **params) -> int:
        lb = int(params.get("lookback", STRATEGY.mom_lookback))
        an = int(params.get("anchor", STRATEGY.mom_anchor))
        use_anchor = params.get("use_anchor", STRATEGY.mom_use_anchor)
        return max(lb, an) if use_anchor else lb

    def generate(self, close, lookback=None, anchor=None, allow_short=True,
                 use_anchor=True, vol_filter=False, vol_lookback=20,
                 vol_window=252, vol_max_pct=0.90) -> Signals:
        close = np.asarray(close, dtype=float)
        n = close.shape[0]
        lookback = int(STRATEGY.mom_lookback if lookback is None else lookback)
        anchor = int(STRATEGY.mom_anchor if anchor is None else anchor)
        warmup = max(lookback, anchor) if use_anchor else lookback

        regime = np.zeros(n, dtype=int)
        if n <= warmup:
            return Signals(regime=regime, entries=np.zeros(n, dtype=int))

        # --- core TSMOM: sign of the trailing lookback-period return ---
        tr = trailing_return(close, lookback)
        raw = np.zeros(n, dtype=int)
        raw[tr > 0] = 1
        raw[tr < 0] = -1 if allow_short else 0

        # --- trend-confirmation filter (on by default): cancel signals that fight
        # the long-term trend (price on the wrong side of the anchor EMA) ---
        if use_anchor:
            anchor_ema = ema(close, anchor)
            raw[(raw == 1) & ~(close > anchor_ema)] = 0   # longs need price above
            raw[(raw == -1) & ~(close < anchor_ema)] = 0  # shorts need price below

        raw[:warmup] = 0          # no signal until both windows are fully formed

        # --- optional volatility entry filter (off by default): block NEW entries /
        # reversals while trailing realized vol is in an extreme trailing percentile;
        # never blocks holds or exits, never resizes (entry filter only) ---
        if vol_filter:
            rv = realized_vol(close, int(vol_lookback))
            vw = int(vol_window)
            entry_ok = np.zeros(n, dtype=bool)
            for t in range(n):
                if np.isnan(rv[t]):
                    continue                       # not enough data → block (conservative)
                lo = max(0, t - vw + 1)
                window = rv[lo:t + 1]
                window = window[~np.isnan(window)]
                entry_ok[t] = window.size < 2 or rv[t] <= np.quantile(window, vol_max_pct)
            filtered = np.zeros(n, dtype=int)
            cur = 0
            for t in range(n):
                target = int(raw[t])
                if target == 0 or target == cur or entry_ok[t]:
                    cur = target                   # exit / continue / allowed entry
                filtered[t] = cur                  # else: block new entry, hold current
            raw = filtered

        regime = raw
        return Signals(regime=regime, entries=_entries_from_regime(regime))
