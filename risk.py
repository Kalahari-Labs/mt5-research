"""risk.py — deterministic, unit-testable risk control.

THE ONLY module allowed to approve an order. Position sizing comes from a fixed
% risk + stop distance; hard caps enforce max risk per trade, max daily loss,
and max open positions; a kill switch can halt everything. No I/O, no broker
calls — pure arithmetic on explicit inputs, so every branch is testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    volume: float        # lots (0.0 when rejected)
    risk_amount: float   # account-currency risk at the protective stop
    reason: str


@dataclass(frozen=True)
class SymbolSpec:
    tick_value: float    # account-currency value of one tick move, per 1.0 lot
    tick_size: float     # price increment of one tick
    volume_min: float
    volume_max: float
    volume_step: float

    @classmethod
    def from_specs(cls, d: dict) -> "SymbolSpec":
        return cls(
            tick_value=float(d.get("trade_tick_value", 1.0)),
            tick_size=float(d.get("trade_tick_size", d.get("point", 1e-05))),
            volume_min=float(d.get("volume_min", 0.01)),
            volume_max=float(d.get("volume_max", 100.0)),
            volume_step=float(d.get("volume_step", 0.01)),
        )

    def money_per_lot(self, stop_distance_price: float) -> float:
        """Loss in account currency for 1.0 lot if price moves stop_distance."""
        if self.tick_size <= 0:
            return 0.0
        return (stop_distance_price / self.tick_size) * self.tick_value


def round_to_step(volume: float, step: float) -> float:
    """Floor to the lot step (never round risk UP), robust to float dust."""
    if step <= 0:
        return float(volume)
    steps = math.floor(round(volume / step, 9))
    return round(steps * step, 10)


class RiskManager:
    def __init__(self, config, spec: SymbolSpec, day_start_balance: float):
        self.config = config              # RiskConfig
        self.spec = spec
        self.day_start_balance = float(day_start_balance)
        self.realized_pnl_today = 0.0     # negative = net loss today
        self.open_positions = 0
        self._manual_kill = False

    # ---- daily-loss accounting ----
    @property
    def daily_loss(self) -> float:
        return max(0.0, -self.realized_pnl_today)

    @property
    def max_daily_loss_amount(self) -> float:
        return self.day_start_balance * self.config.max_daily_loss_pct / 100.0

    @property
    def kill_switch(self) -> bool:
        return self._manual_kill or (self.daily_loss >= self.max_daily_loss_amount)

    def trip_kill_switch(self) -> None:
        self._manual_kill = True

    def reset_day(self, balance: float) -> None:
        self.day_start_balance = float(balance)
        self.realized_pnl_today = 0.0
        self._manual_kill = False

    def register_open(self) -> None:
        self.open_positions += 1

    def register_close(self, pnl: float) -> None:
        self.realized_pnl_today += float(pnl)
        self.open_positions = max(0, self.open_positions - 1)

    # ---- sizing ----
    def position_size(self, balance: float, stop_distance_price: float) -> float:
        risk_amount = balance * self.config.risk_per_trade_pct / 100.0
        money_per_lot = self.spec.money_per_lot(stop_distance_price)
        if money_per_lot <= 0:
            return 0.0
        return round_to_step(risk_amount / money_per_lot, self.spec.volume_step)

    # ---- THE ONLY approval gate ----
    def evaluate(self, balance: float, stop_distance_price: float) -> RiskDecision:
        if self._manual_kill:
            return RiskDecision(False, 0.0, 0.0, "REJECT: kill switch engaged")
        if self.kill_switch:
            return RiskDecision(
                False, 0.0, 0.0,
                f"REJECT: daily loss {self.daily_loss:.2f} >= cap "
                f"{self.max_daily_loss_amount:.2f}")
        if self.open_positions >= self.config.max_open_positions:
            return RiskDecision(
                False, 0.0, 0.0,
                f"REJECT: open positions {self.open_positions} >= max "
                f"{self.config.max_open_positions}")
        if stop_distance_price <= 0:
            return RiskDecision(False, 0.0, 0.0, "REJECT: non-positive stop distance")

        money_per_lot = self.spec.money_per_lot(stop_distance_price)
        if money_per_lot <= 0:
            return RiskDecision(False, 0.0, 0.0, "REJECT: invalid symbol spec / tick size")

        risk_budget = balance * self.config.risk_per_trade_pct / 100.0
        vol = self.position_size(balance, stop_distance_price)
        if vol < self.spec.volume_min:
            min_risk = self.spec.volume_min * money_per_lot
            return RiskDecision(
                False, 0.0, min_risk,
                f"REJECT: min lot {self.spec.volume_min} risks {min_risk:.2f} "
                f"> budget {risk_budget:.2f}")

        vol = min(vol, self.spec.volume_max)
        risk_amount = vol * money_per_lot

        # Rounding/cap safety: never exceed the per-trade risk budget.
        if risk_amount > risk_budget * (1.0 + 1e-9):
            return RiskDecision(
                False, 0.0, risk_amount,
                f"REJECT: sized risk {risk_amount:.2f} exceeds budget {risk_budget:.2f}")

        # Would this trade, if fully stopped out, breach today's loss cap?
        if self.daily_loss + risk_amount > self.max_daily_loss_amount * (1.0 + 1e-9):
            return RiskDecision(
                False, 0.0, risk_amount,
                f"REJECT: trade risk {risk_amount:.2f} would breach daily loss cap "
                f"{self.max_daily_loss_amount:.2f}")

        return RiskDecision(
            True, vol, risk_amount,
            f"APPROVE: {vol} lots, risk {risk_amount:.2f} "
            f"({self.config.risk_per_trade_pct}% of {balance:.2f})")
