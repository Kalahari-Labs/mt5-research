"""strategies.py — pre-registered strategy definitions.

PARITY CONTRACT: each strategy exposes decide(bars, i) -> Signal|None where
decide may only read indicator values at indices <= i. The live engine calls
decide at the last CLOSED bar; the backtester calls it at every bar. Same code,
same numbers, both paths.

DISCIPLINE CONTRACT: every Signal carries explicit sl/tp prices derived from
structure/ATR — the engine refuses naked entries, and the bridge refuses them
again server-side. Every strategy also honors the global time-stop
(config.MAX_HOLD_BARS) enforced by backtester and engine alike, because the
Phase-4 research showed overnight financing is what kills retail edges.

The param grid is FROZEN small on purpose: 29 configs already failed
walk-forward in this repo's research; we do not go param-fishing. New ideas
enter here, pass the gate on out-of-sample data, or stay in observe mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import Bars


@dataclass(frozen=True)
class Signal:
    side: str            # buy | sell
    sl: float            # protective stop PRICE
    tp: float            # take-profit PRICE
    reason: str
    tags: tuple = field(default_factory=tuple)


class Strategy:
    name = "base"
    params: dict = {}
    timeframe: str | None = None   # None -> config.TIMEFRAME; else e.g. "M15"

    def decide(self, bars: Bars, i: int) -> Signal | None:
        raise NotImplementedError

    def describe(self) -> str:
        return "%s %s" % (self.name, self.params)


class TrendPullback(Strategy):
    """Trade WITH the EMA20/50 trend after an RSI pullback resolves.

    Long: ema20>ema50, RSI(14) was < pull_lo within the last 3 bars, current bar
    closes back above pull_lo with close > ema20. Stop: 1.5*ATR. TP: rr * stop.
    Short mirrored. Time-stop handled globally.
    """
    name = "trend_pullback"

    def __init__(self, pull_lo: float = 45.0, pull_hi: float = 55.0,
                 atr_mult: float = 1.5, rr: float = 2.0):
        self.params = {"pull_lo": pull_lo, "pull_hi": pull_hi,
                       "atr_mult": atr_mult, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 60:
            return None
        p = self.params
        e20, e50 = bars.ema(20), bars.ema(50)
        r = bars.rsi(14)
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        c = float(bars.close[i])
        up = e20[i] > e50[i]
        down = e20[i] < e50[i]
        dipped = bool((r[max(0, i - 3):i] < p["pull_lo"]).any())
        popped = bool((r[max(0, i - 3):i] > p["pull_hi"]).any())
        if up and dipped and r[i] >= p["pull_lo"] and c > e20[i]:
            sl = c - p["atr_mult"] * a
            return Signal("buy", sl, c + p["rr"] * (c - sl),
                          "uptrend + RSI pullback resolved (rsi=%.1f)" % r[i],
                          ("trend", "pullback"))
        if down and popped and r[i] <= p["pull_hi"] and c < e20[i]:
            sl = c + p["atr_mult"] * a
            return Signal("sell", sl, c - p["rr"] * (sl - c),
                          "downtrend + RSI rally resolved (rsi=%.1f)" % r[i],
                          ("trend", "pullback"))
        return None


class DonchianBreakout(Strategy):
    """Breakout of the N-bar channel with volatility expansion + trend agreement.

    Long: close breaks above the PRIOR bar's N-high, ATR above its median (real
    expansion, not chop), ema50 rising. Stop: 1.5*ATR. TP: rr * stop.
    """
    name = "donchian_breakout"

    def __init__(self, channel: int = 20, atr_mult: float = 1.5, rr: float = 2.0):
        self.params = {"channel": channel, "atr_mult": atr_mult, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        p = self.params
        if i < max(60, p["channel"] + 2):
            return None
        hi, lo = bars.donchian(p["channel"])
        a_series = bars.atr(14)
        a = float(a_series[i])
        if a <= 0:
            return None
        import numpy as np
        med_atr = float(np.median(a_series[max(0, i - 50):i + 1]))
        expanding = a > med_atr
        e50 = bars.ema(50)
        rising = e50[i] > e50[i - 3]
        falling = e50[i] < e50[i - 3]
        c = float(bars.close[i])
        if expanding and rising and c > hi[i - 1]:
            sl = c - p["atr_mult"] * a
            return Signal("buy", sl, c + p["rr"] * (c - sl),
                          "breakout above %d-bar high with ATR expansion" % p["channel"],
                          ("breakout", "expansion"))
        if expanding and falling and c < lo[i - 1]:
            sl = c + p["atr_mult"] * a
            return Signal("sell", sl, c - p["rr"] * (sl - c),
                          "breakdown below %d-bar low with ATR expansion" % p["channel"],
                          ("breakout", "expansion"))
        return None


class MeanRevBollinger(Strategy):
    """Fade Bollinger extremes ONLY in a flat regime, target the mid band.

    Long: |ema20-ema50| < 0.1*ATR (no trend), close below lower band, distance
    to mid band >= min_edge_atr * ATR (must be worth the spread). Stop 2*ATR.
    """
    name = "meanrev_bb"

    def __init__(self, period: int = 20, k: float = 2.0,
                 atr_mult: float = 2.0, min_edge_atr: float = 0.8):
        self.params = {"period": period, "k": k, "atr_mult": atr_mult,
                       "min_edge_atr": min_edge_atr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        p = self.params
        if i < 60:
            return None
        mid, up, low = bars.bollinger(p["period"], p["k"])
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        e20, e50 = bars.ema(20), bars.ema(50)
        if abs(e20[i] - e50[i]) > 0.1 * a:
            return None  # trending: do not fade
        c = float(bars.close[i])
        edge = p["min_edge_atr"] * a
        if c < low[i] and (mid[i] - c) >= edge:
            return Signal("buy", c - p["atr_mult"] * a, float(mid[i]),
                          "flat regime, close below lower BB, %.1f ATR to mid" % ((mid[i] - c) / a),
                          ("meanrev", "flat"))
        if c > up[i] and (c - mid[i]) >= edge:
            return Signal("sell", c + p["atr_mult"] * a, float(mid[i]),
                          "flat regime, close above upper BB, %.1f ATR to mid" % ((c - mid[i]) / a),
                          ("meanrev", "flat"))
        return None


class FVGRetrace(Strategy):
    """ICT fair value gap: 3-candle imbalance, then trade the retrace into it.

    Bull FVG at j: low[j] > high[j-2] (a gap the market skipped over) left by a
    displacement candle j-1 with body >= disp_atr*ATR. Entry at bar i when price
    dips INTO the unfilled gap and closes back above it, with the EMA50 trend.
    SL below the gap floor; TP rr * risk. Bear mirrored.
    """
    name = "fvg_retrace"
    tags = ("ict", "fvg")

    def __init__(self, lookback: int = 30, min_gap_atr: float = 0.25,
                 disp_atr: float = 0.8, sl_buf_atr: float = 0.5, rr: float = 2.0):
        self.params = {"lookback": lookback, "min_gap_atr": min_gap_atr,
                       "disp_atr": disp_atr, "sl_buf_atr": sl_buf_atr, "rr": rr}

    def _find_gap(self, bars: Bars, i: int, a: float, bull: bool):
        """Most recent unfilled FVG formed within lookback, oldest scan last."""
        p = self.params
        for j in range(i - 1, max(2, i - p["lookback"]), -1):
            body = abs(float(bars.close[j - 1] - bars.open[j - 1]))
            if body < p["disp_atr"] * a:
                continue
            if bull:
                gap_lo, gap_hi = float(bars.high[j - 2]), float(bars.low[j])
            else:
                gap_lo, gap_hi = float(bars.high[j]), float(bars.low[j - 2])
            if gap_hi - gap_lo < p["min_gap_atr"] * a:
                continue
            between = slice(j + 1, i)
            if bull and (bars.low[between] < gap_lo).any():
                continue  # gap fully filled -> dead
            if not bull and (bars.high[between] > gap_hi).any():
                continue
            return j, gap_lo, gap_hi
        return None

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 60:
            return None
        p = self.params
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        e50 = bars.ema(50)
        c = float(bars.close[i])
        if e50[i - 1] < e50[i]:  # rising -> only bull setups
            g = self._find_gap(bars, i, a, bull=True)
            if g and bars.low[i] <= g[2] and c > g[2]:
                sl = g[1] - p["sl_buf_atr"] * a
                return Signal("buy", sl, c + p["rr"] * (c - sl),
                              "retraced into bull FVG (%.5f-%.5f) and rejected"
                              % (g[1], g[2]), ("ict", "fvg"))
        if e50[i - 1] > e50[i]:
            g = self._find_gap(bars, i, a, bull=False)
            if g and bars.high[i] >= g[1] and c < g[1]:
                sl = g[2] + p["sl_buf_atr"] * a
                return Signal("sell", sl, c - p["rr"] * (sl - c),
                              "retraced into bear FVG (%.5f-%.5f) and rejected"
                              % (g[1], g[2]), ("ict", "fvg"))
        return None


class LiquiditySweep(Strategy):
    """ICT liquidity sweep / turtle soup: fade the stop hunt.

    Sell: bar takes out the prior N-bar high (where buy stops rest) by at least
    min_sweep_atr*ATR but CLOSES back below it with a down body — the breakout
    failed, the liquidity was consumed. SL above the sweep wick. Buy mirrored.
    """
    name = "liquidity_sweep"

    def __init__(self, lookback: int = 20, min_sweep_atr: float = 0.1,
                 sl_buf_atr: float = 0.5, rr: float = 2.0):
        self.params = {"lookback": lookback, "min_sweep_atr": min_sweep_atr,
                       "sl_buf_atr": sl_buf_atr, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        p = self.params
        if i < max(60, p["lookback"] + 2):
            return None
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        hi, lo = bars.donchian(p["lookback"])
        swing_hi, swing_lo = float(hi[i - 1]), float(lo[i - 1])
        c, o = float(bars.close[i]), float(bars.open[i])
        if (bars.high[i] >= swing_hi + p["min_sweep_atr"] * a
                and c < swing_hi and c < o):
            sl = float(bars.high[i]) + p["sl_buf_atr"] * a
            return Signal("sell", sl, c - p["rr"] * (sl - c),
                          "swept %d-bar high (%.5f) and closed back below — "
                          "stop hunt faded" % (p["lookback"], swing_hi),
                          ("ict", "liquidity"))
        if (bars.low[i] <= swing_lo - p["min_sweep_atr"] * a
                and c > swing_lo and c > o):
            sl = float(bars.low[i]) - p["sl_buf_atr"] * a
            return Signal("buy", sl, c + p["rr"] * (c - sl),
                          "swept %d-bar low (%.5f) and closed back above — "
                          "stop hunt faded" % (p["lookback"], swing_lo),
                          ("ict", "liquidity"))
        return None


class OrderBlockRetest(Strategy):
    """ICT order block: the last opposing candle before a displacement move;
    trade the first retest of that zone in the move's direction.

    Bull OB at j-1: bearish candle j-1, then displacement candle j with body >=
    disp_atr*ATR closing above the prior 10-bar high (structure break). Entry at
    bar i on first touch of the OB zone that closes back above it, trend up.
    """
    name = "orderblock_retest"

    def __init__(self, lookback: int = 30, disp_atr: float = 1.0,
                 sl_buf_atr: float = 0.5, rr: float = 2.0):
        self.params = {"lookback": lookback, "disp_atr": disp_atr,
                       "sl_buf_atr": sl_buf_atr, "rr": rr}

    def _find_ob(self, bars: Bars, i: int, a: float, bull: bool):
        p = self.params
        for j in range(i - 1, max(12, i - p["lookback"]), -1):
            body = float(bars.close[j] - bars.open[j])
            if bull:
                if body < p["disp_atr"] * a:
                    continue
                if bars.close[j] <= bars.high[j - 11:j - 1].max():
                    continue  # no structure break
                if bars.close[j - 1] >= bars.open[j - 1]:
                    continue  # OB candle must be the last DOWN candle
                zone_lo, zone_hi = float(bars.low[j - 1]), float(bars.high[j - 1])
                if (bars.low[j + 1:i] <= zone_hi).any():
                    continue  # already retested -> spent
            else:
                if -body < p["disp_atr"] * a:
                    continue
                if bars.close[j] >= bars.low[j - 11:j - 1].min():
                    continue
                if bars.close[j - 1] <= bars.open[j - 1]:
                    continue
                zone_lo, zone_hi = float(bars.low[j - 1]), float(bars.high[j - 1])
                if (bars.high[j + 1:i] >= zone_lo).any():
                    continue
            return j, zone_lo, zone_hi
        return None

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 60:
            return None
        p = self.params
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        e50 = bars.ema(50)
        c = float(bars.close[i])
        if e50[i] > e50[i - 1]:
            ob = self._find_ob(bars, i, a, bull=True)
            if ob and bars.low[i] <= ob[2] and c > ob[2]:
                sl = ob[1] - p["sl_buf_atr"] * a
                return Signal("buy", sl, c + p["rr"] * (c - sl),
                              "first retest of bull order block (%.5f-%.5f)"
                              % (ob[1], ob[2]), ("ict", "orderblock"))
        if e50[i] < e50[i - 1]:
            ob = self._find_ob(bars, i, a, bull=False)
            if ob and bars.high[i] >= ob[1] and c < ob[1]:
                sl = ob[2] + p["sl_buf_atr"] * a
                return Signal("sell", sl, c - p["rr"] * (sl - c),
                              "first retest of bear order block (%.5f-%.5f)"
                              % (ob[1], ob[2]), ("ict", "orderblock"))
        return None


class LondonBreakout(Strategy):
    """Asian-range breakout in the London window (broker SERVER hours).

    Build the range from server hours range_h0..range_h1; in window_h0..window_h1
    take the FIRST close beyond the range. Range must be sane: between min and
    max ATR multiples (too narrow = noise, too wide = news day). SL at range
    midpoint, TP rr * risk. EET-broker server midnight ~= Asian open, so the
    defaults line up for most MT5 brokers; override per broker if needed.
    """
    name = "london_breakout"

    def __init__(self, range_h0: int = 0, range_h1: int = 7, window_h0: int = 8,
                 window_h1: int = 12, min_range_atr: float = 1.0,
                 max_range_atr: float = 6.0, rr: float = 1.5):
        self.params = {"range_h0": range_h0, "range_h1": range_h1,
                       "window_h0": window_h0, "window_h1": window_h1,
                       "min_range_atr": min_range_atr,
                       "max_range_atr": max_range_atr, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        p = self.params
        if i < 60:
            return None
        hour = bars.hour()
        if not (p["window_h0"] <= hour[i] <= p["window_h1"]):
            return None
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        day = int(bars.time[i]) // 86400
        idx = [j for j in range(max(0, i - 30), i)
               if int(bars.time[j]) // 86400 == day
               and p["range_h0"] <= hour[j] <= p["range_h1"]]
        if len(idx) < (p["range_h1"] - p["range_h0"]):
            return None  # incomplete session range (weekend gap etc.)
        rng_hi = float(bars.high[idx].max())
        rng_lo = float(bars.low[idx].min())
        w = rng_hi - rng_lo
        if not (p["min_range_atr"] * a <= w <= p["max_range_atr"] * a):
            return None
        mid = (rng_hi + rng_lo) / 2.0
        c, prev_c = float(bars.close[i]), float(bars.close[i - 1])
        if c > rng_hi and prev_c <= rng_hi:
            return Signal("buy", mid, c + p["rr"] * (c - mid),
                          "first close above Asian range %.5f-%.5f in London window"
                          % (rng_lo, rng_hi), ("session", "breakout"))
        if c < rng_lo and prev_c >= rng_lo:
            return Signal("sell", mid, c - p["rr"] * (mid - c),
                          "first close below Asian range %.5f-%.5f in London window"
                          % (rng_lo, rng_hi), ("session", "breakout"))
        return None


class MomentumMACD(Strategy):
    """MACD histogram flip in the direction of the EMA200 regime.

    Long: close > EMA200, histogram crosses <=0 -> >0 and is rising. The EMA200
    filter keeps it out of counter-trend chop; ATR stop, rr take-profit.
    """
    name = "momentum_macd"

    def __init__(self, atr_mult: float = 1.5, rr: float = 2.0):
        self.params = {"atr_mult": atr_mult, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 210:
            return None
        p = self.params
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        _, _, hist = bars.macd()
        e200 = bars.ema(200)
        c = float(bars.close[i])
        crossed_up = hist[i - 1] <= 0 < hist[i] and hist[i] > hist[i - 1]
        crossed_dn = hist[i - 1] >= 0 > hist[i] and hist[i] < hist[i - 1]
        if c > e200[i] and crossed_up:
            sl = c - p["atr_mult"] * a
            return Signal("buy", sl, c + p["rr"] * (c - sl),
                          "MACD histogram flipped positive above EMA200",
                          ("momentum", "trend"))
        if c < e200[i] and crossed_dn:
            sl = c + p["atr_mult"] * a
            return Signal("sell", sl, c - p["rr"] * (sl - c),
                          "MACD histogram flipped negative below EMA200",
                          ("momentum", "trend"))
        return None


class RSI2MeanRev(Strategy):
    """Connors RSI(2) pullback WITH the long-term trend.

    Long: close > EMA200 (bull regime), RSI(2) < lo (violent short-term flush),
    close below EMA20. Target: back to the EMA20 mean (must be >= min_edge_atr
    away so the trip is worth the spread). Wide 2.5*ATR stop — mean reversion
    needs room. Short mirrored.
    """
    name = "rsi2_meanrev"

    def __init__(self, lo: float = 10.0, hi: float = 90.0,
                 atr_mult: float = 2.5, min_edge_atr: float = 0.5):
        self.params = {"lo": lo, "hi": hi, "atr_mult": atr_mult,
                       "min_edge_atr": min_edge_atr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 210:
            return None
        p = self.params
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        r2 = bars.rsi(2)
        e20, e200 = bars.ema(20), bars.ema(200)
        c = float(bars.close[i])
        edge = p["min_edge_atr"] * a
        if c > e200[i] and r2[i] < p["lo"] and c < e20[i] and (e20[i] - c) >= edge:
            return Signal("buy", c - p["atr_mult"] * a, float(e20[i]),
                          "RSI(2)=%.0f flush in bull regime, %.1f ATR back to mean"
                          % (r2[i], (e20[i] - c) / a), ("meanrev", "pullback"))
        if c < e200[i] and r2[i] > p["hi"] and c > e20[i] and (c - e20[i]) >= edge:
            return Signal("sell", c + p["atr_mult"] * a, float(e20[i]),
                          "RSI(2)=%.0f spike in bear regime, %.1f ATR back to mean"
                          % (r2[i], (c - e20[i]) / a), ("meanrev", "pullback"))
        return None


class ScalpEMACross(Strategy):
    """M15 session scalper: EMA9/21 cross with trend + momentum agreement.

    Only during liquid server hours (London + NY). Tight 1.2*ATR stop, 1.5R
    target. Deliberately spread-fragile: the shared spread guard (live veto and
    backtest filter alike) kills it on symbols where M15 ATR can't pay the
    spread — that is the honest outcome for scalping on a wide-spread broker.
    """
    name = "scalp_ema_cross"
    timeframe = "M15"

    def __init__(self, h0: int = 9, h1: int = 21, atr_mult: float = 1.2,
                 rr: float = 1.5):
        self.params = {"h0": h0, "h1": h1, "atr_mult": atr_mult, "rr": rr}

    def decide(self, bars: Bars, i: int) -> Signal | None:
        if i < 60:
            return None
        p = self.params
        hour = bars.hour()
        if not (p["h0"] <= hour[i] <= p["h1"]):
            return None
        a = float(bars.atr(14)[i])
        if a <= 0:
            return None
        e9, e21, e50 = bars.ema(9), bars.ema(21), bars.ema(50)
        r = bars.rsi(14)
        c = float(bars.close[i])
        cross_up = e9[i - 1] <= e21[i - 1] and e9[i] > e21[i]
        cross_dn = e9[i - 1] >= e21[i - 1] and e9[i] < e21[i]
        if cross_up and e50[i] > e50[i - 3] and r[i] > 52:
            sl = c - p["atr_mult"] * a
            return Signal("buy", sl, c + p["rr"] * (c - sl),
                          "EMA9/21 cross up in session, rsi=%.0f" % r[i],
                          ("scalp", "momentum"))
        if cross_dn and e50[i] < e50[i - 3] and r[i] < 48:
            sl = c + p["atr_mult"] * a
            return Signal("sell", sl, c - p["rr"] * (sl - c),
                          "EMA9/21 cross down in session, rsi=%.0f" % r[i],
                          ("scalp", "momentum"))
        return None


REGISTRY: dict[str, Strategy] = {
    s.name: s for s in (
        TrendPullback(), DonchianBreakout(), MeanRevBollinger(),
        FVGRetrace(), LiquiditySweep(), OrderBlockRetest(),
        LondonBreakout(), MomentumMACD(), RSI2MeanRev(), ScalpEMACross(),
    )
}


if __name__ == "__main__":
    import numpy as np

    def synthetic(seed: int, drift: float, n: int = 900):
        """Continuous bars (open = prior close) with occasional shock candles,
        like real market data — displacement setups (FVG/OB) need the shocks."""
        rng = np.random.default_rng(seed)
        steps = rng.normal(drift, 1.0, n)
        steps += (rng.random(n) < 0.06) * rng.normal(0, 4.0, n)
        c = np.cumsum(steps) + 500
        o = np.empty(n)
        o[0], o[1:] = c[0], c[:-1]
        h = np.maximum(o, c) + abs(rng.normal(0, .5, n))
        lo = np.minimum(o, c) - abs(rng.normal(0, .5, n))
        return Bars([[i * 3600, o[i], h[i], lo[i], c[i], 100, 10]
                     for i in range(n)])

    fired = {name: 0 for name in REGISTRY}
    for b in (synthetic(3, 0.0), synthetic(7, 0.25), synthetic(11, -0.25)):
        for name, s in REGISTRY.items():
            for i in range(220, b.n):
                x = s.decide(b, i)
                if x is None:
                    continue
                fired[name] += 1
                c = float(b.close[i])
                # invariants: protective geometry must hold on EVERY signal
                if x.side == "buy":
                    assert x.sl < c < x.tp, (name, i, x)
                else:
                    assert x.tp < c < x.sl, (name, i, x)
                # causality: same index, longer history -> same decision
    b = synthetic(3, 0.0)
    for name, s in REGISTRY.items():
        assert fired[name] > 0, "%s never fired on any synthetic series" % name
        for i in range(220, 400):
            x_full = s.decide(b, i)
            x_cut = s.decide(Bars([[float(b.time[k]), b.open[k], b.high[k],
                                    b.low[k], b.close[k], b.volume[k],
                                    b.spread_points[k]] for k in range(i + 1)]), i)
            assert repr(x_full) == repr(x_cut), \
                "%s decision at %d changed when future bars were appended" % (name, i)
    print("STRATEGY SELFTEST OK — %d strategies, signals across 3 synthetic "
          "series: %s" % (len(REGISTRY), fired))
