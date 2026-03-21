#!/usr/bin/env python3
"""CONSISTENT PROFIT STRATEGY v2 — Deep redesign after v1 failure.

Key insights from v1:
1. 1.2% flat cost kills scalping (3% target - 1.2% = 1.8% gain, but 3% SL + 1.2% = 4.2% loss)
2. Momentum filter (>0%) cuts trades from 678 to 183 — too few
3. Only 26 trades in 33 days is not enough for consistency

v2 Changes:
1. ACTUAL cost model: Rs 40 flat brokerage + percentage fees (not 1.2% flat)
   - Higher premium trades = lower % cost
2. Shorter confirmation (2-3 bars, not 5)
3. Test HIGH premium too (not just low premium)
4. Wider windows (up to 120 min)
5. More trades: lower cooldown (300s), more per day (5)
6. Multiple momentum thresholds including no-filter
7. HOLD LONGER — let TSL work, no tiny targets
"""
import pandas as pd
import numpy as np
import glob, json
from datetime import timedelta
from collections import defaultdict

EQUITY_DIR = '/root/algo_trading/paper_trades'
COMMODITY_DIR = '/root/algo_trading/paper_trades_commodity'
INITIAL_CAPITAL = 300000
LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
             'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 100}

def actual_cost(entry_p, exit_p, lot):
    """Actual Zerodha cost model for options (NOT flat 1.2%)."""
    buy_val = entry_p * lot
    sell_val = exit_p * lot
    # Brokerage: Rs 20 per order (buy + sell)
    brokerage = 40
    # STT: 0.0625% on sell side
    stt = sell_val * 0.000625
    # Exchange txn: 0.0503% per side (NSE F&O)
    exchange = (buy_val + sell_val) * 0.000503
    # SEBI charges: Rs 10 per crore
    sebi = (buy_val + sell_val) * 0.000001
    # GST: 18% on (brokerage + exchange + SEBI)
    gst = (brokerage + exchange + sebi) * 0.18
    # Stamp duty: 0.003% on buy side
    stamp = buy_val * 0.00003
    total = brokerage + stt + exchange + sebi + gst + stamp
    return round(total, 2)

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

def build_contracts(df, confirm_bars=3, max_window=120):
    """Build contracts with variable confirmation period."""
    df['contract'] = df['date'].astype(str) + '|' + df['symbol'] + '|' + df['strike'].astype(str) + '|' + df['type']
    entries = []
    for cname, group in df.groupby('contract'):
        group = group.sort_values('timestamp')
        if len(group) < confirm_bars + 3:
            continue
        # Confirmation period
        confirm = group.iloc[:confirm_bars]
        entry_after = group.iloc[confirm_bars:]
        if len(entry_after) < 2:
            continue

        first_prem = confirm.iloc[0]['premium']
        confirm_end_prem = confirm.iloc[-1]['premium']
        momentum = (confirm_end_prem - first_prem) / first_prem * 100

        entry_row = entry_after.iloc[0]
        entry_p = entry_row['premium']
        entry_t = entry_row['timestamp']

        cutoff = entry_t + timedelta(minutes=max_window)
        future = entry_after.iloc[1:]
        future = future[future['timestamp'] <= cutoff]
        if len(future) < 2:
            continue

        ps = [((row['timestamp'] - entry_t).total_seconds(), row['premium'], row['timestamp']) for _, row in future.iterrows()]

        entries.append({
            'contract': cname, 'symbol': entry_row['symbol'], 'type': entry_row['type'],
            'strategy': entry_row.get('strategy', ''), 'quality_score': entry_row.get('quality_score', 0),
            'premium': entry_p, 'strike': entry_row['strike'], 'iv': entry_row.get('iv', 0),
            'hour': entry_row['hour'], 'date': str(entry_row['date']),
            'asset_class': entry_row.get('asset_class', ''), 'entry_time': entry_t,
            'momentum': round(momentum, 2), 'ps': ps,
        })
    return entries

def sim_tsl(ps, entry_p, be_trigger_pct, trail_pct, init_sl_pct, window_sec):
    """Pure TSL (no target cap — let winners run)."""
    init_sl = entry_p * (1 - init_sl_pct / 100)
    be_trigger = entry_p * (1 + be_trigger_pct / 100)
    current_sl = init_sl
    max_p = entry_p
    be_on = False
    exit_p = entry_p
    exit_time = ps[-1][2] if ps else None

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
            reason = 'TSL_WIN' if prem >= entry_p else 'SL_HIT'
            return exit_p, exit_time, reason, max_p
        exit_p = prem
        exit_time = ts

    return exit_p, exit_time, 'TIMEOUT', max_p

def sim_target_tsl(ps, entry_p, target_pct, be_trigger_pct, trail_pct, init_sl_pct, window_sec):
    """TSL with target cap."""
    target = entry_p * (1 + target_pct / 100)
    init_sl = entry_p * (1 - init_sl_pct / 100)
    be_trigger = entry_p * (1 + be_trigger_pct / 100)
    current_sl = init_sl
    max_p = entry_p
    be_on = False
    exit_p = entry_p
    exit_time = ps[-1][2] if ps else None

    for secs, prem, ts in ps:
        if secs > window_sec: break
        if prem > max_p: max_p = prem
        if prem >= target:
            return target, ts, 'TARGET', max_p
        if not be_on and prem >= be_trigger:
            be_on = True
            current_sl = entry_p
        if be_on:
            trail = max_p * (1 - trail_pct / 100)
            current_sl = max(current_sl, trail)
        if prem <= current_sl:
            exit_p = prem
            exit_time = ts
            reason = 'TSL_WIN' if prem >= entry_p else 'SL_HIT'
            return exit_p, exit_time, reason, max_p
        exit_p = prem
        exit_time = ts

    return exit_p, exit_time, 'TIMEOUT', max_p

STRATEGIES = {
    # ===== GROUP 1: Pure TSL, no target cap, let winners run =====
    # Momentum + AM + LowPrem (proven best filter)
    'T1_Mom_TSL_BE3_T1_SL5': {
        'desc': 'Momentum AM LowP, TSL(BE3,T1,SL5) 60min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'T2_Mom_TSL_BE3_T1_SL5_120m': {
        'desc': 'Momentum AM LowP, TSL(BE3,T1,SL5) 120min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'tsl', 'params': (3, 1, 5, 7200), 'cooldown': 300, 'max_day': 5,
    },
    'T3_Mom_TSL_BE5_T2_SL5': {
        'desc': 'Momentum AM LowP, TSL(BE5,T2,SL5) 60min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'tsl', 'params': (5, 2, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'T4_Mom_TSL_BE2_T1_SL3': {
        'desc': 'Momentum AM LowP, TSL(BE2,T1,SL3) tight',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'tsl', 'params': (2, 1, 3, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 2: No momentum filter (more trades) =====
    'N1_NoMom_TSL_BE3_T1_SL5': {
        'desc': 'No momentum AM LowP, TSL(BE3,T1,SL5) 60min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'N2_NoMom_TSL_BE3_T1_SL5_120m': {
        'desc': 'No momentum AM LowP, TSL(BE3,T1,SL5) 120min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (3, 1, 5, 7200), 'cooldown': 300, 'max_day': 5,
    },
    'N3_NoMom_TSL_BE2_T1_SL3': {
        'desc': 'No momentum AM LowP, TSL(BE2,T1,SL3) tight',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (2, 1, 3, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 3: TSL with target cap (lock profits at 7-10%) =====
    'TG1_Mom_Target7_BE3_T1_SL5': {
        'desc': 'Momentum AM LowP, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'TG2_Mom_Target10_BE3_T1_SL5': {
        'desc': 'Momentum AM LowP, Target10% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'sim': 'target_tsl', 'params': (10, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'TG3_NoMom_Target7_BE3_T1_SL5': {
        'desc': 'No momentum AM LowP, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'TG4_NoMom_Target10_BE3_T1_SL5': {
        'desc': 'No momentum AM LowP, Target10% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'target_tsl', 'params': (10, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'TG5_NoMom_Target5_BE2_T1_SL3': {
        'desc': 'No momentum AM LowP, Target5% TSL(BE2,T1,SL3)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'target_tsl', 'params': (5, 2, 1, 3, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 4: All day (not just morning) =====
    'A1_AllDay_TSL_BE3_T1_SL5': {
        'desc': 'All day LowP, TSL(BE3,T1,SL5) 60min',
        'filter': lambda e, med: e['premium'] < med,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'A2_AllDay_Target7_BE3_T1_SL5': {
        'desc': 'All day LowP, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['premium'] < med,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 5: HIGH premium (lower % cost) =====
    'H1_HighP_TSL_BE3_T1_SL5': {
        'desc': 'AM HighPrem, TSL(BE3,T1,SL5) 60min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] >= med,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'H2_HighP_Target7_BE3_T1_SL5': {
        'desc': 'AM HighPrem, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] >= med,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 6: BANKNIFTY specific (best symbol historically) =====
    'B1_BNF_TSL_BE3_T1_SL5': {
        'desc': 'BANKNIFTY AM, TSL(BE3,T1,SL5) 60min',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'B2_BNF_Target7_BE3_T1_SL5': {
        'desc': 'BANKNIFTY AM, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'B3_BNF_Mom_TSL_BE3_T1_SL5': {
        'desc': 'BANKNIFTY Momentum AM, TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['momentum'] > 0,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'B4_BNF_LowP_TSL_BE5_T2_SL10': {
        'desc': 'BANKNIFTY AM LowP, TSL(BE5,T2,SL10) 120min',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (5, 2, 10, 7200), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 7: PE only (proven edge in directional) =====
    'P1_PE_TSL_BE3_T1_SL5': {
        'desc': 'PE Only AM LowP, TSL(BE3,T1,SL5)',
        'filter': lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'P2_PE_Target7_BE3_T1_SL5': {
        'desc': 'PE Only AM LowP, Target7% TSL(BE3,T1,SL5)',
        'filter': lambda e, med: 'BUY_PE' in e['type'] and e['hour'] < 11 and e['premium'] < med,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 8: Very tight SL (minimize losses) =====
    'S1_TightSL2_BE2_T1': {
        'desc': 'AM LowP, TSL(BE2,T1,SL2) ultra-tight',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'tsl', 'params': (2, 1, 2, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'S2_TightSL2_Target5': {
        'desc': 'AM LowP, Target5% TSL(BE2,T1,SL2)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'sim': 'target_tsl', 'params': (5, 2, 1, 2, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 9: Quality filter variants =====
    'Q1_Q60_TSL_BE3_T1_SL5': {
        'desc': 'Q60+ AM LowP, TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'Q2_Q40_TSL_BE3_T1_SL5': {
        'desc': 'Q40+ AM LowP, TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 40,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },

    # ===== GROUP 10: Multi-symbol equity (proven in v1) =====
    'M1_MULTI_Q60_TSL_BE3_T1_SL5': {
        'desc': 'NIF+BNF+SEN Q60+ AM LowP TSL(BE3,T1,SL5)',
        'filter': lambda e, med: e['symbol'] in ['NIFTY','BANKNIFTY','SENSEX'] and e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60,
        'sim': 'tsl', 'params': (3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
    'M2_MULTI_Q60_Target7': {
        'desc': 'NIF+BNF+SEN Q60+ AM LowP Target7% TSL',
        'filter': lambda e, med: e['symbol'] in ['NIFTY','BANKNIFTY','SENSEX'] and e['hour'] < 11 and e['premium'] < med and e['quality_score'] >= 60,
        'sim': 'target_tsl', 'params': (7, 3, 1, 5, 3600), 'cooldown': 300, 'max_day': 5,
    },
}


def run():
    print("=" * 120)
    print("CONSISTENT PROFIT STRATEGY v2 — Actual Cost Model + Better Filters")
    print("=" * 120)

    signals = load_signals()
    print(f'Loaded {len(signals)} signals')

    # Build contracts with shorter confirmation (3 bars instead of 5)
    entries = build_contracts(signals, confirm_bars=3, max_window=120)
    print(f'{len(entries)} contracts (3-bar confirmation, 120min window)')

    # Stats
    moms = [e['momentum'] for e in entries]
    print(f'Momentum: mean={np.mean(moms):.2f}%, median={np.median(moms):.2f}%')
    print(f'  >0%: {sum(1 for m in moms if m > 0)} ({sum(1 for m in moms if m > 0)/len(moms)*100:.0f}%)')
    prems = [e['premium'] for e in entries]
    print(f'Premium: mean={np.mean(prems):.0f}, median={np.median(prems):.0f}')

    sym_medians = {}
    for sym in set(e['symbol'] for e in entries):
        sp = [e['premium'] for e in entries if e['symbol'] == sym]
        sym_medians[sym] = np.median(sp) if sp else 0

    # Show cost impact at different premium levels
    print('\n--- COST MODEL (Rs 40 flat brokerage) ---')
    for p_val in [50, 100, 200, 500, 1000]:
        lot = 30  # BNF
        c = actual_cost(p_val, p_val, lot)
        print(f'  Premium Rs{p_val:>5}, Lot={lot}: Cost=Rs {c:.0f} ({c/(p_val*lot)*100:.2f}%)')

    all_results = {}
    summary_rows = []

    for sname, sconfig in sorted(STRATEGIES.items()):
        filter_func = sconfig['filter']
        cooldown = sconfig['cooldown']
        max_day = sconfig['max_day']

        filtered = []
        for e in entries:
            med = sym_medians.get(e['symbol'], 999999)
            try:
                if filter_func(e, med): filtered.append(e)
            except: pass

        filtered.sort(key=lambda x: x['entry_time'])
        active = []
        daily_cnt = defaultdict(int)
        last_e = {}
        for e in filtered:
            sym, date = e['symbol'], e['date']
            dk = f'{sym}_{date}'
            if daily_cnt[dk] >= max_day: continue
            if sym in last_e and (e['entry_time'] - last_e[sym]).total_seconds() < cooldown: continue
            active.append(e)
            daily_cnt[dk] += 1
            last_e[sym] = e['entry_time']

        if len(active) < 5:
            continue

        capital = INITIAL_CAPITAL
        trades = []
        for e in active:
            entry_p = e['premium']
            lot = LOT_SIZES.get(e['symbol'], 50)
            invested = entry_p * lot

            if sconfig['sim'] == 'tsl':
                be, trail, sl, win = sconfig['params']
                exit_p, exit_t, reason, max_p = sim_tsl(e['ps'], entry_p, be, trail, sl, win)
            else:  # target_tsl
                tgt, be, trail, sl, win = sconfig['params']
                exit_p, exit_t, reason, max_p = sim_target_tsl(e['ps'], entry_p, tgt, be, trail, sl, win)

            gross = (exit_p - entry_p) * lot
            cost = actual_cost(entry_p, exit_p, lot)
            net = gross - cost
            pnl_pct = net / invested * 100 if invested > 0 else 0
            capital += net

            trades.append({
                'Trade#': len(trades) + 1, 'Date': e['date'], 'Symbol': e['symbol'],
                'Strategy': e['strategy'], 'Signal': e['type'], 'Strike': e['strike'],
                'Entry_Time': e['entry_time'].strftime('%Y-%m-%d %H:%M:%S'),
                'Exit_Time': exit_t.strftime('%Y-%m-%d %H:%M:%S') if exit_t else '',
                'Entry_Premium': round(entry_p, 2), 'Exit_Premium': round(exit_p, 2),
                'Lot_Size': lot, 'Capital_Invested': round(invested, 2),
                'Gross_PnL': round(gross, 2), 'Brokerage_Cost': round(cost, 2),
                'Net_PnL': round(net, 2), 'PnL_Pct': round(pnl_pct, 2),
                'Capital_After': round(capital, 2), 'Max_Premium': round(max_p, 2),
                'Max_Gain_Pct': round((max_p - entry_p) / entry_p * 100, 2),
                'Exit_Reason': reason, 'Momentum': e['momentum'],
                'Quality_Score': e['quality_score'], 'IV': e.get('iv', 0),
                'Result': 'WIN' if net > 0 else 'LOSS',
            })

        all_results[sname] = {'desc': sconfig['desc'], 'trades': trades}

        total = len(trades)
        wins = sum(1 for t in trades if t['Result'] == 'WIN')
        pnls = sorted([t['Net_PnL'] for t in trades], reverse=True)
        gw = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf = gw / gl if gl > 0 else 99
        net_pnl = sum(pnls)
        wr = wins / total * 100

        # Consistency: remove top 3
        pnl_no_top3 = sum(pnls[3:])
        top3_pct = sum(pnls[:3]) / net_pnl * 100 if net_pnl != 0 else 0

        # Remove top 5 too
        pnl_no_top5 = sum(pnls[5:])

        # Daily
        daily = defaultdict(float)
        for t in trades:
            daily[t['Date']] += t['Net_PnL']
        profit_days = sum(1 for v in daily.values() if v > 0)
        day_pct = profit_days / len(daily) * 100 if daily else 0

        # Exit reasons
        exits = defaultdict(int)
        for t in trades:
            exits[t['Exit_Reason']] += 1

        # Avg cost as %
        avg_cost_pct = np.mean([t['Brokerage_Cost'] / t['Capital_Invested'] * 100 for t in trades])

        marker = "***" if pnl_no_top3 > 0 and wr >= 40 else "   "
        print(f'{marker} {sname:35s}: {total:3d} trades, WR={wr:4.0f}%, PF={pf:4.2f}, PnL=Rs {net_pnl:>8,.0f} | NoTop3=Rs {pnl_no_top3:>7,.0f} | NoTop5=Rs {pnl_no_top5:>7,.0f} | Days={day_pct:.0f}%P | Cost={avg_cost_pct:.2f}% | {dict(exits)}')

        summary_rows.append({
            'strategy': sname, 'desc': sconfig['desc'],
            'trades': total, 'wins': wins, 'losses': total - wins,
            'wr': round(wr, 1), 'pf': round(pf, 2), 'net_pnl': round(net_pnl),
            'no_top3_pnl': round(pnl_no_top3), 'no_top5_pnl': round(pnl_no_top5),
            'top3_pct': round(top3_pct, 1), 'profit_day_pct': round(day_pct, 1),
            'avg_win': round(gw / wins) if wins > 0 else 0,
            'avg_loss': round(gl / (total - wins)) if total - wins > 0 else 0,
            'avg_cost_pct': round(avg_cost_pct, 2),
        })

    # Save trades
    with open('/root/algo_trading/paper_trades/consistent_v2_trades.json', 'w') as f:
        json.dump(all_results, f, indent=2, default=str)

    # Rankings
    print('\n' + '=' * 140)
    print('CONSISTENCY RANKING — Sorted by "PnL without top 3 trades"')
    print('=' * 140)
    sdf = sorted(summary_rows, key=lambda x: x['no_top3_pnl'], reverse=True)
    print(f'  {"Strategy":35s} {"#":>4s} {"WR%":>5s} {"PF":>5s} {"NetPnL":>10s} {"NoTop3":>10s} {"NoTop5":>10s} {"Top3%":>6s} {"ProfDays":>8s} {"AvgW":>7s} {"AvgL":>7s} {"Cost%":>6s}')
    print(f'  {"-"*125}')
    for r in sdf:
        m = "**" if r['no_top3_pnl'] > 0 else "  "
        print(f'{m}{r["strategy"]:35s} {r["trades"]:4d} {r["wr"]:4.0f}% {r["pf"]:4.2f} Rs{r["net_pnl"]:>8,.0f} Rs{r["no_top3_pnl"]:>8,.0f} Rs{r["no_top5_pnl"]:>8,.0f} {r["top3_pct"]:5.0f}% {r["profit_day_pct"]:6.0f}%P {r["avg_win"]:>6,.0f} {r["avg_loss"]:>6,.0f} {r["avg_cost_pct"]:5.2f}%')

    # Truly consistent
    consistent = [r for r in sdf if r['no_top3_pnl'] > 0]
    print(f'\n*** STRATEGIES PROFITABLE WITHOUT TOP 3: {len(consistent)} ***')
    if consistent:
        for r in consistent:
            print(f'  {r["strategy"]:35s}: WR={r["wr"]}%, PF={r["pf"]}, PnL=Rs {r["net_pnl"]:,.0f}, NoTop3=Rs {r["no_top3_pnl"]:,.0f}, NoTop5=Rs {r["no_top5_pnl"]:,.0f}')

    # Also check: profitable after removing top 5?
    super_consistent = [r for r in sdf if r['no_top5_pnl'] > 0]
    if super_consistent:
        print(f'\n*** ULTRA-CONSISTENT (profitable without TOP 5): {len(super_consistent)} ***')
        for r in super_consistent:
            print(f'  {r["strategy"]:35s}: WR={r["wr"]}%, PF={r["pf"]}, NoTop5=Rs {r["no_top5_pnl"]:,.0f}')

    print('\nDone!')

if __name__ == '__main__':
    run()
