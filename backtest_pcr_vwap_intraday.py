#!/usr/bin/env python3
"""
PCR+VWAP Strategy Backtest — Intraday 5-Min Data (100 days from Angel SmartAPI)
================================================================================
Fetches 5-minute candle data from Angel SmartAPI and backtests PCR+VWAP strategy
with intraday VWAP, volume-based PCR, and realistic entry/exit timing.

Usage:
    python backtest_pcr_vwap_intraday.py              # Fetch + backtest
    python backtest_pcr_vwap_intraday.py --use-cache   # Use cached data only
"""
import sys
import os
import time
import argparse
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import datetime, timedelta

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ====================================================================
# CONFIGURATION
# ====================================================================
RESULTS_DIR = os.path.join(BASE_DIR, "results")
DATA_DIR = os.path.join(BASE_DIR, "data", "options")
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

INITIAL_CAPITAL = 300000
RISK_FREE_RATE = 0.065
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}

# Costs
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18
SLIPPAGE_PCT = 0.003  # 0.3% slippage

# Strategy params
PCR_LOOKBACK_BARS = 5  # 5 bars of 5-min = 25 minutes
VWAP_TOLERANCE_PCT = 0.3
TARGET_PARTIAL = 1.5
TARGET_FULL = 2.0
OI_VOL_CONFIRM = 1.3
MAX_VOLATILITY_PCT = 3.0
DAILY_LOSS_LIMIT_PCT = 2.0
MAX_TRADES_PER_DAY = 2
MIN_PREMIUM = 15  # Min Rs 15 to trade

# Angel API tokens
INDEX_TOKENS = {
    'NIFTY': ('NSE', '99926000'),
    'BANKNIFTY': ('NSE', '99926009'),
    'SENSEX': ('BSE', '99919000'),
}

# Angel credentials
ANGEL_CRED_FILE = os.path.join(os.path.dirname(BASE_DIR), 'Angel', 'ANGEL_API_KEY=your_api_key.txt')


# ====================================================================
# ANGEL SMARTAPI CONNECTION
# ====================================================================
def connect_angel():
    """Connect to Angel SmartAPI for historical data."""
    try:
        from SmartApi import SmartConnect
        import pyotp
    except ImportError:
        print("ERROR: smartapi-python or pyotp not installed.")
        print("  pip install smartapi-python pyotp")
        return None

    # Parse credentials — use first block (Historical API)
    creds = {}
    if os.path.exists(ANGEL_CRED_FILE):
        with open(ANGEL_CRED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, val = line.split('=', 1)
                    # Check if the actual value is in a comment (e.g., "your_totp_secret  # ACTUAL_KEY")
                    if '#' in val:
                        before_comment = val.split('#')[0].strip()
                        after_comment = val.split('#')[1].strip()
                        # If before comment is placeholder, use after-comment value
                        if 'your_' in before_comment.lower() or before_comment == '':
                            val = after_comment
                        else:
                            val = before_comment
                    else:
                        val = val.strip()
                    # Only set if not already set (use first block = Historical)
                    if key not in creds:
                        creds[key] = val

    api_key = creds.get('ANGEL_API_KEY', '')
    client_code = creds.get('ANGEL_CLIENT_CODE', '')
    pin = creds.get('ANGEL_PIN', '')
    totp_key = creds.get('ANGEL_TOTP_KEY', '')

    if not all([api_key, client_code, pin, totp_key]):
        print("ERROR: Missing Angel API credentials in", ANGEL_CRED_FILE)
        return None

    try:
        obj = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_key).now()
        session = obj.generateSession(client_code, pin, totp)
        if session and session.get('status'):
            print(f"  Angel API connected (client: {client_code})")
            return obj
        else:
            print(f"  Angel login failed: {session}")
            return None
    except Exception as e:
        print(f"  Angel connection error: {e}")
        return None


def fetch_5min_data(angel_obj, symbol, days_back=100):
    """Fetch 5-minute candle data from Angel SmartAPI.
    Max ~8000 candles per request (~26 trading days of 75 candles each).
    """
    exchange, token = INDEX_TOKENS[symbol]
    all_candles = []

    # Fetch in chunks of 25 trading days (~35 calendar days)
    end_date = datetime.now()
    chunk_calendar_days = 35
    total_chunks = (days_back // 25) + 1

    print(f"  Fetching {symbol} 5-min data ({days_back} days in {total_chunks} chunks)...")

    for chunk in range(total_chunks):
        chunk_end = end_date - timedelta(days=chunk * chunk_calendar_days)
        chunk_start = chunk_end - timedelta(days=chunk_calendar_days)

        from_str = chunk_start.strftime('%Y-%m-%d 09:15')
        to_str = chunk_end.strftime('%Y-%m-%d 15:30')

        try:
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": "FIVE_MINUTE",
                "fromdate": from_str,
                "todate": to_str,
            }
            data = angel_obj.getCandleData(params)
            if data and data.get('data'):
                candles = data['data']
                all_candles.extend(candles)
                print(f"    Chunk {chunk + 1}/{total_chunks}: {len(candles)} candles "
                      f"({chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')})")
            else:
                print(f"    Chunk {chunk + 1}: No data")
            time.sleep(0.5)  # Rate limit: 3 req/sec for historical
        except Exception as e:
            print(f"    Chunk {chunk + 1} error: {e}")
            time.sleep(1)

    if not all_candles:
        return None

    df = pd.DataFrame(all_candles, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['DateTime'] = pd.to_datetime(df['DateTime'])
    df = df.drop_duplicates(subset=['DateTime']).sort_values('DateTime').reset_index(drop=True)
    df.set_index('DateTime', inplace=True)

    # Remove timezone if present
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    print(f"  Total: {len(df)} candles ({df.index[0]} to {df.index[-1]})")
    return df


def load_or_fetch_data(symbol, angel_obj=None, use_cache=False):
    """Load cached 5-min data or fetch from Angel."""
    cache_file = os.path.join(DATA_DIR, f'{symbol}_spot_five_min_100d.csv')

    if os.path.exists(cache_file):
        mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        age_days = (datetime.now() - mod_time).days
        df = pd.read_csv(cache_file, parse_dates=['DateTime'], index_col='DateTime')
        print(f"  Loaded {len(df)} candles from cache ({age_days}d old)")
        if use_cache or age_days < 7:
            return df
        print(f"  Cache is {age_days} days old, refreshing...")

    if angel_obj is None:
        if os.path.exists(cache_file):
            df = pd.read_csv(cache_file, parse_dates=['DateTime'], index_col='DateTime')
            print(f"  Using cached data ({len(df)} candles) — no Angel connection")
            return df
        print(f"  No cached data and no Angel connection for {symbol}")
        return None

    df = fetch_5min_data(angel_obj, symbol, days_back=100)
    if df is not None:
        df.to_csv(cache_file)
        print(f"  Saved {len(df)} candles to {cache_file}")
    return df


# ====================================================================
# BLACK-SCHOLES + COSTS
# ====================================================================
def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    S = max(S, 0.01)
    K = max(K, 0.01)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == 'CE':
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = -norm.cdf(-d1)
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    theta = -(S * sigma * norm.pdf(d1)) / (2 * np.sqrt(T))
    theta /= 365
    vega = S * np.sqrt(T) * norm.pdf(d1) / 100
    return {'price': max(price, 0.05), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega}


def calc_total_costs(premium, qty, is_sell=False):
    turnover = premium * qty
    brokerage = BROKERAGE * 2
    stt = turnover * STT_SELL if is_sell else 0
    exchange = turnover * EXCHANGE_CHARGES
    gst = brokerage * GST_RATE
    slippage = turnover * SLIPPAGE_PCT
    return brokerage + stt + exchange + gst + slippage


# ====================================================================
# INTRADAY PCR+VWAP BACKTEST
# ====================================================================
def backtest_intraday(df, symbol, lot_size, debug_mode=False):
    """Run PCR+VWAP on 5-minute candle data."""
    capital = INITIAL_CAPITAL
    trades = []
    equity_curve = [capital]
    equity_dates = [df.index[0]]

    strike_interval = STRIKE_INTERVALS.get(symbol, 50)

    # Group candles by date
    df = df.copy()

    # Index spot data from Angel has Volume=0. Use range as volume proxy.
    using_proxy_volume = False
    if df['Volume'].sum() == 0:
        print(f"  NOTE: Volume=0 for {symbol} (index spot). Using range-based proxy.")
        # Proxy volume = bar range * 1000 (normalized by typical price)
        df['Volume'] = ((df['High'] - df['Low']) / ((df['High'] + df['Low']) / 2) * 100000).clip(lower=1).astype(int)
        using_proxy_volume = True

    df['date'] = df.index.date

    daily_groups = df.groupby('date')
    trading_days = sorted(daily_groups.groups.keys())

    # Pre-compute 14-day daily ATR for volatility filter
    daily_ohlc = df.groupby('date').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    })

    equity_hwm = capital
    skipped_days = 0
    signal_days = 0

    for day_idx, day in enumerate(trading_days):
        if day_idx < 5:  # Need lookback
            continue

        day_candles = daily_groups.get_group(day).copy()
        if len(day_candles) < 20:  # Need enough candles (at least ~2 hours)
            continue

        # Daily volatility filter
        day_high = day_candles['High'].max()
        day_low = day_candles['Low'].min()
        day_close = day_candles['Close'].iloc[-1]
        day_range_pct = (day_high - day_low) / day_close * 100

        # Use previous 14 days ATR for context
        prev_days = trading_days[max(0, day_idx - 14):day_idx]
        if len(prev_days) >= 5:
            prev_ranges = []
            for pd_day in prev_days:
                pd_candles = daily_groups.get_group(pd_day)
                prev_ranges.append(pd_candles['High'].max() - pd_candles['Low'].min())
            atr = np.mean(prev_ranges)
        else:
            atr = day_high - day_low

        # Historical volatility from previous days
        if day_idx >= 20:
            prev_closes = [daily_ohlc.loc[d, 'Close'] for d in trading_days[day_idx - 20:day_idx]
                           if d in daily_ohlc.index]
            if len(prev_closes) >= 10:
                log_rets = np.diff(np.log(prev_closes))
                hv = np.std(log_rets) * np.sqrt(252)
                iv = max(min(hv * 1.15, 0.60), 0.08)
            else:
                iv = 0.15
        else:
            iv = 0.15

        # Days to expiry (approximate: Thursday expiry for NIFTY weekly)
        from datetime import date as dt_date
        weekday = dt_date(*[int(x) for x in str(day).split('-')]).weekday() if isinstance(day, str) else day.weekday()
        dte = max(1, (3 - weekday) % 7 + 1) if weekday <= 3 else 1

        daily_pnl = 0.0
        daily_loss_limit = capital * DAILY_LOSS_LIMIT_PCT / 100
        daily_trades = 0
        bar_count = 0

        # Drawdown recovery
        equity_hwm = max(equity_hwm, capital)
        current_dd = (equity_hwm - capital) / equity_hwm * 100
        position_scale = 0.5 if current_dd > 5.0 else 1.0

        # Compute running VWAP across all candles of the day
        cum_tp_vol = 0.0
        cum_vol = 0.0

        # Track 20-bar average volume for OI confirmation
        vol_history = []

        for bar_idx in range(len(day_candles)):
            bar = day_candles.iloc[bar_idx]
            bar_time = day_candles.index[bar_idx]

            # Skip first ~30 min (high noise) and last 15 min (close)
            bar_hour = bar_time.hour
            bar_min = bar_time.minute
            trade_time = bar_hour * 60 + bar_min  # minutes since midnight

            if trade_time < 595:  # Before 9:55 AM — accumulate VWAP only
                tp = (bar['High'] + bar['Low'] + bar['Close']) / 3
                cum_tp_vol += tp * bar['Volume']
                cum_vol += bar['Volume']
                vol_history.append(bar['Volume'])
                continue
            if trade_time >= 915:  # After 3:15 PM — too close to close
                continue

            # Update running intraday VWAP
            tp = (bar['High'] + bar['Low'] + bar['Close']) / 3
            cum_tp_vol += tp * bar['Volume']
            cum_vol += bar['Volume']
            vol_history.append(bar['Volume'])

            if cum_vol <= 0:
                continue

            vwap = cum_tp_vol / cum_vol

            # Check trade limits
            if daily_trades >= MAX_TRADES_PER_DAY:
                continue
            if daily_pnl < -daily_loss_limit:
                continue

            # PCR from recent bars (up-volume vs down-volume)
            start_idx = max(0, bar_idx - PCR_LOOKBACK_BARS)
            lookback = day_candles.iloc[start_idx:bar_idx + 1]
            if len(lookback) < 3:
                continue

            up_bars = lookback[lookback['Close'] > lookback['Open']]
            down_bars = lookback[lookback['Close'] <= lookback['Open']]
            up_vol = up_bars['Volume'].sum() if len(up_bars) > 0 else 1
            down_vol = down_bars['Volume'].sum() if len(down_bars) > 0 else 1
            pcr = up_vol / max(down_vol, 1)

            # OI confirmation: volume surge check
            # Note: With proxy volume (index spot has no real volume), use relaxed threshold
            avg_vol = np.mean(vol_history[-20:]) if len(vol_history) >= 20 else np.mean(vol_history) if vol_history else 1
            if using_proxy_volume:
                # With proxy volume, check if range is above median (not 1.3x)
                has_oi = bar['Volume'] > avg_vol * 0.8  # 80% of avg = mild activity filter
            else:
                has_oi = bar['Volume'] > avg_vol * OI_VOL_CONFIRM if avg_vol > 0 else True

            # Adaptive tolerance
            atr_pct = (atr / bar['Close'] * 100) if bar['Close'] > 0 else 0.3
            tolerance = max(VWAP_TOLERANCE_PCT, min(atr_pct * 0.5, 1.0))

            close = bar['Close']
            high = bar['High']
            low = bar['Low']

            # Debug: log every 500th bar to understand conditions
            if debug_mode and bar_count % 500 == 0:
                vwap_dist = abs(close - vwap) / vwap * 100
                print(f"    [DBG] {bar_time} PCR={pcr:.3f} VWAP={vwap:.1f} Close={close:.1f} "
                      f"Dist={vwap_dist:.3f}% Tol={tolerance:.3f}% OI={has_oi} Vol={bar['Volume']}")
            bar_count += 1

            T = max(dte, 1) / 365

            # === BUY CE: PCR bullish (up-volume dominant) + VWAP bounce ===
            if pcr > 1.0 and has_oi:
                ce_vwap_cond = low <= vwap * (1 + tolerance / 100) and close > vwap
                if debug_mode and day_idx < 10 and bar_count <= 5:
                    print(f"    [CE?] {bar_time} PCR={pcr:.2f} low={low:.1f} vwap_upper={vwap*(1+tolerance/100):.1f} "
                          f"close={close:.1f} vwap={vwap:.1f} cond={ce_vwap_cond}")
                if ce_vwap_cond:
                    entry_spot = vwap
                    sl_distance = vwap * tolerance / 100
                    sl_spot = vwap - sl_distance

                    ce_strike = round(close / strike_interval) * strike_interval
                    g = bs_greeks(entry_spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                    entry_premium = g['price']
                    delta = abs(g['delta'])

                    if entry_premium < MIN_PREMIUM:
                        continue

                    # Simulate forward exit: look at remaining bars today
                    exit_premium = entry_premium
                    exit_reason = 'EOD'
                    remaining = day_candles.iloc[bar_idx + 1:]
                    target_premium = entry_premium * TARGET_FULL
                    sl_premium = entry_premium * 0.4  # 60% loss SL

                    for _, future_bar in remaining.iterrows():
                        spot_move_up = future_bar['High'] - entry_spot
                        spot_move_down = future_bar['Low'] - entry_spot
                        spot_move_close = future_bar['Close'] - entry_spot

                        potential_high_premium = entry_premium + spot_move_up * delta
                        potential_low_premium = entry_premium + spot_move_down * delta
                        current_exit = entry_premium + spot_move_close * delta

                        if potential_high_premium >= target_premium:
                            exit_premium = target_premium
                            exit_reason = 'TARGET'
                            break
                        elif potential_low_premium <= sl_premium:
                            exit_premium = sl_premium
                            exit_reason = 'SL'
                            break
                        else:
                            exit_premium = current_exit

                    # Apply slippage and costs
                    entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                    exit_with_slip = max(exit_premium * (1 - SLIPPAGE_PCT), 0.05)

                    gross_pnl = (exit_with_slip - entry_with_slip) * lot_size * position_scale
                    costs = calc_total_costs(entry_premium, lot_size)
                    net_pnl = gross_pnl - costs

                    trades.append({
                        'date': str(day), 'time': bar_time.strftime('%H:%M'),
                        'symbol': symbol, 'type': 'BUY_CE',
                        'strike': ce_strike, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2),
                        'spot_entry': round(entry_spot, 2), 'vwap': round(vwap, 2),
                        'pcr': round(pcr, 3), 'tolerance': round(tolerance, 3),
                        'has_oi': has_oi, 'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                        'net_pnl': round(net_pnl, 2),
                    })
                    capital += net_pnl
                    daily_pnl += net_pnl
                    daily_trades += 1
                    signal_days += 1
                    continue  # One trade per direction signal per bar

            # === BUY PE: PCR bearish (down-volume dominant) + VWAP rejection ===
            elif pcr < 1.0 and has_oi:
                pe_vwap_cond = high >= vwap * (1 - tolerance / 100) and close < vwap
                if debug_mode and day_idx < 10 and bar_count <= 5:
                    print(f"    [PE?] {bar_time} PCR={pcr:.2f} high={high:.1f} vwap_lower={vwap*(1-tolerance/100):.1f} "
                          f"close={close:.1f} vwap={vwap:.1f} cond={pe_vwap_cond}")
                if pe_vwap_cond:
                    entry_spot = vwap
                    sl_distance = vwap * tolerance / 100
                    sl_spot = vwap + sl_distance

                    pe_strike = round(close / strike_interval) * strike_interval
                    g = bs_greeks(entry_spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                    entry_premium = g['price']
                    delta = abs(g['delta'])

                    if entry_premium < MIN_PREMIUM:
                        continue

                    exit_premium = entry_premium
                    exit_reason = 'EOD'
                    remaining = day_candles.iloc[bar_idx + 1:]
                    target_premium = entry_premium * TARGET_FULL
                    sl_premium = entry_premium * 0.4

                    for _, future_bar in remaining.iterrows():
                        spot_move_down = entry_spot - future_bar['Low']
                        spot_move_up = entry_spot - future_bar['High']
                        spot_move_close = entry_spot - future_bar['Close']

                        potential_high_premium = entry_premium + spot_move_down * delta
                        potential_low_premium = entry_premium + spot_move_up * delta
                        current_exit = entry_premium + spot_move_close * delta

                        if potential_high_premium >= target_premium:
                            exit_premium = target_premium
                            exit_reason = 'TARGET'
                            break
                        elif potential_low_premium <= sl_premium:
                            exit_premium = sl_premium
                            exit_reason = 'SL'
                            break
                        else:
                            exit_premium = current_exit

                    entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                    exit_with_slip = max(exit_premium * (1 - SLIPPAGE_PCT), 0.05)

                    gross_pnl = (exit_with_slip - entry_with_slip) * lot_size * position_scale
                    costs = calc_total_costs(entry_premium, lot_size)
                    net_pnl = gross_pnl - costs

                    trades.append({
                        'date': str(day), 'time': bar_time.strftime('%H:%M'),
                        'symbol': symbol, 'type': 'BUY_PE',
                        'strike': pe_strike, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2),
                        'spot_entry': round(entry_spot, 2), 'vwap': round(vwap, 2),
                        'pcr': round(pcr, 3), 'tolerance': round(tolerance, 3),
                        'has_oi': has_oi, 'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                        'net_pnl': round(net_pnl, 2),
                    })
                    capital += net_pnl
                    daily_pnl += net_pnl
                    daily_trades += 1
                    signal_days += 1
                    continue

        equity_curve.append(capital)
        equity_dates.append(pd.Timestamp(day))

    print(f"  {symbol}: {len(trades)} trades across {len(trading_days)} days "
          f"({signal_days} signal days, {len(trading_days) - signal_days - 5} no-signal days)")

    return trades, capital, equity_curve, equity_dates


# ====================================================================
# STATISTICS
# ====================================================================
def compute_stats(trades, equity_curve, equity_dates, capital, name, symbol):
    if not trades:
        return {
            'Strategy': name, 'Symbol': symbol, 'Trades': 0, 'Win Rate': '0.0%',
            'Total PnL': 'Rs 0', 'Final Capital': f'Rs {capital:,.0f}',
            'Return': '0.0%', 'Max DD': '0.0%', 'Sharpe': 0.0,
            'Profit Factor': 0.0,
        }

    pnls = [t['net_pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    equity = pd.Series(equity_curve, index=equity_dates)
    peak = equity.expanding().max()
    drawdown = (equity - peak) / peak * 100
    max_dd = drawdown.min()

    daily_ret = equity.pct_change().dropna()
    sharpe = 0.0
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        rf_daily = 0.06 / 252
        sharpe = np.sqrt(252) * (daily_ret.mean() - rf_daily) / daily_ret.std()

    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')

    # Exit reason breakdown
    target_exits = sum(1 for t in trades if t.get('exit_reason') == 'TARGET')
    sl_exits = sum(1 for t in trades if t.get('exit_reason') == 'SL')
    eod_exits = sum(1 for t in trades if t.get('exit_reason') == 'EOD')

    return {
        'Strategy': name, 'Symbol': symbol,
        'Trades': len(trades),
        'CE Trades': sum(1 for t in trades if 'CE' in t['type']),
        'PE Trades': sum(1 for t in trades if 'PE' in t['type']),
        'Win Rate': f'{len(wins) / len(pnls) * 100:.1f}%',
        'Total PnL': f'Rs {sum(pnls):,.0f}',
        'Gross PnL': f'Rs {sum(t["gross_pnl"] for t in trades):,.0f}',
        'Total Costs': f'Rs {sum(t["costs"] for t in trades):,.0f}',
        'Final Capital': f'Rs {capital:,.0f}',
        'Return': f'{(capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:.1f}%',
        'Max DD': f'{max_dd:.1f}%',
        'Sharpe': round(sharpe, 2),
        'Profit Factor': round(pf, 2),
        'Avg Win': f'Rs {np.mean(wins):,.0f}' if wins else 'Rs 0',
        'Avg Loss': f'Rs {np.mean(losses):,.0f}' if losses else 'Rs 0',
        'Target Exits': target_exits,
        'SL Exits': sl_exits,
        'EOD Exits': eod_exits,
    }


# ====================================================================
# MAIN
# ====================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--use-cache', action='store_true', help='Use cached data only')
    args = parser.parse_args()

    print("=" * 80)
    print("PCR+VWAP STRATEGY BACKTEST — INTRADAY 5-MIN DATA (Angel SmartAPI)")
    print(f"Capital: Rs {INITIAL_CAPITAL:,} | Slippage: {SLIPPAGE_PCT * 100}% | Max {MAX_TRADES_PER_DAY} trades/day")
    print("=" * 80)

    # Connect to Angel API (if not using cache)
    angel_obj = None
    if not args.use_cache:
        print("\nConnecting to Angel SmartAPI...")
        angel_obj = connect_angel()
        if angel_obj is None:
            print("  Falling back to cached data...")

    all_results = []
    all_trades = []

    for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        print(f"\n{'=' * 60}")
        print(f"  {symbol}")
        print(f"{'=' * 60}")

        df = load_or_fetch_data(symbol, angel_obj, args.use_cache)
        if df is None:
            print(f"  No data for {symbol} — skipping")
            continue

        lot_size = LOT_SIZES.get(symbol, 50)
        print(f"  Candles: {len(df)} | Lot size: {lot_size}")
        print(f"  Period: {df.index[0]} to {df.index[-1]}")

        trades, final_cap, eq_curve, eq_dates = backtest_intraday(df, symbol, lot_size)
        stats = compute_stats(trades, eq_curve, eq_dates, final_cap,
                              'PCR+VWAP Intraday', symbol)
        all_results.append(stats)

        for t in trades:
            t['version'] = 'Intraday'
        all_trades.extend(trades)

        # Print stats
        print(f"\n  {'Metric':<20} {'Value':>18}")
        print(f"  {'-' * 40}")
        for key in ['Trades', 'CE Trades', 'PE Trades', 'Win Rate', 'Total PnL',
                     'Gross PnL', 'Total Costs', 'Return', 'Max DD', 'Sharpe',
                     'Profit Factor', 'Avg Win', 'Avg Loss',
                     'Target Exits', 'SL Exits', 'EOD Exits']:
            print(f"  {key:<20} {str(stats.get(key, 'N/A')):>18}")

    # Summary
    print(f"\n\n{'=' * 80}")
    print("INTRADAY BACKTEST SUMMARY")
    print(f"{'=' * 80}")
    print(f"\n{'Symbol':<12} {'Trades':>7} {'WR':>7} {'Net PnL':>12} {'Return':>9} {'Sharpe':>8} {'Max DD':>8} {'PF':>6} {'Tgt':>5} {'SL':>5} {'EOD':>5}")
    print("-" * 95)
    for r in all_results:
        print(f"{r['Symbol']:<12} {r['Trades']:>7} {r['Win Rate']:>7} {r['Total PnL']:>12} "
              f"{r['Return']:>9} {r['Sharpe']:>8} {r['Max DD']:>8} {r['Profit Factor']:>6} "
              f"{r.get('Target Exits', 0):>5} {r.get('SL Exits', 0):>5} {r.get('EOD Exits', 0):>5}")

    # Save results
    results_csv = os.path.join(RESULTS_DIR, 'pcr_vwap_backtest_intraday.csv')
    pd.DataFrame(all_results).to_csv(results_csv, index=False)
    print(f"\nResults saved to: {results_csv}")

    if all_trades:
        trades_csv = os.path.join(RESULTS_DIR, 'pcr_vwap_trades_intraday.csv')
        pd.DataFrame(all_trades).to_csv(trades_csv, index=False)
        print(f"Trades saved to: {trades_csv}")

    # Compare with daily backtest if available
    daily_csv = os.path.join(RESULTS_DIR, 'pcr_vwap_backtest_daily.csv')
    if os.path.exists(daily_csv):
        print(f"\n\n{'=' * 80}")
        print("COMPARISON: DAILY vs INTRADAY BACKTEST")
        print(f"{'=' * 80}")
        daily_df = pd.read_csv(daily_csv)
        print("\nDAILY (5.5yr — look-ahead bias present):")
        for _, row in daily_df.iterrows():
            print(f"  {row['Strategy']:<20} {row['Symbol']:<12} Trades={row['Trades']:>5} "
                  f"WR={row['Win Rate']:>7} PnL={row['Total PnL']:>14} Sharpe={row['Sharpe']:>6}")

        print("\nINTRADAY (100d — forward-looking exit, more realistic):")
        for r in all_results:
            print(f"  {'PCR+VWAP Intraday':<20} {r['Symbol']:<12} Trades={r['Trades']:>5} "
                  f"WR={r['Win Rate']:>7} PnL={r['Total PnL']:>14} Sharpe={r['Sharpe']:>6}")

        print("\nNOTE: Daily backtest has look-ahead bias (knows full day OHLC at entry).")
        print("      Intraday backtest is more realistic (forward-looking exits only).")


if __name__ == '__main__':
    main()
