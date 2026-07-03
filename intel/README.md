# market-intel

Two planes, one honest system:

1. **Intel plane** (`collectors/ analysis/ dashboard/`) — read-only market
   intelligence, 24/7. No module in it imports a broker write API.
2. **Executor plane** (`executor/`) — a **demo-gated autonomous trading loop**
   added on top. It is the only place order code exists, the write path
   re-verifies the account is DEMO server-side before every order, and no
   strategy×symbol may trade until it passes an out-of-sample backtest gate on
   fresh broker data. Read [executor/EXECUTOR.md](executor/EXECUTOR.md) —
   including its risk disclosure — before running it. New machine?
   `python3 -m executor.onboard` checks every prerequisite with live probes.
   See a full trade lifecycle on the dashboard: [docs/SHOWCASE.md](docs/SHOWCASE.md).

**Honesty context you should know first:** this project's own research found
**29/29 momentum strategy configurations failed walk-forward validation under
realistic costs** (see `SHORTHOLDS.md` in the research repo and the registry
panel on the intel dashboard). The executor exists to trade *only* what
survives its gate on current data, to journal every decision including skips,
and to audit its own losses. On the reference demo account the gate enabled
2 of 12 combos. Expect it to refuse to trade a lot. That is the feature.
**Nothing here is financial advice; demo results do not imply live profits;
if you unlock a live account (deliberately hard), losses are yours.**

## What it does, 24/7

- **Collects** OHLCV + live ticks for FX majors, gold, indices, oil via a
  read-only MetaTrader 5 bridge, and BTC/ETH via CoinGecko's free API.
- **Analyzes** every symbol on H1/H4/D1: trend, volatility regime,
  support/resistance clustering, liquidity-sweep detection. Descriptive
  numbers and labels only — there are no buy/sell fields in the schema.
- **Reads the news**: MarketWatch, CNBC, FXStreet, Cointelegraph RSS + CoinGecko
  trending, with keyword-rule sentiment scoring per symbol.
- **Synthesizes** a per-symbol market state: structure levels, invalidation
  level, and an *empirical* recent-window hit-rate (a measurement, not a
  forecast).
- **Shows it live** on a local read-only dashboard, with the failed research
  verdicts displayed on the same page so the numbers stay honest.
- **Survives**: systemd services with auto-restart, per-step failure isolation,
  heartbeats, and a stale-heartbeat watchdog timer.

## Quick start

See [GUIDE.md](GUIDE.md) for full setup (schema migrations, your own API keys
via `.env` — never committed — and the process supervisor).

```bash
python3 store.py            # round-trip self-test (creates the local DB)
python3 runner.py --once    # one full collection+analysis pass
python3 dashboard/server.py # http://127.0.0.1:8899
```

## Storage

SQLite out of the box (zero dependencies). Point `SUPABASE_URL` +
`SUPABASE_SERVICE_KEY` at your own Supabase project and the same code writes
there instead; `migrations/0001_init.sql` reproduces the schema on a fresh
project.
