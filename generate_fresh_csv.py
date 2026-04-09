#!/usr/bin/env python3
"""Generate fresh CSV of today's (2026-02-23) trades only, omitting all old trades."""

import csv
import json
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

###############################################
# PART 1: EQUITY TRADES - Filter from existing CSV
###############################################
equity_rows = []
equity_headers = None
equity_csv = os.path.join(BASE_DIR, 'reports', 'equity_trades_20260223.csv')

with open(equity_csv) as f:
    reader = csv.DictReader(f)
    equity_headers = reader.fieldnames
    for row in reader:
        # Only keep trades ENTERED today (2026-02-23)
        if row['Entry_Time'].startswith('2026-02-23'):
            equity_rows.append(row)

print(f"=== EQUITY TRADES (Entered 2026-02-23) ===")
print(f"Total: {len(equity_rows)}")

eq_closed = [r for r in equity_rows if r['Status'] == 'CLOSED']
eq_open = [r for r in equity_rows if r['Status'] == 'OPEN']
print(f"  Closed: {len(eq_closed)}")
print(f"  Open: {len(eq_open)}")

###############################################
# PART 2: COMMODITY TRADES - Extract from portfolio JSON
###############################################
comm_json = os.path.join(BASE_DIR, 'paper_trades_commodity', 'commodity_portfolio_state.json')
with open(comm_json) as f:
    commodity_data = json.load(f)

commodity_rows = []
sno = 1

# Closed commodity trades entered today
for t in commodity_data.get('closed_trades', []):
    if t.get('timestamp', '').startswith('2026-02-23'):
        entry_prem = float(t.get('entry_premium', 0))
        lot = int(t.get('lot_size', 1))
        mult = int(t.get('multiplier', 1))
        row = {
            'S.No': sno,
            'Date': '2026-02-23',
            'Market': 'MCX',
            'Strategy': t.get('strategy', ''),
            'Symbol': t.get('commodity', ''),
            'Signal_Type': t.get('signal_type', ''),
            'Direction': 'SELL' if t.get('is_sell', False) else 'BUY',
            'Strike': t.get('strike', ''),
            'Lot_Size': lot,
            'Multiplier': mult,
            'Entry_Time': t.get('timestamp', ''),
            'Exit_Time': t.get('exit_time', ''),
            'Entry_Price': round(entry_prem, 2),
            'Exit_Price': round(float(t.get('exit_premium', 0)), 2),
            'Capital_Used': round(entry_prem * lot * mult, 2),
            'PnL': round(float(t.get('pnl', 0)), 2),
            'Exit_Reason': t.get('exit_reason', ''),
            'Delta': round(float(t.get('delta', 0)), 4),
            'IV': t.get('iv', ''),
            'Status': 'CLOSED'
        }
        commodity_rows.append(row)
        sno += 1

# Open commodity positions entered today
for p in commodity_data.get('positions', []):
    if p.get('timestamp', '').startswith('2026-02-23'):
        entry_prem = float(p.get('entry_premium', 0))
        curr_prem = float(p.get('current_premium', 0))
        lot = int(p.get('lot_size', 1))
        mult = int(p.get('multiplier', 1))

        if p.get('is_sell', False):
            unrealized = (entry_prem - curr_prem) * lot * mult
        else:
            unrealized = (curr_prem - entry_prem) * lot * mult

        row = {
            'S.No': sno,
            'Date': '2026-02-23',
            'Market': 'MCX',
            'Strategy': p.get('strategy', ''),
            'Symbol': p.get('commodity', ''),
            'Signal_Type': p.get('signal_type', ''),
            'Direction': 'SELL' if p.get('is_sell', False) else 'BUY',
            'Strike': p.get('strike', ''),
            'Lot_Size': lot,
            'Multiplier': mult,
            'Entry_Time': p.get('timestamp', ''),
            'Exit_Time': 'OPEN',
            'Entry_Price': round(entry_prem, 2),
            'Exit_Price': round(curr_prem, 2),
            'Capital_Used': round(entry_prem * lot * mult, 2),
            'PnL': round(unrealized, 2),
            'Exit_Reason': 'OPEN',
            'Delta': round(float(p.get('delta', 0)), 4),
            'IV': p.get('iv', ''),
            'Status': 'OPEN'
        }
        commodity_rows.append(row)
        sno += 1

comm_closed = [r for r in commodity_rows if r['Status'] == 'CLOSED']
comm_open = [r for r in commodity_rows if r['Status'] == 'OPEN']

print(f"\n=== COMMODITY TRADES (Entered 2026-02-23) ===")
print(f"Total: {len(commodity_rows)}")
print(f"  Closed: {len(comm_closed)}")
print(f"  Open: {len(comm_open)}")

###############################################
# PART 3: Write fresh CSVs
###############################################
os.makedirs(os.path.join(BASE_DIR, 'reports'), exist_ok=True)

# Write equity-only fresh CSV
eq_out = os.path.join(BASE_DIR, 'reports', 'fresh_equity_trades_20260223.csv')
with open(eq_out, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=equity_headers)
    writer.writeheader()
    for i, row in enumerate(equity_rows, 1):
        row['S.No'] = i
        writer.writerow(row)

print(f"\nWritten: {eq_out} ({len(equity_rows)} rows)")

# Write commodity fresh CSV
commodity_headers = ['S.No', 'Date', 'Market', 'Strategy', 'Symbol', 'Signal_Type', 'Direction',
                     'Strike', 'Lot_Size', 'Multiplier', 'Entry_Time', 'Exit_Time',
                     'Entry_Price', 'Exit_Price', 'Capital_Used', 'PnL', 'Exit_Reason',
                     'Delta', 'IV', 'Status']

comm_out = os.path.join(BASE_DIR, 'reports', 'fresh_commodity_trades_20260223.csv')
with open(comm_out, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=commodity_headers)
    writer.writeheader()
    for i, row in enumerate(commodity_rows, 1):
        row['S.No'] = i
        writer.writerow(row)

print(f"Written: {comm_out} ({len(commodity_rows)} rows)")

###############################################
# PART 4: SUMMARY ANALYSIS
###############################################
print("\n" + "=" * 60)
print("      TODAY'S FRESH TRADE SUMMARY (2026-02-23)")
print("=" * 60)

# EQUITY SUMMARY
print("\n--- EQUITY TRADES ---")
print(f"  Total trades entered today: {len(equity_rows)}")
print(f"  Closed: {len(eq_closed)}")
print(f"  Still Open: {len(eq_open)}")

if eq_closed:
    eq_winners = [r for r in eq_closed if float(r['PnL']) > 0]
    eq_losers = [r for r in eq_closed if float(r['PnL']) < 0]
    eq_total_pnl = sum(float(r['PnL']) for r in eq_closed)
    eq_win_pnl = sum(float(r['PnL']) for r in eq_winners)
    eq_loss_pnl = sum(float(r['PnL']) for r in eq_losers)

    print(f"  Winners: {len(eq_winners)} | Losers: {len(eq_losers)}")
    win_rate = len(eq_winners) / len(eq_closed) * 100 if eq_closed else 0
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Total Realized PnL: Rs {eq_total_pnl:,.2f}")
    print(f"    Winning PnL: Rs {eq_win_pnl:,.2f}")
    print(f"    Losing PnL:  Rs {eq_loss_pnl:,.2f}")

    best_eq = max(eq_closed, key=lambda r: float(r['PnL']))
    worst_eq = min(eq_closed, key=lambda r: float(r['PnL']))
    print(f"  Best Trade:  {best_eq['Strategy']} {best_eq['Symbol']} {best_eq['Signal_Type']} -> Rs {float(best_eq['PnL']):,.2f}")
    print(f"  Worst Trade: {worst_eq['Strategy']} {worst_eq['Symbol']} {worst_eq['Signal_Type']} -> Rs {float(worst_eq['PnL']):,.2f}")

    # Strategy breakdown
    print(f"\n  Strategy Breakdown:")
    strategies = {}
    for r in eq_closed:
        s = r['Strategy']
        if s not in strategies:
            strategies[s] = {'count': 0, 'wins': 0, 'pnl': 0}
        strategies[s]['count'] += 1
        strategies[s]['pnl'] += float(r['PnL'])
        if float(r['PnL']) > 0:
            strategies[s]['wins'] += 1

    for s, d in sorted(strategies.items()):
        wr = d['wins'] / d['count'] * 100 if d['count'] > 0 else 0
        print(f"    {s:15s}: {d['count']:3d} trades | Win: {wr:5.1f}% | PnL: Rs {d['pnl']:>12,.2f}")

    # Symbol breakdown
    print(f"\n  Symbol Breakdown:")
    symbols = {}
    for r in eq_closed:
        sym = r['Symbol']
        if sym not in symbols:
            symbols[sym] = {'count': 0, 'wins': 0, 'pnl': 0}
        symbols[sym]['count'] += 1
        symbols[sym]['pnl'] += float(r['PnL'])
        if float(r['PnL']) > 0:
            symbols[sym]['wins'] += 1

    for sym, d in sorted(symbols.items()):
        wr = d['wins'] / d['count'] * 100 if d['count'] > 0 else 0
        print(f"    {sym:15s}: {d['count']:3d} trades | Win: {wr:5.1f}% | PnL: Rs {d['pnl']:>12,.2f}")

# Open equity unrealized
if eq_open:
    eq_unrealized = sum(float(r['PnL']) for r in eq_open)
    print(f"\n  Open Positions Unrealized PnL: Rs {eq_unrealized:,.2f}")

# COMMODITY SUMMARY
print("\n--- COMMODITY TRADES ---")
print(f"  Total trades entered today: {len(commodity_rows)}")
print(f"  Closed: {len(comm_closed)}")
print(f"  Still Open: {len(comm_open)}")

if comm_closed:
    comm_winners = [r for r in comm_closed if float(r['PnL']) > 0]
    comm_losers = [r for r in comm_closed if float(r['PnL']) < 0]
    comm_total_pnl = sum(float(r['PnL']) for r in comm_closed)
    comm_win_pnl = sum(float(r['PnL']) for r in comm_winners)
    comm_loss_pnl = sum(float(r['PnL']) for r in comm_losers)

    print(f"  Winners: {len(comm_winners)} | Losers: {len(comm_losers)}")
    comm_wr = len(comm_winners) / len(comm_closed) * 100 if comm_closed else 0
    print(f"  Win Rate: {comm_wr:.1f}%")
    print(f"  Total Realized PnL: Rs {comm_total_pnl:,.2f}")
    print(f"    Winning PnL: Rs {comm_win_pnl:,.2f}")
    print(f"    Losing PnL:  Rs {comm_loss_pnl:,.2f}")

    best_comm = max(comm_closed, key=lambda r: float(r['PnL']))
    worst_comm = min(comm_closed, key=lambda r: float(r['PnL']))
    print(f"  Best Trade:  {best_comm['Strategy']} {best_comm['Symbol']} {best_comm['Signal_Type']} -> Rs {float(best_comm['PnL']):,.2f}")
    print(f"  Worst Trade: {worst_comm['Strategy']} {worst_comm['Symbol']} {worst_comm['Signal_Type']} -> Rs {float(worst_comm['PnL']):,.2f}")

    print(f"\n  Strategy x Symbol Breakdown:")
    strat_sym = {}
    for r in comm_closed:
        key = f"{r['Strategy']} ({r['Symbol']})"
        if key not in strat_sym:
            strat_sym[key] = {'count': 0, 'wins': 0, 'pnl': 0}
        strat_sym[key]['count'] += 1
        strat_sym[key]['pnl'] += float(r['PnL'])
        if float(r['PnL']) > 0:
            strat_sym[key]['wins'] += 1

    for s, d in sorted(strat_sym.items()):
        wr = d['wins'] / d['count'] * 100 if d['count'] > 0 else 0
        print(f"    {s:30s}: {d['count']:3d} trades | Win: {wr:5.1f}% | PnL: Rs {d['pnl']:>12,.2f}")

if comm_open:
    comm_unrealized = sum(float(r['PnL']) for r in comm_open)
    print(f"\n  Open Positions Unrealized PnL: Rs {comm_unrealized:,.2f}")

# LOSING TRADES DETAIL
print("\n" + "=" * 60)
print("  LOSING TRADES DETAIL")
print("=" * 60)

print("\n  Equity Losing Trades:")
if eq_closed:
    eq_losers_detail = [r for r in eq_closed if float(r['PnL']) < 0]
    if eq_losers_detail:
        for r in sorted(eq_losers_detail, key=lambda x: float(x['PnL'])):
            print(f"    {r['Strategy']:10s} {r['Symbol']:10s} {r['Signal_Type']:15s} Strike={r['Strike']:>8s} Entry={r['Entry_Price']:>8s} Exit={r['Exit_Price']:>8s} PnL=Rs {float(r['PnL']):>10,.2f} Reason={r['Exit_Reason']}")
    else:
        print("    None!")

print("\n  Commodity Losing Trades:")
if comm_closed:
    comm_losers_detail = [r for r in comm_closed if float(r['PnL']) < 0]
    if comm_losers_detail:
        for r in sorted(comm_losers_detail, key=lambda x: float(x['PnL'])):
            print(f"    {r['Strategy']:10s} {r['Symbol']:10s} {r['Signal_Type']:15s} Strike={str(r['Strike']):>8s} Entry={str(r['Entry_Price']):>10s} Exit={str(r['Exit_Price']):>10s} PnL=Rs {float(r['PnL']):>12,.2f} Reason={r['Exit_Reason']}")
    else:
        print("    None!")

# COMBINED SUMMARY
print("\n" + "=" * 60)
print("  COMBINED SUMMARY")
print("=" * 60)
total_eq_pnl = sum(float(r['PnL']) for r in eq_closed) if eq_closed else 0
total_comm_pnl = sum(float(r['PnL']) for r in comm_closed) if comm_closed else 0
eq_unreal = sum(float(r['PnL']) for r in eq_open) if eq_open else 0
comm_unreal = sum(float(r['PnL']) for r in comm_open) if comm_open else 0

print(f"  Equity Realized:     Rs {total_eq_pnl:>12,.2f}")
print(f"  Commodity Realized:  Rs {total_comm_pnl:>12,.2f}")
print(f"  Total Realized:      Rs {total_eq_pnl + total_comm_pnl:>12,.2f}")
print(f"  Equity Unrealized:   Rs {eq_unreal:>12,.2f}")
print(f"  Commodity Unrealized:Rs {comm_unreal:>12,.2f}")
print(f"  Total Unrealized:    Rs {eq_unreal + comm_unreal:>12,.2f}")
print(f"  --------------------------------")
print(f"  GRAND TOTAL:         Rs {total_eq_pnl + total_comm_pnl + eq_unreal + comm_unreal:>12,.2f}")

# Capital info
print(f"\n  Starting Capital: Rs 2,00,000 (Equity) + Rs 1,00,000 (Commodity) = Rs 3,00,000")
grand_realized = total_eq_pnl + total_comm_pnl
print(f"  Capital After Trades: Rs {300000 + grand_realized:,.2f}")
