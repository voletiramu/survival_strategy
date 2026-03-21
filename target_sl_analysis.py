#!/usr/bin/env python3
"""Target-before-SL analysis v3 — FAST vectorized version.
Tracks same contract (strike+type) premium over time using real Zerodha LTP."""

import pandas as pd
import numpy as np
import glob, os
from datetime import timedelta
from collections import defaultdict

EQUITY_DIR = '/root/algo_trading/paper_trades'
COMMODITY_DIR = '/root/algo_trading/paper_trades_commodity'
MAX_TRADES_PER_DAY = 3
COOLDOWN_SEC = 600
TRACKING_MIN = 30
COST_PCT = 1.2

LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
             'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 100}

COMBOS = [
    ('T2_S3', 2, 3), ('T2_S5', 2, 5), ('T3_S3', 3, 3),
    ('T3_S5', 3, 5), ('T3_S7', 3, 7), ('T3_S10', 3, 10),
    ('T5_S5', 5, 5), ('T5_S7', 5, 7), ('T5_S10', 5, 10), ('T5_S15', 5, 15),
    ('T7_S10', 7, 10), ('T7_S15', 7, 15),
    ('T10_S15', 10, 15), ('T10_S20', 10, 20),
    ('T15_S20', 15, 20), ('T15_S30', 15, 30), ('T20_S30', 20, 30),
]

FILTERS = [
    'No_Filter', 'Quality60', 'Quality70', 'Quality80',
    'Morning_Only', 'Afternoon', 'HighPremium', 'LowPremium',
    'Q80_Morning', 'Q80_Afternoon', 'Q70_Morning',
    'CPR_Only', 'GhostZone', 'BUY_CE', 'BUY_PE',
    'Commodity_Only', 'Equity_Only',
    'BANKNIFTY', 'GOLDM', 'NIFTY', 'CRUDEOILM', 'SILVERM',
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
    print(f"Dates: {df['timestamp'].min().date()} to {df['timestamp'].max().date()}")
    return df

def apply_filter(df, name):
    if name == 'No_Filter': return df
    if name == 'Quality80': return df[df['quality_score'] >= 80]
    if name == 'Quality70': return df[df['quality_score'] >= 70]
    if name == 'Quality60': return df[df['quality_score'] >= 60]
    if name == 'Morning_Only': return df[df['hour'] < 11]
    if name == 'Afternoon': return df[(df['hour'] >= 11) & (df['hour'] < 14)]
    if name == 'HighPremium':
        med = df.groupby('symbol')['premium'].transform('median')
        return df[df['premium'] >= med]
    if name == 'LowPremium':
        med = df.groupby('symbol')['premium'].transform('median')
        return df[df['premium'] < med]
    if name == 'Q80_Morning': return df[(df['quality_score'] >= 80) & (df['hour'] < 11)]
    if name == 'Q80_Afternoon': return df[(df['quality_score'] >= 80) & (df['hour'] >= 11) & (df['hour'] < 14)]
    if name == 'Q70_Morning': return df[(df['quality_score'] >= 70) & (df['hour'] < 11)]
    if name == 'CPR_Only': return df[df['strategy'].str.contains('CPR', case=False, na=False)]
    if name == 'GhostZone': return df[df['strategy'].str.contains('Ghost|ghost', case=False, na=False)]
    if name == 'BUY_CE': return df[df['type'].str.contains('BUY_CE', case=False, na=False)]
    if name == 'BUY_PE': return df[df['type'].str.contains('BUY_PE', case=False, na=False)]
    if name == 'Commodity_Only': return df[df['asset_class'] == 'commodity']
    if name == 'Equity_Only': return df[df['asset_class'] == 'equity']
    if name == 'BANKNIFTY': return df[df['symbol'] == 'BANKNIFTY']
    if name == 'GOLDM': return df[df['symbol'] == 'GOLDM']
    if name == 'NIFTY': return df[df['symbol'] == 'NIFTY']
    if name == 'CRUDEOILM': return df[df['symbol'] == 'CRUDEOILM']
    if name == 'SILVERM': return df[df['symbol'] == 'SILVERM']
    return df

def precompute_entries(all_signals):
    """For each unique contract (date+symbol+strike+type), compute the FIRST signal
    as entry and track max gain / max loss / whether target hit before SL for each combo."""
    # Group by contract
    all_signals = all_signals.copy()
    all_signals['contract'] = (all_signals['date'].astype(str) + '|' +
                                all_signals['symbol'] + '|' +
                                all_signals['strike'].astype(str) + '|' +
                                all_signals['type'])

    contracts = all_signals.groupby('contract')
    print(f"Processing {len(contracts)} unique contracts...")

    # For each contract, compute entry and premium trajectory
    entries = []
    for cname, group in contracts:
        group = group.sort_values('timestamp')
        if len(group) < 2:
            continue
        parts = cname.split('|')
        date_str, sym, strike, sig_type = parts[0], parts[1], parts[2], parts[3]
        entry_row = group.iloc[0]
        entry_prem = entry_row['premium']
        entry_time = entry_row['timestamp']
        cutoff = entry_time + timedelta(minutes=TRACKING_MIN)

        # Get premium series after entry within window
        future = group[group['timestamp'] > entry_time]
        future = future[future['timestamp'] <= cutoff]
        if future.empty:
            continue

        premiums = future['premium'].values
        max_prem = premiums.max()
        min_prem = premiums.min()
        last_prem = premiums[-1]
        max_gain_pct = (max_prem - entry_prem) / entry_prem * 100
        max_loss_pct = (min_prem - entry_prem) / entry_prem * 100

        # For each T/SL combo, check if target hit before SL
        combo_results = {}
        for combo_name, t_pct, s_pct in COMBOS:
            target_p = entry_prem * (1 + t_pct / 100)
            sl_p = entry_prem * (1 - s_pct / 100)
            hit_t, hit_s = False, False
            exit_p = last_prem
            for p in premiums:
                if p >= target_p:
                    hit_t = True; exit_p = target_p; break
                if p <= sl_p:
                    hit_s = True; exit_p = sl_p; break
            combo_results[combo_name] = (hit_t, hit_s, exit_p)

        entries.append({
            'date': date_str, 'symbol': sym, 'strike': strike,
            'type': sig_type, 'entry_prem': entry_prem,
            'entry_time': entry_time, 'max_gain_pct': max_gain_pct,
            'max_loss_pct': max_loss_pct, 'last_prem': last_prem,
            'quality_score': entry_row['quality_score'],
            'strategy': entry_row.get('strategy', ''),
            'asset_class': entry_row['asset_class'],
            'hour': entry_row['hour'],
            'combos': combo_results,
            'n_bars': len(future),
        })

    print(f"  {len(entries)} tradeable contracts (with future data)")
    return entries

def run_analysis():
    print("=" * 90)
    print("TARGET-BEFORE-SL ANALYSIS v3 (FAST)")
    print("Same-contract premium tracking | Real Zerodha LTP | ~15sec intervals")
    print("=" * 90)

    signals = load_signals()
    if signals.empty:
        return

    # Precompute all entries and their combo results
    entries = precompute_entries(signals)
    if not entries:
        print("No entries!"); return

    # Convert to DataFrame for fast filtering
    edf = pd.DataFrame(entries)
    edf['entry_time'] = pd.to_datetime(edf['entry_time'])

    all_results = []
    done = 0
    total = len(FILTERS) * len(COMBOS)

    for filter_name in FILTERS:
        # Apply filter
        mask = pd.Series(True, index=edf.index)
        if filter_name == 'Quality80': mask = edf['quality_score'] >= 80
        elif filter_name == 'Quality70': mask = edf['quality_score'] >= 70
        elif filter_name == 'Quality60': mask = edf['quality_score'] >= 60
        elif filter_name == 'Morning_Only': mask = edf['hour'] < 11
        elif filter_name == 'Afternoon': mask = (edf['hour'] >= 11) & (edf['hour'] < 14)
        elif filter_name == 'HighPremium':
            med = edf.groupby('symbol')['entry_prem'].transform('median')
            mask = edf['entry_prem'] >= med
        elif filter_name == 'LowPremium':
            med = edf.groupby('symbol')['entry_prem'].transform('median')
            mask = edf['entry_prem'] < med
        elif filter_name == 'Q80_Morning': mask = (edf['quality_score'] >= 80) & (edf['hour'] < 11)
        elif filter_name == 'Q80_Afternoon': mask = (edf['quality_score'] >= 80) & (edf['hour'] >= 11) & (edf['hour'] < 14)
        elif filter_name == 'Q70_Morning': mask = (edf['quality_score'] >= 70) & (edf['hour'] < 11)
        elif filter_name == 'CPR_Only': mask = edf['strategy'].str.contains('CPR', case=False, na=False)
        elif filter_name == 'GhostZone': mask = edf['strategy'].str.contains('Ghost|ghost', case=False, na=False)
        elif filter_name == 'BUY_CE': mask = edf['type'].str.contains('BUY_CE', case=False, na=False)
        elif filter_name == 'BUY_PE': mask = edf['type'].str.contains('BUY_PE', case=False, na=False)
        elif filter_name == 'Commodity_Only': mask = edf['asset_class'] == 'commodity'
        elif filter_name == 'Equity_Only': mask = edf['asset_class'] == 'equity'
        elif filter_name == 'BANKNIFTY': mask = edf['symbol'] == 'BANKNIFTY'
        elif filter_name == 'GOLDM': mask = edf['symbol'] == 'GOLDM'
        elif filter_name == 'NIFTY': mask = edf['symbol'] == 'NIFTY'
        elif filter_name == 'CRUDEOILM': mask = edf['symbol'] == 'CRUDEOILM'
        elif filter_name == 'SILVERM': mask = edf['symbol'] == 'SILVERM'

        filtered = edf[mask]
        if len(filtered) < 5:
            done += len(COMBOS)
            continue

        # Apply cooldown and max trades per day
        filtered = filtered.sort_values('entry_time')
        selected_idx = []
        daily_count = defaultdict(int)
        last_entry_sym = {}

        for idx, row in filtered.iterrows():
            sym = row['symbol']
            date = row['date']
            day_key = f"{sym}_{date}"
            if daily_count[day_key] >= MAX_TRADES_PER_DAY:
                continue
            if sym in last_entry_sym:
                if (row['entry_time'] - last_entry_sym[sym]).total_seconds() < COOLDOWN_SEC:
                    continue
            selected_idx.append(idx)
            daily_count[day_key] += 1
            last_entry_sym[sym] = row['entry_time']

        active = filtered.loc[selected_idx]
        n_trades = len(active)

        for combo_name, t_pct, s_pct in COMBOS:
            done += 1
            if n_trades < 5:
                continue

            wins, losses, timeouts = 0, 0, 0
            win_pnl, loss_pnl, total_pnl = 0, 0, 0

            for _, row in active.iterrows():
                cr = row['combos'].get(combo_name)
                if not cr:
                    continue
                hit_t, hit_s, exit_p = cr
                entry_p = row['entry_prem']
                lot = LOT_SIZES.get(row['symbol'], 50)
                gross = (exit_p - entry_p) * lot
                cost = entry_p * lot * COST_PCT / 100
                net = gross - cost

                if hit_t:
                    wins += 1; win_pnl += net
                elif hit_s:
                    losses += 1; loss_pnl += net
                else:
                    timeouts += 1
                    if net > 0: win_pnl += net
                    else: loss_pnl += net
                total_pnl += net

            total_t = wins + losses + timeouts
            if total_t == 0:
                continue
            wr = wins / total_t * 100
            gw = abs(win_pnl)
            gl = abs(loss_pnl)
            pf = gw / gl if gl > 0 else 99.9

            all_results.append({
                'filter': filter_name, 'combo': combo_name,
                'target_pct': t_pct, 'sl_pct': s_pct,
                'trades': total_t, 'wins': wins, 'losses': losses,
                'timeout': timeouts, 'win_rate': round(wr, 1),
                'pf': round(pf, 2), 'net_pnl': round(total_pnl),
                'avg_pnl': round(total_pnl / total_t),
            })

        if done % 50 == 0:
            print(f"  Progress: {done}/{total}")

    if not all_results:
        print("No results!"); return

    df = pd.DataFrame(all_results)
    df.to_csv('/root/algo_trading/paper_trades/target_sl_v2_results.csv', index=False)

    min_t = 15
    valid = df[df['trades'] >= min_t]

    def show(title, sdf, n=30):
        print(f"\n{'='*90}\n{title}\n{'='*90}")
        print(f"  {'Filter':22s} {'Combo':10s} {'#':>5s} {'W':>4s} {'L':>4s} {'TO':>4s} {'WR%':>6s} {'PF':>6s} {'NetPnL':>12s} {'Avg':>8s}")
        print(f"  {'-'*85}")
        for _, r in sdf.head(n).iterrows():
            m = "**" if r['win_rate'] >= 60 and r['pf'] >= 1.5 else "  "
            print(f"{m}{r['filter']:22s} {r['combo']:10s} {r['trades']:5d} {r['wins']:4d} {r['losses']:4d} {r['timeout']:4d} {r['win_rate']:5.1f}% {r['pf']:5.2f} Rs{r['net_pnl']:>10,.0f} {r['avg_pnl']:>7,.0f}")

    show(f"TOP 30 BY NET PNL (min {min_t} trades)", valid.sort_values('net_pnl', ascending=False))
    show(f"TOP 30 BY PROFIT FACTOR (min {min_t} trades)", valid.sort_values('pf', ascending=False))
    show(f"TOP 30 BY WIN RATE (min {min_t} trades)", valid.sort_values('win_rate', ascending=False))

    # Profitable
    prof = valid[valid['net_pnl'] > 0].sort_values('net_pnl', ascending=False)
    if len(prof) > 0:
        show(f"*** ALL PROFITABLE ({len(prof)} combos) ***", prof, 50)

    # Target: WR >= 60% AND PF >= 1.5
    w = valid[(valid['win_rate'] >= 60) & (valid['pf'] >= 1.5)].sort_values('net_pnl', ascending=False)
    if len(w) > 0:
        show(f"*** TARGET MET: WR>=60% PF>=1.5 ({len(w)}) ***", w)
    else:
        print("\n  No combos with WR>=60% AND PF>=1.5")
        w2 = valid[(valid['win_rate'] >= 50) & (valid['pf'] >= 1.2)].sort_values('net_pnl', ascending=False)
        if len(w2) > 0:
            show(f"  Relaxed: WR>=50% PF>=1.2 ({len(w2)})", w2)

    # Premium movement analysis
    print(f"\n{'='*90}\nPREMIUM MOVEMENT ANALYSIS (max gain/loss within 30min per contract)\n{'='*90}")
    for sym in sorted(edf['symbol'].unique()):
        sdf = edf[edf['symbol'] == sym]
        print(f"\n  {sym} ({len(sdf)} contracts):")
        for pct in [2, 3, 5, 7, 10, 15, 20]:
            up = (sdf['max_gain_pct'] >= pct).mean() * 100
            dn = (sdf['max_loss_pct'] <= -pct).mean() * 100
            print(f"    +{pct:2d}%: {up:5.1f}% reach    -{pct:2d}%: {dn:5.1f}% reach    Edge: {up-dn:+.1f}pp")

    # Symbol breakdown for top 5 profitable
    if len(prof) > 0:
        print(f"\n{'='*90}\nSYMBOL BREAKDOWN FOR TOP 5 PROFITABLE COMBOS\n{'='*90}")
        for _, r in prof.head(5).iterrows():
            print(f"\n  {r['filter']} + {r['combo']} (WR={r['win_rate']}%, PF={r['pf']}, PnL=Rs {r['net_pnl']:,.0f}):")
            # Re-run per symbol
            mask_f = pd.Series(True, index=edf.index)
            fn = r['filter']
            if fn == 'Quality80': mask_f = edf['quality_score'] >= 80
            elif fn == 'Quality70': mask_f = edf['quality_score'] >= 70
            elif fn == 'Quality60': mask_f = edf['quality_score'] >= 60
            elif fn == 'Morning_Only': mask_f = edf['hour'] < 11
            elif fn == 'Commodity_Only': mask_f = edf['asset_class'] == 'commodity'
            elif fn == 'Equity_Only': mask_f = edf['asset_class'] == 'equity'
            elif fn == 'BANKNIFTY': mask_f = edf['symbol'] == 'BANKNIFTY'
            elif fn == 'GOLDM': mask_f = edf['symbol'] == 'GOLDM'
            elif fn == 'NIFTY': mask_f = edf['symbol'] == 'NIFTY'
            elif fn == 'CRUDEOILM': mask_f = edf['symbol'] == 'CRUDEOILM'
            elif fn == 'SILVERM': mask_f = edf['symbol'] == 'SILVERM'
            elif fn == 'CPR_Only': mask_f = edf['strategy'].str.contains('CPR', case=False, na=False)
            elif fn == 'GhostZone': mask_f = edf['strategy'].str.contains('Ghost', case=False, na=False)
            elif fn == 'BUY_CE': mask_f = edf['type'].str.contains('BUY_CE', case=False, na=False)
            elif fn == 'BUY_PE': mask_f = edf['type'].str.contains('BUY_PE', case=False, na=False)
            combo_n = r['combo']
            for sym in sorted(edf[mask_f]['symbol'].unique()):
                sym_entries = edf[mask_f & (edf['symbol'] == sym)]
                sw, sl_c, st, sp = 0, 0, 0, 0
                for _, er in sym_entries.iterrows():
                    cr = er['combos'].get(combo_n)
                    if not cr: continue
                    ht, hs, ep = cr
                    lot = LOT_SIZES.get(sym, 50)
                    net = (ep - er['entry_prem']) * lot - er['entry_prem'] * lot * COST_PCT / 100
                    if ht: sw += 1
                    elif hs: sl_c += 1
                    else: st += 1
                    sp += net
                tt = sw + sl_c + st
                if tt > 0:
                    print(f"    {sym:15s}: {tt:3d} trades, W={sw} L={sl_c} TO={st}, WR={sw/tt*100:5.1f}%, PnL=Rs {sp:>8,.0f}")

    print(f"\n\nSaved: /root/algo_trading/paper_trades/target_sl_v2_results.csv")
    print("DONE!")

if __name__ == '__main__':
    run_analysis()
