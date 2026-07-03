"""data.py — OHLCV provider.

Tries the official MetaTrader5 package first (works on Windows / a proper
bridge); if it cannot import or connect — as on this Linux box — it falls back
to the cached CSV produced by tools/mt5_dump.py, which holds REAL broker data
pulled through the Wine bridge. Returns plain numpy arrays (no pandas needed).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from config import STRATEGY, data_csv_path, symbol_specs_path


@dataclass
class OHLCV:
    symbol: str
    timeframe_min: int
    time: np.ndarray     # datetime64[s], UTC
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    source: str          # "MetaTrader5" or "csv"

    def __len__(self) -> int:
        return int(self.close.shape[0])


def _write_cache(symbol, timeframe_min, rates) -> None:
    """Persist a freshly-pulled MT5 structured array to the CSV cache."""
    path = data_csv_path(symbol, timeframe_min)
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close",
                    "tick_volume", "spread", "real_volume"])
        for b in rates:
            ts = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), b["open"], b["high"],
                        b["low"], b["close"], int(b["tick_volume"]),
                        int(b["spread"]), int(b["real_volume"])])


def _try_mt5(symbol, timeframe_min, bars):
    """Pull from a live MT5 terminal. Returns OHLCV or None if unavailable."""
    try:
        import MetaTrader5 as mt5
    except Exception:
        return None
    tf_map = {1: mt5.TIMEFRAME_M1, 5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15,
              30: mt5.TIMEFRAME_M30, 60: mt5.TIMEFRAME_H1, 240: mt5.TIMEFRAME_H4,
              1440: mt5.TIMEFRAME_D1}
    if timeframe_min not in tf_map:
        return None
    if not mt5.initialize(timeout=60000):
        return None
    try:
        rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe_min], 0, bars)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        return None
    _write_cache(symbol, timeframe_min, rates)
    return OHLCV(
        symbol=symbol, timeframe_min=timeframe_min,
        time=rates["time"].astype("datetime64[s]"),
        open=rates["open"].astype(float), high=rates["high"].astype(float),
        low=rates["low"].astype(float), close=rates["close"].astype(float),
        volume=rates["tick_volume"].astype(float), source="MetaTrader5")


def _load_csv(symbol, timeframe_min) -> OHLCV:
    path = data_csv_path(symbol, timeframe_min)
    if not path.exists():
        raise FileNotFoundError(
            f"No cached data at {path}. Refresh it with the Wine dumper:\n"
            f"  WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine "
            f"'C:\\Program Files\\Python312\\python.exe' "
            f"'Z:\\home\\flowdaaddy\\mt5-research\\tools\\mt5_dump.py' "
            f"{symbol} {timeframe_min} 15000")
    t, o, h, l, c, v = [], [], [], [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t.append(np.datetime64(row["time"].replace(" ", "T")))
            o.append(float(row["open"])); h.append(float(row["high"]))
            l.append(float(row["low"])); c.append(float(row["close"]))
            v.append(float(row["tick_volume"]))
    return OHLCV(
        symbol=symbol, timeframe_min=timeframe_min,
        time=np.array(t, dtype="datetime64[s]"),
        open=np.array(o), high=np.array(h), low=np.array(l),
        close=np.array(c), volume=np.array(v), source="csv")


def load_ohlcv(symbol=None, timeframe_min=None, bars=15000, prefer_live=True) -> OHLCV:
    """Load OHLCV: live MT5 if reachable, else the real cached CSV."""
    symbol = symbol or STRATEGY.symbol
    timeframe_min = timeframe_min or STRATEGY.timeframe_min
    if prefer_live:
        data = _try_mt5(symbol, timeframe_min, bars)
        if data is not None:
            return data
    return _load_csv(symbol, timeframe_min)


def load_symbol_specs(symbol=None) -> dict:
    """Real contract specs captured by the dumper (tick value/size, lot step...)."""
    symbol = symbol or STRATEGY.symbol
    path = symbol_specs_path(symbol)
    if path.exists():
        return json.loads(Path(path).read_text())
    # Conservative EURUSD-like fallback so risk math still works.
    return {"trade_tick_value": 1.0, "trade_tick_size": 1e-05, "point": 1e-05,
            "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01,
            "trade_contract_size": 100000.0}
