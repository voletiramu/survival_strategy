#!/usr/bin/env python3
"""Simulate EXIT ONLY vs EXIT+REVERSE for momentum reversal trades."""
import re, json, sys
from collections import defaultdict
from datetime import datetime

SPOT_PCT = 0.4
TIME_MIN = 20

def parse_exit_checks(filepath):
    pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*'
        r'EXIT_CHECK:\s+(\S+)\s+\|'
        r'\s+Entry:\s+([\d.]+)\s+.*?Current:\s+([\d.]+).*?'
        r'Target:\s+([\d.]+)\s+SL:\s+([\d.]+).*?'
        r'Spot:\s+([\d.]+).*?([\d.]+)\s+'
    )
    trades = defaultdict(list)
    with open(filepath, 'r', errors='ignore') as f:
        for line in f:
            m = pattern.search(line)
            if m:
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                trades[m.group(2)].append({
                    'time': ts, 'entry_premium': float(m.group(3)),
                    'current_premium': float(m.group(4)),
                    'spot_entry': float(m.group(7)), 'spot_now': float(m.group(8)),
                })
    return trades

def get_results(files):
    results = {}
    for path in files:
        try:
            d = json.load(open(path))
            for t in d.get('closed_trades', []):
                results[t.get('id', '')] = {
                    'pnl': t.get('pnl', 0),
                    'symbol': t.get('symbol', t.get('commodity', '?')),
                    'signal_type': t.get('signal_type', '?'),
                    'lot_size': t.get('lot_size', 1),
                    'multiplier': t.get('multiplier', 1),
                    'exit_reason': t.get('exit_reason', '?'),
                }
        except Exception:
            pass
    return results

trades = parse_exit_checks('D:/AlgoTrading/data/all_exit_checks.txt')
actual = get_results([
    'D:/AlgoTrading/temp_equity_state.json',
    'D:/AlgoTrading/temp_comm_state.json',
    'D:/AlgoTrading/temp_stock_state.json',
])

actual_total = sum(r['pnl'] for r in actual.values())
actual_wins = sum(1 for r in actual.values() if r['pnl'] > 0)
N = len(actual)

print("=" * 85)
print(f"  EXIT ONLY vs EXIT+REVERSE SIMULATION")
print(f"  Rule: If spot moves {SPOT_PCT}% against direction after {TIME_MIN}min, take action")
print("=" * 85)
print(f"\n  ACTUAL: {N} trades, WR={actual_wins}/{N} ({actual_wins/N*100:.0f}%), PnL=Rs {actual_total:,.0f}")

rows = []
exit_total = 0
rev_total = 0
exit_wins = 0
rev_wins = 0
count = 0

for tid, checks in trades.items():
    if tid not in actual or len(checks) < 3:
        continue
    act = actual[tid]
    entry_prem = checks[0]['entry_premium']
    spot_entry = checks[0]['spot_entry']
    lot = act['lot_size']
    mult = act['multiplier']
    sig = act['signal_type']
    is_ce = 'CE' in sig
    entry_time = checks[0]['time']

    # Find trigger
    trigger_idx = None
    for i, c in enumerate(checks):
        elapsed = (c['time'] - entry_time).total_seconds() / 60
        if elapsed >= TIME_MIN:
            spot_move = (c['spot_now'] - spot_entry) / spot_entry * 100
            if (is_ce and spot_move < -SPOT_PCT) or (not is_ce and spot_move > SPOT_PCT):
                trigger_idx = i
                break

    if trigger_idx is None:
        # Not triggered
        exit_total += act['pnl']
        rev_total += act['pnl']
        if act['pnl'] > 0:
            exit_wins += 1
            rev_wins += 1
        count += 1
        continue

    # EXIT PnL
    tc = checks[trigger_idx]
    prem_diff = tc['current_premium'] - entry_prem
    if 'SELL' in sig:
        prem_diff = entry_prem - tc['current_premium']
    exit_pnl = prem_diff * lot * mult

    exit_total += exit_pnl
    if exit_pnl > 0:
        exit_wins += 1

    # REVERSE: track what happens to spot AFTER trigger
    remaining = checks[trigger_idx:]
    trigger_spot = tc['spot_now']
    trigger_prem = tc['current_premium']
    trigger_time = tc['time']

    # For reverse trade: opposite direction
    # If original CE failed (spot went down), reverse = PE (profit when spot goes down further)
    # Simple delta model: reverse PnL = spot_change * delta * lot * mult / spot * entry_prem
    # Or simpler: use actual premium data from remaining checks

    best_rev = 0
    rev_pnls = []
    for rc in remaining[1:]:
        # Spot change from trigger point
        dspot = rc['spot_now'] - trigger_spot
        # If we reversed (opposite direction):
        # CE failed -> now PE: spot going DOWN = profit
        # PE failed -> now CE: spot going UP = profit
        if is_ce:
            rev_spot_gain = -dspot  # PE profits when spot drops
        else:
            rev_spot_gain = dspot   # CE profits when spot rises

        # Approximate premium change: delta * spot_change
        # Delta ~0.5 for ATM, but premium moves faster near ATM
        delta_approx = 0.5
        rev_prem_change = rev_spot_gain * delta_approx
        rev_pnl_now = rev_prem_change * lot * mult
        rev_pnls.append(rev_pnl_now)
        if rev_pnl_now > best_rev:
            best_rev = rev_pnl_now

    # Exit reverse: use trailing SL at 70% of peak, or last point
    final_rev = 0
    if rev_pnls:
        peak = max(rev_pnls)
        if peak > 0:
            # Trailing SL: exit at 70% of peak
            tsl = peak * 0.7
            for rp in rev_pnls:
                if rp >= peak:
                    continue
                if rp < tsl:
                    final_rev = tsl
                    break
            else:
                final_rev = rev_pnls[-1]
        else:
            # Reverse also losing - SL at 50% of trigger premium
            rev_sl = -trigger_prem * 0.3 * lot * mult
            final_rev = max(rev_pnls[-1], rev_sl)

    total_with_rev = exit_pnl + final_rev
    rev_total += total_with_rev
    if total_with_rev > 0:
        rev_wins += 1
    count += 1

    trigger_mins = (trigger_time - entry_time).total_seconds() / 60
    rev_hold = (remaining[-1]['time'] - trigger_time).total_seconds() / 60 if len(remaining) > 1 else 0

    rows.append({
        'sym': act['symbol'], 'sig': sig,
        'actual': act['pnl'], 'exit_only': exit_pnl,
        'rev_pnl': final_rev, 'best_rev': best_rev,
        'total_rev': total_with_rev,
        'trigger_min': trigger_mins, 'rev_hold': rev_hold,
        'exit_reason': act['exit_reason'],
    })

# Results
print(f"\n  Triggered: {len(rows)}/{count} trades")
print(f"\n  {'Scenario':<28} {'PnL':>12} {'vs Actual':>12} {'WR':>8}")
print(f"  {'-'*28} {'-'*12} {'-'*12} {'-'*8}")
print(f"  {'ACTUAL (no change)':<28} Rs {actual_total:>9,.0f} {'--':>12} {actual_wins/N*100:>5.0f}%")
print(f"  {'EXIT ONLY (0.4%/20m)':<28} Rs {exit_total:>9,.0f} Rs{exit_total-actual_total:>+8,.0f} {exit_wins/count*100:>5.0f}%")
print(f"  {'EXIT + REVERSE':<28} Rs {rev_total:>9,.0f} Rs{rev_total-actual_total:>+8,.0f} {rev_wins/count*100:>5.0f}%")

print(f"\n  TRIGGERED TRADE DETAILS:")
print(f"  {'Symbol':<12} {'Signal':<16} {'Actual':>9} {'Exit':>9} {'Reverse':>9} {'Total':>9} {'Impact':>9} {'TrigMin':>7} {'RevHold':>7}")
print(f"  {'-'*12} {'-'*16} {'-'*9} {'-'*9} {'-'*9} {'-'*9} {'-'*9} {'-'*7} {'-'*7}")

for r in sorted(rows, key=lambda x: x['total_rev'] - x['actual'], reverse=True):
    impact = r['total_rev'] - r['actual']
    print(f"  {r['sym']:<12} {r['sig']:<16} {r['actual']:>+8,.0f} {r['exit_only']:>+8,.0f} "
          f"{r['rev_pnl']:>+8,.0f} {r['total_rev']:>+8,.0f} {impact:>+8,.0f} "
          f"{r['trigger_min']:>5.0f}m {r['rev_hold']:>5.0f}m")

print(f"\n  NET IMPACT:")
exit_imp = exit_total - actual_total
rev_imp = rev_total - actual_total
print(f"  Exit Only improvement:     Rs {exit_imp:>+8,.0f}")
print(f"  Exit+Reverse improvement:  Rs {rev_imp:>+8,.0f}")
print(f"  Reverse adds on top:       Rs {rev_imp - exit_imp:>+8,.0f}")
