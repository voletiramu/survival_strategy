#!/usr/bin/env python3
"""ULTIMATE STRATEGY TEST — Combining magic filters with TSL.
Key insights:
1. Morning entries (before 11am) + Low Premium + Quality 60+ = 60-73% profitable
2. TSL with breakeven at 3-5% + trail 1-3% = PF 2-5
3. BUY_PE > BUY_CE (negative delta wins)
4. NIFTY + BANKNIFTY best symbols
5. Higher IV (15-35) = better success
"""
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

def load_signals():
    dfs = []
    for f in sorted(glob.glob(f'{EQUITY_DIR}/signals_*.csv')):
        try: d = pd.read_csv(f); d['asset_class'] = 'equity'; dfs.append(d)
        except: pass
    for f in sorted(glob.glob(f'{COMMODITY_DIR}/commodity_signals_*.csv')):
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
    df['strategy'] = df.get('strategy', pd.Series(dtype=str)).fillna('')
    df['iv'] = pd.to_numeric(df.get('iv', pd.Series(dtype=float)), errors='coerce').fillna(0)
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df

def build_contracts(df, max_window=120):
    """Build contract-level entries with premium series."""
    df['contract'] = df['date'].astype(str) + '|' + df['symbol'] + '|' + df['strike'].astype(str) + '|' + df['type']
    entries = []
    for cname, group in df.groupby('contract'):
        group = group.sort_values('timestamp')
        if len(group) < 3: continue
        entry = group.iloc[0]
        entry_p = entry['premium']
        entry_t = entry['timestamp']
        cutoff = entry_t + timedelta(minutes=max_window)
        future = group[(group['timestamp'] > entry_t) & (group['timestamp'] <= cutoff)]
        if future.empty: continue
        # Premium series: (seconds, premium)
        ps = [((row['timestamp'] - entry_t).total_seconds(), row['premium']) for _, row in future.iterrows()]
        # Compute median premium per symbol for "low premium" classification
        entries.append({
            'contract': cname,
            'symbol': entry['symbol'],
            'type': entry['type'],
            'strategy': entry.get('strategy', ''),
            'quality_score': entry.get('quality_score', 0),
            'premium': entry_p,
            'iv': entry.get('iv', 0),
            'hour': entry['hour'],
            'date': str(entry['date']),
            'asset_class': entry.get('asset_class', ''),
            'entry_time': entry_t,
            'ps': ps,
        })
    return entries

def sim_tsl(ps, entry_p, be_trigger_pct, trail_pct, init_sl_pct, window_sec):
    """TSL simulation."""
    init_sl = entry_p * (1 - init_sl_pct / 100)
    be_trigger = entry_p * (1 + be_trigger_pct / 100)
    current_sl = init_sl
    max_p = entry_p
    be_on = False
    exit_p = entry_p

    for secs, prem in ps:
        if secs > window_sec: break
        if prem > max_p: max_p = prem
        if not be_on and prem >= be_trigger:
            be_on = True
            current_sl = entry_p
        if be_on:
            trail = max_p * (1 - trail_pct / 100)
            current_sl = max(current_sl, trail)
        if prem <= current_sl:
            exit_p = prem
            outcome = 'tsl_win' if prem >= entry_p else 'tsl_loss'
            return outcome, exit_p, (max_p - entry_p) / entry_p * 100
        exit_p = prem

    return 'timeout', exit_p, (max_p - entry_p) / entry_p * 100

def sim_fixed(ps, entry_p, target_pct, sl_pct, window_sec):
    """Fixed target/SL simulation."""
    target = entry_p * (1 + target_pct / 100)
    sl = entry_p * (1 - sl_pct / 100)
    exit_p = entry_p
    for secs, prem in ps:
        if secs > window_sec: break
        if prem >= target: return 'win', target
        if prem <= sl: return 'loss', sl
        exit_p = prem
    return 'timeout', exit_p

# === ULTIMATE STRATEGIES TO TEST ===
STRATEGIES = [
    # name, filter_func, exit_type, exit_params
    # Exit params for TSL: (be_trigger, trail, init_sl, window_min)
    # Exit params for Fixed: (target, sl, window_min)

    # --- STRATEGY GROUP 1: Morning + Quality + LowPremium + TSL ---
    ('S1_Q60_AM_LowP_TSL3_1', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 60 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S2_Q60_AM_LowP_TSL5_2', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 60 and e['premium'] < med, 'tsl', (5, 2, 15, 60)),
    ('S3_Q60_AM_LowP_TSL3_2', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 60 and e['premium'] < med, 'tsl', (3, 2, 15, 60)),
    ('S4_Q80_AM_LowP_TSL3_1', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 80 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S5_Q80_AM_LowP_TSL5_2', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 80 and e['premium'] < med, 'tsl', (5, 2, 15, 60)),

    # --- STRATEGY GROUP 2: Morning + LowPremium only (no quality) ---
    ('S6_AM_LowP_TSL3_1', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S7_AM_LowP_TSL5_2', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (5, 2, 15, 60)),
    ('S8_AM_LowP_TSL3_1_120', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 15, 120)),

    # --- STRATEGY GROUP 3: NIFTY focused ---
    ('S9_NIFTY_AM_TSL3_1', lambda e, med: e['symbol'] == 'NIFTY' and e['hour'] < 11, 'tsl', (3, 1, 15, 60)),
    ('S10_NIFTY_LowP_TSL3_1', lambda e, med: e['symbol'] == 'NIFTY' and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S11_NIFTY_AM_LowP_TSL3_1', lambda e, med: e['symbol'] == 'NIFTY' and e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S12_NIFTY_AM_LowP_TSL5_2', lambda e, med: e['symbol'] == 'NIFTY' and e['hour'] < 11 and e['premium'] < med, 'tsl', (5, 2, 15, 90)),

    # --- STRATEGY GROUP 4: BANKNIFTY focused ---
    ('S13_BNF_AM_TSL3_1', lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11, 'tsl', (3, 1, 10, 120)),
    ('S14_BNF_AM_LowP_TSL3_1', lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 10, 120)),
    ('S15_BNF_AM_LowP_TSL5_2', lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['premium'] < med, 'tsl', (5, 2, 10, 120)),

    # --- STRATEGY GROUP 5: PE only (winners have -ve delta) ---
    ('S16_PE_AM_TSL3_1', lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11, 'tsl', (3, 1, 15, 60)),
    ('S17_PE_AM_LowP_TSL3_1', lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),
    ('S18_PE_Q60_AM_TSL3_1', lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11 and e['quality_score'] >= 60, 'tsl', (3, 1, 15, 60)),

    # --- STRATEGY GROUP 6: IV filter (IV 15-35 = sweet spot) ---
    ('S19_IV15_35_AM_TSL3_1', lambda e, med: 15 <= e['iv'] <= 35 and e['hour'] < 11, 'tsl', (3, 1, 15, 60)),
    ('S20_IV15_35_LowP_TSL3_1', lambda e, med: 15 <= e['iv'] <= 35 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),

    # --- STRATEGY GROUP 7: Fixed target for comparison ---
    ('S21_AM_LowP_T5_S10', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'fixed', (5, 10, 60)),
    ('S22_Q60_AM_LowP_T5_S10', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 60 and e['premium'] < med, 'fixed', (5, 10, 60)),
    ('S23_AM_LowP_T10_S15', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'fixed', (10, 15, 60)),

    # --- STRATEGY GROUP 8: SENSEX focused (highest QS profitability) ---
    ('S24_SENSEX_Q70_AM_TSL3_1', lambda e, med: e['symbol'] == 'SENSEX' and e['quality_score'] >= 70 and e['hour'] < 11, 'tsl', (3, 1, 15, 60)),
    ('S25_SENSEX_Q60_AM_TSL5_2', lambda e, med: e['symbol'] == 'SENSEX' and e['quality_score'] >= 60 and e['hour'] < 11, 'tsl', (5, 2, 15, 60)),

    # --- STRATEGY GROUP 9: Combined best signals ---
    ('S26_MULTI_BEST_TSL3_1', lambda e, med: (e['symbol'] in ['NIFTY','BANKNIFTY','SENSEX']) and e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60, 'tsl', (3, 1, 15, 60)),
    ('S27_MULTI_BEST_TSL5_2', lambda e, med: (e['symbol'] in ['NIFTY','BANKNIFTY','SENSEX']) and e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60, 'tsl', (5, 2, 15, 90)),

    # --- STRATEGY GROUP 10: Wider TSL for bigger moves ---
    ('S28_AM_LowP_TSL10_5', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (10, 5, 15, 120)),
    ('S29_AM_LowP_TSL7_3', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (7, 3, 15, 90)),

    # --- STRATEGY GROUP 11: All time + TSL (current bot style) ---
    ('S30_NoFilter_TSL3_1', lambda e, med: True, 'tsl', (3, 1, 15, 60)),
    ('S31_NoFilter_TSL5_2', lambda e, med: True, 'tsl', (5, 2, 15, 60)),

    # --- STRATEGY GROUP 12: CPR only (best strategy from actual trades) ---
    ('S32_CPR_AM_TSL3_1', lambda e, med: 'CPR' in e['strategy'] and e['hour'] < 11, 'tsl', (3, 1, 15, 60)),
    ('S33_CPR_AM_LowP_TSL3_1', lambda e, med: 'CPR' in e['strategy'] and e['hour'] < 11 and e['premium'] < med, 'tsl', (3, 1, 15, 60)),

    # --- STRATEGY GROUP 13: Tighter TSL variants ---
    ('S34_AM_LowP_TSL2_1', lambda e, med: e['hour'] < 11 and e['premium'] < med, 'tsl', (2, 1, 10, 60)),
    ('S35_Q60_AM_LowP_TSL2_1', lambda e, med: e['hour'] < 11 and e['quality_score'] >= 60 and e['premium'] < med, 'tsl', (2, 1, 10, 60)),
]

def run():
    print("=" * 100)
    print("ULTIMATE STRATEGY TEST")
    print("Combining Magic Filters + TSL + Real Zerodha Premium Data")
    print("=" * 100)

    signals = load_signals()
    print(f'Loaded {len(signals)} signals')

    entries = build_contracts(signals, max_window=120)
    print(f'{len(entries)} tradeable contracts\n')

    # Compute median premium per symbol
    sym_medians = {}
    for sym in set(e['symbol'] for e in entries):
        prems = [e['premium'] for e in entries if e['symbol'] == sym]
        sym_medians[sym] = np.median(prems) if prems else 0

    results = []

    for strat_name, filter_func, exit_type, exit_params in STRATEGIES:
        # Filter entries
        filtered = []
        for e in entries:
            med = sym_medians.get(e['symbol'], 999999)
            try:
                if filter_func(e, med):
                    filtered.append(e)
            except: pass

        if len(filtered) < 5:
            print(f'  {strat_name}: SKIP (only {len(filtered)} entries)')
            continue

        # Apply cooldown and max trades
        filtered.sort(key=lambda x: x['entry_time'])
        active = []
        daily_cnt = defaultdict(int)
        last_entry = {}
        for e in filtered:
            sym, date = e['symbol'], e['date']
            dk = f'{sym}_{date}'
            if daily_cnt[dk] >= MAX_TRADES_PER_DAY: continue
            if sym in last_entry and (e['entry_time'] - last_entry[sym]).total_seconds() < COOLDOWN_SEC: continue
            active.append(e)
            daily_cnt[dk] += 1
            last_entry[sym] = e['entry_time']

        if len(active) < 5:
            continue

        # Simulate
        wins, losses, be_exits, timeouts = 0, 0, 0, 0
        win_pnl, loss_pnl, total_pnl = 0, 0, 0
        trade_details = []

        for e in active:
            entry_p = e['premium']
            ps = e['ps']
            lot = LOT_SIZES.get(e['symbol'], 50)

            if exit_type == 'tsl':
                be_trig, trail, init_sl, window = exit_params
                outcome, exit_p, max_gain = sim_tsl(ps, entry_p, be_trig, trail, init_sl, window * 60)
            else:
                target, sl, window = exit_params
                outcome, exit_p = sim_fixed(ps, entry_p, target, sl, window * 60)
                max_gain = 0

            gross = (exit_p - entry_p) * lot
            cost = entry_p * lot * COST_PCT / 100
            net = gross - cost

            if net > 0:
                wins += 1
                win_pnl += net
            else:
                losses += 1
                loss_pnl += net

            total_pnl += net
            trade_details.append({
                'symbol': e['symbol'], 'date': e['date'],
                'entry': entry_p, 'exit': exit_p, 'net': net,
                'outcome': outcome, 'type': e['type'],
            })

        total = wins + losses
        wr = wins / total * 100 if total > 0 else 0
        gw = abs(win_pnl)
        gl = abs(loss_pnl)
        pf = gw / gl if gl > 0 else 99.9
        avg_win = gw / wins if wins > 0 else 0
        avg_loss = gl / losses if losses > 0 else 0

        r = {
            'strategy': strat_name,
            'trades': total, 'wins': wins, 'losses': losses,
            'win_rate': round(wr, 1), 'pf': round(pf, 2),
            'net_pnl': round(total_pnl),
            'avg_win': round(avg_win), 'avg_loss': round(avg_loss),
            'avg_pnl': round(total_pnl / total) if total > 0 else 0,
        }
        results.append(r)

        # Symbol breakdown
        sym_detail = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
        for td in trade_details:
            if td['net'] > 0: sym_detail[td['symbol']]['w'] += 1
            else: sym_detail[td['symbol']]['l'] += 1
            sym_detail[td['symbol']]['pnl'] += td['net']

        # Print compact result
        marker = "***" if wr >= 50 and pf >= 1.5 else "   "
        print(f'{marker} {strat_name:35s}: {total:3d} trades, W={wins:2d} L={losses:2d}, WR={wr:5.1f}%, PF={pf:5.2f}, PnL=Rs {total_pnl:>10,.0f} | AvgW={avg_win:>6,.0f} AvgL={avg_loss:>6,.0f}')
        for sym in sorted(sym_detail.keys()):
            sd = sym_detail[sym]
            st = sd['w'] + sd['l']
            swr = sd['w'] / st * 100 if st > 0 else 0
            print(f'          {sym:12s}: {st:2d} trades, WR={swr:4.0f}%, PnL=Rs {sd["pnl"]:>8,.0f}')

    # Summary table
    print('\n' + '=' * 100)
    print('STRATEGY COMPARISON — SORTED BY NET PNL')
    print('=' * 100)
    rdf = pd.DataFrame(results).sort_values('net_pnl', ascending=False)
    print(f'  {"Strategy":35s} {"#":>4s} {"W":>3s} {"L":>3s} {"WR%":>6s} {"PF":>6s} {"NetPnL":>12s} {"AvgW":>8s} {"AvgL":>8s} {"AvgPnL":>8s}')
    print(f'  {"-"*95}')
    for _, r in rdf.iterrows():
        m = "**" if r['win_rate'] >= 50 and r['pf'] >= 1.5 else "  "
        print(f'{m}{r["strategy"]:35s} {r["trades"]:4d} {r["wins"]:3d} {r["losses"]:3d} {r["win_rate"]:5.1f}% {r["pf"]:5.2f} Rs{r["net_pnl"]:>10,.0f} {r["avg_win"]:>7,.0f} {r["avg_loss"]:>7,.0f} {r["avg_pnl"]:>7,.0f}')

    # Save
    rdf.to_csv('/root/algo_trading/paper_trades/ultimate_strategy_results.csv', index=False)

    # Highlight winners
    winners = rdf[(rdf['win_rate'] >= 50) & (rdf['pf'] >= 1.5)]
    if len(winners) > 0:
        print(f'\n*** {len(winners)} STRATEGIES MEETING TARGET (WR>=50%, PF>=1.5) ***')
        for _, r in winners.iterrows():
            print(f'  {r["strategy"]:35s}: WR={r["win_rate"]}%, PF={r["pf"]}, PnL=Rs {r["net_pnl"]:,.0f}')

    near = rdf[(rdf['pf'] >= 2.0)]
    if len(near) > 0:
        print(f'\n*** {len(near)} STRATEGIES WITH PF >= 2.0 ***')
        for _, r in near.iterrows():
            print(f'  {r["strategy"]:35s}: WR={r["win_rate"]}%, PF={r["pf"]}, PnL=Rs {r["net_pnl"]:,.0f}')

    print('\nDONE!')

if __name__ == '__main__':
    run()
