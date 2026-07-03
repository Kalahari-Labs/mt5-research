# GUIDE.md — Operator's Manual

How to run, extend, and trust this rig — from a fresh machine to a demo forward-test,
and what would have to be true before real money is ever discussed.

**What this is:** a disciplined, demo-only MT5 trading research system. Deterministic
rule-based strategies, an honest cost model, out-of-sample validation, a results
registry that counts every hypothesis ever tested, and a guarded execution pipeline
that has placed and closed a real demo order end-to-end.

**What this is NOT:** an autonomous live-trading bot. No LLM makes trading decisions.
Nothing trades real money — the execution module hard-refuses non-demo accounts in
two places by design.

---

## 1. Current status (2026-07-02)

| phase | question | verdict |
|---|---|---|
| 0–0.75 | build honest rig: costs, walk-forward, robustness, registry | ✅ rig works; SMA baseline loses (as expected) |
| 3 | ts_momentum D1 EURUSD | real but thin: +1.25%/yr gross, PLATEAU, OOS PF 1.18 |
| 4 | cross-asset portfolio + symmetric swap | ❌ financing erases the edge; every sleeve net-negative |
| 4b | directional swap (real broker quotes, triple-swap Wed) | built; hash-regression clean; D1 portfolio still negative (OOS Sharpe −0.23) |
| 5 | short-hold H4 momentum vs financing | ❌ **NO EDGE** — mechanism doesn't exist (see SHORTHOLDS.md); OOS Sharpe −0.73…−0.92 |
| — | execution plumbing | ✅ **proven live on demo** (ticket 794897548, round trip, retcode 10009; found+fixed silent-rejection bug) |

**Gate status: SHUT.** 29 distinct configs have been evaluated out-of-sample; none is
tradeable net of real costs. The infrastructure is ready; the edge is not found yet.

---

## 2. Setup from scratch

### The two-python reality
This box runs **two Pythons**:
- **Linux python3** — runs all research code (numpy + stdlib only; PyPI unreachable
  here, so no pandas/pytest — `requirements.txt` lists the canonical stack for a
  networked machine).
- **Windows Python 3.12 under Wine** (`WINEPREFIX=$HOME/.mt5`) — the only python
  that can import the official `MetaTrader5` package. Used ONLY by `tools/*.py`
  dumpers and the round-trip test.

### One-time setup
1. Install MT5 terminal under Wine (`~/.mt5/drive_c/Program Files/MetaTrader 5/`),
   log in to a **DEMO** account (this rig: XMGlobal-MT5 demo `336582315`).
2. Install Windows Python 3.12 in the same prefix and `pip install MetaTrader5`
   (from a machine/network that can reach PyPI, or a wheel copied over).
3. Clone this repo. Bar data is **not** in git (broker data, `.gitignore`d) — dump
   it fresh (next section).
4. Optional: copy `.env.example` → `.env` and override anything in `config.py`.

### Verify
```bash
python3 -m unittest discover -s tests        # 100 tests, zero installs
python3 registry.py list                     # results registry (empty on fresh clone)
```

---

## 3. Data workflow

All dumpers run through Wine, verify the account is DEMO before writing, and cache
CSVs into `data/` where `data.py` reads them by `SYMBOL`/`TIMEFRAME_MIN`:

```bash
# H1/D1 single symbol (SMA + momentum reference series)
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine 'C:\Program Files\Python312\python.exe' \
  'Z:\home\flowdaaddy\mt5-research\tools\mt5_dump.py' EURUSD 1440 15000

# Phase-4 D1 basket (case-sensitive symbols, downward depth probing)
... 'Z:\...\tools\dump_basket.py'

# Phase-5 H4 basket + REAL swap specs (swap_long/swap_short/mode/triple-day)
... 'Z:\...\tools\dump_h4.py'
```

Gotchas learned the hard way:
- `copy_rates_from_pos` **blocks or fails** when asking for more bars than the
  terminal has downloaded → the dumpers probe **downward** through a ladder with
  thread timeouts. Never request a deep range blind.
- Broker symbol names are **case-sensitive** (`US500Cash`); the MCP `get_bars`
  uppercases names and breaks them — use the dumpers for data, MCP for peeking.
- `data/EURUSD_60.csv` and `data/EURUSD_1440.csv` are the **regression reference
  series** — never re-dump over them casually; prior-phase numbers and registry
  hashes assume them byte-identical.

---

## 4. The research discipline (how a strategy earns its way forward)

**Nothing is evaluated without a written brief first** — hypothesis, configs fixed
a priori, kill criteria, gate criteria. Then, in order:

```bash
python3 backtest.py        # legacy-vs-realistic cost audit for the configured strategy
python3 robustness.py      # param surface → SPIKE / PLATEAU / NO-EDGE (in-sample shape)
python3 walkforward.py     # rolling IS-optimise → OOS test; pooled OOS is the number
python3 portfolio.py       # cross-asset sleeves, equal-risk weights, portfolio WF (D1)
python3 shortholds.py      # Phase-5 H4 runner (decomposition + kill criterion)
python3 registry.py list   # every run, content-hashed; duplicates flagged
python3 registry.py count  # multiple-testing counter — the luck budget
```

Non-negotiables baked into the code:
- **Costs are explicit** (`config.CostModel`): spread, per-lot commission, slippage,
  next-bar-open fills, and financing — either the conservative symmetric drag or the
  **directional model** (real per-side `swap_long`/`swap_short` from
  `data/{SYM}_swap.json`, triple-swap Wednesday 3×, weekend nights free).
  `cost_for(symbol, swap_model="directional")` builds a sleeve's full stack.
- **No look-ahead**: signals form on close, fill at next open; walk-forward OOS is
  strictly after IS; weights are estimated causally.
- **Every OOS-touched config is counted** (`registry.py count`). At 29 configs,
  ~1.5 would look like winners by pure luck at 5% — a "winner" needs a much higher
  bar than p<0.05 eyeballing.
- **Reference numbers are regression-guarded** by the test suite AND by registry
  content hashes (SMA −13.0427%/328 trades; momentum +47.9931%/219 trades).

Add a strategy = one file in `strategies/` + one `register()` line (see README §Add
a new strategy). Then write its brief before running anything.

---

## 5. Execution pipeline & safety model

```
signal → risk.py (THE ONLY approver: sizing, caps, kill switch)
       → execution.py Executor.submit()
           guard 1: account_info() must be DEMO — else REFUSED_LIVE, no order built
           flags:   EXECUTION_ENABLED=false → DISABLED (default)
                    DRY_RUN=true            → DRY_RUN  (default)
           guard 2: sender re-reads account at send time — non-DEMO raises
           send:    filling mode chosen from the SYMBOL's advertised flags
                    (XM EURUSD is FOK-only; hardcoded IOC gets retcode 10030)
           verify:  retcode must be 10009 (DONE) — else REJECTED_BROKER
       → journal.py (append-only audit: every decision, rejection, fill)
```

Statuses: `REFUSED_LIVE | REJECTED_RISK | DISABLED | DRY_RUN | REJECTED_BROKER |
ERROR | SENT`. The `REJECTED_BROKER` check exists because the first live plumbing
test caught `order_send` rejections passing silently as SENT — **found on demo,
fixed, unit-tested** (`tests/test_execution.py::TestBrokerVerification`).

### Prove the pipe (round-trip test)
```bash
# Requires: terminal running, AutoTrading ON (Ctrl+E in the MT5 window — retcode
# 10027 means it's off; it is a client-side toggle, not an API flag)
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine 'C:\Program Files\Python312\python.exe' \
  'Z:\home\flowdaaddy\mt5-research\tools\demo_roundtrip.py'
```
It sizes ~min-lot through `risk.py`, submits through the full guarded pipeline,
closes immediately, and prints the round-trip cost — which should match the
backtest cost model's spread+commission (it did: −$0.19 on 0.01 lots EURUSD).

---

## 6. Demo forward-test protocol (LOCKED until a strategy passes the gate)

**The gate:** portfolio Sharpe **≥ ~0.5 net of ALL costs on pooled out-of-sample**,
with a PLATEAU robustness verdict, from a pre-registered brief. No config has ever
passed it. When one does:

1. **Freeze the config** — params, basket, cost model, and code commit hash logged
   in the registry before the first live-demo signal.
2. Run signal→execution on the demo account with `EXECUTION_ENABLED=true`,
   `DRY_RUN=false`, minimum risk sizing (`RISK_PER_TRADE_PCT` small).
3. **Duration:** minimum 3 months / 30 trades, whichever is later.
4. **Weekly reconciliation:** live fills vs model assumptions — spread paid,
   slippage, swap charged (check the broker's actual rollover lines vs
   `CostModel`), and equity curve vs backtest expectation.
5. **Kill rules:** stop the test if drawdown exceeds 1.5× the backtest maxDD, or
   realized costs exceed modelled costs by >50%, or the broker changes swap terms
   materially.
6. Only a forward test that **matches its backtest within costs** graduates to the
   real-money conversation.

## 7. Real-money policy (read this twice)

**Current answer: NO — and not because of caution theatre.** There is no
positive-edge strategy in this repo. Trading real money today would simply donate
spread + swap to the broker with extra steps. The demo→real path is:

```
brief → backtest → robustness PLATEAU → pooled OOS Sharpe ≥ 0.5 → demo forward
test matches model ≥ 3 months → THEN a deliberate, reviewed decision
```

The code enforces the demo boundary **structurally**: two independent guards refuse
non-demo accounts, and there is deliberately no flag, env var, or config switch
that enables live trading. Going real would require *editing execution.py* — that
is intentional friction. If that day comes, the change must add (not remove)
safeguards: hard per-order and per-day loss caps in account currency, position
limits, an independent monitor process, broker-side stop-losses on every order,
and starting capital you can lose entirely without consequence. Leverage 1:1000 on
the demo account is a marketing number — real sizing comes from `risk.py`, never
from available margin.

---

## 8. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `retcode=10027 AutoTrading disabled by client` | terminal toggle off | Ctrl+E in the MT5 window (button turns green) |
| `retcode=10030 Unsupported filling mode` | broker doesn't accept the filling type | fixed — sender now reads `symbol_info().filling_mode`; if seen again, broker changed flags |
| `SENT` but no position | pre-fix behaviour (unverified retcode) | can't happen since the `REJECTED_BROKER` check; if it does, check `journal.py` trail |
| `copy_rates` hangs / returns None | requesting deeper history than downloaded | use the dumpers' downward probe; never raw deep requests |
| `FileNotFoundError: No cached data` | fresh clone (CSVs aren't in git) | run the dumpers (§3) |
| `load_swap_spec` raises | no `{SYM}_swap.json` | run `tools/dump_h4.py` |
| MCP `get_symbol_info` fine but `get_bars` fails on CFDs | MCP uppercases symbol names | use dumpers for case-sensitive symbols |
| tests fail on reference numbers | reference CSVs were overwritten | restore `EURUSD_60.csv` / `EURUSD_1440.csv` from backup/re-dump and verify hashes |

## 9. Command cheat sheet

```bash
python3 -m unittest discover -s tests   # full suite (100)
python3 backtest.py                     # cost audit for configured strategy
python3 robustness.py                   # param surface + verdict
python3 walkforward.py                  # OOS validation
python3 portfolio.py                    # D1 cross-asset portfolio (Phase 4)
python3 shortholds.py                   # H4 short-hold study (Phase 5)
python3 registry.py list 30             # recent runs + hashes
python3 registry.py count               # multiple-testing budget
# Wine side:
#   tools/mt5_dump.py SYMBOL TF BARS    # single-symbol dump
#   tools/dump_basket.py                # D1 basket
#   tools/dump_h4.py                    # H4 basket + swap specs
#   tools/demo_roundtrip.py             # guarded live-demo round trip
```
