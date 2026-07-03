"""config.py — the single place to change everything.

Symbol, timeframe, strategy params, risk params, backtest params, and execution
flags all live here. Any value can be overridden from a `.env` file (loaded once
at import) so flags/secrets stay out of source control.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is optional; defaults work without it
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _get(name, default, cast=str):
    """Read env var `name`, cast it, or fall back to `default`."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return cast(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StrategyConfig:
    name: str = _get("STRATEGY_NAME", "sma_crossover")   # key into the strategies/ registry
    symbol: str = _get("SYMBOL", "EURUSD")
    timeframe_min: int = _get("TIMEFRAME_MIN", 60, int)   # 60 = H1, 240 = H4, 1440 = D1
    fast_period: int = _get("SMA_FAST", 20, int)          # default params for sma_crossover
    slow_period: int = _get("SMA_SLOW", 50, int)

    # --- ts_momentum (time-series / trend-following momentum) knobs ---
    # LOOKBACK = trailing-return horizon. Default 120 D1 bars ≈ 6 months — squarely
    # inside the 1–12 month window where TSMOM (Moskowitz-Ooi-Pedersen 2012) is
    # documented. NB this horizon is meant for D1+; on H1 it is only ~5 days.
    mom_lookback: int = _get("MOM_LOOKBACK", 120, int)
    # ANCHOR = long-EMA length for the trend-confirmation filter (take a momentum
    # signal only on the correct side of this anchor). 200 ≈ the classic long filter.
    mom_anchor: int = _get("MOM_ANCHOR", 200, int)
    mom_allow_short: bool = _get("MOM_ALLOW_SHORT", True, bool)   # short when momentum < 0
    mom_use_anchor: bool = _get("MOM_USE_ANCHOR", True, bool)     # trend filter ON by default
    mom_vol_filter: bool = _get("MOM_VOL_FILTER", False, bool)    # vol filter OFF by default
    mom_vol_lookback: int = _get("MOM_VOL_LOOKBACK", 20, int)     # realized-vol window (bars)
    mom_vol_window: int = _get("MOM_VOL_WINDOW", 252, int)        # trailing dist for percentile
    mom_vol_max_pct: float = _get("MOM_VOL_MAX_PCT", 0.90, float) # skip entries above this pctl

    def __post_init__(self):
        if self.fast_period >= self.slow_period:
            raise ValueError("SMA_FAST must be strictly less than SMA_SLOW")


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = _get("RISK_PER_TRADE_PCT", 1.0, float)
    max_daily_loss_pct: float = _get("MAX_DAILY_LOSS_PCT", 3.0, float)
    max_open_positions: int = _get("MAX_OPEN_POSITIONS", 1, int)
    # Protective stop distance (in price units) used to size positions.
    default_stop_distance: float = _get("DEFAULT_STOP_DISTANCE", 0.0050, float)


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = _get("INITIAL_CASH", 10_000.0, float)
    exposure: float = _get("EXPOSURE", 1.0, float)               # fraction of equity per trade
    # LEGACY notional-fraction commission proxy (~0.7 bps). Kept only to reproduce
    # the original Phase-0 numbers; the realistic engine uses CostModel below.
    commission_per_side: float = _get("COMMISSION_PER_SIDE", 0.00007, float)
    allow_short: bool = _get("ALLOW_SHORT", True, bool)


@dataclass(frozen=True)
class CostModel:
    """Explicit, configurable transaction-cost model. Every cost is named — there
    are NO implicit or zero costs hidden in the engine.

    NOTE vs canonical backtesting.py: that library applies `commission` as a single
    fraction of trade value and has NO separate spread/slippage. This model keeps
    them distinct so they map to MetaTrader5 reality (spread in pips, commission
    per lot). To port back, fold spread + 2*slippage + commission into the one
    backtesting.py `commission=` fraction. See FILL_MODEL.md.
    """
    spread_pips: float = 0.8           # round-trip bid/ask width, in pips
    commission_per_lot: float = 3.5    # account ccy, per 1.0 lot, PER SIDE
    slippage_pips: float = 0.2         # adverse fill per side, in pips
    fill_timing: str = "next_open"     # "next_open" (REALISTIC) | "close" (LOOK-AHEAD/optimistic)
    pip_size: float = 0.0001           # EURUSD: 1 pip = 0.0001 (= 10 points at digits=5)
    contract_size: float = 100_000.0   # EURUSD: 1.0 lot = 100k units
    commission_per_side: float = 0.0   # LEGACY proxy: commission as a FRACTION of notional
    # --- overnight swap / financing (Phase 4) ---------------------------------
    # Annualised financing rate as a FRACTION of the held notional, charged per
    # night a position is carried. DEFAULT 0.0 => no financing, so every pre-Phase-4
    # run (SMA, single-EURUSD momentum) reproduces its prior numbers BIT-FOR-BIT.
    # MODELLING CHOICE: real broker swaps are quoted per side (swap_long vs
    # swap_short) and are usually a COST on both sides after markup, occasionally a
    # small credit. The brief notes long/short carry "opposite signs"; we instead
    # take the conservative WORST CASE — a symmetric DRAG charged on whichever side
    # is held — because Phase 4 asks whether the thin momentum edge survives realistic
    # multi-week holding costs, not whether a carry subsidy rescues it. Exact
    # historical swaps are unavailable, so each rate is a documented conservative
    # constant (see SWAP_RATES_ANNUAL). The per-side directional model is the
    # canonical upgrade (set swap_long/swap_short from MT5 symbol_info().swap_long).
    swap_rate_annual: float = 0.0      # e.g. 0.05 = 5%/yr cost of carry on notional
    # --- directional swap, Phase 4b -------------------------------------------
    # Per-side financing from the broker's REAL quotes (tools/dump_h4.py captures
    # symbol_info().swap_long/swap_short into data/{sym}_swap.json). Values are the
    # PRICE move per unit of base asset per rollover night (broker points × point
    # size), signed the way the broker signs them: NEGATIVE = cost, POSITIVE = credit.
    # swap_model="directional" switches the engine to per-night charging with the
    # broker's triple-swap day (FX rolls charge 3× swap once a week for T+2 weekend
    # settlement) and NO charge on Sat/Sun nights — Mon 1×,Tue 1×,Wed 3×,Thu 1×,
    # Fri 1× = 7 nights/week, same weekly total as the symmetric calendar model but
    # with honest per-side signs and honest WEDNESDAY TIMING (which matters at short
    # holds). Defaults keep the model OFF so every pre-4b run is byte-identical.
    swap_model: str = "symmetric"       # "symmetric" (Phase 4) | "directional" (4b)
    swap_long_per_night: float = 0.0    # price units/unit/night when LONG (+credit/−cost)
    swap_short_per_night: float = 0.0   # price units/unit/night when SHORT
    swap_triple_weekday: int = 2        # Python weekday charged 3× (Mon=0 … Wed=2)

    @property
    def half_spread_price(self) -> float:
        return self.spread_pips * self.pip_size / 2.0

    @property
    def slippage_price(self) -> float:
        return self.slippage_pips * self.pip_size

    def fill_price(self, base_px: float, is_buy: bool) -> float:
        """Effective fill: a BUY pays half-spread + slippage above base; a SELL
        receives them below. Bar prices are treated as mid (MT5 bars are bid-based,
        a documented modeling choice — see FILL_MODEL.md)."""
        adj = self.half_spread_price + self.slippage_price
        return base_px + adj if is_buy else base_px - adj

    def commission(self, notional: float, lots: float) -> float:
        """Per-side commission in account currency."""
        return notional * self.commission_per_side + lots * self.commission_per_lot

    def swap_cost(self, notional: float, nights: float) -> float:
        """Overnight financing for carrying `notional` (account ccy) for `nights`
        calendar nights. Returns a NON-NEGATIVE cost (it always reduces P&L — the
        conservative symmetric-drag choice documented on `swap_rate_annual`). A
        360-day year is the market convention for accrual. Zero when the rate or the
        holding period is zero, so swap-free runs are unaffected."""
        if nights <= 0.0 or notional <= 0.0 or self.swap_rate_annual <= 0.0:
            return 0.0
        return self.swap_rate_annual / 360.0 * notional * nights


@dataclass(frozen=True)
class WalkForwardConfig:
    # Rolling (NON-anchored) windows. Defaults ~ 6 months in-sample / 1 month
    # out-of-sample at H1 (FX ~ 120 bars/week -> ~3000 IS, ~500 OOS). step == OOS
    # so the OOS windows tile the timeline with no gaps and no overlap.
    in_sample_bars: int = _get("WF_IS_BARS", 3000, int)
    out_of_sample_bars: int = _get("WF_OOS_BARS", 500, int)
    step: int = _get("WF_STEP", 500, int)
    min_trades_in_sample: int = _get("WF_MIN_TRADES", 30, int)
    fast_grid: tuple = (5, 10, 15, 20, 30)
    slow_grid: tuple = (30, 50, 80, 100, 150, 200)


@dataclass(frozen=True)
class ExecutionConfig:
    # BOTH must be flipped before a single order is sent — and only on a demo account.
    execution_enabled: bool = _get("EXECUTION_ENABLED", False, bool)
    dry_run: bool = _get("DRY_RUN", True, bool)
    magic: int = _get("MAGIC", 990100, int)
    deviation: int = _get("DEVIATION", 20, int)


@dataclass(frozen=True)
class JournalConfig:
    sqlite_path: str = _get("JOURNAL_DB", str(DATA_DIR / "journal.sqlite"))
    supabase_url: str = _get("SUPABASE_URL", "")
    supabase_key: str = _get("SUPABASE_KEY", "")

    @property
    def use_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


STRATEGY = StrategyConfig()
RISK = RiskConfig()
BACKTEST = BacktestConfig()
EXECUTION = ExecutionConfig()
JOURNAL = JournalConfig()
WALKFORWARD = WalkForwardConfig()

# Default realistic cost model. ⚠ set spread_pips/commission_per_lot to YOUR
# broker's actual figures before trusting absolute returns (see FILL_MODEL.md).
REALISTIC_COSTS = CostModel(
    spread_pips=_get("SPREAD_PIPS", 0.8, float),
    commission_per_lot=_get("COMMISSION_PER_LOT", 3.5, float),
    slippage_pips=_get("SLIPPAGE_PIPS", 0.2, float),
    fill_timing=_get("FILL_TIMING", "next_open"),
    pip_size=_get("PIP_SIZE", 0.0001, float),
    contract_size=_get("CONTRACT_SIZE", 100_000.0, float),
    commission_per_side=0.0,
)

# Reproduces the original Phase-0 engine EXACTLY (no spread, no slippage, the old
# notional-fraction commission proxy, next-open fills). Audit comparison ONLY.
LEGACY_COSTS = CostModel(
    spread_pips=0.0, commission_per_lot=0.0, slippage_pips=0.0,
    fill_timing="next_open", pip_size=0.0001, contract_size=100_000.0,
    commission_per_side=BACKTEST.commission_per_side,
)


# ─────────────────────────── Phase 4: cross-asset portfolio ──────────────────
# The basket diversifies across asset classes (not just FX). Broker symbol names
# are verified against this account's table (XM demo, see tools/dump_basket.py).
PORTFOLIO_BASKET = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "GOLD", "US500Cash", "OILCash")

# Per-instrument REALISTIC cost + financing spec. Each row builds a CostModel for
# that sleeve. spread/slip are in the instrument's conventional pip; comm/lot is the
# per-1.0-lot commission (FX only — index/metal/oil CFDs price their cost in the
# spread, so comm/lot=0). swap_annual is the conservative overnight-financing DRAG
# (fraction of notional per year, see CostModel.swap_rate_annual). Spreads & swaps
# are DOCUMENTED CONSERVATIVE APPROXIMATIONS — exact historical figures are not
# available; the point is to stress the edge, not to nail a penny. EURUSD's row is
# byte-identical to REALISTIC_COSTS (0.8/0.2/3.5) so the EURUSD sleeve == the
# single-instrument reference except for the added swap.
#   key:        pip_size,  spread, slip, comm/lot, contract,  swap_annual
INSTRUMENT_COSTS = {
    "EURUSD":    dict(pip_size=1e-4, spread_pips=0.8, slippage_pips=0.2, commission_per_lot=3.5, contract_size=1e5,  swap_rate_annual=0.020),
    "GBPUSD":    dict(pip_size=1e-4, spread_pips=1.2, slippage_pips=0.3, commission_per_lot=3.5, contract_size=1e5,  swap_rate_annual=0.020),
    "AUDUSD":    dict(pip_size=1e-4, spread_pips=1.0, slippage_pips=0.3, commission_per_lot=3.5, contract_size=1e5,  swap_rate_annual=0.025),
    "USDJPY":    dict(pip_size=1e-2, spread_pips=1.0, slippage_pips=0.3, commission_per_lot=3.5, contract_size=1e5,  swap_rate_annual=0.015),
    "GOLD":      dict(pip_size=1e-2, spread_pips=25.0, slippage_pips=8.0, commission_per_lot=0.0, contract_size=100.0, swap_rate_annual=0.040),
    "US500Cash": dict(pip_size=1e-1, spread_pips=5.0,  slippage_pips=2.0, commission_per_lot=0.0, contract_size=1.0,   swap_rate_annual=0.050),
    "OILCash":   dict(pip_size=1e-2, spread_pips=3.0,  slippage_pips=1.0, commission_per_lot=0.0, contract_size=100.0, swap_rate_annual=0.060),
}
# Fallback for any symbol missing above: EURUSD-like FX costs + a mid swap.
_DEFAULT_INSTRUMENT_COST = dict(pip_size=1e-4, spread_pips=1.0, slippage_pips=0.3,
                                commission_per_lot=3.5, contract_size=1e5, swap_rate_annual=0.030)


def cost_for(symbol: str, fill_timing: str = "next_open", with_swap: bool = True,
             swap_model: str = "symmetric") -> CostModel:
    """Build the REALISTIC per-sleeve CostModel for `symbol` from INSTRUMENT_COSTS.
    `with_swap=False` drops financing entirely for swap-on/swap-off comparisons.
    `swap_model="directional"` (Phase 4b) replaces the conservative symmetric drag
    with the broker's REAL per-side swap quotes from data/{sym}_swap.json — it
    RAISES if that file is missing, because silently falling back to the symmetric
    model would defeat the point of the directional test. All fields are explicit;
    nothing is implicit or zero by accident."""
    spec = dict(INSTRUMENT_COSTS.get(symbol, _DEFAULT_INSTRUMENT_COST))
    if not with_swap:
        spec["swap_rate_annual"] = 0.0
        return CostModel(fill_timing=fill_timing, commission_per_side=0.0, **spec)
    if swap_model == "directional":
        d = load_swap_spec(symbol)
        spec["swap_rate_annual"] = 0.0            # one financing model at a time
        return CostModel(fill_timing=fill_timing, commission_per_side=0.0,
                         swap_model="directional",
                         swap_long_per_night=d["swap_long_per_night"],
                         swap_short_per_night=d["swap_short_per_night"],
                         swap_triple_weekday=d["swap_triple_weekday"], **spec)
    return CostModel(fill_timing=fill_timing, commission_per_side=0.0, **spec)


def swap_spec_path(symbol: str) -> Path:
    return DATA_DIR / f"{symbol}_swap.json"


def load_swap_spec(symbol: str) -> dict:
    """Load the broker's REAL swap quote captured by tools/dump_h4.py and convert
    it to engine terms. Only swap_mode==1 (points) is implemented — that is what
    this account quotes for every basket instrument (verified in the dump); any
    other mode must be implemented deliberately, not guessed. Returns:
      swap_long_per_night / swap_short_per_night — PRICE units per unit of base
        asset per rollover night (broker sign: negative = cost, positive = credit)
      swap_triple_weekday — Python weekday (Mon=0) of the 3× rollover day,
        converted from MT5's ENUM_DAY_OF_WEEK (Sun=0)."""
    import json
    path = swap_spec_path(symbol)
    if not path.exists():
        raise FileNotFoundError(
            f"No swap spec at {path}. Capture it with the Wine dumper:\n"
            f"  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine "
            f"'C:\\Program Files\\Python312\\python.exe' "
            f"'Z:\\home\\flowdaaddy\\mt5-research\\tools\\dump_h4.py'")
    raw = json.loads(path.read_text())
    if raw.get("swap_mode") != 1:
        raise ValueError(f"{symbol}: swap_mode={raw.get('swap_mode')} not implemented "
                         f"(only 1 = points). Extend load_swap_spec deliberately.")
    return {
        "swap_long_per_night": raw["swap_long"] * raw["point"],
        "swap_short_per_night": raw["swap_short"] * raw["point"],
        "swap_triple_weekday": (int(raw["swap_rollover3days"]) + 6) % 7,
        "raw": raw,
    }


def data_csv_path(symbol=None, timeframe_min=None) -> Path:
    symbol = symbol or STRATEGY.symbol
    timeframe_min = timeframe_min or STRATEGY.timeframe_min
    return DATA_DIR / f"{symbol}_{timeframe_min}.csv"


def symbol_specs_path(symbol=None) -> Path:
    return DATA_DIR / f"{symbol or STRATEGY.symbol}_symbol.json"
