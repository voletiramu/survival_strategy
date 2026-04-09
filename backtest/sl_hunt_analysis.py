#!/usr/bin/env python3
"""
SL Hunt / Institutional Liquidity Sweep Detector & Simulator
============================================================
Analyzes 1-min spot data to detect:
1. Sharp spikes (institutional SL hunts) — fast moves that reverse
2. The environment before/during/after the hunt
3. Optimal entry/exit points for counter-trade (reversal after hunt)

SL Hunt Anatomy:
  - Institutions push price sharply to trigger retail stop-losses
  - This creates a liquidity pool (SL orders filling)
  - Price then reverses in the "real" direction
  - The entire pattern takes 2-10 minutes

Detection Math:
  - Velocity spike: |dp/dt| > 3σ of recent velocity
  - Acceleration reversal: sign(d²p/dt²) flips during the move
  - Volume/OI surge during the spike
  - Price reversal: retraces >50% of spike within 5-15 bars
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os

DATA_DIR = 'D:/AlgoTrading/data'


def load_spot_data(symbol, date='20260318'):
    """Load 1-min LTP data and construct synthetic OHLC."""
    path = os.path.join(DATA_DIR, f'live_{symbol.lower()}_{date}.csv')
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    # LTP data — use as close, estimate OHLC from adjacent bars
    df['close'] = df['ltp']
    df['open'] = df['close'].shift(1).fillna(df['close'])
    df['high'] = df[['open', 'close']].max(axis=1)
    df['low'] = df[['open', 'close']].min(axis=1)
    return df


def load_indicators(date='20260318'):
    """Load market indicators (PCR, OI) snapshots."""
    path = os.path.join(DATA_DIR, f'live_indicators_{date}.csv')
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df


def detect_sl_hunts(df, symbol, atr_period=20, spike_sigma=2.5, reversal_pct=0.5):
    """
    Detect SL hunt patterns in 1-min data.

    SL Hunt signature:
    1. SPIKE: Price moves > spike_sigma * σ(velocity) in 1-3 bars
    2. REVERSAL: Price retraces > reversal_pct of the spike within 5-15 bars
    3. ACCELERATION: d²p/dt² sign flips (deceleration then reversal)

    Returns list of hunt events with entry/exit simulation.
    """
    prices = df['close'].values
    times = df['timestamp'].values
    n = len(prices)

    if n < atr_period + 10:
        return []

    # ---- Compute derivatives ----
    # Velocity (dp/dt) — 1-bar rate of change
    velocity = np.diff(prices, prepend=prices[0])

    # Acceleration (d²p/dt²) — change in velocity
    accel = np.diff(velocity, prepend=velocity[0])

    # Jerk (d³p/dt³) — change in acceleration (snap detection)
    jerk = np.diff(accel, prepend=accel[0])

    # Rolling velocity σ (20-bar lookback)
    vel_std = pd.Series(velocity).rolling(atr_period, min_periods=5).std().values

    # ATR proxy (20-bar rolling range)
    hi = df['high'].rolling(atr_period, min_periods=5).max()
    lo = df['low'].rolling(atr_period, min_periods=5).min()
    atr = (hi - lo).values

    # Normalized velocity (% move per bar)
    vel_pct = velocity / np.maximum(prices, 1) * 100

    hunts = []

    for i in range(atr_period + 5, n - 15):
        local_vel_std = vel_std[i]
        if local_vel_std == 0 or np.isnan(local_vel_std):
            continue

        # ---- STEP 1: Detect velocity spike ----
        # Z-score of current velocity vs recent σ
        z_score = abs(velocity[i]) / local_vel_std

        if z_score < spike_sigma:
            continue

        # Spike direction
        spike_dir = 'UP' if velocity[i] > 0 else 'DOWN'
        spike_magnitude = abs(vel_pct[i])

        # Check if this is a multi-bar spike (accumulate over 1-3 bars)
        cumulative_move = 0
        spike_bars = 0
        for j in range(3):
            if i + j >= n:
                break
            bar_move = velocity[i + j]
            if (spike_dir == 'UP' and bar_move > 0) or (spike_dir == 'DOWN' and bar_move < 0):
                cumulative_move += bar_move
                spike_bars += 1
            else:
                break

        if spike_bars == 0:
            spike_bars = 1
            cumulative_move = velocity[i]

        spike_size = abs(cumulative_move)
        spike_pct = abs(cumulative_move) / prices[i] * 100

        # Need meaningful spike (> 0.1% for index)
        if spike_pct < 0.08:
            continue

        # ---- STEP 2: Check for reversal ----
        # Look 5-15 bars ahead for price reversal
        spike_peak_idx = i + spike_bars - 1
        if spike_peak_idx >= n:
            continue
        spike_peak = prices[spike_peak_idx]
        spike_start = prices[i - 1] if i > 0 else prices[i]

        best_reversal = 0
        reversal_idx = None
        reversal_time = None

        for k in range(1, min(16, n - spike_peak_idx)):
            check_idx = spike_peak_idx + k
            if check_idx >= n:
                break

            if spike_dir == 'UP':
                # Hunt was UP, reversal is DOWN
                reversal_amount = spike_peak - prices[check_idx]
            else:
                # Hunt was DOWN, reversal is UP
                reversal_amount = prices[check_idx] - spike_peak

            if reversal_amount > best_reversal:
                best_reversal = reversal_amount
                reversal_idx = check_idx
                reversal_time = k  # bars after spike peak

        reversal_ratio = best_reversal / spike_size if spike_size > 0 else 0

        if reversal_ratio < reversal_pct:
            continue  # Not enough reversal — not an SL hunt

        # ---- STEP 3: Check acceleration signature ----
        # During spike: acceleration in spike direction
        # After spike: acceleration reverses (deceleration/reversal)
        accel_during = accel[i]
        accel_after = accel[min(spike_peak_idx + 2, n - 1)] if spike_peak_idx + 2 < n else 0
        accel_flip = (accel_during > 0 and accel_after < 0) or (accel_during < 0 and accel_after > 0)

        # ---- STEP 4: Environment analysis ----
        # Pre-spike: was price in a tight range (accumulation)?
        pre_range = np.ptp(prices[max(0, i-10):i])
        pre_range_pct = pre_range / prices[i] * 100

        # Volume proxy: velocity variance in spike vs pre-spike
        pre_vel_var = np.var(velocity[max(0, i-10):i])
        spike_vel_var = np.var(velocity[i:spike_peak_idx+1]) if spike_bars > 1 else velocity[i]**2
        vol_ratio = spike_vel_var / max(pre_vel_var, 1e-10)

        # ---- STEP 5: Simulate reversal trade ----
        # Entry: 2 bars after spike peak (confirmation of reversal)
        entry_idx = spike_peak_idx + 2
        if entry_idx >= n:
            continue

        entry_price = prices[entry_idx]
        entry_time = times[entry_idx]

        # Direction: opposite to spike (buy the reversal)
        trade_dir = 'PE' if spike_dir == 'UP' else 'CE'

        # Exit: best reversal point or 15-bar timeout
        exit_idx = reversal_idx if reversal_idx else min(entry_idx + 15, n - 1)
        exit_price = prices[exit_idx]

        # PnL in index points
        if trade_dir == 'CE':
            spot_pnl = exit_price - entry_price
        else:
            spot_pnl = entry_price - exit_price

        spot_pnl_pct = spot_pnl / entry_price * 100

        # Premium estimate (ATM option moves ~50% of spot for near-expiry)
        premium_multiplier = 0.5
        option_pnl_pct = spot_pnl_pct * premium_multiplier / 0.01  # rough % of premium

        hunt = {
            'timestamp': pd.Timestamp(times[i]),
            'symbol': symbol,
            'spike_dir': spike_dir,
            'spike_pct': round(spike_pct, 3),
            'spike_bars': spike_bars,
            'spike_size': round(spike_size, 2),
            'reversal_ratio': round(reversal_ratio, 2),
            'reversal_bars': reversal_time,
            'accel_flip': accel_flip,
            'z_score': round(z_score, 1),
            'pre_range_pct': round(pre_range_pct, 3),
            'vol_ratio': round(vol_ratio, 1),
            'trade_dir': trade_dir,
            'entry_price': round(entry_price, 2),
            'entry_time': pd.Timestamp(entry_time),
            'exit_price': round(exit_price, 2),
            'spot_pnl': round(spot_pnl, 2),
            'spot_pnl_pct': round(spot_pnl_pct, 3),
            'spike_start': round(spike_start, 2),
            'spike_peak': round(spike_peak, 2),
            'quality': 0,  # Computed below
        }

        # ---- Quality Score for SL Hunt ----
        q = 0
        if z_score >= 4.0: q += 25
        elif z_score >= 3.0: q += 15
        elif z_score >= 2.5: q += 10

        if reversal_ratio >= 0.8: q += 25  # Full reversal = strongest hunt
        elif reversal_ratio >= 0.6: q += 15
        elif reversal_ratio >= 0.5: q += 10

        if accel_flip: q += 20  # Acceleration confirmed

        if pre_range_pct < 0.15: q += 15  # Tight range before (accumulation)

        if reversal_time and reversal_time <= 5: q += 15  # Fast reversal = strong
        elif reversal_time and reversal_time <= 10: q += 8

        hunt['quality'] = min(q, 100)

        hunts.append(hunt)

    # De-duplicate (remove hunts within 5 bars of each other)
    if hunts:
        filtered = [hunts[0]]
        for h in hunts[1:]:
            time_diff = (h['timestamp'] - filtered[-1]['timestamp']).total_seconds() / 60
            if time_diff >= 5:
                filtered.append(h)
        hunts = filtered

    return hunts


def simulate_sl_hunt_trades(hunts, lot_sizes, symbol):
    """Simulate actual option trades from detected SL hunts."""
    lot = lot_sizes.get(symbol, 50)

    results = []
    for h in hunts:
        # Estimate ATM option premium (rough: 0.4% of spot for near-expiry)
        atm_premium = h['entry_price'] * 0.004

        # Option PnL: delta * spot_move * lot_size
        delta = 0.50  # ATM delta
        spot_move = h['spot_pnl']
        option_pnl_per_lot = delta * spot_move * lot

        # Premium change estimate
        premium_entry = atm_premium
        premium_exit = premium_entry + (delta * spot_move)
        premium_pnl_pct = (premium_exit - premium_entry) / premium_entry * 100

        results.append({
            **h,
            'lot_size': lot,
            'delta': delta,
            'est_premium_entry': round(premium_entry, 2),
            'est_premium_exit': round(premium_exit, 2),
            'est_option_pnl': round(option_pnl_per_lot, 2),
            'est_premium_pnl_pct': round(premium_pnl_pct, 1),
        })

    return results


def analyze_environment(hunts_df, indicators_df, symbol):
    """Analyze market environment when SL hunts occur."""
    if hunts_df.empty or indicators_df.empty:
        return {}

    sym_ind = indicators_df[indicators_df['symbol'] == symbol].copy()
    if sym_ind.empty:
        return {}

    env_stats = []
    for _, hunt in hunts_df.iterrows():
        ht = hunt['timestamp']
        # Find nearest indicator snapshot
        sym_ind['tdiff'] = abs((sym_ind['timestamp'] - ht).dt.total_seconds())
        nearest = sym_ind.loc[sym_ind['tdiff'].idxmin()]

        env_stats.append({
            'hunt_time': ht,
            'pcr': nearest.get('pcr', np.nan),
            'vix': nearest.get('vix', np.nan),
            'oi_call': nearest.get('oi_call', 0),
            'oi_put': nearest.get('oi_put', 0),
            'oi_change_call': nearest.get('oi_change_call', 0),
            'oi_change_put': nearest.get('oi_change_put', 0),
        })

    return pd.DataFrame(env_stats)


def main():
    lot_sizes = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20}
    symbols = ['NIFTY', 'BANKNIFTY', 'SENSEX']

    # Load indicators
    indicators = load_indicators()

    all_hunts = []

    for symbol in symbols:
        print(f"\n{'='*70}")
        print(f"  SL HUNT ANALYSIS: {symbol}")
        print(f"{'='*70}")

        try:
            df = load_spot_data(symbol)
        except Exception as e:
            print(f"  Error loading {symbol}: {e}")
            continue

        print(f"  Bars: {len(df)} | Range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
        print(f"  Price: Low={df['close'].min():.2f} High={df['close'].max():.2f}")
        print(f"  Day Range: {(df['close'].max() - df['close'].min()):.2f} pts "
              f"({(df['close'].max() - df['close'].min()) / df['close'].iloc[0] * 100:.2f}%)")

        # Detect SL hunts with different sensitivity
        hunts = detect_sl_hunts(df, symbol, spike_sigma=2.0, reversal_pct=0.4)

        if not hunts:
            print(f"  No SL hunts detected")
            continue

        print(f"\n  Detected {len(hunts)} SL Hunt patterns:")
        print(f"  {'Time':>8} {'Dir':>4} {'Spike%':>7} {'Bars':>4} {'Rev%':>5} "
              f"{'RevBars':>7} {'AccFlip':>7} {'Z':>4} {'Trade':>5} {'SpotPnL':>8} {'Q':>3}")
        print(f"  {'-'*80}")

        # Simulate trades
        trades = simulate_sl_hunt_trades(hunts, lot_sizes, symbol)

        total_pnl = 0
        winners = 0
        losers = 0

        for t in trades:
            ts = t['timestamp'].strftime('%H:%M')
            win = 'W' if t['spot_pnl'] > 0 else 'L'
            total_pnl += t['est_option_pnl']
            if t['spot_pnl'] > 0:
                winners += 1
            else:
                losers += 1

            print(f"  {ts:>8} {t['spike_dir']:>4} {t['spike_pct']:>7.3f} "
                  f"{t['spike_bars']:>4} {t['reversal_ratio']:>5.2f} "
                  f"{t['reversal_bars']:>7} {str(t['accel_flip']):>7} "
                  f"{t['z_score']:>4.1f} {t['trade_dir']:>5} "
                  f"{t['spot_pnl']:>8.2f} {t['quality']:>3} {win}")

            t['symbol'] = symbol
            all_hunts.append(t)

        wr = winners / (winners + losers) * 100 if (winners + losers) > 0 else 0
        print(f"\n  Summary: {winners}W / {losers}L | Win Rate: {wr:.0f}% | "
              f"Est Option PnL: Rs {total_pnl:,.0f}")

    # ---- Overall Summary ----
    if all_hunts:
        print(f"\n{'='*70}")
        print(f"  OVERALL SL HUNT ANALYSIS — March 18, 2026")
        print(f"{'='*70}")

        hunts_df = pd.DataFrame(all_hunts)

        total_trades = len(hunts_df)
        total_winners = len(hunts_df[hunts_df['spot_pnl'] > 0])
        total_losers = len(hunts_df[hunts_df['spot_pnl'] <= 0])
        total_option_pnl = hunts_df['est_option_pnl'].sum()
        avg_quality = hunts_df['quality'].mean()

        print(f"  Total hunts detected: {total_trades}")
        print(f"  Winners: {total_winners} | Losers: {total_losers} | "
              f"Win Rate: {total_winners/total_trades*100:.0f}%")
        print(f"  Est Total Option PnL: Rs {total_option_pnl:,.0f}")
        print(f"  Avg Hunt Quality: {avg_quality:.0f}/100")

        # Quality filter analysis
        print(f"\n  --- QUALITY FILTER ANALYSIS ---")
        for q_min in [40, 50, 60, 70]:
            hq = hunts_df[hunts_df['quality'] >= q_min]
            if len(hq) == 0:
                continue
            hq_w = len(hq[hq['spot_pnl'] > 0])
            hq_l = len(hq[hq['spot_pnl'] <= 0])
            hq_wr = hq_w / len(hq) * 100
            hq_pnl = hq['est_option_pnl'].sum()
            print(f"  Q >= {q_min}: {len(hq)} trades, WR={hq_wr:.0f}%, PnL=Rs {hq_pnl:,.0f}")

        # Spike direction analysis
        print(f"\n  --- SPIKE DIRECTION ---")
        for d in ['UP', 'DOWN']:
            sd = hunts_df[hunts_df['spike_dir'] == d]
            if len(sd) == 0:
                continue
            w = len(sd[sd['spot_pnl'] > 0])
            print(f"  {d} spikes: {len(sd)} | Winners: {w} ({w/len(sd)*100:.0f}%) "
                  f"| Avg Spike: {sd['spike_pct'].mean():.3f}%")

        # Time-of-day analysis
        print(f"\n  --- TIME OF DAY ---")
        hunts_df['hour'] = hunts_df['timestamp'].dt.hour
        for hr in sorted(hunts_df['hour'].unique()):
            hh = hunts_df[hunts_df['hour'] == hr]
            w = len(hh[hh['spot_pnl'] > 0])
            print(f"  {hr}:00-{hr}:59: {len(hh)} hunts, {w} winners ({w/len(hh)*100:.0f}%)")

        # Environment analysis
        print(f"\n  --- ENVIRONMENT AT HUNT TIME ---")
        env = analyze_environment(hunts_df, indicators, 'NIFTY')
        if not env.empty:
            print(f"  Avg PCR at hunt: {env['pcr'].mean():.3f}")
            print(f"  Avg VIX at hunt: {env['vix'].mean():.1f}")

        # Save to Excel
        output_path = 'D:/AlgoTrading/data/sl_hunt_simulation_20260318.xlsx'
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            hunts_df.to_excel(writer, sheet_name='SL_Hunts', index=False)

            # Summary sheet
            summary_data = {
                'Metric': ['Total Hunts', 'Winners', 'Losers', 'Win Rate',
                           'Est Option PnL', 'Avg Quality', 'Avg Spike%',
                           'Avg Reversal Ratio', 'Avg Reversal Bars'],
                'Value': [total_trades, total_winners, total_losers,
                          f"{total_winners/total_trades*100:.0f}%",
                          f"Rs {total_option_pnl:,.0f}", f"{avg_quality:.0f}",
                          f"{hunts_df['spike_pct'].mean():.3f}%",
                          f"{hunts_df['reversal_ratio'].mean():.2f}",
                          f"{hunts_df['reversal_bars'].mean():.1f}"]
            }
            pd.DataFrame(summary_data).to_excel(writer, sheet_name='Summary', index=False)

        print(f"\n  Excel saved: {output_path}")

    return all_hunts


if __name__ == '__main__':
    hunts = main()
