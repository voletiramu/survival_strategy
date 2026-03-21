"""
Simulation: March 11, 2026 — What SHOULD have happened vs what our bot did.
Uses REAL option chain premium data from live data logger (3-min snapshots).

Market context: Massive bearish day
  NIFTY:     24228 → 23844 = -384 pts (-1.6%)
  BANKNIFTY: 56708 → 55660 = -1047 pts (-1.8%)
  SENSEX:    78054 → 76778 = -1275 pts (-1.6%)
"""

import pandas as pd
import os
import json
from datetime import datetime

DATA_DIR = 'data/live/2026-03-11'

# Lot sizes
LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20}
# Note: NIFTY exchange lot is 75 (user confirmed) - code says 65 but NSE may vary

def load_option_chain(symbol):
    """Load option chain CSV."""
    f = os.path.join(DATA_DIR, f'option_chain_{symbol}.csv')
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df

def load_spot(symbol):
    """Load 1-min spot data."""
    f = os.path.join(DATA_DIR, f'spot_1min_{symbol}.csv')
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df

def get_strike_timeline(oc, strike, opt_type):
    """Get premium timeline for a specific strike."""
    mask = (oc['strike'] == strike) & (oc['type'] == opt_type)
    return oc[mask][['timestamp', 'ltp', 'oi', 'iv', 'spot']].sort_values('timestamp').reset_index(drop=True)

def simulate_trade(timeline, entry_idx, exit_strategy='optimal', tsl_pct=0.10, lots=1, lot_size=75):
    """Simulate a single trade on premium timeline.

    exit_strategy:
      'optimal' - exit at absolute peak premium
      'tsl_10' - trailing SL at 10% from peak
      'tsl_15' - trailing SL at 15% from peak
      'tsl_20' - trailing SL at 20% from peak
      'eod' - hold till end of day
      'time_2hr' - exit 2 hours after entry
      'bot_actual' - replicate bot's actual TSL logic
    """
    if entry_idx >= len(timeline):
        return None

    entry_premium = timeline.iloc[entry_idx]['ltp']
    entry_time = timeline.iloc[entry_idx]['timestamp']
    entry_spot = timeline.iloc[entry_idx]['spot']

    peak_premium = entry_premium
    peak_idx = entry_idx
    exit_idx = len(timeline) - 1  # default: EOD
    exit_premium = timeline.iloc[exit_idx]['ltp']
    exit_reason = 'EOD'

    if exit_strategy == 'optimal':
        # Find absolute peak after entry
        for i in range(entry_idx + 1, len(timeline)):
            if timeline.iloc[i]['ltp'] > peak_premium:
                peak_premium = timeline.iloc[i]['ltp']
                peak_idx = i
        exit_idx = peak_idx
        exit_premium = peak_premium
        exit_reason = 'OPTIMAL_PEAK'

    elif exit_strategy.startswith('tsl_'):
        pct = int(exit_strategy.split('_')[1]) / 100
        for i in range(entry_idx + 1, len(timeline)):
            current = timeline.iloc[i]['ltp']
            if current > peak_premium:
                peak_premium = current
                peak_idx = i
            tsl_level = peak_premium * (1 - pct)
            if current <= tsl_level and peak_premium > entry_premium:
                exit_idx = i
                exit_premium = current
                exit_reason = f'TSL_{int(pct*100)}%'
                break
        else:
            exit_premium = timeline.iloc[exit_idx]['ltp']
            exit_reason = 'EOD (TSL never hit)'

    elif exit_strategy == 'eod':
        exit_premium = timeline.iloc[exit_idx]['ltp']
        exit_reason = 'EOD'

    elif exit_strategy == 'time_2hr':
        target_time = entry_time + pd.Timedelta(hours=2)
        for i in range(entry_idx + 1, len(timeline)):
            if timeline.iloc[i]['timestamp'] >= target_time:
                exit_idx = i
                exit_premium = timeline.iloc[i]['ltp']
                exit_reason = 'TIME_2HR'
                break
            if timeline.iloc[i]['ltp'] > peak_premium:
                peak_premium = timeline.iloc[i]['ltp']

    exit_time = timeline.iloc[exit_idx]['timestamp']
    exit_spot = timeline.iloc[exit_idx]['spot']

    total_qty = lots * lot_size
    raw_pnl = (exit_premium - entry_premium) * total_qty

    # Approximate costs (brokerage + STT + charges)
    turnover = (entry_premium + exit_premium) * total_qty
    costs = turnover * 0.001  # ~0.1% total costs
    net_pnl = raw_pnl - costs

    return {
        'entry_time': entry_time.strftime('%H:%M'),
        'exit_time': exit_time.strftime('%H:%M'),
        'entry_premium': round(entry_premium, 1),
        'exit_premium': round(exit_premium, 1),
        'peak_premium': round(peak_premium, 1),
        'entry_spot': round(entry_spot, 0),
        'exit_spot': round(exit_spot, 0),
        'premium_gain': round(exit_premium - entry_premium, 1),
        'premium_gain_pct': round((exit_premium - entry_premium) / entry_premium * 100, 1),
        'total_qty': total_qty,
        'raw_pnl': round(raw_pnl, 0),
        'net_pnl': round(net_pnl, 0),
        'exit_reason': exit_reason,
        'duration': str(exit_time - entry_time).split('.')[0],
        'peak_capture_pct': round((exit_premium - entry_premium) / max(peak_premium - entry_premium, 1) * 100, 0) if peak_premium > entry_premium else 0,
    }


def run_simulation(symbol, strike, opt_type, entry_time_str, num_lots, strategies):
    """Run simulation for one instrument across multiple exit strategies."""
    oc = load_option_chain(symbol)
    if oc is None:
        print(f"  No option chain data for {symbol}")
        return {}

    lot_size = LOT_SIZES.get(symbol, 75)
    timeline = get_strike_timeline(oc, strike, opt_type)

    if len(timeline) == 0:
        print(f"  No data for {symbol} {strike} {opt_type}")
        return {}

    # Find entry index (first snapshot >= entry_time)
    entry_time = pd.Timestamp(f'2026-03-11 {entry_time_str}')
    entry_idx = 0
    for i, row in timeline.iterrows():
        if timeline.loc[i, 'timestamp'] >= entry_time:
            entry_idx = i
            break

    results = {}
    for strat in strategies:
        result = simulate_trade(timeline, entry_idx, strat, lots=num_lots, lot_size=lot_size)
        if result:
            results[strat] = result

    return results


def print_results_table(symbol, strike, opt_type, results, bot_actual=None):
    """Print comparison table."""
    print(f"\n{'='*100}")
    print(f"  {symbol} {strike} {opt_type} — Simulation Results")
    print(f"{'='*100}")

    if bot_actual:
        print(f"\n  BOT ACTUAL: Entry={bot_actual['entry']:.1f} Peak={bot_actual['peak']:.1f} "
              f"Exit={bot_actual['exit']:.1f} | PnL=₹{bot_actual['pnl']:+,.0f} | {bot_actual['reason']}")
        print(f"  Bot captured: ₹{bot_actual['exit']-bot_actual['entry']:.1f} per unit "
              f"({(bot_actual['exit']-bot_actual['entry'])/bot_actual['entry']*100:.1f}%)")

    print(f"\n  {'Strategy':<20s} {'Entry':>8s} {'Peak':>8s} {'Exit':>8s} {'Gain':>8s} {'%':>7s} {'PnL':>12s} {'Duration':>10s} {'Peak%':>7s} {'Reason':<25s}")
    print(f"  {'-'*20} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7} {'-'*12} {'-'*10} {'-'*7} {'-'*25}")

    for strat, r in results.items():
        print(f"  {strat:<20s} {r['entry_premium']:>8.1f} {r['peak_premium']:>8.1f} {r['exit_premium']:>8.1f} "
              f"{r['premium_gain']:>+8.1f} {r['premium_gain_pct']:>+6.1f}% "
              f"₹{r['net_pnl']:>+10,.0f} {r['duration']:>10s} {r['peak_capture_pct']:>6.0f}% {r['exit_reason']:<25s}")


# ============================================================
# MAIN SIMULATION
# ============================================================

strategies = ['optimal', 'tsl_10', 'tsl_15', 'tsl_20', 'eod', 'time_2hr']

print("=" * 100)
print("  MARCH 11, 2026 — SIMULATION USING REAL OPTION CHAIN DATA")
print("  Market: NIFTY -384pts | BANKNIFTY -1047pts | SENSEX -1275pts")
print("=" * 100)

# ---- NIFTY 24200 PE ----
results = run_simulation('NIFTY', 24200, 'PE', '09:34', 2, strategies)
bot_nifty_1 = {'entry': 244.3, 'peak': 248.0, 'exit': 248.0, 'pnl': 117, 'reason': 'BREAKOUT_FAIL (5 min)'}
print_results_table('NIFTY', 24200, 'PE', results, bot_nifty_1)

# ---- NIFTY 24200 PE (2nd entry at 09:53) ----
results2 = run_simulation('NIFTY', 24200, 'PE', '09:53', 2, strategies)
bot_nifty_2 = {'entry': 248.2, 'peak': 332.9, 'exit': 306.8, 'pnl': 3684+3684, 'reason': 'TSL_HIT at 11:05'}
print_results_table('NIFTY', 24200, 'PE (re-entry)', results2, bot_nifty_2)

# ---- NIFTY 24250 PE ----
results3 = run_simulation('NIFTY', 24250, 'PE', '09:34', 2, strategies)
print_results_table('NIFTY', 24250, 'PE', results3)

# ---- BANKNIFTY 56400 PE ----
# Need to check available BN strikes
oc_bn = load_option_chain('BANKNIFTY')
if oc_bn is not None:
    bn_pe = oc_bn[oc_bn['type'] == 'PE']
    available_strikes = sorted(bn_pe['strike'].unique())
    # Find strikes near 56400
    atm_strikes = [s for s in available_strikes if 55500 <= s <= 57000]
    print(f"\n  Available BANKNIFTY PE strikes near ATM: {atm_strikes[:15]}")

# ---- BANKNIFTY 56700 PE (Gamma Blast trade — best performer) ----
results_bn1 = run_simulation('BANKNIFTY', 56700, 'PE', '09:30', 2, strategies)
bot_bn_gamma = {'entry': 921.0, 'peak': 1375.0, 'exit': 1261.2, 'pnl': 20205, 'reason': 'TSL_HIT at 12:34'}
print_results_table('BANKNIFTY', 56700, 'PE (Gamma)', results_bn1, bot_bn_gamma)

# ---- BANKNIFTY 56100 PE ----
results_bn2 = run_simulation('BANKNIFTY', 56100, 'PE', '09:56', 2, strategies)
bot_bn_cpr = {'entry': 983.1, 'peak': 1480.0, 'exit': 1377.2, 'pnl': 11669, 'reason': 'TSL_HIT at 13:27'}
print_results_table('BANKNIFTY', 56100, 'PE (CPR)', results_bn2, bot_bn_cpr)

# ---- BANKNIFTY 56400 PE ----
results_bn3 = run_simulation('BANKNIFTY', 56400, 'PE', '10:40', 2, strategies)
bot_bn_fail = {'entry': 996.8, 'peak': 996.8, 'exit': 982.5, 'pnl': -570, 'reason': 'BREAKOUT_FAIL (5 min)'}
print_results_table('BANKNIFTY', 56400, 'PE', results_bn3, bot_bn_fail)

# ---- SENSEX PE ----
oc_sx = load_option_chain('SENSEX')
if oc_sx is not None:
    sx_pe = oc_sx[oc_sx['type'] == 'PE']
    available_sx = sorted(sx_pe['strike'].unique())
    # Find ATM strikes
    atm_sx = [s for s in available_sx if 77000 <= s <= 79000]
    print(f"\n  Available SENSEX PE strikes near ATM: {atm_sx[:15]}")

# ---- SENSEX 78000 PE (biggest winner) ----
results_sx1 = run_simulation('SENSEX', 78000, 'PE', '09:30', 4, strategies)
bot_sx = {'entry': 411.8, 'peak': 778.4, 'exit': 701.6, 'pnl': 23010+23010+17234, 'reason': 'TSL_HIT at 11:05 (3 positions)'}
print_results_table('SENSEX', 78000, 'PE', results_sx1, bot_sx)

# ---- SENSEX 78000 PE (re-entry after 11:05) ----
results_sx2 = run_simulation('SENSEX', 78000, 'PE', '11:15', 3, strategies)
bot_sx2 = {'entry': 416.1, 'peak': 416.1, 'exit': 416.1, 'pnl': -148-135, 'reason': 'BREAKOUT_FAIL (5 min)'}
print_results_table('SENSEX', 78000, 'PE (re-entry)', results_sx2, bot_sx2)


# ============================================================
# GRAND SUMMARY
# ============================================================
print("\n" + "=" * 100)
print("  GRAND SUMMARY — Bot Actual vs Simulation Strategies")
print("=" * 100)

# Calculate bot totals
bot_total = 117 + 117 - 570 + 23010 + 23010 + 5496 + 17234 + 3684 + 3684 - 148 - 135 + 5728 + 20205 + 1008 + 11669
print(f"\n  BOT ACTUAL TOTAL PnL:    ₹{bot_total:+,.0f}")
print(f"  Bot total trades: 15 (11 wins, 4 losses)")

# Summarize what each strategy would have captured
# Collect all simulation results
all_results = {}
simulations = [
    ('NIFTY 24200 PE (1)', results, 2, 'NIFTY'),
    ('NIFTY 24200 PE (2)', results2, 2, 'NIFTY'),
    ('BN 56700 PE (Gamma)', results_bn1, 2, 'BANKNIFTY'),
    ('BN 56100 PE (CPR)', results_bn2, 2, 'BANKNIFTY'),
    ('SENSEX 78000 PE (1)', results_sx1, 4, 'SENSEX'),
]

print(f"\n  {'Strategy':<20s} {'NIFTY':>12s} {'BANKNIFTY':>12s} {'SENSEX':>12s} {'TOTAL':>14s}")
print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12} {'-'*14}")

for strat in strategies:
    nifty_pnl = 0
    bn_pnl = 0
    sx_pnl = 0
    for name, res, lots, idx_sym in simulations:
        if strat in res:
            pnl = res[strat]['net_pnl']
            if idx_sym == 'NIFTY':
                nifty_pnl += pnl
            elif idx_sym == 'BANKNIFTY':
                bn_pnl += pnl
            else:
                sx_pnl += pnl
    total = nifty_pnl + bn_pnl + sx_pnl
    print(f"  {strat:<20s} ₹{nifty_pnl:>+10,.0f} ₹{bn_pnl:>+10,.0f} ₹{sx_pnl:>+10,.0f} ₹{total:>+12,.0f}")

print(f"\n  {'BOT ACTUAL':<20s} {'':>12s} {'':>12s} {'':>12s} ₹{bot_total:>+12,.0f}")

# Key insight: What was LEFT on the table
print(f"\n  KEY INSIGHT:")
print(f"  Market fell: NIFTY -384 pts, BANKNIFTY -1047 pts, SENSEX -1275 pts")
print(f"  This was a ONE-WAY bearish day — ideal for trend-following PE trades")
print(f"  The bot exited most positions by 11:05 (NIFTY) and 13:27 (BN)")
print(f"  But the fall CONTINUED until 15:30 — the last 2+ hours were missed")
