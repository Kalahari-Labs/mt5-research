"""test_core_contracts — the teeth behind the core/ migration (Phase 1).

These tests assert that the EXISTING implementations in both subsystems already
satisfy the shared `core` contracts, structurally, with no changes to their
source. If any implementation ever drifts from a contract, CI fails here — that
is how `core/` governs the code without rewriting it (docs/ARCHITECTURE.md §5).

They also validate the two net-new value objects (`Recommendation`, `Decision`)
that Phases 5-6 will produce.
"""
import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

import core  # noqa: E402


def _import_executor():
    """Import the executor package (canonical live subsystem) the way it runs:
    with `intel/` on the path so `executor.*` resolves. Appended (not inserted)
    so root modules keep precedence and the rest of the suite is unaffected."""
    intel_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intel")
    if intel_dir not in sys.path:
        sys.path.append(intel_dir)
    import executor.bridge as bridge  # noqa: E402
    import executor.strategies as strategies  # noqa: E402
    return bridge, strategies


class TestRootConformsToCore(unittest.TestCase):
    """The root research subsystem satisfies the contracts it maps to."""

    def test_risk_manager_is_core_risk_manager(self):
        from risk import RiskManager, RiskDecision, SymbolSpec
        cfg = types.SimpleNamespace(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0,
                                    max_open_positions=3)
        rm = RiskManager(cfg, SymbolSpec.from_specs({}), day_start_balance=10_000.0)
        self.assertIsInstance(rm, core.RiskManager)

    def test_risk_decision_is_core_risk_verdict(self):
        from risk import RiskDecision
        verdict = RiskDecision(approved=True, volume=0.1, risk_amount=5.0, reason="ok")
        self.assertIsInstance(verdict, core.RiskVerdict)

    def test_root_strategies_are_core_strategies(self):
        from strategies.sma_crossover import SmaCrossover
        from strategies.ts_momentum import TsMomentum
        from strategies.buy_and_hold import BuyAndHold
        for strat in (SmaCrossover(), TsMomentum(), BuyAndHold()):
            with self.subTest(strategy=strat.name):
                self.assertIsInstance(strat, core.Strategy)


class TestExecutorConformsToCore(unittest.TestCase):
    """The canonical live subsystem satisfies the broker/data/strategy contracts."""

    def test_bridge_is_broker_adapter(self):
        bridge, _ = _import_executor()
        b = bridge.Bridge()  # no network — just holds base_url/timeout
        self.assertIsInstance(b, core.BrokerAdapter)

    def test_bridge_is_market_data_provider(self):
        bridge, _ = _import_executor()
        self.assertIsInstance(bridge.Bridge(), core.MarketDataProvider)

    def test_executor_strategies_are_core_strategies(self):
        _, strategies = _import_executor()
        self.assertTrue(strategies.REGISTRY, "executor REGISTRY is empty")
        for name, strat in strategies.REGISTRY.items():
            with self.subTest(strategy=name):
                self.assertIsInstance(strat, core.Strategy)


class TestContractsRejectNonConformers(unittest.TestCase):
    """The Protocols are discriminating, not vacuous."""

    def test_plain_object_is_not_a_broker(self):
        self.assertNotIsInstance(object(), core.BrokerAdapter)

    def test_plain_object_is_not_a_strategy(self):
        self.assertNotIsInstance(object(), core.Strategy)

    def test_partial_broker_is_rejected(self):
        class HalfBroker:  # has account/positions but not order/close/modify/alive
            def account(self): ...
            def positions(self, symbol=None): ...
        self.assertNotIsInstance(HalfBroker(), core.BrokerAdapter)

    def test_broker_missing_reconciliation_or_health_is_rejected(self):
        """A broker that can trade but cannot report deal history or health is
        NOT a BrokerAdapter — the engine's reconcile step and startup banner
        depend on both, so the contract now requires them."""
        class NoHistoryBroker:
            def account(self): ...
            def positions(self, symbol=None): ...
            def order(self, symbol, side, volume, sl, tp, comment="", magic=0): ...
            def close(self, ticket, comment="", volume=None): ...
            def modify(self, ticket, sl=None, tp=None): ...
            def alive(self): ...
            def health(self): ...
            # no history_deals -> must be rejected
        self.assertNotIsInstance(NoHistoryBroker(), core.BrokerAdapter)

    def test_market_data_missing_symbol_is_rejected(self):
        """bars + tick without symbol metadata cannot size an order, so it is
        not a MarketDataProvider."""
        class NoSymbolFeed:
            def bars(self, symbol, tf="H1", count=300, start=0): ...
            def tick(self, symbol): ...
            # no symbol() -> must be rejected
        self.assertNotIsInstance(NoSymbolFeed(), core.MarketDataProvider)


class TestContractMatchesBridgeReality(unittest.TestCase):
    """Signature-level teeth: runtime_checkable only checks method NAMES, so
    these guard the parameters the engine actually passes (a partial close, a
    dated deal-history query) that isinstance alone cannot see."""

    def test_broker_close_accepts_partial_volume(self):
        """engine.manage_partials calls close(ticket, volume=half). Both the
        contract and the live Bridge must accept `volume`, or partial
        take-profits break silently under a Phase-2 adapter."""
        for target in (core.BrokerAdapter.close, _bridge_class().close):
            with self.subTest(target=target):
                self.assertIn("volume", inspect.signature(target).parameters)

    def test_broker_history_deals_accepts_days(self):
        for target in (core.BrokerAdapter.history_deals, _bridge_class().history_deals):
            with self.subTest(target=target):
                self.assertIn("days", inspect.signature(target).parameters)


def _bridge_class():
    bridge, _ = _import_executor()
    return bridge.Bridge


class TestRecommendationValueObject(unittest.TestCase):
    def test_valid_recommendation(self):
        r = core.Recommendation(side="buy", confidence=0.75,
                                reasoning="ema up + rsi pullback resolved",
                                metadata={"rsi": 42.0}, sl=1.0900, tp=1.1100)
        self.assertEqual(r.side, "buy")
        self.assertEqual(r.metadata["rsi"], 42.0)

    def test_rejects_bad_side_confidence_and_empty_reasoning(self):
        with self.assertRaises(ValueError):
            core.Recommendation(side="hold", confidence=0.5, reasoning="x")
        with self.assertRaises(ValueError):
            core.Recommendation(side="buy", confidence=1.5, reasoning="x")
        with self.assertRaises(ValueError):
            core.Recommendation(side="buy", confidence=0.5, reasoning="")


class TestDecisionValueObject(unittest.TestCase):
    def test_actionable_and_serialization(self):
        d = core.Decision(action=core.Action.BUY, symbol="EURUSD",
                          explanation="3 strategies agree, regime up",
                          confidence=0.7, factors={"votes": 3}, strategy="fvg_retrace")
        self.assertTrue(d.is_actionable)
        self.assertEqual(d.to_dict()["action"], "BUY")

    def test_wait_and_ignore_are_not_actionable(self):
        for action in (core.Action.WAIT, core.Action.IGNORE):
            d = core.Decision(action=action, symbol="EURUSD",
                              explanation="spread wider than 15%% of ATR")
            self.assertFalse(d.is_actionable)

    def test_rejects_bad_action_confidence_and_empty_explanation(self):
        with self.assertRaises(ValueError):
            core.Decision(action="BUY", symbol="X", explanation="y")  # not an Action
        with self.assertRaises(ValueError):
            core.Decision(action=core.Action.BUY, symbol="X", explanation="")
        with self.assertRaises(ValueError):
            core.Decision(action=core.Action.BUY, symbol="X", explanation="y",
                          confidence=2.0)


if __name__ == "__main__":
    unittest.main()
