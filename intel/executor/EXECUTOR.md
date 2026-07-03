# executor — demo-gated autonomous trading plane

> **Read this before running anything.**
> Trading leveraged products can lose money faster than you expect. This
> software places real orders on whatever account the attached MT5 terminal is
> logged into. It ships locked to **demo accounts**. Nothing in this repository
> is financial advice, and no past backtest or demo result implies future
> profit. This project's own research (see the research registry on the intel
> dashboard) found that most simple strategies LOSE after real costs — that is
> exactly why the gate below exists. If you unlock a live account, every loss
> is yours alone.

**See it run:** [docs/SHOWCASE.md](../docs/SHOWCASE.md) — one full trade
lifecycle (open → live P&L → close → booked loss) captured on the dashboard.

## What it is

A disciplined, self-auditing execution loop on top of the market-intel
read-only plane:

```
                       ┌──────────────────────────────────────────────┐
 MT5 terminal (Wine)   │ bridge_server.py (Wine Python, HTTP :8787)   │
 logged into DEMO ◄────┤  * the ONLY file that imports order_send      │
                       │  * re-verifies DEMO before EVERY write        │
                       │  * refuses orders without SL+TP               │
                       │  * hard volume cap per order                  │
                       └───────────────▲──────────────────────────────┘
                                       │ localhost HTTP
        ┌──────────────────────────────┴───────────────────────────────┐
        │ engine.py — every 30s:                                        │
        │   reconcile broker-closed trades -> review.py (post-mortem)   │
        │   manage: time-stop, Friday-flat                              │
        │   on new closed bar: strategies -> risk vetoes -> order       │
        │   snapshot equity, refresh calendar, re-run gate when stale   │
        └──┬──────────────┬───────────────┬────────────────┬───────────┘
           │              │               │                │
      store.py       gate.py          risk.py         review.py
      (SQLite:       backtest 5000    sizing, halts,  finds its own
      trades,        REAL bars per    news blackout,  mistakes, writes
      decisions,     combo; only      spread guard,   lessons + memory,
      lessons,       OOS-profitable   cooldowns,      cools down / disables
      equity, ...)   combos trade     kill switch     losing combos
           │
      dashboard.py  http://127.0.0.1:8877  (read-only UI)
```

## The safety model (layered, server-side)

| Layer | Rule | Where |
|---|---|---|
| 1 | Writes refused unless account is DEMO (re-checked per order) | bridge_server.py |
| 2 | Orders without SL **and** TP refused | bridge_server.py |
| 3 | Volume > `MI_MAX_ORDER_VOLUME` (default 0.50 lots) refused | bridge_server.py |
| 4 | Live accounts: triple unlock required (below) | bridge_server.py |
| 5 | Strategy×symbol may trade only after passing the backtest gate OOS | gate.py |
| 6 | 0.5%-risk sizing, 2% daily loss halt, 10% drawdown halt | risk.py |
| 7 | News blackout ±30 min around high-impact events (ForexFactory) | risk.py |
| 8 | Cooldown after 3 consecutive losses; disable after 5 losses/7d | review.py |
| 9 | Kill switch: `touch executor/data/KILL` → flatten all + halt | engine.py |

**Live unlock (don't):** a REAL account only trades if ALL of these hold —
env `MI_ALLOW_LIVE=1`, a file `executor/ALLOW_LIVE` containing the exact
account login, and that login matching the logged-in account. Any mismatch =
read-only. This is deliberate friction. Demo results do not transfer 1:1 to
live (fills, slippage, spread widening, and your own interference all differ).

## The strategies

Ten pre-registered strategies, all behind the same contract: one causal
`decide(bars, i)` used identically by the backtester and the live engine, an
explicit SL + TP on every signal, and **no strategy trades until its
strategy×symbol combo passes the out-of-sample gate on your broker's own
data**. Expect most combos to be rejected — that is the system working.

| strategy | idea | timeframe |
|---|---|---|
| `trend_pullback` | EMA20/50 trend + RSI pullback resolution | H1 |
| `donchian_breakout` | channel breakout w/ ATR expansion + trend agreement | H1 |
| `meanrev_bb` | fade Bollinger extremes in flat regimes only | H1 |
| `fvg_retrace` | ICT fair value gap: retrace into an unfilled 3-candle imbalance | H1 |
| `liquidity_sweep` | ICT stop hunt: fade a failed sweep of the N-bar high/low | H1 |
| `orderblock_retest` | ICT order block: first retest after a displacement structure break | H1 |
| `london_breakout` | Asian-range breakout in the London window (server hours) | H1 |
| `momentum_macd` | MACD histogram flip with the EMA200 regime | H1 |
| `rsi2_meanrev` | Connors RSI(2) flush back to the EMA20 mean, with-trend | H1 |
| `scalp_ema_cross` | session scalper, EMA9/21 cross, tight ATR stops | M15 |

Param grids stay frozen on purpose: this repo's own research burned 29 configs
in walk-forward. New ideas enter the registry, pass the gate, or stay in
observe mode. There is no param-fishing path and no "500 strategies" — 500
guessed configs is how you overfit yourself into a smoking account.

## Phone notifications (optional)

Set `MI_NTFY_TOPIC` (keyless — [ntfy.sh](https://ntfy.sh) app) and/or
`MI_TELEGRAM_BOT_TOKEN` + `MI_TELEGRAM_CHAT_ID` in `.env`. Your phone buzzes on
every entry, close and halt. Delivery is fire-and-forget: a dead network can
never stall the trading loop, and the SQLite journal stays the source of truth.

## The honesty model

- **Backtest/live parity** — strategies expose one `decide(bars, i)` function;
  the backtester and the live engine call the *same code* at the same index.
- **No optimistic fills** — signals fill at next-bar open, buys pay the bar's
  own recorded spread, SL fills first when SL and TP share a bar, shorts exit
  at ask, swap is charged nightly with the Wednesday triple.
- **Gate before trade** — each strategy×symbol must show out-of-sample profit
  (last 30% of 5000 fresh broker bars) after all costs, AND be profitable in at
  least 2 of 3 sequential time slices (an edge that existed in only one slice
  is a streak, not an edge). Fail → observe mode: the full pipeline runs and
  journals, but no order is sent. The gate re-runs every 24h on fresh data and
  after protections fire.
- **Every decision journaled** — including every *skip*, with the veto reason.
  The dashboard renders only journaled rows and live bridge reads.
- **Self-review** — every closed trade gets a data-checked post-mortem
  (stop-too-tight, against-H4-trend, overpaid-spread, gap-through-stop,
  chop-entry) written to `lessons` and `executor/data/memory.json`.

## Run it

```bash
cd intel
./start.sh              # trade mode (demo-gated regardless)
./start.sh observe      # everything except order_send
```

Dashboard: http://127.0.0.1:8877 · Bridge health: http://127.0.0.1:8787/health
Logs: `logs/engine.log`, `logs/bridge.log`, `logs/dashboard.log`
Stop entries immediately: `touch executor/data/KILL` (remove file to resume).

Supervised 24/7:

```bash
cp ops/market-intel-executor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now market-intel-executor
loginctl enable-linger $USER
```

## Onboarding a new machine / account

Platform matrix:

| platform | terminal + bridge | engine + dashboard | guide |
|---|---|---|---|
| Windows | native (`pip install MetaTrader5`) | same Python | [docs/INSTALL-WINDOWS.md](../docs/INSTALL-WINDOWS.md) |
| Linux | Wine prefix | system Python 3.10+ | [docs/INSTALL-WINE-MT5.md](../docs/INSTALL-WINE-MT5.md) |
| macOS | Wine (brew) or a remote Windows box | system Python 3.10+ | both docs above |
| Docker | on the host (bridge can't live in the container) | container | `docker compose up -d` (see repo root) |

Linux/macOS steps:

1. **Wine + MT5 + Windows Python** — install a Wine prefix (default `~/.mt5`)
   with MetaTrader 5 and Windows Python 3.12, then
   `wine pip install MetaTrader5`. (Any broker with an MT5 demo works.)
2. **Log the terminal into a DEMO account** in the MT5 GUI and enable
   AutoTrading. The executor attaches to whatever the terminal is logged into
   — and refuses to write unless it is demo.
3. **Configure** — `cp .env.example .env`, adjust `MI_SYMBOLS` to your
   broker's symbol names (XM: `EURUSD,GBPUSD,USDJPY,GOLD`), tune risk knobs.
4. **Verify the plumbing read-only first**:
   ```bash
   python3 -m executor.bridge      # boots bridge, prints /health
   python3 -m executor.gate        # backtests all combos on YOUR broker's data
   ./start.sh observe              # run a while; watch the decision feed
   ```
5. **Then** `./start.sh`. It will only ever trade combos that passed the gate
   on your broker's own data.
6. Adding another account = another Wine prefix + terminal login; point
   `MI_WINEPREFIX` at it (one executor instance per account/prefix,
   `MI_BRIDGE_PORT`/`MI_DASH_PORT` must differ).

## Configuration reference

Every knob is an env var (or `.env` entry); defaults in `executor/config.py`.
Key ones: `MI_SYMBOLS`, `MI_TIMEFRAME` (H1), `MI_RISK_PER_TRADE_PCT` (0.5),
`MI_MAX_DAILY_LOSS_PCT` (2), `MI_MAX_DRAWDOWN_PCT` (10), `MI_MAX_ORDER_VOLUME`
(0.5), `MI_MAX_OPEN_POSITIONS` (2), `MI_MAX_HOLD_BARS` (48),
`MI_NEWS_BLACKOUT_MIN` (30), `MI_EXEC_MODE` (trade|observe), gate thresholds
`MI_GATE_*`.

## What this is NOT

- Not a money printer. The gate typically rejects most combos — on the
  reference XM demo it enabled 2 of 12. Expect long flat stretches; that is
  the system refusing bad trades, which is the entire point.
- Not latency-sensitive HFT. It decides once per closed H1 bar.
- Not unattended-live-ready. Demo is the product until months of live-forward
  demo evidence say otherwise — the daily reports are that evidence.
