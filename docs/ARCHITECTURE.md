# Kalahari Labs MT5 — Architecture

> Status: living document. Phase 1 (architecture stabilization) in progress.
> This file describes **what exists today** honestly, and the **target** we are
> migrating toward incrementally. It never describes aspirational code as if it
> were built.

---

## 1. How to read this repository

The repository grew in phases and today contains **three internally-coherent
subsystems** plus a new **shared contract layer** (`core/`) that is being
introduced to unify them without rewrites.

| Subsystem | Path | Role | Runtime | Tests |
|---|---|---|---|---|
| **1. Research layer** | repo root (`backtest.py`, `portfolio.py`, `walkforward.py`, `robustness.py`, `risk.py`, `execution.py`, `registry.py`, `strategies/`) | Offline backtest, walk-forward, Monte-Carlo, portfolio research + demo-execution proof-of-concept | numpy + stdlib | `tests/` — 100 unittest tests |
| **2. Intelligence plane** | `intel/` (`runner.py`, `collectors/`, `analysis/`, `store.py`, `dashboard/`, `ops/`) | Read-only 24/7 market observation → `intel.sqlite` | numpy + stdlib | embedded `--once` pass |
| **3. Live executor** | `intel/executor/` | **Canonical** autonomous trader: gate → decide → risk → demo-guarded MT5 order | numpy + stdlib | `intel/executor/selftest.sh` |
| **4. Shared contracts** | `core/` | Dependency-free interfaces the three subsystems converge on | stdlib only | `tests/test_core_contracts.py` |

**Dependency reality:** despite historical entries in `requirements.txt`, no
module imports `pandas`, `pandas-ta`, or `backtesting`. Everything runs on
**numpy + the Python standard library**. `pyproject.toml` (`numpy` only) is the
truthful runtime contract. Optional integrations (`MetaTrader5`, `supabase`,
`python-dotenv`) are always imported behind guards.

---

## 2. Subsystem 3 — the live executor (canonical)

```
                     MT5 terminal (broker) — DEMO by default
                                  │  order_send / copy_rates (Windows Python)
                     ┌────────────┴─────────────────────────────┐
                     │ bridge_server.py — SERVER-SIDE SAFETY GATE│  only writer of MT5
                     │  • demo triple-gate, re-checked per write │
                     │  • naked-order refuse (sl+tp mandatory)   │
                     │  • volume clamp, protective-side check    │
                     │  • single-threaded → serializes MT5 calls │
                     └────────────┬─────────────────────────────┘
                        HTTP :8787 │  urllib client (bridge.py)
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ run.py  supervisor: reboots bridge / engine / dashboard with backoff        │
   │   └─ engine.py  cycle: health → kill → reconcile → manage → DECIDE →        │
   │                        snapshot → maintenance                               │
   │        ├─ strategies.py   10 plugins  decide(bars,i) → Signal(sl,tp,reason) │
   │        ├─ analysis.py     causal indicators (EMA/RSI/ATR/BB/Donchian/MACD)  │
   │        ├─ gate.py         OOS + segment-stability backtest gate             │
   │        ├─ backtester.py   event-driven, intrabar SL-first, swap, live sizing│
   │        ├─ risk.py         veto layer (sizing, daily/DD halt, cooldown, ...) │
   │        ├─ review.py       post-mortem taxonomy → lessons + cooldown/disable │
   │        ├─ news_calendar.py  ForexFactory blackout · notify.py phone alerts  │
   │        └─ store.py        executor.sqlite (trades/decisions/equity/status)  │
   │   └─ dashboard.py  11-panel dark UI + /api/* JSON   ·  onboard.py = doctor  │
   └────────────────────────────────────────────────────────────────────────────┘
```

**Safety invariant (do not weaken):** live trading is disabled by default and
gated **server-side** in `bridge_server.py`. A REAL account only accepts writes
when all three hold — `MI_ALLOW_LIVE=1`, an `ALLOW_LIVE` file present, and its
contents equal to the account login — re-verified before *every* order. No
client-side bug can place a live order.

**Decision path today:** the gate (`gate.py`) enables a `(strategy, symbol)`
combo only after out-of-sample + segment-stability backtest thresholds pass;
`engine.decide_entries` runs each enabled strategy independently and every
entry must clear the `risk.py` veto stack. This is a *gate + independent entry*
model, not yet a central voting/decision engine (see §6 target).

---

## 3. Subsystem 2 — the intelligence plane (`intel/`)

`runner.py` runs a resilient 24/7 loop (per-step try/except, heartbeats,
escalating alerts) that pulls MT5 (read-only), CoinGecko, and RSS/NewsAPI, then
computes descriptive TA (`analysis/ta.py`) and market-state synthesis into
`intel.sqlite` (or Supabase). **Hard boundary:** it imports nothing from
`execution.py`/`risk.py` and sends no orders.

---

## 4. Subsystem 1 — the research layer (repo root)

Pure numpy research rig, fully unit-tested:

- `backtest.py` — event-loop simulator with an explicit, named cost model
  (spread / commission / slippage / directional swap), next-open fills, no
  look-ahead.
- `walkforward.py` — rolling in-sample/out-of-sample validation.
- `robustness.py` — parameter-robustness surface + Monte-Carlo.
- `portfolio.py` — multi-sleeve, risk-equalised aggregation.
- `risk.py` — `RiskManager` (the only order approver): fixed-% sizing, daily
  loss cap, max open positions, kill switch.
- `execution.py` — `Executor`: demo-only, dry-run by default, hard-refuses any
  non-DEMO account before an order is even constructed.
- `strategies/` — `Strategy.generate(close, **params) → Signals(regime, entries)`.
- `registry.py` / `journal.py` — results registry + append-only research journal.

---

## 5. Subsystem 4 — the shared contract layer (`core/`) — NEW

`core/` is a **dependency-free** package (stdlib `typing`, `dataclasses`,
`enum` only) that defines the interfaces the three subsystems converge on. It
contains **no behavior** — only contracts — so importing it can never change
runtime behavior or pull in new dependencies.

| Contract | Kind | Satisfied today by | Purpose |
|---|---|---|---|
| `core.MarketDataProvider` | `@runtime_checkable` Protocol | executor `Bridge` (`bars`, `tick`, `symbol`) | Uniform market-data reads |
| `core.BrokerAdapter` | `@runtime_checkable` Protocol | executor `Bridge` (`account`, `positions`, `order`, `close`, `modify`, `alive`) | Broker independence (Phase 2) |
| `core.RiskManager` | `@runtime_checkable` Protocol | root `risk.RiskManager` (`evaluate`) | One risk-approval shape |
| `core.Strategy` | `@runtime_checkable` Protocol | every strategy in both subsystems (`name`) | Plugin identity |
| `core.Recommendation` | dataclass (new) | — (produced in Phase 5) | Strategy output: side + confidence + reasoning + metadata |
| `core.Decision` / `core.Action` | dataclass + enum (new) | — (produced in Phase 6) | Central decision: BUY / SELL / WAIT / IGNORE + explanation |

**Migration policy.** Contracts are structural Protocols, so existing classes
conform **as-is** — the migration adds no runtime coupling, and
`tests/test_core_contracts.py` fails CI if any existing implementation ever
drifts from a contract. New code (Phase 2 adapters, Phase 6 decision engine)
imports `core` directly. We migrate *toward* the contracts; we never rewrite a
working subsystem to satisfy them in one step.

---

## 6. Target architecture (incremental, by phase)

The end state is one **autonomous trading operating system** whose engines are
modular, testable, observable, and replaceable — reached without rewriting the
working executor.

```
 MarketDataProvider ──► Intelligence (features + confidence) ──► Strategies
        │                                                            │ Recommendation
        │                                                            ▼
        └────────────► Decision Engine ◄──── Risk / Portfolio / News / Regime
                              │  BUY | SELL | WAIT | IGNORE  (+ explanation)
                              ▼
                      BrokerAdapter (MT5 | Paper | …) ──► Journal ──► Continuous learning
```

| Phase | Goal | Touches |
|---|---|---|
| **1** | Stabilize: `core/` contracts, manifest truth, CI | `core/`, `docs/`, manifests, `.github/` |
| **2** | `BrokerAdapter` seam: `MT5BrokerAdapter`, `PaperBrokerAdapter` | `core/`, executor `bridge` |
| **3** | Account profiles (demo/paper/live) via `--profile`; live stays gated | config, `run.sh` |
| **4** | FastAPI (`/health /status /account /portfolio /positions /signals /risk /metrics /journal`) over existing storage; dashboard consumes it | new `api/` |
| **5** | Strategies emit `Recommendation` (signal + confidence + reasoning + metadata) | strategies |
| **6** | Central `Decision` engine (regime, vol, spread, news, exposure, drawdown, session, account) | new decision engine |
| **7** | Weekly learning reports (best/worst strategy, symbol, rejection & loss patterns); AI analyzes, never trades | `review`/reports |

**Non-negotiables across all phases:** never break passing tests; never remove
a safety check; never bypass the live gate; never fabricate market data or
metrics; keep the repo deployable after every commit.
