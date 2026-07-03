"""backtester.py — event-driven backtest with NO optimistic shortcuts.

Fill model (FILL_MODEL.md heritage — every cost is named, none are hidden):
  * signals are computed on CLOSED bar i, filled at bar i+1 OPEN (no look-ahead)
  * bars are bid quotes: buys fill at open+spread, sells at open (minus slippage
    both ways); the spread used is the bar's own RECORDED spread from MT5
  * intrabar SL/TP: if both could hit in one bar, SL fills first (conservative)
  * shorts exit at ask = bid + spread — the spread is paid where it really is
  * swap: broker's live swap points per night, Wednesday counts triple (approx),
    charged in account currency via tick_value/tick_size
  * time-stop (MAX_HOLD_BARS) and Friday-flat are enforced exactly like live

Sizing matches the live engine: fixed-fractional risk on compounding equity,
clamped to broker volume_min/step/max.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import config
from .analysis import Bars
from .strategies import Strategy


@dataclass
class SymbolSpec:
    name: str
    point: float
    digits: int
    tick_value: float        # account ccy per tick per 1.0 lot
    tick_size: float
    contract_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    swap_long: float         # points per night (swap_mode=1)
    swap_short: float

    @classmethod
    def from_bridge(cls, info: dict) -> "SymbolSpec":
        return cls(
            name=info["name"], point=info["point"], digits=info["digits"],
            tick_value=info["trade_tick_value"], tick_size=info["trade_tick_size"],
            contract_size=info["trade_contract_size"],
            volume_min=info["volume_min"], volume_step=info["volume_step"],
            volume_max=info["volume_max"],
            swap_long=info.get("swap_long", 0.0), swap_short=info.get("swap_short", 0.0))

    @property
    def unit_value(self) -> float:
        """Account-currency value of a 1.0 price-unit move per 1.0 lot."""
        return self.tick_value / self.tick_size

    def round_volume(self, lots: float) -> float:
        lots = max(self.volume_min, min(self.volume_max, lots))
        steps = math.floor(lots / self.volume_step + 1e-9)
        return round(max(self.volume_min, steps * self.volume_step), 8)


SLIPPAGE_POINTS = 2.0  # adverse points per fill, on top of recorded spread


def _nights_between(t_entry: int, t_exit: int) -> float:
    """Server-midnight crossings; each Wednesday->Thursday night counts 3x
    (standard FX triple-swap approximation)."""
    d0, d1 = t_entry // 86400, t_exit // 86400
    nights = 0.0
    for d in range(d0, d1):
        # day index d -> weekday of that date (epoch day 0 = Thursday)
        weekday = (d + 4) % 7  # 0=Mon .. 6=Sun
        nights += 3.0 if weekday == 2 else 1.0
    return nights


def run_backtest(bars: Bars, strat: Strategy, spec: SymbolSpec,
                 initial_equity: float = 10_000.0,
                 risk_pct: float = config.RISK_PER_TRADE_PCT,
                 max_hold_bars: int = config.MAX_HOLD_BARS,
                 max_spread_atr_frac: float = config.MAX_SPREAD_ATR_FRAC,
                 commission_per_lot: float = config.COMMISSION_PER_LOT) -> dict:
    equity = initial_equity
    peak = equity
    max_dd_pct = 0.0
    trades: list[dict] = []
    pos = None  # open position dict
    atr = bars.atr(14)
    eq_curve = [equity]

    for i in range(60, bars.n - 1):
        t = int(bars.time[i])
        # ---- manage open position on bar i (bar AFTER entry) -------------------
        if pos is not None and i > pos["entry_i"]:
            spread = bars.spread_points[i] * spec.point
            exit_price, reason = None, None
            if pos["side"] == "buy":
                if bars.low[i] <= pos["sl"]:
                    exit_price, reason = pos["sl"] - SLIPPAGE_POINTS * spec.point, "sl"
                elif bars.high[i] >= pos["tp"]:
                    exit_price, reason = pos["tp"], "tp"
            else:  # short exits at ask = bid + spread
                if bars.high[i] + spread >= pos["sl"]:
                    exit_price, reason = pos["sl"] + SLIPPAGE_POINTS * spec.point, "sl"
                elif bars.low[i] + spread <= pos["tp"]:
                    exit_price, reason = pos["tp"], "tp"
            if exit_price is None and (i - pos["entry_i"]) >= max_hold_bars:
                exit_price = bars.close[i] + (spread if pos["side"] == "sell" else 0.0)
                reason = "time_stop"
            if exit_price is None:
                # Friday flat (server time): close late-Friday bars
                wd = ((t // 86400) + 4) % 7
                hour = (t % 86400) // 3600
                if wd == 4 and hour >= config.FRIDAY_FLAT_HOUR_UTC:
                    exit_price = bars.close[i] + (spread if pos["side"] == "sell" else 0.0)
                    reason = "friday_flat"
            if exit_price is not None:
                sign = 1.0 if pos["side"] == "buy" else -1.0
                gross = sign * (exit_price - pos["entry_price"]) * spec.unit_value * pos["lots"]
                swap_pts = spec.swap_long if pos["side"] == "buy" else spec.swap_short
                nights = _nights_between(pos["entry_t"], t)
                swap = swap_pts * spec.point * spec.unit_value * pos["lots"] * nights
                commission = -2.0 * commission_per_lot * pos["lots"]  # both sides
                pnl = gross + swap + commission
                equity += pnl
                peak = max(peak, equity)
                max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100.0)
                eq_curve.append(equity)
                trades.append({
                    "entry_t": pos["entry_t"], "exit_t": t, "side": pos["side"],
                    "lots": pos["lots"], "entry": pos["entry_price"],
                    "exit": float(exit_price), "pnl": pnl, "swap": swap,
                    "commission": commission,
                    "r": pnl / pos["risk_amount"] if pos["risk_amount"] > 0 else 0.0,
                    "hold_bars": i - pos["entry_i"], "reason": reason,
                    "entry_i": pos["entry_i"],
                })
                pos = None
        if pos is not None:
            continue
        if equity <= initial_equity * 0.5:
            break  # 50% drawdown: a real account would stop; so does the test

        # ---- entries: signal on closed bar i, fill at bar i+1 open --------------
        sig = strat.decide(bars, i)
        if sig is None:
            continue
        spread_next = bars.spread_points[i + 1] * spec.point
        cur_atr = float(atr[i])
        if cur_atr <= 0 or spread_next > max_spread_atr_frac * cur_atr:
            continue  # same spread filter the live engine applies
        slip = SLIPPAGE_POINTS * spec.point
        if sig.side == "buy":
            fill = bars.open[i + 1] + spread_next + slip
        else:
            fill = bars.open[i + 1] - slip
        stop_dist = abs(fill - sig.sl)
        if stop_dist <= 0:
            continue
        risk_amount = equity * risk_pct / 100.0
        lots = spec.round_volume(risk_amount / (stop_dist * spec.unit_value))
        real_risk = stop_dist * spec.unit_value * lots
        if real_risk > 2.0 * risk_amount:
            continue  # min lot would over-risk this account size: skip, like live
        pos = {"side": sig.side, "entry_i": i + 1, "entry_t": int(bars.time[i + 1]),
               "entry_price": float(fill), "sl": sig.sl, "tp": sig.tp,
               "lots": lots, "risk_amount": real_risk}

    return {"trades": trades, "final_equity": equity,
            "initial_equity": initial_equity, "max_dd_pct": round(max_dd_pct, 2),
            "metrics": compute_metrics(trades, initial_equity, equity, max_dd_pct)}


def compute_metrics(trades: list[dict], initial: float, final: float,
                    max_dd_pct: float) -> dict:
    if not trades:
        return {"n": 0, "note": "no trades"}
    pnls = np.array([t["pnl"] for t in trades])
    rs = np.array([t["r"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_w, gross_l = wins.sum(), -losses.sum()
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(float(gross_w / gross_l), 3) if gross_l > 0 else float("inf"),
        "expectancy_r": round(float(rs.mean()), 4),
        "expectancy_usd": round(float(pnls.mean()), 2),
        "total_return_pct": round((final - initial) / initial * 100, 2),
        "max_dd_pct": max_dd_pct,
        "avg_hold_bars": round(float(np.mean([t["hold_bars"] for t in trades])), 1),
        "swap_total": round(float(sum(t["swap"] for t in trades)), 2),
        "commission_total": round(float(sum(t.get("commission", 0.0) for t in trades)), 2),
        "sl_exits": sum(1 for t in trades if t["reason"] == "sl"),
        "tp_exits": sum(1 for t in trades if t["reason"] == "tp"),
        "time_exits": sum(1 for t in trades if t["reason"] in ("time_stop", "friday_flat")),
    }


if __name__ == "__main__":
    # Deterministic scenario: clean uptrend with pullbacks -> trend_pullback
    # must be profitable when costs are tiny; then verify costs reduce pnl.
    from .strategies import TrendPullback
    n = 600
    px, t = [], []
    p = 100.0
    for i in range(n):
        cycle = i % 20
        p += 0.35 if cycle < 14 else -0.70  # 14 up / 6 hard down: RSI dips ~37, net up
        px.append(p)
        t.append(i * 3600)
    raw = [[t[i], px[i], px[i] + 0.15, px[i] - 0.15, px[i], 100, 2] for i in range(n)]
    spec = SymbolSpec("TEST", point=0.01, digits=2, tick_value=1.0, tick_size=0.01,
                      contract_size=100.0, volume_min=0.01, volume_step=0.01,
                      volume_max=50.0, swap_long=0.0, swap_short=0.0)
    res = run_backtest(Bars(raw), TrendPullback(), spec)
    m = res["metrics"]
    assert m["n"] > 5, m
    assert m["total_return_pct"] > 0, m
    # same data, brutal spread -> must strictly reduce return (costs are real)
    raw_wide = [[r[0], r[1], r[2], r[3], r[4], r[5], 40] for r in raw]
    res_wide = run_backtest(Bars(raw_wide), TrendPullback(), spec)
    if res_wide["metrics"]["n"]:
        assert res_wide["metrics"]["total_return_pct"] < m["total_return_pct"]
    # swap must cost money when held overnight with negative points
    spec_swap = SymbolSpec("TEST", 0.01, 2, 1.0, 0.01, 100.0, 0.01, 0.01, 50.0,
                           swap_long=-50.0, swap_short=-50.0)
    res_swap = run_backtest(Bars(raw), TrendPullback(), spec_swap)
    assert res_swap["final_equity"] < res["final_equity"]
    # commission must cost money too, exactly 2 sides per round trip
    res_comm = run_backtest(Bars(raw), TrendPullback(), spec, commission_per_lot=3.5)
    assert res_comm["final_equity"] < res["final_equity"]
    assert res_comm["metrics"]["commission_total"] < 0
    print("BACKTESTER SELFTEST OK", m)
