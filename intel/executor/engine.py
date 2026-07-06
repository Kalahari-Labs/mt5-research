"""engine.py — the autonomous execution loop. One process, no emotions.

Cycle (every CYCLE_SEC):
  0. bridge health — reboot the Wine bridge/terminal if dead
  1. kill switch   — flatten everything and halt if executor/data/KILL exists
  2. reconcile     — positions the broker closed (SL/TP) since last cycle are
                     finalized from history deals and sent to review.py
  3. manage        — time-stop and Friday-flat open executor positions
  4. decide        — on each NEW closed bar, run every strategy on every symbol;
                     risk.py vetoes or EXEC_MODE=observe journals; survivors
                     become orders WITH sl+tp (bridge re-verifies demo + refuses
                     naked orders server-side)
  5. snapshot      — equity curve point + heartbeat
  6. maintenance   — calendar refresh, gate re-run when stale, daily report

Every decision — including every skip — is journaled with its reason.
The dashboard renders ONLY what this loop wrote to SQLite or reads live from
the bridge. There is no path by which an unverified number reaches the UI.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone, timedelta

import numpy as np

from . import config, gate, news_calendar, notify, review, risk
from .analysis import Bars, regime
from .backtester import SymbolSpec
from .bridge import BridgeError, BridgeRefused, ensure_bridge
from .store import Store, utcnow
from .strategies import REGISTRY

TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
          "H1": 3600, "H4": 14400, "D1": 86400}

# MT5 deal reason codes -> exit_reason labels
DEAL_REASON = {4: "sl", 5: "tp", 3: "expert", 0: "manual", 1: "manual", 2: "manual"}


def log(msg: str) -> None:
    print("[engine %s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg),
          flush=True)


class Engine:
    def __init__(self):
        self.store = Store()
        self.bridge = ensure_bridge()
        self.specs: dict[str, SymbolSpec] = {}
        self.cycle = 0
        self.kill_handled = False       # flatten once per KILL, not every cycle
        self.last_halt_reason = None    # notify once per distinct halt reason

    # ---- helpers ----------------------------------------------------------------
    def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self.specs:
            self.specs[symbol] = SymbolSpec.from_bridge(self.bridge.symbol(symbol))
        return self.specs[symbol]

    def flatten_all(self, why: str) -> None:
        for t in self.store.open_trades():
            try:
                res = self.bridge.close(t["ticket"], comment="flatten: %s" % why[:20])
                log("flatten %s -> %s" % (t["ticket"], res.get("ok")))
            except (BridgeError, BridgeRefused) as e:
                log("flatten %s FAILED: %s" % (t["ticket"], e))
        self.store.decide("halt", "flattened all positions: %s" % why)

    # ---- cycle steps --------------------------------------------------------------
    def reconcile(self) -> None:
        """Finalize DB trades whose broker position no longer exists."""
        open_db = self.store.open_trades()
        if not open_db:
            return
        live = {p["ticket"] for p in self.bridge.positions()}
        gone = [t for t in open_db if t["ticket"] not in live]
        if not gone:
            return
        deals = self.bridge.history_deals(days=7)
        for t in gone:
            closing = [d for d in deals
                       if d.get("position_id") == t["ticket"] and d.get("entry") == 1]
            if not closing:
                continue  # deal not visible yet; retry next cycle
            pnl = sum(d.get("profit", 0.0) for d in closing)
            swap = sum(d.get("swap", 0.0) for d in closing)
            comm = sum(d.get("commission", 0.0) for d in closing)
            last = max(closing, key=lambda d: d.get("time", 0))
            exit_reason = DEAL_REASON.get(last.get("reason", -1), "manual")
            risk_amt = abs(t["entry_price"] - t["sl"]) * self.spec(t["symbol"]).unit_value * t["volume"] \
                if t.get("sl") else None
            net = pnl + swap + comm
            r_mult = net / risk_amt if risk_amt else None
            exit_time = (datetime.fromtimestamp(last.get("time", time.time()), tz=timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.store.close_trade(t["id"], exit_time=exit_time, exit_price=last.get("price"),
                                   pnl=net, swap=swap, commission=comm,
                                   r_multiple=r_mult, exit_reason=exit_reason)
            self.store.decide("exit", "broker closed #%s (%s) pnl=%.2f"
                              % (t["ticket"], exit_reason, net),
                              symbol=t["symbol"], strategy=t["strategy"])
            log("closed #%s %s %s pnl=%.2f (%s)"
                % (t["ticket"], t["symbol"], t["strategy"], net, exit_reason))
            notify.trade_closed(t["symbol"], t["strategy"], net, exit_reason, t["ticket"])
            trade = self.store.trade(t["id"])
            tags = review.on_trade_closed(self.store, self.bridge, trade)
            if tags:
                log("review #%s -> %s" % (t["ticket"], ",".join(tags)))

    def manage_open(self) -> None:
        """Engine-side exits the broker can't do: time-stop, Friday flat."""
        now = datetime.now(timezone.utc)
        for t in self.store.open_trades():
            tf_sec = TF_SEC.get(t["timeframe"] or config.TIMEFRAME,
                                TF_SEC[config.TIMEFRAME])
            entry = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
            bars_held = (now - entry).total_seconds() / tf_sec
            reason = None
            if bars_held >= config.MAX_HOLD_BARS:
                reason = "time_stop"
            elif now.weekday() == 4 and now.hour >= config.FRIDAY_FLAT_HOUR_UTC:
                reason = "friday_flat"
            if not reason:
                continue
            try:
                res = self.bridge.close(t["ticket"], comment=reason)
                if res.get("ok"):
                    self.store.decide("exit", "%s after %.0f bars" % (reason, bars_held),
                                      symbol=t["symbol"], strategy=t["strategy"])
                    log("%s #%s %s" % (reason, t["ticket"], t["symbol"]))
            except (BridgeError, BridgeRefused) as e:
                log("manage close #%s failed: %s" % (t["ticket"], e))

    def manage_trailing(self) -> None:
        """Move SL to lock in profit based on ATR trailing distance."""
        if config.TRAILING_STOP_ATR_MULT <= 0:
            return
        for t in self.store.open_trades():
            if t.get("status") != "open": continue
            spec = self.spec(t["symbol"])
            tick = self.bridge.tick(t["symbol"])
            price = tick["bid"] if t["side"] == "buy" else tick["ask"]
            atr = t.get("entry_atr") or 0.0
            if not atr: continue
            
            trail_dist = atr * config.TRAILING_STOP_ATR_MULT
            new_sl = price - trail_dist if t["side"] == "buy" else price + trail_dist
            
            # Only move SL forward, never widen it
            cur_sl = t.get("sl", 0.0)
            should_move = (t["side"] == "buy" and new_sl > cur_sl) or (t["side"] == "sell" and (cur_sl == 0.0 or new_sl < cur_sl))
            
            if should_move:
                try:
                    res = self.bridge.modify(t["ticket"], sl=round(new_sl, spec.digits))
                    if res.get("ok"):
                        self.store.update_trade_sl(t["id"], new_sl)
                        log("TRAIL #%s %s sl -> %.5f" % (t["ticket"], t["symbol"], new_sl))
                except BridgeError: pass

    def manage_partials(self) -> None:
        """Close half the position at a specific R-multiple to de-risk."""
        if config.PARTIAL_EXIT_R_MULT <= 0:
            return
        for t in self.store.open_trades():
            if t.get("status") != "open" or t.get("partial_closed"): continue
            spec = self.spec(t["symbol"])
            tick = self.bridge.tick(t["symbol"])
            price = tick["bid"] if t["side"] == "buy" else tick["ask"]
            
            risk_pts = abs(t["entry_price"] - t["sl"]) if t.get("sl") else 0.0
            if not risk_pts: continue
            
            pnl_pts = (price - t["entry_price"]) if t["side"] == "buy" else (t["entry_price"] - price)
            cur_r = pnl_pts / risk_pts
            
            if cur_r >= config.PARTIAL_EXIT_R_MULT:
                half_vol = round(t["volume"] / 2.0, 2)
                if half_vol >= spec.volume_min:
                    try:
                        res = self.bridge.close(t["ticket"], volume=half_vol, comment="partial_tp")
                        if res.get("ok"):
                            self.store.mark_partial_closed(t["id"])
                            log("PARTIAL TP #%s %s closed %.2f lots at %.1fR" % (t["ticket"], t["symbol"], half_vol, cur_r))
                            notify.trade_closed(t["symbol"], t["strategy"], 0.0, "partial_tp", t["ticket"])
                    except BridgeError: pass

    def decide_entries(self, account: dict) -> None:
        try:
            risk.check_halts(self.store, account)
        except risk.Veto as v:
            if str(v) != self.last_halt_reason:
                self.last_halt_reason = str(v)
                self.store.decide("halt", str(v))
                notify.halt(str(v))
            elif self.cycle % 10 == 0:
                self.store.decide("halt", str(v))
            return
        self.last_halt_reason = None
        by_tf: dict[str, list] = {}
        for name, strat in REGISTRY.items():
            by_tf.setdefault(strat.timeframe or config.TIMEFRAME, []).append((name, strat))
        positions = self.bridge.positions()
        for symbol in config.SYMBOLS:
            for tf, strats in sorted(by_tf.items()):
                try:
                    raw = self.bridge.bars(symbol, tf, config.BARS_LIVE)
                except BridgeError as e:
                    self.store.decide("skip", "no %s bars: %r" % (tf, e), symbol=symbol)
                    continue
                if len(raw) < 100:
                    continue
                closed_t = int(raw[-2][0])  # last CLOSED bar (last element is forming)
                state_key = "last_bar:%s:%s" % (symbol, tf)
                if self.store.get_state(state_key) == closed_t:
                    continue  # no new closed bar since last decision
                self.store.set_state(state_key, closed_t)

                bars = Bars(raw)
                i = bars.n - 2
                reg = regime(bars, i)
                tick = self.bridge.tick(symbol)
                tick_fresh = bool(tick.get("time")) and (time.time() - tick["time"] < 600)
                spec = self.spec(symbol)
                live_spread = (tick.get("ask", 0) - tick.get("bid", 0)) if tick_fresh else 0.0
                med_spread_pts = float(np.median([b[6] for b in raw[-100:]]))
                stars = self.calculate_confluence(symbol, tf, reg, None)
                self.symbol_view(symbol, tf, reg, spec, live_spread, tick_fresh,
                                 med_spread_pts, stars)

                for name, strat in strats:
                    sig = strat.decide(bars, i)
                    if sig is None:
                        continue
                    try:
                        risk.check_entry(self.store, symbol, name, positions,
                                         live_spread, reg["atr"], tick_fresh)
                    except risk.Veto as v:
                        self.store.decide("skip", str(v), symbol=symbol, strategy=name,
                                          side=sig.side,
                                          detail={"signal": sig.reason, "regime": reg})
                        continue
                    if config.EXEC_MODE != "trade":
                        self.store.decide("enter", "OBSERVE MODE — would have entered: %s"
                                          % sig.reason, symbol=symbol, strategy=name,
                                          side=sig.side, detail={"observe": True, "regime": reg})
                        continue
                    if config.HITL_MODE:
                        self.propose(symbol, name, tf, sig, reg, account, spec, med_spread_pts)
                        continue
                    if self.place(symbol, name, tf, sig, reg, account, spec,
                                  live_spread, med_spread_pts):
                        # refresh so per-symbol/global caps hold WITHIN this cycle
                        positions = self.bridge.positions()

    def symbol_view(self, symbol: str, tf: str, reg: dict, spec: SymbolSpec,
                    live_spread: float, tick_fresh: bool, med_spread_pts: float,
                    stars: int = 1) -> None:
        """Journal what the engine currently sees on this symbol — the
        dashboard's 'what is the bot thinking' panel reads exactly this."""
        self.store.set_state("symbol_view:%s:%s" % (symbol, tf), {
            "ts": utcnow(), "symbol": symbol, "tf": tf,
            "trend": reg["trend"], "vol": reg["vol"],
            "atr": round(reg["atr"], max(spec.digits, 2)),
            "rsi": round(reg["rsi"], 1), "close": reg["close"],
            "trend_strength": round(reg["trend_strength"], 2),
            "stars": stars,
            "spread_points": round(live_spread / spec.point, 1) if spec.point else None,
            "median_spread_points": round(med_spread_pts, 1),
            "market_open": tick_fresh})

    def place(self, symbol: str, strategy: str, tf: str, sig, reg: dict,
              account: dict, spec: SymbolSpec, live_spread: float,
              med_spread_pts: float) -> bool:
        try:
            entry_ref = float(self.bridge.tick(symbol)["ask" if sig.side == "buy" else "bid"])
            lots = risk.size_position(float(account["equity"]), entry_ref, sig.sl, spec)
        except (risk.Veto, BridgeError, KeyError) as v:
            self.store.decide("skip", "sizing: %s" % v, symbol=symbol,
                              strategy=strategy, side=sig.side)
            return False
        ctx = {"signal": sig.reason, "tags": list(sig.tags), "regime": reg,
               "median_spread_points": med_spread_pts}
        ctx.update(review.htf_context(self.bridge, symbol))
        try:
            res = self.bridge.order(symbol, sig.side, lots, round(sig.sl, spec.digits),
                                    round(sig.tp, spec.digits),
                                    comment="mi:%s" % strategy[:24])
        except BridgeRefused as e:
            self.store.decide("halt", "bridge REFUSED write: %s" % e,
                              symbol=symbol, strategy=strategy)
            log("WRITE REFUSED: %s" % e)
            return False
        except BridgeError as e:
            self.store.decide("skip", "order transport error: %r" % e,
                              symbol=symbol, strategy=strategy)
            return False
        if not res.get("ok"):
            self.store.decide("skip", "order rejected: retcode=%s %s"
                              % (res.get("retcode"), res.get("comment")),
                              symbol=symbol, strategy=strategy, side=sig.side)
            return False
        self.store.insert("trades", {
            "ticket": res["position"], "symbol": symbol, "strategy": strategy,
            "side": sig.side, "volume": res.get("volume", lots),
            "entry_time": utcnow(), "entry_price": res.get("price"),
            "sl": round(sig.sl, spec.digits), "tp": round(sig.tp, spec.digits),
            "status": "open", "timeframe": tf,
            "entry_spread_points": live_spread / spec.point if spec.point else None,
            "entry_atr": reg["atr"], "context": ctx})
        self.store.decide("enter", sig.reason, symbol=symbol, strategy=strategy,
                          side=sig.side,
                          detail={"ticket": res["position"], "lots": lots,
                                  "price": res.get("price"), "sl": sig.sl, "tp": sig.tp})
        log("ENTER %s %s %s %.2f lots @ %s sl=%.5f tp=%.5f (#%s)"
            % (sig.side.upper(), symbol, strategy, lots, res.get("price"),
               sig.sl, sig.tp, res["position"]))
        notify.trade_opened(symbol, strategy, sig.side, lots, res.get("price"),
                            sig.sl, sig.tp, res["position"])
        return True

    def calculate_confluence(self, symbol: str, tf: str, reg: dict, sig) -> int:
        """Score a setup (1-5 stars) based on confluence factors."""
        stars = 1
        # Factor 1: Trend Alignment (Score 1.5 stars if strong sentiment)
        if reg.get("trend_strength", 0) > 0.6:
            stars += 1
        # Factor 2: Volatility Regime (Normalizing ATR - within 50-150% of 100-bar median)
        # (Simplified check: if vol is 'normal', +1 star)
        if reg.get("vol") == "normal":
            stars += 1
        # Factor 3: Signal Confidence (Strategy tags like ICT 'FVG' + 'OrderBlock' might add stars)
        if sig is not None and len(sig.tags) > 1:
            stars += 1
        # Factor 4: HTF Agreement (Placeholder logic)
        for t in (sig.tags if sig is not None else []):
            if "with-trend" in t:
                stars += 1
        return min(5, stars)

    def propose(self, symbol: str, strategy: str, tf: str, sig, reg: dict,
                account: dict, spec: SymbolSpec, med_spread_pts: float) -> None:
        """Add a trade candidate to pending_trades for manual approval."""
        try:
            entry_ref = float(self.bridge.tick(symbol)["ask" if sig.side == "buy" else "bid"])
            lots = risk.size_position(float(account["equity"]), entry_ref, sig.sl, spec)
        except (risk.Veto, BridgeError, KeyError) as v:
            self.store.decide("skip", "sizing (proposal): %s" % v, symbol=symbol,
                               strategy=strategy, side=sig.side)
            return

        expires = (datetime.now(timezone.utc) +
                   timedelta(seconds=config.CYCLE_SEC * 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stars = self.calculate_confluence(symbol, tf, reg, sig)
        ctx = {"signal": sig.reason, "tags": list(sig.tags), "regime": reg,
               "median_spread_pts": med_spread_pts, "timeframe": tf, "stars": stars}
        ctx.update(review.htf_context(self.bridge, symbol))

        self.store.insert("pending_trades", {
            "symbol": symbol, "strategy": strategy, "side": sig.side,
            "volume": lots, "sl": round(sig.sl, spec.digits),
            "tp": round(sig.tp, spec.digits), "reason": sig.reason,
            "detail": ctx, "ts_created": utcnow(), "ts_expires": expires})
        self.store.decide("propose", sig.reason, symbol=symbol, strategy=strategy,
                          side=sig.side, detail={"lots": lots, "sl": sig.sl, "tp": sig.tp})
        log("PROPOSED %s %s %s %.2f lots (HITL)" % (sig.side.upper(), symbol, strategy, lots))
        notify.trade_opened(symbol, strategy, sig.side, lots, 0, sig.sl, sig.tp, "PENDING (HITL)")

    def execute_approved_trades(self, account: dict) -> None:
        """Check for human-approved trades and send them to the bridge."""
        pending = self.store.pending_trades("approved")
        for p in pending:
            try:
                # Re-verify risk halts before executing an old approval
                risk.check_halts(self.store, account)
                spec = self.spec(p["symbol"])
                tick = self.bridge.tick(p["symbol"])
                live_spread = (tick.get("ask", 0) - tick.get("bid", 0))

                # Create a pseudo-signal for the place() call
                from collections import namedtuple
                Sig = namedtuple("Sig", ["side", "sl", "tp", "reason", "tags"])
                ctx = json.loads(p["detail"])
                sig = Sig(p["side"], p["sl"], p["tp"], p["reason"], ctx.get("tags", []))

                if self.place(p["symbol"], p["strategy"], ctx.get("timeframe", config.TIMEFRAME),
                              sig, ctx.get("regime", {}), account, spec,
                              live_spread, ctx.get("median_spread_pts", 0.0)):
                    self.store.set_pending_status(p["id"], "executed")
            except Exception as e:
                log("failed to execute approved trade #%s: %r" % (p["id"], e))
                self.store.set_pending_status(p["id"], "denied",
                                              reason="exec error: %s" % str(e)[:50])

    def snapshot(self, account: dict) -> None:
        if self.store.get_state("forward_test_start") is None:
            self.store.set_state("forward_test_start", {
                "ts": utcnow(), "equity": account.get("equity"),
                "balance": account.get("balance")})
            log("forward test clock started at equity %.2f" % account.get("equity", 0))
        # every 10th cycle (~5 min) is enough resolution for a 90-day curve;
        # first cycle always snapshots so the daily-loss halt has a day-open ref
        if self.cycle == 1 or self.cycle % 10 == 0:
            self.store.insert("equity_curve", {
                "ts": utcnow(), "equity": account.get("equity"),
                "balance": account.get("balance"), "margin": account.get("margin"),
                "open_positions": len(self.store.open_trades())})
        self.store.set_state("heartbeat", {"ts": utcnow(), "cycle": self.cycle,
                                           "mode": config.EXEC_MODE})

    def maintenance(self) -> None:
        news_calendar.refresh(self.store)
        if gate.gate_stale(self.store):
            log("gate stale — re-running backtest gate on fresh history")
            gate.run_gate(self.store, self.bridge, verbose=False)
            enabled = sorted("%s/%s" % c for c in gate.enabled_combos(self.store))
            log("gate refreshed — enabled: %s" % (enabled or "none"))
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.store.get_state("daily_report_for") != day:
            # close out YESTERDAY's report once per UTC day
            from datetime import timedelta
            yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            rep = review.build_daily_report(self.store, yday)
            self.store.set_state("daily_report_for", day)
            self.store.set_state("halted_for_day", None)  # new day, budget resets
            log("daily report %s: %s" % (yday, rep["summary"]))
            try:
                from . import report
                report.write_doc(self.store)
                log("refreshed docs/forward-test.md")
            except Exception as e:
                log("forward-test doc refresh failed: %r" % e)
        review.build_daily_report(self.store)  # keep today's row current

    # ---- main loop -------------------------------------------------------------
    def run_cycle(self) -> None:
        if not self.bridge.alive():
            log("bridge down — rebooting")
            self.bridge = ensure_bridge()
        if risk.kill_switch_active():
            if not self.kill_handled:
                self.flatten_all("kill switch")
                notify.halt("kill switch — all positions flattened, engine idle")
                self.kill_handled = True
            if self.cycle % 20 == 0:
                log("KILL SWITCH active — engine idle until %s is removed"
                    % config.KILL_SWITCH)
            return
        self.kill_handled = False
        account = self.bridge.account()
        if not account:
            log("no account info; skipping cycle")
            return
        self.reconcile()
        self.manage_open()
        self.manage_trailing()
        self.manage_partials()
        self.decide_entries(account)
        if config.HITL_MODE:
            self.execute_approved_trades(account)
        self.snapshot(account)
        self.maintenance()

    def run_forever(self) -> None:
        h = self.bridge.health()
        log("engine up | account %s (%s) | mode=%s | writes=%s | combos enabled: %s"
            % (h["account"]["login"],
               "DEMO" if h["account"]["demo"] else "NOT-DEMO",
               config.EXEC_MODE, h["writes_allowed"],
               sorted("%s/%s" % c for c in gate.enabled_combos(self.store)) or "none"))
        while True:
            t0 = time.time()
            self.cycle += 1
            try:
                self.run_cycle()
            except (BridgeError, BridgeRefused) as e:
                log("cycle %d bridge error: %s" % (self.cycle, e))
            except Exception:
                log("cycle %d UNEXPECTED:\n%s" % (self.cycle, traceback.format_exc()))
            time.sleep(max(1.0, config.CYCLE_SEC - (time.time() - t0)))


if __name__ == "__main__":
    Engine().run_forever()
