#!/usr/bin/env python3
"""Live Trading Dashboard — Flask server for monitoring algo trading positions.
Reads portfolio state JSON files and serves a mobile-friendly dashboard.
v2.4: Added lots, capital invested, capital available, capital after close.
"""
import json
import os
import csv
import io
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, Response, request

app = Flask(__name__, static_folder='static', static_url_path='/static')

# Portfolio state file paths (auto-detect Vultr vs local)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EQUITY_STATE = os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json')
COMMODITY_STATE = os.path.join(BASE_DIR, 'paper_trades_commodity', 'commodity_portfolio_state.json')
CRYPTO_STATE = os.path.join(BASE_DIR, 'paper_trades_crypto', 'crypto_portfolio_state.json')

EQUITY_CAPITAL = 300000
COMMODITY_CAPITAL = 300000

# Margin per lot for capital invested calculation
MARGIN_PER_LOT = {'NIFTY': 100000, 'BANKNIFTY': 95000, 'SENSEX': 70000}
MCX_MARGINS = {'GOLDM': 15000, 'SILVERM': 15000, 'CRUDEOILM': 8000,
               'GOLD': 100000, 'SILVER': 80000, 'NATURALGAS': 70000, 'COPPER': 60000}


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def parse_positions(data, market='equity'):
    """Extract open positions with computed fields."""
    if not data:
        return []
    positions = []
    for pos in data.get('positions', []):
        entry_time = pos.get('timestamp', '')
        try:
            dt = datetime.fromisoformat(entry_time)
            duration = datetime.now() - dt
            duration_str = f"{int(duration.total_seconds() // 3600)}h {int((duration.total_seconds() % 3600) // 60)}m"
        except Exception:
            duration_str = 'N/A'
            dt = None

        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        pnl = pos.get('unrealized_pnl', 0)

        # v2.4: Compute capital invested
        lot_size = pos.get('lot_size', 0)
        num_lots = pos.get('num_lots', 1)
        multiplier = pos.get('multiplier', 1)
        is_sell = pos.get('is_sell', False)
        symbol = pos.get('symbol', pos.get('commodity', ''))

        if is_sell:
            if market == 'equity':
                cap_invested = MARGIN_PER_LOT.get(symbol, 100000) * num_lots
            else:
                cap_invested = MCX_MARGINS.get(symbol, 15000) * num_lots
        else:
            cap_invested = pos.get('entry_premium', 0) * lot_size * multiplier

        # v7.6: PnL percentage
        pnl_pct = round(pnl / cap_invested * 100, 1) if cap_invested > 0 else 0

        positions.append({
            'id': pos.get('id', ''),
            'symbol': symbol,
            'strategy': pos.get('strategy', ''),
            'type': pos.get('signal_type', ''),
            'strike': pos.get('strike', 0),
            'entry_price': pos.get('entry_premium', 0),
            'current_price': pos.get('current_premium', 0),
            'pnl': round(pnl, 2),
            'pnl_pct': pnl_pct,
            'entry_time': entry_time,
            'duration': duration_str,
            'delta': pos.get('delta', 0),
            'tsl': pos.get('trailing_sl', None),
            'target': details.get('target', 0),
            'sl': details.get('sl', 0),
            'is_sell': is_sell,
            'score': pos.get('quality_score', details.get('quality_score', '')),
            'market': market,
            # v2.4: New fields
            'lot_size': lot_size,
            'num_lots': num_lots,
            'multiplier': multiplier,
            'capital_invested': round(cap_invested, 2),
            'capital_available': pos.get('capital_available', 0),
        })
    return positions


def parse_closed_trades(data, market='equity'):
    """Extract today's closed trades."""
    if not data:
        return []
    today = datetime.now().strftime('%Y-%m-%d')
    trades = []
    for t in data.get('closed_trades', []):
        ts = t.get('timestamp', t.get('entry_time', ''))
        exit_ts = t.get('exit_time', t.get('closed_at', ''))
        # Filter for today only
        if today not in ts and today not in exit_ts:
            continue

        details = t.get('details', {}) if isinstance(t.get('details'), dict) else {}
        pnl = t.get('pnl', 0)

        # Compute hold duration
        try:
            entry_dt = datetime.fromisoformat(ts)
            exit_dt = datetime.fromisoformat(exit_ts) if exit_ts else datetime.now()
            hold_mins = int((exit_dt - entry_dt).total_seconds() // 60)
            hold_str = f"{hold_mins // 60}h {hold_mins % 60}m" if hold_mins >= 60 else f"{hold_mins}m"
        except Exception:
            hold_str = 'N/A'

        # v7.6: PnL percentage
        entry_prem = t.get('entry_premium', 0)
        t_lot_size = t.get('lot_size', 0)
        t_mult = t.get('multiplier', 1)
        t_is_sell = t.get('is_sell', False)
        if t_is_sell:
            t_cap = MARGIN_PER_LOT.get(t.get('symbol', t.get('commodity', '')), 100000) * t.get('num_lots', 1) if market == 'equity' else MCX_MARGINS.get(t.get('symbol', t.get('commodity', '')), 15000) * t.get('num_lots', 1)
        else:
            t_cap = entry_prem * t_lot_size * t_mult
        t_pnl_pct = round(pnl / t_cap * 100, 1) if t_cap > 0 else 0

        trades.append({
            'id': t.get('id', ''),
            'symbol': t.get('symbol', t.get('commodity', '')),
            'strategy': t.get('strategy', ''),
            'type': t.get('signal_type', ''),
            'strike': t.get('strike', 0),
            'entry_price': t.get('entry_premium', 0),
            'exit_price': t.get('exit_premium', t.get('exit_price', 0)),
            'pnl': round(pnl, 2),
            'pnl_pct': t_pnl_pct,
            'exit_reason': t.get('exit_reason', ''),
            'hold_duration': hold_str,
            'entry_time': ts,
            'exit_time': exit_ts,
            'is_sell': t.get('is_sell', False),
            'market': market,
            # v2.4: New fields
            'num_lots': t.get('num_lots', 1),
            'lot_size': t.get('lot_size', 0),
            'multiplier': t.get('multiplier', 1),
            'capital_after': t.get('capital_after', 0),
        })

    # Sort by exit time and compute running capital_after if not present
    trades.sort(key=lambda x: x.get('exit_time', ''))
    default_cap = EQUITY_CAPITAL if market == 'equity' else COMMODITY_CAPITAL
    capital = data.get('capital', default_cap) if data else default_cap
    if trades and not trades[-1].get('capital_after'):
        total_today = sum(t['pnl'] for t in trades)
        running = capital - total_today
        for t in trades:
            running += t['pnl']
            t['capital_after'] = round(running, 2)

    return trades


@app.route('/')
def dashboard():
    return render_template('dashboard.html')


@app.route('/api/equity')
def api_equity():
    data = load_json(EQUITY_STATE)
    if not data:
        return jsonify({'error': 'No equity data'}), 404
    positions = parse_positions(data, 'equity')
    closed = parse_closed_trades(data, 'equity')
    capital = data.get('capital', EQUITY_CAPITAL)
    invested = sum(p['capital_invested'] for p in positions)
    return jsonify({
        'positions': positions,
        'closed_trades': closed,
        'capital': round(capital, 2),
        'invested': round(invested, 2),
        'available': round(capital - invested, 2),
        'daily_pnl': data.get('daily_pnl', {}),
        'last_updated': data.get('last_updated', ''),
    })


@app.route('/api/commodity')
def api_commodity():
    data = load_json(COMMODITY_STATE)
    if not data:
        return jsonify({'error': 'No commodity data'}), 404
    positions = parse_positions(data, 'commodity')
    closed = parse_closed_trades(data, 'commodity')
    capital = data.get('capital', COMMODITY_CAPITAL)
    invested = sum(p['capital_invested'] for p in positions)
    return jsonify({
        'positions': positions,
        'closed_trades': closed,
        'capital': round(capital, 2),
        'invested': round(invested, 2),
        'available': round(capital - invested, 2),
        'last_updated': data.get('last_updated', ''),
    })


@app.route('/api/summary')
def api_summary():
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)

    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    eq_closed = parse_closed_trades(eq_data, 'equity') if eq_data else []
    cm_closed = parse_closed_trades(cm_data, 'commodity') if cm_data else []

    all_closed = eq_closed + cm_closed
    total_open_pnl = sum(p['pnl'] for p in eq_positions + cm_positions)
    total_closed_pnl = sum(t['pnl'] for t in all_closed)
    wins = sum(1 for t in all_closed if t['pnl'] > 0)
    losses = sum(1 for t in all_closed if t['pnl'] <= 0)
    win_rate = round(wins / max(wins + losses, 1) * 100, 1)

    best_trade = max(all_closed, key=lambda t: t['pnl']) if all_closed else None
    worst_trade = min(all_closed, key=lambda t: t['pnl']) if all_closed else None

    eq_capital = eq_data.get('capital', EQUITY_CAPITAL) if eq_data else EQUITY_CAPITAL
    cm_capital = cm_data.get('capital', COMMODITY_CAPITAL) if cm_data else COMMODITY_CAPITAL

    eq_invested = sum(p['capital_invested'] for p in eq_positions)
    cm_invested = sum(p['capital_invested'] for p in cm_positions)

    today = datetime.now().strftime('%Y-%m-%d')
    eq_daily = eq_data.get('daily_pnl', {}).get(today, 0) if eq_data else 0
    cm_daily = cm_data.get('daily_pnl', {}).get(today, 0) if cm_data else 0

    total_pnl = total_open_pnl + total_closed_pnl
    total_capital = eq_capital + cm_capital
    total_pnl_pct = round(total_pnl / total_capital * 100, 2) if total_capital > 0 else 0
    eq_pnl_pct = round(eq_daily / eq_capital * 100, 2) if eq_capital > 0 else 0
    cm_pnl_pct = round(cm_daily / cm_capital * 100, 2) if cm_capital > 0 else 0

    return jsonify({
        'total_pnl': round(total_pnl, 2),
        'total_pnl_pct': total_pnl_pct,
        'open_pnl': round(total_open_pnl, 2),
        'closed_pnl': round(total_closed_pnl, 2),
        'equity_daily_pnl': round(eq_daily, 2),
        'equity_daily_pnl_pct': eq_pnl_pct,
        'commodity_daily_pnl': round(cm_daily, 2),
        'commodity_daily_pnl_pct': cm_pnl_pct,
        'open_positions': len(eq_positions) + len(cm_positions),
        'closed_today': len(all_closed),
        'win_rate': win_rate,
        'wins': wins,
        'losses': losses,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'equity_capital': round(eq_capital, 2),
        'commodity_capital': round(cm_capital, 2),
        'equity_available': round(eq_capital - eq_invested, 2),
        'commodity_available': round(cm_capital - cm_invested, 2),
        'last_updated': datetime.now().isoformat(),
    })


# ====================================================================
# EXPORT ENDPOINTS
# ====================================================================
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)


def _get_all_closed_trades(market='all'):
    """Get ALL closed trades (not just today) from portfolio state."""
    trades = []
    if market in ('all', 'equity'):
        eq_data = load_json(EQUITY_STATE)
        if eq_data:
            for t in eq_data.get('closed_trades', []):
                details = t.get('details', {}) if isinstance(t.get('details'), dict) else {}
                trades.append({
                    'timestamp': t.get('timestamp', ''),
                    'exit_time': t.get('exit_time', ''),
                    'symbol': t.get('symbol', ''),
                    'strategy': t.get('strategy', ''),
                    'type': t.get('signal_type', ''),
                    'strike': t.get('strike', 0),
                    'entry_price': t.get('entry_premium', 0),
                    'exit_price': t.get('exit_premium', 0),
                    'gross_pnl': t.get('gross_pnl', t.get('pnl', 0)),
                    'pnl': t.get('pnl', 0),
                    'total_slippage': t.get('total_slippage', 0),
                    'exit_reason': t.get('exit_reason', ''),
                    'num_lots': t.get('num_lots', 1),
                    'lot_size': t.get('lot_size', 0),
                    'capital_after': t.get('capital_after', 0),
                    'market': 'equity',
                })
    if market in ('all', 'commodity'):
        cm_data = load_json(COMMODITY_STATE)
        if cm_data:
            for t in cm_data.get('closed_trades', []):
                details = t.get('details', {}) if isinstance(t.get('details'), dict) else {}
                trades.append({
                    'timestamp': t.get('timestamp', ''),
                    'exit_time': t.get('exit_time', t.get('closed_at', '')),
                    'symbol': t.get('symbol', t.get('commodity', '')),
                    'strategy': t.get('strategy', ''),
                    'type': t.get('signal_type', ''),
                    'strike': t.get('strike', 0),
                    'entry_price': t.get('entry_premium', t.get('entry_price', 0)),
                    'exit_price': t.get('exit_premium', t.get('exit_price', 0)),
                    'gross_pnl': t.get('gross_pnl', t.get('pnl', 0)),
                    'pnl': t.get('pnl', 0),
                    'total_slippage': t.get('total_slippage', 0),
                    'exit_reason': t.get('exit_reason', ''),
                    'num_lots': t.get('num_lots', 1),
                    'lot_size': t.get('lot_size', 0),
                    'capital_after': t.get('capital_after', 0),
                    'market': 'commodity',
                })
    trades.sort(key=lambda x: x.get('exit_time', x.get('timestamp', '')))
    return trades


@app.route('/api/export/trades')
def export_trades():
    """Download all closed trades as CSV."""
    market = request.args.get('market', 'all')
    date_filter = request.args.get('date', None)

    trades = _get_all_closed_trades(market)

    # Optional date filter
    if date_filter:
        trades = [t for t in trades
                  if date_filter in t.get('timestamp', '') or date_filter in t.get('exit_time', '')]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        'timestamp', 'exit_time', 'symbol', 'strategy', 'type', 'strike',
        'entry_price', 'exit_price', 'gross_pnl', 'pnl', 'total_slippage',
        'exit_reason', 'num_lots', 'lot_size', 'capital_after', 'market'
    ])
    writer.writeheader()
    writer.writerows(trades)

    today_str = datetime.now().strftime('%Y%m%d')
    filename = f"trades_{market}_{today_str}.csv"

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/export/positions')
def export_positions():
    """Download open positions as CSV."""
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)

    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    all_positions = eq_positions + cm_positions

    output = io.StringIO()
    fieldnames = [
        'id', 'symbol', 'strategy', 'type', 'strike', 'entry_price',
        'current_price', 'pnl', 'entry_time', 'duration', 'delta',
        'tsl', 'target', 'sl', 'is_sell', 'score', 'market',
        'lot_size', 'num_lots', 'capital_invested', 'capital_available'
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(all_positions)

    today_str = datetime.now().strftime('%Y%m%d')
    filename = f"positions_{today_str}.csv"

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/export/summary')
def export_summary():
    """Download daily summary as JSON."""
    # Reuse existing summary endpoint data
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)

    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    eq_closed = parse_closed_trades(eq_data, 'equity') if eq_data else []
    cm_closed = parse_closed_trades(cm_data, 'commodity') if cm_data else []

    all_closed = eq_closed + cm_closed
    all_positions = eq_positions + cm_positions

    total_open_pnl = sum(p['pnl'] for p in all_positions)
    total_closed_pnl = sum(t['pnl'] for t in all_closed)
    wins = sum(1 for t in all_closed if t['pnl'] > 0)
    losses = sum(1 for t in all_closed if t['pnl'] <= 0)

    eq_capital = eq_data.get('capital', EQUITY_CAPITAL) if eq_data else EQUITY_CAPITAL
    cm_capital = cm_data.get('capital', COMMODITY_CAPITAL) if cm_data else COMMODITY_CAPITAL

    today = datetime.now().strftime('%Y-%m-%d')
    summary = {
        'date': today,
        'timestamp': datetime.now().isoformat(),
        'equity_capital': round(eq_capital, 2),
        'commodity_capital': round(cm_capital, 2),
        'total_capital': round(eq_capital + cm_capital, 2),
        'total_pnl': round(total_open_pnl + total_closed_pnl, 2),
        'open_pnl': round(total_open_pnl, 2),
        'closed_pnl': round(total_closed_pnl, 2),
        'open_positions': len(all_positions),
        'closed_today': len(all_closed),
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / max(wins + losses, 1) * 100, 1),
        'positions': all_positions,
        'closed_trades': all_closed,
    }

    today_str = datetime.now().strftime('%Y%m%d')
    filename = f"summary_{today_str}.json"

    return Response(
        json.dumps(summary, indent=2, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/export/save_report')
def save_daily_report():
    """Save daily report to reports/ directory (callable from dashboard or cron)."""
    trades = _get_all_closed_trades('all')
    today_str = datetime.now().strftime('%Y%m%d')

    # Save trades CSV
    trades_path = os.path.join(REPORTS_DIR, f'dashboard_report_{today_str}.csv')
    if trades:
        import csv as csv_mod
        with open(trades_path, 'w', newline='') as f:
            writer = csv_mod.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)

    return jsonify({
        'status': 'ok',
        'trades_saved': len(trades),
        'path': trades_path,
    })


@app.route('/static/sw.js')
def service_worker():
    """Serve service worker with correct scope header."""
    sw_path = os.path.join(BASE_DIR, 'static', 'sw.js')
    with open(sw_path, 'r') as f:
        content = f.read()
    resp = Response(content, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
