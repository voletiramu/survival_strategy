#!/usr/bin/env python3
"""Target-SL Analysis v4 — Extended windows + TSL + NIFTY edge exploitation.
Key insight: NIFTY has +20% directional edge but 30min window too short.
Tests 30/60/90/120 min windows, TSL variants, and NIFTY-focused strategies."""

import pandas as pd
import numpy as np
import glob
from datetime import timedelta
from collections import defaultdict

EQUITY_DIR = '/root/algo_trading/paper_trades'
COMMODITY_DIR = '/root/algo_trading/paper_trades_commodity'
MAX_TRADES_PER_DAY = 3
COOLDOWN_SEC = 600
COST_PCT = 1.2

LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
             'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 100}

# Extended combos with different windows
WINDOWS = [30, 60, 90, 120]

COMBOS = [
    ('T3_S5', 3, 5), ('T3_S7', 3, 7), ('T3_S10', 3, 10),
    ('T5_S7', 5, 7), ('T5_S10', 5, 10), ('T5_S15', 5, 15),
    ('T7_S10', 7, 10), ('T7_S15', 7, 15),
    ('T10_S15', 10, 15), ('T10_S20', 10, 20),
    ('T15_S20', 15, 20), ('T15_S30', 15, 30),
    ('T20_S30', 20, 30),
]

# TSL configs: (name, breakeven_trigger%, trail_distance%)
TSL_CONFIGS = [
    ('TSL_BE3_T2', 3, 2),    # Breakeven at +3%, trail 2% behind peak
    ('TSL_BE5_T3', 5, 3),    # Breakeven at +5%, trail 3%
    ('TSL_BE7_T5', 7, 5),    # Breakeven at +7%, trail 5%
    ('TSL_BE10_T5', 10, 5),  # Breakeven at +10%, trail 5%
    ('TSL_BE3_T1', 3, 1),    # Tight trail
    ('TSL_BE5_T2', 5, 2),
]

FILTERS = [
    'No_Filter', 'Quality70', 'Quality80',
    'Morning_Only', 'Q80_Morning', 'Q70_Morning',
    'BUY_CE', 'BUY_PE', 'CPR_Only',
    'Commodity_Only', 'Equity_Only',
    'BANKNIFTY', 'GOLDM', 'NIFTY', 'SENSEX',
    'NIFTY_Morning', 'NIFTY_Q80', 'NIFTY_CE', 'NIFTY_PE',
    'BANKNIFTY_Morning', 'BANKNIFTY_Q80',
]

def load_signals():
    dfs = []
    for f in sorted(glob.glob(f'{EQUITY_DIR}/signals_*.csv')):
        try:
            d = pd.read_csv(f); d['asset_class'] = 'equity'; dfs.append(d)
        except: pass
    for f in sorted(glob.glob(f'{COMMODITY_DIR}/commodity_signals_*.csv')):
        try:
            d = pd.read_csv(f); d['asset_class'] = 'commodity'
            if 'symbol' not in d.columns and 'commodity' in d.columns:
                d['symbol'] = d['commodity']
            dfs.append(d)
        except: pass
    if not dfs:
        print("No files!"); return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df = df.dropna(subset=['timestamp', 'symbol', 'premium', 'strike', 'type'])
    df['premium'] = pd.to_numeric(df['premium'], errors='coerce')
    df = df[df['premium'] > 0].copy()
    df['date'] = df['timestamp'].dt.date
    df['hour'] = df['timestamp'].dt.hour
    df['quality_score'] = pd.to_numeric(df.get('quality_score', pd.Series(dtype=float)), errors='coerce').fillna(0)
    df['strategy'] = df.get('strategy', pd.Series(dtype=str)).fillna('')
    df = df.sort_values('timestamp').reset_index(drop=True)
    print(f"Loaded {len(df)} signals: {df['asset_class'].value_counts().to_dict()}")
    print(f"Symbols: {sorted(df['symbol'].unique())}")
    return df

def apply_filter(edf, name):
    mask = pd.Series(True, index=edf.index)
    if name == 'Quality80': mask = edf['quality_score'] >= 80
    elif name == 'Quality70': mask = edf['quality_score'] >= 70
    elif name == 'Morning_Only': mask = edf['hour'] < 11
    elif name == 'Q80_Morning': mask = (edf['quality_score'] >= 80) & (edf['hour'] < 11)
    elif name == 'Q70_Morning': mask = (edf['quality_score'] >= 70) & (edf['hour'] < 11)
    elif name == 'CPR_Only': mask = edf['strategy'].str.contains('CPR', case=False, na=False)
    elif name == 'BUY_CE': mask = edf['type'].str.contains('BUY_CE', case=False, na=False)
    elif name == 'BUY_PE': mask = edf['type'].str.contains('BUY_PE', case=False, na=False)
    elif name == 'Commodity_Only': mask = edf['asset_class'] == 'commodity'
    elif name == 'Equity_Only': mask = edf['asset_class'] == 'equity'
    elif name == 'BANKNIFTY': mask = edf['symbol'] == 'BANKNIFTY'
    elif name == 'GOLDM': mask = edf['symbol'] == 'GOLDM'
    elif name == 'NIFTY': mask = edf['symbol'] == 'NIFTY'
    elif name == 'SENSEX': mask = edf['symbol'] == 'SENSEX'
    elif name == 'NIFTY_Morning': mask = (edf['symbol'] == 'NIFTY') & (edf['hour'] < 11)
    elif name == 'NIFTY_Q80': mask = (edf['symbol'] == 'NIFTY') & (edf['quality_score'] >= 80)
    elif name == 'NIFTY_CE': mask = (edf['symbol'] == 'NIFTY') & edf['type'].str.contains('BUY_CE', case=False, na=False)
    elif name == 'NIFTY_PE': mask = (edf['symbol'] == 'NIFTY') & edf['type'].str.contains('BUY_PE', case=False, na=False)
    elif name == 'BANKNIFTY_Morning': mask = (edf['symbol'] == 'BANKNIFTY') & (edf['hour'] < 11)
    elif name == 'BANKNIFTY_Q80': mask = (edf['symbol'] == 'BANKNIFTY') & (edf['quality_score'] >= 80)
    return edf[mask]

def precompute_contracts(all_signals, max_window_min=120):
    """Pre-compute premium series for each unique contract."""
    all_signals = all_signals.copy()
    all_signals['contract'] = (all_signals['date'].astype(str) + '|' +
                                all_signals['symbol'] + '|' +
                                all_signals['strike'].astype(str) + '|' +
                                all_signals['type'])
    contracts = all_signals.groupby('contract')
    print(f"Processing {len(contracts)} contracts...")

    entries = []
    for cname, group in contracts:
        group = group.sort_values('timestamp')
        if len(group) < 3:
            continue
        parts = cname.split('|')
        entry_row = group.iloc[0]
        entry_prem = entry_row['premium']
        entry_time = entry_row['timestamp']

        # Get ALL future premiums within max window
        cutoff = entry_time + timedelta(minutes=max_window_min)
        future = group[(group['timestamp'] > entry_time) & (group['timestamp'] <= cutoff)]
        if future.empty:
            continue

        # Store premium series as list of (seconds_from_entry, premium)
        premium_series = []
        for _, row in future.iterrows():
            secs = (row['timestamp'] - entry_time).total_seconds()
            premium_series.append((secs, row['premium']))

        entries.append({
            'contract': cname,
            'symbol': parts[1],
            'date': parts[0],
            'strike': parts[2],
            'type': parts[3],
            'entry_prem': entry_prem,
            'entry_time': entry_time,
            'quality_score': entry_row['quality_score'],
            'strategy': entry_row.get('strategy', ''),
            'asset_class': entry_row['asset_class'],
            'hour': entry_row['hour'],
            'premium_series': premium_series,
            'n_bars': len(premium_series),
        })

    print(f"  {len(entries)} tradeable contracts")
    return entries

def sim_fixed(premium_series, entry_prem, target_pct, sl_pct, window_sec):
    """Simulate fixed target/SL. Returns (hit_target, hit_sl, exit_prem, max_gain_pct)."""
    target_p = entry_prem * (1 + target_pct / 100)
    sl_p = entry_prem * (1 - sl_pct / 100)
    exit_p = entry_prem
    max_p = entry_prem

    for secs, prem in premium_series:
        if secs > window_sec:
            break
        max_p = max(max_p, prem)
        if prem >= target_p:
            return True, False, target_p, (max_p - entry_prem) / entry_prem * 100
        if prem <= sl_p:
            return False, True, sl_p, (max_p - entry_prem) / entry_prem * 100
        exit_p = prem

    return False, False, exit_p, (max_p - entry_prem) / entry_prem * 100

def sim_tsl(premium_series, entry_prem, be_trigger_pct, trail_pct, sl_pct, window_sec):
    """Simulate TSL: initial SL, then breakeven at trigger, then trail.
    Returns (outcome, exit_prem, max_gain_pct)."""
    initial_sl = entry_prem * (1 - sl_pct / 100)
    be_trigger = entry_prem * (1 + be_trigger_pct / 100)
    current_sl = initial_sl
    max_prem = entry_prem
    be_activated = False
    exit_p = entry_prem

    for secs, prem in premium_series:
        if secs > window_sec:
            break

        if prem > max_prem:
            max_prem = prem

        # Activate breakeven
        if not be_activated and prem >= be_trigger:
            be_activated = True
            current_sl = entry_prem  # breakeven

        # Trail SL
        if be_activated:
            trail_sl = max_prem * (1 - trail_pct / 100)
            current_sl = max(current_sl, trail_sl)

        # Check SL hit
        if prem <= current_sl:
            return ('tsl_exit', prem, (max_prem - entry_prem) / entry_prem * 100)

        exit_p = prem

    # Timeout
    return ('timeout', exit_p, (max_prem - entry_prem) / entry_prem * 100)

def run():
    print("=" * 90)
    print("TARGET-SL ANALYSIS v4 — Extended Windows + TSL")
    print("=" * 90)

    signals = load_signals()
    if signals.empty: return

    entries = precompute_contracts(signals, max_window_min=120)
    if not entries: return

    edf = pd.DataFrame([{k: v for k, v in e.items() if k != 'premium_series'} for e in entries])
    edf['entry_time'] = pd.to_datetime(edf['entry_time'])
    # Keep premium_series in separate dict
    ps_dict = {e['contract']: e['premium_series'] for e in entries}

    all_results = []
    done = 0

    # PART 1: Fixed T/SL with different windows
    print("\n--- PART 1: Fixed Target/SL with different windows ---")
    for window_min in WINDOWS:
        window_sec = window_min * 60
        for filter_name in FILTERS:
            filtered = apply_filter(edf, filter_name)
            if len(filtered) < 5: continue

            # Apply cooldown
            filtered = filtered.sort_values('entry_time')
            sel = []
            daily_cnt = defaultdict(int)
            last_e = {}
            for idx, row in filtered.iterrows():
                sym, date = row['symbol'], row['date']
                dk = f"{sym}_{date}"
                if daily_cnt[dk] >= MAX_TRADES_PER_DAY: continue
                if sym in last_e and (row['entry_time'] - last_e[sym]).total_seconds() < COOLDOWN_SEC: continue
                sel.append(idx)
                daily_cnt[dk] += 1
                last_e[sym] = row['entry_time']
            active = filtered.loc[sel]
            if len(active) < 5: continue

            for combo_name, t_pct, s_pct in COMBOS:
                done += 1
                wins, losses, timeouts = 0, 0, 0
                win_pnl, loss_pnl, total_pnl = 0, 0, 0

                for _, row in active.iterrows():
                    ps = ps_dict.get(row['contract'], [])
                    if not ps: continue
                    ht, hs, ep, mg = sim_fixed(ps, row['entry_prem'], t_pct, s_pct, window_sec)
                    lot = LOT_SIZES.get(row['symbol'], 50)
                    gross = (ep - row['entry_prem']) * lot
                    cost = row['entry_prem'] * lot * COST_PCT / 100
                    net = gross - cost

                    if ht: wins += 1; win_pnl += net
                    elif hs: losses += 1; loss_pnl += net
                    else:
                        timeouts += 1
                        if net > 0: win_pnl += net
                        else: loss_pnl += net
                    total_pnl += net

                tt = wins + losses + timeouts
                if tt < 5: continue
                wr = wins / tt * 100
                gw, gl = abs(win_pnl), abs(loss_pnl)
                pf = gw / gl if gl > 0 else 99.9

                all_results.append({
                    'mode': 'fixed', 'window': window_min,
                    'filter': filter_name, 'combo': combo_name,
                    'target_pct': t_pct, 'sl_pct': s_pct,
                    'trades': tt, 'wins': wins, 'losses': losses,
                    'timeout': timeouts, 'win_rate': round(wr, 1),
                    'pf': round(pf, 2), 'net_pnl': round(total_pnl),
                    'avg_pnl': round(total_pnl / tt),
                    'resolved_wr': round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
                })

        if done % 100 == 0:
            print(f"  Fixed window={window_min}min: {done} combos done")

    # PART 2: TSL with different windows and initial SL
    print("\n--- PART 2: TSL Strategies ---")
    initial_sls = [5, 7, 10, 15]  # initial SL before breakeven activates
    tsl_done = 0

    for window_min in [60, 90, 120]:
        window_sec = window_min * 60
        for filter_name in FILTERS:
            filtered = apply_filter(edf, filter_name)
            if len(filtered) < 5: continue

            filtered = filtered.sort_values('entry_time')
            sel = []
            daily_cnt = defaultdict(int)
            last_e = {}
            for idx, row in filtered.iterrows():
                sym, date = row['symbol'], row['date']
                dk = f"{sym}_{date}"
                if daily_cnt[dk] >= MAX_TRADES_PER_DAY: continue
                if sym in last_e and (row['entry_time'] - last_e[sym]).total_seconds() < COOLDOWN_SEC: continue
                sel.append(idx)
                daily_cnt[dk] += 1
                last_e[sym] = row['entry_time']
            active = filtered.loc[sel]
            if len(active) < 5: continue

            for tsl_name, be_pct, trail_pct in TSL_CONFIGS:
                for init_sl in initial_sls:
                    tsl_done += 1
                    wins, losses, timeouts = 0, 0, 0
                    win_pnl, loss_pnl, total_pnl = 0, 0, 0

                    for _, row in active.iterrows():
                        ps = ps_dict.get(row['contract'], [])
                        if not ps: continue
                        outcome, ep, mg = sim_tsl(ps, row['entry_prem'], be_pct, trail_pct, init_sl, window_sec)
                        lot = LOT_SIZES.get(row['symbol'], 50)
                        gross = (ep - row['entry_prem']) * lot
                        cost = row['entry_prem'] * lot * COST_PCT / 100
                        net = gross - cost

                        if net > 0: wins += 1; win_pnl += net
                        else: losses += 1; loss_pnl += net
                        total_pnl += net

                    tt = wins + losses + timeouts
                    if tt < 5: continue
                    wr = wins / tt * 100
                    gw, gl = abs(win_pnl), abs(loss_pnl)
                    pf = gw / gl if gl > 0 else 99.9

                    all_results.append({
                        'mode': 'tsl', 'window': window_min,
                        'filter': filter_name,
                        'combo': f"{tsl_name}_SL{init_sl}",
                        'target_pct': be_pct, 'sl_pct': init_sl,
                        'trades': tt, 'wins': wins, 'losses': losses,
                        'timeout': 0, 'win_rate': round(wr, 1),
                        'pf': round(pf, 2), 'net_pnl': round(total_pnl),
                        'avg_pnl': round(total_pnl / tt),
                        'resolved_wr': round(wr, 1),
                    })

            if tsl_done % 100 == 0:
                print(f"  TSL window={window_min}min: {tsl_done} combos done")

    if not all_results:
        print("No results!"); return

    df = pd.DataFrame(all_results)
    df.to_csv('/root/algo_trading/paper_trades/target_sl_v4_results.csv', index=False)

    min_t = 15

    def show(title, sdf, n=30):
        print(f"\n{'='*95}\n{title}\n{'='*95}")
        print(f"  {'Mode':5s} {'W':>3s} {'Filter':22s} {'Combo':16s} {'#':>4s} {'W':>3s} {'L':>3s} {'TO':>3s} {'WR%':>6s} {'RWR':>5s} {'PF':>6s} {'NetPnL':>12s}")
        print(f"  {'-'*90}")
        for _, r in sdf.head(n).iterrows():
            m = "**" if r['pf'] >= 1.5 and r['win_rate'] >= 40 else "  "
            rwr = r.get('resolved_wr', 0)
            print(f"{m}{r['mode']:5s} {r['window']:3d} {r['filter']:22s} {r['combo']:16s} {r['trades']:4d} {r['wins']:3d} {r['losses']:3d} {r['timeout']:3d} {r['win_rate']:5.1f}% {rwr:4.0f}% {r['pf']:5.2f} Rs{r['net_pnl']:>10,.0f}")

    valid = df[df['trades'] >= min_t]

    # All profitable
    prof = valid[valid['net_pnl'] > 0].sort_values('net_pnl', ascending=False)
    if len(prof) > 0:
        show(f"*** ALL PROFITABLE COMBOS ({len(prof)}) ***", prof, 60)
    else:
        show("LEAST LOSS (all negative)", valid.sort_values('net_pnl', ascending=False), 30)

    # Best PF
    show("TOP 30 BY PROFIT FACTOR", valid.sort_values('pf', ascending=False))

    # Best WR
    show("TOP 30 BY WIN RATE", valid.sort_values('win_rate', ascending=False))

    # TSL-only results
    tsl_valid = valid[valid['mode'] == 'tsl']
    if len(tsl_valid) > 0:
        show("TOP 30 TSL STRATEGIES", tsl_valid.sort_values('net_pnl', ascending=False))

    # Fixed targets with longer windows
    for w in [60, 90, 120]:
        fw = valid[(valid['mode'] == 'fixed') & (valid['window'] == w)]
        if len(fw) > 0:
            p = fw[fw['net_pnl'] > 0]
            if len(p) > 0:
                show(f"PROFITABLE WITH {w}min WINDOW ({len(p)} combos)", p, 30)

    # Target met
    target = valid[(valid['win_rate'] >= 50) & (valid['pf'] >= 1.5)].sort_values('net_pnl', ascending=False)
    if len(target) > 0:
        show(f"*** WR>=50% AND PF>=1.5 ({len(target)}) ***", target)
    target2 = valid[(valid['win_rate'] >= 60) & (valid['pf'] >= 2.0)].sort_values('net_pnl', ascending=False)
    if len(target2) > 0:
        show(f"*** WR>=60% AND PF>=2.0 ({len(target2)}) ***", target2)

    # Resolved WR analysis (excluding timeouts)
    print(f"\n{'='*95}")
    print("RESOLVED WIN RATE ANALYSIS (target vs SL hit, excluding timeouts)")
    print(f"{'='*95}")
    fixed = valid[valid['mode'] == 'fixed']
    high_rwr = fixed[fixed['resolved_wr'] >= 60].sort_values('net_pnl', ascending=False)
    if len(high_rwr) > 0:
        show(f"Resolved WR >= 60% ({len(high_rwr)} combos)", high_rwr, 30)

    print(f"\n\nTotal combos tested: {len(df)}")
    print(f"Profitable: {len(prof)}")
    print(f"Saved: /root/algo_trading/paper_trades/target_sl_v4_results.csv")

if __name__ == '__main__':
    run()
