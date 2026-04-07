#!/usr/bin/env python3
"""
Live Trading Dashboard v24
Real-time web dashboard for monitoring live/shadow trading.
Reads from live_trades/live_status.json (updated every 10s by live_trader.py).
Run: python live_dashboard.py  (starts on port 5555)
"""

import json
import os
from pathlib import Path
from flask import Flask, jsonify, Response

app = Flask(__name__)

STATUS_FILE = Path(__file__).parent / "live_trades" / "live_status.json"
STOCK_STATUS_FILE = Path(__file__).parent / "stock_live_trades" / "stock_live_status.json"

# ─── Demo data for when live_status.json doesn't exist yet ───────────────────
DEMO_STATUS = {
    "version": "v24",
    "mode": "SHADOW",
    "capital": 100000,
    "daily_loss_limit": 10000,
    "strategies": ["CPR", "Gamma Blast", "Wave"],
    "positions": [],
    "closed_trades": [],
    "open_count": 0,
    "closed_today": 0,
    "trades_today": 0,
    "max_trades": 24,
    "unrealized_pnl": 0,
    "realized_pnl": 0,
    "total_pnl": 0,
    "win_rate": 0,
    "winners": 0,
    "losers": 0,
    "risk": {"daily_pnl": 0, "daily_loss_remaining": 10000, "halted": False},
    "oi_heatmap": True,
    "reversal_detector": True,
    "timestamp": "—",
}


def load_status() -> dict:
    """Load live_status.json + stock_live_status.json, merge into combined view."""
    equity = None
    stock = None
    try:
        if STATUS_FILE.exists():
            with open(STATUS_FILE, "r") as f:
                equity = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    try:
        if STOCK_STATUS_FILE.exists():
            with open(STOCK_STATUS_FILE, "r") as f:
                stock = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

    if not equity and not stock:
        return DEMO_STATUS.copy()

    # Use equity as base, merge stock positions/trades into it
    combined = (equity or DEMO_STATUS).copy()
    if stock:
        # Merge stock positions and closed trades into equity view
        stock_positions = [dict(p, _bot='STOCK') for p in stock.get('positions', [])]
        stock_closed = [dict(p, _bot='STOCK') for p in stock.get('closed_trades', [])]
        combined['positions'] = combined.get('positions', []) + stock_positions
        combined['closed_trades'] = combined.get('closed_trades', []) + stock_closed
        combined['open_count'] = len(combined['positions'])
        combined['closed_today'] = combined.get('closed_today', 0) + stock.get('closed_today', 0)
        combined['trades_today'] = combined.get('trades_today', 0) + stock.get('trades_today', 0)
        combined['unrealized_pnl'] = round(
            combined.get('unrealized_pnl', 0) + stock.get('unrealized_pnl', 0), 2)
        combined['realized_pnl'] = round(
            combined.get('realized_pnl', 0) + stock.get('realized_pnl', 0), 2)
        combined['total_pnl'] = round(
            combined['realized_pnl'] + combined['unrealized_pnl'], 2)
        all_winners = combined.get('winners', 0) + stock.get('winners', 0)
        all_losers = combined.get('losers', 0) + stock.get('losers', 0)
        combined['winners'] = all_winners
        combined['losers'] = all_losers
        total_closed = all_winners + all_losers
        combined['win_rate'] = round(all_winners / total_closed * 100, 1) if total_closed > 0 else 0
        # Merge market data
        if stock.get('market_data'):
            combined.setdefault('market_data', {}).update(stock['market_data'])
        combined['stock_bot'] = {
            'positions': stock.get('open_count', 0),
            'trades_today': stock.get('trades_today', 0),
            'pnl': stock.get('total_pnl', 0),
        }
    return combined


# ─── API endpoint ────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify(load_status())


# ─── HTML Dashboard ──────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Trading Dashboard v24</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#eee;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;padding:16px 24px;min-height:100vh}
.mono{font-family:'Cascadia Code','Fira Code','Consolas',monospace}

/* Header */
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#16213e;border-radius:10px;margin-bottom:16px;border:1px solid #0f3460}
.header h1{font-size:1.35rem;font-weight:700;letter-spacing:0.5px}
.header h1 span.ver{color:#7f8fa6;font-weight:400;font-size:0.9rem;margin-left:6px}
.badge{display:inline-block;padding:3px 14px;border-radius:20px;font-size:0.78rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-left:12px}
.badge-shadow{background:#2980b9;color:#fff}
.badge-live{background:#e74c3c;color:#fff;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}
.header-right{display:flex;align-items:center;gap:16px;font-size:0.82rem;color:#7f8fa6}
.header-right .order-type{color:#f39c12;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot-green{background:#00ff88}
.dot-red{background:#ff4444}

/* Summary cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
.card{background:#16213e;border-radius:10px;padding:16px 18px;border:1px solid #0f3460;transition:transform 0.15s}
.card:hover{transform:translateY(-2px)}
.card-label{font-size:0.72rem;text-transform:uppercase;letter-spacing:1.2px;color:#7f8fa6;margin-bottom:6px}
.card-value{font-size:1.55rem;font-weight:700}
.card-sub{font-size:0.72rem;color:#7f8fa6;margin-top:4px}
.green{color:#00ff88}
.red{color:#ff4444}
.neutral{color:#eee}
.yellow{color:#f1c40f}

/* Tables */
.section{margin-bottom:18px}
.section-title{font-size:0.95rem;font-weight:600;margin-bottom:8px;padding-left:4px;color:#ddd;display:flex;align-items:center;gap:8px}
.section-title .count{background:#0f3460;padding:2px 10px;border-radius:12px;font-size:0.75rem;color:#7f8fa6}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:10px;overflow:hidden;border:1px solid #0f3460;font-size:0.82rem}
thead{background:#0f3460}
th{padding:10px 12px;text-align:left;font-weight:600;font-size:0.72rem;text-transform:uppercase;letter-spacing:1px;color:#7f8fa6}
td{padding:9px 12px;border-top:1px solid #1a1a2e}
tr:hover td{background:rgba(15,52,96,0.3)}
.empty-row td{text-align:center;color:#555;padding:28px;font-style:italic}

/* Strategy breakdown + Risk */
.bottom-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
@media(max-width:800px){.bottom-grid{grid-template-columns:1fr}}

.risk-panel{background:#16213e;border-radius:10px;padding:16px 18px;border:1px solid #0f3460}
.risk-panel h3{font-size:0.85rem;margin-bottom:12px;color:#ddd}
.risk-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;font-size:0.82rem}
.risk-bar-bg{width:100%;height:10px;background:#0f3460;border-radius:6px;overflow:hidden;margin-top:4px}
.risk-bar-fill{height:100%;border-radius:6px;transition:width 0.4s}
.feature-row{display:flex;gap:18px;margin-top:10px;font-size:0.8rem}
.feature-tag{padding:3px 12px;border-radius:14px;font-weight:600;font-size:0.72rem}
.feature-on{background:#00482a;color:#00ff88;border:1px solid #00ff88}
.feature-off{background:#3d0000;color:#ff4444;border:1px solid #ff4444}

.strat-table td,.strat-table th{padding:8px 12px}

/* Signal strength bar */
.sig-bar{display:inline-block;height:8px;border-radius:4px;min-width:10px;max-width:60px;vertical-align:middle;margin-right:4px}
.sig-strong{background:#00ff88}
.sig-medium{background:#f1c40f}
.sig-weak{background:#ff4444}
.src-badge{display:inline-block;padding:1px 8px;border-radius:8px;font-size:0.65rem;font-weight:700;letter-spacing:0.5px}
.src-dhan{background:#1565c0;color:#fff}
.src-zerodha{background:#e65100;color:#fff}

/* Market indicators */
.mkt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.mkt-card{background:#16213e;border-radius:10px;padding:12px 16px;border:1px solid #0f3460}
.mkt-card h4{font-size:0.78rem;color:#7f8fa6;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.mkt-row{display:flex;justify-content:space-between;margin-bottom:4px;font-size:0.8rem}
.mkt-label{color:#7f8fa6}
.mkt-val{font-weight:600}
.vix-high{color:#ff4444}
.vix-normal{color:#00ff88}
.vix-low{color:#f1c40f}

/* Footer */
.footer{text-align:center;font-size:0.7rem;color:#444;padding:10px 0}

/* Refresh indicator */
.refresh-dot{width:6px;height:6px;border-radius:50%;background:#00ff88;display:inline-block;margin-right:6px;transition:opacity 0.3s}
.refresh-dot.stale{background:#ff4444}

/* Clickable rows */
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:rgba(41,128,185,0.15) !important}

/* Chart Modal */
.chart-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:1000;justify-content:center;align-items:center}
.chart-overlay.active{display:flex}
.chart-container{background:#16213e;border:1px solid #0f3460;border-radius:12px;width:90%;max-width:1000px;max-height:90vh;overflow:hidden;position:relative}
.chart-header{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;border-bottom:1px solid #0f3460}
.chart-header h3{font-size:0.95rem}
.chart-header .chart-info{font-size:0.78rem;color:#7f8fa6}
.chart-close{background:none;border:none;color:#ff4444;font-size:1.4rem;cursor:pointer;padding:0 6px;font-weight:700}
.chart-close:hover{color:#ff6666}
.chart-body{height:400px;position:relative}
.chart-legend{display:flex;gap:18px;padding:8px 18px;font-size:0.75rem;border-top:1px solid #0f3460;color:#7f8fa6}
.chart-legend .leg-item{display:flex;align-items:center;gap:4px}
.chart-legend .leg-line{width:20px;height:3px;border-radius:1px}
</style>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>Live Trading Dashboard<span class="ver" id="hdr-ver">v24</span>
    <span class="badge badge-shadow" id="hdr-mode">SHADOW</span>
  </h1>
  <div class="header-right">
    <span class="order-type">LIMIT ORDERS</span>
    <span><span class="refresh-dot" id="refresh-dot"></span><span id="hdr-ts">—</span></span>
  </div>
</div>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="card-label">Capital</div>
    <div class="card-value mono" id="c-capital">—</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&amp;L</div>
    <div class="card-value mono" id="c-pnl">—</div>
    <div class="card-sub"><span id="c-pnl-ur">—</span> unrealized &nbsp;|&nbsp; <span id="c-pnl-r">—</span> realized</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value mono" id="c-wr">—</div>
    <div class="card-sub"><span id="c-wl">— W / — L</span></div>
  </div>
  <div class="card">
    <div class="card-label">Trades Today</div>
    <div class="card-value mono" id="c-trades">—</div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value mono" id="c-open">—</div>
  </div>
</div>

<!-- Market Indicators -->
<div class="mkt-grid" id="mkt-grid">
  <div class="mkt-card">
    <h4>India VIX</h4>
    <div class="mkt-row"><span class="mkt-label">Value</span><span class="mkt-val mono" id="mkt-vix">—</span></div>
  </div>
  <div class="mkt-card" id="mkt-nifty-card">
    <h4>NIFTY</h4>
    <div class="mkt-row"><span class="mkt-label">Spot</span><span class="mkt-val mono" id="mkt-nifty-spot">—</span></div>
    <div class="mkt-row"><span class="mkt-label">PCR</span><span class="mkt-val mono" id="mkt-nifty-pcr">—</span></div>
    <div class="mkt-row"><span class="mkt-label">ATM IV</span><span class="mkt-val mono" id="mkt-nifty-iv">—</span></div>
    <div class="mkt-row"><span class="mkt-label">VWAP</span><span class="mkt-val mono" id="mkt-nifty-vwap">—</span></div>
    <div class="mkt-row"><span class="mkt-label">Total OI</span><span class="mkt-val mono" id="mkt-nifty-oi">—</span></div>
  </div>
  <div class="mkt-card" id="mkt-bnf-card">
    <h4>BANKNIFTY</h4>
    <div class="mkt-row"><span class="mkt-label">Spot</span><span class="mkt-val mono" id="mkt-bnf-spot">—</span></div>
    <div class="mkt-row"><span class="mkt-label">PCR</span><span class="mkt-val mono" id="mkt-bnf-pcr">—</span></div>
    <div class="mkt-row"><span class="mkt-label">ATM IV</span><span class="mkt-val mono" id="mkt-bnf-iv">—</span></div>
    <div class="mkt-row"><span class="mkt-label">VWAP</span><span class="mkt-val mono" id="mkt-bnf-vwap">—</span></div>
    <div class="mkt-row"><span class="mkt-label">Total OI</span><span class="mkt-val mono" id="mkt-bnf-oi">—</span></div>
  </div>
  <div class="mkt-card" id="mkt-sensex-card">
    <h4>SENSEX</h4>
    <div class="mkt-row"><span class="mkt-label">Spot</span><span class="mkt-val mono" id="mkt-sensex-spot">—</span></div>
    <div class="mkt-row"><span class="mkt-label">PCR</span><span class="mkt-val mono" id="mkt-sensex-pcr">—</span></div>
    <div class="mkt-row"><span class="mkt-label">ATM IV</span><span class="mkt-val mono" id="mkt-sensex-iv">—</span></div>
    <div class="mkt-row"><span class="mkt-label">VWAP</span><span class="mkt-val mono" id="mkt-sensex-vwap">—</span></div>
    <div class="mkt-row"><span class="mkt-label">Total OI</span><span class="mkt-val mono" id="mkt-sensex-oi">—</span></div>
  </div>
</div>

<!-- Open Positions -->
<div class="section">
  <div class="section-title">Open Positions <span class="count" id="open-count">0</span></div>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Strategy</th><th>Dir</th><th>Strike</th>
        <th>Entry</th><th>Current</th><th>P&amp;L</th><th>Gain%</th>
        <th>Peak%</th><th>TSL</th><th>Signal</th><th>Source</th><th>Elapsed</th>
      </tr>
    </thead>
    <tbody id="open-tbody">
      <tr class="empty-row"><td colspan="13">No open positions</td></tr>
    </tbody>
  </table>
</div>

<!-- Closed Trades -->
<div class="section">
  <div class="section-title">Closed Trades <span class="count" id="closed-count">0</span></div>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Strategy</th><th>Dir</th><th>Strike</th>
        <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Signal</th><th>Source</th><th>Exit Reason</th><th>Duration</th>
      </tr>
    </thead>
    <tbody id="closed-tbody">
      <tr class="empty-row"><td colspan="11">No closed trades today</td></tr>
    </tbody>
  </table>
</div>

<!-- Strategy + Risk -->
<div class="bottom-grid">
  <div class="risk-panel">
    <h3>Strategy Breakdown</h3>
    <table class="strat-table">
      <thead><tr><th>Strategy</th><th>Trades</th><th>P&amp;L</th></tr></thead>
      <tbody id="strat-tbody">
        <tr class="empty-row"><td colspan="3">No data</td></tr>
      </tbody>
    </table>
  </div>

  <div class="risk-panel">
    <h3>Risk Monitor</h3>
    <div class="risk-row">
      <span>Daily Loss Remaining</span>
      <span class="mono" id="risk-remaining">—</span>
    </div>
    <div class="risk-bar-bg"><div class="risk-bar-fill" id="risk-bar" style="width:100%;background:#00ff88"></div></div>
    <div class="risk-row" style="margin-top:14px">
      <span>Circuit Breaker</span>
      <span id="risk-halted" class="green">OFF</span>
    </div>
    <div class="feature-row">
      <span class="feature-tag feature-on" id="feat-oi">OI Heatmap ON</span>
      <span class="feature-tag feature-on" id="feat-vr">V-Reversal ON</span>
    </div>
  </div>
</div>

<!-- Chart Modal -->
<div class="chart-overlay" id="chart-overlay" onclick="if(event.target===this)closeChart()">
  <div class="chart-container">
    <div class="chart-header">
      <h3 id="chart-title">Premium Chart</h3>
      <div>
        <span class="chart-info" id="chart-info"></span>
        <button class="chart-close" onclick="closeChart()">&times;</button>
      </div>
    </div>
    <div class="chart-body" id="chart-body"></div>
    <div class="chart-legend">
      <div class="leg-item"><div class="leg-line" style="background:#2196F3"></div>Entry</div>
      <div class="leg-item"><div class="leg-line" style="background:#ff4444"></div>SL</div>
      <div class="leg-item"><div class="leg-line" style="background:#00ff88"></div>Target</div>
      <div class="leg-item"><div class="leg-line" style="background:#f1c40f"></div>TSL</div>
      <div class="leg-item"><div class="leg-line" style="background:#9b59b6"></div>TC/BC</div>
      <div class="leg-item"><div class="leg-line" style="background:#8e44ad"></div>Pivot</div>
      <div class="leg-item"><div class="leg-line" style="background:#e67e22"></div>VWAP</div>
      <div class="leg-item"><div class="leg-line" style="background:#1abc9c"></div>Swing H/L</div>
    </div>
  </div>
</div>

<div class="footer">Auto-refreshes every 5s &bull; All orders placed as LIMIT &bull; Dashboard v24 &bull; Click any trade row for premium chart</div>

<script>
const $ = id => document.getElementById(id);

function pnlColor(v) { return v > 0 ? 'green' : v < 0 ? 'red' : 'neutral'; }
function fmt(v, d=2) { return (v||0).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function fmtInt(v) { return (v||0).toLocaleString('en-IN'); }

function direction(sig) {
  if (!sig) return '—';
  if (sig.includes('CE')) return 'CE';
  if (sig.includes('PE')) return 'PE';
  return sig;
}

function elapsedStr(mins) {
  if (mins == null) return '—';
  if (mins < 60) return Math.round(mins) + 'm';
  return Math.floor(mins/60) + 'h ' + Math.round(mins%60) + 'm';
}

// Store all position data globally for chart access
let _allPositions = [];
let _allClosed = [];

function sigBar(score) {
  if (!score) return '<span class="mono" style="color:#555">—</span>';
  const w = Math.max(10, Math.min(60, score * 0.6));
  const cls = score >= 75 ? 'sig-strong' : score >= 55 ? 'sig-medium' : 'sig-weak';
  return `<span class="sig-bar ${cls}" style="width:${w}px"></span><span class="mono" style="font-size:0.72rem">${score}</span>`;
}

function srcBadge(src) {
  if (!src) return '—';
  const cls = src === 'ZERODHA' ? 'src-zerodha' : 'src-dhan';
  return `<span class="src-badge ${cls}">${src}</span>`;
}

function renderOpen(positions) {
  _allPositions = positions || [];
  const tb = $('open-tbody');
  if (!positions || positions.length === 0) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="13">No open positions</td></tr>';
    $('open-count').textContent = '0';
    return;
  }
  $('open-count').textContent = positions.length;
  let html = '';
  for (const p of positions) {
    const pnl = p.unrealized_pnl || 0;
    const peakPct = p.peak_premium && p.entry_premium
      ? ((p.peak_premium - p.entry_premium) / p.entry_premium * 100) : 0;
    html += `<tr class="clickable" onclick="openChart('${p.id}','open')">
      <td><b>${p.symbol||'—'}</b></td>
      <td>${p.strategy||'—'}</td>
      <td>${direction(p.signal_type)}</td>
      <td class="mono">${p.strike||'—'}</td>
      <td class="mono">${fmt(p.entry_premium)}</td>
      <td class="mono">${fmt(p.current_premium)}</td>
      <td class="mono ${pnlColor(pnl)}">${pnl>=0?'+':''}${fmt(pnl)}</td>
      <td class="mono ${pnlColor(p.premium_gain_pct)}">${fmt(p.premium_gain_pct,1)}%</td>
      <td class="mono">${fmt(peakPct,1)}%</td>
      <td class="mono">${fmt(p.trailing_sl)}</td>
      <td>${sigBar(p.quality_score)}</td>
      <td>${srcBadge(p.data_source)}</td>
      <td class="mono">${elapsedStr(p.elapsed_min)}</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

function renderClosed(trades) {
  _allClosed = trades || [];
  const tb = $('closed-tbody');
  if (!trades || trades.length === 0) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="11">No closed trades today</td></tr>';
    $('closed-count').textContent = '0';
    return;
  }
  $('closed-count').textContent = trades.length;
  let html = '';
  for (const t of trades) {
    const pnl = t.realized_pnl || 0;
    const dur = t.elapsed_min != null ? elapsedStr(t.elapsed_min) : '—';
    html += `<tr class="clickable" onclick="openChart('${t.id}','closed')">
      <td><b>${t.symbol||'—'}</b></td>
      <td>${t.strategy||'—'}</td>
      <td>${direction(t.signal_type)}</td>
      <td class="mono">${t.strike||'—'}</td>
      <td class="mono">${fmt(t.entry_premium)}</td>
      <td class="mono">${fmt(t.exit_premium)}</td>
      <td class="mono ${pnlColor(pnl)}">${pnl>=0?'+':''}${fmt(pnl)}</td>
      <td>${sigBar(t.quality_score)}</td>
      <td>${srcBadge(t.data_source)}</td>
      <td>${t.exit_reason||'—'}</td>
      <td class="mono">${dur}</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

function renderStratBreakdown(positions, closedTrades) {
  const map = {};
  const all = [...(positions||[]), ...(closedTrades||[])];
  for (const t of all) {
    const s = t.strategy || 'Unknown';
    if (!map[s]) map[s] = {count:0, pnl:0};
    map[s].count++;
    map[s].pnl += (t.realized_pnl || t.unrealized_pnl || 0);
  }
  const tb = $('strat-tbody');
  const keys = Object.keys(map);
  if (keys.length === 0) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="3">No data</td></tr>';
    return;
  }
  let html = '';
  for (const s of keys.sort()) {
    const d = map[s];
    html += `<tr>
      <td>${s}</td>
      <td class="mono">${d.count}</td>
      <td class="mono ${pnlColor(d.pnl)}">${d.pnl>=0?'+':''}${fmt(d.pnl)}</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

function renderMarket(data) {
  const mkt = data.market_data || {};

  // VIX
  const vix = mkt.vix || 0;
  const vixEl = $('mkt-vix');
  vixEl.textContent = vix > 0 ? vix.toFixed(2) : '—';
  vixEl.className = 'mkt-val mono ' + (vix > 20 ? 'vix-high' : vix > 13 ? 'vix-normal' : 'vix-low');

  // Per-symbol
  const syms = [['nifty','NIFTY'],['bnf','BANKNIFTY'],['sensex','SENSEX']];
  for (const [key, sym] of syms) {
    const d = mkt[sym] || {};
    $(`mkt-${key}-spot`).textContent = d.spot ? fmtInt(Math.round(d.spot)) : '—';
    const pcrEl = $(`mkt-${key}-pcr`);
    const pcr = d.pcr || 0;
    pcrEl.textContent = pcr > 0 ? pcr.toFixed(2) : '—';
    pcrEl.className = 'mkt-val mono ' + (pcr > 1.2 ? 'green' : pcr < 0.8 ? 'red' : 'yellow');
    $(`mkt-${key}-iv`).textContent = d.atm_iv > 0 ? d.atm_iv.toFixed(1) + '%' : '—';
    $(`mkt-${key}-vwap`).textContent = d.vwap > 0 ? fmtInt(Math.round(d.vwap)) : '—';
    $(`mkt-${key}-oi`).textContent = d.total_oi > 0 ? (d.total_oi / 100000).toFixed(1) + 'L' : '—';
  }
}

function renderRisk(data) {
  const risk = data.risk || {};
  const remaining = risk.daily_loss_remaining || 0;
  const limit = data.daily_loss_limit || 10000;
  const pct = Math.max(0, Math.min(100, remaining / limit * 100));

  $('risk-remaining').textContent = '\u20B9' + fmtInt(remaining);
  const bar = $('risk-bar');
  bar.style.width = pct + '%';
  bar.style.background = pct > 50 ? '#00ff88' : pct > 20 ? '#f1c40f' : '#ff4444';

  const halted = risk.halted;
  const el = $('risk-halted');
  el.textContent = halted ? 'HALTED' : 'OFF';
  el.className = halted ? 'red' : 'green';

  const oi = $('feat-oi');
  oi.textContent = 'OI Heatmap ' + (data.oi_heatmap ? 'ON' : 'OFF');
  oi.className = 'feature-tag ' + (data.oi_heatmap ? 'feature-on' : 'feature-off');

  const vr = $('feat-vr');
  vr.textContent = 'V-Reversal ' + (data.reversal_detector ? 'ON' : 'OFF');
  vr.className = 'feature-tag ' + (data.reversal_detector ? 'feature-on' : 'feature-off');
}

function update(data) {
  // Header
  $('hdr-ver').textContent = data.version || 'v24';
  const modeEl = $('hdr-mode');
  modeEl.textContent = data.mode || 'SHADOW';
  modeEl.className = 'badge ' + (data.mode === 'LIVE' ? 'badge-live' : 'badge-shadow');
  $('hdr-ts').textContent = data.timestamp ? data.timestamp.replace('T',' ') : '—';

  // Cards
  $('c-capital').textContent = '\u20B9' + fmtInt(data.capital);
  const totalPnl = data.total_pnl || 0;
  const pnlEl = $('c-pnl');
  pnlEl.textContent = (totalPnl>=0?'+':'') + '\u20B9' + fmt(totalPnl);
  pnlEl.className = 'card-value mono ' + pnlColor(totalPnl);

  $('c-pnl-ur').textContent = '\u20B9' + fmt(data.unrealized_pnl);
  $('c-pnl-r').textContent = '\u20B9' + fmt(data.realized_pnl);

  const wr = data.win_rate || 0;
  const wrEl = $('c-wr');
  wrEl.textContent = fmt(wr,1) + '%';
  wrEl.className = 'card-value mono ' + (wr >= 50 ? 'green' : wr > 0 ? 'yellow' : 'neutral');
  $('c-wl').textContent = (data.winners||0) + ' W / ' + (data.losers||0) + ' L';

  $('c-trades').textContent = (data.trades_today||0) + ' / ' + (data.max_trades||24);
  $('c-open').textContent = data.open_count || 0;

  // Tables
  const openPos = (data.positions||[]).filter(p => !p.closed);
  renderOpen(openPos);
  renderClosed(data.closed_trades);
  renderStratBreakdown(openPos, data.closed_trades);
  renderMarket(data);
  renderRisk(data);
}

// ── Chart Logic ──────────────────────────────────────────────────────────
let _chart = null;
let _chartSeries = null;

function openChart(posId, type) {
  const list = type === 'open' ? _allPositions : _allClosed;
  const pos = list.find(p => p.id === posId);
  if (!pos) return;

  const chart_data = pos.chart;
  const dir = direction(pos.signal_type);

  // Set title & info
  $('chart-title').textContent = `${pos.symbol} ${pos.strategy} ${dir} ${pos.strike}`;
  const pnl = type === 'open' ? (pos.unrealized_pnl||0) : (pos.realized_pnl||0);
  const pnlStr = (pnl >= 0 ? '+' : '') + '\u20B9' + fmt(pnl);
  $('chart-info').innerHTML = `Entry: \u20B9${fmt(pos.entry_premium)} | ` +
    (type === 'closed' ? `Exit: \u20B9${fmt(pos.exit_premium)} | ` : `Current: \u20B9${fmt(pos.current_premium)} | `) +
    `<span class="${pnlColor(pnl)}">${pnlStr}</span>`;

  // Show overlay
  $('chart-overlay').classList.add('active');

  // Clear previous chart
  const container = $('chart-body');
  container.innerHTML = '';

  // Check if we have candle data
  if (!chart_data || !chart_data.candles || chart_data.candles.length === 0) {
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#7f8fa6;font-style:italic">No premium tick data available yet. Chart appears after first price update.</div>';
    return;
  }

  // Create chart
  _chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 400,
    layout: {
      background: { type: 'solid', color: '#16213e' },
      textColor: '#aaa',
      fontSize: 12,
    },
    grid: {
      vertLines: { color: 'rgba(15,52,96,0.5)' },
      horzLines: { color: 'rgba(15,52,96,0.5)' },
    },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      borderColor: '#0f3460',
    },
    rightPriceScale: {
      borderColor: '#0f3460',
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
  });

  // Add candlestick series
  _chartSeries = _chart.addCandlestickSeries({
    upColor: '#00ff88',
    downColor: '#ff4444',
    borderUpColor: '#00ff88',
    borderDownColor: '#ff4444',
    wickUpColor: '#00ff88',
    wickDownColor: '#ff4444',
  });

  _chartSeries.setData(chart_data.candles);

  // Price lines: Entry (blue), SL (red), Target (green), Trailing SL (yellow dashed)
  if (chart_data.entry_price) {
    _chartSeries.createPriceLine({
      price: chart_data.entry_price,
      color: '#2196F3',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: 'Entry ' + fmt(chart_data.entry_price),
    });
  }

  if (chart_data.sl) {
    _chartSeries.createPriceLine({
      price: chart_data.sl,
      color: '#ff4444',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: 'SL ' + fmt(chart_data.sl),
    });
  }

  if (chart_data.target) {
    _chartSeries.createPriceLine({
      price: chart_data.target,
      color: '#00ff88',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: 'Target ' + fmt(chart_data.target),
    });
  }

  if (chart_data.trailing_sl && chart_data.trailing_sl !== chart_data.sl) {
    _chartSeries.createPriceLine({
      price: chart_data.trailing_sl,
      color: '#f1c40f',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: 'TSL ' + fmt(chart_data.trailing_sl),
    });
  }

  // Exit marker (for closed trades)
  if (chart_data.exit_price && chart_data.exit_time) {
    _chartSeries.createPriceLine({
      price: chart_data.exit_price,
      color: '#e67e22',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true,
      title: 'Exit ' + fmt(chart_data.exit_price),
    });
  }

  // Strategy-specific indicator overlays
  const ind = chart_data.indicator_levels || pos.indicator_levels || {};
  const strat = chart_data.strategy || pos.strategy || '';

  // CPR levels (for CPR strategy)
  if (strat === 'CPR' || ind.pivot) {
    if (ind.tc) _chartSeries.createPriceLine({price:ind.tc, color:'#9b59b6', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'TC '+ind.tc});
    if (ind.bc) _chartSeries.createPriceLine({price:ind.bc, color:'#9b59b6', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'BC '+ind.bc});
    if (ind.pivot) _chartSeries.createPriceLine({price:ind.pivot, color:'#8e44ad', lineWidth:1, lineStyle:1, axisLabelVisible:true, title:'Pivot '+ind.pivot});
  }

  // Camarilla levels
  if (ind.r1) _chartSeries.createPriceLine({price:ind.r1, color:'rgba(0,255,136,0.4)', lineWidth:1, lineStyle:2, axisLabelVisible:false, title:'R1'});
  if (ind.s1) _chartSeries.createPriceLine({price:ind.s1, color:'rgba(255,68,68,0.4)', lineWidth:1, lineStyle:2, axisLabelVisible:false, title:'S1'});

  // VWAP (for all strategies)
  if (ind.vwap && ind.vwap > 0) {
    _chartSeries.createPriceLine({price:ind.vwap, color:'#e67e22', lineWidth:1, lineStyle:1, axisLabelVisible:true, title:'VWAP '+ind.vwap});
  }

  // Wave swing levels
  if (strat === 'Wave') {
    if (ind.swing_high) _chartSeries.createPriceLine({price:ind.swing_high, color:'#1abc9c', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'SwH '+ind.swing_high});
    if (ind.swing_low) _chartSeries.createPriceLine({price:ind.swing_low, color:'#e74c3c', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'SwL '+ind.swing_low});
  }

  _chart.timeScale().fitContent();

  // Resize handler
  const ro = new ResizeObserver(() => {
    if (_chart) _chart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}

function closeChart() {
  $('chart-overlay').classList.remove('active');
  if (_chart) {
    _chart.remove();
    _chart = null;
    _chartSeries = null;
  }
}

// ESC to close chart
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeChart(); });

// ── Fetch loop ──────────────────────────────────────────────────────────
let lastTs = '';
async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    update(data);
    const dot = $('refresh-dot');
    dot.className = 'refresh-dot';
    // Flash effect
    dot.style.opacity = '0.3';
    setTimeout(() => dot.style.opacity = '1', 200);
    lastTs = data.timestamp || '';
  } catch(e) {
    $('refresh-dot').className = 'refresh-dot stale';
    console.error('Refresh failed:', e);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Live Trading Dashboard v24")
    print(f"  Status file : {STATUS_FILE}")
    print(f"  URL         : http://localhost:5555")
    print("  Order type  : LIMIT")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5555, debug=False)
