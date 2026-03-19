#!/usr/bin/env python3
"""Full momentum reversal exit simulation across ALL strategies.

Tests 0.4%/20min exit rule on every strategy independently,
computes: PnL, WR, PF, Max DD, Sharpe, Avg Win/Loss, Expectancy.
"""
import re, json, math
from collections import defaultdict
from datetime import datetime

def parse_exit_checks(filepath):
    pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*'
        r'EXIT_CHECK:\s+(\S+)\s+\|'
        r'\s+Entry:\s+([\d.]+)\s+.*?Current:\s+([\d.]+).*?'
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
                    'spot_entry': float(m.group(5)), 'spot_now': float(m.group(6)),
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
                    'strategy': t.get('strategy', '?'),
                    'lot_size': t.get('lot_size', 1),
                    'multiplier': t.get('multiplier', 1),
                    'exit_reason': t.get('exit_reason', '?'),
                    'timestamp': t.get('timestamp', ''),
                }
        except Exception:
            pass
    return results

def compute_metrics(pnl_list, capital=300000):
    """Compute full trading metrics from a list of PnLs."""
    if not pnl_list:
        return {}

    total = sum(pnl_list)
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]

    # Max drawdown
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnl_list:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Profit factor
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 1
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

    # Sharpe (daily: assume ~5 trades/day over 7 days)
    if len(pnl_list) >= 2:
        std = max(0.01, float((sum((p - total/len(pnl_list))**2 for p in pnl_list) / len(pnl_list))**0.5))
        sharpe = (total / len(pnl_list)) / std * (252**0.5)
    else:
        sharpe = 0

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = total / len(pnl_list) if pnl_list else 0

    return {
        'trades': len(pnl_list),
        'wins': len(wins),
        'losses': len(losses),
        'wr': len(wins) / len(pnl_list) * 100 if pnl_list else 0,
        'pnl': total,
        'pf': pf,
        'max_dd': max_dd,
        'max_dd_pct': max_dd / capital * 100,
        'sharpe': sharpe,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'expectancy': expectancy,
        'gross_win': gross_win,
        'gross_loss': gross_loss,
    }

def simulate_exit(trades, actual, spot_pct=0.4, time_min=20):
    """Apply momentum exit and return per-trade simulated PnLs."""
    sim_pnls = {}  # tid -> simulated pnl
    triggers = {}  # tid -> trigger info

    for tid, checks in trades.items():
        if tid not in actual or len(checks) < 2:
            if tid in actual:
                sim_pnls[tid] = actual[tid]['pnl']
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
        triggered = False
        for i, c in enumerate(checks):
            elapsed = (c['time'] - entry_time).total_seconds() / 60
            if elapsed >= time_min:
                spot_move = (c['spot_now'] - spot_entry) / spot_entry * 100
                if (is_ce and spot_move < -spot_pct) or (not is_ce and spot_move > spot_pct):
                    # Exit at this point
                    prem_diff = c['current_premium'] - entry_prem
                    if 'SELL' in sig:
                        prem_diff = entry_prem - c['current_premium']
                    sim_pnl = prem_diff * lot * mult
                    sim_pnls[tid] = sim_pnl
                    triggers[tid] = {
                        'trigger_min': elapsed,
                        'spot_move': spot_move,
                        'actual_pnl': act['pnl'],
                        'sim_pnl': sim_pnl,
                    }
                    triggered = True
                    break

        if not triggered:
            sim_pnls[tid] = act['pnl']

    # Add trades without EXIT_CHECK data
    for tid, act in actual.items():
        if tid not in sim_pnls:
            sim_pnls[tid] = act['pnl']

    return sim_pnls, triggers

# Load data
trades = parse_exit_checks('D:/AlgoTrading/data/all_exit_checks.txt')
actual = get_results([
    'D:/AlgoTrading/temp_equity_state.json',
    'D:/AlgoTrading/temp_comm_state.json',
    'D:/AlgoTrading/temp_stock_state.json',
])

sim_pnls, triggers = simulate_exit(trades, actual)

# Group by strategy
strats = defaultdict(lambda: {'actual': [], 'sim': [], 'triggers': []})
for tid, act in actual.items():
    strat = act['strategy']
    strats[strat]['actual'].append(act['pnl'])
    strats[strat]['sim'].append(sim_pnls.get(tid, act['pnl']))
    if tid in triggers:
        strats[strat]['triggers'].append(triggers[tid])

# Also compute ALL combined
strats['ALL']['actual'] = [act['pnl'] for act in actual.values()]
strats['ALL']['sim'] = [sim_pnls.get(tid, act['pnl']) for tid, act in actual.items()]
strats['ALL']['triggers'] = list(triggers.values())

# Print comprehensive table
print("=" * 110)
print("  MOMENTUM REVERSAL EXIT (0.4% spot / 20min) - PER STRATEGY ANALYSIS")
print("=" * 110)

# Header
print(f"\n  {'Strategy':<18} | {'':^44} | {'':^44}")
print(f"  {'':18} | {'ACTUAL (no change)':^44} | {'WITH MOMENTUM EXIT':^44}")
print(f"  {'-'*18}-+-{'-'*44}-+-{'-'*44}")
print(f"  {'':18} | {'Trades':>6} {'WR':>6} {'PnL':>10} {'PF':>6} {'MaxDD':>8} {'DD%':>6} {'Sharpe':>7}"
      f" | {'Trades':>6} {'WR':>6} {'PnL':>10} {'PF':>6} {'MaxDD':>8} {'DD%':>6} {'Sharpe':>7}")
print(f"  {'-'*18}-+-{'-'*44}-+-{'-'*44}")

for strat in ['CPR', 'Gamma Blast', 'PCR+VWAP', 'ALL']:
    if strat not in strats:
        continue
    s = strats[strat]
    a = compute_metrics(s['actual'])
    m = compute_metrics(s['sim'])

    label = strat if strat != 'ALL' else 'ALL COMBINED'

    print(f"  {label:<18} | "
          f"{a['trades']:>6} {a['wr']:>5.0f}% {a['pnl']:>+9,.0f} {a['pf']:>5.2f} {a['max_dd']:>+7,.0f} {a['max_dd_pct']:>5.1f}% {a['sharpe']:>6.1f}"
          f" | "
          f"{m['trades']:>6} {m['wr']:>5.0f}% {m['pnl']:>+9,.0f} {m['pf']:>5.2f} {m['max_dd']:>+7,.0f} {m['max_dd_pct']:>5.1f}% {m['sharpe']:>6.1f}")

# Delta table
print(f"\n  {'IMPROVEMENT':18} | {'Trig':>6} {'dWR':>6} {'dPnL':>10} {'dPF':>6} {'dDD':>8} {'dDD%':>6} {'dSharpe':>7}")
print(f"  {'-'*18}-+-{'-'*44}")

for strat in ['CPR', 'Gamma Blast', 'PCR+VWAP', 'ALL']:
    if strat not in strats:
        continue
    s = strats[strat]
    a = compute_metrics(s['actual'])
    m = compute_metrics(s['sim'])
    n_trig = len(s['triggers'])
    label = strat if strat != 'ALL' else 'ALL COMBINED'

    print(f"  {label:<18} | "
          f"{n_trig:>6} {m['wr']-a['wr']:>+5.1f}% {m['pnl']-a['pnl']:>+9,.0f} {m['pf']-a['pf']:>+5.2f} "
          f"{a['max_dd']-m['max_dd']:>+7,.0f} {a['max_dd_pct']-m['max_dd_pct']:>+5.1f}% {m['sharpe']-a['sharpe']:>+6.1f}")

# Detailed avg win/loss
print(f"\n  RISK METRICS:")
print(f"  {'Strategy':<18} | {'':^22} ACTUAL {'':^22} | {'':^22} SIMULATED {'':^22}")
print(f"  {'':18} | {'AvgWin':>8} {'AvgLoss':>9} {'Expect':>9} {'W/L':>6} | {'AvgWin':>8} {'AvgLoss':>9} {'Expect':>9} {'W/L':>6}")
print(f"  {'-'*18}-+-{'-'*36}-+-{'-'*36}")

for strat in ['CPR', 'Gamma Blast', 'PCR+VWAP', 'ALL']:
    if strat not in strats:
        continue
    s = strats[strat]
    a = compute_metrics(s['actual'])
    m = compute_metrics(s['sim'])
    label = strat if strat != 'ALL' else 'ALL COMBINED'

    a_wl = abs(a['avg_win'] / a['avg_loss']) if a['avg_loss'] != 0 else 0
    m_wl = abs(m['avg_win'] / m['avg_loss']) if m['avg_loss'] != 0 else 0

    print(f"  {label:<18} | "
          f"{a['avg_win']:>+7,.0f} {a['avg_loss']:>+8,.0f} {a['expectancy']:>+8,.0f} {a_wl:>5.2f}"
          f" | "
          f"{m['avg_win']:>+7,.0f} {m['avg_loss']:>+8,.0f} {m['expectancy']:>+8,.0f} {m_wl:>5.2f}")

# Show triggered trades detail
print(f"\n  TRIGGERED TRADES (would exit early):")
print(f"  {'TID':<30} {'Strategy':<15} {'Symbol':<12} {'Actual':>9} {'Simulated':>9} {'Saved':>9} {'TrigMin':>7}")
print(f"  {'-'*30} {'-'*15} {'-'*12} {'-'*9} {'-'*9} {'-'*9} {'-'*7}")

for tid, trig in sorted(triggers.items(), key=lambda x: x[1]['actual_pnl'] - x[1]['sim_pnl']):
    act = actual[tid]
    saved = trig['sim_pnl'] - trig['actual_pnl']
    print(f"  {tid:<30} {act['strategy']:<15} {act['symbol']:<12} "
          f"{trig['actual_pnl']:>+8,.0f} {trig['sim_pnl']:>+8,.0f} {saved:>+8,.0f} {trig['trigger_min']:>5.0f}m")
