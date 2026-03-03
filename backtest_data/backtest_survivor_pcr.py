#!/usr/bin/env python3
"""
Deep-dive backtest: Survivor + PCR+VWAP strategies.
Analyze why they perform the way they do and what changes could help.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


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
                'entry_time': ts,
                'exit_time': exit_ts,
                'target': details.get('target', 0),
                'sl': details.get('sl', 0),
            })
    return trades, data


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


def analyze_survivor_commodity():
    """Deep analysis of Commodity Survivor trades."""
    print("\n" + "="*80)
    print("  SURVIVOR STRATEGY — COMMODITY DEEP DIVE")
    print("="*80)

    trades, data = load_trades(os.path.join(DATA_DIR, 'commodity_portfolio_state.json'))
    signals = load_signals(os.path.join(DATA_DIR, 'commodity_signals_20260227.csv'))

    surv_trades = [t for t in trades if 'Survivor' in t['strategy']]
    surv_signals = [s for s in signals if s['strategy'] == 'Survivor']

    # Dedup signals per 15min
    dedup = {}
    for s in surv_signals:
        key = f"{s['symbol']}_{s['type']}_{s['strike']}"
        try:
            dt = datetime.fromisoformat(s['timestamp'])
            window = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
            wkey = f"{key}_{window.isoformat()}"
            if wkey not in dedup:
                dedup[wkey] = s
        except:
            pass
    unique_signals = list(dedup.values())

    print(f"\n  Survivor raw signals: {len(surv_signals)}")
    print(f"  Survivor unique/15min: {len(unique_signals)}")
    print(f"  Survivor executed trades: {len(surv_trades)}")
    print(f"  Conversion rate: {len(surv_trades)/max(len(unique_signals),1)*100:.1f}%")

    # Signal analysis by commodity
    print(f"\n  {'Commodity':<15s} {'Signals':>8s} {'AvgPrem':>10s} {'AvgDTE':>8s}")
    print(f"  {'-'*43}")
    by_comm = defaultdict(list)
    for s in unique_signals:
        by_comm[s['symbol']].append(s)
    for comm, sigs in sorted(by_comm.items()):
        avg_prem = sum(s['premium'] for s in sigs) / len(sigs)
        avg_dte = sum(s['dte'] for s in sigs) / len(sigs)
        print(f"  {comm:<15s} {len(sigs):>8d} {avg_prem:>10.2f} {avg_dte:>8.1f}")

    # Trade analysis
    print(f"\n  {'Trade ID':<30s} {'Symbol':<12s} {'Type':<15s} {'Entry':>10s} {'Exit':>10s} {'PnL':>10s} {'Reason':<25s}")
    print(f"  {'-'*115}")
    surv_trades.sort(key=lambda t: t['pnl'], reverse=True)
    for t in surv_trades:
        premium_change = ((t['entry_premium'] - t['exit_premium']) / t['entry_premium'] * 100) if t['entry_premium'] > 0 else 0
        print(f"  {t['id']:<30s} {t['symbol']:<12s} {t['signal_type']:<15s} "
              f"Rs {t['entry_premium']:>8.2f} Rs {t['exit_premium']:>8.2f} {t['pnl']:>10.2f} "
              f"{t['exit_reason']:<25s} PremChg={premium_change:.1f}%")

    # Key metrics
    wins = [t for t in surv_trades if t['pnl'] > 0]
    losses = [t for t in surv_trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in surv_trades)

    print(f"\n  === SURVIVOR SUMMARY ===")
    print(f"  Total: {len(surv_trades)} trades | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"  Win Rate: {len(wins)/max(len(surv_trades),1)*100:.1f}%")
    print(f"  PnL: Rs {total_pnl:,.2f}")

    # By commodity
    print(f"\n  {'Commodity':<12s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s} {'AvgPremChg':>12s}")
    print(f"  {'-'*50}")
    by_comm_trades = defaultdict(list)
    for t in surv_trades:
        by_comm_trades[t['symbol']].append(t)
    for comm, ct in sorted(by_comm_trades.items()):
        c_wins = len([t for t in ct if t['pnl'] > 0])
        c_pnl = sum(t['pnl'] for t in ct)
        c_wr = c_wins / max(len(ct), 1) * 100
        avg_chg = sum((t['entry_premium'] - t['exit_premium']) / max(t['entry_premium'], 1) * 100 for t in ct) / len(ct)
        print(f"  {comm:<12s} {len(ct):>6d} {c_wr:>5.1f}% {c_pnl:>11,.2f} {avg_chg:>11.1f}%")

    # Exit reason analysis
    print(f"\n  {'Exit Reason':<25s} {'Count':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  {'-'*50}")
    by_reason = defaultdict(list)
    for t in surv_trades:
        by_reason[t['exit_reason']].append(t)
    for r, rt in sorted(by_reason.items(), key=lambda x: len(x[1]), reverse=True):
        r_wins = len([t for t in rt if t['pnl'] > 0])
        r_pnl = sum(t['pnl'] for t in rt)
        r_wr = r_wins / max(len(rt), 1) * 100
        print(f"  {r:<25s} {len(rt):>6d} {r_wr:>5.1f}% {r_pnl:>11,.2f}")

    # Capital analysis for Survivor
    print(f"\n  === CAPITAL REQUIREMENT ===")
    # MCX margins: GOLDM=100000, SILVERM=80000, CRUDEOILM=8000, GOLDM margin=15000 for GOLDM mini
    MCX_MARGINS = {'GOLDM': 15000, 'SILVERM': 15000, 'CRUDEOILM': 8000}
    total_margin_used = 0
    for t in surv_trades:
        margin = MCX_MARGINS.get(t['symbol'], 15000) * (t.get('num_lots', 1) or 1)
        total_margin_used += margin
        print(f"    {t['id']}: margin Rs {margin:,.0f} | PnL Rs {t['pnl']:,.2f} | "
              f"Return on margin: {t['pnl']/max(margin,1)*100:.2f}%")

    print(f"\n  Total margin deployed: Rs {total_margin_used:,.0f}")
    print(f"  Return on margin: {total_pnl/max(total_margin_used,1)*100:.3f}%")
    print(f"  Avg margin per trade: Rs {total_margin_used/max(len(surv_trades),1):,.0f}")

    # What-if: What if we ONLY kept GOLDM Survivor?
    goldm_surv = [t for t in surv_trades if t['symbol'] == 'GOLDM']
    goldm_pnl = sum(t['pnl'] for t in goldm_surv)
    goldm_wins = len([t for t in goldm_surv if t['pnl'] > 0])
    print(f"\n  === WHAT-IF: GOLDM SURVIVOR ONLY ===")
    print(f"  Trades: {len(goldm_surv)} | Wins: {goldm_wins} | WR: {goldm_wins/max(len(goldm_surv),1)*100:.1f}%")
    print(f"  PnL: Rs {goldm_pnl:,.2f}")

    # What-if: What if we remove SILVERM?
    no_silver = [t for t in surv_trades if t['symbol'] != 'SILVERM']
    no_silver_pnl = sum(t['pnl'] for t in no_silver)
    no_silver_wins = len([t for t in no_silver if t['pnl'] > 0])
    print(f"\n  === WHAT-IF: NO SILVERM SURVIVOR ===")
    print(f"  Trades: {len(no_silver)} | Wins: {no_silver_wins} | WR: {no_silver_wins/max(len(no_silver),1)*100:.1f}%")
    print(f"  PnL: Rs {no_silver_pnl:,.2f}")

    # Scenario: Modify target from 0.3x to 0.5x (less aggressive)
    print(f"\n  === TARGET SENSITIVITY ===")
    for target_mult in [0.3, 0.4, 0.5, 0.6, 0.7]:
        # For SELL, target = entry * mult means option must decay to mult * entry
        # Entry premium must drop to target (which is entry * mult)
        # So profit = (entry - target) * lot * mult_qty
        # Check how many trades would have hit target at different levels
        # Since we don't have tick data, we use the actual exit premium as a proxy
        would_hit = 0
        for t in surv_trades:
            simulated_target = t['entry_premium'] * target_mult
            # For SELL: profit when premium drops. Check if exit_premium reached target
            if t['exit_premium'] <= simulated_target:
                would_hit += 1
        print(f"    Target {target_mult:.1f}x: {would_hit}/{len(surv_trades)} trades would hit "
              f"({would_hit/max(len(surv_trades),1)*100:.1f}%)")


def analyze_pcr_vwap():
    """Analyze why PCR+VWAP generates ZERO signals."""
    print("\n" + "="*80)
    print("  PCR+VWAP STRATEGY — ANALYSIS")
    print("="*80)

    # Equity signals
    eq_signals = load_signals(os.path.join(DATA_DIR, 'equity_signals_20260227.csv'))
    cm_signals = load_signals(os.path.join(DATA_DIR, 'commodity_signals_20260227.csv'))

    eq_pcr = [s for s in eq_signals if 'PCR' in s['strategy'] or 'PCRVWAP' in s['type']]
    cm_pcr = [s for s in cm_signals if 'PCR' in s['strategy'] or 'PCRVWAP' in s['type']]

    print(f"\n  Equity PCR+VWAP signals: {len(eq_pcr)}")
    print(f"  Commodity PCR+VWAP signals: {len(cm_pcr)}")

    if eq_pcr:
        print(f"\n  Equity PCR+VWAP sample signals:")
        for s in eq_pcr[:5]:
            print(f"    {s['timestamp'][:19]} {s['symbol']} {s['type']} Strike={s['strike']} "
                  f"Premium={s['premium']:.2f} Reason={s['reason'][:80]}")

    if cm_pcr:
        print(f"\n  Commodity PCR+VWAP sample signals:")
        for s in cm_pcr[:5]:
            print(f"    {s['timestamp'][:19]} {s['symbol']} {s['type']} Strike={s['strike']} "
                  f"Premium={s['premium']:.2f} Reason={s['reason'][:80]}")

    # Check if there are any PCR+VWAP trades in history
    eq_trades, _ = load_trades(os.path.join(DATA_DIR, 'equity_portfolio_state.json'))
    cm_trades, _ = load_trades(os.path.join(DATA_DIR, 'commodity_portfolio_state.json'))

    eq_pcr_trades = [t for t in eq_trades if 'PCR' in t['strategy'] or 'PCRVWAP' in t['signal_type']]
    cm_pcr_trades = [t for t in cm_trades if 'PCR' in t['strategy'] or 'PCRVWAP' in t['signal_type']]

    print(f"\n  Equity PCR+VWAP executed trades (Feb 27): {len(eq_pcr_trades)}")
    print(f"  Commodity PCR+VWAP executed trades (Feb 27): {len(cm_pcr_trades)}")

    # Also check ALL trades in portfolio (not just Feb 27)
    with open(os.path.join(DATA_DIR, 'equity_portfolio_state.json')) as f:
        eq_data = json.load(f)
    with open(os.path.join(DATA_DIR, 'commodity_portfolio_state.json')) as f:
        cm_data = json.load(f)

    all_eq_closed = eq_data.get('closed_trades', [])
    all_cm_closed = cm_data.get('closed_trades', [])

    eq_pcr_all = [t for t in all_eq_closed if 'PCR' in t.get('strategy', '') or 'PCRVWAP' in t.get('signal_type', '')]
    cm_pcr_all = [t for t in all_cm_closed if 'PCR' in t.get('strategy', '') or 'PCRVWAP' in t.get('signal_type', '')]

    print(f"\n  Total historical equity PCR+VWAP trades: {len(eq_pcr_all)}")
    print(f"  Total historical commodity PCR+VWAP trades: {len(cm_pcr_all)}")

    if eq_pcr_all:
        for t in eq_pcr_all[:5]:
            print(f"    {t.get('timestamp', '')[:19]} {t.get('symbol','')} {t.get('signal_type','')} "
                  f"PnL={t.get('pnl',0):.2f}")

    # REASON: PCR+VWAP requires:
    # Equity: IV < 0.40, PCR > 1.05 (CE) or PCR < 0.95 (PE), spot near VWAP
    # Commodity: IV < 0.50, PCR > 1.05 (CE) or PCR < 0.95 (PE), spot near VWAP
    # The PCR is computed as a proxy (not real OI-based) — may be always near 1.0
    print(f"\n  === WHY ZERO SIGNALS? ===")
    print(f"  PCR+VWAP requires: PCR > 1.05 (CE) or PCR < 0.95 (PE)")
    print(f"  The PCR is computed as a PROXY (momentum-based), NOT real OI data.")
    print(f"  If the proxy PCR stays between 0.95-1.05, no signals fire.")
    print(f"  This strategy is effectively DEAD without real PCR data.")


def analyze_equity_survivor():
    """Check why equity Survivor generates zero signals."""
    print("\n" + "="*80)
    print("  EQUITY SURVIVOR — ANALYSIS")
    print("="*80)

    eq_signals = load_signals(os.path.join(DATA_DIR, 'equity_signals_20260227.csv'))
    surv_signals = [s for s in eq_signals if s['strategy'] == 'Survivor']

    print(f"\n  Equity Survivor signals: {len(surv_signals)}")

    if surv_signals:
        print(f"  Sample signals:")
        for s in surv_signals[:5]:
            print(f"    {s['timestamp'][:19]} {s['symbol']} {s['type']} Strike={s['strike']} "
                  f"Premium={s['premium']:.2f} DTE={s['dte']} Reason={s['reason'][:80]}")
    else:
        print(f"\n  Equity Survivor generated ZERO signals on Feb 27.")
        print(f"  Possible reasons:")
        print(f"  1. Survivor requires breakout above prev high or below prev low")
        print(f"  2. If market was ranging, no breakouts occurred")
        print(f"  3. Margin requirement: needs Rs 100K+ per lot (NIFTY)")
        print(f"  4. Capital check: portfolio.capital >= MARGIN_PER_LOT")
        print(f"  5. With capital at Rs 316K, only 3 NIFTY margin lots possible")

    # Check all-time Survivor equity trades
    with open(os.path.join(DATA_DIR, 'equity_portfolio_state.json')) as f:
        data = json.load(f)
    all_surv = [t for t in data.get('closed_trades', []) if 'Survivor' in t.get('strategy', '')]
    print(f"\n  Total historical equity Survivor trades: {len(all_surv)}")
    if all_surv:
        total_pnl = sum(t.get('pnl', 0) for t in all_surv)
        wins = len([t for t in all_surv if t.get('pnl', 0) > 0])
        print(f"  Total PnL: Rs {total_pnl:,.2f}")
        print(f"  Win Rate: {wins}/{len(all_surv)} = {wins/len(all_surv)*100:.1f}%")
        for t in all_surv[-5:]:
            print(f"    {t.get('timestamp','')[:19]} {t.get('symbol','')} {t.get('signal_type','')} "
                  f"PnL={t.get('pnl',0):.2f} Reason={t.get('exit_reason','')}")


def overall_survivor_recommendation():
    """Final recommendation for Survivor strategy."""
    print("\n" + "="*80)
    print("  SURVIVOR STRATEGY — RECOMMENDATION")
    print("="*80)

    trades, _ = load_trades(os.path.join(DATA_DIR, 'commodity_portfolio_state.json'))
    surv = [t for t in trades if 'Survivor' in t['strategy']]

    total_pnl = sum(t['pnl'] for t in surv)
    by_comm = defaultdict(list)
    for t in surv:
        by_comm[t['symbol']].append(t)

    print(f"\n  Total Survivor PnL: Rs {total_pnl:,.2f}")
    print()

    for comm, ct in sorted(by_comm.items()):
        c_pnl = sum(t['pnl'] for t in ct)
        c_wins = len([t for t in ct if t['pnl'] > 0])
        c_wr = c_wins / max(len(ct), 1) * 100
        # Check premium movement
        no_movement = len([t for t in ct if abs(t['entry_premium'] - t['exit_premium']) < 1])
        print(f"  {comm}:")
        print(f"    Trades: {len(ct)}, WR: {c_wr:.1f}%, PnL: Rs {c_pnl:,.2f}")
        print(f"    Premium never moved: {no_movement}/{len(ct)} trades")
        if ct:
            avg_hold_hrs = 0
            for t in ct:
                try:
                    entry = datetime.fromisoformat(t['entry_time'])
                    exit_t = datetime.fromisoformat(t['exit_time'])
                    avg_hold_hrs += (exit_t - entry).total_seconds() / 3600
                except:
                    pass
            avg_hold_hrs /= len(ct)
            print(f"    Avg hold: {avg_hold_hrs:.1f} hours")

    print(f"""
  === VERDICT ===

  GOLDM Survivor: KEEP (only winner, Rs 3,742 from 1 trade, reasonable margin Rs 15K)
  CRUDEOILM Survivor: KEEP (small winner Rs 306, good margin Rs 8K, worth keeping)
  SILVERM Survivor: DISABLE (all 6 trades lost, premium never moved, waste of capital)

  EQUITY Survivor: ZERO signals generated — not worth keeping unless market conditions change.
                   Needs Rs 100K margin per lot. Too capital-intensive for Rs 3L portfolio.

  OVERALL: Keep Survivor for GOLDM + CRUDEOILM only. Disable for SILVERM.
  Capital requirement: Rs 15K (GOLDM) + Rs 8K (CRUDEOILM) = Rs 23K per trade — manageable.

  PCR+VWAP: DEAD strategy. Zero signals because proxy PCR stays between 0.95-1.05.
            Either get real PCR data (Angel OI chain) or disable entirely.
""")


if __name__ == '__main__':
    analyze_survivor_commodity()
    analyze_pcr_vwap()
    analyze_equity_survivor()
    overall_survivor_recommendation()
