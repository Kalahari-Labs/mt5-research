# FILL_MODEL.md — Fill & Cost Model Audit

Audit of the Phase-0 backtest engine (`backtest.py`) **as built**, then the
realistic defaults added in this upgrade. Every dimension is read from the actual
code (line refs are to the original Phase-0 `backtest.py`) and labeled
**OPTIMISTIC / REALISTIC / PESSIMISTIC**.

## Summary — original (Phase-0) engine
| Dimension | How it was handled | Label |
|---|---|---|
| Signal → fill timing | prior-bar signal (`regime[i-1]`) filled at next bar **open** | **REALISTIC** (no look-ahead) |
| Spread (bid/ask) | not modeled — fills at raw bar price | **OPTIMISTIC** |
| Commission | `0.00007` × notional per side (~0.7 bps) | modeled; magnitude OK but a proxy |
| Slippage | not modeled | **OPTIMISTIC** |
| Stop / exit checks | none; exit on signal flip at next open; no intrabar high/low | **OPTIMISTIC** (drawdown understated) |
| Position sizing | fixed fraction of equity (exposure=1.0), **not** `risk.py` | by design (documented) |
| **Net transaction cost** | spread = 0, slippage = 0 | **OPTIMISTIC overall** |

### 1. Signal → fill timing — REALISTIC ✅
`backtest.py:79` `desired = 0 if i == 0 else int(regime[i-1])` together with
`:82` `px = o[i]`. The signal formed on bar **N**'s close (`regime[N]`) is acted on
at bar **N+1**'s open. There is **no same-bar-close fill**, so **no look-ahead
bias**. This is the single most important thing to get right, and it was correct.

> The new `fill_timing="close"` switch exists ONLY to *demonstrate* the bias: it
> fills bar-N's signal at bar-N's close. The audit run prints what that inflated
> number would be. **Never use `"close"` for real numbers.**

### 2. Spread (bid/ask) — OPTIMISTIC
Entry (`:90-92`) and exit (`:82-84`) both fill at the raw bar price `o[i]` with no
half-spread added. MT5 bars are **bid**-based, so a real BUY fills at
`ask = bid + spread`. Modeled spread = **0 → optimistic**.

### 3. Commission — modeled (proxy), magnitude roughly REALISTIC
`:84`, `:93`, `:98` charge `abs(position) * px * commission` with
`commission = 0.00007` (`config.py:61`) = **0.7 bps of notional per side**
(~$7.6 per lot per side on EURUSD). It *is* a real, non-zero cost, but it is a
notional-fraction proxy rather than MT5's per-lot commission, and it was the
*only* cost in the engine. In isolation 0.7 bps/side is slightly high; combined
with zero spread and zero slippage the **total** cost is still optimistic.

### 4. Slippage — OPTIMISTIC
Not present anywhere in the engine. Modeled slippage = **0 → optimistic**.

### 5. Stop / exit checks — bar-close only, no intrabar — OPTIMISTIC (drawdown)
There are **no stops**. A position is held until the regime flips (`:81`), then
closed at the next bar's open (`:82`). The engine never inspects intrabar
`high`/`low`, so worst-case intrabar excursions and stop-outs are invisible →
**reported max drawdown is understated**. The exits themselves (on signal
reversal) are realistic for a pure reversal system; the gap is the absence of any
protective stop and of intrabar accounting.

### 6. Position sizing — fixed fraction of equity (unchanged, by design)
`:89-91` `notional = realized * exposure; size = notional / px` — fully invested
(exposure = 1.0), **not** `risk.py`'s %-risk sizing. **Deliberately left as-is**: a
fixed notional keeps the equity curve a function of the *strategy*, not the sizing
model. `risk.py` remains the sizing/approval authority for (demo) execution; the
backtest and the live sizer are intentionally separate concerns.

---

## Realistic defaults (this upgrade)
`config.py` now exposes an explicit, named `CostModel`. `REALISTIC_COSTS` is the
**new default** for `run()` and for the walk-forward — there are no implicit or
zero costs hidden in the engine anymore:

| Param | Default | Notes |
|---|---|---|
| `spread_pips` | **0.8** | ⚠ **Set this to YOUR broker's typical EURUSD spread** (XM demo varies through the session — check Market Watch). |
| `commission_per_lot` | **3.5** | account ccy, **per side**; representative ECN-style. Set to your account's real figure. |
| `slippage_pips` | **0.2** | adverse fill per side. |
| `fill_timing` | **"next_open"** | realistic; `"close"` = look-ahead, audit-only. |

`LEGACY_COSTS` reproduces the original Phase-0 numbers **exactly** (spread = 0,
slippage = 0, the old 0.7-bps commission proxy, next-open fills) so the audit
side-by-side is apples-to-apples.

### Porting note (canonical libraries)
`backtesting.py` models cost as a single `commission=` fraction of trade value and
has **no separate spread/slippage** concept — you fold them in. When porting,
sum `spread + 2·slippage` (in price) plus commission into that one fraction. Small
differences are expected: backtesting.py also fills at the next bar's open but
applies its commission symmetrically on trade value, whereas this model separates
the bid/ask half-spread on each fill. `pandas-ta`'s SMA equals the numpy rolling
mean used here, so indicator values are identical.

---

## Verdict on the original −12.18% (measured)

Both cost models, same 15,000 EURUSD H1 bars (`python3 backtest.py`):

| Metric | LEGACY (Phase-0) | REALISTIC (new default) | Δ |
|---|--:|--:|--:|
| Total return % | −12.18 | **−13.04** | −0.86 |
| Final equity | 8,781.86 | 8,695.73 | −86.13 |
| Trades | 328 | 328 | 0 |
| Win rate % | 35.37 | 35.37 | 0.00 |
| Profit factor | 0.812 | 0.799 | −0.013 |
| Expectancy/trade | −3.71 | −3.98 | −0.26 |
| Max drawdown % | −13.23 | −14.06 | −0.84 |
| Sharpe | −0.73 | −0.79 | −0.06 |

**Verdict: the original −12.18% was OPTIMISTIC.** Adding realistic spread (0.8p) +
slippage (0.2p) + per-lot commission moves it to **−13.04%** (Δ −0.86 pts) and
deepens max drawdown to −14.06%. The optimism is *mild* only because the old
0.7-bps-of-notional commission proxy happened to approximate total round-turn cost
— luck, not design: it modelled none of spread, slippage, or intrabar risk
explicitly. Set `spread_pips` / `commission_per_lot` to your broker's real figures
and it will move again.

> Note: per-trade win rate / PF / expectancy shifted slightly from the Phase-0
> report because each round-trip now carries BOTH its entry and exit commission.
> The equity-based figures (return / maxDD / Sharpe) are unchanged, and
> `sum(trade PnLs)` now reconciles exactly to the equity curve.

**Look-ahead control:** filling bar N's signal at bar N's own close
(`fill_timing="close"`, not executable) reports −13.81% — close to the realistic
−13.04% because 24h FX has ~no close→open gap. The bias is structurally larger on
gappy instruments; the engine defaults to `next_open` and offers `"close"` only to
demonstrate the trap.
