"""dashboard.py — executor dashboard on http://127.0.0.1:8877

Read-only window into the executor. Every number is either a row the engine
wrote to SQLite or a live read from the MT5 bridge — the dashboard itself
computes nothing and invents nothing. If the bridge is down the account panel
says so instead of showing stale numbers.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config
from .bridge import Bridge, BridgeError
from .store import Store, utcnow


def api_gate_proximity(store: Store) -> list:
    """For every strategy×symbol, return gate metrics and distance to threshold."""
    rows = store.strategy_statuses()
    result = []
    for r in rows:
        bt: dict = {}
        try:
            bt = json.loads(r.get("backtest") or "{}")
        except Exception:
            pass
        oos = bt.get("oos") or {}
        full = bt.get("full") or {}
        result.append({
            "strategy": r["strategy"], "symbol": r["symbol"],
            "status": r["status"], "reason": r["reason"],
            "oos_pf": oos.get("profit_factor"),
            "oos_exp_r": oos.get("expectancy_r"),
            "full_pf": full.get("profit_factor"),
            "full_n": full.get("n"),
            "max_dd": full.get("max_dd_pct"),
        })
    return result


# timeframes the chart panel may request; the bridge validates again server-side
CHART_TFS = ("M5", "M15", "M30", "H1", "H4", "D1")


def parse_chart_query(path: str) -> tuple[str, str, int]:
    """Validate /api/chart params. Symbol is whitelisted against the symbols
    the engine actually trades, tf against CHART_TFS, count clamped — the
    dashboard never forwards arbitrary strings to the bridge."""
    q = parse_qs(urlparse(path).query)
    symbol = q.get("symbol", [config.SYMBOLS[0]])[0]
    if symbol not in config.SYMBOLS:
        raise ValueError("symbol must be one of %s" % ",".join(config.SYMBOLS))
    tf = q.get("tf", ["M15"])[0]
    if tf not in CHART_TFS:
        raise ValueError("tf must be one of %s" % ",".join(CHART_TFS))
    count = max(20, min(int(q.get("count", ["180"])[0]), 500))
    return symbol, tf, count


def api_chart(store: Store, bridge: Bridge, symbol: str, tf: str,
              count: int) -> dict:
    """Everything the price-chart panel needs in one payload: raw bars from
    the MT5 bridge plus overlays (open positions, journaled trades, pending
    HITL proposals) — all read from the bridge or SQLite, computed nowhere."""
    out: dict = {"symbol": symbol, "tf": tf, "bars": [], "positions": [],
                 "trades": [], "pending": []}
    try:
        out["bars"] = bridge.bars(symbol, tf, count)
    except BridgeError as e:
        out["error"] = str(e)[:200]
        return out
    try:
        out["positions"] = [
            {k: p.get(k) for k in ("ticket", "type", "volume", "price_open",
                                   "price_current", "sl", "tp", "profit")}
            for p in bridge.positions() if p.get("symbol") == symbol]
    except BridgeError:
        pass
    out["trades"] = store.trades_for_chart(symbol)
    out["pending"] = [p for p in store.pending_trades()
                      if p.get("symbol") == symbol]
    return out


# hard cap on a manual dashboard order — a fat-fingered lot size should die
# at validation, not at the broker
MANUAL_MAX_LOTS = 1.0


def api_control(store: Store, body: dict) -> dict:
    """Human override switches. Everything is journaled. Nothing here can
    START trading autonomously — halt/kill only remove permission; resume/
    clear restore the normal (still demo-gated, still risk-vetoed) state."""
    action = body.get("action")
    if action == "halt":
        store.set_state("manual_halt", "paused from dashboard at %s" % utcnow())
        store.decide("halt", "manual pause from dashboard — no new entries")
    elif action == "resume":
        store.clear_state("manual_halt")
        store.decide("manage", "manual pause lifted from dashboard")
    elif action == "kill":
        config.KILL_SWITCH.touch()
        store.decide("halt", "KILL SWITCH pulled from dashboard — engine flattens and halts")
    elif action == "clear_kill":
        config.KILL_SWITCH.unlink(missing_ok=True)
        store.decide("manage", "kill switch cleared from dashboard")
    else:
        return {"ok": False, "error": "unknown action"}
    return {"ok": True}


def api_close_position(store: Store, bridge: Bridge, body: dict) -> dict:
    """Close one open position from the dashboard. The ticket must be a
    position the bridge can actually see right now; the close is journaled."""
    try:
        ticket = int(body.get("ticket"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "ticket must be an integer"}
    try:
        pos = [p for p in bridge.positions() if p.get("ticket") == ticket]
        if not pos:
            return {"ok": False, "error": "ticket %d is not an open position" % ticket}
        r = bridge.close(ticket, comment="mi-dashboard manual close")
        store.decide("exit", "manual close from dashboard",
                     symbol=pos[0].get("symbol", ""),
                     detail={"ticket": ticket, "result": r})
        return {"ok": True, "result": r}
    except BridgeError as e:
        return {"ok": False, "error": str(e)[:200]}


def api_manual_order(store: Store, bridge: Bridge, body: dict) -> dict:
    """A human trade ticket. Same pipe as the engine — the server-side
    demo gate in bridge_server.py guards this write like any other — and the
    same journal. Discipline still applies: whitelisted symbol, capped size,
    SL and TP mandatory. No stop, no order."""
    if not config.MANUAL_TICKET:
        return {"ok": False,
                "error": "manual ticket disabled (set MI_MANUAL_TICKET=1 to enable)"}
    symbol, side = body.get("symbol"), body.get("side")
    if symbol not in config.SYMBOLS:
        return {"ok": False, "error": "symbol must be one of %s" % ",".join(config.SYMBOLS)}
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "side must be buy or sell"}
    try:
        volume = float(body.get("volume"))
        sl = float(body.get("sl"))
        tp = float(body.get("tp"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "volume, sl and tp must all be numbers"}
    if not 0.01 <= volume <= MANUAL_MAX_LOTS:
        return {"ok": False, "error": "volume must be 0.01–%s lots" % MANUAL_MAX_LOTS}
    if sl <= 0 or tp <= 0:
        return {"ok": False, "error": "SL and TP are mandatory — no stop, no order"}
    try:
        r = bridge.order(symbol, side, volume, sl, tp, comment="mi-dashboard manual")
        store.decide("enter", "manual order from dashboard", symbol=symbol,
                     strategy="manual", side=side,
                     detail={"volume": volume, "sl": sl, "tp": tp, "result": r})
        return {"ok": True, "result": r}
    except BridgeError as e:
        return {"ok": False, "error": str(e)[:200]}


def api_summary(store: Store, bridge: Bridge) -> dict:
    out: dict = {"ts": utcnow(), "mode": config.EXEC_MODE,
                 "symbols": config.SYMBOLS,
                 "kill_switch": config.KILL_SWITCH.exists(),
                 "manual_halt": store.get_state("manual_halt"),
                 "halted_for_day": store.get_state("halted_for_day"),
                 "heartbeat": store.get_state("heartbeat"),
                 "gate_last_run": store.get_state("gate_last_run"),
                 "streaks": store.streaks(),
                 "rules": {
                     "exec_mode": config.EXEC_MODE,
                     "hitl": config.HITL_MODE,
                     "hitl_ttl_min": config.HITL_TTL_MIN,
                     "manual_ticket": config.MANUAL_TICKET,
                     "max_positions": config.MAX_OPEN_POSITIONS,
                     "risk_pct": config.RISK_PER_TRADE_PCT,
                     "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                     "max_dd_pct": config.MAX_DRAWDOWN_PCT,
                     "news_blackout_min": config.NEWS_BLACKOUT_MIN,
                     "max_spread_atr_frac": config.MAX_SPREAD_ATR_FRAC,
                     "trailing_stop_atr": config.TRAILING_STOP_ATR_MULT,
                     "partial_exit_r": config.PARTIAL_EXIT_R_MULT,
                 }}
    try:
        h = bridge.health()
        acct = bridge.account()
        out["bridge"] = {"up": True, "demo": h["account"]["demo"],
                         "writes_allowed": h["writes_allowed"], "gate": h["gate"],
                         "login": h["account"]["login"], "server": h["account"]["server"]}
        out["account"] = {k: acct.get(k) for k in
                          ("balance", "equity", "margin", "margin_free", "profit",
                           "currency", "leverage", "margin_level")}
        positions = [
            {k: p.get(k) for k in ("ticket", "symbol", "type", "volume",
                                   "price_open", "price_current", "sl", "tp",
                                   "profit", "swap", "comment")}
            for p in bridge.positions()]
        # MT5's own positions_get() already marks each position to market
        # (price_current) and carries live floating profit — this loop is a
        # read-only safety net for the rare position that arrives without a
        # price, nothing more. It mirrors the exact bid/ask convention
        # bridge_server.py itself uses to price a close (buy marks to bid,
        # sell marks to ask) so the number means the same thing everywhere.
        # It deliberately does NOT synthesize `profit` — that needs contract
        # size + currency conversion this dashboard doesn't have, and this
        # module invents nothing; a missing profit stays None (renders as —).
        ticks: dict = {}
        for p in positions:
            if p.get("price_current") is not None or not p.get("symbol"):
                continue
            sym = p["symbol"]
            if sym not in ticks:
                try:
                    ticks[sym] = bridge.tick(sym)
                except BridgeError:
                    ticks[sym] = {}
            t = ticks[sym]
            px = t.get("bid") if p.get("type") == 0 else t.get("ask")
            if px is not None:
                p["price_current"] = px
        out["positions"] = positions
    except BridgeError as e:
        out["bridge"] = {"up": False, "error": str(e)[:200]}
        out["account"] = None
        out["positions"] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "T00:00:00Z"
    out["stats_all"] = store.stats()
    out["stats_today"] = store.stats(since=today)
    ft = store.get_state("forward_test_start")
    if ft and ft.get("ts"):
        t0 = datetime.strptime(ft["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        eq_now = (out.get("account") or {}).get("equity")
        out["forward_test"] = {
            "start": ft["ts"][:10], "day": (datetime.now(timezone.utc) - t0).days + 1,
            "start_equity": ft.get("equity"),
            "return_pct": round((eq_now - ft["equity"]) / ft["equity"] * 100, 2)
            if eq_now and ft.get("equity") else None}
    else:
        out["forward_test"] = None
    return out


ROUTES = {
    "/api/equity": lambda s, b: s.equity_curve(),
    # what the engine saw on each symbol at its last closed bar (regime, spread,
    # market state) — written by engine.symbol_view, invented nowhere
    "/api/monitor": lambda s, b: s.symbol_views(),
    # active combo cooldowns (consecutive-loss protection) — another slice of
    # "what the bot is thinking right now" alongside the symbol monitor
    "/api/cooldowns": lambda s, b: s.cooldowns(),
    "/api/combos": lambda s, b: s.combos(),
    "/api/decisions": lambda s, b: s.recent_decisions(),
    "/api/trades": lambda s, b: s.recent_trades(),
    "/api/strategies": lambda s, b: s.strategy_statuses(),
    "/api/lessons": lambda s, b: s.recent_lessons(),
    "/api/reports": lambda s, b: s.daily_reports(),
    "/api/calendar": lambda s, b: s.upcoming_events(),
    "/api/pending": lambda s, b: s.pending_trades(),
    "/api/gate_proximity": lambda s, b: api_gate_proximity(s),
}

HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>market-intel executor</title>
<style>
:root{--bg:#0b0e14;--panel:#12161f;--line:#1e2430;--txt:#c8d0dc;--dim:#6b7686;
--green:#3ddc84;--red:#ff5470;--amber:#ffb02e;--blue:#4da3ff;--purple:#c084fc;}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;padding:14px}
h1{font-size:15px;margin-bottom:2px}
h2{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;display:flex;align-items:center;gap:8px}
h2 .badge{font-size:10px;padding:1px 6px;border-radius:10px;letter-spacing:0;text-transform:none;font-weight:bold}
.badge-ok{background:#123626;color:var(--green)}
.badge-warn{background:#2a2417;color:var(--amber)}
.badge-off{background:#1e2430;color:var(--dim)}
.badge-bad{background:#3a1420;color:var(--red)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);text-align:left;font-weight:normal;padding:3px 6px;border-bottom:1px solid var(--line)}
td{padding:3px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.pos{color:var(--green)}.neg{color:var(--red)}.dim{color:var(--dim)}.amb{color:var(--amber)}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:bold}
.tag.enabled{background:#123626;color:var(--green)}
.tag.observing{background:#2a2417;color:var(--amber)}
.tag.disabled,.tag.cooldown{background:#3a1420;color:var(--red)}
.kpi{display:flex;gap:20px;flex-wrap:wrap;margin:6px 0}
.kpi-item b{display:block;font-size:18px;line-height:1.2}
.kpi-item small{color:var(--dim);font-size:11px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:middle}
#topline{margin-bottom:12px;display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
#topline-left{flex:1}
#topline-right{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px}
#eq{width:100%;height:90px;display:block}
#eq circle{pointer-events:all}
#eq_legend{font-size:10px;margin-top:3px}
.warn{background:#3a1420;border:1px solid var(--red);border-radius:8px;
padding:8px 12px;margin-bottom:10px;color:#ffb3c0;font-size:12px}
small{color:var(--dim);font-size:11px}
.btn{cursor:pointer;border:none;border-radius:4px;padding:3px 8px;font-size:11px;font-weight:bold;margin-right:4px}
.btn-ok{background:var(--green);color:var(--bg)}
.btn-no{background:var(--red);color:#fff}
.btn:hover{opacity:.8}
.bar-wrap{display:inline-block;vertical-align:middle;width:60px;height:7px;
background:#1e2430;border-radius:3px;overflow:hidden;margin-left:4px}
.bar-fill{height:100%;border-radius:3px;transition:width .3s}
.rule-row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--line);font-size:12px}
.rule-row:last-child{border-bottom:none}
.rule-val{color:var(--txt);font-weight:bold}
.cbtn{cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--dim);
border-radius:4px;padding:2px 9px;font-size:11px;margin-right:3px;font-family:inherit}
.cbtn.on{background:#123626;color:var(--green);border-color:var(--green)}
.cbtn:hover{color:var(--txt)}
.btn-amb{background:var(--amber);color:var(--bg)}
#ticket select,#ticket input{background:var(--bg);border:1px solid var(--line);color:var(--txt);
border-radius:4px;padding:3px 6px;font:11px ui-monospace,monospace;margin:0 4px 4px 0;width:86px}
#ticket select{width:auto}
.ctl-row{display:flex;align-items:center;gap:8px;padding:4px 0;flex-wrap:wrap}
#chart{width:100%;height:280px;display:block;background:#0d1119;border-radius:6px}
</style></head><body>
<div id="topline">
 <div id="topline-left">
  <h1>market-intel executor <span id="acct" class="dim"></span></h1>
  <small>demo-gated autonomous executor — every number is journaled or read live from MT5 — no emotions, no overrides</small>
 </div>
 <div id="topline-right">
  <span id="badge_mode" class="badge"></span>
  <span id="badge_enabled" class="badge"></span>
  <span id="badge_engine" class="badge"></span>
 </div>
</div>
<div id="alerts"></div>
<div class="grid">

<div class="panel"><h2>Account <span id="live_ind" class="badge badge-off">connecting&hellip;</span></h2>
<div class="kpi" id="kpi_acct"></div>
<svg id="eq"></svg>
<div id="eq_legend" class="dim"></div></div>

<div class="panel"><h2>Performance</h2>
<div class="kpi" id="kpi_perf"></div>
<div class="kpi" id="kpi_today"></div>
<div id="fwd" class="dim" style="margin-top:6px;font-size:12px"></div></div>

<div class="panel"><h2>Discipline rules — enforced every cycle</h2>
<div id="rules_panel"></div></div>

<div class="panel"><h2>Manual controls — human overrides, all journaled</h2>
<div id="controls"><span class="dim">loading&hellip;</span></div>
<div id="ticket_wrap" style="display:none;margin-top:8px;border-top:1px solid var(--line);padding-top:8px">
<div class="dim" style="font-size:11px;margin-bottom:5px">manual trade ticket — same demo-gated bridge as the bot; SL + TP mandatory, max 1.0 lot</div>
<div id="ticket">
<select id="mo_sym"></select>
<select id="mo_side"><option value="buy">BUY</option><option value="sell">SELL</option></select>
<input id="mo_vol" type="number" step="0.01" min="0.01" max="1" value="0.01" title="lots">
<input id="mo_sl" type="number" step="any" placeholder="SL price">
<input id="mo_tp" type="number" step="any" placeholder="TP price">
<button class="btn btn-ok" onclick="manualOrder()">SEND ORDER</button>
</div></div></div>

<div class="panel"><h2>Cooldowns — combos paused after consecutive losses</h2>
<table id="cooldowns"></table></div>

<div class="panel wide" id="p_pending" style="display:none;border:1px solid var(--amber)">
<h2>Trade approvals — the bot proposes, you decide <span id="hitl_badge" class="badge badge-warn">HITL</span></h2>
<table id="pending"></table></div>

<div class="panel wide"><h2>Price chart &#8212; live from MT5 <span id="chart_meta" class="badge badge-off">loading&hellip;</span></h2>
<div id="chart_ctl" style="margin-bottom:8px"></div>
<svg id="chart"></svg>
<div id="chart_legend" style="font-size:10px;margin-top:4px;color:var(--dim)"></div></div>

<div class="panel wide"><h2>Open positions (live from bridge)</h2><table id="positions"></table></div>
<div class="panel wide"><h2>Symbol monitor — what the engine sees right now</h2><table id="monitor"></table></div>
<div class="panel wide"><h2>Gate proximity — OOS performance vs threshold</h2><table id="gate_prox"></table></div>
<div class="panel wide"><h2>Strategy gate — full status</h2><table id="strategies"></table></div>
<div class="panel wide"><h2>Live results by strategy x symbol</h2><table id="combos"></table></div>
<div class="panel wide"><h2>Decision feed — every entry, skip and halt</h2><table id="decisions"></table></div>
<div class="panel"><h2>Closed trades</h2><table id="trades"></table></div>
<div class="panel"><h2>Lessons (self-review)</h2><table id="lessons"></table></div>
<div class="panel"><h2>Daily reports</h2><table id="reports"></table></div>
<div class="panel"><h2>Upcoming high-impact news</h2><table id="calendar"></table></div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmt=(x,d=2)=>x==null?'&#8212;':Number(x).toFixed(d);
const cls=x=>x==null?'dim':x>0?'pos':x<0?'neg':'dim';
let lastGoodSummaryAt=null;   // Date.now() of the last successful /api/summary fetch
let chartSym=null,chartTf='M15',chartSyms=[],chartBusy=false;
let hitlOn=false,hitlTtl=15;
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()}
function rows(el,head,data,f){
 el.innerHTML='<tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr>';
 if(!data.length)return;
 el.innerHTML+=(Array.isArray(data)?data:[]).map(f).join('');
}
function pf_bar(val,thr){
 const pct=val==null?0:Math.min(100,Math.round(val/thr*100));
 const col=val==null?'#1e2430':val>=thr?'#3ddc84':val>thr*.8?'#ffb02e':'#ff5470';
 return '<div class="bar-wrap"><div class="bar-fill" style="width:'+pct+'%;background:'+col+'"></div></div>';
}
function streak_txt(n){
 if(n===0)return '<span class="dim">no trades</span>';
 if(n>0)return '<span class="pos">+'+n+' wins</span>';
 return '<span class="neg">'+n+' losses</span>';
}

async function tick(){
 try{
  const s=await j('/api/summary');
  lastGoodSummaryAt=Date.now();
  updateLiveBadge();
  const b=s.bridge||{};
  $('acct').textContent=b.up?(b.login?('· '+b.login+' @ '+b.server+(b.demo?' · DEMO':' · LIVE')):'· bridge up — terminal NOT LOGGED IN'):'· bridge DOWN';

  const fresh=s.heartbeat&&(Date.now()-Date.parse(s.heartbeat.ts))<120000;
  $('badge_mode').className='badge '+(s.mode==='trade'?'badge-ok':'badge-warn');
  $('badge_mode').textContent=s.mode;
  $('badge_engine').className='badge '+(fresh?'badge-ok':'badge-warn');
  $('badge_engine').textContent=fresh?'engine live':'engine stale';

  let alerts='';
  if(!b.up)alerts+='<div class="warn">MT5 bridge is DOWN &#8212; engine cannot see or trade the market.</div>';
  if(b.up&&!b.login)alerts+='<div class="warn">Bridge is up but the terminal is NOT LOGGED IN &#8212; re-attaching automatically; writes fail closed until then.</div>';
  if(b.up&&b.login&&!b.demo)alerts+='<div class="warn">Account is NOT a demo account. Writes: '+(b.writes_allowed?'UNLOCKED (live!)':'refused')+'</div>';
  if(s.kill_switch)alerts+='<div class="warn">KILL SWITCH active &#8212; engine flattening/halted.</div>';
  if(s.manual_halt)alerts+='<div class="warn">HALT: '+s.manual_halt+'</div>';
  if(s.halted_for_day)alerts+='<div class="warn">Daily loss limit hit &#8212; no new entries today.</div>';
  $('alerts').innerHTML=alerts;

  const a=s.account;
  const mlvl=(a&&a.margin_level!=null&&a.margin)?fmt(a.margin_level,1)+'%':'&#8212;';
  $('kpi_acct').innerHTML=a?[
   '<div class="kpi-item"><b>'+fmt(a.equity)+'</b><small>equity '+a.currency+'</small></div>',
   '<div class="kpi-item"><b>'+fmt(a.balance)+'</b><small>balance</small></div>',
   '<div class="kpi-item"><b class="'+cls(a.profit)+'">'+fmt(a.profit)+'</b><small>floating P&L</small></div>',
   '<div class="kpi-item"><b>'+mlvl+'</b><small>margin level</small></div>',
   '<div class="kpi-item"><b><span class="dot" style="background:'+(fresh?'var(--green)':'var(--red)')+'"></span>'+
    'cycle '+(s.heartbeat?s.heartbeat.cycle:'?')+'</b><small>engine heartbeat</small></div>'
  ].join(''):'<div class="dim">no account data</div>';

  const p=s.stats_all,t=s.stats_today,sk=s.streaks||{};
  $('kpi_perf').innerHTML=[
   '<div class="kpi-item"><b>'+(p.trades||0)+'</b><small>all-time trades</small></div>',
   '<div class="kpi-item"><b>'+(p.win_rate==null?'&#8212;':p.win_rate+'%')+'</b><small>win rate</small></div>',
   '<div class="kpi-item"><b class="'+cls(p.pnl)+'">'+fmt(p.pnl)+'</b><small>total P&L</small></div>',
   '<div class="kpi-item"><b>'+(p.profit_factor==null?'&#8212;':p.profit_factor)+'</b><small>profit factor</small></div>',
   '<div class="kpi-item"><b class="'+cls(p.avg_r)+'">'+fmt(p.avg_r,3)+'R</b><small>avg R/trade</small></div>',
   '<div class="kpi-item"><b>'+streak_txt(sk.current||0)+'</b><small>current streak</small></div>'
  ].join('');
  $('kpi_today').innerHTML=[
   '<div class="kpi-item"><b>'+(t.trades||0)+'</b><small>trades today</small></div>',
   '<div class="kpi-item"><b>'+(t.win_rate==null?'&#8212;':t.win_rate+'%')+'</b><small>win rate</small></div>',
   '<div class="kpi-item"><b class="'+cls(t.pnl)+'">'+fmt(t.pnl)+'</b><small>P&L today</small></div>',
   '<div class="kpi-item"><b class="'+cls(t.avg_r)+'">'+fmt(t.avg_r,3)+'R</b><small>avg R today</small></div>'
  ].join('');

  const f=s.forward_test;
  $('fwd').textContent=f?('forward test day '+f.day+' of 90 · started '+f.start+
   ' @ '+fmt(f.start_equity)+(f.return_pct==null?'':' · '+(f.return_pct>=0?'+':'')+f.return_pct+'% since start')):'';

  const ru=s.rules||{};
  $('rules_panel').innerHTML=[
   ['Mode',ru.exec_mode==='trade'?'<span class="pos">TRADE (live orders)</span>':'<span class="amb">OBSERVE (no orders)</span>'],
   ['HITL approval',ru.hitl?'<span class="amb">ON (human confirms each trade)</span>':'<span class="pos">OFF (fully autonomous)</span>'],
   ['Max open positions','<span class="rule-val">'+ru.max_positions+'</span>'],
   ['Risk per trade','<span class="rule-val">'+ru.risk_pct+'% equity</span>'],
   ['Daily loss halt','<span class="rule-val">'+ru.max_daily_loss_pct+'%</span>'],
   ['Max drawdown halt','<span class="rule-val">'+ru.max_dd_pct+'%</span>'],
   ['News blackout','<span class="rule-val">&#177;'+ru.news_blackout_min+' min around high-impact</span>'],
   ['Spread guard','<span class="rule-val">skip if spread &gt;'+Math.round(ru.max_spread_atr_frac*100)+'% ATR</span>'],
   ['Trailing stop','<span class="rule-val">'+(ru.trailing_stop_atr>0?ru.trailing_stop_atr+'&#215; ATR':'off')+'</span>'],
   ['Partial exit','<span class="rule-val">'+(ru.partial_exit_r>0?'50% off at '+ru.partial_exit_r+'R':'off')+'</span>'],
  ].map(([k,v])=>'<div class="rule-row"><span class="dim">'+k+'</span>'+v+'</div>').join('');

  hitlOn=!!ru.hitl;hitlTtl=ru.hitl_ttl_min||15;
  if(!chartSyms.length&&s.symbols&&s.symbols.length){
   chartSyms=s.symbols;
   // default to the symbol with an open position, else the first configured
   chartSym=(s.positions&&s.positions[0]&&chartSyms.indexOf(s.positions[0].symbol)>=0)?s.positions[0].symbol:chartSyms[0];
   chartCtl();
   $('mo_sym').innerHTML=chartSyms.map(x=>'<option>'+x+'</option>').join('');
  }

  $('ticket_wrap').style.display=ru.manual_ticket?'block':'none';
  $('controls').innerHTML=
   '<div class="ctl-row">'+(s.manual_halt
    ?'<button class="btn btn-ok" onclick="control(\\'resume\\')">RESUME ENTRIES</button><span class="amb">paused &#8212; '+s.manual_halt+'</span>'
    :'<button class="btn btn-amb" onclick="control(\\'halt\\')">PAUSE NEW ENTRIES</button><span class="dim">bot may open positions (open ones stay managed)</span>')+'</div>'+
   '<div class="ctl-row">'+(s.kill_switch
    ?'<button class="btn btn-ok" onclick="control(\\'clear_kill\\')">CLEAR KILL SWITCH</button><span class="neg">KILL active &#8212; engine flattens &amp; halts</span>'
    :'<button class="btn btn-no" onclick="control(\\'kill\\')">KILL SWITCH</button><span class="dim">flatten every position, halt the engine</span>')+'</div>';

  rows($('positions'),['ticket','symbol','side','lots','open','now','sl','tp','P&L','swap','actions'],
   s.positions,px=>'<tr><td>'+px.ticket+'</td><td>'+px.symbol+'</td><td class="'+(px.type===0?'pos':'neg')+'">'+(px.type===0?'buy':'sell')+
   '</td><td>'+px.volume+'</td><td>'+px.price_open+'</td><td>'+px.price_current+'</td><td>'+px.sl+
   '</td><td>'+px.tp+'</td><td class="'+cls(px.profit)+'">'+fmt(px.profit)+'</td><td class="dim">'+fmt(px.swap)+'</td>'+
   '<td><button class="btn btn-no" onclick="closePos('+px.ticket+',\\''+px.symbol+'\\')">CLOSE</button></td></tr>');
  if(!s.positions.length)$('positions').innerHTML+='<tr><td colspan="11" class="dim">flat &#8212; no open positions</td></tr>';
 }catch(e){$('alerts').innerHTML='<div class="warn">Dashboard fetch error (summary): '+e+'</div>'}

 try{
  const eq=await j('/api/equity');drawEq(eq);
 }catch(e){}

 loadChart();

 try{
  const mo=await j('/api/monitor');
  rows($('monitor'),['symbol','tf','stars','trend','strength','vol','rsi','atr','spread pts (median)','market','updated'],mo,m=>
   '<tr><td><b>'+m.symbol+'</b></td><td class="dim">'+m.tf+'</td><td>'+'&#11088;'.repeat(m.stars||1)+'</td>'+
   '<td class="'+(m.trend==='up'?'pos':m.trend==='down'?'neg':'dim')+'">'+m.trend+'</td>'+
   '<td>'+fmt(m.trend_strength)+'</td><td class="dim">'+m.vol+'</td><td>'+fmt(m.rsi,1)+'</td>'+
   '<td>'+m.atr+'</td><td>'+fmt(m.spread_points,1)+' ('+fmt(m.median_spread_points,1)+')</td>'+
   '<td>'+(m.market_open?'<span class="pos">open</span>':'<span class="neg">closed</span>')+'</td>'+
   '<td class="dim">'+(m.ts||'').slice(5,16)+'</td></tr>');
  if(!mo.length)$('monitor').innerHTML+='<tr><td colspan="11" class="dim">no bar snapshots yet &#8212; engine warms up on the next closed bar</td></tr>';
 }catch(e){}

 try{
  const cd=await j('/api/cooldowns');
  rows($('cooldowns'),['strategy','symbol','until (UTC)'],cd,c=>
   '<tr><td>'+c.strategy+'</td><td>'+c.symbol+'</td>'+
   '<td class="amb">'+String(c.until).slice(0,19).replace('T',' ')+'</td></tr>');
  if(!cd.length)$('cooldowns').innerHTML+='<tr><td colspan="3" class="dim">no active cooldowns</td></tr>';
 }catch(e){}

 try{
  // Gate proximity: sorted by oos_pf desc (enabled first, then closest to passing)
  const gp=await j('/api/gate_proximity');
  const PF_THR=1.05,EXP_THR=0.02;
  const sorted=[...gp].sort((a,b)=>{
   if(a.status==='enabled'&&b.status!=='enabled')return -1;
   if(b.status==='enabled'&&a.status!=='enabled')return 1;
   return (b.oos_pf||0)-(a.oos_pf||0);
  });
  rows($('gate_prox'),['strategy','symbol','status','n','OOS PF (need '+PF_THR+')','OOS exp R','full PF','max DD%'],sorted,r=>{
   const opf=r.oos_pf!=null?fmt(r.oos_pf):'&#8212;';
   const oexp=r.oos_exp_r!=null?fmt(r.oos_exp_r,4)+'R':'&#8212;';
   const fpf=r.full_pf!=null?fmt(r.full_pf):'&#8212;';
   const dd=r.max_dd!=null?fmt(r.max_dd,1)+'%':'&#8212;';
   const pfClass=r.oos_pf==null?'dim':r.oos_pf>=PF_THR?'pos':r.oos_pf>PF_THR*.8?'amb':'neg';
   return '<tr><td>'+r.strategy+'</td><td>'+r.symbol+'</td>'+
    '<td><span class="tag '+r.status+'">'+r.status+'</span></td>'+
    '<td class="dim">'+(r.full_n||'&#8212;')+'</td>'+
    '<td><span class="'+pfClass+'">'+opf+'</span>'+pf_bar(r.oos_pf,PF_THR)+'</td>'+
    '<td class="'+(r.oos_exp_r==null?'dim':r.oos_exp_r>=EXP_THR?'pos':'neg')+'">'+oexp+'</td>'+
    '<td class="'+(r.full_pf==null?'dim':r.full_pf>=1.0?'pos':'neg')+'">'+fpf+'</td>'+
    '<td class="'+(r.max_dd==null?'dim':r.max_dd<25?'pos':'neg')+'">'+dd+'</td></tr>';
  });
  const enabled=gp.filter(r=>r.status==='enabled');
  $('badge_enabled').className='badge '+(enabled.length>0?'badge-ok':'badge-warn');
  $('badge_enabled').textContent=enabled.length+'/'+gp.length+' combos enabled';
  if(!gp.length)$('gate_prox').innerHTML+='<tr><td colspan="8" class="dim">gate not run yet</td></tr>';
 }catch(e){}

 try{
  const st=await j('/api/strategies');
  rows($('strategies'),['strategy','symbol','status','backtest summary','as of'],st,r=>{
   let m={};try{m=JSON.parse(r.backtest||'{}')}catch(ex){}
   const f=m.full||{},o=m.oos||{};
   const bt=f.n?('n='+f.n+' wr='+f.win_rate+'% pf='+f.profit_factor+
    ' | OOS pf='+fmt(o.profit_factor)+' exp='+fmt(o.expectancy_r,4)+'R'):'';
   return '<tr><td>'+r.strategy+'</td><td>'+r.symbol+'</td>'+
    '<td><span class="tag '+r.status+'">'+r.status+'</span></td>'+
    '<td><span class="dim">'+r.reason+'</span>'+(bt?' &#183; <span class="dim">'+bt+'</span>':'')+'</td>'+
    '<td class="dim">'+r.ts.slice(0,16)+'</td></tr>';
  });
 }catch(e){}

 try{
  const co=await j('/api/combos');
  rows($('combos'),['strategy','symbol','trades','wins','P&L','avg R'],co,c=>
   '<tr><td>'+c.strategy+'</td><td>'+c.symbol+'</td><td>'+c.n+'</td><td>'+c.wins+
   '</td><td class="'+cls(c.pnl)+'">'+fmt(c.pnl)+'</td><td class="'+cls(c.avg_r)+'">'+fmt(c.avg_r)+'</td></tr>');
  if(!co.length)$('combos').innerHTML+='<tr><td colspan="6" class="dim">no closed trades yet</td></tr>';
 }catch(e){}

 try{
  const de=await j('/api/decisions');
  rows($('decisions'),['ts','symbol','strategy','action','side','reason'],de,d=>
   '<tr><td class="dim">'+d.ts.slice(5,16)+'</td><td>'+(d.symbol||'')+'</td>'+
   '<td class="dim">'+(d.strategy||'')+'</td>'+
   '<td>'+(d.action==='enter'?'<b class="pos">ENTER</b>':
           d.action==='halt'?'<b class="neg">HALT</b>':
           d.action==='exit'?'<span class="amb">exit</span>':
           '<span class="dim">'+d.action+'</span>')+'</td>'+
   '<td class="'+(d.side==='buy'?'pos':d.side==='sell'?'neg':'dim')+'">'+(d.side||'')+'</td>'+
   '<td style="white-space:normal;max-width:400px">'+d.reason+'</td></tr>');
 }catch(e){}

 try{
  const tr=await j('/api/trades');
  rows($('trades'),['symbol','side','lots','entry','exit','P&L','R','exit reason'],tr,t=>
   '<tr><td>'+t.symbol+'</td><td class="'+(t.side==='buy'?'pos':'neg')+'">'+t.side+'</td>'+
   '<td>'+t.volume+'</td><td>'+fmt(t.entry_price,5)+'</td>'+
   '<td>'+(t.status==='open'?'<span class="tag enabled">open</span>':fmt(t.exit_price,5))+'</td>'+
   '<td class="'+cls(t.pnl)+'">'+fmt(t.pnl)+'</td>'+
   '<td class="'+cls(t.r_multiple)+'">'+fmt(t.r_multiple,3)+'</td>'+
   '<td class="dim">'+(t.exit_reason||'')+'</td></tr>');
  if(!tr.length)$('trades').innerHTML+='<tr><td colspan="8" class="dim">no closed trades yet</td></tr>';
 }catch(e){}

 try{
  const le=await j('/api/lessons');
  rows($('lessons'),['ts','combo','tag','lesson'],le,l=>
   '<tr><td class="dim">'+l.ts.slice(5,16)+'</td>'+
   '<td class="dim">'+(l.strategy||'')+'/'+(l.symbol||'')+'</td>'+
   '<td class="amb">'+(l.tag||'')+'</td>'+
   '<td style="white-space:normal;max-width:300px">'+l.lesson+'</td></tr>');
  if(!le.length)$('lessons').innerHTML+='<tr><td colspan="4" class="dim">no lessons yet</td></tr>';
 }catch(e){}

 try{
  const re=await j('/api/reports');
  rows($('reports'),['date','trades','win%','P&L','eq close'],re,r=>
   '<tr><td>'+r.date+'</td><td>'+r.trades+'</td>'+
   '<td>'+(r.win_rate==null?'&#8212;':r.win_rate+'%')+'</td>'+
   '<td class="'+cls(r.pnl)+'">'+fmt(r.pnl)+'</td>'+
   '<td class="dim">'+fmt(r.equity_close)+'</td></tr>');
  if(!re.length)$('reports').innerHTML+='<tr><td colspan="5" class="dim">no daily reports yet</td></tr>';
 }catch(e){}

 try{
  const ca=await j('/api/calendar');
  rows($('calendar'),['when (UTC)','ccy','event'],ca,c=>
   '<tr><td class="dim">'+c.ts_event.slice(0,16).replace('T',' ')+'</td>'+
   '<td><b>'+c.currency+'</b></td><td>'+c.title+'</td></tr>');
  if(!ca.length)$('calendar').innerHTML+='<tr><td colspan="3" class="dim">no upcoming high-impact events this week</td></tr>';
 }catch(e){}

 try{
  const pe=await j('/api/pending');
  $('p_pending').style.display=(pe.length||hitlOn)?'block':'none';
  $('hitl_badge').className='badge '+(hitlOn?'badge-ok':'badge-off');
  $('hitl_badge').textContent=hitlOn?'HITL ON':'HITL OFF';
  if(!pe.length){
   $('pending').innerHTML='<tr><td class="dim">no proposals waiting &#8212; the engine is scanning every cycle; '+
    'when a gate-passed setup fires it holds here for '+hitlTtl+' min for your APPROVE / DENY</td></tr>';
  }
  if(pe.length){
   rows($('pending'),['symbol','strategy','stars','side','lots','sl','tp','why the bot wants this','expires','actions'],pe,p=>{
    let ctx={};try{ctx=JSON.parse(p.detail||'{}')}catch(ex){}
    const mins=Math.max(0,Math.ceil((Date.parse(p.ts_expires)-Date.now())/60000));
    const why=p.reason+(ctx.regime?' &#183; regime: '+ctx.regime:'')+
     (ctx.median_spread_pts!=null?' &#183; spread(med): '+fmt(ctx.median_spread_pts,1)+' pts':'');
    return '<tr><td><b>'+p.symbol+'</b></td>'+
     '<td class="dim">'+p.strategy+(ctx.timeframe?' '+ctx.timeframe:'')+'</td>'+
     '<td>'+'&#11088;'.repeat(ctx.stars||1)+'</td>'+
     '<td class="'+(p.side==='buy'?'pos':'neg')+'">'+p.side.toUpperCase()+'</td>'+
     '<td>'+p.volume+'</td><td>'+p.sl+'</td><td>'+p.tp+'</td>'+
     '<td style="white-space:normal;max-width:340px">'+why+'</td>'+
     '<td class="amb">'+(mins>0?mins+'m left':'expiring&hellip;')+'</td>'+
     '<td><button class="btn btn-ok" onclick="act('+p.id+',\\'approve\\')">APPROVE</button>'+
     '<button class="btn btn-no" onclick="act('+p.id+',\\'deny\\')">DENY</button></td></tr>';
   });
  }
 }catch(e){}
}

async function post(url,body){
 const r=await fetch(url,{method:'POST',body:JSON.stringify(body),
  headers:{'Content-Type':'application/json'}});
 return r.json();
}
async function act(id,action){
 if(!confirm('Confirm: '+action+' this trade?'))return;
 try{
  const res=await post('/api/act',{id,action});
  if(res.ok)tick();else alert('Error: '+res.error);
 }catch(e){alert('Network error: '+e)}
}
const CONTROL_CONFIRM={
 halt:'Pause NEW entries? Open positions stay managed (trailing, partials, exits).',
 resume:'Resume entries? The bot may open positions again next cycle.',
 kill:'KILL SWITCH: the engine will FLATTEN EVERY OPEN POSITION and halt. Continue?',
 clear_kill:'Clear the kill switch? The engine resumes on its next cycle.'};
async function control(action){
 if(!confirm(CONTROL_CONFIRM[action]))return;
 try{
  const res=await post('/api/control',{action});
  if(res.ok)tick();else alert('Error: '+res.error);
 }catch(e){alert('Network error: '+e)}
}
async function closePos(ticket,symbol){
 if(!confirm('Close '+symbol+' position #'+ticket+' at market?'))return;
 try{
  const res=await post('/api/close',{ticket});
  if(res.ok)tick();else alert('Error: '+res.error);
 }catch(e){alert('Network error: '+e)}
}
async function manualOrder(){
 const b={symbol:$('mo_sym').value,side:$('mo_side').value,
  volume:parseFloat($('mo_vol').value),sl:parseFloat($('mo_sl').value),
  tp:parseFloat($('mo_tp').value)};
 if(!b.sl||!b.tp){alert('SL and TP are mandatory — no stop, no order.');return}
 if(!confirm('Send to bridge: '+b.side.toUpperCase()+' '+b.volume+' '+b.symbol+
  '  SL '+b.sl+'  TP '+b.tp+' ?'))return;
 try{
  const res=await post('/api/manual_order',b);
  if(res.ok){$('mo_sl').value='';$('mo_tp').value='';tick()}
  else alert('Bridge refused: '+res.error);
 }catch(e){alert('Network error: '+e)}
}

function chartCtl(){
 $('chart_ctl').innerHTML=
  chartSyms.map(s=>'<button class="cbtn'+(s===chartSym?' on':'')+'" onclick="setChart(\\''+s+'\\',null)">'+s+'</button>').join('')+
  '<span style="display:inline-block;width:16px"></span>'+
  ['M5','M15','M30','H1','H4','D1'].map(t=>'<button class="cbtn'+(t===chartTf?' on':'')+'" onclick="setChart(null,\\''+t+'\\')">'+t+'</button>').join('');
}
function setChart(s,t){if(s)chartSym=s;if(t)chartTf=t;chartCtl();loadChart();}
async function loadChart(){
 if(!chartSym||chartBusy)return;
 chartBusy=true;
 try{const c=await j('/api/chart?symbol='+chartSym+'&tf='+chartTf+'&count=180');drawChart(c);}
 catch(e){}
 finally{chartBusy=false}
}

function drawChart(c){
 // Self-contained SVG candlestick chart. Bars come straight from the MT5
 // bridge ([epoch,o,h,l,c,vol,spread]); overlays are the executor's own
 // journal rows. No library, no external requests, nothing invented.
 const svg=$('chart');
 const W=svg.clientWidth||900,H=svg.clientHeight||280;
 svg.setAttribute('viewBox','0 0 '+W+' '+H);
 if(c.error){
  $('chart_meta').className='badge badge-bad';$('chart_meta').textContent='bridge error';
  svg.innerHTML='<text x="8" y="20" fill="#6b7686" font-size="11">'+c.error+'</text>';
  $('chart_legend').textContent='';return;
 }
 const bars=c.bars||[];
 if(bars.length<2){
  svg.innerHTML='<text x="8" y="20" fill="#6b7686" font-size="11">no bars from bridge yet</text>';return;
 }
 const n=bars.length,padR=64,padB=18,padT=8;
 const t0=bars[0][0],t1=bars[n-1][0],tfSec=Math.max(1,Math.round((t1-t0)/(n-1)));
 // y-domain: bar range, widened by overlay levels that sit reasonably close
 // (an SL parked 3x the visible range away should not squash the candles)
 let lo=Math.min(...bars.map(b=>b[3])),hi=Math.max(...bars.map(b=>b[2]));
 const rng=(hi-lo)||1;
 const lvls=[];
 (c.positions||[]).forEach(p=>lvls.push(p.price_open,p.sl,p.tp));
 (c.pending||[]).forEach(p=>lvls.push(p.sl,p.tp));
 lvls.filter(v=>v!=null&&v>0&&v>lo-3*rng&&v<hi+3*rng).forEach(v=>{lo=Math.min(lo,v);hi=Math.max(hi,v)});
 const span=(hi-lo)||1;lo-=span*.04;hi+=span*.04;
 const X=i=>2+i/(n-1)*(W-padR-8);
 const Y=v=>padT+(1-(v-lo)/(hi-lo))*(H-padT-padB);
 const tsX=ts=>{ // x of the bar nearest an epoch-seconds timestamp, null if off-window
  if(ts==null||isNaN(ts)||ts<t0-tfSec||ts>t1+tfSec)return null;
  let best=0,bd=Infinity;
  for(let i=0;i<n;i++){const d=Math.abs(bars[i][0]-ts);if(d<bd){bd=d;best=i}}
  return X(best);
 };
 const dp=hi>=500?2:hi>=20?3:5; // GOLD 2dp, JPY-quoted 3dp, majors 5dp
 const P=v=>Number(v).toFixed(dp);
 let out='';
 for(let g=0;g<=4;g++){
  const v=lo+(hi-lo)*g/4,y=Y(v);
  out+='<line x1="2" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="#1e2430" stroke-width="1"/>';
  out+='<text x="'+(W-padR+4)+'" y="'+(y+3)+'" fill="#6b7686" font-size="10">'+P(v)+'</text>';
 }
 const tstep=Math.max(1,Math.floor(n/6));
 for(let i=0;i<n;i+=tstep){
  const d=new Date(bars[i][0]*1000).toISOString();
  out+='<text x="'+X(i)+'" y="'+(H-4)+'" fill="#6b7686" font-size="9">'+
   (chartTf==='D1'?d.slice(5,10):d.slice(5,16).replace('T',' '))+'</text>';
 }
 const cw=Math.max(1,(W-padR)/n*.65);
 bars.forEach((b,i)=>{
  const x=X(i),up=b[4]>=b[1],col=up?'#3ddc84':'#ff5470';
  const yO=Y(b[1]),yC=Y(b[4]);
  const ts=new Date(b[0]*1000).toISOString().slice(0,16).replace('T',' ');
  out+='<g><title>'+ts+'  O '+P(b[1])+'  H '+P(b[2])+'  L '+P(b[3])+'  C '+P(b[4])+'  vol '+b[5]+'</title>'+
   '<line x1="'+x+'" y1="'+Y(b[2])+'" x2="'+x+'" y2="'+Y(b[3])+'" stroke="'+col+'" stroke-width="1"/>'+
   '<rect x="'+(x-cw/2)+'" y="'+Math.min(yO,yC)+'" width="'+cw+'" height="'+Math.max(1,Math.abs(yC-yO))+'" fill="'+col+'"/></g>';
 });
 const lastC=bars[n-1][4],yLast=Y(lastC);
 out+='<line x1="2" y1="'+yLast+'" x2="'+(W-padR)+'" y2="'+yLast+'" stroke="#4da3ff" stroke-width="1" stroke-dasharray="1,3"/>';
 out+='<text x="'+(W-padR+4)+'" y="'+(yLast+3)+'" fill="#4da3ff" font-size="10" font-weight="bold">'+P(lastC)+'</text>';
 const lvl=(v,col,dash,label)=>{
  if(v==null||v<=0||v<lo||v>hi)return '';
  const y=Y(v);
  return '<line x1="2" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="'+col+'" stroke-width="1"'+(dash?' stroke-dasharray="4,3"':'')+'/>'+
   '<text x="6" y="'+(y-3)+'" fill="'+col+'" font-size="9">'+label+'</text>';
 };
 (c.positions||[]).forEach(p=>{
  const side=p.type===0?'buy':'sell';
  out+=lvl(p.price_open,'#4da3ff',false,side+' '+p.volume+' @ '+P(p.price_open)+(p.profit!=null?' &#183; P&amp;L '+fmt(p.profit):''));
  out+=lvl(p.sl,'#ff5470',true,'SL '+P(p.sl));
  out+=lvl(p.tp,'#3ddc84',true,'TP '+P(p.tp));
 });
 (c.pending||[]).forEach(p=>{
  out+=lvl(p.sl,'#ffb02e',true,'awaiting approval &#183; '+p.side+' SL '+P(p.sl));
  out+=lvl(p.tp,'#ffb02e',true,'awaiting approval &#183; '+p.side+' TP '+P(p.tp));
 });
 (c.trades||[]).forEach(t=>{
  let ctx={};try{ctx=JSON.parse(t.context||'{}')}catch(ex){}
  if(t.entry_time&&t.entry_price!=null){
   const x=tsX(Date.parse(t.entry_time)/1000);
   if(x!=null){
    const y=Y(t.entry_price),up=t.side==='buy';
    out+='<g><title>'+t.strategy+' '+t.side+' @ '+P(t.entry_price)+(ctx.signal?' &#8212; '+ctx.signal:'')+'</title>'+
     '<path d="M'+x+' '+(up?y-5:y+5)+' l5 '+(up?9:-9)+' h-10 z" fill="'+(up?'#3ddc84':'#ff5470')+'" stroke="#0b0e14" stroke-width="1"/></g>';
   }
  }
  if(t.exit_time&&t.exit_price!=null){
   const x=tsX(Date.parse(t.exit_time)/1000);
   if(x!=null){
    const y=Y(t.exit_price),col=t.pnl>0?'#3ddc84':t.pnl<0?'#ff5470':'#6b7686';
    out+='<g><title>exit '+t.strategy+' @ '+P(t.exit_price)+' &#183; '+(t.exit_reason||'')+' &#183; P&amp;L '+fmt(t.pnl)+' ('+fmt(t.r_multiple,2)+'R)</title>'+
     '<path d="M'+(x-4)+' '+(y-4)+' l8 8 M'+(x+4)+' '+(y-4)+' l-8 8" stroke="'+col+'" stroke-width="2" fill="none"/></g>';
   }
  }
 });
 svg.innerHTML=out;
 $('chart_meta').className='badge badge-ok';
 $('chart_meta').textContent=c.symbol+' '+c.tf+' · '+n+' bars · spread '+bars[n-1][6]+' pts';
 $('chart_legend').innerHTML='&#9650;&#9660; executor entries &#183; &#10005; exits &#183; '+
  '<span style="color:#4da3ff">&#9472;</span> open entry &#183; <span style="color:#ff5470">&#9476;</span> SL &#183; '+
  '<span style="color:#3ddc84">&#9476;</span> TP &#183; <span style="color:#ffb02e">&#9476;</span> awaiting approval &#183; hover any candle for OHLC';
}

function drawEq(eq){
 const svg=$('eq');
 const W=svg.clientWidth||640,H=svg.clientHeight||90;
 svg.setAttribute('viewBox','0 0 '+W+' '+H);
 if(!eq||eq.length<2){
  svg.innerHTML='<text x="4" y="'+Math.round(H/2)+'" fill="#6b7686" font-size="11">not enough equity history yet</text>';
  $('eq_legend').textContent='';
  return;
 }
 const n=eq.length;
 const hasBal=eq.some(p=>p.balance!=null);
 const vals=eq.map(p=>p.equity).concat(hasBal?eq.map(p=>p.balance).filter(v=>v!=null):[]);
 const mn=Math.min(...vals),mx=Math.max(...vals),pad=(mx-mn)||1;
 const X=i=>i/(n-1)*(W-4)+2;
 const Y=v=>H-8-((v-mn)/pad)*(H-18);
 const last=eq[n-1];
 const rising=last.equity>=eq[0].equity;
 const col=rising?'#3ddc84':'#ff5470';
 const gradId='eqGrad'+Math.round(Math.random()*1e6);
 const eqPts=eq.map((p,i)=>X(i)+','+Y(p.equity)).join(' ');
 let out='<defs><linearGradient id="'+gradId+'" x1="0" y1="0" x2="0" y2="1">'+
  '<stop offset="0%" stop-color="'+col+'" stop-opacity=".22"/>'+
  '<stop offset="100%" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>';
 out+='<polygon points="2,'+H+' '+eqPts+' '+(W-2)+','+H+'" fill="url(#'+gradId+')" stroke="none"/>';
 if(hasBal){
  const balPts=eq.map((p,i)=>X(i)+','+Y(p.balance==null?p.equity:p.balance)).join(' ');
  out+='<polyline points="'+balPts+'" fill="none" stroke="#6b7686" stroke-width="1" stroke-dasharray="3,2"/>';
 }
 out+='<polyline points="'+eqPts+'" fill="none" stroke="'+col+'" stroke-width="1.5"/>';
 // hover targets — <title> gives a native tooltip with the timestamp, no JS listeners needed
 const step=Math.max(1,Math.floor(n/80));
 const idxs=new Set();
 for(let i=0;i<n;i+=step)idxs.add(i);
 idxs.add(n-1);
 idxs.forEach(i=>{
  const p=eq[i];
  const label=p.ts+' — equity '+fmt(p.equity)+(hasBal?' / balance '+fmt(p.balance):'');
  out+='<circle cx="'+X(i)+'" cy="'+Y(p.equity)+'" r="7" fill="transparent"><title>'+label+'</title></circle>';
 });
 out+='<text x="'+(W-4)+'" y="11" text-anchor="end" fill="#6b7686" font-size="10">'+fmt(last.equity)+'</text>';
 out+='<text x="4" y="'+(H-3)+'" fill="#6b7686" font-size="10">'+fmt(mn)+'</text>';
 if(hasBal)out+='<text x="4" y="11" fill="#6b7686" font-size="10">bal '+fmt(last.balance)+'</text>';
 svg.innerHTML=out;
 $('eq_legend').textContent=hasBal?'— solid: equity   - - dashed: balance':'';
}

function updateLiveBadge(){
 const el=$('live_ind');
 if(!el)return;
 if(lastGoodSummaryAt==null){
  el.className='badge badge-off';el.textContent='connecting…';return;
 }
 const secs=Math.max(0,Math.floor((Date.now()-lastGoodSummaryAt)/1000));
 el.className='badge '+(secs<=15?'badge-ok':secs<=60?'badge-warn':'badge-bad');
 el.textContent=(secs<=60?'live':'stale')+' · updated '+secs+'s ago';
}

tick();setInterval(tick,5000);
updateLiveBadge();setInterval(updateLiveBadge,1000);
</script></body></html>"""


_local = threading.local()


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge = None

    @property
    def store(self) -> Store:
        if not hasattr(_local, "store"):
            _local.store = Store()  # sqlite connections are per-thread
        return _local.store

    def log_message(self, *a):
        pass

    @staticmethod
    def _json_safe(o):
        """json.dumps emits Infinity/NaN for non-finite floats — that is NOT
        valid JSON and the browser's JSON.parse rejects the whole payload (an
        all-win combo's OOS profit factor is float('inf'), which blanked the
        gate-proximity panel). Replace non-finite floats with None; the
        frontend already renders null as an em-dash."""
        if isinstance(o, float):
            return o if math.isfinite(o) else None
        if isinstance(o, dict):
            return {k: Handler._json_safe(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [Handler._json_safe(v) for v in o]
        return o

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            cl = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            if self.path == "/api/act":
                tid, act = body.get("id"), body.get("action")
                if self.store.act_on_pending(int(tid), act):
                    resp = {"ok": True}
                else:
                    resp = {"ok": False,
                            "error": "proposal no longer pending (expired or already acted on)"}
            elif self.path == "/api/control":
                resp = api_control(self.store, body)
            elif self.path == "/api/close":
                resp = api_close_position(self.store, self.bridge, body)
            elif self.path == "/api/manual_order":
                resp = api_manual_order(self.store, self.bridge, body)
            else:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, json.dumps(self._json_safe(resp)).encode(),
                       "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
            elif self.path == "/favicon.ico":
                self._send(200, b"", "image/x-icon")
            elif self.path == "/api/summary":
                self._send(200, json.dumps(self._json_safe(
                    api_summary(self.store, self.bridge))).encode(),
                           "application/json")
            elif self.path.startswith("/api/chart"):
                try:
                    symbol, tf, count = parse_chart_query(self.path)
                except ValueError as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(),
                               "application/json")
                    return
                self._send(200, json.dumps(self._json_safe(
                    api_chart(self.store, self.bridge, symbol, tf, count))).encode(),
                           "application/json")
            elif self.path in ROUTES:
                self._send(200, json.dumps(self._json_safe(
                    ROUTES[self.path](self.store, self.bridge))).encode(),
                           "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")


def main() -> None:
    Handler.bridge = Bridge(timeout=10)
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((config.DASH_HOST, config.DASH_PORT), Handler)
    print("executor dashboard on http://%s:%s" % (config.DASH_HOST, config.DASH_PORT),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
