# GUIDE — market-intel setup & operations

> Read-only by construction: no module in this repo imports a broker write API.
> The MT5 bridge script uses only `initialize / account_info / symbol_select /
> symbol_info_tick / copy_rates_from_pos` and hard-verifies the account is DEMO
> before writing anything.

## Architecture

```
            ┌──────────────────────────── every cycle (default 5 min) ───────────────────────────┐
            │                                                                                     │
 MT5 (Wine, │  collectors/mt5_pull.py ──► data/mt5_live.json ──► collectors/prices.ingest_mt5()  │
 demo, READ │                                                                    │                │
 ONLY)      │  CoinGecko free API ────► collectors/prices.pull_coingecko()       ▼                │
            │                                                             price_snapshots         │
            │  analysis/ta.py  (trend, vol regime, S/R, sweeps) ────► technical_signals           │
            │  analysis/synthesis.py (state + structure levels) ────► market_state               │
            │                                                                                     │
            │  every 3rd cycle: collectors/news.py (4 RSS feeds + trending)                       │
            │       ──► news_events ──► keyword sentiment ──► sentiment_scores                    │
            │                                                                                     │
            │  every cycle: heartbeat ──► system_health                                           │
            └─────────────────────────────────────────────────────────────────────────────────────┘
                                     │
                     store.py (SQLite ▲ or Supabase via PostgREST — same schema)
                                     │
                dashboard/server.py ──► http://127.0.0.1:8899 (read-only UI)
                ops/watchdog.py (systemd timer) ──► alert if heartbeat goes stale
```

## Setup from scratch

1. **Clone + self-test** (Python 3.10+, numpy is the only third-party package,
   and only for the analysis layer):
   ```bash
   git clone https://github.com/Kalahari-Labs/market-intel
   cd market-intel
   python3 store.py        # creates data/intel.sqlite from migrations/, round-trips a row
   ```

2. **API keys** — copy `.env.example` to `.env` and fill in what you use.
   `.env` is git-ignored; never commit it. Everything works with **zero keys**
   (SQLite + keyless APIs); keys only add sources.

3. **Market data source** — either:
   - **MT5 via Wine** (what this was built on): a Wine prefix at `~/.mt5` with
     Windows Python 3.12 + the `MetaTrader5` package and a logged-in **demo**
     terminal. `collectors/mt5_pull.py` and the paths at the top of
     `collectors/prices.py` are where to adjust prefix/paths.
   - **No MT5?** The crypto + news layers still run; extend
     `collectors/prices.py` with any free OHLCV source and write to
     `price_snapshots` in the same shape.

4. **Migrations** — SQLite applies them automatically on first use. For
   Supabase: create a project, run everything in `migrations/` (SQL editor,
   `supabase db push`, or the Supabase MCP `apply_migration`), then set
   `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env` — the store switches
   backends automatically.

## Running

```bash
python3 runner.py --once     # single pass, prints each step
python3 runner.py            # 24/7 loop in the foreground
python3 dashboard/server.py  # dashboard on http://127.0.0.1:8899
```

**Supervised (recommended):**
```bash
cp ops/*.service ops/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now market-intel-runner market-intel-dashboard market-intel-watchdog.timer
loginctl enable-linger $USER          # keep running after logout/reboot
```

Stop / logs:
```bash
systemctl --user stop market-intel-runner market-intel-dashboard
journalctl --user -u market-intel-runner -f     # or tail -f logs/runner.log
```

## Reading the dashboard (http://127.0.0.1:8899)

- **Header dot** — green: runner heartbeat fresh (<15 min); red: stale.
- **Live prices** — newest tick per symbol with source and timestamp.
- **Market state cards** — per symbol: trend, vol regime, S/R, sweep flag,
  sentiment. The *structure points* block (level → next level, invalidation,
  recent-window hit-rate) is **descriptive**: it reports the structure the
  analysis sees and how often similar moves resolved in the last 300 bars.
  It is not advice, and there is nothing on the page that can act on it.
- **Technical signals** — the raw descriptive layer per symbol × timeframe.
- **News / sentiment** — deduped headlines and the keyword-rule score
  (−1…+1) per symbol over 24 h.
- **System health** — last heartbeats per component.
- **Research registry** — the actual backtest/robustness/walk-forward results
  (all negative) that justify keeping execution off.

## Data sources & free-tier limits

| Source | What | Limit | Key |
|---|---|---|---|
| MT5 demo (Wine bridge) | FX/gold/index/oil OHLCV + ticks | broker feed, no API limit | none (demo login) |
| CoinGecko `/simple/price`, `/search/trending` | BTC/ETH spot, trending | ~30 req/min keyless; we use 2/cycle | none |
| MarketWatch RSS | top stories | public feed; 1 req/15 min | none |
| CNBC Markets RSS | market news | public feed; 1 req/15 min | none |
| FXStreet RSS | FX news | public feed; 1 req/15 min | none |
| Cointelegraph RSS | crypto news | public feed; 1 req/15 min | none |
| NewsAPI (optional) | business headlines | free tier 100 req/day | `NEWSAPI_KEY` |

If a feed dies, the step logs to `system_health`, the loop continues, and
three consecutive failures raise an alert (log + optional webhook).

## Configuration (.env)

| Var | Default | Meaning |
|---|---|---|
| `INTEL_CYCLE_SEC` | 300 | seconds between collection cycles |
| `INTEL_NEWS_EVERY_N` | 3 | pull news every Nth cycle |
| `INTEL_ALERT_AFTER` | 3 | consecutive step failures before alerting |
| `INTEL_ALERT_WEBHOOK` | — | POST target for alerts (Slack/n8n/etc.) |
| `INTEL_STALE_MIN` | 20 | watchdog staleness threshold (minutes) |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | — | switch storage to your Supabase project |
| `NEWSAPI_KEY` | — | enable NewsAPI source |
