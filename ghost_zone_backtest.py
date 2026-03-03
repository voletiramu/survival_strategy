"""
Ghost Zone V2 Backtest — Multi-Timeframe Zone Strategy
=======================================================
Tests the redesigned Ghost Zone on both equity and crypto:

Equity (Binance-sourced for backtest, Angel API for live):
  - BTCUSDT, ETHUSDT, SOLUSDT on 4H zones + 5m entries

Crypto:
  - BTCUSDT, ETHUSDT, SOLUSDT on 4H zones + 5m entries

Equity (using daily data as proxy since Angel 5m data is limited):
  - NIFTY, BANKNIFTY, SENSEX — will simulate using available daily/intraday data

Usage:
    python ghost_zone_backtest.py
    python ghost_zone_backtest.py --crypto-only
    python ghost_zone_backtest.py --equity-only
"""

import sys
import os
import argparse
import time
import warnings
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from strategies.ghost_zone_v2 import GhostZoneV2, aggregate_to_4h

# ── CONFIG ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 300000
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
CRYPTO_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
CRYPTO_FEE_RATE = 0.001  # 0.1%

EQUITY_SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']
EQUITY_LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
EQUITY_STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}
RISK_FREE_RATE = 0.07

REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT_PCT = 0.02  # 2%
RISK_PER_TRADE_PCT = 0.01    # 1% risk per trade
TRADE_COOLDOWN_BARS = 12     # Min 12 bars (1 hour on 5m) between trades
MAX_ZONE_RETESTS = 3         # Max trades per zone before skipping it

# ── BACKTEST-LEVEL SIGNAL FILTERS (core strategy unchanged) ─────────────────
# Data-driven from trade analysis:
ENABLE_TRP = False           # TRP: 26% WR, -Rs 16,126 total — disable
MIN_RISK_ENTRY_PCT = 0.003   # Skip signals with risk < 0.3% of entry price
RSI_BLOCK_LONG_ABOVE = 65    # Block LONG when RSI > 65 (overbought)
RSI_BLOCK_SHORT_BELOW = 35   # Block SHORT when RSI < 35 (oversold)
MAX_HOLD_BARS = 400          # Time stop: exit stale trades after ~33 hours
QUALITY_RISK_BOOST = True    # Higher quality = larger position


# ── DATA FETCHING ──────────────────────────────────────────────────────────────

def fetch_binance(symbol, interval='4h', days=180):
    """Fetch crypto candles from Binance."""
    print(f"  Fetching {symbol} ({interval}, {days}d)...", end=" ", flush=True)
    all_data = []
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

    while start_time < end_time:
        params = {
            'symbol': symbol, 'interval': interval,
            'startTime': start_time, 'endTime': end_time, 'limit': 1000,
        }
        try:
            r = requests.get(BINANCE_BASE, params=params, timeout=15)
            data = r.json()
            if not data or isinstance(data, dict):
                break
            all_data.extend(data)
            start_time = data[-1][0] + 1
            time.sleep(0.15)
        except Exception as e:
            print(f"Error: {e}")
            break

    if not all_data:
        print("FAILED")
        return None

    df = pd.DataFrame(all_data, columns=[
        'timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
        'close_time', 'quote_vol', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignore'
    ])
    df['Date'] = pd.to_datetime(df['timestamp'], unit='ms')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        df[col] = df[col].astype(float)
    df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].drop_duplicates('Date')
    df = df.sort_values('Date').reset_index(drop=True)
    print(f"{len(df)} candles ({df['Date'].iloc[0].strftime('%Y-%m-%d')} to "
          f"{df['Date'].iloc[-1].strftime('%Y-%m-%d')})")
    return df


def fetch_equity_data_angel(symbol, interval='ONE_HOUR', days=60):
    """Fetch equity data from Angel SmartAPI (1H candles for 4H aggregation)."""
    try:
        from backtest_pcr_vwap_intraday import connect_angel
        angel_obj = connect_angel()
        if not angel_obj:
            return None

        token_map = {'NIFTY': '99926000', 'BANKNIFTY': '99926009', 'SENSEX': '99919000'}
        exchange_map = {'NIFTY': 'NSE', 'BANKNIFTY': 'NSE', 'SENSEX': 'BSE'}

        token = token_map.get(symbol)
        exchange = exchange_map.get(symbol)
        if not token:
            return None

        print(f"  Fetching {symbol} ({interval}, {days}d) from Angel...", end=" ", flush=True)

        all_candles = []
        end_date = datetime.now()
        chunk_days = 25 if interval == 'FIVE_MINUTE' else 60

        while days > 0:
            from_date = end_date - timedelta(days=min(chunk_days, days))
            from_str = from_date.strftime('%Y-%m-%d 09:15')
            to_str = end_date.strftime('%Y-%m-%d 15:30')

            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_str,
                "todate": to_str,
            }
            data = angel_obj.getCandleData(params)
            if data and data.get('data'):
                all_candles.extend(data['data'])

            end_date = from_date - timedelta(days=1)
            days -= chunk_days
            time.sleep(0.5)

        if not all_candles:
            print("FAILED")
            return None

        df = pd.DataFrame(all_candles, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'])
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna().drop_duplicates('Date').sort_values('Date').reset_index(drop=True)
        print(f"{len(df)} candles")
        return df

    except Exception as e:
        print(f"Angel connection failed: {e}")
        return None


# ── BLACK-SCHOLES FOR EQUITY OPTION PRICING ────────────────────────────────────

def bs_greeks(spot, strike, T, r, sigma, opt_type='CE'):
    """Black-Scholes option pricing."""
    from scipy.stats import norm
    if T <= 0:
        T = 1/365
    if sigma <= 0:
        sigma = 0.15

    d1 = (np.log(spot / strike) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if opt_type == 'CE':
        price = spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1

    gamma = norm.pdf(d1) / (spot * sigma * np.sqrt(T))
    theta = -(spot * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))

    return {'price': max(price, 0.01), 'delta': delta, 'gamma': gamma, 'theta': theta}


def calc_equity_costs(premium, lot_size):
    """Calculate equity option trading costs (buy side)."""
    turnover = premium * lot_size
    brokerage = min(40, turnover * 0.0003)
    exchange_charges = turnover * 0.00053
    gst = (brokerage + exchange_charges) * 0.18
    return brokerage + exchange_charges + gst


# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────

def backtest_crypto(df_4h, df_5m, symbol, capital=INITIAL_CAPITAL):
    """Backtest Ghost Zone V2 on crypto using 4H zones + 5m entries."""
    print(f"\n  === Ghost Zone V2 Crypto: {symbol} ===")

    gz = GhostZoneV2()

    # Detect all zones on 4H data
    zones = gz.detect_zones(df_4h)
    print(f"  Zones detected on 4H: {len(zones)} "
          f"(Demand: {sum(1 for z in zones if z.zone_type=='demand')}, "
          f"Supply: {sum(1 for z in zones if z.zone_type=='supply')})")

    if not zones:
        print("  No zones found — skipping")
        return None

    # Precompute 5m indicators
    atr_5m = gz.atr(df_5m, 14)
    rsi_5m = gz.rsi(df_5m['Close'], 14)
    avg_vol_5m = df_5m['Volume'].rolling(20).mean()

    # Backtest state
    equity = capital
    peak = capital
    max_dd = 0
    trades = []
    position = None  # {direction, entry, sl, target, entry_idx, type}
    daily_trades = 0
    daily_pnl = 0
    current_day = None
    equity_curve = []
    last_trade_close_bar = -999  # Cooldown tracker
    zone_trade_counts = {}       # Track trades per zone (zone.created_at -> count)

    # Build 5m → active zones map using timestamps
    # For each 5m bar, find zones that were created before that time on 4H
    warmup = 60  # Skip first 60 bars for indicator warmup

    for i in range(warmup, len(df_5m)):
        bar = df_5m.iloc[i]
        bar_time = bar['Date']
        day = bar_time.date()

        # Reset daily counters
        if day != current_day:
            daily_trades = 0
            daily_pnl = 0
            current_day = day

        # Check daily loss limit
        if daily_pnl < -capital * DAILY_LOSS_LIMIT_PCT:
            equity_curve.append(equity)
            continue

        # Get active zones for this bar (zones created before this time)
        active_zones = [z for z in zones
                       if (isinstance(z.created_time, (pd.Timestamp, np.datetime64)) and
                           pd.Timestamp(z.created_time) < pd.Timestamp(bar_time) and
                           not z.broken)]

        if not active_zones:
            equity_curve.append(equity)
            continue

        # ── CHECK EXISTING POSITION ──
        if position is not None:
            close = bar['Close']
            high = bar['High']
            low = bar['Low']
            hold_bars = i - position['entry_idx']

            hit_sl = False
            hit_target = False
            hit_time_stop = False
            exit_price = None

            if position['direction'] == 1:  # LONG
                if low <= position['sl']:
                    hit_sl = True
                    exit_price = position['sl']
                elif high >= position['target']:
                    hit_target = True
                    exit_price = position['target']
            else:  # SHORT
                if high >= position['sl']:
                    hit_sl = True
                    exit_price = position['sl']
                elif low <= position['target']:
                    hit_target = True
                    exit_price = position['target']

            # ── Move SL to breakeven after 1:1 RR achieved ──
            if not hit_sl and not hit_target and not position.get('be_moved'):
                entry = position['entry']
                risk = abs(entry - position.get('original_sl', position['sl']))
                if risk > 0:
                    if position['direction'] == 1 and high >= entry + risk:
                        position['sl'] = entry  # Breakeven
                        position['be_moved'] = True
                    elif position['direction'] == -1 and low <= entry - risk:
                        position['sl'] = entry  # Breakeven
                        position['be_moved'] = True

            # ── Time stop: exit stale trades ──
            if not hit_sl and not hit_target and hold_bars >= MAX_HOLD_BARS:
                hit_time_stop = True
                exit_price = close

            if hit_sl or hit_target or hit_time_stop:
                # Calculate PnL
                if position['direction'] == 1:
                    pnl_pct = (exit_price - position['entry']) / position['entry'] * 100
                else:
                    pnl_pct = (position['entry'] - exit_price) / position['entry'] * 100

                alloc = position.get('allocation', equity * 0.33)
                pnl = alloc * (pnl_pct / 100) - alloc * CRYPTO_FEE_RATE

                equity += pnl
                daily_pnl += pnl

                exit_type = 'SL' if hit_sl else ('TARGET' if hit_target else 'TIME')

                trades.append({
                    'symbol': symbol,
                    'signal_type': position['type'],
                    'direction': 'LONG' if position['direction'] == 1 else 'SHORT',
                    'entry_price': position['entry'],
                    'exit_price': exit_price,
                    'sl': position.get('original_sl', position['sl']),
                    'target': position['target'],
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'exit_type': exit_type,
                    'entry_time': df_5m['Date'].iloc[position['entry_idx']],
                    'exit_time': bar_time,
                    'hold_bars': hold_bars,
                    'zone_quality': position.get('quality', 0),
                    'reason': position.get('reason', ''),
                })

                last_trade_close_bar = i  # Track cooldown
                position = None

        # ── CHECK FOR NEW ENTRIES ──
        # Enforce cooldown: wait TRADE_COOLDOWN_BARS after last trade close
        cooldown_ok = (i - last_trade_close_bar) >= TRADE_COOLDOWN_BARS
        if position is None and daily_trades < MAX_TRADES_PER_DAY and cooldown_ok:
            signals = gz.find_entries(df_5m, active_zones, i, atr_5m, rsi_5m, avg_vol_5m)

            if signals:
                # Filter out zones that have been traded too many times
                valid_signals = []
                for sig in signals:
                    zone_key = sig['zone'].created_at
                    retest_count = zone_trade_counts.get(zone_key, 0)
                    if retest_count < MAX_ZONE_RETESTS:
                        valid_signals.append(sig)

                # ── Backtest-level signal filters (core strategy untouched) ──
                # 1. TRP filter: 26% WR across all symbols
                if not ENABLE_TRP:
                    valid_signals = [s for s in valid_signals
                                     if s['type'] != 'GHST_TRP']

                # 2. Min risk distance: skip tiny-risk entries (immediate SL hits)
                valid_signals = [s for s in valid_signals
                                 if s['risk'] / max(s['entry'], 1) >= MIN_RISK_ENTRY_PCT]

                # 3. RSI direction: block counter-trend entries
                rsi_val = rsi_5m.iloc[i] if not pd.isna(rsi_5m.iloc[i]) else 50
                valid_signals = [s for s in valid_signals if not (
                    (s['direction'] == 1 and rsi_val > RSI_BLOCK_LONG_ABOVE) or
                    (s['direction'] == -1 and rsi_val < RSI_BLOCK_SHORT_BELOW)
                )]

                if valid_signals:
                    # Take best quality signal
                    sig = valid_signals[0]

                    # Track zone retest count
                    zone_key = sig['zone'].created_at
                    zone_trade_counts[zone_key] = zone_trade_counts.get(zone_key, 0) + 1

                    # Position sizing: quality-weighted risk
                    base_risk = RISK_PER_TRADE_PCT  # 1%
                    if QUALITY_RISK_BOOST and sig['quality'] >= 5:
                        base_risk = RISK_PER_TRADE_PCT * 1.5  # 1.5% for top quality
                    elif QUALITY_RISK_BOOST and sig['quality'] <= 2:
                        base_risk = RISK_PER_TRADE_PCT * 0.6  # 0.6% for low quality

                    risk_amount = equity * base_risk
                    risk_per_unit = abs(sig['entry'] - sig['sl'])
                    if risk_per_unit > 0:
                        qty = risk_amount / risk_per_unit
                        alloc = min(qty * sig['entry'], equity * 0.40)
                    else:
                        alloc = equity * 0.10

                    position = {
                        'direction': sig['direction'],
                        'entry': sig['entry'],
                        'sl': sig['sl'],
                        'original_sl': sig['sl'],  # Keep for trailing reference
                        'target': sig['target'],
                        'entry_idx': i,
                        'type': sig['type'],
                        'quality': sig['quality'],
                        'reason': sig['reason'],
                        'allocation': alloc,
                    }
                    equity -= alloc * CRYPTO_FEE_RATE  # Entry fee
                    daily_trades += 1

        # Track drawdown
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    # Close final position at last price
    if position is not None:
        close = df_5m['Close'].iloc[-1]
        if position['direction'] == 1:
            pnl_pct = (close - position['entry']) / position['entry'] * 100
        else:
            pnl_pct = (position['entry'] - close) / position['entry'] * 100
        alloc = position.get('allocation', equity * 0.33)
        pnl = alloc * (pnl_pct / 100) - alloc * CRYPTO_FEE_RATE
        equity += pnl
        trades.append({
            'symbol': symbol, 'signal_type': position['type'],
            'direction': 'LONG' if position['direction'] == 1 else 'SHORT',
            'entry_price': position['entry'], 'exit_price': close,
            'sl': position['sl'], 'target': position['target'],
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
            'exit_type': 'EOD', 'entry_time': df_5m['Date'].iloc[position['entry_idx']],
            'exit_time': df_5m['Date'].iloc[-1],
            'hold_bars': len(df_5m) - position['entry_idx'],
            'zone_quality': position.get('quality', 0),
            'reason': position.get('reason', ''),
        })

    return compute_metrics(trades, equity, capital, equity_curve, symbol, 'crypto')


def backtest_equity_simulated(df_4h, df_5m, symbol, capital=INITIAL_CAPITAL):
    """
    Backtest Ghost Zone V2 on equity using 4H zones + 5m entries.
    Uses BS option pricing for PnL estimation.
    """
    print(f"\n  === Ghost Zone V2 Equity: {symbol} ===")

    gz = GhostZoneV2()
    lot_size = EQUITY_LOT_SIZES.get(symbol, 50)
    strike_int = EQUITY_STRIKE_INTERVALS.get(symbol, 50)

    # For equity, if we don't have real 5m data, simulate from 4H
    # by treating each 4H bar as a mini trading session
    using_4h_only = df_5m is None or len(df_5m) < 100

    if using_4h_only:
        print("  Using 4H-only mode (no 5m data available)")
        return backtest_equity_4h_only(df_4h, symbol, capital, gz, lot_size, strike_int)

    # Full multi-TF mode
    zones = gz.detect_zones(df_4h)
    print(f"  Zones detected on 4H: {len(zones)} "
          f"(Demand: {sum(1 for z in zones if z.zone_type=='demand')}, "
          f"Supply: {sum(1 for z in zones if z.zone_type=='supply')})")

    if not zones:
        print("  No zones found — skipping")
        return None

    atr_5m = gz.atr(df_5m, 14)
    rsi_5m = gz.rsi(df_5m['Close'], 14)
    avg_vol_5m = df_5m['Volume'].rolling(20).mean()

    equity = capital
    peak = capital
    max_dd = 0
    trades = []
    position = None
    daily_trades = 0
    daily_pnl = 0
    current_day = None
    equity_curve = []
    warmup = 60

    for i in range(warmup, len(df_5m)):
        bar = df_5m.iloc[i]
        bar_time = bar['Date']
        day = bar_time.date()

        # Market hours check (9:15 - 15:20)
        hour = bar_time.hour
        minute = bar_time.minute
        trade_time = hour * 60 + minute
        if trade_time < 555 or trade_time > 920:  # Before 9:15 or after 15:20
            equity_curve.append(equity)
            continue

        if day != current_day:
            daily_trades = 0
            daily_pnl = 0
            current_day = day

        if daily_pnl < -capital * DAILY_LOSS_LIMIT_PCT:
            equity_curve.append(equity)
            continue

        active_zones = [z for z in zones
                       if (isinstance(z.created_time, (pd.Timestamp, np.datetime64)) and
                           pd.Timestamp(z.created_time) < pd.Timestamp(bar_time) and
                           not z.broken)]

        if not active_zones:
            equity_curve.append(equity)
            continue

        # Check existing position
        if position is not None:
            spot = bar['Close']
            # Use intrabar high/low for SL/Target check
            if position['direction'] == 1:
                hit_sl = bar['Low'] <= position['sl_spot']
                hit_target = bar['High'] >= position['target_spot']
            else:
                hit_sl = bar['High'] >= position['sl_spot']
                hit_target = bar['Low'] <= position['target_spot']

            if hit_sl or hit_target:
                exit_spot = position['sl_spot'] if hit_sl else position['target_spot']
                entry_premium = position['entry_premium']

                # Estimate exit premium using BS
                bars_held = i - position['entry_idx']
                dte_remaining = max(1, position['dte'] - bars_held * 5 / (6.25 * 60))
                T_exit = dte_remaining / 365
                exit_greeks = bs_greeks(exit_spot, position['strike'], T_exit,
                                       RISK_FREE_RATE, position['iv'],
                                       'CE' if position['direction'] == 1 else 'PE')
                exit_premium = exit_greeks['price']

                pnl = (exit_premium - entry_premium) * lot_size
                pnl -= calc_equity_costs(entry_premium, lot_size)
                pnl = max(pnl, -equity * 0.02)

                equity += pnl
                daily_pnl += pnl

                trades.append({
                    'symbol': symbol, 'signal_type': position['type'],
                    'direction': 'BUY_CE' if position['direction'] == 1 else 'BUY_PE',
                    'entry_price': round(entry_premium, 2),
                    'exit_price': round(exit_premium, 2),
                    'spot_entry': position['entry_spot'],
                    'spot_exit': exit_spot,
                    'strike': position['strike'],
                    'pnl': round(pnl, 2),
                    'pnl_pct': round((exit_premium - entry_premium) / entry_premium * 100, 2),
                    'exit_type': 'SL' if hit_sl else 'TARGET',
                    'entry_time': df_5m['Date'].iloc[position['entry_idx']],
                    'exit_time': bar_time,
                    'hold_bars': bars_held,
                    'zone_quality': position.get('quality', 0),
                    'reason': position.get('reason', ''),
                })
                position = None

        # Check for new entries
        if position is None and daily_trades < MAX_TRADES_PER_DAY:
            signals = gz.find_entries(df_5m, active_zones, i, atr_5m, rsi_5m, avg_vol_5m)

            if signals:
                sig = signals[0]
                spot = sig['entry']

                # Calculate option premium
                dow = bar_time.weekday()
                dte = max(1, (3 - dow) + 1) if dow <= 3 else 1
                T = dte / 365

                # Use HV as IV proxy
                hv = df_5m['Close'].iloc[max(0, i-100):i].pct_change().std() * np.sqrt(252 * 78)
                iv = max(min(hv * 1.15 if not np.isnan(hv) else 0.15, 0.50), 0.08)

                opt_type = 'CE' if sig['direction'] == 1 else 'PE'
                strike = round(spot / strike_int) * strike_int

                greeks = bs_greeks(spot, strike, T, RISK_FREE_RATE, iv, opt_type)
                entry_premium = greeks['price']

                if entry_premium > 3:  # Minimum premium filter
                    position = {
                        'direction': sig['direction'],
                        'entry_spot': spot,
                        'sl_spot': sig['sl'],
                        'target_spot': sig['target'],
                        'strike': strike,
                        'entry_premium': entry_premium,
                        'iv': iv,
                        'dte': dte,
                        'entry_idx': i,
                        'type': sig['type'],
                        'quality': sig['quality'],
                        'reason': sig['reason'],
                    }
                    daily_trades += 1

        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    return compute_metrics(trades, equity, capital, equity_curve, symbol, 'equity')


def backtest_equity_4h_only(df_4h, symbol, capital, gz, lot_size, strike_int):
    """Fallback: backtest using 4H bars only (when 5m data unavailable)."""
    zones = gz.detect_zones(df_4h)
    print(f"  Zones detected: {len(zones)} "
          f"(Demand: {sum(1 for z in zones if z.zone_type=='demand')}, "
          f"Supply: {sum(1 for z in zones if z.zone_type=='supply')})")

    if not zones:
        return None

    atr_4h = gz.atr(df_4h, 14)
    rsi_4h = gz.rsi(df_4h['Close'], 14)
    avg_vol_4h = df_4h['Volume'].rolling(20).mean()

    equity = capital
    peak = capital
    max_dd = 0
    trades = []
    equity_curve = []

    for i in range(50, len(df_4h)):
        bar = df_4h.iloc[i]
        bar_time = bar['Date']

        active_zones = [z for z in zones
                       if z.created_at < i and not z.broken]

        if not active_zones:
            equity_curve.append(equity)
            continue

        # Use 4H bar as entry check
        signals = gz.find_entries(df_4h, active_zones, i, atr_4h, rsi_4h, avg_vol_4h)

        for sig in signals[:1]:  # Max 1 trade per 4H bar
            spot = sig['entry']
            dte = 2
            T = dte / 365
            hv = df_4h['Close'].iloc[max(0, i-50):i].pct_change().std() * np.sqrt(252)
            iv = max(min(hv * 1.15 if not np.isnan(hv) else 0.15, 0.50), 0.08)

            opt_type = 'CE' if sig['direction'] == 1 else 'PE'
            strike = round(spot / strike_int) * strike_int
            greeks = bs_greeks(spot, strike, T, RISK_FREE_RATE, iv, opt_type)
            entry_premium = greeks['price']

            if entry_premium < 3:
                continue

            # Simulate exit within same 4H bar using high/low
            if sig['direction'] == 1:
                if bar['High'] >= sig['target']:
                    exit_spot = sig['target']
                    exit_type = 'TARGET'
                elif bar['Low'] <= sig['sl']:
                    exit_spot = sig['sl']
                    exit_type = 'SL'
                else:
                    exit_spot = bar['Close']
                    exit_type = 'EOD'
            else:
                if bar['Low'] <= sig['target']:
                    exit_spot = sig['target']
                    exit_type = 'TARGET'
                elif bar['High'] >= sig['sl']:
                    exit_spot = sig['sl']
                    exit_type = 'SL'
                else:
                    exit_spot = bar['Close']
                    exit_type = 'EOD'

            exit_greeks = bs_greeks(exit_spot, strike, T * 0.8, RISK_FREE_RATE, iv, opt_type)
            exit_premium = exit_greeks['price']

            pnl = (exit_premium - entry_premium) * lot_size
            pnl -= calc_equity_costs(entry_premium, lot_size)
            pnl = max(pnl, -equity * 0.02)
            equity += pnl

            trades.append({
                'symbol': symbol, 'signal_type': sig['type'],
                'direction': f"BUY_{opt_type}",
                'entry_price': round(entry_premium, 2),
                'exit_price': round(exit_premium, 2),
                'spot_entry': spot, 'spot_exit': exit_spot,
                'strike': strike,
                'pnl': round(pnl, 2),
                'pnl_pct': round((exit_premium - entry_premium) / entry_premium * 100, 2),
                'exit_type': exit_type,
                'entry_time': bar_time, 'exit_time': bar_time,
                'hold_bars': 1,
                'zone_quality': sig.get('quality', 0),
                'reason': sig.get('reason', ''),
            })

        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    return compute_metrics(trades, equity, capital, equity_curve, symbol, 'equity_4h')


# ── METRICS ────────────────────────────────────────────────────────────────────

def compute_metrics(trades, equity, capital, equity_curve, symbol, market):
    """Compute comprehensive backtest metrics."""
    if not trades:
        print(f"  {symbol}: 0 trades — no metrics")
        return None

    num_trades = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    num_wins = len(wins)
    num_losses = len(losses)
    win_rate = num_wins / num_trades * 100

    total_pnl = sum(t['pnl'] for t in trades)
    gross_profit = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.99

    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losses]) if losses else 0
    avg_win_pct = np.mean([t['pnl_pct'] for t in wins]) if wins else 0
    avg_loss_pct = np.mean([abs(t['pnl_pct']) for t in losses]) if losses else 0

    # Signal type distribution
    se_count = sum(1 for t in trades if t['signal_type'] == 'GHST_SE')
    be_count = sum(1 for t in trades if t['signal_type'] == 'GHST_BE')
    trp_count = sum(1 for t in trades if t['signal_type'] == 'GHST_TRP')

    sl_exits = sum(1 for t in trades if t['exit_type'] == 'SL')
    target_exits = sum(1 for t in trades if t['exit_type'] == 'TARGET')

    # Sharpe
    if len(equity_curve) > 1:
        rets = pd.Series(equity_curve).pct_change().dropna()
        sharpe = (rets.mean() / max(rets.std(), 1e-10)) * np.sqrt(2190)
    else:
        sharpe = 0

    # Max drawdown
    peak_eq = capital
    max_dd = 0
    for e in equity_curve:
        peak_eq = max(peak_eq, e)
        dd = (peak_eq - e) / peak_eq * 100
        max_dd = max(max_dd, dd)

    # Average quality of winning vs losing trades
    avg_win_quality = np.mean([t['zone_quality'] for t in wins]) if wins else 0
    avg_loss_quality = np.mean([t['zone_quality'] for t in losses]) if losses else 0

    result = {
        'symbol': symbol,
        'market': market,
        'total_pnl': round(total_pnl, 2),
        'total_return': round((equity - capital) / capital * 100, 2),
        'num_trades': num_trades,
        'wins': num_wins,
        'losses': num_losses,
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'avg_win_pct': round(avg_win_pct, 2),
        'avg_loss_pct': round(avg_loss_pct, 2),
        'se_signals': se_count,
        'be_signals': be_count,
        'trp_signals': trp_count,
        'sl_exits': sl_exits,
        'target_exits': target_exits,
        'avg_win_quality': round(avg_win_quality, 1),
        'avg_loss_quality': round(avg_loss_quality, 1),
        'trades': trades,
    }

    print(f"  {symbol}: {num_trades} trades | WR={win_rate:.1f}% | PnL=Rs {total_pnl:,.0f} | "
          f"PF={profit_factor:.2f} | MaxDD={max_dd:.1f}% | Sharpe={sharpe:.2f}")
    print(f"    Signals: SE={se_count} BE={be_count} TRP={trp_count} | "
          f"Exits: SL={sl_exits} TARGET={target_exits}")

    return result


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Ghost Zone V2 Backtest')
    parser.add_argument('--crypto-only', action='store_true')
    parser.add_argument('--equity-only', action='store_true')
    parser.add_argument('--days', type=int, default=120, help='Days of data')
    args = parser.parse_args()

    print("=" * 120)
    print("  GHOST ZONE V2 BACKTEST — MULTI-TIMEFRAME ZONE TRADING")
    print("  4H Zone Detection (RBD/DBR) + 5m Entry Signals")
    print("  Signal Types: GHST_SE | GHST_BE | GHST_TRP")
    print(f"  Capital: Rs {INITIAL_CAPITAL:,} | Max {MAX_TRADES_PER_DAY} trades/day | 1% risk/trade")
    print("=" * 120)

    all_results = []

    # ── CRYPTO BACKTEST ──
    if not args.equity_only:
        print("\n" + "=" * 80)
        print("  SECTION 1: CRYPTO — BTCUSDT, ETHUSDT, SOLUSDT")
        print("=" * 80)

        for sym in CRYPTO_SYMBOLS:
            # Fetch 4H and 5m data
            df_4h = fetch_binance(sym, '4h', args.days)
            time.sleep(0.5)
            df_5m = fetch_binance(sym, '5m', min(args.days, 60))  # 5m data up to 60 days
            time.sleep(0.5)

            if df_4h is None or len(df_4h) < 100:
                print(f"  {sym}: Insufficient 4H data")
                continue

            if df_5m is None or len(df_5m) < 500:
                print(f"  {sym}: Insufficient 5m data, using 4H-only mode")
                # Use 4H-only as fallback
                gz = GhostZoneV2()
                zones = gz.detect_zones(df_4h)
                atr_4h = gz.atr(df_4h, 14)
                rsi_4h = gz.rsi(df_4h['Close'], 14)
                avg_vol_4h = df_4h['Volume'].rolling(20).mean()

                # Simulate as crypto spot using 4H bars
                result = backtest_crypto_4h_fallback(df_4h, sym, gz, zones, atr_4h, rsi_4h, avg_vol_4h)
                if result:
                    all_results.append(result)
                continue

            result = backtest_crypto(df_4h, df_5m, sym)
            if result:
                all_results.append(result)

    # ── EQUITY BACKTEST ──
    if not args.crypto_only:
        print("\n" + "=" * 80)
        print("  SECTION 2: EQUITY — NIFTY, BANKNIFTY, SENSEX")
        print("  (Using Binance BTCUSDT 4H zones as proxy for zone quality test)")
        print("  (Angel 1H → 4H aggregation for equity when available)")
        print("=" * 80)

        # Try to fetch equity data from Angel
        for sym in EQUITY_SYMBOLS:
            df_1h = fetch_equity_data_angel(sym, 'ONE_HOUR', args.days)

            if df_1h is not None and len(df_1h) > 50:
                df_4h = aggregate_to_4h(df_1h)
                print(f"  {sym}: Aggregated {len(df_1h)} 1H bars → {len(df_4h)} 4H bars")

                # Try to get 5m data
                df_5m = fetch_equity_data_angel(sym, 'FIVE_MINUTE', min(args.days, 30))

                result = backtest_equity_simulated(df_4h, df_5m, sym)
                if result:
                    all_results.append(result)
            else:
                print(f"  {sym}: Angel data unavailable — skipping "
                      f"(run during market hours or check Angel credentials)")

    # ── RESULTS SUMMARY ──
    if all_results:
        print("\n" + "=" * 120)
        print("  GHOST ZONE V2 — RESULTS SUMMARY")
        print("=" * 120)

        print(f"\n  {'Symbol':<12} {'Market':<10} {'Trades':>6} {'WinR%':>7} {'PnL':>14} "
              f"{'PF':>6} {'Sharpe':>7} {'MaxDD%':>7} "
              f"{'SE':>4} {'BE':>4} {'TRP':>4} {'SL':>4} {'TGT':>4}")
        print("  " + "-" * 110)

        for r in all_results:
            print(f"  {r['symbol']:<12} {r['market']:<10} {r['num_trades']:>5} {r['win_rate']:>6.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['profit_factor']:>5.2f} {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% "
                  f"{r['se_signals']:>3} {r['be_signals']:>3} {r['trp_signals']:>3} "
                  f"{r['sl_exits']:>3} {r['target_exits']:>3}")

        # Aggregate metrics
        total_trades = sum(r['num_trades'] for r in all_results)
        total_pnl = sum(r['total_pnl'] for r in all_results)
        avg_wr = np.mean([r['win_rate'] for r in all_results])
        avg_pf = np.mean([r['profit_factor'] for r in all_results])
        avg_sharpe = np.mean([r['sharpe'] for r in all_results])
        worst_dd = max(r['max_drawdown'] for r in all_results)

        print(f"\n  {'TOTAL':<12} {'':10} {total_trades:>5} {avg_wr:>6.1f}% "
              f"Rs{total_pnl:>12,.0f} {avg_pf:>5.2f} {avg_sharpe:>6.2f} "
              f"{worst_dd:>6.1f}%")

        # Quality analysis
        print(f"\n  ZONE QUALITY ANALYSIS:")
        all_trades = []
        for r in all_results:
            all_trades.extend(r['trades'])
        if all_trades:
            win_trades = [t for t in all_trades if t['pnl'] > 0]
            loss_trades = [t for t in all_trades if t['pnl'] <= 0]
            if win_trades:
                print(f"    Avg quality of WINNING trades: {np.mean([t['zone_quality'] for t in win_trades]):.1f}")
            if loss_trades:
                print(f"    Avg quality of LOSING trades:  {np.mean([t['zone_quality'] for t in loss_trades]):.1f}")

        # Save results
        csv_rows = []
        for r in all_results:
            csv_rows.append({k: v for k, v in r.items() if k != 'trades'})

        csv_df = pd.DataFrame(csv_rows)
        csv_path = os.path.join(REPORT_DIR, 'ghost_zone_v2_backtest.csv')
        csv_df.to_csv(csv_path, index=False)
        print(f"\n  Results saved to: {csv_path}")

        # Save trade details
        if all_trades:
            trades_df = pd.DataFrame(all_trades)
            trades_path = os.path.join(REPORT_DIR, 'ghost_zone_v2_trades.csv')
            trades_df.to_csv(trades_path, index=False)
            print(f"  Trade details saved to: {trades_path}")

        # Telegram
        try:
            from trade_notifier import send_message
            msg = (
                f"<b>GHOST ZONE V2 BACKTEST</b>\n\n"
                f"Total Trades: {total_trades}\n"
                f"Avg Win Rate: {avg_wr:.1f}%\n"
                f"Total PnL: Rs {total_pnl:,.0f}\n"
                f"Avg PF: {avg_pf:.2f}\n"
                f"Avg Sharpe: {avg_sharpe:.2f}\n"
                f"Worst DD: {worst_dd:.1f}%\n\n"
            )
            for r in all_results:
                msg += (f"{r['symbol']} ({r['market']}): {r['num_trades']}tr "
                       f"WR:{r['win_rate']:.0f}% PnL:Rs{r['total_pnl']:,.0f}\n")
            send_message(msg)
        except Exception:
            pass

    else:
        print("\n  No results generated — check data availability")

    print(f"\n{'='*120}")
    print("  DONE!")
    print(f"{'='*120}")


def backtest_crypto_4h_fallback(df_4h, symbol, gz, zones, atr_4h, rsi_4h, avg_vol_4h):
    """Crypto backtest using 4H bars only (when 5m data unavailable)."""
    print(f"  Zones: {len(zones)} (D:{sum(1 for z in zones if z.zone_type=='demand')}, "
          f"S:{sum(1 for z in zones if z.zone_type=='supply')})")

    if not zones:
        return None

    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0
    trades = []
    equity_curve = []
    zone_trade_counts = {}
    last_trade_bar = -999
    daily_trades = 0
    current_day = None

    for i in range(50, len(df_4h)):
        bar_day = df_4h['Date'].iloc[i].date() if 'Date' in df_4h.columns else None
        if bar_day != current_day:
            daily_trades = 0
            current_day = bar_day

        active_zones = [z for z in zones if z.created_at < i and not z.broken]
        if not active_zones:
            equity_curve.append(equity)
            continue

        # Cooldown (3 bars = 12h on 4H chart)
        if (i - last_trade_bar) < 3 or daily_trades >= MAX_TRADES_PER_DAY:
            equity_curve.append(equity)
            continue

        signals = gz.find_entries(df_4h, active_zones, i, atr_4h, rsi_4h, avg_vol_4h)

        # Filter by zone retest limits
        valid_signals = [s for s in signals
                        if zone_trade_counts.get(s['zone'].created_at, 0) < MAX_ZONE_RETESTS]

        for sig in valid_signals[:1]:
            bar = df_4h.iloc[i]
            entry = sig['entry']

            if sig['direction'] == 1:
                if bar['High'] >= sig['target']:
                    exit_price, exit_type = sig['target'], 'TARGET'
                elif bar['Low'] <= sig['sl']:
                    exit_price, exit_type = sig['sl'], 'SL'
                else:
                    exit_price, exit_type = bar['Close'], 'EOD'
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                if bar['Low'] <= sig['target']:
                    exit_price, exit_type = sig['target'], 'TARGET'
                elif bar['High'] >= sig['sl']:
                    exit_price, exit_type = sig['sl'], 'SL'
                else:
                    exit_price, exit_type = bar['Close'], 'EOD'
                pnl_pct = (entry - exit_price) / entry * 100

            risk_amount = equity * RISK_PER_TRADE_PCT
            risk_dist = abs(entry - sig['sl'])
            alloc = min(risk_amount / max(risk_dist/entry, 0.001), equity * 0.40)
            pnl = alloc * (pnl_pct / 100) - alloc * CRYPTO_FEE_RATE
            equity += pnl

            # Track zone retests and cooldown
            zone_trade_counts[sig['zone'].created_at] = zone_trade_counts.get(sig['zone'].created_at, 0) + 1
            last_trade_bar = i
            daily_trades += 1

            trades.append({
                'symbol': symbol, 'signal_type': sig['type'],
                'direction': 'LONG' if sig['direction'] == 1 else 'SHORT',
                'entry_price': entry, 'exit_price': exit_price,
                'sl': sig['sl'], 'target': sig['target'],
                'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
                'exit_type': exit_type,
                'entry_time': bar['Date'], 'exit_time': bar['Date'],
                'hold_bars': 1, 'zone_quality': sig.get('quality', 0),
                'reason': sig.get('reason', ''),
            })

        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    return compute_metrics(trades, equity, INITIAL_CAPITAL, equity_curve, symbol, 'crypto_4h')


if __name__ == '__main__':
    main()
