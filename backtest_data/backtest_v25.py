#!/usr/bin/env python3
"""
v2.5 Backtest Engine — Replay Feb 27 signals + trades under different parameter configs.
Compares: ACTUAL (v2.3.2) vs OPTIMIZED (v2.5 proposed changes).

Uses real signals CSV and real closed trades from portfolio_state.json.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# 1. LOAD REAL TRADE DATA (what actually happened on Feb 27)
# ============================================================
def load_trades(json_path, date_str='2026-02-27'):
    with open(json_path) as f:
        data = json.load(f)
    closed = data.get('closed_trades', [])
    trades = []
    for t in closed:
        ts = t.get('timestamp', t.get('entry_time', ''))
        exit_ts = t.get('exit_time', t.get('closed_at', ''))
        if date_str in ts or date_str in exit_ts:
            details = t.get('details', {}) if isinstance(t.get('details'), dict) else {}
            trades.append({
                'id': t.get('id', ''),
                'strategy': t.get('strategy', ''),
                'symbol': t.get('symbol', t.get('commodity', '')),
                'signal_type': t.get('signal_type', ''),
                'strike': t.get('strike', 0),
                'entry_premium': t.get('entry_premium', 0),
                'exit_premium': t.get('exit_premium', t.get('exit_price', 0)),
                'pnl': t.get('pnl', 0),
                'exit_reason': t.get('exit_reason', ''),
                'is_sell': t.get('is_sell', False),
                'lot_size': t.get('lot_size', 0),
                'multiplier': t.get('multiplier', 1),
                'quality_score': t.get('quality_score', details.get('quality_score', 0)),
                'entry_time': ts,
                'exit_time': exit_ts,
                'target': details.get('target', 0),
                'sl': details.get('sl', 0),
            })
    return trades, data.get('capital', 300000)

# ============================================================
# 2. LOAD SIGNALS CSV (all generated signals, including skipped)
# ============================================================
def load_signals(csv_path):
    signals = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                signals.append({
                    'timestamp': row.get('timestamp', ''),
                    'strategy': row.get('strategy', ''),
                    'symbol': row.get('symbol', row.get('commodity', '')),
                    'type': row.get('type', ''),
                    'strike': float(row.get('strike', 0)),
                    'premium': float(row.get('premium', 0)),
                    'delta': float(row.get('delta', 0)),
                    'spot': float(row.get('spot', 0)),
                    'dte': int(float(row.get('dte', 1))),
                    'reason': row.get('reason', ''),
                    'target': float(row.get('target', 0)),
                    'sl': float(row.get('sl', 0)),
                })
            except (ValueError, KeyError):
                continue
    return signals

# ============================================================
# 3. BACKTEST SCENARIOS
# ============================================================

def analyze_actual_trades(trades, market_label):
    """Analyze what actually happened."""
    print(f"\n{'='*80}")
    print(f"  ACTUAL RESULTS — {market_label} (Feb 27, v2.3.2)")
    print(f"{'='*80}")

    total_pnl = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / max(len(trades), 1) * 100

    print(f"  Total trades: {len(trades)}")
    print(f"  Wins: {len(wins)}, Losses: {len(losses)}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Total PnL: Rs {total_pnl:,.2f}")
    print(f"  Avg Win: Rs {sum(t['pnl'] for t in wins)/max(len(wins),1):,.2f}")
    print(f"  Avg Loss: Rs {sum(t['pnl'] for t in losses)/max(len(losses),1):,.2f}")

    if wins and losses:
        pf = abs(sum(t['pnl'] for t in wins)) / abs(sum(t['pnl'] for t in losses))
        print(f"  Profit Factor: {pf:.2f}")

    # By strategy
    print(f"\n  {'Strategy':<25s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s} {'AvgWin':>10s} {'AvgLoss':>10s}")
    print(f"  {'-'*70}")
    strats = defaultdict(list)
    for t in trades:
        strats[t['strategy']].append(t)
    for s, st_trades in sorted(strats.items(), key=lambda x: sum(t['pnl'] for t in x[1]), reverse=True):
        st_wins = [t for t in st_trades if t['pnl'] > 0]
        st_losses = [t for t in st_trades if t['pnl'] <= 0]
        st_pnl = sum(t['pnl'] for t in st_trades)
        st_wr = len(st_wins) / max(len(st_trades), 1) * 100
        avg_w = sum(t['pnl'] for t in st_wins) / max(len(st_wins), 1)
        avg_l = sum(t['pnl'] for t in st_losses) / max(len(st_losses), 1)
        print(f"  {s:<25s} {len(st_trades):>6d} {st_wr:>5.1f}% {st_pnl:>11,.2f} {avg_w:>10,.2f} {avg_l:>10,.2f}")

    # By exit reason
    print(f"\n  {'Exit Reason':<25s} {'Count':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*50}")
    reasons = defaultdict(list)
    for t in trades:
        reasons[t['exit_reason']].append(t)
    for r, rt in sorted(reasons.items(), key=lambda x: len(x[1]), reverse=True):
        r_wins = len([t for t in rt if t['pnl'] > 0])
        r_pnl = sum(t['pnl'] for t in rt)
        r_wr = r_wins / max(len(rt), 1) * 100
        print(f"  {r:<25s} {len(rt):>6d} {r_wr:>5.1f}% {r_pnl:>11,.2f}")

    # By symbol
    print(f"\n  {'Symbol':<15s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*40}")
    symbols = defaultdict(list)
    for t in trades:
        symbols[t['symbol']].append(t)
    for sym, st in sorted(symbols.items(), key=lambda x: sum(t['pnl'] for t in x[1]), reverse=True):
        s_wins = len([t for t in st if t['pnl'] > 0])
        s_pnl = sum(t['pnl'] for t in st)
        s_wr = s_wins / max(len(st), 1) * 100
        print(f"  {sym:<15s} {len(st):>6d} {s_wr:>5.1f}% {s_pnl:>11,.2f}")

    # By signal type
    print(f"\n  {'Signal Type':<25s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*50}")
    sig_types = defaultdict(list)
    for t in trades:
        sig_types[t['signal_type']].append(t)
    for st_type, st in sorted(sig_types.items(), key=lambda x: sum(t['pnl'] for t in x[1]), reverse=True):
        s_wins = len([t for t in st if t['pnl'] > 0])
        s_pnl = sum(t['pnl'] for t in st)
        s_wr = s_wins / max(len(st), 1) * 100
        print(f"  {st_type:<25s} {len(st):>6d} {s_wr:>5.1f}% {s_pnl:>11,.2f}")

    # By entry hour
    print(f"\n  {'Hour':<8s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*35}")
    hours = defaultdict(list)
    for t in trades:
        try:
            hr = int(t['entry_time'][11:13])
            hours[hr].append(t)
        except:
            pass
    for h in sorted(hours.keys()):
        ht = hours[h]
        h_wins = len([t for t in ht if t['pnl'] > 0])
        h_pnl = sum(t['pnl'] for t in ht)
        h_wr = h_wins / max(len(ht), 1) * 100
        print(f"  {h:02d}:00   {len(ht):>6d} {h_wr:>5.1f}% {h_pnl:>11,.2f}")

    return total_pnl, win_rate, len(trades)


def simulate_scenario(trades, signals, scenario_name, filters):
    """
    Simulate a scenario by filtering out trades based on rules.
    filters dict:
      - remove_strategies: list of strategy names to exclude
      - min_score: minimum quality score
      - remove_exit_reasons: list of exit reasons to exclude
      - min_premium: minimum entry premium
      - max_hours: max hours before time exit
      - recalc_pnl: dict of {exit_reason: multiplier} to adjust PnL
    """
    print(f"\n{'='*80}")
    print(f"  SCENARIO: {scenario_name}")
    print(f"{'='*80}")

    removed_strategies = filters.get('remove_strategies', [])
    min_score = filters.get('min_score', 0)
    min_premium = filters.get('min_premium', 0)

    filtered = []
    removed = defaultdict(lambda: {'count': 0, 'pnl_saved': 0})

    for t in trades:
        # Remove specific strategies
        if any(rs in t['strategy'] for rs in removed_strategies):
            removed[f"Strategy: {t['strategy']}"]['count'] += 1
            removed[f"Strategy: {t['strategy']}"]['pnl_saved'] += t['pnl']
            continue

        # Quality score filter
        if min_score > 0 and t['quality_score'] and float(t['quality_score']) < min_score:
            removed[f"Score < {min_score}"]['count'] += 1
            removed[f"Score < {min_score}"]['pnl_saved'] += t['pnl']
            continue

        # Minimum premium filter
        if min_premium > 0 and not t['is_sell'] and t['entry_premium'] < min_premium:
            removed[f"Premium < {min_premium}"]['count'] += 1
            removed[f"Premium < {min_premium}"]['pnl_saved'] += t['pnl']
            continue

        filtered.append(t)

    # Report removals
    total_removed = sum(r['count'] for r in removed.values())
    total_pnl_saved = sum(r['pnl_saved'] for r in removed.values())

    print(f"\n  Filters applied:")
    for reason, r in sorted(removed.items(), key=lambda x: x[1]['count'], reverse=True):
        print(f"    {reason}: {r['count']} trades removed (PnL impact: Rs {r['pnl_saved']:,.2f})")
    print(f"    TOTAL REMOVED: {total_removed} trades (PnL saved/lost: Rs {total_pnl_saved:,.2f})")

    if not filtered:
        print(f"\n  No trades remaining after filtering!")
        return 0, 0, 0

    # Analyze filtered trades
    total_pnl = sum(t['pnl'] for t in filtered)
    wins = [t for t in filtered if t['pnl'] > 0]
    losses = [t for t in filtered if t['pnl'] <= 0]
    win_rate = len(wins) / max(len(filtered), 1) * 100

    print(f"\n  Remaining trades: {len(filtered)}")
    print(f"  Wins: {len(wins)}, Losses: {len(losses)}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Total PnL: Rs {total_pnl:,.2f}")
    print(f"  Avg Win: Rs {sum(t['pnl'] for t in wins)/max(len(wins),1):,.2f}")
    print(f"  Avg Loss: Rs {sum(t['pnl'] for t in losses)/max(len(losses),1):,.2f}")

    if wins and losses:
        pf = abs(sum(t['pnl'] for t in wins)) / abs(sum(t['pnl'] for t in losses))
        print(f"  Profit Factor: {pf:.2f}")

    # Strategy breakdown of remaining
    print(f"\n  {'Strategy':<25s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*50}")
    strats = defaultdict(list)
    for t in filtered:
        strats[t['strategy']].append(t)
    for s, st_trades in sorted(strats.items(), key=lambda x: sum(t['pnl'] for t in x[1]), reverse=True):
        st_wins = len([t for t in st_trades if t['pnl'] > 0])
        st_pnl = sum(t['pnl'] for t in st_trades)
        st_wr = st_wins / max(len(st_trades), 1) * 100
        print(f"  {s:<25s} {len(st_trades):>6d} {st_wr:>5.1f}% {st_pnl:>11,.2f}")

    return total_pnl, win_rate, len(filtered)


def analyze_signals(signals, market_label):
    """Analyze signal generation patterns."""
    print(f"\n{'='*80}")
    print(f"  SIGNAL ANALYSIS — {market_label}")
    print(f"{'='*80}")

    # Unique signals (by strategy+symbol+type+strike, within 5 min window)
    seen = set()
    unique = []
    for s in signals:
        key = f"{s['strategy']}_{s['symbol']}_{s['type']}_{s['strike']}"
        ts_key = s['timestamp'][:16]  # Group by minute
        full_key = f"{key}_{ts_key}"
        if full_key not in seen:
            seen.add(full_key)
            unique.append(s)

    # Deduplicate more aggressively — unique per 15-min window
    dedup_15m = {}
    for s in signals:
        key = f"{s['strategy']}_{s['symbol']}_{s['type']}_{s['strike']}"
        ts = s['timestamp']
        try:
            dt = datetime.fromisoformat(ts)
            window = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
            window_key = f"{key}_{window.isoformat()}"
            if window_key not in dedup_15m:
                dedup_15m[window_key] = s
        except:
            pass
    unique_15m = list(dedup_15m.values())

    print(f"  Total signal rows: {len(signals):,}")
    print(f"  Unique per-minute: {len(unique):,}")
    print(f"  Unique per-15min: {len(unique_15m):,}")
    print(f"  Repetition factor: {len(signals)/max(len(unique_15m),1):.1f}x")

    # By strategy
    print(f"\n  {'Strategy':<20s} {'Raw':>8s} {'Unique15m':>10s} {'AvgPrem':>10s} {'AvgDelta':>10s}")
    print(f"  {'-'*60}")
    by_strat_raw = defaultdict(list)
    by_strat_uniq = defaultdict(list)
    for s in signals:
        by_strat_raw[s['strategy']].append(s)
    for s in unique_15m:
        by_strat_uniq[s['strategy']].append(s)
    for strat in sorted(by_strat_raw.keys()):
        raw = by_strat_raw[strat]
        uniq = by_strat_uniq.get(strat, [])
        avg_prem = sum(s['premium'] for s in uniq) / max(len(uniq), 1)
        avg_delta = sum(abs(s['delta']) for s in uniq) / max(len(uniq), 1)
        print(f"  {strat:<20s} {len(raw):>8,} {len(uniq):>10} {avg_prem:>10.2f} {avg_delta:>10.4f}")

    # By symbol
    print(f"\n  {'Symbol':<15s} {'Signals':>8s} {'Unique15m':>10s}")
    print(f"  {'-'*35}")
    by_sym_raw = defaultdict(int)
    by_sym_uniq = defaultdict(int)
    for s in signals:
        by_sym_raw[s['symbol']] += 1
    for s in unique_15m:
        by_sym_uniq[s['symbol']] += 1
    for sym in sorted(by_sym_raw.keys()):
        print(f"  {sym:<15s} {by_sym_raw[sym]:>8,} {by_sym_uniq.get(sym,0):>10}")

    # Signal quality distribution (by premium)
    premiums = [s['premium'] for s in unique_15m if s['premium'] > 0]
    if premiums:
        premiums.sort()
        print(f"\n  Premium distribution (unique signals):")
        print(f"    Min: Rs {min(premiums):,.2f}")
        print(f"    25th: Rs {premiums[len(premiums)//4]:,.2f}")
        print(f"    Median: Rs {premiums[len(premiums)//2]:,.2f}")
        print(f"    75th: Rs {premiums[3*len(premiums)//4]:,.2f}")
        print(f"    Max: Rs {premiums[-1]:,.2f}")

    # Signals by hour
    print(f"\n  {'Hour':<8s} {'Signals':>8s} {'Unique15m':>10s}")
    print(f"  {'-'*28}")
    by_hour_raw = defaultdict(int)
    by_hour_uniq = defaultdict(int)
    for s in signals:
        try:
            hr = int(s['timestamp'][11:13])
            by_hour_raw[hr] += 1
        except:
            pass
    for s in unique_15m:
        try:
            hr = int(s['timestamp'][11:13])
            by_hour_uniq[hr] += 1
        except:
            pass
    for h in sorted(set(list(by_hour_raw.keys()) + list(by_hour_uniq.keys()))):
        print(f"  {h:02d}:00   {by_hour_raw.get(h,0):>8,} {by_hour_uniq.get(h,0):>10}")

    # Signals with low premium (would be filtered by MIN_PREMIUM=20)
    low_prem = [s for s in unique_15m if s['premium'] < 20 and 'SELL' not in s['type']]
    print(f"\n  BUY signals with premium < Rs 20: {len(low_prem)} ({len(low_prem)/max(len(unique_15m),1)*100:.1f}%)")
    low_prem_25 = [s for s in unique_15m if s['premium'] < 25 and 'SELL' not in s['type']]
    print(f"  BUY signals with premium < Rs 25: {len(low_prem_25)} ({len(low_prem_25)/max(len(unique_15m),1)*100:.1f}%)")

    # Ghost Zone signals specifically
    gz_signals = [s for s in unique_15m if s['strategy'] == 'Ghost Zone']
    print(f"\n  Ghost Zone unique signals: {len(gz_signals)}")
    if gz_signals:
        gz_prems = [s['premium'] for s in gz_signals]
        gz_deltas = [abs(s['delta']) for s in gz_signals]
        print(f"    Avg premium: Rs {sum(gz_prems)/len(gz_prems):,.2f}")
        print(f"    Avg |delta|: {sum(gz_deltas)/len(gz_deltas):.4f}")

    return unique_15m


def run_equity_backtest():
    """Run complete equity backtest."""
    print("\n" + "#"*80)
    print("#  EQUITY BACKTEST — Feb 27, 2026")
    print("#"*80)

    trades, capital = load_trades(os.path.join(DATA_DIR, 'equity_portfolio_state.json'))
    signals = load_signals(os.path.join(DATA_DIR, 'equity_signals_20260227.csv'))

    # A. Actual results
    actual_pnl, actual_wr, actual_count = analyze_actual_trades(trades, "EQUITY")

    # B. Signal analysis
    unique_signals = analyze_signals(signals, "EQUITY")

    # C. Scenario 1: Remove Ghost Zone only
    s1_pnl, s1_wr, s1_count = simulate_scenario(trades, signals,
        "NO GHOST ZONE",
        {'remove_strategies': ['Ghost Zone']})

    # D. Scenario 2: Remove Ghost Zone + Reversals
    s2_pnl, s2_wr, s2_count = simulate_scenario(trades, signals,
        "NO GHOST ZONE + NO REVERSALS",
        {'remove_strategies': ['Ghost Zone', 'Reversal']})

    # E. Scenario 3: Remove GZ + Rev + Min Score 50
    s3_pnl, s3_wr, s3_count = simulate_scenario(trades, signals,
        "NO GZ + NO REV + SCORE >= 50",
        {'remove_strategies': ['Ghost Zone', 'Reversal'], 'min_score': 50})

    # F. Scenario 4: Remove GZ + Rev + Min Premium 20
    s4_pnl, s4_wr, s4_count = simulate_scenario(trades, signals,
        "NO GZ + NO REV + MIN PREMIUM 20",
        {'remove_strategies': ['Ghost Zone', 'Reversal'], 'min_premium': 20})

    # G. Scenario 5: Full v2.5 (all filters)
    s5_pnl, s5_wr, s5_count = simulate_scenario(trades, signals,
        "FULL v2.5 (No GZ + No Rev + Score>=50 + Premium>=20)",
        {'remove_strategies': ['Ghost Zone', 'Reversal'], 'min_score': 50, 'min_premium': 20})

    # H. Scenario 6: Keep only Gamma Blast + CPR (best performers)
    s6_pnl, s6_wr, s6_count = simulate_scenario(trades, signals,
        "GAMMA BLAST + CPR ONLY (no GZ, no Reversals, no PCR+VWAP, no Survivor)",
        {'remove_strategies': ['Ghost Zone', 'Reversal', 'PCR+VWAP', 'Survivor']})

    # Summary comparison
    print(f"\n{'='*80}")
    print(f"  EQUITY SCENARIO COMPARISON")
    print(f"{'='*80}")
    print(f"  {'Scenario':<50s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s} {'vs Actual':>12s}")
    print(f"  {'-'*88}")
    scenarios = [
        ("ACTUAL (v2.3.2)", actual_count, actual_wr, actual_pnl, 0),
        ("No Ghost Zone", s1_count, s1_wr, s1_pnl, s1_pnl - actual_pnl),
        ("No GZ + No Reversals", s2_count, s2_wr, s2_pnl, s2_pnl - actual_pnl),
        ("No GZ + No Rev + Score>=50", s3_count, s3_wr, s3_pnl, s3_pnl - actual_pnl),
        ("No GZ + No Rev + Premium>=20", s4_count, s4_wr, s4_pnl, s4_pnl - actual_pnl),
        ("FULL v2.5", s5_count, s5_wr, s5_pnl, s5_pnl - actual_pnl),
        ("Gamma Blast + CPR only", s6_count, s6_wr, s6_pnl, s6_pnl - actual_pnl),
    ]
    for name, count, wr, pnl, diff in scenarios:
        diff_str = f"+Rs {diff:,.0f}" if diff >= 0 else f"-Rs {abs(diff):,.0f}"
        if diff == 0:
            diff_str = "baseline"
        print(f"  {name:<50s} {count:>6d} {wr:>5.1f}% {pnl:>11,.2f} {diff_str:>12s}")

    return actual_pnl, s5_pnl


def run_commodity_backtest():
    """Run complete commodity backtest."""
    print("\n\n" + "#"*80)
    print("#  COMMODITY BACKTEST — Feb 27, 2026")
    print("#"*80)

    trades, capital = load_trades(os.path.join(DATA_DIR, 'commodity_portfolio_state.json'))
    signals = load_signals(os.path.join(DATA_DIR, 'commodity_signals_20260227.csv'))

    # A. Actual results
    actual_pnl, actual_wr, actual_count = analyze_actual_trades(trades, "COMMODITY")

    # B. Signal analysis
    unique_signals = analyze_signals(signals, "COMMODITY")

    # C. Scenario 1: Remove Ghost Zone
    s1_pnl, s1_wr, s1_count = simulate_scenario(trades, signals,
        "NO GHOST ZONE",
        {'remove_strategies': ['Ghost Zone']})

    # D. Scenario 2: Remove GZ + Reversals
    s2_pnl, s2_wr, s2_count = simulate_scenario(trades, signals,
        "NO GHOST ZONE + NO REVERSALS",
        {'remove_strategies': ['Ghost Zone', 'Reversal']})

    # E. Scenario 3: Full v2.5
    s3_pnl, s3_wr, s3_count = simulate_scenario(trades, signals,
        "FULL v2.5 (No GZ + No Rev + Score>=50 + Premium>=30)",
        {'remove_strategies': ['Ghost Zone', 'Reversal'], 'min_score': 50, 'min_premium': 30})

    # Summary
    print(f"\n{'='*80}")
    print(f"  COMMODITY SCENARIO COMPARISON")
    print(f"{'='*80}")
    print(f"  {'Scenario':<50s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s} {'vs Actual':>12s}")
    print(f"  {'-'*88}")
    scenarios = [
        ("ACTUAL (v2.3.2)", actual_count, actual_wr, actual_pnl, 0),
        ("No Ghost Zone", s1_count, s1_wr, s1_pnl, s1_pnl - actual_pnl),
        ("No GZ + No Reversals", s2_count, s2_wr, s2_pnl, s2_pnl - actual_pnl),
        ("FULL v2.5", s3_count, s3_wr, s3_pnl, s3_pnl - actual_pnl),
    ]
    for name, count, wr, pnl, diff in scenarios:
        diff_str = f"+Rs {diff:,.0f}" if diff >= 0 else f"-Rs {abs(diff):,.0f}"
        if diff == 0:
            diff_str = "baseline"
        print(f"  {name:<50s} {count:>6d} {wr:>5.1f}% {pnl:>11,.2f} {diff_str:>12s}")

    return actual_pnl, s3_pnl


def analyze_worst_trades(trades, market_label, n=10):
    """Deep-dive on worst trades to find patterns."""
    print(f"\n{'='*80}")
    print(f"  TOP {n} WORST TRADES — {market_label}")
    print(f"{'='*80}")
    worst = sorted(trades, key=lambda t: t['pnl'])[:n]
    for i, t in enumerate(worst, 1):
        print(f"\n  #{i}: {t['id']}")
        print(f"    Strategy: {t['strategy']} | Symbol: {t['symbol']} | Type: {t['signal_type']}")
        print(f"    Strike: {t['strike']} | Entry: Rs {t['entry_premium']:.2f} | Exit: Rs {t['exit_premium']:.2f}")
        print(f"    PnL: Rs {t['pnl']:,.2f} | Lot: {t['lot_size']} x {t['multiplier']}")
        print(f"    Exit Reason: {t['exit_reason']} | Score: {t['quality_score']}")
        print(f"    Entry: {t['entry_time'][:19]} | Exit: {t['exit_time'][:19]}")
        if t['target']: print(f"    Target: Rs {t['target']:.2f} | SL: Rs {t['sl']:.2f}")


def analyze_best_trades(trades, market_label, n=10):
    """Deep-dive on best trades to find patterns."""
    print(f"\n{'='*80}")
    print(f"  TOP {n} BEST TRADES — {market_label}")
    print(f"{'='*80}")
    best = sorted(trades, key=lambda t: t['pnl'], reverse=True)[:n]
    for i, t in enumerate(best, 1):
        print(f"\n  #{i}: {t['id']}")
        print(f"    Strategy: {t['strategy']} | Symbol: {t['symbol']} | Type: {t['signal_type']}")
        print(f"    Strike: {t['strike']} | Entry: Rs {t['entry_premium']:.2f} | Exit: Rs {t['exit_premium']:.2f}")
        print(f"    PnL: Rs {t['pnl']:,.2f} | Lot: {t['lot_size']} x {t['multiplier']}")
        print(f"    Exit Reason: {t['exit_reason']} | Score: {t['quality_score']}")
        print(f"    Entry: {t['entry_time'][:19]} | Exit: {t['exit_time'][:19]}")
        if t['target']: print(f"    Target: Rs {t['target']:.2f} | SL: Rs {t['sl']:.2f}")


if __name__ == '__main__':
    # === EQUITY ===
    eq_trades, eq_capital = load_trades(os.path.join(DATA_DIR, 'equity_portfolio_state.json'))
    eq_actual, eq_v25 = run_equity_backtest()
    analyze_worst_trades(eq_trades, "EQUITY", 10)
    analyze_best_trades(eq_trades, "EQUITY", 10)

    # === COMMODITY ===
    cm_trades, cm_capital = load_trades(os.path.join(DATA_DIR, 'commodity_portfolio_state.json'))
    cm_actual, cm_v25 = run_commodity_backtest()
    analyze_worst_trades(cm_trades, "COMMODITY", 10)
    analyze_best_trades(cm_trades, "COMMODITY", 10)

    # === FINAL SUMMARY ===
    print(f"\n\n{'#'*80}")
    print(f"#  FINAL COMBINED SUMMARY")
    print(f"{'#'*80}")
    combined_actual = eq_actual + cm_actual
    combined_v25 = eq_v25 + cm_v25
    improvement = combined_v25 - combined_actual
    print(f"\n  Combined ACTUAL PnL:  Rs {combined_actual:,.2f}")
    print(f"  Combined v2.5 PnL:   Rs {combined_v25:,.2f}")
    print(f"  IMPROVEMENT:          Rs {improvement:,.2f} ({improvement/max(abs(combined_actual),1)*100:.1f}%)")
    print(f"\n  Starting Capital:     Rs 6,00,000")
    print(f"  Actual Return:        {combined_actual/600000*100:.2f}%")
    print(f"  v2.5 Projected:       {combined_v25/600000*100:.2f}%")
