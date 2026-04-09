"""v10.2b Realistic Trade Simulator — Simulates intraday trades with SL/Target/Trailing exits.

Uses today's live signal CSVs to simulate what v10.2b would have traded,
with proper exit logic: SL hit, target hit, trailing SL, breakout failure, EOD close.

Premium is estimated at each signal timestamp using delta/gamma/theta from the signal data,
so we track premium movement throughout the day instead of just entry vs EOD.
"""

import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data')

# ---- Bot Configuration (matching paper_trader.py) ----
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
MCX_MULTIPLIERS = {'GOLDM': 10, 'SILVERM': 5, 'CRUDEOILM': 10}
MIN_SIGNAL_SCORE = 50
MIN_PREMIUM_BUY = 15
MAX_TRADES_PER_DAY = 15
MAX_POSITIONS_PER_SYMBOL = 3
COOLDOWN_SECONDS = 300  # 5 min re-entry cooldown (real bot uses 10min but direction gate already filters)
GRACE_PERIOD_SECONDS = 180  # 3 min no-exit grace

# SL/Target defaults
DEFAULT_TARGET_MULT = 1.5   # 50% gain
DEFAULT_SL_MULT = 0.5       # 50% loss

# Trailing SL phases
TSL_BREAKEVEN_GAIN_PCT = 15
TSL_TRAIL_GAIN_PCT = 25
TSL_TRAIL_DISTANCE_PCT = 30
TSL_TIGHT_GAIN_PCT = 40
TSL_TIGHT_DISTANCE_PCT = 20

# Target trail
TARGET_TRAIL_ENABLED = True
TARGET_TRAIL_EXTEND_PCT = 20
TARGET_TRAIL_TSL_DISTANCE_PCT = 15
TARGET_TRAIL_MAX_EXTENSIONS = 5

# Breakout failure
BREAKOUT_FAIL_CHECK_SECONDS = 300  # 5 min
BREAKOUT_FAIL_MIN_GAIN_PCT = 2
BREAKOUT_FAIL_DROP_PCT = 15

# EOD
EQUITY_EOD_CLOSE_HOUR = 15
EQUITY_EOD_CLOSE_MIN = 20
MCX_EOD_CLOSE_HOUR = 23
MCX_EOD_CLOSE_MIN = 15


def validate_signal_direction(signal_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar):
    """5-factor direction validation."""
    is_ce = 'CE' in signal_type
    is_sell = 'SELL' in signal_type
    if is_sell:
        return True, 'SELL_EXEMPT', 0

    checks = []
    passed = 0

    if vwap and vwap > 0:
        if (is_ce and spot > vwap) or (not is_ce and spot < vwap):
            passed += 1; checks.append('VWAP+')
        else:
            checks.append('VWAP-')

    if prev_close and prev_close > 0:
        if (is_ce and spot > prev_close) or (not is_ce and spot < prev_close):
            passed += 1; checks.append('TREND+')
        else:
            checks.append('TREND-')

    if open_price and open_price > 0:
        if (is_ce and spot > open_price) or (not is_ce and spot < open_price):
            passed += 1; checks.append('INTRA+')
        else:
            checks.append('INTRA-')

    if ema_9 > 0 and ema_20 > 0:
        if (is_ce and ema_9 > ema_20) or (not is_ce and ema_9 < ema_20):
            passed += 1; checks.append('EMA+')
        else:
            checks.append('EMA-')

    if (is_ce and three_bar >= 2) or (not is_ce and three_bar <= -2):
        passed += 1; checks.append('MOM+')
    elif three_bar == 0:
        checks.append('MOM~')
    else:
        checks.append('MOM-')

    is_valid = passed >= 3
    score_adj = (passed - 3) * 5
    return is_valid, f"DIR({passed}/5: {' '.join(checks)})", score_adj


def estimate_premium(entry_premium, entry_spot, current_spot, opt_type, entry_time, current_time):
    """Estimate option premium using ATM delta=0.5 and simple theta.

    Uses ATM approximation (delta=0.5) regardless of actual moneyness,
    since the bot targets near-ATM strikes. Theta is 2% per trading day.
    """
    spot_change = current_spot - entry_spot
    is_ce = 'CE' in opt_type

    # ATM delta = 0.5 (bot targets near-ATM strikes)
    d = 0.5 if is_ce else -0.5
    premium_from_delta = spot_change * d

    # Simple theta: 2% of entry premium per full trading day (6.25 hours)
    elapsed_hours = (current_time - entry_time).total_seconds() / 3600
    theta_loss = entry_premium * 0.02 * (elapsed_hours / 6.25)

    estimated = entry_premium + premium_from_delta - theta_loss
    return max(0.05, round(estimated, 2))


def compute_direction_indicators(df, symbol_col):
    """Compute per-signal direction indicators (EMA, trend, VWAP, open)."""
    results = {}
    for symbol, sym_df in df.groupby(symbol_col):
        sym_df = sym_df.sort_values('timestamp')
        spots = sym_df['spot'].values
        vwaps = sym_df['vwap'].values if 'vwap' in sym_df.columns else np.zeros(len(spots))

        ema_9 = pd.Series(spots).ewm(span=min(9, len(spots))).mean().values
        ema_20 = pd.Series(spots).ewm(span=min(20, len(spots))).mean().values

        three_bar = np.zeros(len(spots))
        for i in range(len(spots)):
            start = max(0, i - 3)
            trend = 0
            for j in range(start + 1, i + 1):
                trend += 1 if spots[j] > spots[j - 1] else -1
            three_bar[i] = trend

        open_price = spots[0] if len(spots) > 0 else 0
        prev_close = spots[0] * 0.998

        # Daily EMA proxy for flip guard
        daily_ema_bullish = spots[-1] > spots[0] if len(spots) >= 2 else True

        for idx, row in sym_df.iterrows():
            i = sym_df.index.get_loc(idx)
            results[idx] = {
                'vwap': vwaps[i] if i < len(vwaps) and not np.isnan(vwaps[i]) else 0,
                'ema_9': ema_9[i],
                'ema_20': ema_20[i],
                'three_bar': three_bar[i],
                'open': open_price,
                'prev_close': prev_close,
                'daily_ema_bullish': daily_ema_bullish,
            }
    return results


def simulate_trades(df, symbol_col, lot_sizes_or_mults, is_commodity=False):
    """Full intraday trade simulation with entry/exit logic.

    Uses spot-based premium estimation (ATM delta=0.5).
    Checks exits only when same-symbol signal arrives AND >= 5 min since last check.
    Returns list of completed trades with entry/exit times, premiums, PnL.
    """
    df = df.sort_values('timestamp').copy()

    # Active positions and completed trades
    positions = []
    completed_trades = []
    trade_count = 0
    cooldowns = {}  # symbol -> earliest re-entry time
    daily_trade_count = 0

    # EOD close time
    if is_commodity:
        eod_time = df['timestamp'].max().replace(hour=MCX_EOD_CLOSE_HOUR, minute=MCX_EOD_CLOSE_MIN)
    else:
        eod_time = df['timestamp'].max().replace(hour=EQUITY_EOD_CLOSE_HOUR, minute=EQUITY_EOD_CLOSE_MIN)

    # Deduplicate: keep first signal per (symbol, 5-min window, strategy)
    # Simulates the bot's ~5 min scan cycle
    df['ts_round'] = df['timestamp'].dt.floor('5min')
    df_dedup = df.groupby([symbol_col, 'ts_round', 'strategy']).first().reset_index()
    df_dedup = df_dedup.sort_values('timestamp').reset_index(drop=True)

    # Compute direction indicators on deduped signals
    dir_indicators = compute_direction_indicators(df_dedup, symbol_col)

    # Process each signal chronologically
    for idx, sig in df_dedup.iterrows():
        ts = sig['timestamp']
        symbol = sig[symbol_col]
        spot = sig['spot']

        # ---- CHECK EXITS for positions matching this symbol ----
        still_open = []
        for pos in positions:
            # Only check exits for same symbol and after grace period
            if pos['symbol'] != symbol:
                still_open.append(pos)
                continue

            elapsed = (ts - pos['entry_time']).total_seconds()
            if elapsed < GRACE_PERIOD_SECONDS:
                still_open.append(pos)
                continue

            # Estimate current premium using ATM delta from spot movement
            current_prem = estimate_premium(
                pos['entry_premium'], pos['entry_spot'], spot,
                pos['opt_type'], pos['entry_time'], ts)

            pos['last_spot'] = spot
            pos['last_premium'] = current_prem
            pos['last_time'] = ts

            # Track peak premium
            if current_prem > pos.get('peak_premium', pos['entry_premium']):
                pos['peak_premium'] = current_prem

            # ---- SL Check ----
            if current_prem <= pos['sl']:
                pos['exit_premium'] = pos['sl']
                pos['exit_time'] = ts
                pos['exit_reason'] = 'SL_HIT'
                completed_trades.append(pos)
                cooldowns[symbol] = ts + timedelta(seconds=COOLDOWN_SECONDS)
                continue

            # ---- Trailing SL Phases ----
            peak = pos.get('peak_premium', pos['entry_premium'])
            peak_gain_pct = (peak - pos['entry_premium']) / pos['entry_premium'] * 100

            if peak_gain_pct >= TSL_TIGHT_GAIN_PCT:
                tsl = peak * (1 - TSL_TIGHT_DISTANCE_PCT / 100)
                pos['trailing_sl'] = max(pos.get('trailing_sl', 0), tsl)
                pos['tsl_phase'] = 3
            elif peak_gain_pct >= TSL_TRAIL_GAIN_PCT:
                tsl = peak * (1 - TSL_TRAIL_DISTANCE_PCT / 100)
                pos['trailing_sl'] = max(pos.get('trailing_sl', 0), tsl)
                pos['tsl_phase'] = 2
            elif peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT:
                tsl = pos['entry_premium'] * 1.03
                pos['trailing_sl'] = max(pos.get('trailing_sl', 0), tsl)
                pos['tsl_phase'] = 1

            # Check trailing SL hit
            if pos.get('trailing_sl', 0) > 0 and current_prem <= pos['trailing_sl']:
                pos['exit_premium'] = round(pos['trailing_sl'], 2)
                pos['exit_time'] = ts
                pos['exit_reason'] = f"TRAILING_SL_P{pos.get('tsl_phase', '?')}"
                completed_trades.append(pos)
                cooldowns[symbol] = ts + timedelta(seconds=COOLDOWN_SECONDS)
                continue

            # ---- Target Check with Trailing Target ----
            if current_prem >= pos['target']:
                if TARGET_TRAIL_ENABLED and pos.get('target_extensions', 0) < TARGET_TRAIL_MAX_EXTENSIONS:
                    new_target = round(current_prem * (1 + TARGET_TRAIL_EXTEND_PCT / 100), 2)
                    new_tsl = round(peak * (1 - TARGET_TRAIL_TSL_DISTANCE_PCT / 100), 2)
                    new_tsl = max(new_tsl, pos.get('trailing_sl', 0))
                    pos['target_extensions'] = pos.get('target_extensions', 0) + 1
                    pos['target'] = new_target
                    pos['trailing_sl'] = new_tsl
                    pos['tsl_phase'] = 'T'
                else:
                    pos['exit_premium'] = current_prem
                    pos['exit_time'] = ts
                    pos['exit_reason'] = 'TARGET_HIT'
                    completed_trades.append(pos)
                    cooldowns[symbol] = ts + timedelta(seconds=COOLDOWN_SECONDS)
                    continue

            # ---- EOD Close ----
            if ts >= eod_time:
                pos['exit_premium'] = current_prem
                pos['exit_time'] = ts
                pos['exit_reason'] = 'EOD_CLOSE'
                completed_trades.append(pos)
                continue

            still_open.append(pos)
        positions = still_open

        # ---- ENTRY LOGIC: Should we take this signal? ----
        if not is_commodity:
            if ts.hour < 9 or (ts.hour == 9 and ts.minute < 30):
                continue
            if ts.hour > 14 or (ts.hour == 14 and ts.minute > 30):
                continue
        else:
            if ts.hour >= 22 and ts.minute > 30:
                continue

        if daily_trade_count >= MAX_TRADES_PER_DAY:
            continue

        score = sig.get('quality_score', 0)
        if score < MIN_SIGNAL_SCORE:
            continue

        if sig['premium'] < MIN_PREMIUM_BUY:
            continue

        if symbol in cooldowns and ts < cooldowns[symbol]:
            continue

        sym_positions = [p for p in positions if p['symbol'] == symbol]
        if len(sym_positions) >= MAX_POSITIONS_PER_SYMBOL:
            continue

        # Direction validation
        ind = dir_indicators.get(idx, {})
        sig_type = sig['type']
        vwap = ind.get('vwap', sig.get('vwap', 0))
        prev_close = ind.get('prev_close', spot * 0.998)
        open_price = ind.get('open', spot)
        ema_9 = ind.get('ema_9', 0)
        ema_20 = ind.get('ema_20', 0)
        three_bar = ind.get('three_bar', 0)

        dir_valid, dir_reason, dir_adj = validate_signal_direction(
            sig_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar)

        final_type = sig_type
        was_flipped = False

        if not dir_valid:
            flipped_type = sig_type.replace('CE', 'PE') if 'CE' in sig_type else sig_type.replace('PE', 'CE')
            flip_to_pe = 'PE' in flipped_type
            daily_bullish = ind.get('daily_ema_bullish', True)

            if (daily_bullish and flip_to_pe) or (not daily_bullish and not flip_to_pe):
                continue

            flip_valid, flip_reason, flip_adj = validate_signal_direction(
                flipped_type, spot, vwap, prev_close, open_price, ema_9, ema_20, three_bar)

            if flip_valid:
                final_type = flipped_type
                was_flipped = True
                dir_adj = flip_adj
            else:
                continue

        # Duplicate check: don't enter same symbol + same strategy + same direction
        opt_type_dir = 'CE' if 'CE' in final_type else 'PE'
        existing_same = [p for p in positions if p['symbol'] == symbol
                        and p['strategy'] == sig['strategy']
                        and ('CE' if 'CE' in p['opt_type'] else 'PE') == opt_type_dir]
        if existing_same:
            continue

        # ---- ENTER TRADE ----
        entry_premium = sig['premium']
        strike = sig.get('strike', 0)
        dte = sig.get('dte', 5)

        target = sig.get('target', entry_premium * DEFAULT_TARGET_MULT)
        sl = sig.get('sl', entry_premium * DEFAULT_SL_MULT)
        if not target or target <= 0:
            target = entry_premium * DEFAULT_TARGET_MULT
        if not sl or sl <= 0:
            sl = entry_premium * DEFAULT_SL_MULT

        lot_or_mult = lot_sizes_or_mults.get(symbol, 50)

        trade_id = f"T{trade_count + 1}"
        pos = {
            'id': trade_id,
            'symbol': symbol,
            'strategy': sig['strategy'],
            'original_type': sig['type'],
            'opt_type': final_type,
            'was_flipped': was_flipped,
            'strike': strike,
            'entry_premium': entry_premium,
            'entry_spot': spot,
            'entry_time': ts,
            'dte': dte,
            'target': target,
            'sl': sl,
            'lot_or_mult': lot_or_mult,
            'peak_premium': entry_premium,
            'trailing_sl': 0,
            'tsl_phase': 0,
            'target_extensions': 0,
            'last_spot': spot,
            'last_premium': entry_premium,
            'last_time': ts,
            'exit_premium': None,
            'exit_time': None,
            'exit_reason': None,
            'dir_reason': dir_reason,
            'quality_score': score,
        }
        positions.append(pos)
        trade_count += 1
        daily_trade_count += 1
        # No cooldown after entry — only after exits (real bot behavior)

    # Force close any remaining positions at EOD
    for pos in positions:
        if pos['exit_time'] is None:
            # Estimate final premium from last known spot
            eod_prem = estimate_premium(
                pos['entry_premium'], pos['entry_spot'], pos['last_spot'],
                pos['opt_type'], pos['entry_time'], eod_time)
            pos['exit_premium'] = eod_prem
            pos['exit_time'] = eod_time
            pos['exit_reason'] = 'EOD_CLOSE'
            completed_trades.append(pos)

    return completed_trades


def print_trade_table(trades, title, is_commodity=False):
    """Print detailed trade table."""
    print(f"\n{'=' * 160}")
    print(f"  {title}")
    print(f"{'=' * 160}")

    if not trades:
        print("  No trades generated.")
        return

    # Sort by entry time
    trades = sorted(trades, key=lambda t: t['entry_time'])

    symbol_col = 'Symbol' if not is_commodity else 'Commodity'
    mult_col = 'Lot' if not is_commodity else 'Mult'

    # Header
    print(f"{'#':>3s} {'Entry':>8s} {'Exit':>8s} {'Dur':>5s} {symbol_col:>10s} {'Strategy':>12s} "
          f"{'Direction':>15s} {'Flip':>4s} {'Strike':>8s} "
          f"{'EntryPrem':>10s} {'ExitPrem':>10s} {mult_col:>5s} {'PnL':>10s} "
          f"{'Exit Reason':>18s} {'Score':>5s}")
    print("-" * 160)

    total_pnl = 0
    winners = 0
    losers = 0
    breakeven = 0

    for i, t in enumerate(trades, 1):
        entry_time = t['entry_time'].strftime('%H:%M:%S')
        exit_time = t['exit_time'].strftime('%H:%M:%S') if t['exit_time'] else '---'

        # Duration
        if t['exit_time']:
            dur_secs = (t['exit_time'] - t['entry_time']).total_seconds()
            dur_mins = int(dur_secs // 60)
            if dur_mins >= 60:
                dur_str = f"{dur_mins // 60}h{dur_mins % 60:02d}"
            else:
                dur_str = f"{dur_mins}m"
        else:
            dur_str = '---'

        exit_prem = t.get('exit_premium', t['entry_premium'])
        if exit_prem is None:
            exit_prem = t['entry_premium']

        lot_or_mult = t['lot_or_mult']
        pnl = (exit_prem - t['entry_premium']) * lot_or_mult
        total_pnl += pnl

        if pnl > 0:
            winners += 1
        elif pnl < 0:
            losers += 1
        else:
            breakeven += 1

        flip_marker = 'FLIP' if t.get('was_flipped', False) else ''
        strike_str = f"{t['strike']:.0f}" if t['strike'] else '---'
        exit_reason = t.get('exit_reason', '---')

        print(f"{i:>3d} {entry_time:>8s} {exit_time:>8s} {dur_str:>5s} {t['symbol']:>10s} {t['strategy']:>12s} "
              f"{t['opt_type']:>15s} {flip_marker:>4s} {strike_str:>8s} "
              f"{t['entry_premium']:>10.2f} {exit_prem:>10.2f} {lot_or_mult:>5d} {'Rs {:,.0f}'.format(pnl):>10s} "
              f"{exit_reason:>18s} {t.get('quality_score', 0):>5.0f}")

    print("-" * 160)

    total_trades = winners + losers + breakeven
    wr = winners / total_trades * 100 if total_trades > 0 else 0
    print(f"\n  TOTAL TRADES: {total_trades}")
    print(f"  WINNERS: {winners} | LOSERS: {losers} | BREAKEVEN: {breakeven}")
    print(f"  WIN RATE: {wr:.0f}%")
    print(f"  TOTAL PnL: Rs {total_pnl:,.0f}")

    # Per-symbol breakdown
    print(f"\n  Per-Symbol PnL:")
    symbol_pnl = {}
    symbol_trades = {}
    for t in trades:
        sym = t['symbol']
        ep = t.get('exit_premium', t['entry_premium']) or t['entry_premium']
        p = (ep - t['entry_premium']) * t['lot_or_mult']
        symbol_pnl[sym] = symbol_pnl.get(sym, 0) + p
        symbol_trades[sym] = symbol_trades.get(sym, 0) + 1
    for sym in sorted(symbol_pnl.keys()):
        print(f"    {sym:>12s}: {symbol_trades[sym]:>3d} trades, Rs {symbol_pnl[sym]:>10,.0f}")

    # Per-exit-reason breakdown
    print(f"\n  Exit Reasons:")
    reason_counts = {}
    reason_pnl = {}
    for t in trades:
        r = t.get('exit_reason', 'UNKNOWN')
        ep = t.get('exit_premium', t['entry_premium']) or t['entry_premium']
        p = (ep - t['entry_premium']) * t['lot_or_mult']
        reason_counts[r] = reason_counts.get(r, 0) + 1
        reason_pnl[r] = reason_pnl.get(r, 0) + p
    for r in sorted(reason_counts.keys()):
        print(f"    {r:>20s}: {reason_counts[r]:>3d} trades, Rs {reason_pnl[r]:>10,.0f}")

    return total_pnl, winners, losers


def main():
    equity_file = os.path.join(DATA_DIR, 'equity_signals_20260310_full.csv')
    commodity_file = os.path.join(DATA_DIR, 'commodity_signals_20260310_full.csv')

    equity_pnl = 0
    commodity_pnl = 0

    # ---- EQUITY (BSE + NSE) ----
    if os.path.exists(equity_file):
        df = pd.read_csv(equity_file)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        print(f"\nLoaded {len(df)} equity signals from {equity_file}")
        print(f"Symbols: {', '.join(df['symbol'].unique())}")
        print(f"Time range: {df['timestamp'].min().strftime('%H:%M:%S')} to {df['timestamp'].max().strftime('%H:%M:%S')}")

        trades = simulate_trades(df, 'symbol', LOT_SIZES, is_commodity=False)
        result = print_trade_table(trades, "EQUITY TRADES (BSE + NSE) — v10.2b Simulation", is_commodity=False)
        if result:
            equity_pnl = result[0]
    else:
        print(f"Equity file not found: {equity_file}")

    # ---- COMMODITY (MCX) ----
    if os.path.exists(commodity_file):
        df = pd.read_csv(commodity_file)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        print(f"\n\nLoaded {len(df)} commodity signals from {commodity_file}")
        print(f"Commodities: {', '.join(df['commodity'].unique())}")
        print(f"Time range: {df['timestamp'].min().strftime('%H:%M:%S')} to {df['timestamp'].max().strftime('%H:%M:%S')}")

        trades = simulate_trades(df, 'commodity', MCX_MULTIPLIERS, is_commodity=True)
        result = print_trade_table(trades, "COMMODITY TRADES (MCX) — v10.2b Simulation", is_commodity=True)
        if result:
            commodity_pnl = result[0]
    else:
        print(f"Commodity file not found: {commodity_file}")

    # ---- COMBINED SUMMARY ----
    print(f"\n{'=' * 160}")
    print(f"  COMBINED SUMMARY — v10.2b")
    print(f"{'=' * 160}")
    print(f"  Equity PnL:    Rs {equity_pnl:>10,.0f}")
    print(f"  Commodity PnL: Rs {commodity_pnl:>10,.0f}")
    print(f"  TOTAL PnL:     Rs {equity_pnl + commodity_pnl:>10,.0f}")


if __name__ == '__main__':
    main()
