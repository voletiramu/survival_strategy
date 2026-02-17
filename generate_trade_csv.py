"""
Generate a clean trade CSV with:
S.No, Date, Market, Strategy, Symbol, Signal_Type, Direction, Strike,
Lot_Size, Entry_Timestamp, Exit_Timestamp, Entry_Price, Exit_Price,
Total_PnL, Status
"""

import sys
import os
import json
import pandas as pd
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades')
COMMODITY_DIR = os.path.join(BASE_DIR, 'paper_trades_commodity')
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)


def load_json(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}


def main():
    eq = load_json(os.path.join(PAPER_DIR, 'portfolio_state.json'))
    comm = load_json(os.path.join(COMMODITY_DIR, 'commodity_portfolio_state.json'))

    rows = []
    sno = 0

    # --- EQUITY CLOSED TRADES ---
    for t in eq.get('closed_trades', []):
        sno += 1
        entry_ts = t.get('timestamp', '')
        exit_ts = t.get('exit_time', '')
        entry_dt = entry_ts[:10] if entry_ts else ''
        lot = t.get('lot_size', 1)
        entry_p = t.get('entry_premium', 0)
        exit_p = t.get('exit_premium', 0)
        pnl = t.get('pnl', 0)

        rows.append({
            'S.No': sno,
            'Date': entry_dt,
            'Market': 'EQUITY',
            'Strategy': t.get('strategy', ''),
            'Symbol': t.get('symbol', ''),
            'Signal_Type': t.get('signal_type', ''),
            'Direction': 'SELL' if t.get('is_sell') else 'BUY',
            'Strike': t.get('strike', 0),
            'Lot_Size': lot,
            'Entry_Timestamp': entry_ts,
            'Exit_Timestamp': exit_ts,
            'Entry_Price': round(entry_p, 2),
            'Exit_Price': round(exit_p, 2),
            'Total_PnL': round(pnl, 2),
            'Status': 'CLOSED - ' + t.get('exit_reason', ''),
        })

    # --- EQUITY OPEN POSITIONS ---
    for p in eq.get('positions', []):
        sno += 1
        entry_ts = p.get('timestamp', '')
        entry_dt = entry_ts[:10] if entry_ts else ''
        lot = p.get('lot_size', 1)
        entry_p = p.get('entry_premium', 0)
        current_p = p.get('current_premium', 0)
        unrealized = p.get('unrealized_pnl', 0)

        rows.append({
            'S.No': sno,
            'Date': entry_dt,
            'Market': 'EQUITY',
            'Strategy': p.get('strategy', ''),
            'Symbol': p.get('symbol', ''),
            'Signal_Type': p.get('signal_type', ''),
            'Direction': 'SELL' if p.get('is_sell') else 'BUY',
            'Strike': p.get('strike', 0),
            'Lot_Size': lot,
            'Entry_Timestamp': entry_ts,
            'Exit_Timestamp': '-- OPEN --',
            'Entry_Price': round(entry_p, 2),
            'Exit_Price': round(current_p, 2),
            'Total_PnL': round(unrealized, 2),
            'Status': 'OPEN',
        })

    # --- COMMODITY CLOSED TRADES ---
    for t in comm.get('closed_trades', []):
        sno += 1
        entry_ts = t.get('timestamp', '')
        exit_ts = t.get('exit_time', '')
        entry_dt = entry_ts[:10] if entry_ts else ''
        lot = t.get('lot_size', 1)
        mult = t.get('multiplier', 1)
        entry_p = t.get('entry_premium', 0)
        exit_p = t.get('exit_premium', 0)
        pnl = t.get('pnl', 0)

        rows.append({
            'S.No': sno,
            'Date': entry_dt,
            'Market': 'COMMODITY',
            'Strategy': t.get('strategy', ''),
            'Symbol': t.get('commodity', t.get('symbol', '')),
            'Signal_Type': t.get('signal_type', ''),
            'Direction': 'SELL' if t.get('is_sell') else 'BUY',
            'Strike': t.get('strike', 0),
            'Lot_Size': f"{lot}x{mult}",
            'Entry_Timestamp': entry_ts,
            'Exit_Timestamp': exit_ts,
            'Entry_Price': round(entry_p, 2),
            'Exit_Price': round(exit_p, 2),
            'Total_PnL': round(pnl, 2),
            'Status': 'CLOSED - ' + t.get('exit_reason', ''),
        })

    # --- COMMODITY OPEN POSITIONS ---
    for p in comm.get('positions', []):
        sno += 1
        entry_ts = p.get('timestamp', '')
        entry_dt = entry_ts[:10] if entry_ts else ''
        lot = p.get('lot_size', 1)
        mult = p.get('multiplier', 1)
        entry_p = p.get('entry_premium', 0)
        current_p = p.get('current_premium', 0)
        unrealized = p.get('unrealized_pnl', 0)

        rows.append({
            'S.No': sno,
            'Date': entry_dt,
            'Market': 'COMMODITY',
            'Strategy': p.get('strategy', ''),
            'Symbol': p.get('commodity', p.get('symbol', '')),
            'Signal_Type': p.get('signal_type', ''),
            'Direction': 'SELL' if p.get('is_sell') else 'BUY',
            'Strike': p.get('strike', 0),
            'Lot_Size': f"{lot}x{mult}",
            'Entry_Timestamp': entry_ts,
            'Exit_Timestamp': '-- OPEN --',
            'Entry_Price': round(entry_p, 2),
            'Exit_Price': round(current_p, 2),
            'Total_PnL': round(unrealized, 2),
            'Status': 'OPEN',
        })

    df = pd.DataFrame(rows)

    # Save CSV
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(REPORT_DIR, f'all_trades_{timestamp}.csv')
    latest_path = os.path.join(REPORT_DIR, 'all_trades_latest.csv')
    df.to_csv(csv_path, index=False)
    try:
        df.to_csv(latest_path, index=False)
    except PermissionError:
        alt_path = os.path.join(REPORT_DIR, 'all_trades_current.csv')
        df.to_csv(alt_path, index=False)
        print(f"  (latest.csv locked, saved to {alt_path})")

    # Print to console as table
    print("\n" + "=" * 140)
    print("  ALL TRADES - PAPER TRADING (EQUITY + COMMODITY)")
    print("  Generated: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 140)

    # Summary counts
    closed = df[df['Status'].str.startswith('CLOSED')]
    opened = df[df['Status'] == 'OPEN']
    total_realized = closed['Total_PnL'].sum() if len(closed) > 0 else 0
    total_unrealized = opened['Total_PnL'].sum() if len(opened) > 0 else 0

    # Print table header
    print(f"\n{'S.No':>4} | {'Date':>10} | {'Market':>9} | {'Strategy':>12} | {'Symbol':>10} | "
          f"{'Signal':>12} | {'Dir':>4} | {'Strike':>10} | {'Lot':>5} | "
          f"{'Entry Time':>19} | {'Exit Time':>19} | {'Entry':>9} | {'Exit':>9} | {'PnL':>10} | {'Status'}")
    print("-" * 180)

    for _, r in df.iterrows():
        entry_time = str(r['Entry_Timestamp'])[11:19] if len(str(r['Entry_Timestamp'])) > 11 else ''
        exit_time = str(r['Exit_Timestamp'])[11:19] if str(r['Exit_Timestamp']) != '-- OPEN --' and len(str(r['Exit_Timestamp'])) > 11 else '-- OPEN --'
        entry_full = str(r['Entry_Timestamp'])[:19]
        exit_full = str(r['Exit_Timestamp'])[:19] if str(r['Exit_Timestamp']) != '-- OPEN --' else '-- OPEN --'

        pnl_str = f"Rs {r['Total_PnL']:>8,.2f}"
        pnl_marker = '+' if r['Total_PnL'] > 0 else ('-' if r['Total_PnL'] < 0 else ' ')

        print(f"{r['S.No']:>4} | {r['Date']:>10} | {r['Market']:>9} | {r['Strategy']:>12} | {r['Symbol']:>10} | "
              f"{r['Signal_Type']:>12} | {r['Direction']:>4} | {r['Strike']:>10.0f} | {str(r['Lot_Size']):>5} | "
              f"{entry_full:>19} | {exit_full:>19} | {r['Entry_Price']:>9.2f} | {r['Exit_Price']:>9.2f} | "
              f"{pnl_str} | {r['Status']}")

    print("-" * 180)
    print(f"\n  Total Trades: {len(df)} | Closed: {len(closed)} | Open: {len(opened)}")
    print(f"  Realized PnL:   Rs {total_realized:>10,.2f}")
    print(f"  Unrealized PnL: Rs {total_unrealized:>10,.2f}")
    print(f"  Total PnL:      Rs {total_realized + total_unrealized:>10,.2f}")
    print(f"\n  CSV saved: {csv_path}")
    print(f"  Latest:    {latest_path}")
    print("=" * 140)


if __name__ == '__main__':
    main()
