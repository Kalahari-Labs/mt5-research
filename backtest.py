"""backtest.py — run a registered strategy over real history and print a clean
performance report, with an EXPLICIT, configurable cost model.

The engine (`_simulate`) is a small, transparent, vectorised-signal + event-loop
simulator on numpy (the `backtesting.py` stand-in on this offline box). It is now
STRATEGY-AGNOSTIC: it takes a `strategies.Strategy` instance + a params dict and
consumes only `strategy.generate(close, **params).regime`. Signals are formed on
each bar's CLOSE and the position is changed at the NEXT bar's OPEN — no look-ahead
(`cost.fill_timing="close"` re-introduces the bias for the audit only). All costs
come from a `config.CostModel`; nothing is implicit or zero.

Model: single net position, long/short, fully invested (exposure of equity).
Position sizing is a fixed fraction of equity, NOT risk.py — see FILL_MODEL.md §6.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

import strategies
from config import STRATEGY, BACKTEST, REALISTIC_COSTS, LEGACY_COSTS
from data import load_ohlcv


@dataclass
class BacktestResult:
    total_return_pct: float
    n_trades: int
    win_rate_pct: float
    profit_factor: float
    expectancy_per_trade: float
    expectancy_pct: float
    max_drawdown_pct: float
    sharpe: float
    final_equity: float
    initial_cash: float
    bars: int
    start: str
    end: str
    source: str
    fill_timing: str
    spread_pips: float
    commission_per_lot: float
    slippage_pips: float
    strategy: str
    params: dict
    symbol: str
    timeframe_min: int
    trade_pnls: np.ndarray
    equity_curve: np.ndarray
    # Phase 4b/5 bookkeeping (defaults keep every pre-4b constructor call valid).
    total_swap_cost: float = 0.0       # net financing over the run, account ccy (+ = cost)
    total_commission: float = 0.0      # entry+exit commissions over the run
    holding_bars: np.ndarray = None    # per round-trip: bars held
    holding_days: np.ndarray = None    # per round-trip: calendar days held


def _bars_per_year(timeframe_min: int) -> float:
    # FX trades ~24h x 5 days x 52 weeks. H1 -> ~6240 bars/yr, D1 -> ~260.
    return (60.0 / timeframe_min) * 24 * 5 * 52


def _build_result(trade_pnls, equity, time_slice, initial_cash, timeframe_min,
                  source, cost, strategy, params, symbol, total_swap_cost=0.0,
                  total_commission=0.0, holding_bars=None,
                  holding_days=None) -> BacktestResult:
    n = int(equity.shape[0])
    final_equity = float(equity[-1]) if n else float(initial_cash)
    n_trades = int(trade_pnls.size)
    wins = trade_pnls[trade_pnls > 0]
    losses = trade_pnls[trade_pnls < 0]

    win_rate = (wins.size / n_trades * 100.0) if n_trades else 0.0
    gross_profit = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0
    expectancy = float(trade_pnls.mean()) if n_trades else 0.0

    if n:
        peak = np.maximum.accumulate(equity)
        safe_peak = np.where(peak == 0, np.nan, peak)
        dd = (equity - peak) / safe_peak
        max_dd_pct = float(np.nanmin(dd) * 100.0)
    else:
        max_dd_pct = 0.0

    if n > 1:
        prev = equity[:-1]
        diff = np.diff(equity)
        mask = prev != 0
        rets = np.zeros(diff.shape)
        rets[mask] = diff[mask] / prev[mask]
        sharpe = (float(rets.mean() / rets.std() * np.sqrt(_bars_per_year(timeframe_min)))
                  if rets.std() > 0 else 0.0)
    else:
        sharpe = 0.0

    return BacktestResult(
        total_return_pct=(final_equity / initial_cash - 1.0) * 100.0,
        n_trades=n_trades, win_rate_pct=win_rate, profit_factor=profit_factor,
        expectancy_per_trade=expectancy,
        expectancy_pct=(expectancy / initial_cash * 100.0) if n_trades else 0.0,
        max_drawdown_pct=max_dd_pct, sharpe=sharpe, final_equity=final_equity,
        initial_cash=float(initial_cash), bars=n,
        start=str(time_slice[0]) if n else "", end=str(time_slice[-1]) if n else "",
        source=source, fill_timing=cost.fill_timing, spread_pips=cost.spread_pips,
        commission_per_lot=cost.commission_per_lot, slippage_pips=cost.slippage_pips,
        strategy=strategy, params=dict(params), symbol=symbol,
        timeframe_min=timeframe_min, trade_pnls=trade_pnls, equity_curve=equity,
        total_swap_cost=float(total_swap_cost), total_commission=float(total_commission),
        holding_bars=(np.asarray(holding_bars, dtype=float) if holding_bars is not None
                      else np.empty(0)),
        holding_days=(np.asarray(holding_days, dtype=float) if holding_days is not None
                      else np.empty(0)))


def _simulate(open_, close_, time_arr, strategy, params, initial_cash, exposure,
              allow_short, cost, warmup=0, timeframe_min=60, source="csv",
              symbol="EURUSD") -> BacktestResult:
    """Core event loop. `strategy` is a strategies.Strategy, `params` its kwargs.
    `warmup` bars at the front seed the indicator only (no trades / no equity
    recorded) so a walk-forward OOS window can trade from bar 1 using legitimately
    prior closes — without look-ahead. Costs come entirely from `cost`."""
    o = np.asarray(open_, dtype=float)
    c = np.asarray(close_, dtype=float)
    n = c.shape[0]

    regime = strategy.generate(c, **params).regime.copy()
    if not allow_short:
        regime[regime < 0] = 0

    realized = float(initial_cash)
    position = 0.0          # signed units of the base asset
    entry = 0.0
    entry_i = 0             # absolute bar index the current position was opened on
    entry_commission = 0.0  # commission paid to OPEN the current position
    accrued_swap = 0.0      # overnight financing accrued on the CURRENT open position
    swap_total = 0.0        # run-level net financing (+ = cost; credits subtract)
    comm_total = 0.0        # run-level entry+exit commissions
    trade_pnls = []
    hold_bars, hold_days = [], []
    cs = cost.contract_size
    swap_on = getattr(cost, "swap_rate_annual", 0.0) > 0.0   # gate: pre-Phase-4 runs skip
    swap_dir = getattr(cost, "swap_model", "symmetric") == "directional"  # Phase 4b
    ta = np.asarray(time_arr)
    m = max(n - warmup, 0)
    equity = np.empty(m)

    if swap_dir:
        # Rollover-night multiplier for each bar transition (bar i-1 → bar i): one
        # midnight crossed = the night ending day D is charged 1×, the broker's
        # triple day 3×, Sat/Sun 0× (no rollover — the triple day carries the
        # weekend for T+2 settlement). Weekday from days-since-epoch: 1970-01-01
        # was a Thursday, so (d + 3) % 7 gives Python weekday (Mon=0).
        days_int = ta.astype("datetime64[D]").astype(np.int64)
        trip = int(getattr(cost, "swap_triple_weekday", 2))
        nights_mult = np.zeros(n)
        for i in range(1, n):
            for d in range(days_int[i - 1], days_int[i]):
                wd = (d + 3) % 7
                if wd < 5:
                    nights_mult[i] += 3.0 if wd == trip else 1.0

    # Each round-trip PnL is attributed its entry cost, exit cost AND the overnight
    # financing accrued while it was open, so sum(trade_pnls) == final equity -
    # initial cash and PF/expectancy reconcile exactly with the equity curve (no
    # cost — including swap — leaks into the curve unaccounted).
    def _close(base_px, idx):
        nonlocal realized, position, entry_commission, accrued_swap, comm_total
        close_is_buy = position < 0                          # covering a short = buy
        fill = cost.fill_price(base_px, close_is_buy)
        exit_comm = cost.commission(abs(position) * base_px, abs(position) / cs)
        price_pnl = position * (fill - entry)
        realized += price_pnl - exit_comm
        comm_total += exit_comm
        trade_pnls.append(price_pnl - exit_comm - entry_commission - accrued_swap)
        hold_bars.append(idx - entry_i)
        hold_days.append(float((ta[idx] - ta[entry_i]) / np.timedelta64(1, "D")))
        position = 0.0
        entry_commission = 0.0
        accrued_swap = 0.0

    for k in range(m):
        i = warmup + k
        # Overnight financing for carrying the position across the night just ended
        # (bar i-1 → bar i), charged BEFORE today's decision. Directional (4b): the
        # broker's real per-side quote × rollover nights, credits allowed. Symmetric
        # (Phase 4): conservative drag on notional × calendar nights. Both gated so
        # swap-free runs are byte-for-byte unchanged.
        if position != 0.0 and i > 0:
            if swap_dir:
                nm = nights_mult[i]
                if nm > 0.0:
                    per_night = (cost.swap_long_per_night if position > 0
                                 else cost.swap_short_per_night)
                    pnl = abs(position) * per_night * nm     # + credit / − cost
                    realized += pnl
                    accrued_swap -= pnl
                    swap_total -= pnl
            elif swap_on:
                dt = ta[i] - ta[i - 1]
                nights = float(dt / np.timedelta64(1, "D")) if isinstance(dt, np.timedelta64) else float(dt)
                sc = cost.swap_cost(abs(position) * c[i - 1], nights)
                realized -= sc
                accrued_swap += sc
                swap_total += sc
        if cost.fill_timing == "close":
            desired = int(regime[i])        # LOOK-AHEAD: signal and fill on same close
            base_px = c[i]
        else:                                # "next_open" (realistic)
            desired = 0 if i == 0 else int(regime[i - 1])
            base_px = o[i]
        current = 0 if position == 0 else (1 if position > 0 else -1)

        if desired != current:
            if position != 0:                               # close existing
                _close(base_px, i)
            if desired != 0:                                # open new
                open_is_buy = desired > 0
                fill = cost.fill_price(base_px, open_is_buy)
                notional = realized * exposure
                position = desired * (notional / fill)
                entry = fill
                entry_i = i
                entry_commission = cost.commission(notional, abs(position) / cs)
                realized -= entry_commission
                comm_total += entry_commission

        equity[k] = realized + position * (c[i] - entry)    # mark-to-market on close

    # Close any residual position at the last close so its PnL counts.
    if position != 0 and m > 0:
        _close(c[-1], n - 1)
        equity[-1] = realized

    return _build_result(np.asarray(trade_pnls, dtype=float), equity,
                         time_arr[warmup:], initial_cash, timeframe_min, source,
                         cost, strategy.name, params, symbol,
                         total_swap_cost=swap_total, total_commission=comm_total,
                         holding_bars=hold_bars, holding_days=hold_days)


def run(symbol=None, timeframe_min=None, strategy_name=None, params=None,
        initial_cash=None, exposure=None, allow_short=None,
        cost=None) -> BacktestResult:
    """Load real data and run a registered strategy. Defaults: configured strategy
    + its default params + the REALISTIC cost model."""
    symbol = symbol or STRATEGY.symbol
    timeframe_min = timeframe_min or STRATEGY.timeframe_min
    strat = strategies.get(strategy_name or STRATEGY.name)
    params = strat.default_params() if params is None else params
    initial_cash = float(initial_cash or BACKTEST.initial_cash)
    exposure = BACKTEST.exposure if exposure is None else exposure
    allow_short = BACKTEST.allow_short if allow_short is None else allow_short
    cost = REALISTIC_COSTS if cost is None else cost

    data = load_ohlcv(symbol, timeframe_min)
    return _simulate(data.open, data.close, data.time, strat, params, initial_cash,
                     exposure, allow_short, cost, warmup=0,
                     timeframe_min=timeframe_min, source=data.source, symbol=symbol)


# ---- helpers used by the report and the results registry ----
def _pf(x) -> str:
    return "inf" if x == float("inf") else f"{x:.3f}"


def _pstr(params: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items()) if params else "(no params)"


def metrics_dict(r: BacktestResult) -> dict:
    return {"return_pct": round(r.total_return_pct, 4), "trades": r.n_trades,
            "win_rate_pct": round(r.win_rate_pct, 4),
            "profit_factor": None if r.profit_factor == float("inf") else round(r.profit_factor, 4),
            "expectancy": round(r.expectancy_per_trade, 4),
            "max_dd_pct": round(r.max_drawdown_pct, 4), "sharpe": round(r.sharpe, 4)}


def cost_dict(cost) -> dict:
    d = {"spread_pips": cost.spread_pips, "commission_per_lot": cost.commission_per_lot,
         "slippage_pips": cost.slippage_pips, "fill_timing": cost.fill_timing,
         "commission_per_side": cost.commission_per_side}
    # Record swap ONLY when present, so swap-free runs (SMA, single-EURUSD momentum)
    # keep a byte-identical cost payload and therefore the SAME content hash as before.
    if getattr(cost, "swap_rate_annual", 0.0):
        d["swap_rate_annual"] = cost.swap_rate_annual
    if getattr(cost, "swap_model", "symmetric") == "directional":
        d["swap_model"] = "directional"
        d["swap_long_per_night"] = cost.swap_long_per_night
        d["swap_short_per_night"] = cost.swap_short_per_night
        d["swap_triple_weekday"] = cost.swap_triple_weekday
    return d


def data_meta(r: BacktestResult) -> dict:
    return {"symbol": r.symbol, "timeframe": r.timeframe_min,
            "data_start": r.start, "data_end": r.end, "n_bars": r.bars}


def print_report(r: BacktestResult) -> None:
    bar = "=" * 60
    print()
    print(bar)
    print(f"  BACKTEST — {r.strategy}  [{_pstr(r.params)}]")
    print(f"  {r.symbol}  TF={r.timeframe_min}min  bars={r.bars}  data={r.source}")
    print(f"  {r.start}  ->  {r.end}")
    print(f"  Costs: spread={r.spread_pips}p  comm/lot={r.commission_per_lot}"
          f"  slip={r.slippage_pips}p  fill={r.fill_timing}")
    print(bar)
    print(f"  Initial cash         : {r.initial_cash:>14,.2f}")
    print(f"  Final equity         : {r.final_equity:>14,.2f}")
    print(f"  Total return         : {r.total_return_pct:>13,.2f} %")
    print(f"  Trades               : {r.n_trades:>14d}")
    print(f"  Win rate             : {r.win_rate_pct:>13.2f} %")
    print(f"  Profit factor        : {_pf(r.profit_factor):>14}")
    print(f"  Expectancy / trade   : {r.expectancy_per_trade:>14,.2f}"
          f"  ({r.expectancy_pct:+.3f}% of start)")
    print(f"  Max drawdown         : {r.max_drawdown_pct:>13.2f} %")
    print(f"  Sharpe (annualised)  : {r.sharpe:>14.2f}")
    print(bar)


def print_comparison(a: BacktestResult, b: BacktestResult,
                     la="LEGACY (Phase-0)", lb="REALISTIC (new default)") -> None:
    bar = "=" * 76
    print("\n" + bar)
    print(f"  FILL / COST AUDIT — {a.strategy} [{_pstr(a.params)}] "
          f"{a.symbol} H{a.timeframe_min // 60}  ({a.bars} bars, {a.start[:10]}→{a.end[:10]})")
    print(bar)
    print(f"  {'Metric':<20}{la:>18}{lb:>18}{'Δ':>18}")
    print("  " + "-" * 72)

    def row(name, av, bv, fmt, delta=True):
        d = ""
        if delta:
            try:
                d = fmt(bv - av)
            except Exception:
                d = ""
        print(f"  {name:<20}{fmt(av):>18}{fmt(bv):>18}{d:>18}")

    f2 = lambda x: f"{x:,.2f}"
    di = lambda x: f"{int(x):d}"
    row("Total return %", a.total_return_pct, b.total_return_pct, f2)
    row("Final equity", a.final_equity, b.final_equity, f2)
    row("Trades", a.n_trades, b.n_trades, di)
    row("Win rate %", a.win_rate_pct, b.win_rate_pct, f2)
    row("Profit factor", a.profit_factor, b.profit_factor, _pf, delta=False)
    row("Expectancy/trade", a.expectancy_per_trade, b.expectancy_per_trade, f2)
    row("Max drawdown %", a.max_drawdown_pct, b.max_drawdown_pct, f2)
    row("Sharpe", a.sharpe, b.sharpe, f2)
    print(bar)
    print(f"  {la}: spread=0 slip=0 comm(legacy-proxy) fill={a.fill_timing}")
    print(f"  {lb}: spread={b.spread_pips}p slip={b.slippage_pips}p "
          f"comm/lot={b.commission_per_lot} fill={b.fill_timing}")


def verdict(legacy: BacktestResult, realistic: BacktestResult) -> str:
    d = realistic.total_return_pct - legacy.total_return_pct
    if d < -1e-6:
        word = "OPTIMISTIC"
    elif d > 1e-6:
        word = "PESSIMISTIC"
    else:
        word = "HONEST"
    return (f"VERDICT: the original {legacy.total_return_pct:+.2f}% was {word}. "
            f"Realistic costs move it to {realistic.total_return_pct:+.2f}% "
            f"(Δ {d:+.2f} pts, {realistic.n_trades} trades).")


if __name__ == "__main__":
    legacy = run(cost=LEGACY_COSTS)
    realistic = run(cost=REALISTIC_COSTS)
    print_comparison(legacy, realistic)

    # Look-ahead demonstration — NEVER used for real numbers.
    look = run(cost=replace(REALISTIC_COSTS, fill_timing="close"))
    ld = look.total_return_pct - realistic.total_return_pct
    print(f"\n  ⚠ Look-ahead check (fill_timing='close', NOT executable): filling bar N's "
          f"signal at bar N's own close => {look.total_return_pct:+.2f}% vs realistic "
          f"next-open {realistic.total_return_pct:+.2f}% (Δ {ld:+.2f} pts).\n"
          f"    On 24h FX the close→next-open gap is ~0, so the bias is small HERE; it is "
          f"larger on gappy instruments (stocks/overnight). Shown to expose the mechanism.")

    v = verdict(legacy, realistic)
    print(f"\n  {v}\n")

    try:
        from registry import ResultsRegistry
        reg = ResultsRegistry()
        rh, dup = reg.log_run("backtest", realistic.strategy, realistic.params,
                              cost_dict(REALISTIC_COSTS), data_meta(realistic),
                              metrics_is=metrics_dict(realistic), notes=v)
        reg.close()
        print(f"  logged to results registry (hash {rh}"
              f"{'  ↻ DUPLICATE of a prior run' if dup else ''})\n")
    except Exception as e:
        print(f"  [registry] skipped: {e}")
