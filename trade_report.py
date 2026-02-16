"""
TRADE REPORT GENERATOR
======================
Generates detailed CSV reports for all paper trading activity.

Reports generated:
1. open_positions.csv        - All currently open positions (equity + commodity)
2. closed_trades.csv         - All closed/completed trades with PnL
3. signals_log.csv           - All signals generated (executed or not)
4. daily_summary.csv         - Day-wise PnL summary
5. strategy_performance.csv  - Strategy-wise performance breakdown
6. portfolio_summary.csv     - Overall portfolio metrics

Usage:
    python trade_report.py              # Generate all reports
    python trade_report.py --live       # Connect to API, update prices, then report
    python trade_report.py --date 2026-02-16  # Report for specific date
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades')
COMMODITY_DIR = os.path.join(BASE_DIR, 'paper_trades_commodity')
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

INITIAL_CAPITAL = 300000


def load_equity_state():
    """Load equity portfolio state."""
    sf = os.path.join(PAPER_DIR, 'portfolio_state.json')
    if os.path.exists(sf):
        with open(sf, 'r') as f:
            return json.load(f)
    return {'capital': INITIAL_CAPITAL, 'positions': [], 'closed_trades': [], 'daily_pnl': {}}


def load_commodity_state():
    """Load commodity portfolio state."""
    sf = os.path.join(COMMODITY_DIR, 'commodity_portfolio_state.json')
    if os.path.exists(sf):
        with open(sf, 'r') as f:
            return json.load(f)
    return {'capital': INITIAL_CAPITAL, 'positions': [], 'closed_trades': [], 'daily_pnl': {}}


def load_all_signals():
    """Load all signal CSVs from both equity and commodity."""
    all_signals = []

    # Equity signals
    for f in sorted(os.listdir(PAPER_DIR)):
        if f.startswith('signals_') and f.endswith('.csv'):
            fpath = os.path.join(PAPER_DIR, f)
            try:
                df = pd.read_csv(fpath)
                df['market'] = 'EQUITY'
                all_signals.append(df)
            except Exception:
                pass

    # Commodity signals
    if os.path.exists(COMMODITY_DIR):
        for f in sorted(os.listdir(COMMODITY_DIR)):
            if f.startswith('commodity_signals_') and f.endswith('.csv'):
                fpath = os.path.join(COMMODITY_DIR, f)
                try:
                    df = pd.read_csv(fpath)
                    df['market'] = 'COMMODITY'
                    # Normalize column names
                    if 'commodity' in df.columns and 'symbol' not in df.columns:
                        df['symbol'] = df['commodity']
                    all_signals.append(df)
                except Exception:
                    pass

    if all_signals:
        return pd.concat(all_signals, ignore_index=True)
    return pd.DataFrame()


def generate_open_positions_csv(eq_state, comm_state):
    """Report 1: All open positions."""
    rows = []

    # Equity positions
    for p in eq_state.get('positions', []):
        rows.append({
            'Market': 'EQUITY',
            'Trade_ID': p.get('id', ''),
            'Entry_Time': p.get('timestamp', ''),
            'Strategy': p.get('strategy', ''),
            'Symbol': p.get('symbol', ''),
            'Signal_Type': p.get('signal_type', ''),
            'Direction': 'SELL' if p.get('is_sell') else 'BUY',
            'Strike': p.get('strike', 0),
            'Entry_Premium': p.get('entry_premium', 0),
            'Current_Premium': p.get('current_premium', 0),
            'Lot_Size': p.get('lot_size', 0),
            'Multiplier': 1,
            'Entry_Cost': p.get('entry_cost', 0),
            'Unrealized_PnL': p.get('unrealized_pnl', 0),
            'Delta': p.get('delta', 0),
            'Gamma': p.get('gamma', 0),
            'Theta': p.get('theta', 0),
            'IV_Pct': p.get('iv', 0),
            'DTE': p.get('dte', 0),
            'Target': p.get('details', {}).get('target', '') if isinstance(p.get('details'), dict) else '',
            'Stop_Loss': p.get('details', {}).get('sl', '') if isinstance(p.get('details'), dict) else '',
            'Spot_at_Entry': p.get('details', {}).get('spot', '') if isinstance(p.get('details'), dict) else '',
            'Entry_Reason': p.get('details', {}).get('reason', '') if isinstance(p.get('details'), dict) else '',
            'Status': 'OPEN',
        })

    # Commodity positions
    for p in comm_state.get('positions', []):
        rows.append({
            'Market': 'COMMODITY',
            'Trade_ID': p.get('id', ''),
            'Entry_Time': p.get('timestamp', ''),
            'Strategy': p.get('strategy', ''),
            'Symbol': p.get('commodity', p.get('symbol', '')),
            'Signal_Type': p.get('signal_type', ''),
            'Direction': 'SELL' if p.get('is_sell') else 'BUY',
            'Strike': p.get('strike', 0),
            'Entry_Premium': p.get('entry_premium', 0),
            'Current_Premium': p.get('current_premium', 0),
            'Lot_Size': p.get('lot_size', 0),
            'Multiplier': p.get('multiplier', 1),
            'Entry_Cost': p.get('entry_cost', 0),
            'Unrealized_PnL': p.get('unrealized_pnl', 0),
            'Delta': p.get('delta', 0),
            'Gamma': p.get('gamma', 0),
            'Theta': p.get('theta', 0),
            'IV_Pct': '',
            'DTE': p.get('dte', 0),
            'Target': p.get('details', {}).get('target', '') if isinstance(p.get('details'), dict) else '',
            'Stop_Loss': p.get('details', {}).get('sl', '') if isinstance(p.get('details'), dict) else '',
            'Spot_at_Entry': p.get('details', {}).get('spot', '') if isinstance(p.get('details'), dict) else '',
            'Entry_Reason': p.get('details', {}).get('reason', '') if isinstance(p.get('details'), dict) else '',
            'Status': 'OPEN',
        })

    df = pd.DataFrame(rows)
    if len(df) > 0:
        df = df.sort_values('Entry_Time', ascending=False)
    return df


def generate_closed_trades_csv(eq_state, comm_state):
    """Report 2: All closed trades with PnL."""
    rows = []

    # Equity closed trades
    for t in eq_state.get('closed_trades', []):
        entry_premium = t.get('entry_premium', 0)
        exit_premium = t.get('exit_premium', 0)
        pnl = t.get('pnl', 0)
        lot = t.get('lot_size', 1)

        rows.append({
            'Market': 'EQUITY',
            'Trade_ID': t.get('id', ''),
            'Entry_Time': t.get('timestamp', ''),
            'Exit_Time': t.get('exit_time', ''),
            'Strategy': t.get('strategy', ''),
            'Symbol': t.get('symbol', ''),
            'Signal_Type': t.get('signal_type', ''),
            'Direction': 'SELL' if t.get('is_sell') else 'BUY',
            'Strike': t.get('strike', 0),
            'Entry_Premium': entry_premium,
            'Exit_Premium': exit_premium,
            'Lot_Size': lot,
            'Multiplier': 1,
            'Premium_Change': round(exit_premium - entry_premium, 2),
            'Premium_Change_Pct': round((exit_premium - entry_premium) / max(entry_premium, 0.01) * 100, 2),
            'Gross_PnL': round((exit_premium - entry_premium) * lot if not t.get('is_sell') else (entry_premium - exit_premium) * lot, 2),
            'Entry_Cost': t.get('entry_cost', 0),
            'Exit_Cost': t.get('exit_cost', 0),
            'Total_Costs': round(t.get('entry_cost', 0) + t.get('exit_cost', 0), 2),
            'Net_PnL': pnl,
            'Net_PnL_Pct': round(pnl / max(entry_premium * lot, 1) * 100, 2),
            'Exit_Reason': t.get('exit_reason', ''),
            'Delta_at_Entry': t.get('delta', 0),
            'IV_at_Entry': t.get('iv', 0),
            'DTE_at_Entry': t.get('dte', 0),
            'Entry_Reason': t.get('details', {}).get('reason', '') if isinstance(t.get('details'), dict) else '',
            'Spot_at_Entry': t.get('details', {}).get('spot', '') if isinstance(t.get('details'), dict) else '',
            'Result': 'WIN' if pnl > 0 else 'LOSS',
            'Duration_Hours': '',
        })

    # Commodity closed trades
    for t in comm_state.get('closed_trades', []):
        entry_premium = t.get('entry_premium', 0)
        exit_premium = t.get('exit_premium', 0)
        pnl = t.get('pnl', 0)
        lot = t.get('lot_size', 1)
        mult = t.get('multiplier', 1)

        rows.append({
            'Market': 'COMMODITY',
            'Trade_ID': t.get('id', ''),
            'Entry_Time': t.get('timestamp', ''),
            'Exit_Time': t.get('exit_time', ''),
            'Strategy': t.get('strategy', ''),
            'Symbol': t.get('commodity', t.get('symbol', '')),
            'Signal_Type': t.get('signal_type', ''),
            'Direction': 'SELL' if t.get('is_sell') else 'BUY',
            'Strike': t.get('strike', 0),
            'Entry_Premium': entry_premium,
            'Exit_Premium': exit_premium,
            'Lot_Size': lot,
            'Multiplier': mult,
            'Premium_Change': round(exit_premium - entry_premium, 2),
            'Premium_Change_Pct': round((exit_premium - entry_premium) / max(entry_premium, 0.01) * 100, 2),
            'Gross_PnL': round((exit_premium - entry_premium) * lot * mult if not t.get('is_sell') else (entry_premium - exit_premium) * lot * mult, 2),
            'Entry_Cost': t.get('entry_cost', 0),
            'Exit_Cost': t.get('exit_cost', 0) if 'exit_cost' in t else 0,
            'Total_Costs': round(t.get('entry_cost', 0) + t.get('exit_cost', 0), 2),
            'Net_PnL': pnl,
            'Net_PnL_Pct': round(pnl / max(entry_premium * lot * mult, 1) * 100, 2),
            'Exit_Reason': t.get('exit_reason', ''),
            'Delta_at_Entry': t.get('delta', 0),
            'IV_at_Entry': '',
            'DTE_at_Entry': t.get('dte', 0),
            'Entry_Reason': t.get('details', {}).get('reason', '') if isinstance(t.get('details'), dict) else '',
            'Spot_at_Entry': t.get('details', {}).get('spot', '') if isinstance(t.get('details'), dict) else '',
            'Result': 'WIN' if pnl > 0 else 'LOSS',
            'Duration_Hours': '',
        })

    df = pd.DataFrame(rows)

    # Calculate duration
    if len(df) > 0:
        try:
            entry_times = pd.to_datetime(df['Entry_Time'], errors='coerce')
            exit_times = pd.to_datetime(df['Exit_Time'], errors='coerce')
            duration = (exit_times - entry_times).dt.total_seconds() / 3600
            df['Duration_Hours'] = duration.round(2)
        except Exception:
            pass
        df = df.sort_values('Exit_Time', ascending=False)

    return df


def generate_daily_summary_csv(eq_state, comm_state):
    """Report 3: Daily PnL summary."""
    # Merge daily PnL from both
    all_dates = set()
    eq_daily = eq_state.get('daily_pnl', {})
    comm_daily = comm_state.get('daily_pnl', {})
    all_dates.update(eq_daily.keys())
    all_dates.update(comm_daily.keys())

    rows = []
    eq_cum = 0
    comm_cum = 0
    total_cum = 0

    for date in sorted(all_dates):
        eq_pnl = eq_daily.get(date, 0)
        comm_pnl = comm_daily.get(date, 0)
        total_pnl = eq_pnl + comm_pnl
        eq_cum += eq_pnl
        comm_cum += comm_pnl
        total_cum += total_pnl

        # Count trades for the day
        eq_trades = [t for t in eq_state.get('closed_trades', [])
                     if t.get('exit_time', '').startswith(date)]
        comm_trades = [t for t in comm_state.get('closed_trades', [])
                       if t.get('exit_time', '').startswith(date)]

        eq_wins = sum(1 for t in eq_trades if t.get('pnl', 0) > 0)
        comm_wins = sum(1 for t in comm_trades if t.get('pnl', 0) > 0)
        total_trades = len(eq_trades) + len(comm_trades)
        total_wins = eq_wins + comm_wins

        rows.append({
            'Date': date,
            'Equity_PnL': round(eq_pnl, 2),
            'Commodity_PnL': round(comm_pnl, 2),
            'Total_PnL': round(total_pnl, 2),
            'Equity_Cumulative': round(eq_cum, 2),
            'Commodity_Cumulative': round(comm_cum, 2),
            'Total_Cumulative': round(total_cum, 2),
            'Equity_Capital': round(INITIAL_CAPITAL + eq_cum, 2),
            'Commodity_Capital': round(INITIAL_CAPITAL + comm_cum, 2),
            'Combined_Capital': round(2 * INITIAL_CAPITAL + total_cum, 2),
            'Equity_Trades': len(eq_trades),
            'Commodity_Trades': len(comm_trades),
            'Total_Trades': total_trades,
            'Wins': total_wins,
            'Losses': total_trades - total_wins,
            'Win_Rate_Pct': round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0,
            'Daily_Return_Pct': round(total_pnl / (2 * INITIAL_CAPITAL) * 100, 4),
        })

    return pd.DataFrame(rows)


def generate_strategy_performance_csv(eq_state, comm_state):
    """Report 4: Strategy-wise performance breakdown."""
    all_trades = eq_state.get('closed_trades', []) + comm_state.get('closed_trades', [])

    if not all_trades:
        return pd.DataFrame()

    # Group by strategy
    strat_data = {}
    for t in all_trades:
        strat = t.get('strategy', 'Unknown')
        if strat not in strat_data:
            strat_data[strat] = {'trades': [], 'pnls': []}
        strat_data[strat]['trades'].append(t)
        strat_data[strat]['pnls'].append(t.get('pnl', 0))

    rows = []
    for strat, data in sorted(strat_data.items()):
        pnls = data['pnls']
        trades = data['trades']
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # By symbol
        symbols = set(t.get('symbol', t.get('commodity', '')) for t in trades)

        rows.append({
            'Strategy': strat,
            'Total_Trades': len(pnls),
            'Wins': len(wins),
            'Losses': len(losses),
            'Win_Rate_Pct': round(len(wins) / len(pnls) * 100, 1),
            'Total_PnL': round(sum(pnls), 2),
            'Avg_PnL': round(np.mean(pnls), 2),
            'Max_Win': round(max(pnls), 2) if pnls else 0,
            'Max_Loss': round(min(pnls), 2) if pnls else 0,
            'Avg_Win': round(np.mean(wins), 2) if wins else 0,
            'Avg_Loss': round(np.mean(losses), 2) if losses else 0,
            'Profit_Factor': round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else float('inf'),
            'Win_Loss_Ratio': round(np.mean(wins) / abs(np.mean(losses)), 2) if losses and np.mean(losses) != 0 else float('inf'),
            'Std_PnL': round(np.std(pnls), 2) if len(pnls) > 1 else 0,
            'Sharpe_Approx': round(np.mean(pnls) / np.std(pnls), 2) if len(pnls) > 1 and np.std(pnls) > 0 else 0,
            'Symbols_Traded': ', '.join(sorted(symbols)),
        })

    # Add overall row
    all_pnls = [t.get('pnl', 0) for t in all_trades]
    all_wins = [p for p in all_pnls if p > 0]
    all_losses = [p for p in all_pnls if p <= 0]
    rows.append({
        'Strategy': '** OVERALL **',
        'Total_Trades': len(all_pnls),
        'Wins': len(all_wins),
        'Losses': len(all_losses),
        'Win_Rate_Pct': round(len(all_wins) / len(all_pnls) * 100, 1) if all_pnls else 0,
        'Total_PnL': round(sum(all_pnls), 2),
        'Avg_PnL': round(np.mean(all_pnls), 2) if all_pnls else 0,
        'Max_Win': round(max(all_pnls), 2) if all_pnls else 0,
        'Max_Loss': round(min(all_pnls), 2) if all_pnls else 0,
        'Avg_Win': round(np.mean(all_wins), 2) if all_wins else 0,
        'Avg_Loss': round(np.mean(all_losses), 2) if all_losses else 0,
        'Profit_Factor': round(sum(all_wins) / abs(sum(all_losses)), 2) if all_losses and sum(all_losses) != 0 else float('inf'),
        'Win_Loss_Ratio': round(np.mean(all_wins) / abs(np.mean(all_losses)), 2) if all_losses and np.mean(all_losses) != 0 else float('inf'),
        'Std_PnL': round(np.std(all_pnls), 2) if len(all_pnls) > 1 else 0,
        'Sharpe_Approx': round(np.mean(all_pnls) / np.std(all_pnls), 2) if len(all_pnls) > 1 and np.std(all_pnls) > 0 else 0,
        'Symbols_Traded': 'ALL',
    })

    return pd.DataFrame(rows)


def generate_portfolio_summary_csv(eq_state, comm_state):
    """Report 5: Overall portfolio summary."""
    eq_trades = eq_state.get('closed_trades', [])
    comm_trades = comm_state.get('closed_trades', [])
    eq_positions = eq_state.get('positions', [])
    comm_positions = comm_state.get('positions', [])

    eq_capital = eq_state.get('capital', INITIAL_CAPITAL)
    comm_capital = comm_state.get('capital', INITIAL_CAPITAL)

    eq_realized = sum(t.get('pnl', 0) for t in eq_trades)
    comm_realized = sum(t.get('pnl', 0) for t in comm_trades)
    eq_unrealized = sum(p.get('unrealized_pnl', 0) for p in eq_positions)
    comm_unrealized = sum(p.get('unrealized_pnl', 0) for p in comm_positions)

    eq_wins = sum(1 for t in eq_trades if t.get('pnl', 0) > 0)
    comm_wins = sum(1 for t in comm_trades if t.get('pnl', 0) > 0)

    rows = [
        {
            'Metric': 'Initial Capital',
            'Equity': f'Rs {INITIAL_CAPITAL:,.0f}',
            'Commodity': f'Rs {INITIAL_CAPITAL:,.0f}',
            'Combined': f'Rs {2*INITIAL_CAPITAL:,.0f}',
        },
        {
            'Metric': 'Current Capital',
            'Equity': f'Rs {eq_capital:,.0f}',
            'Commodity': f'Rs {comm_capital:,.0f}',
            'Combined': f'Rs {eq_capital + comm_capital:,.0f}',
        },
        {
            'Metric': 'Total Return',
            'Equity': f'{(eq_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:.2f}%',
            'Commodity': f'{(comm_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:.2f}%',
            'Combined': f'{(eq_capital + comm_capital - 2*INITIAL_CAPITAL) / (2*INITIAL_CAPITAL) * 100:.2f}%',
        },
        {
            'Metric': 'Realized PnL',
            'Equity': f'Rs {eq_realized:,.2f}',
            'Commodity': f'Rs {comm_realized:,.2f}',
            'Combined': f'Rs {eq_realized + comm_realized:,.2f}',
        },
        {
            'Metric': 'Unrealized PnL',
            'Equity': f'Rs {eq_unrealized:,.2f}',
            'Commodity': f'Rs {comm_unrealized:,.2f}',
            'Combined': f'Rs {eq_unrealized + comm_unrealized:,.2f}',
        },
        {
            'Metric': 'Open Positions',
            'Equity': str(len(eq_positions)),
            'Commodity': str(len(comm_positions)),
            'Combined': str(len(eq_positions) + len(comm_positions)),
        },
        {
            'Metric': 'Closed Trades',
            'Equity': str(len(eq_trades)),
            'Commodity': str(len(comm_trades)),
            'Combined': str(len(eq_trades) + len(comm_trades)),
        },
        {
            'Metric': 'Win Rate',
            'Equity': f'{eq_wins / len(eq_trades) * 100:.1f}%' if eq_trades else 'N/A',
            'Commodity': f'{comm_wins / len(comm_trades) * 100:.1f}%' if comm_trades else 'N/A',
            'Combined': f'{(eq_wins + comm_wins) / max(len(eq_trades) + len(comm_trades), 1) * 100:.1f}%' if (eq_trades or comm_trades) else 'N/A',
        },
        {
            'Metric': 'Total Wins',
            'Equity': str(eq_wins),
            'Commodity': str(comm_wins),
            'Combined': str(eq_wins + comm_wins),
        },
        {
            'Metric': 'Total Losses',
            'Equity': str(len(eq_trades) - eq_wins),
            'Commodity': str(len(comm_trades) - comm_wins),
            'Combined': str(len(eq_trades) + len(comm_trades) - eq_wins - comm_wins),
        },
        {
            'Metric': 'Strategies Active',
            'Equity': ', '.join(sorted(set(p.get('strategy', '') for p in eq_positions))),
            'Commodity': ', '.join(sorted(set(p.get('strategy', '') for p in comm_positions))),
            'Combined': '',
        },
        {
            'Metric': 'Symbols Active',
            'Equity': ', '.join(sorted(set(p.get('symbol', '') for p in eq_positions))),
            'Commodity': ', '.join(sorted(set(p.get('commodity', p.get('symbol', '')) for p in comm_positions))),
            'Combined': '',
        },
        {
            'Metric': 'Last Updated',
            'Equity': eq_state.get('last_updated', ''),
            'Commodity': comm_state.get('last_updated', ''),
            'Combined': datetime.now().isoformat(),
        },
    ]

    return pd.DataFrame(rows)


def generate_signals_report_csv():
    """Report 6: All signals log consolidated."""
    df = load_all_signals()
    if len(df) == 0:
        return pd.DataFrame()

    # Standardize columns
    cols = ['timestamp', 'market', 'strategy', 'symbol', 'type', 'strike',
            'premium', 'delta', 'gamma', 'theta', 'spot', 'dte', 'reason',
            'target', 'sl']
    for c in cols:
        if c not in df.columns:
            df[c] = ''

    df = df[cols].copy()
    df.columns = ['Timestamp', 'Market', 'Strategy', 'Symbol', 'Signal_Type',
                   'Strike', 'Premium', 'Delta', 'Gamma', 'Theta', 'Spot',
                   'DTE', 'Reason', 'Target', 'Stop_Loss']

    df = df.sort_values('Timestamp', ascending=False)
    return df


def main():
    parser = argparse.ArgumentParser(description='Generate Paper Trade Reports')
    parser.add_argument('--live', action='store_true', help='Connect to API for live prices')
    parser.add_argument('--date', type=str, default=None, help='Specific date (YYYY-MM-DD)')
    args = parser.parse_args()

    print("=" * 70)
    print("  PAPER TRADING - DETAILED REPORT GENERATOR")
    print("=" * 70)

    # Load states
    eq_state = load_equity_state()
    comm_state = load_commodity_state()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Generate all reports
    reports = {}

    # 1. Open Positions
    print("\n  Generating: open_positions.csv ...")
    df = generate_open_positions_csv(eq_state, comm_state)
    fname = os.path.join(REPORT_DIR, f'open_positions_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Open Positions'] = (fname, len(df))
    # Also save a "latest" copy
    df.to_csv(os.path.join(REPORT_DIR, 'open_positions_latest.csv'), index=False)

    # 2. Closed Trades
    print("  Generating: closed_trades.csv ...")
    df = generate_closed_trades_csv(eq_state, comm_state)
    fname = os.path.join(REPORT_DIR, f'closed_trades_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Closed Trades'] = (fname, len(df))
    df.to_csv(os.path.join(REPORT_DIR, 'closed_trades_latest.csv'), index=False)

    # 3. Signals Log
    print("  Generating: signals_log.csv ...")
    df = generate_signals_report_csv()
    fname = os.path.join(REPORT_DIR, f'signals_log_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Signals Log'] = (fname, len(df))
    df.to_csv(os.path.join(REPORT_DIR, 'signals_log_latest.csv'), index=False)

    # 4. Daily Summary
    print("  Generating: daily_summary.csv ...")
    df = generate_daily_summary_csv(eq_state, comm_state)
    fname = os.path.join(REPORT_DIR, f'daily_summary_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Daily Summary'] = (fname, len(df))
    df.to_csv(os.path.join(REPORT_DIR, 'daily_summary_latest.csv'), index=False)

    # 5. Strategy Performance
    print("  Generating: strategy_performance.csv ...")
    df = generate_strategy_performance_csv(eq_state, comm_state)
    fname = os.path.join(REPORT_DIR, f'strategy_performance_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Strategy Performance'] = (fname, len(df))
    df.to_csv(os.path.join(REPORT_DIR, 'strategy_performance_latest.csv'), index=False)

    # 6. Portfolio Summary
    print("  Generating: portfolio_summary.csv ...")
    df = generate_portfolio_summary_csv(eq_state, comm_state)
    fname = os.path.join(REPORT_DIR, f'portfolio_summary_{timestamp}.csv')
    df.to_csv(fname, index=False)
    reports['Portfolio Summary'] = (fname, len(df))
    df.to_csv(os.path.join(REPORT_DIR, 'portfolio_summary_latest.csv'), index=False)

    # Print summary
    print("\n" + "=" * 70)
    print("  REPORTS GENERATED")
    print("=" * 70)
    for name, (fpath, count) in reports.items():
        print(f"    {name:25s} | {count:5d} rows | {os.path.basename(fpath)}")
    print(f"\n  Output directory: {REPORT_DIR}")
    print("=" * 70)

    # Print quick portfolio overview
    eq_cap = eq_state.get('capital', INITIAL_CAPITAL)
    comm_cap = comm_state.get('capital', INITIAL_CAPITAL)
    total_cap = eq_cap + comm_cap
    eq_return = (eq_cap - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    comm_return = (comm_cap - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    total_return = (total_cap - 2 * INITIAL_CAPITAL) / (2 * INITIAL_CAPITAL) * 100

    eq_open = len(eq_state.get('positions', []))
    comm_open = len(comm_state.get('positions', []))
    eq_closed = len(eq_state.get('closed_trades', []))
    comm_closed = len(comm_state.get('closed_trades', []))

    eq_unrealized = sum(p.get('unrealized_pnl', 0) for p in eq_state.get('positions', []))
    comm_unrealized = sum(p.get('unrealized_pnl', 0) for p in comm_state.get('positions', []))

    print(f"\n  PORTFOLIO SNAPSHOT:")
    print(f"  {'':25s}   {'EQUITY':>12s}   {'COMMODITY':>12s}   {'COMBINED':>12s}")
    print(f"  {'Capital':25s}   Rs {eq_cap:>9,.0f}   Rs {comm_cap:>9,.0f}   Rs {total_cap:>9,.0f}")
    print(f"  {'Return':25s}   {eq_return:>11.2f}%   {comm_return:>11.2f}%   {total_return:>11.2f}%")
    print(f"  {'Open Positions':25s}   {eq_open:>12d}   {comm_open:>12d}   {eq_open+comm_open:>12d}")
    print(f"  {'Closed Trades':25s}   {eq_closed:>12d}   {comm_closed:>12d}   {eq_closed+comm_closed:>12d}")
    print(f"  {'Unrealized PnL':25s}   Rs {eq_unrealized:>9,.0f}   Rs {comm_unrealized:>9,.0f}   Rs {eq_unrealized+comm_unrealized:>9,.0f}")
    print("=" * 70)


if __name__ == '__main__':
    main()
