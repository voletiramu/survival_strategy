#!/usr/bin/env python3
"""Live Trading Dashboard — Flask server for monitoring algo trading positions.
Reads portfolio state JSON files and serves a mobile-friendly dashboard.
"""
import json
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Portfolio state file paths (auto-detect Vultr vs local)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EQUITY_STATE = os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json')
COMMODITY_STATE = os.path.join(BASE_DIR, 'paper_trades_commodity', 'commodity_portfolio_state.json')
CRYPTO_STATE = os.path.join(BASE_DIR, 'paper_trades_crypto', 'crypto_portfolio_state.json')

EQUITY_CAPITAL = 300000
COMMODITY_CAPITAL = 300000


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

        positions.append({
            'id': pos.get('id', ''),
            'symbol': pos.get('symbol', pos.get('commodity', '')),
            'strategy': pos.get('strategy', ''),
            'type': pos.get('signal_type', ''),
            'strike': pos.get('strike', 0),
            'entry_price': pos.get('entry_premium', 0),
            'current_price': pos.get('current_premium', 0),
            'pnl': round(pnl, 2),
            'entry_time': entry_time,
            'duration': duration_str,
            'delta': pos.get('delta', 0),
            'tsl': pos.get('trailing_sl', None),
            'target': details.get('target', 0),
            'sl': details.get('sl', 0),
            'is_sell': pos.get('is_sell', False),
            'score': pos.get('quality_score', details.get('quality_score', '')),
            'market': market,
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

        trades.append({
            'id': t.get('id', ''),
            'symbol': t.get('symbol', t.get('commodity', '')),
            'strategy': t.get('strategy', ''),
            'type': t.get('signal_type', ''),
            'strike': t.get('strike', 0),
            'entry_price': t.get('entry_premium', 0),
            'exit_price': t.get('exit_premium', t.get('exit_price', 0)),
            'pnl': round(pnl, 2),
            'exit_reason': t.get('exit_reason', ''),
            'hold_duration': hold_str,
            'entry_time': ts,
            'exit_time': exit_ts,
            'is_sell': t.get('is_sell', False),
            'market': market,
        })
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
    invested = sum(
        p['entry_price'] * 65 if not p['is_sell'] else 100000
        for p in positions
    )
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
    invested = sum(
        p['entry_price'] * 10 if not p['is_sell'] else 15000
        for p in positions
    )
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

    today = datetime.now().strftime('%Y-%m-%d')
    eq_daily = eq_data.get('daily_pnl', {}).get(today, 0) if eq_data else 0
    cm_daily = cm_data.get('daily_pnl', {}).get(today, 0) if cm_data else 0

    return jsonify({
        'total_pnl': round(total_open_pnl + total_closed_pnl, 2),
        'open_pnl': round(total_open_pnl, 2),
        'closed_pnl': round(total_closed_pnl, 2),
        'equity_daily_pnl': round(eq_daily, 2),
        'commodity_daily_pnl': round(cm_daily, 2),
        'open_positions': len(eq_positions) + len(cm_positions),
        'closed_today': len(all_closed),
        'win_rate': win_rate,
        'wins': wins,
        'losses': losses,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'equity_capital': round(eq_capital, 2),
        'commodity_capital': round(cm_capital, 2),
        'last_updated': datetime.now().isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)
