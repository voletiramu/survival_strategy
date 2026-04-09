#!/usr/bin/env python3
"""CONSISTENT PROFIT STRATEGY — Focus on reliable small wins.
Problem: Current strategies rely on 3-4 huge wins (fluke crash days).
Solution:
1. MOMENTUM ENTRY: Only enter when premium is ALREADY rising
2. QUICK SCALP: Take 2-3% profit quickly, don't wait for 20%+ home runs
3. TIME EXIT: Kill flat trades early (5-10 min no progress = exit)
4. TIGHT SL: Small losses (3-5%), not 10-15%
"""
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
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df

def build_contracts_with_momentum(df, max_window=60):
    """Build contracts but also compute initial momentum (are premiums rising?)."""
    df['contract'] = df['date'].astype(str) + '|' + df['symbol'] + '|' + df['strike'].astype(str) + '|' + df['type']
    entries = []
    for cname, group in df.groupby('contract'):
        group = group.sort_values('timestamp')
        if len(group) < 10:
            continue
        # Check first 5 signals as "confirmation period" (about 1-2 min)
        confirm_bars = min(5, len(group) // 3)
        confirm = group.iloc[:confirm_bars]
        entry_after = group.iloc[confirm_bars:]
        if entry_after.empty or len(entry_after) < 3:
            continue

        first_prem = confirm.iloc[0]['premium']
        confirm_end_prem = confirm.iloc[-1]['premium']
        momentum = (confirm_end_prem - first_prem) / first_prem * 100

        # Entry is AFTER confirmation period
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
            'contract': cname,
            'symbol': entry_row['symbol'],
            'type': entry_row['type'],
            'strategy': entry_row.get('strategy', ''),
            'quality_score': entry_row.get('quality_score', 0),
            'premium': entry_p,
            'strike': entry_row['strike'],
            'hour': entry_row['hour'],
            'date': str(entry_row['date']),
            'asset_class': entry_row.get('asset_class', ''),
            'entry_time': entry_t,
            'momentum': round(momentum, 2),
            'ps': ps,
        })
    return entries

def sim_scalp(ps, entry_p, target_pct, sl_pct, max_flat_sec, window_sec):
    """Scalp: fixed target, fixed SL, time-exit if no progress.
    max_flat_sec: exit if premium stays within +-0.5% for this many seconds."""
    target = entry_p * (1 + target_pct / 100)
    sl = entry_p * (1 - sl_pct / 100)
    flat_threshold = entry_p * 0.005  # 0.5% = "flat"
    exit_p = entry_p
    exit_time = ps[-1][2] if ps else None
    last_significant_move_sec = 0

    for secs, prem, ts in ps:
        if secs > window_sec:
            break
        # Target hit
        if prem >= target:
            return 'TARGET', target, ts
        # SL hit
        if prem <= sl:
            return 'SL_HIT', sl, ts
        # Check if flat (no progress)
        if abs(prem - entry_p) > flat_threshold:
            last_significant_move_sec = secs
        elif secs - last_significant_move_sec > max_flat_sec:
            return 'FLAT_EXIT', prem, ts

        exit_p = prem
        exit_time = ts

    return 'TIMEOUT', exit_p, exit_time

def sim_scalp_tsl(ps, entry_p, target_pct, sl_pct, be_trigger_pct, trail_pct, max_flat_sec, window_sec):
    """Scalp with TSL: target + initial SL + breakeven lock + trail + flat exit."""
    target = entry_p * (1 + target_pct / 100)
    init_sl = entry_p * (1 - sl_pct / 100)
    be_trigger = entry_p * (1 + be_trigger_pct / 100)
    current_sl = init_sl
    max_p = entry_p
    be_on = False
    flat_threshold = entry_p * 0.005
    last_significant_move_sec = 0
    exit_p = entry_p
    exit_time = ps[-1][2] if ps else None

    for secs, prem, ts in ps:
        if secs > window_sec:
            break
        if prem > max_p:
            max_p = prem
        # Target hit
        if prem >= target:
            return 'TARGET', target, ts
        # Breakeven activation
        if not be_on and prem >= be_trigger:
            be_on = True
            current_sl = entry_p
        # Trail
        if be_on:
            trail = max_p * (1 - trail_pct / 100)
            current_sl = max(current_sl, trail)
        # SL check
        if prem <= current_sl:
            reason = 'TSL_WIN' if prem >= entry_p else 'SL_HIT'
            return reason, prem, ts
        # Flat exit
        if abs(prem - entry_p) > flat_threshold:
            last_significant_move_sec = secs
        elif max_flat_sec > 0 and secs - last_significant_move_sec > max_flat_sec:
            return 'FLAT_EXIT', prem, ts

        exit_p = prem
        exit_time = ts

    return 'TIMEOUT', exit_p, exit_time

# Strategy definitions
STRATEGIES = {
    # === GROUP A: Pure Scalp (fixed target/SL, flat exit) ===
    'A1_Scalp_T2_S3_Flat5m': {
        'desc': 'Scalp 2% target, 3% SL, flat exit 5min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (2, 3, 300, 1800),  # target%, sl%, flat_sec, window_sec
    },
    'A2_Scalp_T3_S3_Flat5m': {
        'desc': 'Scalp 3% target, 3% SL, flat exit 5min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'A3_Scalp_T2_S3_Flat3m': {
        'desc': 'Scalp 2% target, 3% SL, flat exit 3min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (2, 3, 180, 1800),
    },
    'A4_Scalp_T3_S5_Flat5m': {
        'desc': 'Scalp 3% target, 5% SL, flat exit 5min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (3, 5, 300, 1800),
    },
    'A5_Scalp_T5_S5_Flat5m': {
        'desc': 'Scalp 5% target, 5% SL, flat exit 5min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (5, 5, 300, 3600),
    },

    # === GROUP B: Scalp + TSL hybrid (target cap + trailing) ===
    'B1_ScalpTSL_T5_S3_BE2_T1': {
        'desc': 'Target 5%, SL 3%, BE at 2%, trail 1%, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 300, 1800),
    },
    'B2_ScalpTSL_T5_S3_BE2_T1_NoFlat': {
        'desc': 'Target 5%, SL 3%, BE at 2%, trail 1%, no flat exit',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 0, 3600),
    },
    'B3_ScalpTSL_T10_S5_BE3_T1': {
        'desc': 'Target 10%, SL 5%, BE at 3%, trail 1%, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (10, 5, 3, 1, 300, 3600),
    },
    'B4_ScalpTSL_T7_S3_BE2_T1': {
        'desc': 'Target 7%, SL 3%, BE at 2%, trail 1%, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (7, 3, 2, 1, 300, 3600),
    },
    'B5_ScalpTSL_T5_S5_BE3_T2': {
        'desc': 'Target 5%, SL 5%, BE at 3%, trail 2%, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (5, 5, 3, 2, 300, 3600),
    },

    # === GROUP C: Momentum-only filters (stronger momentum required) ===
    'C1_StrongMom_T3_S3': {
        'desc': 'Strong momentum (>0.5%) + Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0.5,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'C2_StrongMom_T5_S3_TSL': {
        'desc': 'Strong momentum (>0.5%) + Target 5%, SL 3%, BE 2%, trail 1%',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0.5,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 300, 1800),
    },
    'C3_VeryStrongMom_T3_S3': {
        'desc': 'Very strong momentum (>1%) + Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 1.0,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },

    # === GROUP D: No momentum filter (baseline comparison) ===
    'D1_NoMom_T3_S3_Flat5m': {
        'desc': 'No momentum filter, Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'D2_NoMom_TSL_T5_S3': {
        'desc': 'No momentum, Target 5%, SL 3%, BE 2%, trail 1%',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 300, 1800),
    },

    # === GROUP E: Quality + Momentum ===
    'E1_Q60_Mom_T3_S3': {
        'desc': 'Q60+ momentum, Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0 and e['quality_score'] >= 60,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'E2_Q60_Mom_TSL_T5_S3': {
        'desc': 'Q60+ momentum, Target 5%, SL 3%, BE 2%, trail 1%',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0 and e['quality_score'] >= 60,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 300, 1800),
    },

    # === GROUP F: Symbol-specific ===
    'F1_BNF_Mom_T3_S3': {
        'desc': 'BANKNIFTY momentum, Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['momentum'] > 0,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'F2_BNF_Mom_TSL_T5_S3': {
        'desc': 'BANKNIFTY momentum, T5 S3 BE2 Trail1',
        'filter': lambda e, med: e['symbol'] == 'BANKNIFTY' and e['hour'] < 11 and e['momentum'] > 0,
        'mode': 'scalp_tsl', 'params': (5, 3, 2, 1, 300, 3600),
    },
    'F3_NIFTY_Mom_T3_S3': {
        'desc': 'NIFTY momentum, Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['symbol'] == 'NIFTY' and e['hour'] < 11 and e['momentum'] > 0,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },
    'F4_EQ_PE_Mom_T3_S3': {
        'desc': 'Equity PE + momentum, Scalp 3/3, flat 5m',
        'filter': lambda e, med: e['asset_class'] == 'equity' and 'BUY_PE' in e['type'] and e['hour'] < 11 and e['momentum'] > 0,
        'mode': 'scalp', 'params': (3, 3, 300, 1800),
    },

    # === GROUP G: Tighter scalps ===
    'G1_Mom_T1.5_S2_Flat3m': {
        'desc': 'Momentum, Target 1.5%, SL 2%, flat 3min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (1.5, 2, 180, 900),
    },
    'G2_Mom_T2_S2_Flat3m': {
        'desc': 'Momentum, Target 2%, SL 2%, flat 3min',
        'filter': lambda e, med: e['hour'] < 11 and e['premium'] < med and e['momentum'] > 0,
        'mode': 'scalp', 'params': (2, 2, 180, 900),
    },
}

def run():
    print("=" * 100)
    print("CONSISTENT PROFIT STRATEGY TEST")
    print("Focus: Reliable small wins, not home runs")
    print("=" * 100)

    signals = load_signals()
    print(f'Loaded {len(signals)} signals')
    entries = build_contracts_with_momentum(signals, max_window=60)
    print(f'{len(entries)} contracts with momentum data')

    # Momentum distribution
    moms = [e['momentum'] for e in entries]
    print(f'Momentum: mean={np.mean(moms):.2f}%, median={np.median(moms):.2f}%')
    print(f'  >0%: {sum(1 for m in moms if m > 0)} ({sum(1 for m in moms if m > 0)/len(moms)*100:.0f}%)')
    print(f'  >0.5%: {sum(1 for m in moms if m > 0.5)} ({sum(1 for m in moms if m > 0.5)/len(moms)*100:.0f}%)')
    print(f'  >1%: {sum(1 for m in moms if m > 1.0)} ({sum(1 for m in moms if m > 1.0)/len(moms)*100:.0f}%)')

    sym_medians = {}
    for sym in set(e['symbol'] for e in entries):
        prems = [e['premium'] for e in entries if e['symbol'] == sym]
        sym_medians[sym] = np.median(prems) if prems else 0

    all_results = {}
    summary_rows = []

    for sname, sconfig in sorted(STRATEGIES.items()):
        filter_func = sconfig['filter']
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
            if daily_cnt[dk] >= 3: continue
            if sym in last_e and (e['entry_time'] - last_e[sym]).total_seconds() < COOLDOWN_SEC: continue
            active.append(e)
            daily_cnt[dk] += 1
            last_e[sym] = e['entry_time']

        if len(active) < 5:
            print(f'  {sname}: SKIP ({len(active)} trades)')
            continue

        capital = INITIAL_CAPITAL
        trades = []
        for e in active:
            entry_p = e['premium']
            lot = LOT_SIZES.get(e['symbol'], 50)
            invested = entry_p * lot

            if sconfig['mode'] == 'scalp':
                t_pct, s_pct, flat_sec, win_sec = sconfig['params']
                reason, exit_p, exit_t = sim_scalp(e['ps'], entry_p, t_pct, s_pct, flat_sec, win_sec)
            else:  # scalp_tsl
                t_pct, s_pct, be_pct, trail_pct, flat_sec, win_sec = sconfig['params']
                reason, exit_p, exit_t = sim_scalp_tsl(e['ps'], entry_p, t_pct, s_pct, be_pct, trail_pct, flat_sec, win_sec)

            gross = (exit_p - entry_p) * lot
            cost = invested * COST_PCT / 100
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
                'Capital_After': round(capital, 2), 'Exit_Reason': reason,
                'Momentum': e['momentum'], 'Result': 'WIN' if net > 0 else 'LOSS',
            })

        all_results[sname] = {'desc': sconfig['desc'], 'trades': trades}

        # Stats
        total = len(trades)
        wins = sum(1 for t in trades if t['Result'] == 'WIN')
        pnls = sorted([t['Net_PnL'] for t in trades], reverse=True)
        gw = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf = gw / gl if gl > 0 else 99
        net_pnl = sum(pnls)
        wr = wins / total * 100

        # Consistency check: remove top 3
        pnl_no_top3 = sum(pnls[3:])
        top3_pct = sum(pnls[:3]) / net_pnl * 100 if net_pnl != 0 else 0

        # Daily consistency
        daily = defaultdict(float)
        for t in trades:
            daily[t['Date']] += t['Net_PnL']
        profit_days = sum(1 for v in daily.values() if v > 0)
        day_pct = profit_days / len(daily) * 100 if daily else 0

        # Exit reason breakdown
        exits = defaultdict(int)
        for t in trades:
            exits[t['Exit_Reason']] += 1

        marker = "***" if wr >= 50 and pf >= 1.3 and pnl_no_top3 > 0 else "   "
        print(f'{marker} {sname:35s}: {total:3d} trades, WR={wr:4.0f}%, PF={pf:4.2f}, PnL=Rs {net_pnl:>8,.0f} | Top3={top3_pct:4.0f}% | NoTop3=Rs {pnl_no_top3:>7,.0f} | Days={day_pct:.0f}%P | {dict(exits)}')

        summary_rows.append({
            'strategy': sname, 'desc': sconfig['desc'],
            'trades': total, 'wins': wins, 'losses': total - wins,
            'wr': round(wr, 1), 'pf': round(pf, 2), 'net_pnl': round(net_pnl),
            'no_top3_pnl': round(pnl_no_top3), 'top3_pct': round(top3_pct, 1),
            'profit_day_pct': round(day_pct, 1),
            'avg_win': round(gw / wins) if wins > 0 else 0,
            'avg_loss': round(gl / (total - wins)) if total - wins > 0 else 0,
        })

    # Save trades JSON
    with open('/root/algo_trading/paper_trades/consistent_strategy_trades.json', 'w') as f:
        json.dump(all_results, f, indent=2, default=str)

    # Summary sorted by CONSISTENCY (profitable even without top 3)
    print('\n' + '=' * 120)
    print('CONSISTENCY RANKING — Sorted by "PnL without top 3 trades" (the REAL test)')
    print('=' * 120)
    sdf = sorted(summary_rows, key=lambda x: x['no_top3_pnl'], reverse=True)
    print(f'  {"Strategy":35s} {"#":>4s} {"WR%":>5s} {"PF":>5s} {"NetPnL":>10s} {"NoTop3":>10s} {"Top3%":>6s} {"ProfDays":>8s} {"AvgW":>7s} {"AvgL":>7s}')
    print(f'  {"-"*105}')
    for r in sdf:
        m = "**" if r['no_top3_pnl'] > 0 and r['wr'] >= 45 else "  "
        print(f'{m}{r["strategy"]:35s} {r["trades"]:4d} {r["wr"]:4.0f}% {r["pf"]:4.2f} Rs{r["net_pnl"]:>8,.0f} Rs{r["no_top3_pnl"]:>8,.0f} {r["top3_pct"]:5.0f}% {r["profit_day_pct"]:6.0f}%P {r["avg_win"]:>6,.0f} {r["avg_loss"]:>6,.0f}')

    # Find the TRULY consistent ones
    consistent = [r for r in sdf if r['no_top3_pnl'] > 0 and r['wr'] >= 45]
    if consistent:
        print(f'\n*** {len(consistent)} TRULY CONSISTENT STRATEGIES (profitable without top 3, WR>=45%) ***')
        for r in consistent:
            print(f'  {r["strategy"]:35s}: WR={r["wr"]}%, PF={r["pf"]}, PnL=Rs {r["net_pnl"]:,.0f}, NoTop3=Rs {r["no_top3_pnl"]:,.0f}, ProfitDays={r["profit_day_pct"]}%')
    else:
        print('\nNo strategy is profitable after removing top 3 trades WITH WR>=45%')
        best = [r for r in sdf if r['no_top3_pnl'] > 0]
        if best:
            print(f'But {len(best)} strategies ARE profitable without top 3:')
            for r in best[:10]:
                print(f'  {r["strategy"]:35s}: WR={r["wr"]}%, PF={r["pf"]}, PnL=Rs {r["net_pnl"]:,.0f}, NoTop3=Rs {r["no_top3_pnl"]:,.0f}')

    print('\nDone!')

if __name__ == '__main__':
    run()
