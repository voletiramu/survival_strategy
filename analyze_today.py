#!/usr/bin/env python3
"""Analyze today's trades and recent performance."""
import json
from collections import defaultdict, Counter

# Load equity trades from backup (main was reset)
with open('/root/algo_trading/paper_trades/portfolio_state_backup_20260312_103722.json') as f:
    state = json.load(f)
closed = state.get('closed_positions', [])

# Today's trades
today = [t for t in closed if '2026-03-20' in str(t.get('entry_time',''))]
print('=== TODAY EQUITY TRADES (2026-03-20) ===')
for t in today:
    pnl = t.get('pnl', t.get('net_pnl', 0))
    entry_p = t.get('entry_premium', 0)
    exit_p = t.get('exit_premium', 0)
    pnl_pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
    sym = t.get('symbol', '?')
    strat = t.get('strategy', '?')
    sig = t.get('signal_type', '?')
    ex = t.get('exit_reason', '?')
    qs = t.get('quality_score', '?')
    dte = t.get('dte', '?')
    et = str(t.get('entry_time', ''))[-8:]
    xt = str(t.get('exit_time', ''))[-8:]
    dur = ''
    try:
        from datetime import datetime
        e = datetime.fromisoformat(str(t.get('entry_time','')))
        x = datetime.fromisoformat(str(t.get('exit_time','')))
        dur = f'{(x-e).total_seconds()/60:.0f}min'
    except: pass
    print(f'  {sym:12s} {strat:20s} {sig:15s} E={entry_p:>7.1f} X={exit_p:>7.1f} {pnl_pct:>+6.1f}% Rs{pnl:>7,.0f} [{ex}] Q={qs} DTE={dte} {et}->{xt} {dur}')

if today:
    total_pnl = sum(t.get('pnl', t.get('net_pnl', 0)) for t in today)
    wins = sum(1 for t in today if t.get('pnl', t.get('net_pnl', 0)) > 0)
    print(f'\n  Today: {len(today)} trades, {wins} wins ({wins/len(today)*100:.0f}% WR), PnL=Rs {total_pnl:,.0f}')

# Last 7 days
print('\n=== LAST 7 DAYS ===')
daily = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0, 'gross_w': 0, 'gross_l': 0})
for t in closed:
    date = str(t.get('entry_time',''))[:10]
    pnl = t.get('pnl', t.get('net_pnl', 0))
    daily[date]['trades'] += 1
    daily[date]['pnl'] += pnl
    if pnl > 0:
        daily[date]['wins'] += 1
        daily[date]['gross_w'] += pnl
    else:
        daily[date]['gross_l'] += abs(pnl)
for d in sorted(daily.keys())[-7:]:
    v = daily[d]
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    pf = v['gross_w']/v['gross_l'] if v['gross_l'] > 0 else 99
    print(f'  {d}: {v["trades"]:2d} trades, WR={wr:4.0f}%, PF={pf:4.2f}, PnL=Rs {v["pnl"]:>8,.0f}')

# All-time
total = len(closed)
total_wins = sum(1 for t in closed if t.get('pnl', t.get('net_pnl', 0)) > 0)
total_pnl_all = sum(t.get('pnl', t.get('net_pnl', 0)) for t in closed)
print(f'\n  ALL TIME: {total} trades, WR={total_wins/total*100:.1f}%, PnL=Rs {total_pnl_all:,.0f}')

# Exit reason analysis
print('\n=== EXIT REASON ANALYSIS ===')
exit_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    ex = t.get('exit_reason', '?')
    pnl = t.get('pnl', t.get('net_pnl', 0))
    exit_stats[ex]['count'] += 1
    exit_stats[ex]['pnl'] += pnl
    if pnl > 0: exit_stats[ex]['wins'] += 1
for ex, v in sorted(exit_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr = v['wins']/v['count']*100 if v['count'] > 0 else 0
    print(f'  {ex:30s}: {v["count"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Winning trades analysis - what makes them different?
print('\n=== WINNING vs LOSING TRADE CHARACTERISTICS ===')
win_trades = [t for t in closed if t.get('pnl', t.get('net_pnl', 0)) > 0]
loss_trades = [t for t in closed if t.get('pnl', t.get('net_pnl', 0)) <= 0]

def avg_field(trades, field, default=0):
    vals = [t.get(field, default) for t in trades if t.get(field, default) is not None]
    return sum(vals)/len(vals) if vals else 0

print(f'  {"Metric":25s} {"Winners":>10s} {"Losers":>10s}')
print(f'  {"-"*50}')
for field in ['quality_score', 'entry_premium', 'dte', 'delta', 'iv', 'theta', 'vega', 'gamma', 'pcr', 'volume']:
    w = avg_field(win_trades, field)
    l = avg_field(loss_trades, field)
    print(f'  {field:25s} {w:10.2f} {l:10.2f}')

# Quality score distribution
print('\n=== QUALITY SCORE vs WIN RATE ===')
qs_buckets = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    qs = t.get('quality_score', 0) or 0
    bucket = int(qs // 10) * 10
    pnl = t.get('pnl', t.get('net_pnl', 0))
    qs_buckets[bucket]['trades'] += 1
    qs_buckets[bucket]['pnl'] += pnl
    if pnl > 0: qs_buckets[bucket]['wins'] += 1
for b in sorted(qs_buckets.keys()):
    v = qs_buckets[b]
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    print(f'  QS {b:3d}-{b+9}: {v["trades"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Strategy distribution
print('\n=== STRATEGY vs WIN RATE ===')
strat_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    s = t.get('strategy', '?')
    pnl = t.get('pnl', t.get('net_pnl', 0))
    strat_stats[s]['trades'] += 1
    strat_stats[s]['pnl'] += pnl
    if pnl > 0: strat_stats[s]['wins'] += 1
for s, v in sorted(strat_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    print(f'  {s:25s}: {v["trades"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Symbol distribution
print('\n=== SYMBOL vs WIN RATE ===')
sym_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    s = t.get('symbol', '?')
    pnl = t.get('pnl', t.get('net_pnl', 0))
    sym_stats[s]['trades'] += 1
    sym_stats[s]['pnl'] += pnl
    if pnl > 0: sym_stats[s]['wins'] += 1
for s, v in sorted(sym_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    print(f'  {s:15s}: {v["trades"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Hour analysis
print('\n=== ENTRY HOUR vs WIN RATE ===')
hour_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    try:
        h = int(str(t.get('entry_time', ''))[-8:-6])
        pnl = t.get('pnl', t.get('net_pnl', 0))
        hour_stats[h]['trades'] += 1
        hour_stats[h]['pnl'] += pnl
        if pnl > 0: hour_stats[h]['wins'] += 1
    except: pass
for h in sorted(hour_stats.keys()):
    v = hour_stats[h]
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    print(f'  {h:02d}:00: {v["trades"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Duration analysis
print('\n=== TRADE DURATION vs WIN RATE ===')
from datetime import datetime
dur_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    try:
        e = datetime.fromisoformat(str(t.get('entry_time','')))
        x = datetime.fromisoformat(str(t.get('exit_time','')))
        mins = (x - e).total_seconds() / 60
        if mins < 5: bucket = '0-5min'
        elif mins < 15: bucket = '5-15min'
        elif mins < 30: bucket = '15-30min'
        elif mins < 60: bucket = '30-60min'
        elif mins < 120: bucket = '1-2hr'
        else: bucket = '2hr+'
        pnl = t.get('pnl', t.get('net_pnl', 0))
        dur_stats[bucket]['trades'] += 1
        dur_stats[bucket]['pnl'] += pnl
        if pnl > 0: dur_stats[bucket]['wins'] += 1
    except: pass
for b in ['0-5min', '5-15min', '15-30min', '30-60min', '1-2hr', '2hr+']:
    if b in dur_stats:
        v = dur_stats[b]
        wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
        print(f'  {b:10s}: {v["trades"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Also load commodity trades
print('\n\n=== COMMODITY TRADES ===')
try:
    with open('/root/algo_trading/paper_trades_commodity/commodity_portfolio_state.json') as f:
        cstate = json.load(f)
    cclosed = cstate.get('closed_positions', [])
    ctoday = [t for t in cclosed if '2026-03-20' in str(t.get('entry_time',''))]
    print(f'Today: {len(ctoday)} commodity trades')
    for t in ctoday:
        pnl = t.get('pnl', t.get('net_pnl', 0))
        entry_p = t.get('entry_premium', 0)
        exit_p = t.get('exit_premium', 0)
        pnl_pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
        sym = t.get('commodity', t.get('symbol', '?'))
        ex = t.get('exit_reason', '?')
        print(f'  {sym:12s} E={entry_p:>8.1f} X={exit_p:>8.1f} {pnl_pct:>+6.1f}% Rs{pnl:>7,.0f} [{ex}]')
    ctotal = sum(t.get('pnl', t.get('net_pnl', 0)) for t in ctoday)
    cwins = sum(1 for t in ctoday if t.get('pnl', t.get('net_pnl', 0)) > 0)
    if ctoday:
        print(f'  Commodity today: {len(ctoday)} trades, {cwins} wins, PnL=Rs {ctotal:,.0f}')
except Exception as e:
    print(f'  Error: {e}')
