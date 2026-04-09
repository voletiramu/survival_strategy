"""v10.2 Backtest — Simulate direction gate + direction flip on today's live signals.

Reads today's signal CSVs (equity + commodity) and simulates:
1. Which signals would PASS the 5-factor direction gate
2. Which would be REJECTED
3. Which rejected signals would be FLIPPED to opposite direction
4. Estimates PnL using premium movement (entry premium vs EOD/close premium)

This is INDEPENDENT of actual trades taken by the bot.
"""

import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data')


def validate_signal_direction(signal_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar_trend):
    """Simulate the v10.2 5-factor direction validation."""
    is_ce = 'CE' in signal_type
    is_sell = 'SELL' in signal_type
    if is_sell:
        return True, 'SELL_EXEMPT', 0, []

    checks = []
    passed = 0

    # Check 1: VWAP alignment
    if vwap and vwap > 0:
        if (is_ce and spot > vwap) or (not is_ce and spot < vwap):
            passed += 1; checks.append('VWAP+')
        else:
            checks.append('VWAP-')

    # Check 2: Day trend (spot vs prev_close)
    if prev_close and prev_close > 0:
        if (is_ce and spot > prev_close) or (not is_ce and spot < prev_close):
            passed += 1; checks.append('TREND+')
        else:
            checks.append('TREND-')

    # Check 3: Intraday direction (spot vs open)
    if open_price and open_price > 0:
        if (is_ce and spot > open_price) or (not is_ce and spot < open_price):
            passed += 1; checks.append('INTRA+')
        else:
            checks.append('INTRA-')

    # Check 4: EMA trend (9 vs 20)
    if ema_9 > 0 and ema_20 > 0:
        if (is_ce and ema_9 > ema_20) or (not is_ce and ema_9 < ema_20):
            passed += 1; checks.append('EMA+')
        else:
            checks.append('EMA-')

    # Check 5: 3-bar momentum
    if (is_ce and three_bar_trend >= 2) or (not is_ce and three_bar_trend <= -2):
        passed += 1; checks.append('MOM+')
    elif three_bar_trend == 0:
        checks.append('MOM~')
    else:
        checks.append('MOM-')

    is_valid = passed >= 3
    score_adj = (passed - 3) * 5
    return is_valid, f"DIR({passed}/5: {' '.join(checks)})", score_adj, checks


def load_equity_signals(filepath):
    """Load equity signals CSV."""
    df = pd.read_csv(filepath)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['hour'] = df['timestamp'].dt.hour
    df['minute'] = df['timestamp'].dt.minute
    return df


def load_commodity_signals(filepath):
    """Load commodity signals CSV."""
    df = pd.read_csv(filepath)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['hour'] = df['timestamp'].dt.hour
    df['minute'] = df['timestamp'].dt.minute
    return df


def compute_direction_indicators(df_group):
    """Compute direction indicators from signal group (same symbol, sorted by time).

    Uses rolling data from signals to approximate VWAP, EMA, trend, etc.
    """
    # For each unique (symbol, timestamp), get the spot and VWAP from signal data
    # Group by symbol and compute running indicators
    results = {}
    for symbol, sym_df in df_group.groupby('symbol' if 'symbol' in df_group.columns else 'commodity'):
        sym_df = sym_df.sort_values('timestamp')
        spots = sym_df['spot'].values
        vwaps = sym_df['vwap'].values if 'vwap' in sym_df.columns else np.zeros(len(spots))

        # Compute running EMA-9 and EMA-20 from spot prices at scan times
        ema_9 = pd.Series(spots).ewm(span=min(9, len(spots))).mean().values
        ema_20 = pd.Series(spots).ewm(span=min(20, len(spots))).mean().values

        # 3-bar trend: compare consecutive spots
        three_bar = np.zeros(len(spots))
        for i in range(len(spots)):
            start = max(0, i - 3)
            trend = 0
            for j in range(start + 1, i + 1):
                trend += 1 if spots[j] > spots[j-1] else -1
            three_bar[i] = trend

        # First spot as "open" for intraday
        open_price = spots[0] if len(spots) > 0 else 0
        prev_close = spots[0] * 0.998  # Approximate: slightly below open (will use actual if available)

        # v10.2b: Daily EMA trend proxy — overall day direction
        # The real bot uses multi-day EMA from historical data. We approximate using
        # the day's overall price movement (last spot vs first spot).
        # This avoids the problem where running intraday EMA flips during temporary dips.
        daily_ema_bullish = spots[-1] > spots[0] if len(spots) >= 2 else True

        for idx, row in sym_df.iterrows():
            i = sym_df.index.get_loc(idx)
            results[idx] = {
                'vwap': vwaps[i] if i < len(vwaps) and not np.isnan(vwaps[i]) else 0,
                'ema_9': ema_9[i] if i < len(ema_9) else 0,
                'ema_20': ema_20[i] if i < len(ema_20) else 0,
                'three_bar': three_bar[i] if i < len(three_bar) else 0,
                'open': open_price,
                'prev_close': prev_close,
                'daily_ema_bullish': daily_ema_bullish,
            }

    return results


def simulate_direction_gate(df, is_commodity=False):
    """Simulate v10.2 direction gate on all signals.

    Returns DataFrame with additional columns:
    - dir_valid: bool
    - dir_reason: str
    - dir_flip: bool (was flipped from original)
    - dir_flipped_type: str (the flipped type if applicable)
    - final_type: str (the type after potential flip)
    """
    symbol_col = 'commodity' if is_commodity else 'symbol'

    # Compute direction indicators per symbol
    dir_indicators = compute_direction_indicators(df)

    results = []
    for idx, row in df.iterrows():
        ind = dir_indicators.get(idx, {})
        spot = row['spot']
        vwap = ind.get('vwap', row.get('vwap', 0))
        prev_close = ind.get('prev_close', spot * 0.998)
        open_price = ind.get('open', spot)
        ema_9 = ind.get('ema_9', 0)
        ema_20 = ind.get('ema_20', 0)
        three_bar = ind.get('three_bar', 0)

        sig_type = row['type']

        # Step 1: Validate original direction
        valid, reason, adj, checks = validate_signal_direction(
            sig_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar)

        flipped = False
        flipped_type = ''
        final_type = sig_type
        flip_reason = ''

        if not valid:
            # v10.2b: EMA trend guard — don't flip AGAINST the daily EMA trend
            # If daily bullish, only flip to CE, never to PE
            # If daily bearish, only flip to PE, never to CE
            if 'CE' in sig_type:
                flipped_type = sig_type.replace('CE', 'PE')
            else:
                flipped_type = sig_type.replace('PE', 'CE')

            flip_to_pe = 'PE' in flipped_type
            ema_blocks_flip = False
            daily_bullish = ind.get('daily_ema_bullish', True)
            if (daily_bullish and flip_to_pe) or (not daily_bullish and not flip_to_pe):
                ema_blocks_flip = True
                flip_reason = f"no_flip:daily_{'bullish' if daily_bullish else 'bearish'}_vs_{'PE' if flip_to_pe else 'CE'}"

            if not ema_blocks_flip:
                flip_valid, flip_reason_str, flip_adj, flip_checks = validate_signal_direction(
                    flipped_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar)

                if flip_valid:
                    flipped = True
                    final_type = flipped_type
                    flip_reason = flip_reason_str
                    adj = flip_adj
                else:
                    flip_reason = flip_reason_str

        results.append({
            'idx': idx,
            'dir_valid': valid,
            'dir_reason': reason,
            'dir_flipped': flipped,
            'dir_flipped_type': flipped_type,
            'dir_flip_reason': flip_reason,
            'final_type': final_type,
            'dir_adj': adj,
            'vwap_used': vwap,
            'ema_9': ema_9,
            'ema_20': ema_20,
        })

    result_df = pd.DataFrame(results).set_index('idx')
    return pd.concat([df, result_df], axis=1)


def estimate_premium_at_eod(entry_premium, entry_time, opt_type, spot_at_entry, eod_spot, dte):
    """Rough estimate of option premium at EOD based on spot movement.

    For BUY positions:
    - CE: if spot goes up, premium increases; if down, decreases
    - PE: if spot goes down, premium increases; if up, decreases

    Uses approximate delta of 0.5 (ATM) and theta decay.
    """
    spot_change = eod_spot - spot_at_entry
    is_ce = 'CE' in opt_type

    # ATM delta ~ 0.5, adjust by direction
    delta = 0.5 if is_ce else -0.5
    premium_from_spot = spot_change * delta

    # Theta decay (lose ~1/DTE of premium per day, prorated by hours remaining)
    hours_remaining = max(0, 15.5 - entry_time.hour - entry_time.minute / 60)
    hours_total = 6.25  # 9:15 to 15:30
    theta_decay = entry_premium * 0.02 * (hours_remaining / hours_total)  # ~2% theta per day

    eod_premium = max(0.05, entry_premium + premium_from_spot - theta_decay)
    return round(eod_premium, 2)


def backtest_equity(filepath, portfolio_path=None):
    """Backtest equity signals with v10.2 direction gate + flip."""
    print("\n" + "=" * 80)
    print("EQUITY BACKTEST — v10.2 Direction Gate + Flip")
    print("=" * 80)

    df = load_equity_signals(filepath)
    print(f"\nLoaded {len(df)} equity signals from {filepath}")
    print(f"Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(f"Symbols: {df['symbol'].nunique()} — {', '.join(df['symbol'].unique())}")
    print(f"Strategies: {df['strategy'].nunique()} — {', '.join(df['strategy'].unique())}")

    # Filter to market hours (9:15 - 15:30) and entry window (9:30 - 14:30)
    df_market = df[(df['hour'] >= 9) & ((df['hour'] < 14) | ((df['hour'] == 14) & (df['minute'] <= 30)))]
    df_market = df_market[~((df_market['hour'] == 9) & (df_market['minute'] < 30))]
    print(f"After market hours filter (9:30-14:30): {len(df_market)} signals")

    if len(df_market) == 0:
        print("No signals in entry window!")
        return None

    # Apply direction gate
    df_result = simulate_direction_gate(df_market, is_commodity=False)

    # Analyze results
    total = len(df_result)
    dir_pass = df_result['dir_valid'].sum()
    dir_reject_no_flip = ((~df_result['dir_valid']) & (~df_result['dir_flipped'])).sum()
    dir_flipped = df_result['dir_flipped'].sum()

    print(f"\n--- Direction Gate Results ---")
    print(f"  Total signals:       {total}")
    print(f"  DIR_PASS:            {dir_pass} ({dir_pass/total*100:.1f}%) — original direction OK")
    print(f"  DIR_FLIP:            {dir_flipped} ({dir_flipped/total*100:.1f}%) — flipped to opposite direction")
    print(f"  DIR_REJECT:          {dir_reject_no_flip} ({dir_reject_no_flip/total*100:.1f}%) — both directions failed")
    print(f"  Tradeable (pass+flip): {dir_pass + dir_flipped} ({(dir_pass + dir_flipped)/total*100:.1f}%)")

    # Break down by symbol
    print(f"\n--- Per-Symbol Breakdown ---")
    for symbol in sorted(df_result['symbol'].unique()):
        sym_df = df_result[df_result['symbol'] == symbol]
        sym_pass = sym_df['dir_valid'].sum()
        sym_flip = sym_df['dir_flipped'].sum()
        sym_reject = len(sym_df) - sym_pass - sym_flip

        # Direction distribution
        ce_count = sym_df[sym_df['final_type'].str.contains('CE')].shape[0]
        pe_count = sym_df[sym_df['final_type'].str.contains('PE')].shape[0]

        print(f"  {symbol:12s}: {len(sym_df):4d} signals | "
              f"PASS={sym_pass:4d} FLIP={sym_flip:3d} REJECT={sym_reject:3d} | "
              f"Final: CE={ce_count} PE={pe_count}")

    # Break down by strategy
    print(f"\n--- Per-Strategy Breakdown ---")
    for strat in sorted(df_result['strategy'].unique()):
        strat_df = df_result[df_result['strategy'] == strat]
        s_pass = strat_df['dir_valid'].sum()
        s_flip = strat_df['dir_flipped'].sum()
        s_reject = len(strat_df) - s_pass - s_flip
        print(f"  {strat:15s}: {len(strat_df):4d} signals | PASS={s_pass:4d} FLIP={s_flip:3d} REJECT={s_reject:3d}")

    # Simulate trades: take first signal per symbol per direction per 30-min window
    # This simulates the bot's behavior of not taking duplicate trades
    print(f"\n--- Simulated Trade Selection ---")
    df_tradeable = df_result[(df_result['dir_valid']) | (df_result['dir_flipped'])].copy()

    # Apply quality score filter (MIN_SIGNAL_SCORE = 50)
    if 'quality_score' in df_tradeable.columns:
        df_scored = df_tradeable[df_tradeable['quality_score'] >= 50].copy()
        print(f"  After quality score >= 50: {len(df_scored)} signals")
    else:
        df_scored = df_tradeable.copy()

    # Deduplicate: first signal per symbol per 30-min window per final_type
    df_scored['time_bucket'] = df_scored['timestamp'].dt.floor('30min')
    trades = df_scored.groupby(['symbol' if 'symbol' in df_scored.columns else 'commodity',
                                 'time_bucket', 'final_type']).first().reset_index()

    print(f"  Unique trade candidates (30-min dedup): {len(trades)}")

    # Estimate PnL for each trade
    # Get EOD spot for each symbol (last signal's spot)
    eod_spots = {}
    for symbol in df['symbol'].unique():
        sym_signals = df[df['symbol'] == symbol].sort_values('timestamp')
        eod_spots[symbol] = sym_signals.iloc[-1]['spot']

    total_pnl = 0
    trade_results = []
    lot_sizes = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}

    for _, trade in trades.iterrows():
        symbol = trade['symbol']
        entry_premium = trade['premium']
        entry_time = trade['timestamp']
        final_type = trade['final_type']
        spot_at_entry = trade['spot']
        eod_spot = eod_spots.get(symbol, spot_at_entry)
        dte = trade.get('dte', 5)
        was_flipped = trade.get('dir_flipped', False)

        # Estimate EOD premium
        eod_premium = estimate_premium_at_eod(
            entry_premium, entry_time, final_type, spot_at_entry, eod_spot, dte)

        lot = lot_sizes.get(symbol, 50)
        pnl = (eod_premium - entry_premium) * lot
        total_pnl += pnl

        trade_results.append({
            'time': entry_time.strftime('%H:%M'),
            'symbol': symbol,
            'strategy': trade['strategy'],
            'original_type': trade['type'],
            'final_type': final_type,
            'flipped': was_flipped,
            'entry_prem': entry_premium,
            'eod_prem': eod_premium,
            'spot_entry': spot_at_entry,
            'spot_eod': eod_spot,
            'lot': lot,
            'pnl': round(pnl, 0),
            'score': trade.get('quality_score', 0),
        })

    # Print trade results
    print(f"\n--- Simulated Trades (v10.2) ---")
    print(f"{'Time':>5s} {'Symbol':>10s} {'Strategy':>12s} {'Original':>15s} {'Final':>15s} {'Flip':>4s} "
          f"{'Entry':>8s} {'EOD':>8s} {'Spot':>10s} {'SpotEOD':>10s} {'Lot':>5s} {'PnL':>10s} {'Score':>5s}")
    print("-" * 130)

    winners = 0
    losers = 0
    for t in sorted(trade_results, key=lambda x: x['time']):
        flip_marker = 'FLIP' if t['flipped'] else ''
        pnl_str = f"Rs {t['pnl']:,.0f}"
        if t['pnl'] > 0:
            winners += 1
        else:
            losers += 1

        print(f"{t['time']:>5s} {t['symbol']:>10s} {t['strategy']:>12s} {t['original_type']:>15s} "
              f"{t['final_type']:>15s} {flip_marker:>4s} "
              f"{t['entry_prem']:>8.2f} {t['eod_prem']:>8.2f} "
              f"{t['spot_entry']:>10.0f} {t['spot_eod']:>10.0f} {t['lot']:>5d} {pnl_str:>10s} {t['score']:>5.0f}")

    print("-" * 130)
    print(f"TOTAL PnL: Rs {total_pnl:,.0f} | Winners: {winners} | Losers: {losers} | "
          f"Win Rate: {winners/(winners+losers)*100:.0f}%" if winners + losers > 0 else "No trades")

    # Compare: what old code (no direction gate) would have done
    print(f"\n--- Comparison: Old Code (No Direction Gate) ---")
    # Old code would have taken all signals that pass quality score
    if 'quality_score' in df_market.columns:
        old_tradeable = df_market[df_market['quality_score'] >= 50].copy()
    else:
        old_tradeable = df_market.copy()

    old_tradeable['time_bucket'] = old_tradeable['timestamp'].dt.floor('30min')
    old_trades = old_tradeable.groupby(['symbol', 'time_bucket', 'type']).first().reset_index()

    old_pnl = 0
    old_winners = 0
    old_losers = 0
    for _, trade in old_trades.iterrows():
        symbol = trade['symbol']
        entry_premium = trade['premium']
        entry_time = trade['timestamp']
        spot_at_entry = trade['spot']
        eod_spot = eod_spots.get(symbol, spot_at_entry)
        dte = trade.get('dte', 5)

        eod_premium = estimate_premium_at_eod(
            entry_premium, entry_time, trade['type'], spot_at_entry, eod_spot, dte)

        lot = lot_sizes.get(symbol, 50)
        pnl = (eod_premium - entry_premium) * lot
        old_pnl += pnl
        if pnl > 0:
            old_winners += 1
        else:
            old_losers += 1

    print(f"  Old code trades: {len(old_trades)} | PnL: Rs {old_pnl:,.0f} | "
          f"Win: {old_winners} | Loss: {old_losers} | "
          f"WR: {old_winners/(old_winners+old_losers)*100:.0f}%" if old_winners + old_losers > 0 else "No trades")
    print(f"  v10.2 trades:    {len(trade_results)} | PnL: Rs {total_pnl:,.0f} | "
          f"Win: {winners} | Loss: {losers} | "
          f"WR: {winners/(winners+losers)*100:.0f}%" if winners + losers > 0 else "No trades")
    print(f"  Improvement:     Rs {total_pnl - old_pnl:,.0f}")

    return {
        'total_signals': total,
        'dir_pass': dir_pass,
        'dir_flip': dir_flipped,
        'dir_reject': dir_reject_no_flip,
        'trades': len(trade_results),
        'pnl': total_pnl,
        'winners': winners,
        'losers': losers,
        'old_pnl': old_pnl,
        'old_trades': len(old_trades),
    }


def backtest_commodity(filepath):
    """Backtest commodity signals with v10.2 direction gate + flip."""
    print("\n" + "=" * 80)
    print("COMMODITY BACKTEST — v10.2 Direction Gate + Flip")
    print("=" * 80)

    df = load_commodity_signals(filepath)
    print(f"\nLoaded {len(df)} commodity signals from {filepath}")
    print(f"Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(f"Commodities: {df['commodity'].nunique()} — {', '.join(df['commodity'].unique())}")
    print(f"Strategies: {df['strategy'].nunique()} — {', '.join(df['strategy'].unique())}")

    # Filter to MCX hours and entry window (9:00 - 22:30)
    df_market = df.copy()  # MCX has wide hours, keep all
    # Remove late entries (after 22:30)
    df_market = df_market[~((df_market['hour'] >= 22) & (df_market['minute'] > 30))]
    # Remove early entries (before 9:15)
    df_market = df_market[~((df_market['hour'] < 9) | ((df_market['hour'] == 9) & (df_market['minute'] < 15)))]
    print(f"After market hours filter: {len(df_market)} signals")

    if len(df_market) == 0:
        print("No signals in entry window!")
        return None

    # Apply direction gate
    df_result = simulate_direction_gate(df_market, is_commodity=True)

    # Analyze results
    total = len(df_result)
    dir_pass = df_result['dir_valid'].sum()
    dir_reject_no_flip = ((~df_result['dir_valid']) & (~df_result['dir_flipped'])).sum()
    dir_flipped = df_result['dir_flipped'].sum()

    print(f"\n--- Direction Gate Results ---")
    print(f"  Total signals:       {total}")
    print(f"  DIR_PASS:            {dir_pass} ({dir_pass/total*100:.1f}%) — original direction OK")
    print(f"  DIR_FLIP:            {dir_flipped} ({dir_flipped/total*100:.1f}%) — flipped to opposite direction")
    print(f"  DIR_REJECT:          {dir_reject_no_flip} ({dir_reject_no_flip/total*100:.1f}%) — both directions failed")
    print(f"  Tradeable (pass+flip): {dir_pass + dir_flipped} ({(dir_pass + dir_flipped)/total*100:.1f}%)")

    # Break down by commodity
    print(f"\n--- Per-Commodity Breakdown ---")
    for commodity in sorted(df_result['commodity'].unique()):
        com_df = df_result[df_result['commodity'] == commodity]
        c_pass = com_df['dir_valid'].sum()
        c_flip = com_df['dir_flipped'].sum()
        c_reject = len(com_df) - c_pass - c_flip

        ce_count = com_df[com_df['final_type'].str.contains('CE')].shape[0]
        pe_count = com_df[com_df['final_type'].str.contains('PE')].shape[0]

        print(f"  {commodity:12s}: {len(com_df):4d} signals | "
              f"PASS={c_pass:4d} FLIP={c_flip:3d} REJECT={c_reject:3d} | "
              f"Final: CE={ce_count} PE={pe_count}")

    # Simulate trades
    print(f"\n--- Simulated Trade Selection ---")
    df_tradeable = df_result[(df_result['dir_valid']) | (df_result['dir_flipped'])].copy()

    if 'quality_score' in df_tradeable.columns:
        df_scored = df_tradeable[df_tradeable['quality_score'] >= 50].copy()
        print(f"  After quality score >= 50: {len(df_scored)} signals")
    else:
        df_scored = df_tradeable.copy()

    # Deduplicate
    df_scored['time_bucket'] = df_scored['timestamp'].dt.floor('30min')
    trades = df_scored.groupby(['commodity', 'time_bucket', 'final_type']).first().reset_index()
    print(f"  Unique trade candidates (30-min dedup): {len(trades)}")

    # Get EOD spots
    eod_spots = {}
    for commodity in df['commodity'].unique():
        com_signals = df[df['commodity'] == commodity].sort_values('timestamp')
        eod_spots[commodity] = com_signals.iloc[-1]['spot']

    # MCX multipliers
    multipliers = {'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 10, 'GOLD': 100, 'SILVER': 30}

    total_pnl = 0
    trade_results = []

    for _, trade in trades.iterrows():
        commodity = trade['commodity']
        entry_premium = trade['premium']
        entry_time = trade['timestamp']
        final_type = trade['final_type']
        spot_at_entry = trade['spot']
        eod_spot = eod_spots.get(commodity, spot_at_entry)
        dte = trade.get('dte', 5)
        was_flipped = trade.get('dir_flipped', False)
        mult = multipliers.get(commodity, 10)

        # Estimate EOD premium using spot movement
        spot_change = eod_spot - spot_at_entry
        is_ce = 'CE' in final_type
        delta = 0.5 if is_ce else -0.5
        premium_change = spot_change * delta

        # Theta decay
        hours_remaining = max(0, 23.5 - entry_time.hour - entry_time.minute / 60)
        theta = entry_premium * 0.015 * min(1, hours_remaining / 14)

        eod_premium = max(0.5, entry_premium + premium_change - theta)

        pnl = (eod_premium - entry_premium) * mult  # lot_size=1 for all commodities
        total_pnl += pnl

        trade_results.append({
            'time': entry_time.strftime('%H:%M'),
            'commodity': commodity,
            'strategy': trade['strategy'],
            'original_type': trade['type'],
            'final_type': final_type,
            'flipped': was_flipped,
            'entry_prem': entry_premium,
            'eod_prem': round(eod_premium, 2),
            'spot_entry': spot_at_entry,
            'spot_eod': eod_spot,
            'mult': mult,
            'pnl': round(pnl, 0),
            'score': trade.get('quality_score', 0),
        })

    # Print results
    print(f"\n--- Simulated Trades (v10.2) ---")
    print(f"{'Time':>5s} {'Commodity':>12s} {'Strategy':>12s} {'Original':>15s} {'Final':>15s} {'Flip':>4s} "
          f"{'Entry':>10s} {'EOD':>10s} {'Spot':>12s} {'SpotEOD':>12s} {'Mult':>4s} {'PnL':>10s}")
    print("-" * 130)

    winners = 0
    losers = 0
    for t in sorted(trade_results, key=lambda x: x['time']):
        flip_marker = 'FLIP' if t['flipped'] else ''
        pnl_str = f"Rs {t['pnl']:,.0f}"
        if t['pnl'] > 0:
            winners += 1
        else:
            losers += 1

        print(f"{t['time']:>5s} {t['commodity']:>12s} {t['strategy']:>12s} {t['original_type']:>15s} "
              f"{t['final_type']:>15s} {flip_marker:>4s} "
              f"{t['entry_prem']:>10.2f} {t['eod_prem']:>10.2f} "
              f"{t['spot_entry']:>12.0f} {t['spot_eod']:>12.0f} {t['mult']:>4d} {pnl_str:>10s}")

    print("-" * 130)
    if winners + losers > 0:
        print(f"TOTAL PnL: Rs {total_pnl:,.0f} | Winners: {winners} | Losers: {losers} | "
              f"Win Rate: {winners/(winners+losers)*100:.0f}%")

    # Old code comparison
    print(f"\n--- Comparison: Old Code (No Direction Gate) ---")
    if 'quality_score' in df_market.columns:
        old_tradeable = df_market[df_market['quality_score'] >= 50].copy()
    else:
        old_tradeable = df_market.copy()

    old_tradeable['time_bucket'] = old_tradeable['timestamp'].dt.floor('30min')
    old_trades = old_tradeable.groupby(['commodity', 'time_bucket', 'type']).first().reset_index()

    old_pnl = 0
    old_winners = 0
    old_losers = 0
    for _, trade in old_trades.iterrows():
        commodity = trade['commodity']
        entry_premium = trade['premium']
        entry_time = trade['timestamp']
        spot_at_entry = trade['spot']
        eod_spot = eod_spots.get(commodity, spot_at_entry)
        mult = multipliers.get(commodity, 10)

        spot_change = eod_spot - spot_at_entry
        is_ce = 'CE' in trade['type']
        delta = 0.5 if is_ce else -0.5
        premium_change = spot_change * delta
        theta = entry_premium * 0.015 * 0.5
        eod_premium = max(0.5, entry_premium + premium_change - theta)

        pnl = (eod_premium - entry_premium) * mult
        old_pnl += pnl
        if pnl > 0:
            old_winners += 1
        else:
            old_losers += 1

    if old_winners + old_losers > 0:
        print(f"  Old code trades: {len(old_trades)} | PnL: Rs {old_pnl:,.0f} | "
              f"Win: {old_winners} | Loss: {old_losers} | "
              f"WR: {old_winners/(old_winners+old_losers)*100:.0f}%")
    if winners + losers > 0:
        print(f"  v10.2 trades:    {len(trade_results)} | PnL: Rs {total_pnl:,.0f} | "
              f"Win: {winners} | Loss: {losers} | "
              f"WR: {winners/(winners+losers)*100:.0f}%")
    print(f"  Improvement:     Rs {total_pnl - old_pnl:,.0f}")

    return {
        'total_signals': total,
        'dir_pass': dir_pass,
        'dir_flip': dir_flipped,
        'dir_reject': dir_reject_no_flip,
        'trades': len(trade_results),
        'pnl': total_pnl,
        'winners': winners,
        'losers': losers,
        'old_pnl': old_pnl,
        'old_trades': len(old_trades),
    }


if __name__ == '__main__':
    equity_file = os.path.join(DATA_DIR, 'equity_signals_20260310_full.csv')
    commodity_file = os.path.join(DATA_DIR, 'commodity_signals_20260310_full.csv')

    equity_result = None
    commodity_result = None

    if os.path.exists(equity_file):
        equity_result = backtest_equity(equity_file)
    else:
        print(f"Equity signals file not found: {equity_file}")

    if os.path.exists(commodity_file):
        commodity_result = backtest_commodity(commodity_file)
    else:
        print(f"Commodity signals file not found: {commodity_file}")

    # Final summary
    print("\n" + "=" * 80)
    print("COMBINED BACKTEST SUMMARY — v10.2 vs Old Code")
    print("=" * 80)

    total_v10_pnl = 0
    total_old_pnl = 0

    if equity_result:
        print(f"\n  EQUITY:")
        print(f"    Signals: {equity_result['total_signals']} | "
              f"PASS: {equity_result['dir_pass']} | FLIP: {equity_result['dir_flip']} | REJECT: {equity_result['dir_reject']}")
        print(f"    v10.2: {equity_result['trades']} trades, Rs {equity_result['pnl']:,.0f} PnL, "
              f"{equity_result['winners']}W/{equity_result['losers']}L")
        print(f"    Old:   {equity_result['old_trades']} trades, Rs {equity_result['old_pnl']:,.0f} PnL")
        total_v10_pnl += equity_result['pnl']
        total_old_pnl += equity_result['old_pnl']

    if commodity_result:
        print(f"\n  COMMODITY:")
        print(f"    Signals: {commodity_result['total_signals']} | "
              f"PASS: {commodity_result['dir_pass']} | FLIP: {commodity_result['dir_flip']} | REJECT: {commodity_result['dir_reject']}")
        print(f"    v10.2: {commodity_result['trades']} trades, Rs {commodity_result['pnl']:,.0f} PnL, "
              f"{commodity_result['winners']}W/{commodity_result['losers']}L")
        print(f"    Old:   {commodity_result['old_trades']} trades, Rs {commodity_result['old_pnl']:,.0f} PnL")
        total_v10_pnl += commodity_result['pnl']
        total_old_pnl += commodity_result['old_pnl']

    print(f"\n  COMBINED:")
    print(f"    v10.2 Total PnL: Rs {total_v10_pnl:,.0f}")
    print(f"    Old Code PnL:    Rs {total_old_pnl:,.0f}")
    print(f"    Improvement:     Rs {total_v10_pnl - total_old_pnl:,.0f}")
    print(f"    {'BETTER' if total_v10_pnl > total_old_pnl else 'WORSE'} by Rs {abs(total_v10_pnl - total_old_pnl):,.0f}")
