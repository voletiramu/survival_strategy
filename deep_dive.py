#!/usr/bin/env python3
"""Deep dive analysis: actual bot trades + signal data patterns."""
import json, re, glob
from collections import defaultdict
from datetime import datetime

# === PART 1: Analyze actual bot trades from .bak file ===
print("=" * 90)
print("PART 1: ACTUAL BOT TRADE ANALYSIS (31 closed trades)")
print("=" * 90)

with open('/root/algo_trading/paper_trades/portfolio_state.json.bak_20260311_083043') as f:
    state = json.load(f)
closed = state.get('closed_trades', [])

print(f'\nTotal closed trades: {len(closed)}')

# Daily PnL
daily = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0, 'gw': 0, 'gl': 0})
for t in closed:
    date = str(t.get('entry_time', t.get('timestamp', '')))[:10]
    pnl = t.get('pnl', t.get('net_pnl', 0))
    daily[date]['trades'] += 1
    daily[date]['pnl'] += pnl
    if pnl > 0:
        daily[date]['wins'] += 1
        daily[date]['gw'] += pnl
    else:
        daily[date]['gl'] += abs(pnl)

print('\n--- Daily Performance ---')
for d in sorted(daily.keys()):
    v = daily[d]
    wr = v['wins']/v['trades']*100 if v['trades'] > 0 else 0
    pf = v['gw']/v['gl'] if v['gl'] > 0 else 99
    print(f'  {d}: {v["trades"]:2d} trades, WR={wr:4.0f}%, PF={pf:5.2f}, PnL=Rs {v["pnl"]:>8,.0f}')

total = len(closed)
wins = sum(1 for t in closed if t.get('pnl', 0) > 0)
total_pnl = sum(t.get('pnl', 0) for t in closed)
gw = sum(t.get('pnl', 0) for t in closed if t.get('pnl', 0) > 0)
gl = abs(sum(t.get('pnl', 0) for t in closed if t.get('pnl', 0) <= 0))
pf = gw / gl if gl > 0 else 0
print(f'\nALL TIME: {total} trades, WR={wins/total*100:.1f}%, PF={pf:.2f}, PnL=Rs {total_pnl:,.0f}')
print(f'Avg Win: Rs {gw/wins:,.0f}, Avg Loss: Rs {-gl/(total-wins):,.0f}' if wins > 0 and total-wins > 0 else '')

# Exit reasons
print('\n--- Exit Reasons ---')
exit_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'pnl': 0})
for t in closed:
    ex = t.get('exit_reason', '?')
    pnl = t.get('pnl', 0)
    exit_stats[ex]['count'] += 1
    exit_stats[ex]['pnl'] += pnl
    if pnl > 0: exit_stats[ex]['wins'] += 1
for ex, v in sorted(exit_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr = v['wins']/v['count']*100 if v['count'] > 0 else 0
    print(f'  {ex:30s}: {v["count"]:3d} trades, WR={wr:4.0f}%, PnL=Rs {v["pnl"]:>8,.0f}')

# Winners vs Losers
print('\n--- Winners vs Losers Characteristics ---')
win_trades = [t for t in closed if t.get('pnl', 0) > 0]
loss_trades = [t for t in closed if t.get('pnl', 0) <= 0]

def avg(trades, field):
    vals = [t.get(field, 0) for t in trades if t.get(field) is not None]
    vals = [v for v in vals if v != 0]
    return sum(vals)/len(vals) if vals else 0

print(f'  {"Metric":25s} {"Winners":>10s} {"Losers":>10s} {"Signal":>8s}')
for field in ['quality_score', 'entry_premium', 'dte', 'delta', 'iv', 'theta', 'vega', 'gamma']:
    w = avg(win_trades, field)
    l = avg(loss_trades, field)
    diff_pct = abs(w - l) / abs(max(abs(w), abs(l), 0.01)) * 100
    sig = 'DIFF!' if diff_pct > 25 else ''
    print(f'  {field:25s} {w:10.2f} {l:10.2f} {sig:>8s}')

# All trades detail
print('\n--- All Trades Detail ---')
for t in sorted(closed, key=lambda x: x.get('pnl', 0), reverse=True):
    pnl = t.get('pnl', 0)
    sym = t.get('symbol', '?')
    strat = t.get('strategy', '?')
    sig = t.get('signal_type', '?')
    ex = t.get('exit_reason', '?')
    qs = t.get('quality_score', 0)
    ep = t.get('entry_premium', 0)
    xp = t.get('exit_premium', 0)
    dte = t.get('dte', 0)
    marker = 'W' if pnl > 0 else 'L'
    print(f'  [{marker}] {sym:12s} {strat:8s} {sig:15s} E={ep:>7.1f} X={xp:>7.1f} Rs{pnl:>7,.0f} Q={qs:>3} DTE={dte} [{ex}]')

# === PART 2: Parse recent logs for trades ===
print('\n' + '=' * 90)
print('PART 2: RECENT LOG ANALYSIS (last 5 days)')
print('=' * 90)

for logfile in sorted(glob.glob('/root/algo_trading/logs/unified_paper_*.log'))[-5:]:
    date = logfile.split('_')[-1].replace('.log', '')
    entries = []
    exits = []
    with open(logfile) as f:
        for line in f:
            if 'TRADE ENTRY' in line or 'OPENED' in line or 'ENTRY EXECUTED' in line:
                entries.append(line.strip()[:200])
            elif 'TRADE EXIT' in line or 'CLOSED' in line or 'EXIT' in line.upper() and 'PNL' in line.upper():
                exits.append(line.strip()[:200])
    print(f'\n  {date}: {len(entries)} entries, {len(exits)} exits')
    for e in entries[:5]:
        print(f'    ENTRY: {e[:180]}')
    for e in exits[:5]:
        print(f'    EXIT:  {e[:180]}')

# === PART 3: Signal quality deep dive ===
print('\n' + '=' * 90)
print('PART 3: SIGNAL QUALITY DEEP DIVE (what predicts success?)')
print('=' * 90)

import pandas as pd
import numpy as np

# Load all signals
dfs = []
for f in sorted(glob.glob('/root/algo_trading/paper_trades/signals_*.csv')):
    try: d = pd.read_csv(f); d['asset_class'] = 'equity'; dfs.append(d)
    except: pass
for f in sorted(glob.glob('/root/algo_trading/paper_trades_commodity/commodity_signals_*.csv')):
    try:
        d = pd.read_csv(f); d['asset_class'] = 'commodity'
        if 'symbol' not in d.columns: d['symbol'] = d.get('commodity', 'UNK')
        dfs.append(d)
    except: pass

df = pd.concat(dfs, ignore_index=True)
df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
df = df.dropna(subset=['timestamp', 'symbol', 'premium', 'strike', 'type'])
df['premium'] = pd.to_numeric(df['premium'], errors='coerce')
df = df[df['premium'] > 0].copy()
df['date'] = df['timestamp'].dt.date
df['hour'] = df['timestamp'].dt.hour
df['quality_score'] = pd.to_numeric(df.get('quality_score', pd.Series(dtype=float)), errors='coerce').fillna(0)

# Build contract-level data: entry premium and subsequent premium trajectory
df['contract'] = df['date'].astype(str) + '|' + df['symbol'] + '|' + df['strike'].astype(str) + '|' + df['type']
contracts = df.groupby('contract')

print(f'Total signals: {len(df)}, Unique contracts: {len(contracts)}')

# For each contract: compute entry, max gain within 30/60 min
entries_data = []
for cname, group in contracts:
    group = group.sort_values('timestamp')
    if len(group) < 5:
        continue
    entry = group.iloc[0]
    entry_p = entry['premium']
    entry_t = entry['timestamp']

    for window in [30, 60]:
        cutoff = entry_t + pd.Timedelta(minutes=window)
        future = group[(group['timestamp'] > entry_t) & (group['timestamp'] <= cutoff)]
        if future.empty:
            continue
        prems = future['premium'].values
        max_p = prems.max()
        min_p = prems.min()
        last_p = prems[-1]
        max_gain = (max_p - entry_p) / entry_p * 100
        max_loss = (min_p - entry_p) / entry_p * 100
        final_change = (last_p - entry_p) / entry_p * 100

        entries_data.append({
            'symbol': entry['symbol'],
            'type': entry['type'],
            'strategy': entry.get('strategy', ''),
            'quality_score': entry.get('quality_score', 0),
            'premium': entry_p,
            'hour': entry['hour'],
            'dte': entry.get('dte', 0),
            'delta': entry.get('delta', 0),
            'iv': entry.get('iv', 0),
            'theta': entry.get('theta', 0),
            'gamma': entry.get('gamma', 0),
            'vega': entry.get('vega', 0),
            'pcr': entry.get('pcr', 0),
            'volume': entry.get('volume', 0),
            'oi': entry.get('oi', 0),
            'asset_class': entry.get('asset_class', ''),
            'window': window,
            'max_gain': max_gain,
            'max_loss': max_loss,
            'final_change': final_change,
            'profitable': 1 if final_change > 1.2 else 0,  # > cost
        })

edf = pd.DataFrame(entries_data)
print(f'Entries analyzed: {len(edf)}')

# Split by window
for win in [30, 60]:
    wdf = edf[edf['window'] == win]
    print(f'\n--- {win}min Window ---')
    print(f'Overall: {len(wdf)} entries, {wdf["profitable"].mean()*100:.1f}% profitable (beat 1.2% cost)')

    # Feature importance: which features predict success?
    print(f'\n  Feature correlations with profitability:')
    features = ['quality_score', 'premium', 'hour', 'delta', 'iv', 'theta', 'gamma', 'vega']
    for feat in features:
        if feat in wdf.columns:
            vals = pd.to_numeric(wdf[feat], errors='coerce')
            valid = vals.notna() & wdf['profitable'].notna()
            if valid.sum() > 10:
                corr = vals[valid].corr(wdf['profitable'][valid])
                print(f'    {feat:20s}: r={corr:+.3f}')

    # Quality score buckets
    print(f'\n  Quality Score vs Success ({win}min):')
    wdf_c = wdf.copy()
    wdf_c['qs_bucket'] = (wdf_c['quality_score'] // 10 * 10).astype(int)
    for qs, grp in wdf_c.groupby('qs_bucket'):
        prof_rate = grp['profitable'].mean() * 100
        avg_gain = grp['max_gain'].mean()
        avg_loss = grp['max_loss'].mean()
        print(f'    QS {qs:3d}-{qs+9}: {len(grp):4d} entries, {prof_rate:5.1f}% profitable, avg max_gain={avg_gain:+.1f}%, avg max_loss={avg_loss:+.1f}%')

    # Hour buckets
    print(f'\n  Entry Hour vs Success ({win}min):')
    for h, grp in wdf.groupby('hour'):
        prof_rate = grp['profitable'].mean() * 100
        avg_gain = grp['max_gain'].mean()
        print(f'    {h:02d}:00: {len(grp):4d} entries, {prof_rate:5.1f}% profitable, avg max_gain={avg_gain:+.1f}%')

    # Symbol
    print(f'\n  Symbol vs Success ({win}min):')
    for sym, grp in wdf.groupby('symbol'):
        prof_rate = grp['profitable'].mean() * 100
        avg_gain = grp['max_gain'].mean()
        avg_loss = grp['max_loss'].mean()
        print(f'    {sym:15s}: {len(grp):4d} entries, {prof_rate:5.1f}% profitable, gain={avg_gain:+.1f}%, loss={avg_loss:+.1f}%')

    # Signal type (BUY_CE vs BUY_PE)
    print(f'\n  Signal Type vs Success ({win}min):')
    for st, grp in wdf.groupby('type'):
        prof_rate = grp['profitable'].mean() * 100
        print(f'    {st:25s}: {len(grp):4d} entries, {prof_rate:5.1f}% profitable')

    # Premium range
    print(f'\n  Premium Range vs Success ({win}min):')
    wdf_c['prem_bucket'] = pd.cut(wdf_c['premium'], bins=[0, 50, 100, 200, 500, 1000, 50000], labels=['<50', '50-100', '100-200', '200-500', '500-1K', '1K+'])
    for pb, grp in wdf_c.groupby('prem_bucket'):
        if len(grp) > 3:
            prof_rate = grp['profitable'].mean() * 100
            avg_gain = grp['max_gain'].mean()
            print(f'    {str(pb):10s}: {len(grp):4d} entries, {prof_rate:5.1f}% profitable, gain={avg_gain:+.1f}%')

    # IV range
    print(f'\n  IV Range vs Success ({win}min):')
    iv_vals = pd.to_numeric(wdf['iv'], errors='coerce')
    valid_iv = iv_vals.notna() & (iv_vals > 0)
    if valid_iv.sum() > 20:
        wdf_iv = wdf[valid_iv].copy()
        wdf_iv['iv_num'] = iv_vals[valid_iv]
        wdf_iv['iv_bucket'] = pd.cut(wdf_iv['iv_num'], bins=[0, 15, 25, 35, 50, 100, 500], labels=['<15', '15-25', '25-35', '35-50', '50-100', '100+'])
        for ib, grp in wdf_iv.groupby('iv_bucket'):
            if len(grp) > 3:
                prof_rate = grp['profitable'].mean() * 100
                print(f'    IV {str(ib):8s}: {len(grp):4d} entries, {prof_rate:5.1f}% profitable')

# === FIND THE MAGIC FILTER ===
print('\n' + '=' * 90)
print('PART 4: FINDING THE MAGIC FILTER COMBINATION')
print('=' * 90)

wdf60 = edf[edf['window'] == 60].copy()
print(f'Working with {len(wdf60)} entries (60min window)')

# Test every combination of filters to find highest profitable%
best = []
for sym_filter in [None, 'NIFTY', 'BANKNIFTY', 'SENSEX', 'GOLDM', 'CRUDEOILM']:
    for qs_min in [0, 50, 60, 70, 80, 90]:
        for hour_filter in [None, 'morning', 'afternoon']:
            for prem_filter in [None, 'low', 'high']:
                mask = pd.Series(True, index=wdf60.index)
                label = []

                if sym_filter:
                    mask &= wdf60['symbol'] == sym_filter
                    label.append(sym_filter)
                if qs_min > 0:
                    mask &= wdf60['quality_score'] >= qs_min
                    label.append(f'Q{qs_min}+')
                if hour_filter == 'morning':
                    mask &= wdf60['hour'] < 11
                    label.append('AM')
                elif hour_filter == 'afternoon':
                    mask &= (wdf60['hour'] >= 11) & (wdf60['hour'] < 14)
                    label.append('PM')
                if prem_filter == 'low':
                    med = wdf60.groupby('symbol')['premium'].transform('median')
                    mask &= wdf60['premium'] < med
                    label.append('LowP')
                elif prem_filter == 'high':
                    med = wdf60.groupby('symbol')['premium'].transform('median')
                    mask &= wdf60['premium'] >= med
                    label.append('HighP')

                subset = wdf60[mask]
                if len(subset) < 10:
                    continue

                prof_rate = subset['profitable'].mean() * 100
                avg_max_gain = subset['max_gain'].mean()
                avg_max_loss = subset['max_loss'].mean()
                # Simulate: if we entered all these, what's the edge?
                edge = avg_max_gain + avg_max_loss  # positive = upside bias
                name = ' + '.join(label) if label else 'All'
                best.append({
                    'name': name, 'n': len(subset),
                    'prof_rate': prof_rate,
                    'avg_gain': avg_max_gain,
                    'avg_loss': avg_max_loss,
                    'edge': edge,
                })

best_df = pd.DataFrame(best)
print(f'\nTested {len(best_df)} filter combinations')

print('\n--- TOP 20 BY PROFITABILITY RATE ---')
top_prof = best_df.sort_values('prof_rate', ascending=False).head(20)
for _, r in top_prof.iterrows():
    print(f'  {r["name"]:35s}: {r["n"]:4d} entries, {r["prof_rate"]:5.1f}% profitable, gain={r["avg_gain"]:+.1f}%, loss={r["avg_loss"]:+.1f}%, edge={r["edge"]:+.1f}%')

print('\n--- TOP 20 BY DIRECTIONAL EDGE ---')
top_edge = best_df.sort_values('edge', ascending=False).head(20)
for _, r in top_edge.iterrows():
    print(f'  {r["name"]:35s}: {r["n"]:4d} entries, {r["prof_rate"]:5.1f}% profitable, gain={r["avg_gain"]:+.1f}%, loss={r["avg_loss"]:+.1f}%, edge={r["edge"]:+.1f}%')

print('\n--- FILTERS WITH >40% PROFITABILITY AND >20 TRADES ---')
good = best_df[(best_df['prof_rate'] >= 40) & (best_df['n'] >= 20)].sort_values('prof_rate', ascending=False)
for _, r in good.iterrows():
    print(f'  {r["name"]:35s}: {r["n"]:4d} entries, {r["prof_rate"]:5.1f}% profitable, edge={r["edge"]:+.1f}%')

print('\nDONE!')
