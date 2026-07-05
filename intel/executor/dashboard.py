"""dashboard.py — executor dashboard on http://127.0.0.1:8877

Read-only window into the executor. Every number is either a row the engine
wrote to SQLite or a live read from the MT5 bridge — the dashboard itself
computes nothing and invents nothing. If the bridge is down the account panel
says so instead of showing stale numbers.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .bridge import Bridge, BridgeError
from .store import Store, utcnow


def api_gate_proximity(store: Store) -> list:
    """For every strategy×symbol, return gate metrics and distance to threshold."""
    rows = store.query(
        "SELECT strategy, symbol, status, reason, backtest FROM strategy_status "
        "ORDER BY strategy, symbol")
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


def api_summary(store: Store, bridge: Bridge) -> dict:
    out: dict = {"ts": utcnow(), "mode": config.EXEC_MODE,
                 "kill_switch": config.KILL_SWITCH.exists(),
                 "manual_halt": store.get_state("manual_halt"),
                 "halted_for_day": store.get_state("halted_for_day"),
                 "heartbeat": store.get_state("heartbeat"),
                 "gate_last_run": store.get_state("gate_last_run"),
                 "streaks": store.streaks(),
                 "rules": {
                     "exec_mode": config.EXEC_MODE,
                     "hitl": config.HITL_MODE,
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
                           "currency", "leverage")}
        out["positions"] = [
            {k: p.get(k) for k in ("ticket", "symbol", "type", "volume",
                                   "price_open", "price_current", "sl", "tp",
                                   "profit", "swap", "comment")}
            for p in bridge.positions()]
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
    "/api/equity": lambda s, b: s.query(
        "SELECT ts, equity, balance FROM equity_curve ORDER BY id DESC LIMIT 500")[::-1],
    # what the engine saw on each symbol at its last closed bar (regime, spread,
    # market state) — written by engine.symbol_view, invented nowhere
    "/api/monitor": lambda s, b: [json.loads(r["value"]) for r in s.query(
        "SELECT value FROM engine_state WHERE key LIKE 'symbol_view:%' ORDER BY key")],
    "/api/combos": lambda s, b: s.query(
        "SELECT strategy, symbol, COUNT(*) n, SUM(pnl>0) wins, "
        "ROUND(SUM(pnl),2) pnl, ROUND(AVG(r_multiple),3) avg_r "
        "FROM trades WHERE status='closed' AND pnl IS NOT NULL "
        "GROUP BY strategy, symbol ORDER BY pnl DESC"),
    "/api/decisions": lambda s, b: s.query(
        "SELECT ts, symbol, strategy, action, side, reason FROM decisions "
        "ORDER BY id DESC LIMIT 60"),
    "/api/trades": lambda s, b: s.query(
        "SELECT ticket, symbol, strategy, side, volume, entry_time, entry_price, "
        "sl, tp, exit_time, exit_price, pnl, r_multiple, exit_reason, status "
        "FROM trades ORDER BY id DESC LIMIT 60"),
    "/api/strategies": lambda s, b: s.query(
        "SELECT ts, strategy, symbol, status, reason, backtest FROM strategy_status "
        "ORDER BY strategy, symbol"),
    "/api/lessons": lambda s, b: s.query(
        "SELECT ts, symbol, strategy, tag, lesson FROM lessons ORDER BY id DESC LIMIT 40"),
    "/api/reports": lambda s, b: s.query(
        "SELECT * FROM daily_reports ORDER BY date DESC LIMIT 14"),
    "/api/calendar": lambda s, b: s.query(
        "SELECT ts_event, currency, title FROM calendar_events "
        "WHERE ts_event >= ? ORDER BY ts_event LIMIT 20", (utcnow(),)),
    "/api/pending": lambda s, b: s.query(
        "SELECT * FROM pending_trades WHERE status='pending' ORDER BY id DESC"),
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
canvas{width:100%;height:90px;display:block}
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

<div class="panel"><h2>Account</h2>
<div class="kpi" id="kpi_acct"></div>
<canvas id="eq" width="640" height="90"></canvas></div>

<div class="panel"><h2>Performance</h2>
<div class="kpi" id="kpi_perf"></div>
<div class="kpi" id="kpi_today"></div>
<div id="fwd" class="dim" style="margin-top:6px;font-size:12px"></div></div>

<div class="panel"><h2>Discipline rules — enforced every cycle</h2>
<div id="rules_panel"></div></div>

<div class="panel wide" id="p_pending" style="display:none;border:1px solid var(--amber)">
<h2>&#9888; Pending Approvals (HITL)</h2><table id="pending"></table></div>

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
  const b=s.bridge||{};
  $('acct').textContent=b.up?('&#183; '+b.login+' @ '+b.server+(b.demo?' &#183; DEMO':' &#183; LIVE')):'&#183; bridge DOWN';

  const fresh=s.heartbeat&&(Date.now()-Date.parse(s.heartbeat.ts))<120000;
  $('badge_mode').className='badge '+(s.mode==='trade'?'badge-ok':'badge-warn');
  $('badge_mode').textContent=s.mode;
  $('badge_engine').className='badge '+(fresh?'badge-ok':'badge-warn');
  $('badge_engine').textContent=fresh?'engine live':'engine stale';

  let alerts='';
  if(!b.up)alerts+='<div class="warn">MT5 bridge is DOWN &#8212; engine cannot see or trade the market.</div>';
  if(b.up&&!b.demo)alerts+='<div class="warn">Account is NOT a demo account. Writes: '+(b.writes_allowed?'UNLOCKED (live!)':'refused')+'</div>';
  if(s.kill_switch)alerts+='<div class="warn">KILL SWITCH active &#8212; engine flattening/halted.</div>';
  if(s.manual_halt)alerts+='<div class="warn">HALT: '+s.manual_halt+'</div>';
  if(s.halted_for_day)alerts+='<div class="warn">Daily loss limit hit &#8212; no new entries today.</div>';
  $('alerts').innerHTML=alerts;

  const a=s.account;
  $('kpi_acct').innerHTML=a?[
   '<div class="kpi-item"><b>'+fmt(a.equity)+'</b><small>equity '+a.currency+'</small></div>',
   '<div class="kpi-item"><b>'+fmt(a.balance)+'</b><small>balance</small></div>',
   '<div class="kpi-item"><b class="'+cls(a.profit)+'">'+fmt(a.profit)+'</b><small>floating P&L</small></div>',
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
  $('fwd').textContent=f?('forward test day '+f.day+' of 90 &#183; started '+f.start+
   ' @ '+fmt(f.start_equity)+(f.return_pct==null?'':' &#183; '+(f.return_pct>=0?'+':'')+f.return_pct+'% since start')):'';

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

  rows($('positions'),['ticket','symbol','side','lots','open','now','sl','tp','P&L','swap'],
   s.positions,px=>'<tr><td>'+px.ticket+'</td><td>'+px.symbol+'</td><td class="'+(px.type===0?'pos':'neg')+'">'+(px.type===0?'buy':'sell')+
   '</td><td>'+px.volume+'</td><td>'+px.price_open+'</td><td>'+px.price_current+'</td><td>'+px.sl+
   '</td><td>'+px.tp+'</td><td class="'+cls(px.profit)+'">'+fmt(px.profit)+'</td><td class="dim">'+fmt(px.swap)+'</td></tr>');
  if(!s.positions.length)$('positions').innerHTML+='<tr><td colspan="10" class="dim">flat &#8212; no open positions</td></tr>';
 }catch(e){$('alerts').innerHTML='<div class="warn">Dashboard fetch error (summary): '+e+'</div>'}

 try{
  const eq=await j('/api/equity');drawEq(eq);
 }catch(e){}

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
  if(pe.length){
   $('p_pending').style.display='block';
   rows($('pending'),['symbol','stars','side','lots','sl','tp','reason','actions'],pe,p=>{
    const ctx=JSON.parse(p.detail||'{}');
    return '<tr><td><b>'+p.symbol+'</b></td><td>'+'&#11088;'.repeat(ctx.stars||1)+'</td>'+
     '<td class="'+(p.side==='buy'?'pos':'neg')+'">'+p.side.toUpperCase()+'</td>'+
     '<td>'+p.volume+'</td><td>'+p.sl+'</td><td>'+p.tp+'</td><td>'+p.reason+'</td>'+
     '<td><button class="btn btn-ok" onclick="act('+p.id+',\'approve\')">APPROVE</button>'+
     '<button class="btn btn-no" onclick="act('+p.id+',\'deny\')">DENY</button></td></tr>';
   });
  }else{$('p_pending').style.display='none'}
 }catch(e){}
}

async function act(id,action){
 if(!confirm('Confirm: '+action+' this trade?'))return;
 try{
  const r=await fetch('/api/act',{method:'POST',body:JSON.stringify({id,action}),
   headers:{'Content-Type':'application/json'}});
  const res=await r.json();if(res.ok)tick();else alert('Error: '+res.error);
 }catch(e){alert('Network error: '+e)}
}

function drawEq(eq){
 const c=$('eq'),x=c.getContext('2d');
 const W=c.clientWidth||640,H=c.clientHeight||90;
 c.width=W;c.height=H;
 x.clearRect(0,0,W,H);
 if(!eq||eq.length<2)return;
 const v=eq.map(p=>p.equity);
 const mn=Math.min(...v),mx=Math.max(...v),pad=(mx-mn)||1;
 const rising=v[v.length-1]>=v[0];
 // gradient fill
 const grad=x.createLinearGradient(0,0,0,H);
 grad.addColorStop(0,rising?'rgba(61,220,132,.18)':'rgba(255,84,112,.18)');
 grad.addColorStop(1,'rgba(0,0,0,0)');
 x.beginPath();
 v.forEach((y,i)=>{const px=i/(v.length-1)*(W-4)+2,py=H-8-((y-mn)/pad)*(H-18);
  i?x.lineTo(px,py):x.moveTo(px,py)});
 x.lineTo(W-2,H);x.lineTo(2,H);x.closePath();
 x.fillStyle=grad;x.fill();
 // line
 x.beginPath();x.strokeStyle=rising?'#3ddc84':'#ff5470';x.lineWidth=1.5;
 v.forEach((y,i)=>{const px=i/(v.length-1)*(W-4)+2,py=H-8-((y-mn)/pad)*(H-18);
  i?x.lineTo(px,py):x.moveTo(px,py)});
 x.stroke();
 x.fillStyle='#6b7686';x.font='10px monospace';
 x.fillText(fmt(v[v.length-1]),W-68,12);
 x.fillText(fmt(mn),4,H-2);
}

tick();setInterval(tick,5000);
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

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            if self.path == "/api/act":
                cl = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(cl).decode())
                tid, act = body.get("id"), body.get("action")
                if act == "approve":
                    self.store.execute("UPDATE pending_trades SET status='approved' WHERE id=?", (tid,))
                elif act == "deny":
                    self.store.execute("UPDATE pending_trades SET status='denied' WHERE id=?", (tid,))
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
            elif self.path == "/favicon.ico":
                self._send(200, b"", "image/x-icon")
            elif self.path == "/api/summary":
                self._send(200, json.dumps(api_summary(self.store, self.bridge)).encode(),
                           "application/json")
            elif self.path in ROUTES:
                self._send(200, json.dumps(ROUTES[self.path](self.store, self.bridge)).encode(),
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
