#!/usr/bin/env python3
"""
PCR+VWAP Strategy Backtest — Daily Data (5.5 years)
=====================================================
Runs both V1 and V2 of PCR+VWAP strategy on NIFTY, BANKNIFTY, SENSEX
using Angel SmartAPI 2000-day OHLC data + Yahoo volume data.

Usage:
    python backtest_pcr_vwap.py
"""
import sys
import os
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)
ANGEL_DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
YAHOO_DATA_DIR = os.path.join(BASE_DIR, 'data')

INITIAL_CAPITAL = 300000
RISK_FREE_RATE = 0.065
LOT_SIZES = {'NIFTY50': 65, 'BANKNIFTY': 30, 'SENSEX': 20}

# Costs
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18
SLIPPAGE_PCT = 0.003  # 0.3% slippage on premium


# ====================================================================
# DATA LOADING (from run_final_comparison.py pattern)
# ====================================================================
def load_merged_data(symbol):
    """Load Angel OHLC + Yahoo Volume merged data."""
    angel_map = {
        'NIFTY50': 'NIFTY_spot_one_day_2000d.csv',
        'BANKNIFTY': 'BANKNIFTY_spot_one_day_2000d.csv',
        'SENSEX': 'SENSEX_spot_one_day_2000d.csv',
    }
    yahoo_map = {
        'NIFTY50': 'NIFTY50_1d_5y.csv',
        'BANKNIFTY': 'BANKNIFTY_1d_5y.csv',
        'SENSEX': 'SENSEX_1d_5y.csv',
    }

    angel_df = None
    fpath = os.path.join(ANGEL_DATA_DIR, angel_map.get(symbol, ''))
    if os.path.exists(fpath):
        angel_df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
        if angel_df.index.tz is not None:
            angel_df.index = angel_df.index.tz_localize(None)
        angel_df.index = angel_df.index.normalize()

    yahoo_df = None
    ypath = os.path.join(YAHOO_DATA_DIR, yahoo_map.get(symbol, ''))
    if os.path.exists(ypath):
        yahoo_df = pd.read_csv(ypath, parse_dates=['Date'], index_col='Date')
        if yahoo_df.index.tz is not None:
            yahoo_df.index = yahoo_df.index.tz_localize(None)
        yahoo_df.index = yahoo_df.index.normalize()

    if angel_df is not None:
        df = angel_df[['Open', 'High', 'Low', 'Close']].copy()
        if yahoo_df is not None:
            vol_map = yahoo_df['Volume'].to_dict()
            df['Volume'] = df.index.map(lambda d: vol_map.get(d, 0))
            no_vol = df['Volume'] == 0
            if no_vol.any():
                avg_vol = df.loc[~no_vol, 'Volume'].mean() if (~no_vol).any() else 300000
                range_pct = (df['High'] - df['Low']) / df['Close']
                avg_range = range_pct[~no_vol].mean() if (~no_vol).any() else 0.01
                synth_vol = (range_pct / max(avg_range, 0.001)) * avg_vol
                df.loc[no_vol, 'Volume'] = synth_vol[no_vol].clip(lower=50000)
        else:
            df['Volume'] = 300000
        return df
    elif yahoo_df is not None:
        return yahoo_df[['Open', 'High', 'Low', 'Close', 'Volume']]
    return None


# ====================================================================
# BLACK-SCHOLES + COSTS
# ====================================================================
def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option pricing with Greeks."""
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
    theta = -(S * sigma * norm.pdf(d1)) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2 if opt_type == 'CE' else -d2)
    theta /= 365
    vega = S * np.sqrt(T) * norm.pdf(d1) / 100
    return {'price': max(price, 0.05), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega}


def calc_total_costs(premium, qty, is_sell=False, slippage_pct=SLIPPAGE_PCT):
    """Calculate all trading costs including slippage."""
    turnover = premium * qty
    brokerage = BROKERAGE * 2
    stt = turnover * STT_SELL if is_sell else 0
    exchange = turnover * EXCHANGE_CHARGES
    gst = brokerage * GST_RATE
    slippage = turnover * slippage_pct
    return brokerage + stt + exchange + gst + slippage


# ====================================================================
# PCR+VWAP V1 STRATEGY (Original — CA Nitin Muraka)
# ====================================================================
def run_pcr_vwap_v1(df, symbol, lot_size):
    """PCR+VWAP V1: Simple volume-based PCR + 5-day rolling VWAP."""
    capital = INITIAL_CAPITAL
    trades = []
    equity_curve = [capital]
    equity_dates = [df.index[0]]

    pcr_lookback = 5
    vwap_tolerance = 0.3
    target_mult = 2.0

    # Indicators
    df = df.copy()
    df['TypicalPrice'] = (df['High'] + df['Low'] + df['Close']) / 3
    df['CumVolPrice'] = (df['TypicalPrice'] * df['Volume']).rolling(5).sum()
    df['CumVol'] = df['Volume'].rolling(5).sum()
    df['VWAP'] = df['CumVolPrice'] / df['CumVol']

    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    df['HV'] = log_ret.rolling(20).std() * np.sqrt(252)

    for i in range(max(50, pcr_lookback + 1), len(df)):
        row = df.iloc[i]
        date = df.index[i]
        vwap = df.iloc[i]['VWAP']
        hv = df.iloc[i]['HV']

        if pd.isna(vwap) or pd.isna(hv):
            equity_curve.append(capital)
            equity_dates.append(date)
            continue

        iv = max(min(hv * 1.15, 0.60), 0.08)

        # PCR proxy from volume
        lookback = df.iloc[i - pcr_lookback:i]
        up_days = lookback[lookback['Close'] > lookback['Open']]
        down_days = lookback[lookback['Close'] <= lookback['Open']]
        up_vol = up_days['Volume'].sum() if len(up_days) > 0 else 1
        down_vol = down_days['Volume'].sum() if len(down_days) > 0 else 1
        pcr = up_vol / max(down_vol, 1)

        close = row['Close']
        high = row['High']
        low = row['Low']

        strike_interval = 50 if 'NIFTY' in symbol and 'BANK' not in symbol else 100
        T = 3 / 365  # Assume ~3 DTE on average

        trade_taken = False

        # BUY CE: PCR > 1.0, price dips to VWAP and bounces
        if pcr > 1.0 and not trade_taken:
            if low <= vwap * 1.003 and close > vwap:
                entry_spot = vwap
                sl_spot = vwap * (1 - vwap_tolerance / 100)
                target_spot = entry_spot + (entry_spot - sl_spot) * target_mult

                ce_strike = round(close / strike_interval) * strike_interval
                g = bs_greeks(entry_spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                entry_premium = g['price']
                delta = abs(g['delta'])

                if entry_premium > 5:
                    spot_move = min(high, target_spot) - entry_spot if high >= target_spot else (
                        sl_spot - entry_spot if close < sl_spot else close - entry_spot
                    )
                    premium_change = spot_move * delta
                    exit_premium = entry_premium + premium_change

                    # Apply slippage
                    entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                    exit_with_slip = exit_premium * (1 - SLIPPAGE_PCT)

                    gross_pnl = (exit_with_slip - entry_with_slip) * lot_size
                    costs = calc_total_costs(entry_premium, lot_size, is_sell=False)
                    net_pnl = gross_pnl - costs

                    trades.append({
                        'date': date, 'symbol': symbol, 'type': 'BUY_CE',
                        'strike': ce_strike, 'entry': entry_premium, 'exit': exit_premium,
                        'spot_entry': entry_spot, 'pcr': pcr, 'vwap': vwap,
                        'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                        'net_pnl': round(net_pnl, 2),
                    })
                    capital += net_pnl
                    trade_taken = True

        # BUY PE: PCR < 1.0, price rises to VWAP and rejects
        elif pcr < 1.0 and not trade_taken:
            if high >= vwap * 0.997 and close < vwap:
                entry_spot = vwap
                sl_spot = vwap * (1 + vwap_tolerance / 100)
                target_spot = entry_spot - (sl_spot - entry_spot) * target_mult

                pe_strike = round(close / strike_interval) * strike_interval
                g = bs_greeks(entry_spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                entry_premium = g['price']
                delta = abs(g['delta'])

                if entry_premium > 5:
                    spot_move = entry_spot - max(low, target_spot) if low <= target_spot else (
                        entry_spot - sl_spot if close > sl_spot else entry_spot - close
                    )
                    premium_change = spot_move * delta
                    exit_premium = entry_premium + premium_change

                    entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                    exit_with_slip = exit_premium * (1 - SLIPPAGE_PCT)

                    gross_pnl = (exit_with_slip - entry_with_slip) * lot_size
                    costs = calc_total_costs(entry_premium, lot_size, is_sell=False)
                    net_pnl = gross_pnl - costs

                    trades.append({
                        'date': date, 'symbol': symbol, 'type': 'BUY_PE',
                        'strike': pe_strike, 'entry': entry_premium, 'exit': exit_premium,
                        'spot_entry': entry_spot, 'pcr': pcr, 'vwap': vwap,
                        'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                        'net_pnl': round(net_pnl, 2),
                    })
                    capital += net_pnl
                    trade_taken = True

        equity_curve.append(capital)
        equity_dates.append(date)

    return trades, capital, equity_curve, equity_dates


# ====================================================================
# PCR+VWAP V2 STRATEGY (Enhanced — OI Confirmation + Drawdown Controls)
# ====================================================================
def run_pcr_vwap_v2(df, symbol, lot_size):
    """PCR+VWAP V2: OI volume confirmation, ATR-adaptive tolerance, partial profit booking."""
    capital = INITIAL_CAPITAL
    trades = []
    equity_curve = [capital]
    equity_dates = [df.index[0]]

    pcr_lookback = 5
    vwap_base_tol = 0.3
    atr_tol_factor = 0.5
    target_partial = 1.5
    target_full = 2.0
    oi_vol_confirm = 1.3
    max_volatility_pct = 3.0
    daily_loss_limit_pct = 2.0
    dd_recovery_threshold = 5.0
    dd_recovery_scale = 0.5

    df = df.copy()
    df['TypicalPrice'] = (df['High'] + df['Low'] + df['Close']) / 3
    df['CumVolPrice'] = (df['TypicalPrice'] * df['Volume']).rolling(5).sum()
    df['CumVol'] = df['Volume'].rolling(5).sum()
    df['VWAP'] = df['CumVolPrice'] / df['CumVol']
    df['TR'] = np.maximum(
        df['High'] - df['Low'],
        np.maximum(abs(df['High'] - df['Close'].shift(1)),
                   abs(df['Low'] - df['Close'].shift(1)))
    )
    df['ATR'] = df['TR'].rolling(14).mean()

    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    df['HV'] = log_ret.rolling(20).std() * np.sqrt(252)

    equity_hwm = capital
    daily_pnl = 0.0

    for i in range(max(50, pcr_lookback + 1), len(df)):
        row = df.iloc[i]
        date = df.index[i]
        vwap = df.iloc[i]['VWAP']
        atr = df.iloc[i]['ATR']
        hv = df.iloc[i]['HV']

        if pd.isna(vwap) or pd.isna(atr) or atr == 0 or pd.isna(hv):
            equity_curve.append(capital)
            equity_dates.append(date)
            continue

        iv = max(min(hv * 1.15, 0.60), 0.08)

        # Volatility filter
        day_range_pct = (row['High'] - row['Low']) / row['Close'] * 100
        if day_range_pct > max_volatility_pct:
            equity_curve.append(capital)
            equity_dates.append(date)
            continue

        # Daily loss limit
        daily_loss_limit = capital * daily_loss_limit_pct / 100
        if daily_pnl < -daily_loss_limit:
            equity_curve.append(capital)
            equity_dates.append(date)
            next_date = df.index[min(i + 1, len(df) - 1)]
            if date.date() != next_date.date():
                daily_pnl = 0.0
            continue

        # Drawdown recovery
        equity_hwm = max(equity_hwm, capital)
        current_dd = (equity_hwm - capital) / equity_hwm * 100
        position_scale = dd_recovery_scale if current_dd > dd_recovery_threshold else 1.0

        # Adaptive tolerance
        atr_pct = (atr / row['Close'] * 100) if row['Close'] > 0 else 0.3
        tolerance = max(vwap_base_tol, min(atr_pct * atr_tol_factor, 1.0))

        # PCR
        lookback = df.iloc[i - pcr_lookback:i]
        up_days = lookback[lookback['Close'] > lookback['Open']]
        down_days = lookback[lookback['Close'] <= lookback['Open']]
        up_vol = up_days['Volume'].sum() if len(up_days) > 0 else 1
        down_vol = down_days['Volume'].sum() if len(down_days) > 0 else 1
        pcr = up_vol / max(down_vol, 1)

        # OI confirmation (volume surge)
        has_oi = True
        if i >= 20:
            avg_vol = df['Volume'].iloc[max(0, i - 20):i].mean()
            if avg_vol > 0:
                has_oi = df.iloc[i]['Volume'] > avg_vol * oi_vol_confirm

        close = row['Close']
        high = row['High']
        low = row['Low']

        strike_interval = 50 if 'NIFTY' in symbol and 'BANK' not in symbol else 100
        T = 3 / 365

        trade_taken = False

        # BUY CE
        if pcr > 1.0 and not trade_taken:
            if low <= vwap * (1 + tolerance / 100) and close > vwap:
                if has_oi:
                    entry_spot = vwap
                    sl_distance = vwap * tolerance / 100
                    sl_spot = vwap - sl_distance
                    target_partial_spot = entry_spot + sl_distance * target_partial
                    target_full_spot = entry_spot + sl_distance * target_full

                    ce_strike = round(close / strike_interval) * strike_interval
                    g = bs_greeks(entry_spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                    entry_premium = g['price']
                    delta = abs(g['delta'])

                    if entry_premium > 5:
                        # Determine exit spot
                        if high >= target_full_spot:
                            exit_spot = (target_partial_spot + target_full_spot) / 2
                        elif high >= target_partial_spot:
                            exit_spot = (target_partial_spot + close) / 2
                        elif close < sl_spot:
                            exit_spot = sl_spot
                        else:
                            exit_spot = close

                        spot_move = exit_spot - entry_spot
                        premium_change = spot_move * delta
                        exit_premium = entry_premium + premium_change

                        entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                        exit_with_slip = exit_premium * (1 - SLIPPAGE_PCT)

                        gross_pnl = (exit_with_slip - entry_with_slip) * lot_size * position_scale
                        costs = calc_total_costs(entry_premium, lot_size, is_sell=False)
                        net_pnl = gross_pnl - costs

                        trades.append({
                            'date': date, 'symbol': symbol, 'type': 'BUY_CE',
                            'strike': ce_strike, 'entry': round(entry_premium, 2),
                            'exit': round(exit_premium, 2),
                            'spot_entry': round(entry_spot, 2), 'pcr': round(pcr, 3),
                            'vwap': round(vwap, 2), 'tolerance': round(tolerance, 3),
                            'has_oi': has_oi, 'position_scale': position_scale,
                            'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                            'net_pnl': round(net_pnl, 2),
                        })
                        capital += net_pnl
                        daily_pnl += net_pnl
                        trade_taken = True

        # BUY PE
        elif pcr < 1.0 and not trade_taken:
            if high >= vwap * (1 - tolerance / 100) and close < vwap:
                if has_oi:
                    entry_spot = vwap
                    sl_distance = vwap * tolerance / 100
                    sl_spot = vwap + sl_distance
                    target_partial_spot = entry_spot - sl_distance * target_partial
                    target_full_spot = entry_spot - sl_distance * target_full

                    pe_strike = round(close / strike_interval) * strike_interval
                    g = bs_greeks(entry_spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                    entry_premium = g['price']
                    delta = abs(g['delta'])

                    if entry_premium > 5:
                        if low <= target_full_spot:
                            exit_spot = (target_partial_spot + target_full_spot) / 2
                        elif low <= target_partial_spot:
                            exit_spot = (target_partial_spot + close) / 2
                        elif close > sl_spot:
                            exit_spot = sl_spot
                        else:
                            exit_spot = close

                        spot_move = entry_spot - exit_spot
                        premium_change = spot_move * delta
                        exit_premium = entry_premium + premium_change

                        entry_with_slip = entry_premium * (1 + SLIPPAGE_PCT)
                        exit_with_slip = exit_premium * (1 - SLIPPAGE_PCT)

                        gross_pnl = (exit_with_slip - entry_with_slip) * lot_size * position_scale
                        costs = calc_total_costs(entry_premium, lot_size, is_sell=False)
                        net_pnl = gross_pnl - costs

                        trades.append({
                            'date': date, 'symbol': symbol, 'type': 'BUY_PE',
                            'strike': pe_strike, 'entry': round(entry_premium, 2),
                            'exit': round(exit_premium, 2),
                            'spot_entry': round(entry_spot, 2), 'pcr': round(pcr, 3),
                            'vwap': round(vwap, 2), 'tolerance': round(tolerance, 3),
                            'has_oi': has_oi, 'position_scale': position_scale,
                            'gross_pnl': round(gross_pnl, 2), 'costs': round(costs, 2),
                            'net_pnl': round(net_pnl, 2),
                        })
                        capital += net_pnl
                        daily_pnl += net_pnl
                        trade_taken = True

        # Reset daily PnL
        next_date = df.index[min(i + 1, len(df) - 1)]
        if date.date() != next_date.date():
            daily_pnl = 0.0

        equity_curve.append(capital)
        equity_dates.append(date)

    return trades, capital, equity_curve, equity_dates


# ====================================================================
# STATISTICS COMPUTATION
# ====================================================================
def compute_stats(trades, equity_curve, equity_dates, capital, name, symbol):
    """Compute comprehensive backtest statistics."""
    if not trades:
        return {
            'Strategy': name, 'Symbol': symbol, 'Trades': 0, 'Win Rate': '0.0%',
            'Total PnL': 'Rs 0', 'Final Capital': f'Rs {capital:,.0f}',
            'Return': '0.0%', 'Max DD': '0.0%', 'Sharpe': 0.0,
            'Profit Factor': 0.0, 'Avg Win': 'Rs 0', 'Avg Loss': 'Rs 0',
            'CE Trades': 0, 'PE Trades': 0,
        }

    pnls = [t['net_pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    ce_trades = [t for t in trades if 'CE' in t['type']]
    pe_trades = [t for t in trades if 'PE' in t['type']]

    equity = pd.Series(equity_curve, index=equity_dates)
    peak = equity.expanding().max()
    drawdown = (equity - peak) / peak * 100
    max_dd = drawdown.min()

    daily_ret = equity.pct_change().dropna()
    sharpe = 0.0
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        rf_daily = 0.06 / 252
        excess = daily_ret - rf_daily
        sharpe = np.sqrt(252) * excess.mean() / excess.std()

    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')

    return {
        'Strategy': name,
        'Symbol': symbol,
        'Trades': len(trades),
        'CE Trades': len(ce_trades),
        'PE Trades': len(pe_trades),
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
        'Largest Win': f'Rs {max(wins):,.0f}' if wins else 'Rs 0',
        'Largest Loss': f'Rs {min(losses):,.0f}' if losses else 'Rs 0',
    }


# ====================================================================
# MAIN
# ====================================================================
def main():
    print("=" * 80)
    print("PCR+VWAP STRATEGY BACKTEST — DAILY DATA (5.5 YEARS)")
    print(f"Capital: Rs {INITIAL_CAPITAL:,} | Slippage: {SLIPPAGE_PCT*100}% | Data: Angel+Yahoo")
    print("=" * 80)

    all_results = []
    all_trades = []

    for symbol in ['NIFTY50', 'BANKNIFTY', 'SENSEX']:
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")

        df = load_merged_data(symbol)
        if df is None:
            print(f"  No data for {symbol}")
            continue

        print(f"  Data: {len(df)} days ({df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')})")
        lot_size = LOT_SIZES.get(symbol, 50)
        print(f"  Lot size: {lot_size}")

        # V1
        print(f"\n  Running V1 (Original)...")
        v1_trades, v1_cap, v1_eq, v1_dates = run_pcr_vwap_v1(df, symbol, lot_size)
        v1_stats = compute_stats(v1_trades, v1_eq, v1_dates, v1_cap, 'PCR+VWAP V1', symbol)
        all_results.append(v1_stats)
        for t in v1_trades:
            t['version'] = 'V1'
        all_trades.extend(v1_trades)

        # V2
        print(f"  Running V2 (Enhanced OI+Drawdown)...")
        v2_trades, v2_cap, v2_eq, v2_dates = run_pcr_vwap_v2(df, symbol, lot_size)
        v2_stats = compute_stats(v2_trades, v2_eq, v2_dates, v2_cap, 'PCR+VWAP V2', symbol)
        all_results.append(v2_stats)
        for t in v2_trades:
            t['version'] = 'V2'
        all_trades.extend(v2_trades)

        # Print comparison
        print(f"\n  {'Metric':<20} {'V1':>18} {'V2':>18}")
        print(f"  {'-'*56}")
        for key in ['Trades', 'CE Trades', 'PE Trades', 'Win Rate', 'Total PnL',
                     'Gross PnL', 'Total Costs', 'Return', 'Max DD', 'Sharpe',
                     'Profit Factor', 'Avg Win', 'Avg Loss', 'Largest Win', 'Largest Loss']:
            v1_val = v1_stats.get(key, 'N/A')
            v2_val = v2_stats.get(key, 'N/A')
            print(f"  {key:<20} {str(v1_val):>18} {str(v2_val):>18}")

    # Summary table
    print(f"\n\n{'='*80}")
    print("SUMMARY — ALL SYMBOLS")
    print(f"{'='*80}")
    print(f"\n{'Strategy':<20} {'Symbol':<12} {'Trades':>7} {'Win Rate':>10} {'Net PnL':>14} {'Return':>9} {'Sharpe':>8} {'Max DD':>8} {'PF':>6}")
    print("-" * 100)
    for r in all_results:
        print(f"{r['Strategy']:<20} {r['Symbol']:<12} {r['Trades']:>7} {r['Win Rate']:>10} "
              f"{r['Total PnL']:>14} {r['Return']:>9} {r['Sharpe']:>8} {r['Max DD']:>8} {r['Profit Factor']:>6}")

    # Save results
    results_csv = os.path.join(RESULTS_DIR, 'pcr_vwap_backtest_daily.csv')
    pd.DataFrame(all_results).to_csv(results_csv, index=False)
    print(f"\nResults saved to: {results_csv}")

    # Save all trades
    trades_csv = os.path.join(RESULTS_DIR, 'pcr_vwap_trades_daily.csv')
    if all_trades:
        pd.DataFrame(all_trades).to_csv(trades_csv, index=False)
        print(f"Trades saved to: {trades_csv}")

    # PCR distribution analysis
    print(f"\n{'='*80}")
    print("PCR VALUE DISTRIBUTION (shows why proxy PCR threshold matters)")
    print(f"{'='*80}")
    for symbol in ['NIFTY50', 'BANKNIFTY', 'SENSEX']:
        symbol_trades = [t for t in all_trades if t['symbol'] == symbol and t['version'] == 'V2']
        if symbol_trades:
            pcrs = [t['pcr'] for t in symbol_trades]
            print(f"\n  {symbol}: {len(symbol_trades)} trades")
            print(f"    PCR range: {min(pcrs):.3f} — {max(pcrs):.3f}")
            print(f"    PCR mean:  {np.mean(pcrs):.3f}")
            print(f"    PCR > 1.1: {sum(1 for p in pcrs if p > 1.1)} trades")
            print(f"    PCR < 0.9: {sum(1 for p in pcrs if p < 0.9)} trades")
            bullish = [t for t in symbol_trades if 'CE' in t['type']]
            bearish = [t for t in symbol_trades if 'PE' in t['type']]
            if bullish:
                print(f"    CE avg PCR: {np.mean([t['pcr'] for t in bullish]):.3f}, avg PnL: Rs {np.mean([t['net_pnl'] for t in bullish]):,.0f}")
            if bearish:
                print(f"    PE avg PCR: {np.mean([t['pcr'] for t in bearish]):.3f}, avg PnL: Rs {np.mean([t['net_pnl'] for t in bearish]):,.0f}")


if __name__ == '__main__':
    main()
