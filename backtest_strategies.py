#!/usr/bin/env python3
"""
Comprehensive Strategy Backtest — v2.5.3
Uses daily OHLC data to backtest all trading strategies.
Tests with Rs 3L capital constraint.

Strategies tested:
  1. CPR (Narrow Breakout + Wide Mean Reversion)
  2. Gamma Blast (Coiled Day Breakout)
  3. Ghost Zone (Demand/Supply Zone)
  4. PCR+VWAP (simulated PCR from price momentum)
  5. Survivor (Option Selling)

Author: Backtest Engine
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

# ====================================================================
# CONFIGURATION
# ====================================================================
CAPITAL = 300000  # Rs 3L
MAX_RISK_PCT = 25
MAX_PER_TRADE = CAPITAL * MAX_RISK_PCT / 100  # Rs 75,000
MAX_DAILY_LOSS = CAPITAL * 0.10  # Rs 30,000

LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}
MARGIN_PER_LOT = {'NIFTY': 100000, 'BANKNIFTY': 95000, 'SENSEX': 70000}

# Transaction costs per lot (both sides)
BROKERAGE_PER_LOT = 40  # flat brokerage
STT_PCT = 0.000625  # on sell side
EXCHANGE_PCT = 0.00053
GST_PCT = 0.18

# Black-Scholes helpers
from scipy.stats import norm
import math

def bs_price(spot, strike, T, r, sigma, opt_type='CE'):
    """Black-Scholes option price."""
    if T <= 0:
        T = 1/365
    d1 = (math.log(spot/strike) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if opt_type == 'CE':
        return spot * norm.cdf(d1) - strike * math.exp(-r*T) * norm.cdf(d2)
    else:
        return strike * math.exp(-r*T) * norm.cdf(-d2) - spot * norm.cdf(-d1)

def bs_delta(spot, strike, T, r, sigma, opt_type='CE'):
    if T <= 0:
        T = 1/365
    d1 = (math.log(spot/strike) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    if opt_type == 'CE':
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1

def estimate_premium_change(entry_premium, delta, spot_change, gamma=0):
    """Estimate option premium change given spot movement."""
    return delta * spot_change + 0.5 * gamma * spot_change**2

def round_strike(price, interval):
    return round(price / interval) * interval

def compute_total_cost(premium, lot_size, num_lots):
    """Compute total transaction cost for a round trip."""
    turnover = premium * lot_size * num_lots
    brokerage = BROKERAGE_PER_LOT * num_lots * 2  # both sides
    stt = turnover * STT_PCT
    exchange_charges = turnover * EXCHANGE_PCT * 2
    gst = brokerage * GST_PCT
    return brokerage + stt + exchange_charges + gst

# ====================================================================
# DATA LOADING
# ====================================================================
def load_data(symbol, data_dir='data/options'):
    """Load historical OHLC data."""
    fname = f"{symbol}_spot_one_day_2000d.csv"
    fpath = f"{data_dir}/{fname}"
    try:
        df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"Error loading {fpath}: {e}")
        return None

# ====================================================================
# INDICATOR COMPUTATION
# ====================================================================
def compute_indicators(df, idx):
    """Compute all indicators for a given day index."""
    if idx < 20:
        return None

    today = df.iloc[idx]
    yesterday = df.iloc[idx-1]

    # CPR from yesterday
    h, l, c = yesterday['high'], yesterday['low'], yesterday['close']
    pivot = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * pivot - bc
    cpr_width = abs(tc - bc) / c * 100

    # Camarilla from yesterday
    rng = h - l
    r3 = c + rng * 1.1 / 4
    r4 = c + rng * 1.1 / 2
    s3 = c - rng * 1.1 / 4
    s4 = c - rng * 1.1 / 2

    # ATR (14-day)
    atr_data = df.iloc[max(0,idx-14):idx]
    true_ranges = []
    for i in range(1, len(atr_data)):
        tr = max(
            atr_data.iloc[i]['high'] - atr_data.iloc[i]['low'],
            abs(atr_data.iloc[i]['high'] - atr_data.iloc[i-1]['close']),
            abs(atr_data.iloc[i]['low'] - atr_data.iloc[i-1]['close'])
        )
        true_ranges.append(tr)
    atr = np.mean(true_ranges) if true_ranges else rng

    # Historical volatility (20-day) as IV proxy
    returns = df.iloc[max(0,idx-20):idx]['close'].pct_change().dropna()
    hist_vol = returns.std() * math.sqrt(252) if len(returns) > 5 else 0.20
    hist_vol = max(0.10, min(0.60, hist_vol))  # clamp

    # Previous day range ratio (for Gamma Blast)
    prev_range_ratio = rng / atr if atr > 0 else 1.0

    # VWAP proxy (moving average of close * volume, simplified as yesterday's typical price)
    vwap = (h + l + c) / 3

    # Simulated PCR from 5-day price momentum
    if idx >= 5:
        closes = df.iloc[idx-5:idx]['close'].values
        chg = (closes[-1] - closes[0]) / closes[0]
        pcr_proxy = 1.0 + chg * 5  # amplified momentum
    else:
        pcr_proxy = 1.0

    return {
        'open': today['open'], 'high': today['high'],
        'low': today['low'], 'close': today['close'],
        'pivot': pivot, 'tc': tc, 'bc': bc,
        'cpr_width': cpr_width,
        'r3': r3, 'r4': r4, 's3': s3, 's4': s4,
        'atr': atr, 'iv': hist_vol,
        'prev_range_ratio': prev_range_ratio,
        'prev_close': c, 'prev_high': h, 'prev_low': l,
        'vwap': vwap, 'pcr': pcr_proxy,
        'date': df.index[idx],
        'dow': df.index[idx].weekday(),
    }

# ====================================================================
# STRATEGY SIGNAL GENERATORS
# ====================================================================

def check_cpr_signals(symbol, ind):
    """CPR Breakout/Mean Reversion strategy."""
    signals = []
    spot = ind['open']  # Entry at open
    cpr_width = ind['cpr_width']
    tc, bc = ind['tc'], ind['bc']
    r3, r4, s3, s4 = ind['r3'], ind['r4'], ind['s3'], ind['s4']
    atr = ind['atr']
    iv = ind['iv']
    interval = STRIKE_INTERVALS[symbol]

    # Narrow CPR — Breakout
    if cpr_width < 0.3:
        T = 3 / 365  # Assume ~3 DTE

        if spot > tc:
            strike = round_strike(spot, interval)
            premium = bs_price(spot, strike, T, 0.07, iv * 1.1, 'CE')
            if premium >= 15:
                # Check if Camarilla R3 is within reach
                target_mult = 2.5 if ind['high'] > r3 else 1.8
                signals.append({
                    'strategy': 'CPR', 'type': 'BUY_CE',
                    'symbol': symbol, 'strike': strike,
                    'premium': premium, 'delta': 0.50,
                    'target': premium * target_mult,
                    'sl': premium * 0.4,
                    'reason': f'Narrow CPR ({cpr_width:.3f}%) bullish breakout',
                })

        if spot < bc:
            strike = round_strike(spot, interval)
            premium = bs_price(spot, strike, T, 0.07, iv * 1.1, 'PE')
            if premium >= 15:
                target_mult = 2.5 if ind['low'] < s3 else 1.8
                signals.append({
                    'strategy': 'CPR', 'type': 'BUY_PE',
                    'symbol': symbol, 'strike': strike,
                    'premium': premium, 'delta': -0.50,
                    'target': premium * target_mult,
                    'sl': premium * 0.4,
                    'reason': f'Narrow CPR ({cpr_width:.3f}%) bearish breakout',
                })

    # Wide CPR — Mean Reversion (SELL)
    if cpr_width > 0.5:
        T = 3 / 365

        if ind['high'] >= r3 * 0.998 and spot < r4:
            strike = round_strike(r4, interval)
            premium = bs_price(spot, strike, T, 0.07, iv, 'CE')
            if premium >= 20:
                signals.append({
                    'strategy': 'CPR_SELL', 'type': 'SELL_CE',
                    'symbol': symbol, 'strike': strike,
                    'premium': premium, 'delta': 0.30,
                    'target': premium * 0.3,
                    'sl': premium * 1.2,
                    'is_sell': True,
                    'reason': f'Wide CPR ({cpr_width:.3f}%) mean reversion sell CE',
                })

        if ind['low'] <= s3 * 1.002 and spot > s4:
            strike = round_strike(s4, interval)
            premium = bs_price(spot, strike, T, 0.07, iv, 'PE')
            if premium >= 20:
                signals.append({
                    'strategy': 'CPR_SELL', 'type': 'SELL_PE',
                    'symbol': symbol, 'strike': strike,
                    'premium': premium, 'delta': -0.30,
                    'target': premium * 0.3,
                    'sl': premium * 1.2,
                    'is_sell': True,
                    'reason': f'Wide CPR ({cpr_width:.3f}%) mean reversion sell PE',
                })

    return signals

def check_gamma_blast_signals(symbol, ind):
    """Gamma Blast — coiled day breakout."""
    signals = []
    spot = ind['open']
    atr = ind['atr']
    iv = ind['iv']
    interval = STRIKE_INTERVALS[symbol]

    # Pre-condition: previous day was coiled (range < 1.5 ATR)
    if ind['prev_range_ratio'] > 1.5:
        return signals

    # Body must show directional conviction
    body = ind['close'] - ind['open']
    if abs(body) < atr * 0.15:
        return signals

    # Day range must have started moving
    day_range = (ind['high'] - ind['low']) / atr if atr > 0 else 0
    if day_range < 0.3:
        return signals

    # DTE estimation
    dow = ind['dow']
    if symbol == 'BANKNIFTY':
        dte = max(1, (2 - dow) % 7)  # Wed expiry
    else:
        dte = max(1, (3 - dow) % 7)  # Thu expiry

    T = dte / 365
    target_mult = max(2.0, 2.5 - (dte - 1) * 0.10)
    sl_mult = min(0.4, 0.3 + (dte - 1) * 0.02)

    if body > 0:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv * 1.1, 'CE')
        if premium >= 15:
            signals.append({
                'strategy': 'Gamma Blast', 'type': 'BUY_CE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': 0.50,
                'target': premium * target_mult,
                'sl': premium * sl_mult,
                'reason': f'Gamma Blast: Up breakout body={body:.0f} ATR={atr:.0f}',
            })
    else:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv * 1.1, 'PE')
        if premium >= 15:
            signals.append({
                'strategy': 'Gamma Blast', 'type': 'BUY_PE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': -0.50,
                'target': premium * target_mult,
                'sl': premium * sl_mult,
                'reason': f'Gamma Blast: Down breakout body={body:.0f} ATR={atr:.0f}',
            })

    return signals

def check_ghost_zone_signals(symbol, ind):
    """Ghost Zone — Demand/Supply zone bounces."""
    signals = []
    spot = ind['open']
    atr = ind['atr']
    iv = ind['iv']
    interval = STRIKE_INTERVALS[symbol]
    T = 3 / 365

    # Simplified demand/supply zone detection using recent price levels
    prev_h, prev_l = ind['prev_high'], ind['prev_low']

    # Demand zone: spot near previous low (bouncing off support)
    demand_zone = prev_l - atr * 0.1
    supply_zone = prev_h + atr * 0.1

    if abs(spot - demand_zone) < atr * 0.3 and spot > demand_zone:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv, 'CE')
        if premium >= 15:
            signals.append({
                'strategy': 'Ghost Zone', 'type': 'BUY_CE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': 0.50,
                'target': premium * 1.5,
                'sl': premium * 0.5,
                'reason': 'Ghost Zone demand bounce',
            })

    if abs(spot - supply_zone) < atr * 0.3 and spot < supply_zone:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv, 'PE')
        if premium >= 15:
            signals.append({
                'strategy': 'Ghost Zone', 'type': 'BUY_PE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': -0.50,
                'target': premium * 1.5,
                'sl': premium * 0.5,
                'reason': 'Ghost Zone supply bounce',
            })

    return signals

def check_pcr_vwap_signals(symbol, ind):
    """PCR+VWAP strategy."""
    signals = []
    spot = ind['open']
    atr = ind['atr']
    iv = ind['iv']
    pcr = ind['pcr']
    vwap = ind['vwap']
    interval = STRIKE_INTERVALS[symbol]

    if iv > 0.40:
        return signals

    tolerance = max(atr * 0.1, spot * 0.003)

    T = 3 / 365

    if pcr > 1.05 and abs(spot - vwap) < tolerance * 2 and spot >= vwap * 0.995:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv, 'CE')
        if premium >= 15 and premium < spot * 0.05:
            signals.append({
                'strategy': 'PCR+VWAP', 'type': 'BUY_CE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': 0.50,
                'target': premium * 2.5,
                'sl': premium * 0.4,
                'reason': f'PCR+VWAP: Bullish PCR={pcr:.2f} near VWAP',
            })

    if pcr < 0.95 and abs(spot - vwap) < tolerance * 2 and spot <= vwap * 1.005:
        strike = round_strike(spot, interval)
        premium = bs_price(spot, strike, T, 0.07, iv, 'PE')
        if premium >= 15 and premium < spot * 0.05:
            signals.append({
                'strategy': 'PCR+VWAP', 'type': 'BUY_PE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': -0.50,
                'target': premium * 2.5,
                'sl': premium * 0.4,
                'reason': f'PCR+VWAP: Bearish PCR={pcr:.2f} near VWAP',
            })

    return signals

def check_survivor_signals(symbol, ind):
    """Survivor V2 — Option selling with time decay."""
    signals = []
    spot = ind['open']
    atr = ind['atr']
    iv = ind['iv']
    interval = STRIKE_INTERVALS[symbol]
    dow = ind['dow']

    # Only Mon-Thu
    if dow > 3:
        return signals

    # Day-specific parameters
    day_params = {
        0: {'gap': max(atr * 0.4, 200), 'dist': max(atr * 0.8, 200)},  # Mon
        1: {'gap': max(atr * 0.3, 120), 'dist': max(atr * 0.7, 150)},  # Tue
        2: {'gap': max(atr * 0.2, 70), 'dist': max(atr * 0.5, 100)},   # Wed
        3: {'gap': max(atr * 0.15, 50), 'dist': max(atr * 0.4, 80)},   # Thu
    }
    params = day_params[dow]
    gap = params['gap']
    distance = params['dist']

    # Check for bullish breakout above resistance → SELL PUT (OTM)
    resistance = ind['prev_high']
    support = ind['prev_low']

    if ind['high'] > resistance + gap:
        strike = round_strike(spot - distance, interval)
        T = max(1, (3 - dow) % 7 + 1) / 365
        premium = bs_price(spot, strike, T, 0.07, iv, 'PE')
        if premium >= 20:
            signals.append({
                'strategy': 'Survivor', 'type': 'SELL_PE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': bs_delta(spot, strike, T, 0.07, iv, 'PE'),
                'target': premium * 0.3,
                'sl': premium * 1.8,  # Fixed: SL is premium rising to 1.8x for SELL
                'is_sell': True,
                'reason': f'Survivor: Bullish breakout, sell OTM put',
            })

    if ind['low'] < support - gap:
        strike = round_strike(spot + distance, interval)
        T = max(1, (3 - dow) % 7 + 1) / 365
        premium = bs_price(spot, strike, T, 0.07, iv, 'CE')
        if premium >= 20:
            signals.append({
                'strategy': 'Survivor', 'type': 'SELL_CE',
                'symbol': symbol, 'strike': strike,
                'premium': premium, 'delta': bs_delta(spot, strike, T, 0.07, iv, 'CE'),
                'target': premium * 0.3,
                'sl': premium * 1.8,
                'is_sell': True,
                'reason': f'Survivor: Bearish breakout, sell OTM call',
            })

    return signals


# ====================================================================
# INTRADAY P&L SIMULATION
# ====================================================================
def simulate_intraday_pnl(signal, ind):
    """
    Simulate intraday P&L using daily OHLC.
    Returns (exit_price, exit_reason, pnl_per_lot)
    """
    entry = signal['premium']
    target = signal['target']
    sl = signal['sl']
    delta = abs(signal['delta'])
    is_sell = signal.get('is_sell', False)
    is_ce = 'CE' in signal['type']

    spot_open = ind['open']
    spot_high = ind['high']
    spot_low = ind['low']
    spot_close = ind['close']

    if is_sell:
        # For SELL: profit when premium drops, loss when premium rises
        # Best case: spot stays flat or moves in our direction
        if is_ce:
            # SELL CE: profit if spot drops, loss if spot rises
            # Worst move: spot goes to high (premium increases)
            worst_spot_change = spot_high - spot_open
            best_spot_change = spot_open - spot_low

            worst_premium = entry + delta * worst_spot_change * 0.8  # premium rises
            best_premium = max(0, entry - delta * best_spot_change * 0.7)  # premium falls
        else:
            # SELL PE: profit if spot rises, loss if spot drops
            worst_spot_change = spot_open - spot_low
            best_spot_change = spot_high - spot_open

            worst_premium = entry + delta * worst_spot_change * 0.8
            best_premium = max(0, entry - delta * best_spot_change * 0.7)

        # Time decay benefit (approx 1/DTE of premium per day)
        theta_benefit = entry * 0.05  # ~5% per day for short-dated
        best_premium = max(0, best_premium - theta_benefit)

        # Check SL (premium rises too much)
        if worst_premium >= sl:
            pnl = -(sl - entry)  # Loss
            return sl, 'SL_HIT', pnl

        # Check target (premium drops enough)
        if best_premium <= target:
            pnl = entry - target  # Profit (collected premium - target level)
            return target, 'TARGET_HIT', pnl

        # EOD: close at estimated closing premium
        close_change = spot_close - spot_open
        if is_ce:
            eod_premium = max(0, entry + delta * close_change * 0.5)
        else:
            eod_premium = max(0, entry - delta * close_change * 0.5)
        eod_premium = max(0, eod_premium - theta_benefit)

        pnl = entry - eod_premium
        return eod_premium, 'EOD_CLOSE', pnl

    else:
        # BUY options: profit when premium rises
        if is_ce:
            # BUY CE: profit if spot rises
            max_favorable = spot_high - spot_open
            max_adverse = spot_open - spot_low
        else:
            # BUY PE: profit if spot drops
            max_favorable = spot_open - spot_low
            max_adverse = spot_high - spot_open

        # Estimate best and worst premiums
        best_premium = entry + delta * max_favorable * 0.8  # favorable move
        worst_premium = max(0, entry - delta * max_adverse * 0.9)  # adverse move + theta

        # Time decay penalty
        theta_cost = entry * 0.03  # ~3% per day

        # Check SL first (assume adverse move happens first in worst case)
        if worst_premium <= sl:
            pnl = sl - entry  # Negative (loss)
            return sl, 'SL_HIT', pnl

        # Check target
        if best_premium >= target:
            pnl = target - entry  # Positive (profit)
            return target, 'TARGET_HIT', pnl

        # EOD close
        close_change = spot_close - spot_open
        if is_ce:
            eod_premium = max(0, entry + delta * close_change * 0.6 - theta_cost)
        else:
            eod_premium = max(0, entry - delta * close_change * 0.6 - theta_cost)

        pnl = eod_premium - entry
        return eod_premium, 'EOD_CLOSE', pnl


# ====================================================================
# MAIN BACKTEST ENGINE
# ====================================================================
def run_backtest(symbols, data_dir='data/options', start_date=None, end_date=None,
                 strategies=None, initial_capital=300000):
    """Run comprehensive backtest across all strategies and symbols."""

    all_strategies = ['CPR', 'CPR_SELL', 'Gamma Blast', 'Ghost Zone', 'PCR+VWAP', 'Survivor']
    if strategies:
        active_strategies = strategies
    else:
        active_strategies = all_strategies

    # Load data
    data = {}
    for sym in symbols:
        df = load_data(sym, data_dir)
        if df is not None:
            data[sym] = df
            print(f"Loaded {sym}: {len(df)} days ({df.index[0].date()} to {df.index[-1].date()})")

    if not data:
        print("No data loaded!")
        return

    # Results tracking
    all_trades = []
    capital = initial_capital
    daily_pnl = defaultdict(float)

    # Find common date range
    common_start = max(df.index[20] for df in data.values())
    common_end = min(df.index[-1] for df in data.values())

    if start_date:
        common_start = max(common_start, pd.Timestamp(start_date, tz=common_start.tz))
    if end_date:
        common_end = min(common_end, pd.Timestamp(end_date, tz=common_end.tz))

    print(f"\nBacktest period: {common_start.date()} to {common_end.date()}")
    print(f"Initial capital: Rs {initial_capital:,.0f}")
    print(f"Active strategies: {', '.join(active_strategies)}")
    print("=" * 80)

    # Day-by-day simulation
    for sym in symbols:
        df = data[sym]
        lot_size = LOT_SIZES[sym]
        margin = MARGIN_PER_LOT.get(sym, 100000)

        for idx in range(20, len(df)):
            date = df.index[idx]
            if date < common_start or date > common_end:
                continue

            date_str = date.strftime('%Y-%m-%d')

            # Skip weekends (shouldn't be in data but just in case)
            if date.weekday() >= 5:
                continue

            # Daily loss circuit breaker
            if daily_pnl[date_str] < -MAX_DAILY_LOSS:
                continue

            ind = compute_indicators(df, idx)
            if not ind:
                continue

            # Generate signals from each strategy
            day_signals = []

            strategy_funcs = {
                'CPR': check_cpr_signals,
                'CPR_SELL': check_cpr_signals,  # CPR handles both buy and sell
                'Gamma Blast': check_gamma_blast_signals,
                'Ghost Zone': check_ghost_zone_signals,
                'PCR+VWAP': check_pcr_vwap_signals,
                'Survivor': check_survivor_signals,
            }

            for strat_name in active_strategies:
                if strat_name == 'CPR_SELL':
                    continue  # Already covered by CPR function
                func = strategy_funcs.get(strat_name)
                if func:
                    sigs = func(sym, ind)
                    for s in sigs:
                        if s['strategy'] in active_strategies or \
                           (s['strategy'] == 'CPR_SELL' and 'CPR_SELL' in active_strategies):
                            day_signals.append(s)

            # Execute signals
            for sig in day_signals:
                is_sell = sig.get('is_sell', False)

                # Capital check
                if is_sell:
                    trade_cost = margin
                    if capital < margin:
                        continue
                else:
                    num_lots = 1  # Default
                    trade_cost = sig['premium'] * lot_size * num_lots
                    if trade_cost > MAX_PER_TRADE:
                        continue
                    if trade_cost > capital * 0.30:
                        continue

                # Simulate intraday
                exit_price, exit_reason, pnl_per_lot = simulate_intraday_pnl(sig, ind)

                if is_sell:
                    num_lots = 1  # Single lot for margin
                    total_pnl = pnl_per_lot * lot_size * num_lots
                else:
                    num_lots = 1
                    total_pnl = pnl_per_lot * lot_size * num_lots

                # Transaction costs
                costs = compute_total_cost(sig['premium'], lot_size, num_lots)
                net_pnl = total_pnl - costs

                capital += net_pnl
                daily_pnl[date_str] += net_pnl

                all_trades.append({
                    'date': date_str,
                    'symbol': sym,
                    'strategy': sig['strategy'],
                    'type': sig['type'],
                    'strike': sig['strike'],
                    'entry_premium': sig['premium'],
                    'exit_premium': exit_price,
                    'target': sig['target'],
                    'sl': sig['sl'],
                    'exit_reason': exit_reason,
                    'pnl': net_pnl,
                    'gross_pnl': total_pnl,
                    'costs': costs,
                    'lots': num_lots,
                    'capital_after': capital,
                    'is_sell': is_sell,
                })

    return all_trades, capital, daily_pnl


# ====================================================================
# ANALYSIS & REPORTING
# ====================================================================
def analyze_results(trades, initial_capital=300000):
    """Generate comprehensive strategy analysis."""
    if not trades:
        print("No trades to analyze!")
        return

    df = pd.DataFrame(trades)

    print("\n" + "=" * 90)
    print("BACKTEST RESULTS SUMMARY")
    print("=" * 90)

    # Overall
    total_pnl = df['pnl'].sum()
    final_capital = initial_capital + total_pnl
    total_trades = len(df)
    wins = len(df[df['pnl'] > 0])
    losses = len(df[df['pnl'] < 0])
    flat = total_trades - wins - losses
    wr = wins / total_trades * 100 if total_trades > 0 else 0

    print(f"\nOVERALL: {total_trades} trades | Win Rate: {wr:.1f}%")
    print(f"  Capital: Rs {initial_capital:,.0f} → Rs {final_capital:,.0f} ({(final_capital/initial_capital-1)*100:+.1f}%)")
    print(f"  Total PnL: Rs {total_pnl:,.0f} | Avg: Rs {total_pnl/total_trades:,.0f}")
    print(f"  Wins: {wins} | Losses: {losses} | Flat: {flat}")

    if wins > 0:
        avg_win = df[df['pnl'] > 0]['pnl'].mean()
        max_win = df['pnl'].max()
    else:
        avg_win = max_win = 0

    if losses > 0:
        avg_loss = df[df['pnl'] < 0]['pnl'].mean()
        max_loss = df['pnl'].min()
    else:
        avg_loss = max_loss = 0

    print(f"  Avg Win: Rs {avg_win:,.0f} | Avg Loss: Rs {avg_loss:,.0f}")
    print(f"  Max Win: Rs {max_win:,.0f} | Max Loss: Rs {max_loss:,.0f}")

    if avg_loss != 0:
        profit_factor = abs(avg_win * wins) / abs(avg_loss * losses)
        print(f"  Profit Factor: {profit_factor:.2f}")

    # Max Drawdown
    cumulative = df['pnl'].cumsum() + initial_capital
    peak = cumulative.cummax()
    drawdown = (peak - cumulative) / peak * 100
    max_dd = drawdown.max()
    print(f"  Max Drawdown: {max_dd:.1f}%")

    # By Strategy
    print(f"\n{'='*90}")
    print(f"{'STRATEGY':<15} {'TRADES':>7} {'WINS':>5} {'WR%':>6} {'TOTAL PNL':>12} {'AVG PNL':>10} "
          f"{'AVG WIN':>10} {'AVG LOSS':>10} {'MAX DD%':>8} {'SHARPE':>7}")
    print(f"{'-'*90}")

    strategy_stats = {}
    for strat in sorted(df['strategy'].unique()):
        sdf = df[df['strategy'] == strat]
        n = len(sdf)
        w = len(sdf[sdf['pnl'] > 0])
        l = len(sdf[sdf['pnl'] < 0])
        wr = w / n * 100 if n > 0 else 0
        tp = sdf['pnl'].sum()
        avg = tp / n if n > 0 else 0
        aw = sdf[sdf['pnl'] > 0]['pnl'].mean() if w > 0 else 0
        al = sdf[sdf['pnl'] < 0]['pnl'].mean() if l > 0 else 0

        # Sharpe approximation
        if n > 1:
            sharpe = (sdf['pnl'].mean() / sdf['pnl'].std()) * math.sqrt(252) if sdf['pnl'].std() > 0 else 0
        else:
            sharpe = 0

        # Strategy drawdown
        cum = sdf['pnl'].cumsum()
        pk = cum.cummax()
        dd = pk - cum
        mdd = (dd / (pk + initial_capital)).max() * 100 if len(cum) > 0 else 0

        print(f"{strat:<15} {n:>7} {w:>5} {wr:>5.1f}% {tp:>11,.0f} {avg:>9,.0f} "
              f"{aw:>9,.0f} {al:>9,.0f} {mdd:>7.1f}% {sharpe:>6.2f}")

        strategy_stats[strat] = {
            'trades': n, 'wins': w, 'losses': l, 'win_rate': wr,
            'total_pnl': tp, 'avg_pnl': avg, 'avg_win': aw, 'avg_loss': al,
            'max_dd': mdd, 'sharpe': sharpe,
        }

    # By Symbol
    print(f"\n{'='*90}")
    print(f"{'SYMBOL':<12} {'TRADES':>7} {'WR%':>6} {'TOTAL PNL':>12} {'AVG PNL':>10}")
    print(f"{'-'*50}")

    for sym in sorted(df['symbol'].unique()):
        sdf = df[df['symbol'] == sym]
        n = len(sdf)
        w = len(sdf[sdf['pnl'] > 0])
        wr = w / n * 100 if n > 0 else 0
        tp = sdf['pnl'].sum()
        avg = tp / n if n > 0 else 0
        print(f"{sym:<12} {n:>7} {wr:>5.1f}% {tp:>11,.0f} {avg:>9,.0f}")

    # By Exit Reason
    print(f"\n{'='*90}")
    print(f"{'EXIT REASON':<18} {'COUNT':>7} {'AVG PNL':>10} {'TOTAL PNL':>12}")
    print(f"{'-'*50}")

    for reason in sorted(df['exit_reason'].unique()):
        sdf = df[df['exit_reason'] == reason]
        n = len(sdf)
        tp = sdf['pnl'].sum()
        avg = tp / n if n > 0 else 0
        print(f"{reason:<18} {n:>7} {avg:>9,.0f} {tp:>11,.0f}")

    # Monthly P&L
    df['month'] = pd.to_datetime(df['date']).dt.to_period('M')
    monthly = df.groupby('month')['pnl'].agg(['sum', 'count'])

    print(f"\n{'='*90}")
    print("MONTHLY P&L:")
    print(f"{'MONTH':<10} {'TRADES':>7} {'PNL':>12} {'CUM PNL':>12}")
    print(f"{'-'*45}")

    cum = 0
    for month, row in monthly.iterrows():
        cum += row['sum']
        print(f"{str(month):<10} {int(row['count']):>7} {row['sum']:>11,.0f} {cum:>11,.0f}")

    # Strategy Ranking
    print(f"\n{'='*90}")
    print("STRATEGY RANKING (by risk-adjusted returns):")
    print(f"{'='*90}")

    rankings = []
    for strat, stats in strategy_stats.items():
        # Composite score: weighted average of normalized metrics
        score = 0
        if stats['trades'] >= 5:  # Minimum trade count
            score += stats['win_rate'] * 0.25  # 25% weight on win rate
            score += min(stats['sharpe'], 3) * 10 * 0.30  # 30% weight on Sharpe
            score += max(0, stats['avg_pnl']) / 100 * 0.25  # 25% weight on avg PnL
            score -= stats['max_dd'] * 0.20  # 20% penalty for drawdown
        rankings.append((strat, score, stats))

    rankings.sort(key=lambda x: -x[1])

    for rank, (strat, score, stats) in enumerate(rankings, 1):
        emoji = "✅" if stats['total_pnl'] > 0 else "❌"
        verdict = "RECOMMEND" if score > 15 and stats['total_pnl'] > 0 else "AVOID"
        print(f"  #{rank} {strat:<15} Score={score:.1f} | PnL=Rs {stats['total_pnl']:>10,.0f} | "
              f"WR={stats['win_rate']:.0f}% | Sharpe={stats['sharpe']:.2f} | → {verdict}")

    return strategy_stats


# ====================================================================
# MAIN
# ====================================================================
if __name__ == '__main__':
    import sys

    print("=" * 80)
    print("COMPREHENSIVE STRATEGY BACKTEST v2.5.3")
    print("=" * 80)

    symbols = ['NIFTY', 'BANKNIFTY', 'SENSEX']

    # Test 1: All strategies individually
    all_strats = ['CPR', 'Gamma Blast', 'Ghost Zone', 'PCR+VWAP', 'Survivor']

    print("\n\n" + "#" * 80)
    print("# TEST 1: ALL STRATEGIES COMBINED (last 6 months)")
    print("#" * 80)

    trades, final_cap, daily_pnl = run_backtest(
        symbols,
        strategies=all_strats,
        start_date='2025-09-01',
        initial_capital=300000,
    )
    stats_all = analyze_results(trades)

    # Test 2: Each strategy in isolation
    for strat in all_strats:
        print(f"\n\n{'#' * 80}")
        print(f"# TEST: {strat.upper()} ONLY (last 6 months)")
        print(f"{'#' * 80}")

        trades_single, _, _ = run_backtest(
            symbols,
            strategies=[strat],
            start_date='2025-09-01',
            initial_capital=300000,
        )
        if trades_single:
            analyze_results(trades_single)
        else:
            print(f"  No trades generated for {strat}")

    # Test 3: Best combination (top strategies only)
    print(f"\n\n{'#' * 80}")
    print("# TEST: RECOMMENDED COMBINATION")
    print(f"{'#' * 80}")

    # Based on individual results, combine top performers
    recommended = ['CPR', 'Survivor']  # Will be adjusted based on results

    trades_rec, _, _ = run_backtest(
        symbols,
        strategies=recommended,
        start_date='2025-09-01',
        initial_capital=300000,
    )
    if trades_rec:
        analyze_results(trades_rec)

    print("\n\nBacktest complete!")
