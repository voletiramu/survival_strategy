#!/usr/bin/env python3
"""Live signal simulation — generates detailed trade log with timestamps,
strike, premium, capital tracking for Excel export."""
import pandas as pd
import numpy as np
import glob, json
from datetime import timedelta
from collections import defaultdict

EQUITY_DIR = '/root/algo_trading/paper_trades'
COMMODITY_DIR = '/root/algo_trading/paper_trades_commodity'
COOLDOWN_SEC = 600
COST_PCT = 1.2
INITIAL_CAPITAL = 300000

LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
             'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 100}

# Top strategies to simulate
STRATEGIES = {
    'S15_BNF_AM_LowP_TSL5_2': {
        'desc': 'BANKNIFTY Morning LowPrem TSL(BE5,Trail2,SL10)',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['premium'] < med,
        'tsl': (5, 2, 10, 120), 'max_per_day': 3,
    },
    'S26_MULTI_BEST_TSL3_1': {
        'desc': 'NIF+BNF+SEN Q60 Morning LowPrem TSL(BE3,Trail1,SL15)',
        'filter': lambda e, med: e['symbol'] in ['NIFTY','BANKNIFTY','SENSEX'] and e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60,
        'tsl': (3, 1, 15, 60), 'max_per_day': 3,
    },
    'S6_AM_LowP_TSL3_1': {
        'desc': 'All Morning LowPrem TSL(BE3,Trail1,SL15)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'tsl': (3, 1, 15, 60), 'max_per_day': 3,
    },
    'S14_BNF_AM_LowP_TSL3_1': {
        'desc': 'BANKNIFTY Morning LowPrem TSL(BE3,Trail1,SL10)',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['premium'] < med,
        'tsl': (3, 1, 10, 120), 'max_per_day': 3,
    },
    'S17_PE_AM_LowP_TSL3_1': {
        'desc': 'PE Only Morning LowPrem TSL(BE3,Trail1,SL15)',
        'filter': lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11 and e['premium'] < med,
        'tsl': (3, 1, 15, 60), 'max_per_day': 3,
    },
}

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
        ps = [((row['timestamp'] - entry_t).total_seconds(), row['premium'], row['timestamp']) for _, row in future.iterrows()]
        entries.append({
            'contract': cname, 'symbol': entry['symbol'], 'type': entry['type'],
            'strategy': entry.get('strategy', ''), 'quality_score': entry.get('quality_score', 0),
            'premium': entry_p, 'strike': entry['strike'], 'iv': entry.get('iv', 0),
            'hour': entry['hour'], 'date': str(entry['date']),
            'asset_class': entry.get('asset_class', ''), 'entry_time': entry_t, 'ps': ps,
        })
    return entries

def sim_tsl(ps, entry_p, be_trigger_pct, trail_pct, init_sl_pct, window_sec):
    init_sl = entry_p * (1 - init_sl_pct / 100)
    be_trigger = entry_p * (1 + be_trigger_pct / 100)
    current_sl = init_sl
    max_p = entry_p
    be_on = False
    exit_p = entry_p
    exit_time = ps[-1][2] if ps else None
    exit_reason = 'TIMEOUT'

    for secs, prem, ts in ps:
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
            exit_time = ts
            exit_reason = 'TSL_WIN' if prem >= entry_p else 'SL_HIT'
            return exit_p, exit_time, exit_reason, max_p
        exit_p = prem
        exit_time = ts

    return exit_p, exit_time, exit_reason, max_p

def run():
    print("Loading signals...")
    signals = load_signals()
    print(f'Loaded {len(signals)} signals')
    entries = build_contracts(signals, max_window=120)
    print(f'{len(entries)} tradeable contracts')

    sym_medians = {}
    for sym in set(e['symbol'] for e in entries):
        prems = [e['premium'] for e in entries if e['symbol'] == sym]
        sym_medians[sym] = np.median(prems) if prems else 0

    all_strategy_trades = {}

    for sname, sconfig in STRATEGIES.items():
        print(f'\nSimulating {sname}: {sconfig["desc"]}')
        filter_func = sconfig['filter']
        be_trig, trail, init_sl, window = sconfig['tsl']
        max_per_day = sconfig['max_per_day']

        filtered = []
        for e in entries:
            med = sym_medians.get(e['symbol'], 999999)
            try:
                if filter_func(e, med):
                    filtered.append(e)
            except: pass

        filtered.sort(key=lambda x: x['entry_time'])
        active = []
        daily_cnt = defaultdict(int)
        last_entry = {}
        for e in filtered:
            sym, date = e['symbol'], e['date']
            dk = f'{sym}_{date}'
            if daily_cnt[dk] >= max_per_day: continue
            if sym in last_entry and (e['entry_time'] - last_entry[sym]).total_seconds() < COOLDOWN_SEC: continue
            active.append(e)
            daily_cnt[dk] += 1
            last_entry[sym] = e['entry_time']

        capital = INITIAL_CAPITAL
        trades = []
        for e in active:
            entry_p = e['premium']
            lot = LOT_SIZES.get(e['symbol'], 50)
            invested = entry_p * lot

            exit_p, exit_time, exit_reason, max_p = sim_tsl(e['ps'], entry_p, be_trig, trail, init_sl, window * 60)

            gross_pnl = (exit_p - entry_p) * lot
            cost = invested * COST_PCT / 100
            net_pnl = gross_pnl - cost
            pnl_pct = net_pnl / invested * 100 if invested > 0 else 0
            capital += net_pnl
            max_gain_pct = (max_p - entry_p) / entry_p * 100

            trades.append({
                'Trade#': len(trades) + 1,
                'Date': e['date'],
                'Symbol': e['symbol'],
                'Strategy': e['strategy'],
                'Signal': e['type'],
                'Strike': e['strike'],
                'Entry_Time': e['entry_time'].strftime('%Y-%m-%d %H:%M:%S'),
                'Exit_Time': exit_time.strftime('%Y-%m-%d %H:%M:%S') if exit_time else '',
                'Entry_Premium': round(entry_p, 2),
                'Exit_Premium': round(exit_p, 2),
                'Lot_Size': lot,
                'Capital_Invested': round(invested, 2),
                'Gross_PnL': round(gross_pnl, 2),
                'Brokerage_Cost': round(cost, 2),
                'Net_PnL': round(net_pnl, 2),
                'PnL_Pct': round(pnl_pct, 2),
                'Capital_After': round(capital, 2),
                'Max_Premium': round(max_p, 2),
                'Max_Gain_Pct': round(max_gain_pct, 2),
                'Exit_Reason': exit_reason,
                'Quality_Score': e['quality_score'],
                'IV': e.get('iv', 0),
                'Result': 'WIN' if net_pnl > 0 else 'LOSS',
            })

        all_strategy_trades[sname] = trades
        wins = sum(1 for t in trades if t['Result'] == 'WIN')
        total = len(trades)
        gw = sum(t['Net_PnL'] for t in trades if t['Net_PnL'] > 0)
        gl = abs(sum(t['Net_PnL'] for t in trades if t['Net_PnL'] <= 0))
        pf = gw / gl if gl > 0 else 99
        print(f'  {total} trades, W={wins} L={total-wins}, WR={wins/total*100:.1f}%, PF={pf:.2f}, Net=Rs {capital-INITIAL_CAPITAL:,.0f}, Final Capital=Rs {capital:,.0f}')

    # Save as JSON for Excel generation
    output = {}
    for sname, trades in all_strategy_trades.items():
        output[sname] = {
            'desc': STRATEGIES[sname]['desc'],
            'trades': trades,
        }
    with open('/root/algo_trading/paper_trades/simulation_trades.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f'\nSaved to /root/algo_trading/paper_trades/simulation_trades.json')

if __name__ == '__main__':
    run()
