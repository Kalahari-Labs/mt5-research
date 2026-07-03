"""base.py — the minimal Strategy contract.

A Strategy is PURE: given a close-price series + params it returns entry/exit
signals (a per-bar regime and the crossover/entry events) and nothing else. No
I/O, no order placement, no engine knowledge. The backtest/walk-forward engine
consumes only `.regime`, so any strategy that fills this contract drops straight
into the rig with zero engine changes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Signals:
    regime: np.ndarray    # per bar: +1 long, -1 short, 0 flat/warm-up (int array)
    entries: np.ndarray   # +1/-1 on the bar a new regime begins, else 0 (int array)


class Strategy:
    """Contract. Subclass, set `name`, implement `generate`. Optionally declare a
    `param_grid` (for robustness sweeps) and `default_params`."""

    name: str = "base"

    def generate(self, close, **params) -> Signals:
        raise NotImplementedError

    def default_params(self) -> dict:
        """Params used when none are supplied (e.g. a plain backtest)."""
        return {}

    def param_grid(self) -> dict:
        """Mapping of param-name -> iterable of values. The cross-product (filtered
        by `validate_params`) defines the robustness / search surface."""
        return {}

    def validate_params(self, **params) -> bool:
        return True

    # --- optional hooks the walk-forward harness consults (added so the harness is
    # strategy-agnostic, not SMA-specific). Defaults preserve the original SMA path. ---

    def wf_grid(self) -> dict | None:
        """The {param: iterable} grid the WALK-FORWARD searches in each in-sample
        window. Return None (default) to let walkforward.py fall back to its
        configured SMA fast/slow grid — i.e. SMA's path is unchanged. A strategy
        whose params are NOT (fast, slow) MUST override this so the harness knows
        what to optimise on its IS windows."""
        return None

    def warmup_bars(self, **params) -> int:
        """Leading bars the signal needs before it is valid — used to seed an
        out-of-sample window's indicator from real prior history WITHOUT look-ahead.
        Default: the largest integer param value, which is `slow` for SMA and
        max(lookback, anchor) for momentum."""
        ints = [int(v) for v in params.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return max(ints) if ints else 0
