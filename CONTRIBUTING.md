# Contributing to mt5-research

Thanks for reading this before opening a PR. This project's whole value
proposition is that it does not lie to itself about edge or safety — that
only holds if contributions keep the same discipline. If you haven't yet,
read [README.md](README.md) and [GUIDE.md](GUIDE.md) first; this file assumes
you know what the research plane and the executor plane are.

## Branch strategy

- `main` is protected. Nobody pushes to it directly, including maintainers.
- Work happens on feature branches (`fix/...`, `feat/...`, `research/...` are
  all fine names), opened as a PR against `main`.
- Every PR runs CI (`.github/workflows/ci.yml`):
  - the root suite, `python3 -m unittest discover -s tests`, on Python 3.10
    and 3.12 — it must stay green, and **the test count only ever goes up**.
    A PR that reduces the number of passing tests (by deleting or skipping
    one) needs a very good reason in the description, not just a green
    checkmark.
  - `intel/executor/selftest.sh` — the executor's own self-tests.
- Squash or rebase before merge is fine; force-pushing your own feature
  branch is fine; force-pushing `main` is never fine.

## Test rules

- **Every behavior change ships a test.** If you touched `risk.py`,
  `execution.py`, a strategy, or anything in `intel/executor/`, there should
  be a new or updated test that would have failed before your change.
- **Regression reference numbers are sacred.** `tests/` and `data/EURUSD_*.csv`
  anchor exact figures other tests and the results registry assume are
  byte-identical — for example the SMA regression guard (−13.0427% / 328
  trades) and the momentum regression guard (+47.9931% / 219 trades) in
  `tests/test_portfolio.py`. Never re-dump `data/EURUSD_60.csv` or
  `data/EURUSD_1440.csv` "just to refresh them" — that silently invalidates
  every prior-phase number and every registry content hash that assumes them
  unchanged. If a broker dump genuinely needs to change, that's a deliberate,
  called-out decision in the PR description, not a side effect.
- Guards you should not weaken: the SMA refactor guard, the truncation
  (no-look-ahead) invariance test, the swap-strictly-reduces-P&L guard, and
  the multiple-testing counter dedup tests in `tests/test_registry.py`. If a
  change makes one of these fail, the change is probably wrong, not the test.

## Research discipline

This repo burned 34 configurations across Phases 0–6 and found zero net
edge above the pre-registered 0.5 Sharpe gate. That track record only means
something because of the rules below — please don't shortcut them:

- **No strategy or config runs against out-of-sample data without a
  pre-registered brief first.** A brief fixes the hypothesis, the exact
  configs to be tried (not "and a few more if these don't work"), and the
  kill/gate criteria — all written down *before* the first out-of-sample run.
  `PHASE6.md` and `SHORTHOLDS.md` are real examples of the format; use one as
  a template.
- **Every OOS-touched config gets logged to `registry.py`.** That's what
  `ResultsRegistry.log_run(...)` and the `oos_configs` table are for. Run
  `python3 registry.py count` before and after your research session — the
  multiple-testing count it prints is the project's "luck budget": the more
  configs anyone tests, the more likely the best-looking one is noise, not
  edge. Don't dodge the counter by tweaking a param and calling it "the same
  config" — a new lookback, filter, or timeframe is a new config.
- **A "winner" needs to clear the pre-registered bar, not look interesting.**
  The bar today is pooled OOS Sharpe ≥ 0.5 net of all costs, from a PLATEAU
  (not SPIKE) robustness verdict, profitable in at least 2 of 3 sequential
  time slices. Phase 6's best result (Sharpe 0.28) is the closest anyone has
  gotten, and it is still reported as gate-not-met, not as a discovery.
- Costs are never optional in a research PR: spread, commission, slippage,
  and swap (symmetric or directional) must all be present in whatever
  `CostModel` the run uses. A "backtest" with an incomplete cost model is not
  evidence of anything.

## The two-planes rule

The repo root (research plane) and `intel/executor/` (executor plane) are
deliberately decoupled. PRs must preserve that:

- **Research plane** (`backtest.py`, `walkforward.py`, `robustness.py`,
  `portfolio.py`, `risk.py`, `execution.py`, `registry.py`, `strategies/`,
  `core/`): stdlib + `numpy` only — no new runtime dependencies, no
  network calls, no LLM in the decision loop anywhere. It must never import
  from `intel/`. This keeps the research layer fully offline, deterministic,
  and reproducible from a fresh clone plus cached CSVs.
- **Executor plane** (`intel/executor/`): `bridge_server.py` is the *only*
  file in the codebase permitted to call `order_send` (or import the
  `MetaTrader5` module for writes). If your change adds an order-sending
  code path anywhere else, it will be rejected regardless of how it's
  justified.
- **The demo/live write gate may never be weakened in a PR.** That means:
  never make the DEMO check optional, never add a way to skip the SL+TP
  requirement, never relax the triple-unlock for live (`MI_ALLOW_LIVE=1` +
  an `ALLOW_LIVE` file containing the exact account login + that login
  matching the terminal's logged-in account, re-verified per order), and
  never lower the out-of-sample gate thresholds in `gate.py` from a PR whose
  actual goal is "let my strategy trade." Thresholds change, if ever, as
  their own reviewed, explicitly-labeled PR — not bundled with a feature.
- `intel/` (the read-only intelligence plane) must keep importing nothing
  from `execution.py` or `risk.py`, and must never send an order. It's read
  and analysis only, by design.

## Database schemas

The executor persists everything to SQLite via `intel/executor/store.py`.
If you add a column or table, update the `SCHEMA` string there *and*
transcribe the change here. Current schema (`Store.__init__` runs this
`CREATE TABLE IF NOT EXISTS` script, plus two in-place `ALTER TABLE`
migrations for older DBs — `trades.timeframe` and `trades.partial_closed`):

```sql
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket INTEGER UNIQUE,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  entry_time TEXT, entry_price REAL,
  sl REAL, tp REAL,
  exit_time TEXT, exit_price REAL,
  pnl REAL, swap REAL, commission REAL,
  r_multiple REAL,
  exit_reason TEXT,             -- tp | sl | time_stop | friday_flat | kill | manual
  status TEXT DEFAULT 'open',   -- open | closed
  entry_spread_points REAL, entry_atr REAL,
  timeframe TEXT,               -- decision timeframe of the strategy (H1, M15, ...)
  partial_closed INTEGER DEFAULT 0, -- 1 if a partial TP was already taken
  context TEXT                  -- json: signal reason, regime, indicators at entry
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT, strategy TEXT,
  action TEXT NOT NULL,         -- enter | skip | exit | halt | manage
  side TEXT,
  reason TEXT NOT NULL,
  detail TEXT                   -- json
);
CREATE TABLE IF NOT EXISTS equity_curve (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  equity REAL, balance REAL, margin REAL, open_positions INTEGER
);
CREATE TABLE IF NOT EXISTS strategy_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL, symbol TEXT NOT NULL,
  status TEXT NOT NULL,         -- enabled | observing | cooldown | disabled
  reason TEXT,
  backtest TEXT,                -- json: full gate metrics (IS + OOS)
  UNIQUE(strategy, symbol) ON CONFLICT REPLACE
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  trade_id INTEGER,
  symbol TEXT, strategy TEXT,
  tag TEXT NOT NULL,            -- stopped_then_reversed | against_htf_trend | ...
  lesson TEXT NOT NULL,
  detail TEXT                   -- json
);
CREATE TABLE IF NOT EXISTS daily_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT UNIQUE,
  trades INTEGER, wins INTEGER, losses INTEGER,
  pnl REAL, win_rate REAL,
  equity_open REAL, equity_close REAL,
  best_trade REAL, worst_trade REAL,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS calendar_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_event TEXT NOT NULL,       -- event time UTC iso
  currency TEXT, impact TEXT, title TEXT,
  UNIQUE(ts_event, currency, title) ON CONFLICT IGNORE
);
CREATE TABLE IF NOT EXISTS engine_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated TEXT
);
CREATE TABLE IF NOT EXISTS pending_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  sl REAL, tp REAL,
  reason TEXT,
  detail TEXT,                  -- json: context/regime
  status TEXT DEFAULT 'pending', -- pending | approved | denied | expired | executed
  ts_created TEXT NOT NULL,
  ts_expires TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);
```

Table purposes, briefly: `trades` is one row per executor trade end-to-end;
`decisions` is every engine decision *including skips* (the "why" feed);
`equity_curve` is a per-cycle equity/balance/margin snapshot; `strategy_status`
is the latest gate result per strategy×symbol (this is what decides whether a
combo may trade); `lessons` is the post-mortem output per closed trade;
`daily_reports` is one row per trading day; `calendar_events` is the
ForexFactory high-impact news feed used for the news-blackout risk layer;
`engine_state` is a generic key/value store for halts, cooldowns, and
heartbeats; `pending_trades` backs human-in-the-loop approval mode.

The separate research-side registry (`registry.py`, used by the root
research plane, not the executor) lives in its own two tables — `results`
(one row per backtest/robustness/WF run) and `oos_configs` (one row per
distinct strategy+param combination ever evaluated OOS) — inside the journal
SQLite file configured by `config.JOURNAL`. Same idea, different plane: don't
conflate the two stores in a PR.

## How to run the stack

```bash
./run.sh check     # onboarding probe only — tells you what's missing
./run.sh gate      # backtest every strategy x symbol on YOUR broker's data
./run.sh observe   # full pipeline, journals every decision, sends NO orders
./run.sh           # autonomous trading (demo-gated server-side, always)
```

Before opening a PR that touches the executor, run at least `./run.sh check`
and `./run.sh gate` against a demo account so you know your change didn't
silently change gate outcomes. For research-plane changes:

```bash
python3 -m unittest discover -s tests   # must stay green; count only goes up
python3 backtest.py                     # cost audit for the configured strategy
python3 walkforward.py                  # OOS validation
python3 registry.py count               # multiple-testing budget, before AND after
```

If any of this is unclear, open a draft PR early and ask — a question in a
draft PR is cheaper than a discovered assumption three phases later.
