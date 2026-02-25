"""
Generate Corrected Trade Report — Recalculated with NSE Jan 2026 Lot Sizes
==========================================================================
Reads trades from portfolio_state.json and recalculates using:
  NIFTY=65, BANKNIFTY=30, SENSEX=20

Output Format (20 columns):
  S.No | Symbol | Strategy | Entry Time | Exit Time | Per lot | No of lots |
  Entry | Target | SL | Exit | Amount Invested | Amount After Close | PnL |
  Balance | Status | PnL % | Spot Price | Delta | Exit Reason
"""

import json
import os
import csv
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

# Correct lot sizes (NSE revised Jan 2026)
CORRECT_LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
INITIAL_CAPITAL = 300000


def format_timestamp(ts_str):
    """Convert ISO timestamp to DD/MM/YYYY HH:MM format."""
    if not ts_str:
        return ''
    try:
        # Handle ISO format: 2026-02-18T09:39:55.312364
        ts_str = ts_str.replace('T', ' ')[:19]
        dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d/%m/%Y %H:%M')
    except Exception:
        try:
            dt = datetime.strptime(ts_str[:16], '%Y-%m-%d %H:%M')
            return dt.strftime('%d/%m/%Y %H:%M')
        except Exception:
            return ts_str  # Return as-is if unparseable


def compute_target_sl(entry_premium, is_sell, strategy):
    """Compute target and SL based on strategy and trade type."""
    if strategy == 'Survivor':
        # Seller: target = 20% of entry (80% profit), SL = 150% of entry
        target = round(entry_premium * 0.2, 2)
        sl = round(entry_premium * 1.5, 2)
    elif strategy == 'Gamma Blast':
        # Buyer: target = 2.5x entry, SL = 0.3x entry
        target = round(entry_premium * 2.5, 2)
        sl = round(entry_premium * 0.3, 2)
    elif strategy == 'CPR':
        # Buyer: target = 1.8x entry, SL = 0.4x entry
        target = round(entry_premium * 1.8, 2)
        sl = round(entry_premium * 0.4, 2)
    elif strategy == 'Ghost Zone':
        # Buyer: target = 1.3x entry, SL = 0.4x entry
        target = round(entry_premium * 1.3, 2)
        sl = round(entry_premium * 0.4, 2)
    elif strategy == 'PCR+VWAP':
        # Buyer: target = 2.0x entry, SL = 0.4x entry
        target = round(entry_premium * 2.0, 2)
        sl = round(entry_premium * 0.4, 2)
    else:
        target = round(entry_premium * 1.8, 2)
        sl = round(entry_premium * 0.4, 2)
    return target, sl


def load_portfolio():
    pf_file = os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json')
    with open(pf_file) as f:
        return json.load(f)


def main():
    pf = load_portfolio()
    closed = pf.get('closed_trades', [])
    open_pos = pf.get('positions', [])

    all_trades = []

    # Process CLOSED trades
    for t in closed:
        symbol = t['symbol']
        lot_size = CORRECT_LOT_SIZES.get(symbol, 65)
        entry_premium = t['entry_premium']
        exit_premium = t.get('exit_premium', 0)
        signal_type = t.get('signal_type', '')
        is_sell = 'SELL' in signal_type

        if is_sell:
            pnl = (entry_premium - exit_premium) * lot_size
            amt_invested = entry_premium * lot_size
            amt_after = exit_premium * lot_size
        else:
            pnl = (exit_premium - entry_premium) * lot_size
            amt_invested = entry_premium * lot_size
            amt_after = exit_premium * lot_size

        target, sl = compute_target_sl(entry_premium, is_sell, t['strategy'])
        pnl_pct = round((pnl / amt_invested) * 100, 1) if amt_invested > 0 else 0.0
        opt = 'CE' if 'CE' in signal_type else 'PE'

        all_trades.append({
            'symbol': f"{symbol} {t.get('strike', 0):.0f} {opt}",
            'strategy': t['strategy'],
            'entry_time': t.get('timestamp', ''),
            'exit_time': t.get('exit_time', ''),  # FIXED: was exit_timestamp/closed_at
            'per_lot': lot_size,
            'no_of_lots': 1,
            'entry': round(entry_premium, 2),
            'target': target,
            'sl': sl,
            'exit': round(exit_premium, 2),
            'amt_invested': round(amt_invested, 2),
            'amt_after': round(amt_after, 2),
            'pnl': round(pnl, 2),
            'status': 'Closed',
            'pnl_pct': pnl_pct,
            'spot_price': round(t.get('strike', 0), 2),  # Approximate spot from strike
            'delta': round(t.get('delta', 0), 4),
            'exit_reason': t.get('exit_reason', ''),
            'sort_key': t.get('timestamp', ''),
        })

    # Process OPEN positions
    for p in open_pos:
        symbol = p['symbol']
        lot_size = CORRECT_LOT_SIZES.get(symbol, 65)
        entry_premium = p['entry_premium']
        current_premium = p.get('current_premium', entry_premium)
        signal_type = p.get('signal_type', '')
        is_sell = 'SELL' in signal_type

        amt_invested = entry_premium * lot_size
        target, sl = compute_target_sl(entry_premium, is_sell, p['strategy'])
        opt = 'CE' if 'CE' in signal_type else 'PE'

        all_trades.append({
            'symbol': f"{symbol} {p.get('strike', 0):.0f} {opt}",
            'strategy': p['strategy'],
            'entry_time': p.get('timestamp', ''),
            'exit_time': '',  # OPEN — no exit yet
            'per_lot': lot_size,
            'no_of_lots': 1,
            'entry': round(entry_premium, 2),
            'target': target,
            'sl': sl,
            'exit': '',  # No exit premium yet
            'amt_invested': round(amt_invested, 2),
            'amt_after': '',  # No close yet
            'pnl': 0.00,  # OPEN trades show 0 PnL
            'status': 'Open',
            'pnl_pct': 0.0,
            'spot_price': round(p.get('strike', 0), 2),
            'delta': round(p.get('delta', 0), 4),
            'exit_reason': '',
            'sort_key': p.get('timestamp', ''),
        })

    # Sort by entry time
    all_trades.sort(key=lambda x: x['sort_key'])

    # Calculate running balance: start at 3,00,000 and accumulate
    balance = INITIAL_CAPITAL
    for t in all_trades:
        if t['status'] == 'Closed':
            balance += t['pnl']
        # OPEN trades: balance stays at previous level (no PnL impact)
        t['balance'] = round(balance, 2)

    # Stats
    closed_trades = [t for t in all_trades if t['status'] == 'Closed']
    open_trades = [t for t in all_trades if t['status'] == 'Open']
    closed_count = len(closed_trades)
    open_count = len(open_trades)
    total_realized = sum(t['pnl'] for t in closed_trades)
    wins = sum(1 for t in closed_trades if t['pnl'] > 0)
    losses = sum(1 for t in closed_trades if t['pnl'] <= 0)
    wr = wins / closed_count * 100 if closed_count > 0 else 0

    # Print report
    print("=" * 180)
    print("  TRADE REPORT - CORRECTED LOT SIZES (NIFTY=65, BANKNIFTY=30, SENSEX=20)")
    print("=" * 180)
    print(f"\n{'S.No':>5} {'Symbol':<22} {'Strategy':<12} {'Entry Time':<18} {'Exit Time':<18} "
          f"{'Lot':>4} {'Entry':>8} {'Target':>8} {'SL':>8} {'Exit':>8} "
          f"{'Invested':>12} {'After':>12} {'PnL':>10} {'Balance':>14} {'Status':<7} {'PnL%':>6} {'Delta':>7} {'Reason':<12}")
    print("-" * 180)

    for i, t in enumerate(all_trades, 1):
        entry_fmt = format_timestamp(t['entry_time'])
        exit_fmt = format_timestamp(t['exit_time']) if t['exit_time'] else '-- OPEN --'
        exit_val = f"{t['exit']:>8.2f}" if t['exit'] != '' else '       -'
        after_val = f"{t['amt_after']:>12.2f}" if t['amt_after'] != '' else '           -'

        print(f"{i:>5} {t['symbol']:<22} {t['strategy']:<12} {entry_fmt:<18} {exit_fmt:<18} "
              f"{t['per_lot']:>4} {t['entry']:>8.2f} {t['target']:>8.2f} {t['sl']:>8.2f} {exit_val} "
              f"{t['amt_invested']:>12.2f} {after_val} {t['pnl']:>+10.2f} {t['balance']:>14,.2f} {t['status']:<7} "
              f"{t['pnl_pct']:>+5.1f}% {t['delta']:>7.4f} {t['exit_reason']:<12}")

    # Summary
    print("\n" + "=" * 180)
    print(f"  SUMMARY")
    print(f"  Initial Capital:    Rs {INITIAL_CAPITAL:>14,.2f}")
    print(f"  Total Trades:       {len(all_trades)} ({closed_count} closed + {open_count} open)")
    print(f"  Wins / Losses:      {wins}W / {losses}L ({wr:.0f}% win rate)")
    print(f"  Realized PnL:       Rs {total_realized:>+14,.2f}")
    print(f"  Final Balance:      Rs {balance:>14,.2f}")
    print("=" * 180)

    # Save CSV — 20 columns matching user's desired format
    csv_path = os.path.join(REPORT_DIR, 'todays_trades_corrected.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'S.No', 'Symbol', 'Strategy', 'Entry Time', 'Exit Time',
            'Per lot', 'No fo lots', 'Entry', 'Target', 'SL', 'Exit',
            'Amount Invested', 'Amount After Close', 'PnL', 'Balance',
            'Status', 'PnL %', 'Spot Price', 'Delta', 'Exit Reason'
        ])
        for i, t in enumerate(all_trades, 1):
            entry_fmt = format_timestamp(t['entry_time'])
            exit_fmt = format_timestamp(t['exit_time']) if t['exit_time'] else '-- OPEN --'

            writer.writerow([
                i,
                t['symbol'],
                t['strategy'],
                entry_fmt,
                exit_fmt,
                t['per_lot'],
                t['no_of_lots'],
                t['entry'],
                t['target'],
                t['sl'],
                t['exit'] if t['exit'] != '' else '',
                t['amt_invested'],
                t['amt_after'] if t['amt_after'] != '' else '',
                t['pnl'],
                t['balance'],
                t['status'],
                t['pnl_pct'],
                t['spot_price'],
                t['delta'],
                t['exit_reason']
            ])
    print(f"\n  CSV saved: {csv_path}")

    # Also copy to Desktop for easy access
    desktop_path = os.path.join(os.path.expanduser('~'), 'Desktop', 'Stocks', 'todays_trades_corrected.csv')
    try:
        os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
        import shutil
        shutil.copy2(csv_path, desktop_path)
        print(f"  Also saved to: {desktop_path}")
    except Exception as e:
        print(f"  Desktop copy failed: {e}")

    # Send to Telegram
    try:
        from trade_notifier import send_message

        msg = (
            f"<b>TRADE REPORT (Corrected Lots)</b>\n"
            f"<b>N=65, BN=30, SX=20</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total Trades: {len(all_trades)}\n"
            f"Closed: {closed_count} | Open: {open_count}\n"
            f"Win Rate: {wr:.0f}% ({wins}W/{losses}L)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Realized PnL: Rs {total_realized:+,.0f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Initial: Rs {INITIAL_CAPITAL:,.0f}\n"
            f"<b>Current: Rs {balance:,.0f}</b>\n"
        )

        if closed_trades:
            best = max(closed_trades, key=lambda t: t['pnl'])
            worst = min(closed_trades, key=lambda t: t['pnl'])
            msg += f"\nBest: {best['symbol']} Rs{best['pnl']:+,.0f}\n"
            msg += f"Worst: {worst['symbol']} Rs{worst['pnl']:+,.0f}\n"

        if open_count > 0:
            msg += f"\n<b>OPEN ({open_count}):</b>\n"
            for t in all_trades:
                if t['status'] == 'Open':
                    msg += f"{t['symbol'][:18]} {t['strategy'][:8]}\n"

        send_message(msg)
        print("  Report sent to Telegram!")
    except Exception as e:
        print(f"  Telegram failed: {e}")


if __name__ == '__main__':
    main()
