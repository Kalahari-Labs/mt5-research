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


def api_summary(store: Store, bridge: Bridge) -> dict:
    out: dict = {"ts": utcnow(), "mode": config.EXEC_MODE,
                 "kill_switch": config.KILL_SWITCH.exists(),
                 "manual_halt": store.get_state("manual_halt"),
                 "halted_for_day": store.get_state("halted_for_day"),
                 "heartbeat": store.get_state("heartbeat"),
                 "gate_last_run": store.get_state("gate_last_run")}
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
}

HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>market-intel executor</title>
<style>
:root{--bg:#0b0e14;--panel:#12161f;--line:#1e2430;--txt:#c8d0dc;--dim:#6b7686;
--green:#3ddc84;--red:#ff5470;--amber:#ffb02e;--blue:#4da3ff;}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;padding:14px}
h1{font-size:15px;margin-bottom:2px} h2{font-size:12px;color:var(--dim);
text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);text-align:left;font-weight:normal;padding:2px 6px;border-bottom:1px solid var(--line)}
td{padding:2px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.pos{color:var(--green)}.neg{color:var(--red)}.dim{color:var(--dim)}
.tag{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px}
.tag.enabled{background:#123626;color:var(--green)}
.tag.observing{background:#2a2417;color:var(--amber)}
.tag.disabled,.tag.cooldown{background:#3a1420;color:var(--red)}
.kpi{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0}
.kpi div b{display:block;font-size:17px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
#topline{margin-bottom:12px}
canvas{width:100%;height:90px}
.warn{background:#3a1420;border:1px solid var(--red);border-radius:8px;
padding:8px 12px;margin-bottom:12px;color:#ffb3c0}
small{color:var(--dim)}
</style></head><body>
<div id="topline"><h1>market-intel executor <span id="acct" class="dim"></span></h1>
<small>demo-gated autonomous executor — every number below is journaled or read live from MT5. Trading involves risk; nothing here is financial advice.</small></div>
<div id="alerts"></div>
<div class="grid">
<div class="panel"><h2>Account</h2><div class="kpi" id="kpi_acct"></div>
<canvas id="eq" width="640" height="90"></canvas></div>
<div class="panel"><h2>Performance</h2><div class="kpi" id="kpi_perf"></div>
<div class="kpi" id="kpi_today"></div><div id="fwd" class="dim"></div></div>
<div class="panel wide"><h2>Open positions (live)</h2><table id="positions"></table></div>
<div class="panel wide"><h2>Symbol monitor — what the bot sees right now</h2><table id="monitor"></table></div>
<div class="panel wide"><h2>Strategy gate — what may trade and why</h2><table id="strategies"></table></div>
<div class="panel wide"><h2>Live results by strategy × symbol</h2><table id="combos"></table></div>
<div class="panel wide"><h2>Decision feed (incl. skips)</h2><table id="decisions"></table></div>
<div class="panel"><h2>Closed trades</h2><table id="trades"></table></div>
<div class="panel"><h2>Lessons (self-review)</h2><table id="lessons"></table></div>
<div class="panel"><h2>Daily reports</h2><table id="reports"></table></div>
<div class="panel"><h2>Upcoming high-impact news</h2><table id="calendar"></table></div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmt=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
const cls=x=>x>0?'pos':x<0?'neg':'';
async function j(u){const r=await fetch(u);return r.json()}
function rows(el,head,data,f){el.innerHTML='<tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr>'+
 data.map(f).join('')}
async function tick(){
 try{
  const s=await j('/api/summary');
  const b=s.bridge||{};
  $('acct').textContent=b.up?('· '+b.login+' @ '+b.server+(b.demo?' · DEMO':' · NOT DEMO')):'· bridge DOWN';
  let alerts='';
  if(!b.up)alerts+='<div class="warn">MT5 bridge is DOWN — engine cannot see or trade the market.</div>';
  if(b.up&&!b.demo)alerts+='<div class="warn">Account is NOT a demo account. Writes: '+(b.writes_allowed?'UNLOCKED (live!)':'refused')+'</div>';
  if(s.kill_switch)alerts+='<div class="warn">KILL SWITCH active — engine flattening/halted.</div>';
  if(s.manual_halt)alerts+='<div class="warn">HALT: '+s.manual_halt+'</div>';
  if(s.halted_for_day)alerts+='<div class="warn">Daily loss limit hit — no new entries today.</div>';
  $('alerts').innerHTML=alerts;
  const a=s.account;
  const hb=s.heartbeat?new Date(s.heartbeat.ts+'Z'!==s.heartbeat.ts?s.heartbeat.ts:s.heartbeat.ts):null;
  const fresh=s.heartbeat&&(Date.now()-Date.parse(s.heartbeat.ts))<120000;
  $('kpi_acct').innerHTML=a?['<div><b>'+fmt(a.equity)+'</b>equity '+a.currency+'</div>',
   '<div><b>'+fmt(a.balance)+'</b>balance</div>',
   '<div><b class="'+cls(a.profit)+'">'+fmt(a.profit)+'</b>floating</div>',
   '<div><b><span class="dot" style="background:'+(fresh?'var(--green)':'var(--red)')+'"></span>'+(s.mode||'?')+'</b>engine '+(fresh?'live':'STALE')+'</div>'].join(''):'<div class="dim">no account data</div>';
  const p=s.stats_all,t=s.stats_today;
  $('kpi_perf').innerHTML=['<div><b>'+(p.trades||0)+'</b>trades</div>',
   '<div><b>'+(p.win_rate==null?'—':p.win_rate+'%')+'</b>win rate</div>',
   '<div><b class="'+cls(p.pnl)+'">'+fmt(p.pnl)+'</b>total P&L</div>',
   '<div><b>'+(p.profit_factor==null?'—':p.profit_factor)+'</b>profit factor</div>'].join('');
  $('kpi_today').innerHTML=['<div><b>'+(t.trades||0)+'</b>today</div>',
   '<div><b>'+(t.win_rate==null?'—':t.win_rate+'%')+'</b>win rate</div>',
   '<div><b class="'+cls(t.pnl)+'">'+fmt(t.pnl)+'</b>P&L today</div>'].join('');
  const f=s.forward_test;
  $('fwd').textContent=f?('forward test day '+f.day+' of 90 · started '+f.start+
   ' @ '+fmt(f.start_equity)+(f.return_pct==null?'':' · '+(f.return_pct>=0?'+':'')+
   f.return_pct+'% since start')):'';
  rows($('positions'),['ticket','symbol','side','lots','open','now','sl','tp','P&L','swap'],
   s.positions,p=>'<tr><td>'+p.ticket+'</td><td>'+p.symbol+'</td><td>'+(p.type===0?'buy':'sell')+
   '</td><td>'+p.volume+'</td><td>'+p.price_open+'</td><td>'+p.price_current+'</td><td>'+p.sl+
   '</td><td>'+p.tp+'</td><td class="'+cls(p.profit)+'">'+fmt(p.profit)+'</td><td>'+fmt(p.swap)+'</td></tr>');
  if(!s.positions.length)$('positions').innerHTML+='<tr><td class="dim">flat — no open positions</td></tr>';
 }catch(e){$('alerts').innerHTML='<div class="warn">dashboard API error: '+e+'</div>'}
 try{
  const eq=await j('/api/equity');drawEq(eq);
  const mo=await j('/api/monitor');
  rows($('monitor'),['symbol','tf','trend','strength','vol','rsi','atr','spread (median) pts','market','as of'],mo,m=>
   '<tr><td>'+m.symbol+'</td><td>'+m.tf+'</td><td class="'+(m.trend==='up'?'pos':m.trend==='down'?'neg':'dim')+'">'+m.trend+
   '</td><td>'+fmt(m.trend_strength)+'</td><td>'+m.vol+'</td><td>'+fmt(m.rsi,1)+'</td><td>'+m.atr+
   '</td><td>'+fmt(m.spread_points,1)+' ('+fmt(m.median_spread_points,1)+')</td><td>'+(m.market_open?'open':'<span class="neg">closed/stale</span>')+
   '</td><td class="dim">'+(m.ts||'').slice(5,16)+'</td></tr>');
  if(!mo.length)$('monitor').innerHTML+='<tr><td class="dim">no closed-bar snapshots yet — engine warms up on the next bar</td></tr>';
  const co=await j('/api/combos');
  rows($('combos'),['strategy','symbol','trades','wins','P&L','avg R'],co,c=>
   '<tr><td>'+c.strategy+'</td><td>'+c.symbol+'</td><td>'+c.n+'</td><td>'+c.wins+
   '</td><td class="'+cls(c.pnl)+'">'+fmt(c.pnl)+'</td><td class="'+cls(c.avg_r)+'">'+fmt(c.avg_r)+'</td></tr>');
  if(!co.length)$('combos').innerHTML+='<tr><td class="dim">no closed trades yet</td></tr>';
  const st=await j('/api/strategies');
  rows($('strategies'),['strategy','symbol','status','detail','as of'],st,r=>{
   let m={};try{m=JSON.parse(r.backtest||'{}')}catch(e){}
   const f=m.full||{},o=m.oos||{};
   const bt=f.n?('n='+f.n+' pf='+f.profit_factor+' | OOS pf='+(o.profit_factor??'—')+' exp='+(o.expectancy_r??'—')+'R'):'';
   return '<tr><td>'+r.strategy+'</td><td>'+r.symbol+'</td><td><span class="tag '+r.status+'">'+r.status+
    '</span></td><td>'+(bt||'')+' <span class="dim">'+r.reason+'</span></td><td class="dim">'+r.ts.slice(0,16)+'</td></tr>'});
  const de=await j('/api/decisions');
  rows($('decisions'),['ts','symbol','strategy','action','reason'],de,d=>
   '<tr><td class="dim">'+d.ts.slice(5,16)+'</td><td>'+(d.symbol||'')+'</td><td>'+(d.strategy||'')+
   '</td><td>'+(d.action==='enter'?'<b class="pos">enter '+(d.side||'')+'</b>':d.action)+'</td><td>'+d.reason+'</td></tr>');
  const tr=await j('/api/trades');
  rows($('trades'),['symbol','side','lots','entry','exit','P&L','R','why'],tr,t=>
   '<tr><td>'+t.symbol+'</td><td>'+t.side+'</td><td>'+t.volume+'</td><td>'+fmt(t.entry_price,5)+
   '</td><td>'+(t.status==='open'?'<span class="tag enabled">open</span>':fmt(t.exit_price,5))+
   '</td><td class="'+cls(t.pnl)+'">'+fmt(t.pnl)+'</td><td class="'+cls(t.r_multiple)+'">'+fmt(t.r_multiple)+
   '</td><td class="dim">'+(t.exit_reason||'')+'</td></tr>');
  const le=await j('/api/lessons');
  rows($('lessons'),['ts','combo','lesson'],le,l=>
   '<tr><td class="dim">'+l.ts.slice(5,16)+'</td><td>'+(l.strategy||'')+'/'+(l.symbol||'')+
   '</td><td>'+l.lesson+'</td></tr>');
  const re=await j('/api/reports');
  rows($('reports'),['date','trades','win%','P&L'],re,r=>
   '<tr><td>'+r.date+'</td><td>'+r.trades+'</td><td>'+(r.win_rate==null?'—':r.win_rate)+
   '</td><td class="'+cls(r.pnl)+'">'+fmt(r.pnl)+'</td></tr>');
  const ca=await j('/api/calendar');
  rows($('calendar'),['when (UTC)','ccy','event'],ca,c=>
   '<tr><td>'+c.ts_event.slice(0,16).replace('T',' ')+'</td><td>'+c.currency+'</td><td>'+c.title+'</td></tr>');
  if(!ca.length)$('calendar').innerHTML+='<tr><td class="dim">no upcoming high-impact events this week</td></tr>';
 }catch(e){}
}
function drawEq(eq){
 const c=$('eq'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);
 if(eq.length<2)return;
 const v=eq.map(p=>p.equity),mn=Math.min(...v),mx=Math.max(...v),pad=(mx-mn)||1;
 x.beginPath();x.strokeStyle=v[v.length-1]>=v[0]?'#3ddc84':'#ff5470';x.lineWidth=1.5;
 v.forEach((y,i)=>{const px=i/(v.length-1)*(c.width-4)+2,
  py=c.height-6-((y-mn)/pad)*(c.height-14);i?x.lineTo(px,py):x.moveTo(px,py)});
 x.stroke();
 x.fillStyle='#6b7686';x.font='10px monospace';
 x.fillText(fmt(v[v.length-1]),c.width-70,12);x.fillText(fmt(mn),4,c.height-2);
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
    srv = ThreadingHTTPServer((config.DASH_HOST, config.DASH_PORT), Handler)
    print("executor dashboard on http://%s:%s" % (config.DASH_HOST, config.DASH_PORT),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
