#!/usr/bin/env python3
"""Live Trading Dashboard — Flask server for monitoring algo trading positions.
Reads portfolio state JSON files and serves a mobile-friendly dashboard.
v2.4: Added lots, capital invested, capital available, capital after close.
"""
import json
import os
import csv
import io
import re
import glob
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, render_template, Response, request
from market_holidays import is_nse_holiday

app = Flask(__name__, static_folder='static', static_url_path='/static')

# Portfolio state file paths (auto-detect Vultr vs local)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EQUITY_STATE = os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json')
COMMODITY_STATE = os.path.join(BASE_DIR, 'paper_trades_commodity', 'commodity_portfolio_state.json')
CRYPTO_STATE = os.path.join(BASE_DIR, 'paper_trades_crypto', 'crypto_portfolio_state.json')
STOCK_STATE = os.path.join(BASE_DIR, 'stock_paper_trades', 'stock_portfolio_state.json')
OI_STATE = os.path.join(BASE_DIR, 'paper_trades_oi', 'portfolio_state.json')
STOCK_PAPER_DIR = os.path.join(BASE_DIR, 'stock_paper_trades')
OI_PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_oi')

EQUITY_CAPITAL = 300000
COMMODITY_CAPITAL = 300000
STOCK_CAPITAL = 200000
OI_CAPITAL = 300000

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
            if market in ('equity', 'oi'):
                cap_invested = MARGIN_PER_LOT.get(symbol, 100000) * num_lots
            elif market == 'stocks':
                cap_invested = pos.get('entry_premium', 0) * lot_size * 3  # Approx margin for stock SELL
            else:
                cap_invested = MCX_MARGINS.get(symbol, 15000) * num_lots
        else:
            cap_invested = pos.get('entry_premium', 0) * lot_size * multiplier

        # v7.6: PnL percentage
        pnl_pct = round(pnl / cap_invested * 100, 1) if cap_invested > 0 else 0

        # v9.1: Extract expiry from details, format for display
        raw_expiry = details.get('expiry', '')
        if raw_expiry:
            try:
                exp_dt = datetime.strptime(raw_expiry, '%d%b%Y')
                expiry_display = exp_dt.strftime('%d %b')
            except Exception:
                expiry_display = raw_expiry
        else:
            # Fallback: compute from entry_time + dte
            dte_val = pos.get('dte', 0)
            if dt and dte_val:
                exp_date = dt + timedelta(days=dte_val)
                expiry_display = exp_date.strftime('%d %b')
            else:
                expiry_display = '--'

        positions.append({
            'id': pos.get('id', ''),
            'symbol': symbol,
            'strategy': pos.get('strategy', ''),
            'type': pos.get('signal_type', ''),
            'strike': pos.get('strike', 0),
            'expiry': expiry_display,
            'entry_price': pos.get('entry_premium', 0),
            'current_price': pos.get('current_premium', 0),
            'pnl': round(pnl, 2),
            'pnl_pct': pnl_pct,
            'entry_time': entry_time,
            'duration': duration_str,
            'delta': pos.get('delta', 0),
            'gamma': pos.get('gamma', 0),
            'theta': pos.get('theta', 0),
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
            # v10: Enhanced fields for modern dashboard
            'iv': pos.get('iv', details.get('iv', 0)),
            'dte': pos.get('dte', details.get('dte', 0)),
            'entry_oi': pos.get('entry_oi', details.get('entry_oi', 0)),
            'entry_iv': pos.get('entry_iv', details.get('entry_iv', 0)),
            'entry_spot': pos.get('entry_spot', details.get('entry_spot', 0)),
            'peak_premium': pos.get('peak_premium', 0),
            'trough_premium': pos.get('trough_premium', 0),
            'breakeven_locked': pos.get('breakeven_locked', False),
            'reason': pos.get('reason', details.get('reason', '')),
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

        # v10.3: Filter out invalid/phantom trades
        entry_prem_check = t.get('entry_premium', 0)
        entry_oi_check = t.get('entry_oi', details.get('entry_oi', -1))
        data_source = details.get('data_source', '')
        # Skip Black-Scholes phantom trades (stock bot with no real data)
        if data_source == 'BLACK_SCHOLES':
            continue
        # Skip trades with zero entry premium (broken data)
        if entry_prem_check is not None and entry_prem_check <= 0:
            continue
        # Skip commodity trades with zero OI (illiquid phantom strikes)
        if market == 'commodity' and entry_oi_check == 0:
            continue

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
            if market in ('equity', 'oi'):
                t_cap = MARGIN_PER_LOT.get(t.get('symbol', t.get('commodity', '')), 100000) * t.get('num_lots', 1)
            elif market == 'stocks':
                t_cap = entry_prem * t_lot_size * 3  # Approx margin for stock SELL
            else:
                t_cap = MCX_MARGINS.get(t.get('symbol', t.get('commodity', '')), 15000) * t.get('num_lots', 1)
        else:
            t_cap = entry_prem * t_lot_size * t_mult
        t_pnl_pct = round(pnl / t_cap * 100, 1) if t_cap > 0 else 0

        # v9.1: Extract expiry from details
        raw_expiry = details.get('expiry', '')
        if raw_expiry:
            try:
                exp_dt = datetime.strptime(raw_expiry, '%d%b%Y')
                expiry_display = exp_dt.strftime('%d %b')
            except Exception:
                expiry_display = raw_expiry
        else:
            dte_val = t.get('dte', 0)
            try:
                entry_dt_exp = datetime.fromisoformat(ts)
                if dte_val:
                    exp_date = entry_dt_exp + timedelta(days=dte_val)
                    expiry_display = exp_date.strftime('%d %b')
                else:
                    expiry_display = '--'
            except Exception:
                expiry_display = '--'

        trades.append({
            'id': t.get('id', ''),
            'symbol': t.get('symbol', t.get('commodity', '')),
            'strategy': t.get('strategy', ''),
            'type': t.get('signal_type', ''),
            'strike': t.get('strike', 0),
            'expiry': expiry_display,
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
            'capital_invested': round(t_cap, 2),
            'capital_after': t.get('capital_after', 0),
        })

    # Sort by exit time and compute running capital_after if not present
    trades.sort(key=lambda x: x.get('exit_time', ''))
    default_cap = EQUITY_CAPITAL if market == 'equity' else (STOCK_CAPITAL if market == 'stocks' else (OI_CAPITAL if market == 'oi' else COMMODITY_CAPITAL))
    capital = data.get('capital', default_cap) if data else default_cap
    if trades and not trades[-1].get('capital_after'):
        total_today = sum(t['pnl'] for t in trades)
        running = capital - total_today
        for t in trades:
            running += t['pnl']
            t['capital_after'] = round(running, 2)

    # v10.3: Mark duplicate-strategy trades (same symbol+strike+entry_time, different strategy)
    seen = {}
    for t in trades:
        key = f"{t['symbol']}_{t['strike']}_{t['entry_time'][:16]}"
        if key in seen:
            t['is_grouped'] = True
            t['group_id'] = key
            seen[key]['is_grouped'] = True
            seen[key]['group_id'] = key
        else:
            t['is_grouped'] = False
            t['group_id'] = ''
            seen[key] = t

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


@app.route('/api/stocks')
def api_stocks():
    """Stock options positions + closed trades."""
    data = load_json(STOCK_STATE)
    if not data:
        return jsonify({'error': 'No stock data'}), 404
    positions = parse_positions(data, 'stocks')
    closed = parse_closed_trades(data, 'stocks')
    capital = data.get('capital', STOCK_CAPITAL)
    invested = sum(p['capital_invested'] for p in positions)
    today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'positions': positions,
        'closed_trades': closed,
        'capital': round(capital, 2),
        'invested': round(invested, 2),
        'available': round(capital - invested, 2),
        'daily_pnl': data.get('daily_pnl', {}).get(today, 0),
        'last_updated': data.get('last_updated', ''),
    })


@app.route('/api/oi')
def api_oi():
    """OI-based strategy positions + closed trades."""
    data = load_json(OI_STATE)
    if not data:
        return jsonify({'error': 'No OI data'}), 404
    positions = parse_positions(data, 'oi')
    closed = parse_closed_trades(data, 'oi')
    capital = data.get('capital', OI_CAPITAL)
    invested = sum(p['capital_invested'] for p in positions)
    today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'positions': positions,
        'closed_trades': closed,
        'capital': round(capital, 2),
        'invested': round(invested, 2),
        'available': round(capital - invested, 2),
        'daily_pnl': data.get('daily_pnl', {}).get(today, 0),
        'last_updated': data.get('last_updated', ''),
    })


@app.route('/api/stock_watchlist')
def api_stock_watchlist():
    """Today's narrow CPR watchlist from stock scanner."""
    today = datetime.now().strftime('%Y-%m-%d')
    scan_path = os.path.join(STOCK_PAPER_DIR, f'cpr_scan_{today}.json')
    data = load_json(scan_path)
    return jsonify(data or {'stocks': [], 'narrow_cpr_count': 0, 'date': today})


@app.route('/api/summary')
def api_summary():
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)
    stk_data = load_json(STOCK_STATE)
    oi_data = load_json(OI_STATE)

    # Open positions (current)
    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    stk_positions = parse_positions(stk_data, 'stocks') if stk_data else []
    oi_positions = parse_positions(oi_data, 'oi') if oi_data else []
    all_positions = eq_positions + cm_positions + stk_positions + oi_positions
    total_open_pnl = sum(p['pnl'] for p in all_positions)

    # Today's closed trades (for daily stats)
    eq_closed_today = parse_closed_trades(eq_data, 'equity') if eq_data else []
    cm_closed_today = parse_closed_trades(cm_data, 'commodity') if cm_data else []
    stk_closed_today = parse_closed_trades(stk_data, 'stocks') if stk_data else []
    oi_closed_today = parse_closed_trades(oi_data, 'oi') if oi_data else []
    all_closed_today = eq_closed_today + cm_closed_today + stk_closed_today + oi_closed_today

    # ALL-TIME closed trades (for total stats — uses _get_all_closed_trades)
    all_closed_alltime = _get_all_closed_trades('all')
    total_closed_pnl = sum(t['pnl'] for t in all_closed_alltime)
    wins = sum(1 for t in all_closed_alltime if t['pnl'] > 0)
    losses = sum(1 for t in all_closed_alltime if t['pnl'] <= 0)
    total_trades = wins + losses
    win_rate = round(wins / max(total_trades, 1) * 100, 1)

    best_trade = max(all_closed_alltime, key=lambda t: t['pnl']) if all_closed_alltime else None
    worst_trade = min(all_closed_alltime, key=lambda t: t['pnl']) if all_closed_alltime else None

    eq_capital = eq_data.get('capital', EQUITY_CAPITAL) if eq_data else EQUITY_CAPITAL
    cm_capital = cm_data.get('capital', COMMODITY_CAPITAL) if cm_data else COMMODITY_CAPITAL
    stk_capital = stk_data.get('capital', STOCK_CAPITAL) if stk_data else STOCK_CAPITAL
    oi_capital = oi_data.get('capital', OI_CAPITAL) if oi_data else OI_CAPITAL

    eq_invested = sum(p['capital_invested'] for p in eq_positions)
    cm_invested = sum(p['capital_invested'] for p in cm_positions)
    stk_invested = sum(p['capital_invested'] for p in stk_positions)
    oi_invested = sum(p['capital_invested'] for p in oi_positions)

    today = datetime.now().strftime('%Y-%m-%d')
    eq_daily = eq_data.get('daily_pnl', {}).get(today, 0) if eq_data else 0
    cm_daily = cm_data.get('daily_pnl', {}).get(today, 0) if cm_data else 0
    stk_daily = stk_data.get('daily_pnl', {}).get(today, 0) if stk_data else 0
    oi_daily = oi_data.get('daily_pnl', {}).get(today, 0) if oi_data else 0

    total_pnl = total_open_pnl + total_closed_pnl
    total_capital = eq_capital + cm_capital + stk_capital + oi_capital
    total_pnl_pct = round(total_pnl / total_capital * 100, 2) if total_capital > 0 else 0
    eq_pnl_pct = round(eq_daily / eq_capital * 100, 2) if eq_capital > 0 else 0
    cm_pnl_pct = round(cm_daily / cm_capital * 100, 2) if cm_capital > 0 else 0
    stk_pnl_pct = round(stk_daily / stk_capital * 100, 2) if stk_capital > 0 else 0
    oi_pnl_pct = round(oi_daily / oi_capital * 100, 2) if oi_capital > 0 else 0

    # v10: Advanced stats — Profit Factor, Max Drawdown, Avg Win/Loss (ALL-TIME)
    winning_pnls = [t['pnl'] for t in all_closed_alltime if t['pnl'] > 0]
    losing_pnls = [t['pnl'] for t in all_closed_alltime if t['pnl'] <= 0]
    gross_wins = sum(winning_pnls) if winning_pnls else 0
    gross_losses = abs(sum(losing_pnls)) if losing_pnls else 0
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else (999.0 if gross_wins > 0 else 0)
    avg_win = round(gross_wins / len(winning_pnls), 2) if winning_pnls else 0
    avg_loss = round(sum(losing_pnls) / len(losing_pnls), 2) if losing_pnls else 0

    # Max drawdown from daily P&L cumulative
    eq_daily_all = eq_data.get('daily_pnl', {}) if eq_data else {}
    cm_daily_all = cm_data.get('daily_pnl', {}) if cm_data else {}
    stk_daily_all = stk_data.get('daily_pnl', {}) if stk_data else {}
    oi_daily_all = oi_data.get('daily_pnl', {}) if oi_data else {}
    all_dates = set()
    all_dates.update(eq_daily_all.keys())
    all_dates.update(cm_daily_all.keys())
    all_dates.update(stk_daily_all.keys())
    all_dates.update(oi_daily_all.keys())
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for d in sorted(all_dates):
        day_pnl = eq_daily_all.get(d, 0) + cm_daily_all.get(d, 0) + stk_daily_all.get(d, 0) + oi_daily_all.get(d, 0)
        cumulative += day_pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_drawdown:
            max_drawdown = dd

    return jsonify({
        'total_pnl': round(total_pnl, 2),
        'total_pnl_pct': total_pnl_pct,
        'open_pnl': round(total_open_pnl, 2),
        'closed_pnl': round(total_closed_pnl, 2),
        'equity_daily_pnl': round(eq_daily, 2),
        'equity_daily_pnl_pct': eq_pnl_pct,
        'commodity_daily_pnl': round(cm_daily, 2),
        'commodity_daily_pnl_pct': cm_pnl_pct,
        'stock_daily_pnl': round(stk_daily, 2),
        'stock_daily_pnl_pct': stk_pnl_pct,
        'oi_daily_pnl': round(oi_daily, 2),
        'oi_daily_pnl_pct': oi_pnl_pct,
        'open_positions': len(all_positions),
        'closed_today': len(all_closed_today),
        'total_trades': total_trades,
        'win_rate': win_rate,
        'wins': wins,
        'losses': losses,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'equity_capital': round(eq_capital, 2),
        'commodity_capital': round(cm_capital, 2),
        'stock_capital': round(stk_capital, 2),
        'oi_capital': round(oi_capital, 2),
        'equity_available': round(eq_capital - eq_invested, 2),
        'commodity_available': round(cm_capital - cm_invested, 2),
        'stock_available': round(stk_capital - stk_invested, 2),
        'oi_available': round(oi_capital - oi_invested, 2),
        # v10: Advanced stats
        'profit_factor': profit_factor,
        'max_drawdown': round(max_drawdown, 2),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'gross_wins': round(gross_wins, 2),
        'gross_losses': round(gross_losses, 2),
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
    if market in ('all', 'stocks'):
        stk_data = load_json(STOCK_STATE)
        if stk_data:
            for t in stk_data.get('closed_trades', []):
                details = t.get('details', {}) if isinstance(t.get('details'), dict) else {}
                trades.append({
                    'timestamp': t.get('timestamp', ''),
                    'exit_time': t.get('exit_time', ''),
                    'symbol': t.get('stock_symbol', t.get('symbol', '')),
                    'strategy': t.get('strategy', ''),
                    'type': t.get('signal_type', ''),
                    'strike': t.get('strike', 0),
                    'entry_price': t.get('entry_premium', 0),
                    'exit_price': t.get('exit_premium', 0),
                    'gross_pnl': t.get('raw_pnl', t.get('pnl', 0)),
                    'pnl': t.get('pnl', 0),
                    'total_slippage': t.get('costs', 0),
                    'exit_reason': t.get('exit_reason', ''),
                    'num_lots': 1,
                    'lot_size': t.get('lot_size', 0),
                    'capital_after': t.get('capital_after', 0),
                    'market': 'stocks',
                })
    if market in ('all', 'oi'):
        oi_data = load_json(OI_STATE)
        if oi_data:
            for t in oi_data.get('closed_trades', []):
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
                    'market': 'oi',
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

    stk_data = load_json(STOCK_STATE)
    oi_data_ep = load_json(OI_STATE)
    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    stk_positions = parse_positions(stk_data, 'stocks') if stk_data else []
    oi_positions_ep = parse_positions(oi_data_ep, 'oi') if oi_data_ep else []
    all_positions = eq_positions + cm_positions + stk_positions + oi_positions_ep

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

    stk_data_exp = load_json(STOCK_STATE)
    oi_data_exp = load_json(OI_STATE)
    eq_positions = parse_positions(eq_data, 'equity') if eq_data else []
    cm_positions = parse_positions(cm_data, 'commodity') if cm_data else []
    stk_positions_exp = parse_positions(stk_data_exp, 'stocks') if stk_data_exp else []
    oi_positions_exp = parse_positions(oi_data_exp, 'oi') if oi_data_exp else []
    eq_closed = parse_closed_trades(eq_data, 'equity') if eq_data else []
    cm_closed = parse_closed_trades(cm_data, 'commodity') if cm_data else []
    stk_closed_exp = parse_closed_trades(stk_data_exp, 'stocks') if stk_data_exp else []
    oi_closed_exp = parse_closed_trades(oi_data_exp, 'oi') if oi_data_exp else []

    all_closed = eq_closed + cm_closed + stk_closed_exp + oi_closed_exp
    all_positions = eq_positions + cm_positions + stk_positions_exp + oi_positions_exp

    total_open_pnl = sum(p['pnl'] for p in all_positions)
    total_closed_pnl = sum(t['pnl'] for t in all_closed)
    wins = sum(1 for t in all_closed if t['pnl'] > 0)
    losses = sum(1 for t in all_closed if t['pnl'] <= 0)

    eq_capital = eq_data.get('capital', EQUITY_CAPITAL) if eq_data else EQUITY_CAPITAL
    cm_capital = cm_data.get('capital', COMMODITY_CAPITAL) if cm_data else COMMODITY_CAPITAL
    stk_capital_exp = stk_data_exp.get('capital', STOCK_CAPITAL) if stk_data_exp else STOCK_CAPITAL
    oi_capital_exp = oi_data_exp.get('capital', OI_CAPITAL) if oi_data_exp else OI_CAPITAL

    today = datetime.now().strftime('%Y-%m-%d')
    summary = {
        'date': today,
        'timestamp': datetime.now().isoformat(),
        'equity_capital': round(eq_capital, 2),
        'commodity_capital': round(cm_capital, 2),
        'stock_capital': round(stk_capital_exp, 2),
        'oi_capital': round(oi_capital_exp, 2),
        'total_capital': round(eq_capital + cm_capital + stk_capital_exp + oi_capital_exp, 2),
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


# ====================================================================
# MANUAL CLOSE ENDPOINT — Close positions from dashboard
# ====================================================================
@app.route('/api/close', methods=['POST'])
def manual_close_position():
    """Close an open position manually from the dashboard."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    pos_id = data.get('id', '')
    market = data.get('market', 'equity')

    if not pos_id:
        return jsonify({'error': 'Missing position id'}), 400

    if market == 'equity':
        state_file = EQUITY_STATE
    elif market == 'stocks':
        state_file = STOCK_STATE
    elif market == 'oi':
        state_file = OI_STATE
    else:
        state_file = COMMODITY_STATE

    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Cannot read state: {e}'}), 500

    # Find position
    pos_idx = None
    for i, p in enumerate(state.get('positions', [])):
        if p.get('id') == pos_id:
            pos_idx = i
            break

    if pos_idx is None:
        return jsonify({'error': f'Position {pos_id} not found'}), 404

    pos = state['positions'].pop(pos_idx)

    # Calculate PnL using last known premium
    exit_premium = pos.get('current_premium', pos.get('entry_premium', 0))
    entry_premium = pos.get('entry_premium', 0)
    lot_size = pos.get('lot_size', 1)
    multiplier = pos.get('multiplier', 1)
    is_sell = pos.get('is_sell', False)

    if is_sell:
        pnl = (entry_premium - exit_premium) * lot_size * multiplier
    else:
        pnl = (exit_premium - entry_premium) * lot_size * multiplier

    # Subtract approximate costs (entry + exit)
    entry_cost = pos.get('entry_cost', 0)
    exit_cost = entry_cost  # approximate
    pnl -= (entry_cost + exit_cost)

    # Create closed trade record
    trade = {**pos}
    trade['exit_premium'] = round(exit_premium, 2)
    trade['exit_cost'] = round(exit_cost, 2)
    trade['pnl'] = round(pnl, 2)
    trade['exit_reason'] = 'MANUAL_CLOSE'
    trade['exit_time'] = datetime.now().isoformat()
    trade['status'] = 'CLOSED'

    state.setdefault('closed_trades', []).append(trade)

    # Update capital
    state['capital'] = round(state.get('capital', 0) + pnl, 2)
    trade['capital_after'] = state['capital']

    # Update daily PnL
    today = datetime.now().strftime('%Y-%m-%d')
    daily = state.get('daily_pnl', {})
    daily[today] = round(daily.get(today, 0) + pnl, 2)
    state['daily_pnl'] = daily

    # Write back
    try:
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        return jsonify({'error': f'Write failed: {e}'}), 500

    symbol = pos.get('symbol', pos.get('commodity', ''))
    return jsonify({
        'status': 'ok',
        'message': f'Closed {symbol} {pos.get("signal_type", "")}',
        'pnl': round(pnl, 2),
        'capital': state['capital'],
    })


@app.route('/api/close_all', methods=['POST'])
def manual_close_all():
    """Close ALL open positions manually."""
    results = []
    for market, state_file in [('equity', EQUITY_STATE), ('commodity', COMMODITY_STATE), ('stocks', STOCK_STATE), ('oi', OI_STATE)]:
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
        except Exception:
            continue

        positions = list(state.get('positions', []))
        if not positions:
            continue

        today = datetime.now().strftime('%Y-%m-%d')
        daily = state.get('daily_pnl', {})

        for pos in positions:
            exit_premium = pos.get('current_premium', pos.get('entry_premium', 0))
            entry_premium = pos.get('entry_premium', 0)
            lot_size = pos.get('lot_size', 1)
            multiplier = pos.get('multiplier', 1)

            if pos.get('is_sell', False):
                pnl = (entry_premium - exit_premium) * lot_size * multiplier
            else:
                pnl = (exit_premium - entry_premium) * lot_size * multiplier

            entry_cost = pos.get('entry_cost', 0)
            pnl -= (entry_cost * 2)

            trade = {**pos}
            trade['exit_premium'] = round(exit_premium, 2)
            trade['pnl'] = round(pnl, 2)
            trade['exit_reason'] = 'MANUAL_CLOSE'
            trade['exit_time'] = datetime.now().isoformat()
            trade['status'] = 'CLOSED'

            state.setdefault('closed_trades', []).append(trade)
            state['capital'] = round(state.get('capital', 0) + pnl, 2)
            trade['capital_after'] = state['capital']
            daily[today] = round(daily.get(today, 0) + pnl, 2)

            symbol = pos.get('symbol', pos.get('commodity', ''))
            results.append({'symbol': symbol, 'pnl': round(pnl, 2)})

        state['positions'] = []
        state['daily_pnl'] = daily

        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)

    return jsonify({
        'status': 'ok',
        'closed': len(results),
        'trades': results,
    })


# ====================================================================
# DATA FRESHNESS — Check when state files were last modified
# ====================================================================
@app.route('/api/freshness')
def data_freshness():
    """Return last-modified timestamps of state files."""
    result = {}
    for name, path in [('equity', EQUITY_STATE), ('commodity', COMMODITY_STATE), ('stocks', STOCK_STATE), ('oi', OI_STATE)]:
        try:
            mtime = os.path.getmtime(path)
            result[name] = datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            result[name] = None
    return jsonify(result)


# ====================================================================
# ENHANCED DASHBOARD — New API Endpoints
# ====================================================================

@app.route('/api/market_status')
def api_market_status():
    """Market status: OPEN/CLOSED/PRE_MARKET/HOLIDAY with countdown."""
    now = datetime.now()
    today = now.date()
    weekday = today.weekday()  # 0=Mon, 6=Sun

    # Check holiday
    holiday_name = None
    try:
        if is_nse_holiday(today):
            from market_holidays import NSE_HOLIDAYS
            holiday_name = NSE_HOLIDAYS.get(today, 'Holiday')
    except Exception:
        pass

    if weekday >= 5:  # Saturday/Sunday
        status = 'CLOSED'
        status_detail = 'Weekend'
    elif holiday_name:
        status = 'HOLIDAY'
        status_detail = holiday_name
    else:
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        pre_market = now.replace(hour=9, minute=0, second=0, microsecond=0)

        if now < pre_market:
            status = 'CLOSED'
            status_detail = 'Pre-market starts at 9:00'
        elif now < market_open:
            status = 'PRE_MARKET'
            status_detail = 'Market opens at 9:15'
        elif now <= market_close:
            status = 'OPEN'
            status_detail = 'Closes at 15:30'
        else:
            status = 'CLOSED'
            status_detail = 'After hours'

    # Countdown (seconds to next event)
    if status == 'OPEN':
        countdown = int((now.replace(hour=15, minute=30, second=0) - now).total_seconds())
        countdown_label = 'to close'
    elif status == 'PRE_MARKET':
        countdown = int((now.replace(hour=9, minute=15, second=0) - now).total_seconds())
        countdown_label = 'to open'
    elif weekday < 5 and not holiday_name and now.hour < 9:
        countdown = int((now.replace(hour=9, minute=15, second=0) - now).total_seconds())
        countdown_label = 'to open'
    else:
        countdown = 0
        countdown_label = ''

    # Total capital
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)
    stk_data = load_json(STOCK_STATE)
    oi_data_ms = load_json(OI_STATE)
    total_capital = (
        (eq_data.get('capital', EQUITY_CAPITAL) if eq_data else EQUITY_CAPITAL) +
        (cm_data.get('capital', COMMODITY_CAPITAL) if cm_data else COMMODITY_CAPITAL) +
        (stk_data.get('capital', STOCK_CAPITAL) if stk_data else STOCK_CAPITAL) +
        (oi_data_ms.get('capital', OI_CAPITAL) if oi_data_ms else OI_CAPITAL)
    )

    return jsonify({
        'status': status,
        'detail': status_detail,
        'countdown': countdown,
        'countdown_label': countdown_label,
        'total_capital': round(total_capital, 2),
        'date': today.isoformat(),
        'day': today.strftime('%A'),
    })


@app.route('/api/strategy_performance')
def api_strategy_performance():
    """Per-strategy performance breakdown from all closed trades."""
    trades = _get_all_closed_trades('all')
    today = datetime.now().strftime('%Y-%m-%d')

    # Also get open positions per strategy
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)
    stk_data = load_json(STOCK_STATE)
    oi_data_sp = load_json(OI_STATE)
    all_positions = []
    if eq_data:
        all_positions += parse_positions(eq_data, 'equity')
    if cm_data:
        all_positions += parse_positions(cm_data, 'commodity')
    if stk_data:
        all_positions += parse_positions(stk_data, 'stocks')
    if oi_data_sp:
        all_positions += parse_positions(oi_data_sp, 'oi')

    # Group trades by strategy
    strat_map = {}
    for t in trades:
        s = t.get('strategy', 'Unknown')
        if s not in strat_map:
            strat_map[s] = {'trades': [], 'today_trades': []}
        strat_map[s]['trades'].append(t)
        exit_time = t.get('exit_time', t.get('timestamp', ''))
        if today in exit_time:
            strat_map[s]['today_trades'].append(t)

    # Count open positions per strategy
    open_by_strat = {}
    for p in all_positions:
        s = p.get('strategy', 'Unknown')
        open_by_strat[s] = open_by_strat.get(s, 0) + 1

    results = []
    for strat_name, data in strat_map.items():
        all_t = data['trades']
        today_t = data['today_trades']
        wins = sum(1 for t in all_t if t['pnl'] > 0)
        losses = sum(1 for t in all_t if t['pnl'] <= 0)
        total_pnl = sum(t['pnl'] for t in all_t)
        today_pnl = sum(t['pnl'] for t in today_t)
        pnls = [t['pnl'] for t in all_t]

        results.append({
            'strategy': strat_name,
            'trades': len(all_t),
            'wins': wins,
            'losses': losses,
            'win_rate': round(wins / max(wins + losses, 1) * 100, 1),
            'total_pnl': round(total_pnl, 2),
            'today_pnl': round(today_pnl, 2),
            'today_trades': len(today_t),
            'avg_pnl': round(total_pnl / max(len(all_t), 1), 2),
            'best': round(max(pnls), 2) if pnls else 0,
            'worst': round(min(pnls), 2) if pnls else 0,
            'open_positions': open_by_strat.get(strat_name, 0),
        })

    # Sort by total trades descending
    results.sort(key=lambda x: x['trades'], reverse=True)
    return jsonify({'strategies': results})


@app.route('/api/signals_today')
def api_signals_today():
    """Today's generated signals from signal CSV logs."""
    today_str = datetime.now().strftime('%Y%m%d')
    signals = []

    # Equity signals
    eq_path = os.path.join(BASE_DIR, 'paper_trades', f'signals_{today_str}.csv')
    if os.path.exists(eq_path):
        try:
            with open(eq_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    signals.append({
                        'timestamp': row.get('timestamp', ''),
                        'strategy': row.get('strategy', ''),
                        'symbol': row.get('symbol', ''),
                        'type': row.get('type', ''),
                        'strike': float(row.get('strike', 0) or 0),
                        'premium': round(float(row.get('premium', 0) or 0), 2),
                        'score': row.get('score', row.get('quality_score', '')),
                        'reason': row.get('reason', ''),
                        'market': 'equity',
                    })
        except Exception:
            pass

    # Commodity signals
    cm_path = os.path.join(BASE_DIR, 'paper_trades_commodity', f'commodity_signals_{today_str}.csv')
    if os.path.exists(cm_path):
        try:
            with open(cm_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    signals.append({
                        'timestamp': row.get('timestamp', ''),
                        'strategy': row.get('strategy', ''),
                        'symbol': row.get('commodity', row.get('symbol', '')),
                        'type': row.get('type', ''),
                        'strike': float(row.get('strike', 0) or 0),
                        'premium': round(float(row.get('premium', 0) or 0), 2),
                        'score': row.get('score', ''),
                        'reason': row.get('reason', ''),
                        'market': 'commodity',
                    })
        except Exception:
            pass

    # Stock signals (from stock_paper_trades dir if exists)
    stk_path = os.path.join(STOCK_PAPER_DIR, f'stock_signals_{today_str}.csv')
    if os.path.exists(stk_path):
        try:
            with open(stk_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    signals.append({
                        'timestamp': row.get('timestamp', ''),
                        'strategy': row.get('strategy', ''),
                        'symbol': row.get('symbol', ''),
                        'type': row.get('type', ''),
                        'strike': float(row.get('strike', 0) or 0),
                        'premium': round(float(row.get('premium', 0) or 0), 2),
                        'score': row.get('score', row.get('quality_score', '')),
                        'reason': row.get('reason', ''),
                        'market': 'stocks',
                    })
        except Exception:
            pass

    # OI signals (from paper_trades_oi dir)
    oi_path = os.path.join(OI_PAPER_DIR, f'oi_signals_{today_str}.csv')
    if os.path.exists(oi_path):
        try:
            with open(oi_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    signals.append({
                        'timestamp': row.get('timestamp', ''),
                        'strategy': row.get('strategy', ''),
                        'symbol': row.get('symbol', ''),
                        'type': row.get('signal_type', row.get('type', '')),
                        'strike': float(row.get('strike', 0) or 0),
                        'premium': round(float(row.get('entry_premium', row.get('premium', 0)) or 0), 2),
                        'score': row.get('quality_score', row.get('score', '')),
                        'reason': row.get('reason', ''),
                        'market': 'oi',
                    })
        except Exception:
            pass

    # Sort by timestamp descending, return last 30
    signals.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify({'signals': signals[:30], 'total': len(signals)})


@app.route('/api/physics_gate')
def api_physics_gate():
    """Parse PHYSICS_BLOCK and PHYSICS_PASS entries from today's log files."""
    today_str = datetime.now().strftime('%Y%m%d')
    gates = []

    # Log file paths for all 3 bots
    log_files = [
        (os.path.join(BASE_DIR, 'logs', f'unified_paper_{today_str}.log'), 'equity'),
        (os.path.join(BASE_DIR, 'logs', f'stock_paper_{today_str}.log'), 'stocks'),
        (os.path.join(BASE_DIR, 'logs', f'commodity_paper_{today_str}.log'), 'commodity'),
    ]

    # Regex patterns for PHYSICS_BLOCK and PHYSICS_PASS
    block_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\S*\s+.*?'
        r'PHYSICS_BLOCK:\s+(\S+)\s+(\S+)\s+'
        r'score=([-\d]+)\s+\[([^\]]*)\]\s+'
        r'accel=([\d.?\-]+)\s+wave=([\d.?\-]+)\s+'
        r'vwap=([\d.?\-]+)\s+rsi=([\d.?\-]+)'
    )
    pass_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\S*\s+.*?'
        r'PHYSICS_PASS:\s+(\S+)\s+(\S+)\s+'
        r'score=([-\d]+)\s+accel=([\d.?\-]+)\s+wave=([\d.?\-]+)'
    )

    for log_path, market in log_files:
        if not os.path.exists(log_path):
            continue
        try:
            with open(log_path, 'r', errors='ignore') as f:
                for line in f:
                    if 'PHYSICS_BLOCK' in line:
                        m = block_pattern.search(line)
                        if m:
                            warnings_str = m.group(5)
                            gates.append({
                                'timestamp': m.group(1),
                                'symbol': m.group(2),
                                'type': m.group(3),
                                'action': 'BLOCK',
                                'score': int(m.group(4)),
                                'warnings': warnings_str.split(',') if warnings_str else [],
                                'accel': m.group(6),
                                'wave': m.group(7),
                                'vwap': m.group(8),
                                'rsi': m.group(9),
                                'market': market,
                            })
                    elif 'PHYSICS_PASS' in line:
                        m = pass_pattern.search(line)
                        if m:
                            gates.append({
                                'timestamp': m.group(1),
                                'symbol': m.group(2),
                                'type': m.group(3),
                                'action': 'PASS',
                                'score': int(m.group(4)),
                                'warnings': [],
                                'accel': m.group(5),
                                'wave': m.group(6),
                                'vwap': '',
                                'rsi': '',
                                'market': market,
                            })
        except Exception:
            pass

    # Sort by timestamp descending
    gates.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    # Compute summary stats
    blocked = sum(1 for g in gates if g['action'] == 'BLOCK')
    passed = sum(1 for g in gates if g['action'] == 'PASS')

    return jsonify({
        'gates': gates[:50],
        'total': len(gates),
        'blocked': blocked,
        'passed': passed,
    })


@app.route('/api/daily_pnl_history')
def api_daily_pnl_history():
    """Daily PnL history from all state files, last 30 days."""
    eq_data = load_json(EQUITY_STATE)
    cm_data = load_json(COMMODITY_STATE)
    stk_data = load_json(STOCK_STATE)
    oi_data_hist = load_json(OI_STATE)

    eq_daily = eq_data.get('daily_pnl', {}) if eq_data else {}
    cm_daily = cm_data.get('daily_pnl', {}) if cm_data else {}
    stk_daily = stk_data.get('daily_pnl', {}) if stk_data else {}
    oi_daily_h = oi_data_hist.get('daily_pnl', {}) if oi_data_hist else {}

    # Merge all dates
    all_dates = set()
    all_dates.update(eq_daily.keys())
    all_dates.update(cm_daily.keys())
    all_dates.update(stk_daily.keys())
    all_dates.update(oi_daily_h.keys())

    history = []
    for d in sorted(all_dates):
        eq_pnl = eq_daily.get(d, 0)
        cm_pnl = cm_daily.get(d, 0)
        stk_pnl = stk_daily.get(d, 0)
        oi_pnl = oi_daily_h.get(d, 0)
        total = eq_pnl + cm_pnl + stk_pnl + oi_pnl
        history.append({
            'date': d,
            'total': round(total, 2),
            'equity': round(eq_pnl, 2),
            'commodity': round(cm_pnl, 2),
            'stocks': round(stk_pnl, 2),
            'oi': round(oi_pnl, 2),
        })

    # Last 30 days
    return jsonify({'history': history[-30:]})


@app.route('/api/live_indicators')
def api_live_indicators():
    """v10: Live indicator data from all 3 bots' scan cycles.

    Each bot writes live_scan_data.json every scan cycle with
    OI, PCR, IV, CPR levels, VWAP, support/resistance etc.
    Dashboard uses this to show live indicators next to positions.
    """
    eq_file = os.path.join(BASE_DIR, 'paper_trades', 'live_scan_data.json')
    cm_file = os.path.join(BASE_DIR, 'paper_trades_commodity', 'live_scan_data.json')
    stk_file = os.path.join(BASE_DIR, 'stock_paper_trades', 'live_scan_data.json')

    result = {
        'equity': load_json(eq_file),
        'commodity': load_json(cm_file),
        'stocks': load_json(stk_file),
    }

    # Merge all symbols into a flat lookup for easy frontend access
    all_symbols = {}
    vix = None
    last_updated = None

    for market_key in ['equity', 'commodity', 'stocks']:
        data = result.get(market_key)
        if data and isinstance(data, dict):
            symbols = data.get('symbols', {})
            for sym, ind in symbols.items():
                all_symbols[sym] = ind
                all_symbols[sym]['_market'] = market_key
            if data.get('vix'):
                vix = data['vix']
            ts = data.get('last_updated')
            if ts and (not last_updated or ts > last_updated):
                last_updated = ts

    return jsonify({
        'symbols': all_symbols,
        'vix': vix,
        'last_updated': last_updated,
        'markets': result,
    })


# ====================================================================
# CHART DATA + RECOMMENDATIONS ENGINE
# ====================================================================

# Symbol mapping: dashboard symbol -> TrueData CSV name / live_data_logger name
TRUEDATA_SYMBOL_MAP = {
    'NIFTY': 'NIFTY_50',
    'BANKNIFTY': 'NIFTY_BANK',
    'SENSEX': 'SENSEX',
}

# In-memory price history accumulator (filled from live_scan_data on each request)
_price_history = {}  # symbol -> deque of {time, price}
_indicator_history = {}  # symbol -> deque of {time, pcr, vwap, ...}
from collections import deque

PRICE_HISTORY_MAX = 400  # ~6.5 hours of 1-min data


def _load_intraday_candles(symbol, date_str=None):
    """Load 1-min intraday candles from best available source.

    Priority:
    1. live_data_logger spot_1min CSVs (VPS runtime)
    2. TrueData 1-min CSVs (local/backtest)
    3. In-memory accumulated prices
    """
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')

    candles = []

    # Source 1: live_data_logger spot ticks -> convert to 1-min candles
    live_csv = os.path.join(BASE_DIR, 'data', 'live', date_str, f'spot_1min_{symbol}.csv')
    if os.path.exists(live_csv):
        try:
            with open(live_csv, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get('timestamp', '')
                    ltp = float(row.get('ltp', 0))
                    if ts and ltp > 0:
                        candles.append({'time': ts, 'open': ltp, 'high': ltp, 'low': ltp, 'close': ltp})
        except Exception:
            pass

    # Source 2: TrueData 1-min CSVs (OHLC candles)
    if not candles:
        td_sym = TRUEDATA_SYMBOL_MAP.get(symbol, symbol)
        td_paths = [
            os.path.join(BASE_DIR, '..', 'data', 'truedata', f'{td_sym}_1min.csv'),
            os.path.join(BASE_DIR, '..', 'data', 'truedata', f'{td_sym}_1min_extended.csv'),
        ]
        for td_path in td_paths:
            if os.path.exists(td_path):
                try:
                    with open(td_path, 'r') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            ts = row.get('time', '')
                            if date_str not in ts:
                                continue
                            candles.append({
                                'time': ts,
                                'open': float(row.get('o', 0)),
                                'high': float(row.get('h', 0)),
                                'low': float(row.get('l', 0)),
                                'close': float(row.get('c', 0)),
                            })
                    if candles:
                        break
                except Exception:
                    pass

    # Source 3: In-memory accumulated prices (fallback — builds pseudo candles)
    if not candles and symbol in _price_history:
        for entry in _price_history[symbol]:
            p = entry['price']
            candles.append({
                'time': entry['time'],
                'open': p, 'high': p, 'low': p, 'close': p,
            })

    # Sort by time ascending
    candles.sort(key=lambda c: c['time'])

    # Convert time strings to Unix timestamps for Lightweight Charts
    for c in candles:
        try:
            dt = datetime.strptime(c['time'][:16], '%Y-%m-%d %H:%M')
            c['time'] = int(dt.timestamp())
        except Exception:
            pass

    # Deduplicate by timestamp
    seen = set()
    deduped = []
    for c in candles:
        if c['time'] not in seen:
            seen.add(c['time'])
            deduped.append(c)
    return deduped


def _load_market_indicators_history(symbol, date_str=None):
    """Load PCR/spot history from live_data_logger market_indicators CSV."""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')

    history = []
    ind_csv = os.path.join(BASE_DIR, 'data', 'live', date_str, f'market_indicators.csv')
    if os.path.exists(ind_csv):
        try:
            with open(ind_csv, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('symbol', '') != symbol:
                        continue
                    ts = row.get('timestamp', '')
                    try:
                        dt = datetime.strptime(ts[:16], '%Y-%m-%d %H:%M')
                        unix_ts = int(dt.timestamp())
                    except Exception:
                        continue
                    history.append({
                        'time': unix_ts,
                        'pcr': float(row.get('pcr', 0) or 0),
                        'spot': float(row.get('spot', 0) or 0),
                    })
        except Exception:
            pass
    return history


def _compute_vwap_line(candles):
    """Compute intraday VWAP from candle data (using typical price)."""
    vwap_data = []
    cum_tp_vol = 0
    cum_vol = 0
    for c in candles:
        tp = (c['high'] + c['low'] + c['close']) / 3
        # Use 1 as pseudo-volume since we may not have real volume
        vol = 1
        cum_tp_vol += tp * vol
        cum_vol += vol
        vwap_val = cum_tp_vol / cum_vol if cum_vol > 0 else tp
        vwap_data.append({'time': c['time'], 'value': round(vwap_val, 2)})
    return vwap_data


def _generate_recommendations(symbol, indicators, candles, cpr, positions):
    """Generate trading recommendations based on indicator confluence."""
    recs = []
    if not indicators:
        return recs

    spot = indicators.get('spot', 0)
    pcr = indicators.get('pcr', 0)
    pcr_direction = indicators.get('pcr_shift', indicators.get('pcr_direction', ''))
    oi_sentiment = indicators.get('oi_sentiment', '')
    vwap = indicators.get('vwap', 0)
    iv = indicators.get('iv', 0)
    hv = indicators.get('hv', 0)
    atr = indicators.get('atr', 0)
    pivot = indicators.get('pivot', 0)
    tc = indicators.get('tc', 0)
    bc = indicators.get('bc', 0)
    cpr_width = indicators.get('cpr_width', 0)
    resistance = indicators.get('resistance', 0)
    support = indicators.get('support', 0)
    demand_zone = indicators.get('demand_zone', {})
    if not isinstance(demand_zone, dict):
        demand_zone = {}
    supply_zone = indicators.get('supply_zone', {})
    if not isinstance(supply_zone, dict):
        supply_zone = {}
    cam_r3 = indicators.get('cam_r3', 0)
    cam_r4 = indicators.get('cam_r4', 0)
    cam_s3 = indicators.get('cam_s3', 0)
    cam_s4 = indicators.get('cam_s4', 0)

    if not spot or not pivot:
        return recs

    # --- 1. CPR Position Analysis ---
    if spot > tc > 0:
        recs.append({
            'type': 'bullish',
            'category': 'CPR',
            'title': 'Price Above TC — Bullish Breakout Zone',
            'detail': f'Spot {spot:.0f} is above Top CPR {tc:.0f}. Favors BUY CE on pullback to TC.',
            'strength': 'strong',
        })
    elif spot < bc > 0:
        recs.append({
            'type': 'bearish',
            'category': 'CPR',
            'title': 'Price Below BC — Bearish Breakdown Zone',
            'detail': f'Spot {spot:.0f} is below Bottom CPR {bc:.0f}. Favors BUY PE on pullback to BC.',
            'strength': 'strong',
        })
    elif tc > 0 and bc > 0:
        recs.append({
            'type': 'neutral',
            'category': 'CPR',
            'title': 'Price Inside CPR — Range-Bound',
            'detail': f'Spot {spot:.0f} is between TC {tc:.0f} and BC {bc:.0f}. Wait for breakout.',
            'strength': 'moderate',
        })

    # Narrow CPR = high breakout probability
    if 0 < cpr_width < 0.3:
        recs.append({
            'type': 'alert',
            'category': 'CPR',
            'title': f'Narrow CPR ({cpr_width:.3f}%) — Breakout Expected',
            'detail': 'CPR width < 0.3% signals a trending day. Prepare for directional move.',
            'strength': 'strong',
        })

    # --- 2. PCR Analysis ---
    if pcr > 0:
        if pcr > 1.3:
            recs.append({
                'type': 'bullish',
                'category': 'PCR',
                'title': f'High PCR ({pcr:.2f}) — Bullish Sentiment',
                'detail': 'Heavy put writing indicates strong support. Favors calls.',
                'strength': 'strong',
            })
        elif pcr > 1.0:
            recs.append({
                'type': 'bullish',
                'category': 'PCR',
                'title': f'PCR ({pcr:.2f}) Above 1 — Mildly Bullish',
                'detail': 'More puts than calls being written. Market has support.',
                'strength': 'moderate',
            })
        elif pcr < 0.7:
            recs.append({
                'type': 'bearish',
                'category': 'PCR',
                'title': f'Low PCR ({pcr:.2f}) — Bearish Sentiment',
                'detail': 'Heavy call writing indicates resistance overhead. Favors puts.',
                'strength': 'strong',
            })
        elif pcr < 1.0:
            recs.append({
                'type': 'bearish',
                'category': 'PCR',
                'title': f'PCR ({pcr:.2f}) Below 1 — Mildly Bearish',
                'detail': 'More calls than puts being written. Some resistance.',
                'strength': 'moderate',
            })

    # --- 3. VWAP Position ---
    if vwap > 0 and spot > 0:
        pct_from_vwap = (spot - vwap) / vwap * 100
        if pct_from_vwap > 0.15:
            recs.append({
                'type': 'bullish',
                'category': 'VWAP',
                'title': f'Price Above VWAP (+{pct_from_vwap:.2f}%)',
                'detail': f'Spot {spot:.0f} is above VWAP {vwap:.0f}. Intraday buyers in control.',
                'strength': 'moderate',
            })
        elif pct_from_vwap < -0.15:
            recs.append({
                'type': 'bearish',
                'category': 'VWAP',
                'title': f'Price Below VWAP ({pct_from_vwap:.2f}%)',
                'detail': f'Spot {spot:.0f} is below VWAP {vwap:.0f}. Intraday sellers in control.',
                'strength': 'moderate',
            })

    # --- 4. IV vs HV Analysis ---
    if iv > 0 and hv > 0:
        iv_pct = iv * 100 if iv < 1 else iv
        hv_pct = hv * 100 if hv < 1 else hv
        iv_premium = iv_pct - hv_pct
        if iv_premium > 5:
            recs.append({
                'type': 'alert',
                'category': 'Volatility',
                'title': f'IV Premium ({iv_premium:.1f}pts) — Options Expensive',
                'detail': f'IV {iv_pct:.1f}% > HV {hv_pct:.1f}%. Consider selling strategies or tighter targets.',
                'strength': 'moderate',
            })
        elif iv_premium < -3:
            recs.append({
                'type': 'alert',
                'category': 'Volatility',
                'title': f'IV Discount ({iv_premium:.1f}pts) — Options Cheap',
                'detail': f'IV {iv_pct:.1f}% < HV {hv_pct:.1f}%. Options are relatively cheap. Favor buying.',
                'strength': 'moderate',
            })

    # --- 5. Ghost Zone Proximity ---
    if demand_zone and spot > 0:
        dz_high = demand_zone.get('high', 0)
        dz_low = demand_zone.get('low', 0)
        if dz_high and dz_low:
            dist_to_dz = ((spot - dz_high) / spot * 100) if dz_high else 999
            if 0 < dist_to_dz < 0.5:
                recs.append({
                    'type': 'bullish',
                    'category': 'Ghost Zone',
                    'title': f'Near Demand Zone ({dz_low:.0f}-{dz_high:.0f})',
                    'detail': f'Spot is {dist_to_dz:.2f}% above demand zone. Institutional buying support nearby.',
                    'strength': 'strong',
                })

    if supply_zone and spot > 0:
        sz_low = supply_zone.get('low', 0)
        sz_high = supply_zone.get('high', 0)
        if sz_low and sz_high:
            dist_to_sz = ((sz_low - spot) / spot * 100) if sz_low else 999
            if 0 < dist_to_sz < 0.5:
                recs.append({
                    'type': 'bearish',
                    'category': 'Ghost Zone',
                    'title': f'Near Supply Zone ({sz_low:.0f}-{sz_high:.0f})',
                    'detail': f'Spot is {dist_to_sz:.2f}% below supply zone. Institutional selling pressure nearby.',
                    'strength': 'strong',
                })

    # --- 6. Camarilla Level Signals ---
    if cam_s3 and cam_r3 and spot:
        if spot <= cam_s3:
            recs.append({
                'type': 'bullish',
                'category': 'Camarilla',
                'title': f'At Cam S3 ({cam_s3:.0f}) — Reversal Buy Zone',
                'detail': 'Price at Camarilla S3 support. Potential bounce with SL below S4.',
                'strength': 'moderate',
            })
        elif spot >= cam_r3:
            recs.append({
                'type': 'bearish',
                'category': 'Camarilla',
                'title': f'At Cam R3 ({cam_r3:.0f}) — Reversal Sell Zone',
                'detail': 'Price at Camarilla R3 resistance. Potential rejection with SL above R4.',
                'strength': 'moderate',
            })

    # --- 7. Confluence Score ---
    bullish_count = sum(1 for r in recs if r['type'] == 'bullish')
    bearish_count = sum(1 for r in recs if r['type'] == 'bearish')
    strong_bull = sum(1 for r in recs if r['type'] == 'bullish' and r['strength'] == 'strong')
    strong_bear = sum(1 for r in recs if r['type'] == 'bearish' and r['strength'] == 'strong')

    if bullish_count >= 3 or strong_bull >= 2:
        confluence = 'STRONG BULLISH'
        conf_type = 'bullish'
        conf_detail = f'{bullish_count} bullish signals ({strong_bull} strong). High confluence for BUY CE.'
    elif bearish_count >= 3 or strong_bear >= 2:
        confluence = 'STRONG BEARISH'
        conf_type = 'bearish'
        conf_detail = f'{bearish_count} bearish signals ({strong_bear} strong). High confluence for BUY PE.'
    elif bullish_count > bearish_count:
        confluence = 'LEAN BULLISH'
        conf_type = 'bullish'
        conf_detail = f'{bullish_count} bullish vs {bearish_count} bearish. Mild bullish bias.'
    elif bearish_count > bullish_count:
        confluence = 'LEAN BEARISH'
        conf_type = 'bearish'
        conf_detail = f'{bearish_count} bearish vs {bullish_count} bullish. Mild bearish bias.'
    else:
        confluence = 'NEUTRAL'
        conf_type = 'neutral'
        conf_detail = 'No clear directional bias. Wait for confirmation.'

    # Insert confluence as first recommendation
    recs.insert(0, {
        'type': conf_type,
        'category': 'Confluence',
        'title': confluence,
        'detail': conf_detail,
        'strength': 'strong' if 'STRONG' in confluence else 'moderate',
    })

    return recs


def _accumulate_price(symbol, spot):
    """Accumulate spot price into in-memory history for chart fallback."""
    if not spot or spot <= 0:
        return
    now = datetime.now()
    ts = now.strftime('%Y-%m-%d %H:%M')
    if symbol not in _price_history:
        _price_history[symbol] = deque(maxlen=PRICE_HISTORY_MAX)
    hist = _price_history[symbol]
    # Avoid duplicate minute entries
    if hist and hist[-1]['time'] == ts:
        return
    hist.append({'time': ts, 'price': spot})


@app.route('/api/chart/<symbol>')
def api_chart(symbol):
    """Chart data endpoint — returns candles, CPR, VWAP, PCR, trades, recommendations."""
    symbol = symbol.upper()
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

    # Load candle data
    candles = _load_intraday_candles(symbol, date_str)

    # Load live indicators for this symbol
    eq_scan = load_json(os.path.join(BASE_DIR, 'paper_trades', 'live_scan_data.json'))
    cm_scan = load_json(os.path.join(BASE_DIR, 'paper_trades_commodity', 'live_scan_data.json'))
    stk_scan = load_json(os.path.join(BASE_DIR, 'stock_paper_trades', 'live_scan_data.json'))

    indicators = {}
    vix = None
    for scan in [eq_scan, cm_scan, stk_scan]:
        if scan and isinstance(scan, dict):
            syms = scan.get('symbols', {})
            if symbol in syms:
                indicators = syms[symbol]
            if scan.get('vix'):
                vix = scan['vix']

    # Accumulate price for in-memory fallback
    if indicators.get('spot'):
        _accumulate_price(symbol, indicators['spot'])

    # CPR levels
    cpr = {
        'pivot': indicators.get('pivot', 0),
        'tc': indicators.get('tc', 0),
        'bc': indicators.get('bc', 0),
    }

    # Extended levels
    levels = {
        'resistance': indicators.get('resistance', 0),
        'support': indicators.get('support', 0),
        'cam_r3': indicators.get('cam_r3', 0),
        'cam_r4': indicators.get('cam_r4', 0),
        'cam_s3': indicators.get('cam_s3', 0),
        'cam_s4': indicators.get('cam_s4', 0),
        'vwap': indicators.get('vwap', 0),
    }

    # Compute VWAP line from candles (intraday computed)
    vwap_line = _compute_vwap_line(candles) if candles else []

    # Load PCR history
    pcr_history = _load_market_indicators_history(symbol, date_str)

    # Trade markers (entries/exits on this symbol today)
    trade_markers = []
    for market, state_path in [('equity', EQUITY_STATE), ('commodity', COMMODITY_STATE),
                                ('stocks', STOCK_STATE), ('oi', OI_STATE)]:
        state_data = load_json(state_path)
        if not state_data:
            continue
        # Open positions
        for pos in state_data.get('positions', []):
            pos_sym = pos.get('symbol', pos.get('commodity', ''))
            if pos_sym != symbol:
                continue
            ts = pos.get('timestamp', '')
            try:
                dt = datetime.fromisoformat(ts)
                unix_ts = int(dt.timestamp())
            except Exception:
                continue
            trade_markers.append({
                'time': unix_ts,
                'position': 'belowBar' if 'CE' in pos.get('signal_type', '') else 'aboveBar',
                'color': '#00d26a' if 'CE' in pos.get('signal_type', '') else '#ff3b5c',
                'shape': 'arrowUp' if 'CE' in pos.get('signal_type', '') else 'arrowDown',
                'text': f"{pos.get('strategy', '')} {pos.get('signal_type', '')} @ {pos.get('entry_premium', 0):.1f}",
                'type': 'entry',
                'strategy': pos.get('strategy', ''),
            })
        # Closed trades today
        today_str = date_str
        for t in state_data.get('closed_trades', []):
            t_sym = t.get('symbol', t.get('commodity', ''))
            if t_sym != symbol:
                continue
            exit_ts = t.get('exit_time', t.get('closed_at', ''))
            entry_ts = t.get('timestamp', '')
            if today_str not in exit_ts and today_str not in entry_ts:
                continue
            # Entry marker
            try:
                dt = datetime.fromisoformat(entry_ts)
                unix_ts = int(dt.timestamp())
                trade_markers.append({
                    'time': unix_ts,
                    'position': 'belowBar' if 'CE' in t.get('signal_type', '') else 'aboveBar',
                    'color': '#3b82f6',
                    'shape': 'arrowUp' if 'CE' in t.get('signal_type', '') else 'arrowDown',
                    'text': f"ENTRY {t.get('strategy', '')} @ {t.get('entry_premium', 0):.1f}",
                    'type': 'entry',
                    'strategy': t.get('strategy', ''),
                })
            except Exception:
                pass
            # Exit marker
            try:
                dt = datetime.fromisoformat(exit_ts)
                unix_ts = int(dt.timestamp())
                pnl = t.get('pnl', 0)
                trade_markers.append({
                    'time': unix_ts,
                    'position': 'aboveBar' if 'CE' in t.get('signal_type', '') else 'belowBar',
                    'color': '#00d26a' if pnl >= 0 else '#ff3b5c',
                    'shape': 'circle',
                    'text': f"EXIT {t.get('exit_reason', '')} P&L:{pnl:+.0f}",
                    'type': 'exit',
                    'pnl': pnl,
                })
            except Exception:
                pass

    # Get open positions for this symbol (for recommendations context)
    open_positions = []
    for state_path in [EQUITY_STATE, COMMODITY_STATE, STOCK_STATE, OI_STATE]:
        state_data = load_json(state_path)
        if state_data:
            for pos in state_data.get('positions', []):
                if pos.get('symbol', pos.get('commodity', '')) == symbol:
                    open_positions.append(pos)

    # Generate recommendations
    recommendations = _generate_recommendations(symbol, indicators, candles, cpr, open_positions)

    # Current indicator snapshot
    current_indicators = {
        'spot': indicators.get('spot', 0),
        'pcr': indicators.get('pcr', 0),
        'pcr_direction': indicators.get('pcr_shift', indicators.get('pcr_direction', '')),
        'oi_sentiment': indicators.get('oi_sentiment', ''),
        'iv': indicators.get('iv', 0),
        'hv': indicators.get('hv', 0),
        'atr': indicators.get('atr', 0),
        'vwap': indicators.get('vwap', 0),
        'cpr_width': indicators.get('cpr_width', 0),
        'vix': vix,
    }

    return jsonify({
        'symbol': symbol,
        'date': date_str,
        'candles': candles,
        'cpr': cpr,
        'levels': levels,
        'vwap_line': vwap_line,
        'pcr_history': pcr_history,
        'trade_markers': trade_markers,
        'indicators': current_indicators,
        'recommendations': recommendations,
        'candle_count': len(candles),
    })


@app.route('/api/chart/symbols')
def api_chart_symbols():
    """Return available symbols for chart selection."""
    symbols = ['NIFTY', 'BANKNIFTY', 'SENSEX']
    # Add commodity symbols if data exists
    cm_scan = load_json(os.path.join(BASE_DIR, 'paper_trades_commodity', 'live_scan_data.json'))
    if cm_scan and cm_scan.get('symbols'):
        symbols += list(cm_scan['symbols'].keys())
    # Add stock symbols from open positions
    stk_data = load_json(STOCK_STATE)
    if stk_data:
        for pos in stk_data.get('positions', []):
            sym = pos.get('symbol', '')
            if sym and sym not in symbols:
                symbols.append(sym)
    return jsonify({'symbols': symbols})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
