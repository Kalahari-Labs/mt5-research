"""risk.py — the veto layer. Every entry passes ALL of these or is journaled
as a skip with the exact reason. Freqtrade-style protections, adapted:

  kill switch      touch executor/data/KILL -> flatten everything, halt engine
  daily loss halt  realized+floating loss for the day >= MAX_DAILY_LOSS_PCT
  max drawdown     equity below peak by MAX_DRAWDOWN_PCT -> halt until human
  stoploss guard   N consecutive losses on a combo -> cooldown COOLDOWN_HOURS
  weekly guard     DISABLE_AFTER_LOSSES_7D losses in 7 days -> disabled until
                   the gate re-passes it
  news blackout    +/- NEWS_BLACKOUT_MIN around high-impact events (real
                   ForexFactory calendar, never guessed)
  spread guard     live spread > MAX_SPREAD_ATR_FRAC of ATR -> skip
  exposure caps    MAX_OPEN_POSITIONS global, 1 position per combo
  market closed    no tick movement -> no entries

Position sizing: fixed-fractional. lots = equity*risk% / (stop_distance *
unit_value), clamped to broker volume constraints, hard-capped at MAX_VOLUME.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config, news_calendar
from .backtester import SymbolSpec
from .store import Store


class Veto(Exception):
    """Raised with the human-readable reason an entry was refused."""


def kill_switch_active() -> bool:
    return config.KILL_SWITCH.exists()


def size_position(equity: float, entry: float, sl: float, spec: SymbolSpec) -> float:
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        raise Veto("zero stop distance")
    risk_amount = equity * config.RISK_PER_TRADE_PCT / 100.0
    lots = spec.round_volume(risk_amount / (stop_dist * spec.unit_value))
    lots = min(lots, config.MAX_VOLUME)
    real_risk = stop_dist * spec.unit_value * lots
    if real_risk > 2.0 * risk_amount:
        raise Veto("minimum lot %.2f would risk %.2f (>2x budget %.2f)"
                   % (lots, real_risk, risk_amount))
    return lots


def check_halts(store: Store, account: dict) -> None:
    """Account-level halts. Raise Veto to block ALL entries."""
    if kill_switch_active():
        raise Veto("KILL SWITCH file present (%s)" % config.KILL_SWITCH)
    manual = store.get_state("manual_halt")
    if manual:
        raise Veto("manual halt: %s" % manual)
    equity = float(account["equity"])

    # daily loss: compare with equity at day open (first snapshot today)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_open = store.day_open_equity(day)
    if day_open is not None:
        loss_pct = (day_open - equity) / day_open * 100.0
        if loss_pct >= config.MAX_DAILY_LOSS_PCT:
            store.set_state("halted_for_day", day)
            raise Veto("daily loss %.2f%% >= %.1f%% — halted until tomorrow"
                       % (loss_pct, config.MAX_DAILY_LOSS_PCT))
    if store.get_state("halted_for_day") == day:
        raise Veto("daily loss halt already triggered today")

    # max drawdown from tracked peak
    peak = store.get_state("equity_peak", equity)
    if equity > peak:
        store.set_state("equity_peak", equity)
        peak = equity
    dd_pct = (peak - equity) / peak * 100.0
    if dd_pct >= config.MAX_DRAWDOWN_PCT:
        store.set_state("manual_halt",
                        "max drawdown %.1f%% hit at %s — requires human reset "
                        "(delete engine_state key 'manual_halt')"
                        % (dd_pct, datetime.now(timezone.utc).isoformat()))
        raise Veto("max drawdown %.2f%% >= %.1f%%" % (dd_pct, config.MAX_DRAWDOWN_PCT))


def check_entry(store: Store, symbol: str, strategy: str,
                open_positions: list[dict], live_spread: float, atr: float,
                tick_fresh: bool) -> None:
    """Per-entry vetoes. Raise Veto with the reason to skip."""
    if not tick_fresh:
        raise Veto("market closed or tick stale")
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        raise Veto("max open positions (%d) reached" % config.MAX_OPEN_POSITIONS)
    if any(p["symbol"] == symbol for p in open_positions):
        raise Veto("already holding %s" % symbol)

    # cooldown / disable state for this combo (set by review.py)
    cd = store.get_state("cooldown:%s:%s" % (strategy, symbol))
    if cd:
        until = datetime.fromisoformat(cd)
        if datetime.now(timezone.utc) < until:
            raise Veto("cooldown until %s after consecutive losses" % cd)

    status_row = store.strategy_status(strategy, symbol)
    if not status_row or status_row["status"] != "enabled":
        st = status_row["status"] if status_row else "unknown"
        raise Veto("combo is %s (gate: %s)" % (
            st, status_row["reason"] if status_row else "never evaluated"))

    if atr > 0 and live_spread > config.MAX_SPREAD_ATR_FRAC * atr:
        raise Veto("spread %.5f > %.0f%% of ATR %.5f"
                   % (live_spread, config.MAX_SPREAD_ATR_FRAC * 100, atr))

    ev = news_calendar.blackout(store, symbol)
    if ev:
        raise Veto("news blackout: %s" % ev)


if __name__ == "__main__":
    # sizing self-test with real-shaped EURUSD spec
    spec = SymbolSpec("EURUSD", point=1e-5, digits=5, tick_value=1.0, tick_size=1e-5,
                      contract_size=1e5, volume_min=0.01, volume_step=0.01,
                      volume_max=50.0, swap_long=-8.02, swap_short=1.28)
    # 10k equity, 0.5% risk = $50; stop 50 pips = 0.0050; unit_value = 100000
    lots = size_position(10_000, 1.1000, 1.0950, spec)
    assert lots == 0.10, lots  # 50 / (0.005*100000) = 0.1
    lots = size_position(10_000, 1.1000, 1.0990, spec)  # 10-pip stop -> 0.5 cap
    assert lots == config.MAX_VOLUME, lots
    try:
        size_position(100, 1.1000, 1.0950, spec)  # min lot over-risks a $100 acct
        raise AssertionError("should have vetoed")
    except Veto as v:
        pass
    s = Store()
    acct = {"equity": 10_000.0}
    try:
        check_halts(s, acct)
        print("halts: none active")
    except Veto as v:
        print("halts active:", v)
    print("RISK SELFTEST OK — sizing + caps verified")
