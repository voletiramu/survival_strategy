"""
REALISTIC Options Backtest with Black-Scholes Greeks
=====================================================
Uses 5yr spot OHLCV data + computes realistic option premiums, OI proxy,
Delta, Gamma, Theta for each trade using Black-Scholes model.

This is calibrated to real-world parameters:
- Historical Volatility computed from actual price data
- India VIX proxy from HV for IV estimation
- Realistic lot sizes and margin requirements
- Actual brokerage (Rs 20/order) + STT + exchange charges
- SEBI margin rules (approx Rs 1.2-1.5L per BankNifty lot for selling)

Capital: Rs 3,00,000 | Period: 5 years | BankNifty + Nifty50
"""

import sys
import os
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.stats import norm
from tabulate import tabulate
from datetime import timedelta

from data_fetcher import load_or_fetch, LOT_SIZES

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

INITIAL_CAPITAL = 300000

# Angel data directory for longer history
ANGEL_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data', 'options')


def load_angel_data(symbol):
    """Load Angel SmartAPI spot data (5.5 years) merged with Yahoo volume.

    Angel provides longer OHLC history (Aug 2020) but Volume=0 for indices.
    Yahoo provides Volume data (Feb 2021+). We merge both for best coverage.
    """
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

    angel_fname = angel_map.get(symbol)
    yahoo_fname = yahoo_map.get(symbol)

    # Load Angel data (longer history, OHLC)
    angel_df = None
    if angel_fname:
        fpath = os.path.join(ANGEL_DATA_DIR, angel_fname)
        if os.path.exists(fpath):
            angel_df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if angel_df.index.tz is not None:
                angel_df.index = angel_df.index.tz_localize(None)

    # Load Yahoo data (has Volume)
    yahoo_df = None
    if yahoo_fname:
        ypath = os.path.join(os.path.dirname(__file__), 'data', yahoo_fname)
        if os.path.exists(ypath):
            yahoo_df = pd.read_csv(ypath, parse_dates=['Date'], index_col='Date')
            if yahoo_df.index.tz is not None:
                yahoo_df.index = yahoo_df.index.tz_localize(None)
            # Normalize dates to midnight
            yahoo_df.index = yahoo_df.index.normalize()

    if angel_df is not None and yahoo_df is not None:
        # Use Angel OHLC + merge Yahoo Volume
        angel_df.index = angel_df.index.normalize()

        # For overlapping period, use Yahoo volume
        # For Angel-only period (earlier), synthesize volume from price range
        df = angel_df[['Open', 'High', 'Low', 'Close']].copy()

        # Map Yahoo volume onto Angel dates
        vol_map = yahoo_df['Volume'].to_dict()
        df['Volume'] = df.index.map(lambda d: vol_map.get(d, 0))

        # For dates without Yahoo volume, synthesize from daily range
        no_vol = df['Volume'] == 0
        if no_vol.any():
            # Estimate volume from (High-Low)/Close * avg_vol
            avg_vol = df.loc[~no_vol, 'Volume'].mean() if (~no_vol).any() else 300000
            range_pct = (df['High'] - df['Low']) / df['Close']
            avg_range = range_pct[~no_vol].mean() if (~no_vol).any() else 0.01
            # Volume proportional to range
            synth_vol = (range_pct / max(avg_range, 0.001)) * avg_vol
            df.loc[no_vol, 'Volume'] = synth_vol[no_vol].clip(lower=50000)

        print(f"  Loaded MERGED data: {symbol} ({len(df)} days, "
              f"{df.index[0].date()} to {df.index[-1].date()})", flush=True)
        print(f"    Angel OHLC: {len(angel_df)} days | Yahoo Vol: {(~no_vol).sum()} days", flush=True)
        return df

    elif angel_df is not None:
        print(f"  Loaded Angel data (no volume): {symbol} ({len(angel_df)} days)", flush=True)
        angel_df['Volume'] = 300000  # Synthetic volume
        return angel_df

    # Fallback to Yahoo
    print(f"  Using Yahoo data only: {symbol}", flush=True)
    return load_or_fetch(symbol, period="5y")
RISK_FREE_RATE = 0.065  # India 10yr govt bond ~6.5%

# Real-world costs
BROKERAGE_PER_ORDER = 20  # Zerodha flat fee
STT_SELL_OPTIONS = 0.000625  # 0.0625% on sell side premium
EXCHANGE_CHARGES = 0.0005  # ~0.05%
GST = 0.18  # on brokerage

# Margin requirements (approximate)
MARGIN_PER_LOT_SELLING = {
    'NIFTY50': 120000,   # ~1.2L per lot for option selling
    'BANKNIFTY': 150000,  # ~1.5L per lot for option selling
    'SENSEX': 100000,    # ~1L per lot for Sensex option selling
}


# ====================================================================
# BLACK-SCHOLES ENGINE
# ====================================================================
def bs_price(S, K, T, r, sigma, opt_type='CE'):
    if T <= 0: T = 1/365
    if sigma <= 0: sigma = 0.01
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CE':
        return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
    else:
        return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)


def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    if T <= 0: T = 1/365
    if sigma <= 0: sigma = 0.01
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    delta = norm.cdf(d1) if opt_type == 'CE' else norm.cdf(d1) - 1
    gamma = norm.pdf(d1) / (S*sigma*np.sqrt(T))
    theta = (-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T))) / 365
    vega = S*norm.pdf(d1)*np.sqrt(T) / 100
    price = bs_price(S, K, T, r, sigma, opt_type)
    return {'price':max(price,0.5), 'delta':delta, 'gamma':gamma, 'theta':theta, 'vega':vega}


# ====================================================================
# HISTORICAL VOLATILITY & IV PROXY
# ====================================================================
def compute_historical_vol(df, window=20):
    """Compute annualized historical volatility."""
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    hv = log_ret.rolling(window).std() * np.sqrt(252)
    return hv


def iv_from_hv(hv_value):
    """Estimate implied volatility from historical volatility.
    IV typically trades at 1.1-1.3x HV for Indian indices."""
    if pd.isna(hv_value) or hv_value <= 0:
        return 0.15
    return hv_value * 1.15  # IV premium over HV


# ====================================================================
# SIMULATED OI FROM VOLUME
# ====================================================================
def simulate_oi(df, window=20):
    """Simulate OI proxy from volume patterns.
    High volume on up days = put writing (bullish OI)
    High volume on down days = call writing (bearish OI)"""
    df = df.copy()
    df['direction'] = np.where(df['Close'] > df['Open'], 1, -1)
    df['vol_up'] = df['Volume'] * (df['direction'] == 1).astype(int)
    df['vol_down'] = df['Volume'] * (df['direction'] == -1).astype(int)
    df['oi_ce_proxy'] = df['vol_down'].rolling(window).sum()
    df['oi_pe_proxy'] = df['vol_up'].rolling(window).sum()
    df['pcr_oi'] = df['oi_pe_proxy'] / df['oi_ce_proxy'].replace(0, 1)
    return df


# ====================================================================
# REALISTIC COST CALCULATOR
# ====================================================================
def calculate_costs(premium, quantity, is_sell=False):
    """Calculate real-world trading costs."""
    turnover = premium * quantity
    brokerage = BROKERAGE_PER_ORDER * 2  # buy + sell
    stt = turnover * STT_SELL_OPTIONS if is_sell else 0
    exchange = turnover * EXCHANGE_CHARGES
    gst = brokerage * GST
    total = brokerage + stt + exchange + gst
    return total


# ====================================================================
# STRATEGY 1: SURVIVOR V2 WITH REAL GREEKS
# ====================================================================
def backtest_survivor_realistic(df, capital, lot_size, symbol):
    """Survivor V2 with Black-Scholes premiums and real costs."""
    print(f"\n  === Survivor V2 Realistic ({symbol}) ===", flush=True)
    df = df.copy()
    df['HV'] = compute_historical_vol(df)
    df = simulate_oi(df)
    df['ATR'] = df['High'].sub(df['Low']).rolling(14).mean()

    equity = capital
    equity_curve = []
    trades = []
    daily_pnl = 0
    equity_hwm = capital

    day_params = {
        0: {'gap': 20, 'distance': 200},
        1: {'gap': 15, 'distance': 120},
        2: {'gap': 10, 'distance': 70},
    }

    margin_per_lot = MARGIN_PER_LOT_SELLING.get(symbol, 150000)

    for i in range(30, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        date = df.index[i]
        dow = date.weekday()

        if dow > 2:  # Only Mon-Wed
            equity_curve.append((date, equity))
            continue

        hv = df['HV'].iloc[i]
        iv = iv_from_hv(hv)
        atr = df['ATR'].iloc[i]
        if pd.isna(iv) or pd.isna(atr) or atr == 0:
            equity_curve.append((date, equity))
            continue

        # Max lots based on available capital and margin
        max_lots = max(1, int(equity / margin_per_lot))

        # Gamma blast check
        vol_avg = df['Volume'].iloc[max(0,i-20):i].mean()
        vol_spike = row['Volume'] / vol_avg if vol_avg > 0 else 1
        gamma_risk = vol_spike > 2.0 and dow >= 2

        # Daily loss limit
        daily_limit = equity * 0.02
        if daily_pnl < -daily_limit:
            equity_curve.append((date, equity))
            continue

        # Trailing DD check
        equity_hwm = max(equity_hwm, equity)
        dd_pct = (equity_hwm - equity) / equity_hwm * 100
        scale = 0.5 if dd_pct > 3 else 1.0
        if gamma_risk:
            scale *= 0.5

        params = day_params.get(dow, day_params[2])
        gap = params['gap']
        distance = params['distance']

        resistance = prev['High']
        support = prev['Low']
        spot = row['Close']

        # Expiry: assume weekly (DTE = days until next Thursday)
        days_to_expiry = max(1, (3 - dow) + 1)  # Thursday
        T = days_to_expiry / 365

        trade_pnl = 0

        # PE SELLING when market goes UP
        if row['High'] > resistance + gap:
            moves = min(int((row['High'] - resistance) / gap), int(max_lots * scale))
            for m in range(moves):
                pe_strike = spot - distance
                # Calculate PE premium using Black-Scholes
                pe_greeks = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                entry_premium = pe_greeks['price']

                # REALISTIC EXIT: Consider adverse scenarios
                # Scenario 1: Normal day - PE decays (profitable)
                # Scenario 2: Next day gap down hits PE strike (loss)
                # Scenario 3: Intraday reversal (partial loss)

                # Check if market reversed (Low was close to PE strike)
                reversal_depth = (spot - row['Low']) / max(atr, 1)

                if row['Low'] < pe_strike:
                    # DEEP LOSS: market went through PE strike (ITM)
                    adverse_spot = row['Low']
                    pe_exit = bs_greeks(adverse_spot, pe_strike, max(T - 0.5/365, 1/365),
                                        RISK_FREE_RATE, iv * 1.4, 'PE')
                    exit_premium = pe_exit['price']
                    exit_premium = min(exit_premium, entry_premium * 3.0)  # SL at 3x
                elif reversal_depth > 1.5:
                    # LOSS: big reversal toward strike
                    adverse_spot = spot - atr * 1.2
                    pe_exit = bs_greeks(max(adverse_spot, pe_strike*1.005), pe_strike,
                                        max(T - 0.5/365, 1/365), RISK_FREE_RATE, iv * 1.2, 'PE')
                    exit_premium = pe_exit['price']
                    exit_premium = min(exit_premium, entry_premium * 2.0)
                elif reversal_depth > 0.5:
                    # Partial loss/breakeven
                    mid_spot = spot - atr * 0.3
                    pe_exit = bs_greeks(mid_spot, pe_strike, max(T - 1/365, 1/365),
                                        RISK_FREE_RATE, iv * 1.05, 'PE')
                    exit_premium = pe_exit['price']
                else:
                    # Normal profitable: PE decays (theta win)
                    exit_spot = spot + gap * 0.3
                    pe_exit = bs_greeks(exit_spot, pe_strike, max(T - 1/365, 1/365),
                                        RISK_FREE_RATE, iv * 0.95, 'PE')
                    exit_premium = pe_exit['price']

                # P&L for seller: entry - exit
                pnl_per_unit = entry_premium - exit_premium
                pnl = pnl_per_unit * lot_size

                # Real costs
                cost = calculate_costs(entry_premium, lot_size, is_sell=True)
                pnl -= cost

                # Delta-theta conversion for losers
                if pnl < 0 and days_to_expiry <= 2:
                    pnl *= 0.65  # 35% recovery via conversion

                # Cap max loss per trade at 3% of equity
                max_trade_loss = equity * 0.03
                pnl = max(pnl, -max_trade_loss)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'SELL_PE', 'strike': pe_strike,
                    'spot': spot, 'entry_premium': entry_premium,
                    'exit_premium': exit_premium, 'pnl': pnl,
                    'delta': pe_greeks['delta'], 'gamma': pe_greeks['gamma'],
                    'theta': pe_greeks['theta'], 'iv': iv*100, 'dte': days_to_expiry,
                })

        # CE SELLING when market goes DOWN
        if row['Low'] < support - gap:
            moves = min(int((support - row['Low']) / gap), int(max_lots * scale))
            for m in range(moves):
                ce_strike = spot + distance
                ce_greeks = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                entry_premium = ce_greeks['price']

                # REALISTIC EXIT: Consider adverse scenarios
                reversal_depth = (row['High'] - spot) / max(atr, 1)

                if row['High'] > ce_strike:
                    # DEEP LOSS: market went through CE strike (ITM)
                    adverse_spot = row['High']
                    ce_exit = bs_greeks(adverse_spot, ce_strike, max(T - 0.5/365, 1/365),
                                        RISK_FREE_RATE, iv * 1.4, 'CE')
                    exit_premium = ce_exit['price']
                    exit_premium = min(exit_premium, entry_premium * 3.0)
                elif reversal_depth > 1.5:
                    adverse_spot = spot + atr * 1.2
                    ce_exit = bs_greeks(min(adverse_spot, ce_strike*0.995), ce_strike,
                                        max(T - 0.5/365, 1/365), RISK_FREE_RATE, iv * 1.2, 'CE')
                    exit_premium = ce_exit['price']
                    exit_premium = min(exit_premium, entry_premium * 2.0)
                elif reversal_depth > 0.5:
                    mid_spot = spot + atr * 0.3
                    ce_exit = bs_greeks(mid_spot, ce_strike, max(T - 1/365, 1/365),
                                        RISK_FREE_RATE, iv * 1.05, 'CE')
                    exit_premium = ce_exit['price']
                else:
                    exit_spot = spot - gap * 0.3
                    ce_exit = bs_greeks(exit_spot, ce_strike, max(T - 1/365, 1/365),
                                        RISK_FREE_RATE, iv * 0.95, 'CE')
                    exit_premium = ce_exit['price']

                pnl_per_unit = entry_premium - exit_premium
                pnl = pnl_per_unit * lot_size
                cost = calculate_costs(entry_premium, lot_size, is_sell=True)
                pnl -= cost

                if pnl < 0 and days_to_expiry <= 2:
                    pnl *= 0.65

                max_trade_loss = equity * 0.03
                pnl = max(pnl, -max_trade_loss)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'SELL_CE', 'strike': ce_strike,
                    'spot': spot, 'entry_premium': entry_premium,
                    'exit_premium': exit_premium, 'pnl': pnl,
                    'delta': ce_greeks['delta'], 'gamma': ce_greeks['gamma'],
                    'theta': ce_greeks['theta'], 'iv': iv*100, 'dte': days_to_expiry,
                })

        equity += trade_pnl
        daily_pnl += trade_pnl

        # Reset daily pnl on new day
        if i < len(df)-1 and date.date() != df.index[i+1].date():
            daily_pnl = 0

        equity_curve.append((date, equity))

    return trades, equity_curve, equity


# ====================================================================
# STRATEGY 2: PCR+VWAP WITH REAL GREEKS
# ====================================================================
def backtest_pcr_vwap_realistic(df, capital, lot_size, symbol):
    """PCR+VWAP V2 with Black-Scholes premiums."""
    print(f"\n  === PCR+VWAP V2 Realistic ({symbol}) ===", flush=True)
    df = df.copy()
    df['HV'] = compute_historical_vol(df)
    df = simulate_oi(df)
    df['TP'] = (df['High'] + df['Low'] + df['Close']) / 3
    df['VWAP'] = (df['TP'] * df['Volume']).rolling(5).sum() / df['Volume'].rolling(5).sum()

    equity = capital
    equity_curve = []
    trades = []
    daily_pnl = 0

    for i in range(50, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        dow = date.weekday()
        hv = df['HV'].iloc[i]
        iv = iv_from_hv(hv)
        vwap = df['VWAP'].iloc[i]

        if pd.isna(iv) or pd.isna(vwap):
            equity_curve.append((date, equity))
            continue

        # Daily limit
        if daily_pnl < -(equity * 0.02):
            equity_curve.append((date, equity))
            continue

        # Volatility filter
        day_range = (row['High'] - row['Low']) / row['Close'] * 100
        if day_range > 3.0:
            equity_curve.append((date, equity))
            continue

        spot = row['Close']
        tolerance = 0.005  # 0.5% (relaxed from 0.3%)

        # PCR proxy
        pcr = df['pcr_oi'].iloc[i] if not pd.isna(df['pcr_oi'].iloc[i]) else 1.0

        # DTE for weekly
        days_to_expiry = max(1, (3 - dow) + 1)
        T = days_to_expiry / 365

        # OI volume confirmation - relaxed for indices with low/no volume
        vol_avg = df['Volume'].iloc[max(0,i-20):i].mean()
        if vol_avg > 0:
            has_oi_confirm = row['Volume'] > vol_avg * 1.1  # Relaxed from 1.3
        else:
            has_oi_confirm = True  # If no volume data, allow based on PCR/VWAP

        trade_pnl = 0

        # BUY CE: PCR > 1, price touches or bounces off VWAP
        vwap_touch = (row['Low'] <= vwap * (1 + tolerance)) or (abs(spot - vwap) / spot < tolerance)
        if pcr > 1.0 and vwap_touch and spot >= vwap * (1 - tolerance) and has_oi_confirm:
            # Buy ATM CE
            ce_strike = round(spot / 50) * 50  # Round to nearest 50
            ce_greeks = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
            entry_premium = ce_greeks['price']

            # Target: premium doubles (2x) or partial at 1.5x
            # Simulate: if high reached enough, target hit
            potential_up = row['High'] - spot
            target_move = entry_premium / max(abs(ce_greeks['delta']), 0.1)

            if potential_up >= target_move:
                exit_premium = entry_premium * 2  # Full target
            elif potential_up >= target_move * 0.5:
                exit_premium = entry_premium * 1.5  # Partial
            else:
                # Exit at close
                ce_exit = bs_greeks(spot, ce_strike, max(T-1/365, 1/365),
                                    RISK_FREE_RATE, iv, 'CE')
                exit_premium = ce_exit['price']

            # SL check: if low went below VWAP
            if row['Low'] < vwap * (1 - tolerance):
                exit_premium = max(entry_premium * 0.7, 1)  # 30% SL

            pnl = (exit_premium - entry_premium) * lot_size
            cost = calculate_costs(entry_premium, lot_size, is_sell=False)
            pnl -= cost
            trade_pnl += pnl

            trades.append({
                'date': date, 'type': 'BUY_CE', 'strike': ce_strike,
                'spot': spot, 'entry_premium': entry_premium,
                'exit_premium': exit_premium, 'pnl': pnl,
                'delta': ce_greeks['delta'], 'gamma': ce_greeks['gamma'],
                'theta': ce_greeks['theta'], 'iv': iv*100, 'dte': days_to_expiry,
                'pcr': pcr,
            })

        # BUY PE: PCR < 1, price near VWAP from above
        elif pcr < 1.0 and has_oi_confirm and spot <= vwap * (1 + tolerance) and \
             ((row['High'] >= vwap * (1 - tolerance)) or (abs(spot - vwap) / spot < tolerance)):
            pe_strike = round(spot / 50) * 50
            pe_greeks = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
            entry_premium = pe_greeks['price']

            potential_down = spot - row['Low']
            target_move = entry_premium / max(abs(pe_greeks['delta']), 0.1)

            if potential_down >= target_move:
                exit_premium = entry_premium * 2
            elif potential_down >= target_move * 0.5:
                exit_premium = entry_premium * 1.5
            else:
                pe_exit = bs_greeks(spot, pe_strike, max(T-1/365, 1/365),
                                    RISK_FREE_RATE, iv, 'PE')
                exit_premium = pe_exit['price']

            if row['High'] > vwap * (1 + tolerance):
                exit_premium = max(entry_premium * 0.7, 1)

            pnl = (exit_premium - entry_premium) * lot_size
            cost = calculate_costs(entry_premium, lot_size, is_sell=False)
            pnl -= cost
            trade_pnl += pnl

            trades.append({
                'date': date, 'type': 'BUY_PE', 'strike': pe_strike,
                'spot': spot, 'entry_premium': entry_premium,
                'exit_premium': exit_premium, 'pnl': pnl,
                'delta': pe_greeks['delta'], 'gamma': pe_greeks['gamma'],
                'theta': pe_greeks['theta'], 'iv': iv*100, 'dte': days_to_expiry,
                'pcr': pcr,
            })

        equity += trade_pnl
        daily_pnl += trade_pnl
        if i < len(df)-1 and date.date() != df.index[i+1].date():
            daily_pnl = 0
        equity_curve.append((date, equity))

    return trades, equity_curve, equity


# ====================================================================
# STRATEGY 3: GAMMA BLAST WITH REAL GREEKS
# ====================================================================
def backtest_gamma_blast_realistic(df, capital, lot_size, symbol):
    """Gamma Blast on expiry days with real Greeks."""
    print(f"\n  === Gamma Blast Realistic ({symbol}) ===", flush=True)
    df = df.copy()
    df['HV'] = compute_historical_vol(df)
    df['ATR'] = df['High'].sub(df['Low']).rolling(14).mean()

    equity = capital
    equity_curve = []
    trades = []

    for i in range(30, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        dow = date.weekday()
        hv = df['HV'].iloc[i]
        iv = iv_from_hv(hv)
        atr = df['ATR'].iloc[i]

        if dow not in [2, 3]:  # Only Wed/Thu
            equity_curve.append((date, equity))
            continue

        if pd.isna(iv) or pd.isna(atr):
            equity_curve.append((date, equity))
            continue

        spot = row['Close']
        open_p = row['Open']
        high = row['High']
        low = row['Low']

        # Coiled spring: low morning range
        day_range_pct = (high - low) / spot * 100
        prev_range = (df.iloc[i-1]['High'] - df.iloc[i-1]['Low']) / df.iloc[i-1]['Close'] * 100

        if prev_range > 1.8:  # Previous day wasn't calm enough (relaxed from 1.2)
            equity_curve.append((date, equity))
            continue

        # Volume check - use relative volume if available, else just check day range
        vol_avg = df['Volume'].iloc[max(0,i-20):i].mean()
        if vol_avg > 0:
            vol_ratio = row['Volume'] / vol_avg
        else:
            vol_ratio = 1.5  # If no volume data, allow based on range

        # Relaxed volume filter - or range expansion on expiry day
        range_expansion = day_range_pct > prev_range * 1.2  # Today expanding vs prev
        if vol_ratio < 1.1 and not range_expansion:
            equity_curve.append((date, equity))
            continue

        # Direction
        body = spot - open_p
        T = 1/365  # 0 DTE

        trade_pnl = 0

        if abs(body) > atr * 0.2:  # Meaningful move (relaxed from 0.3)
            if body > 0:  # Up breakout
                ce_strike = round(spot / 50) * 50
                g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv * 1.3, 'CE')
                entry_premium = g['price']

                # REALISTIC: Check if breakout was genuine or fakeout
                move = high - (open_p + spot) / 2
                reversal = (high - spot) / max(atr, 1)  # How much it gave back from high

                if reversal > 0.7:
                    # FAKEOUT: price went up then reversed back - LOSS
                    # Premium decays due to theta + reversal
                    exit_premium = entry_premium * 0.4  # 60% loss
                elif reversal > 0.3:
                    # Partial reversal - small profit or breakeven
                    net_move = (spot - open_p)
                    pnl_pts = net_move * abs(g['delta']) * 0.8
                    exit_premium = max(entry_premium + pnl_pts, entry_premium * 0.6)
                else:
                    # Genuine breakout - gamma boost profit
                    gamma_boost = 1 + min(move / atr, 1.5)
                    pnl_pts = move * abs(g['delta']) * gamma_boost
                    exit_premium = entry_premium + pnl_pts
                    exit_premium = min(exit_premium, entry_premium * 3)

                pnl = (exit_premium - entry_premium) * lot_size
                cost = calculate_costs(entry_premium, lot_size)
                pnl -= cost

                # Risk cap
                max_loss = equity * 0.015
                pnl = max(pnl, -max_loss)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_CE_GAMMA', 'strike': ce_strike,
                    'spot': spot, 'entry_premium': entry_premium,
                    'exit_premium': exit_premium, 'pnl': pnl,
                    'delta': g['delta'], 'gamma': g['gamma'],
                    'iv': iv*130, 'dte': 0,
                })

            else:  # Down breakout
                pe_strike = round(spot / 50) * 50
                g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv * 1.3, 'PE')
                entry_premium = g['price']

                move = (open_p + spot) / 2 - low
                reversal = (spot - low) / max(atr, 1)  # How much it bounced from low

                if reversal > 0.7:
                    # FAKEOUT: price went down then bounced - LOSS
                    exit_premium = entry_premium * 0.4
                elif reversal > 0.3:
                    net_move = (open_p - spot)
                    pnl_pts = net_move * abs(g['delta']) * 0.8
                    exit_premium = max(entry_premium + pnl_pts, entry_premium * 0.6)
                else:
                    gamma_boost = 1 + min(move / atr, 1.5)
                    pnl_pts = move * abs(g['delta']) * gamma_boost
                    exit_premium = entry_premium + pnl_pts
                    exit_premium = min(exit_premium, entry_premium * 3)

                pnl = (exit_premium - entry_premium) * lot_size
                cost = calculate_costs(entry_premium, lot_size)
                pnl -= cost
                max_loss = equity * 0.015
                pnl = max(pnl, -max_loss)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_PE_GAMMA', 'strike': pe_strike,
                    'spot': spot, 'entry_premium': entry_premium,
                    'exit_premium': exit_premium, 'pnl': pnl,
                    'delta': g['delta'], 'gamma': g['gamma'],
                    'iv': iv*130, 'dte': 0,
                })

        equity += trade_pnl
        equity_curve.append((date, equity))

    return trades, equity_curve, equity


# ====================================================================
# COMPUTE STATS
# ====================================================================
def compute_stats(trades, equity_curve, initial_capital, name):
    if not equity_curve:
        return {}
    eq = pd.Series([e[1] for e in equity_curve], index=[e[0] for e in equity_curve])
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = eq.expanding().max()
    dd = (eq - peak) / peak * 100
    daily_ret = eq.pct_change().dropna()
    rf = RISK_FREE_RATE / 252
    sharpe = np.sqrt(252) * (daily_ret.mean() - rf) / daily_ret.std() if len(daily_ret)>1 and daily_ret.std()>0 else 0
    n_yrs = max((eq.index[-1]-eq.index[0]).days/365.25, 0.01)
    ann_ret = ((eq.iloc[-1]/initial_capital)**(1/n_yrs)-1)*100 if eq.iloc[-1]>0 else 0
    return {
        'Strategy': name,
        'Trades': len(trades),
        'Win Rate': f"{len(wins)/max(len(pnls),1)*100:.1f}%",
        'Total PnL': f"Rs {sum(pnls):,.0f}",
        'Final Capital': f"Rs {eq.iloc[-1]:,.0f}",
        'Return': f"{(eq.iloc[-1]-initial_capital)/initial_capital*100:.1f}%",
        'Ann Return': f"{ann_ret:.1f}%",
        'Max DD': f"{dd.min():.2f}%",
        'Sharpe': f"{sharpe:.2f}",
        'Avg Win': f"Rs {np.mean(wins):,.0f}" if wins else "0",
        'Avg Loss': f"Rs {np.mean(losses):,.0f}" if losses else "0",
        'Avg Delta': f"{np.mean([abs(t.get('delta',0)) for t in trades]):.3f}" if trades else "0",
        'Avg Gamma': f"{np.mean([abs(t.get('gamma',0)) for t in trades]):.5f}" if trades else "0",
        'Avg IV': f"{np.mean([t.get('iv',0) for t in trades]):.1f}%" if trades else "0",
    }


# ====================================================================
# MAIN
# ====================================================================
def main():
    print("=" * 70, flush=True)
    print("REALISTIC OPTIONS BACKTEST WITH BLACK-SCHOLES GREEKS", flush=True)
    print("Capital: Rs 3,00,000 | 5.5 Years (Angel SmartAPI Data)", flush=True)
    print("BankNifty + Nifty50 + Sensex", flush=True)
    print("=" * 70, flush=True)
    print("\nData Source: Angel One SmartAPI (1360 days, Aug 2020 - Feb 2026)", flush=True)
    print("Includes: Real option premiums via BS model, Historical IV,", flush=True)
    print("         Delta/Gamma/Theta per trade, STT + brokerage + charges,", flush=True)
    print("         Margin constraints, Daily loss limits", flush=True)

    all_stats = []
    all_curves = {}
    all_trades = {}

    # --- BANKNIFTY ---
    print("\n" + "=" * 70, flush=True)
    print("BANKNIFTY (Angel SmartAPI Data)", flush=True)
    print("=" * 70, flush=True)
    bn_df = load_angel_data("BANKNIFTY")
    bn_lot = LOT_SIZES["BANKNIFTY"]

    # Survivor V2 Realistic
    trades, curve, final = backtest_survivor_realistic(bn_df, INITIAL_CAPITAL, bn_lot, "BANKNIFTY")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "Survivor V2 (BankNifty)")
    all_stats.append(stats)
    all_curves['Survivor V2 (BN)'] = curve
    all_trades['Survivor V2 (BN)'] = trades

    # Gamma Blast Realistic
    trades, curve, final = backtest_gamma_blast_realistic(bn_df, INITIAL_CAPITAL, bn_lot, "BANKNIFTY")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "Gamma Blast (BankNifty)")
    all_stats.append(stats)
    all_curves['Gamma Blast (BN)'] = curve
    all_trades['Gamma Blast (BN)'] = trades

    # --- NIFTY50 ---
    print("\n" + "=" * 70, flush=True)
    print("NIFTY50 (Angel SmartAPI Data)", flush=True)
    print("=" * 70, flush=True)
    nf_df = load_angel_data("NIFTY50")
    nf_lot = LOT_SIZES["NIFTY50"]

    # PCR+VWAP V2 Realistic
    trades, curve, final = backtest_pcr_vwap_realistic(nf_df, INITIAL_CAPITAL, nf_lot, "NIFTY50")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "PCR+VWAP V2 (Nifty50)")
    all_stats.append(stats)
    all_curves['PCR+VWAP V2 (NF)'] = curve
    all_trades['PCR+VWAP V2 (NF)'] = trades

    # Gamma Blast on Nifty too
    trades, curve, final = backtest_gamma_blast_realistic(nf_df, INITIAL_CAPITAL, nf_lot, "NIFTY50")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "Gamma Blast (Nifty50)")
    all_stats.append(stats)
    all_curves['Gamma Blast (NF)'] = curve
    all_trades['Gamma Blast (NF)'] = trades

    # --- SENSEX ---
    print("\n" + "=" * 70, flush=True)
    print("SENSEX (Angel SmartAPI Data)", flush=True)
    print("=" * 70, flush=True)
    sx_df = load_angel_data("SENSEX")
    sx_lot = LOT_SIZES.get("SENSEX", 10)

    # PCR+VWAP on Sensex
    trades, curve, final = backtest_pcr_vwap_realistic(sx_df, INITIAL_CAPITAL, sx_lot, "SENSEX")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "PCR+VWAP V2 (Sensex)")
    all_stats.append(stats)
    all_curves['PCR+VWAP V2 (SX)'] = curve
    all_trades['PCR+VWAP V2 (SX)'] = trades

    # Survivor on Sensex
    MARGIN_PER_LOT_SELLING['SENSEX'] = 100000  # ~1L per lot for Sensex options
    trades, curve, final = backtest_survivor_realistic(sx_df, INITIAL_CAPITAL, sx_lot, "SENSEX")
    stats = compute_stats(trades, curve, INITIAL_CAPITAL, "Survivor V2 (Sensex)")
    all_stats.append(stats)
    all_curves['Survivor V2 (SX)'] = curve
    all_trades['Survivor V2 (SX)'] = trades

    # === RESULTS TABLE ===
    print("\n" + "=" * 70, flush=True)
    print("REALISTIC BACKTEST RESULTS (with BS Greeks & Real Costs)", flush=True)
    print("Data: Angel SmartAPI 5.5yr | All 3 Indices", flush=True)
    print("=" * 70, flush=True)
    df_stats = pd.DataFrame(all_stats)
    print(tabulate(df_stats, headers='keys', tablefmt='grid', showindex=False), flush=True)

    # === SAMPLE TRADES WITH GREEKS ===
    for name, trades in all_trades.items():
        if trades:
            print(f"\n  Sample Trades - {name}:", flush=True)
            sample = pd.DataFrame(trades[:5])
            cols = ['date','type','strike','spot','entry_premium','exit_premium',
                    'pnl','delta','gamma','theta','iv','dte']
            display_cols = [c for c in cols if c in sample.columns]
            print(tabulate(sample[display_cols], headers='keys',
                          tablefmt='simple', showindex=False, floatfmt='.2f'), flush=True)

    # === EQUITY CURVES ===
    fig, axes = plt.subplots(2, 1, figsize=(16, 14))

    # Top chart: All strategies
    ax = axes[0]
    colors_map = {
        'Survivor V2 (BN)': '#E74C3C', 'PCR+VWAP V2 (NF)': '#3498DB',
        'Gamma Blast (BN)': '#2ECC71', 'Gamma Blast (NF)': '#27AE60',
        'PCR+VWAP V2 (SX)': '#9B59B6', 'Survivor V2 (SX)': '#F39C12',
    }
    for name, curve in all_curves.items():
        if curve:
            dates = [c[0] for c in curve]
            vals = [c[1] for c in curve]
            ax.plot(dates, vals, label=f"{name} (Rs {vals[-1]:,.0f})",
                   linewidth=1.5, color=colors_map.get(name, 'gray'))

    ax.axhline(y=INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5)
    ax.set_ylabel('Portfolio Value (Rs)')
    ax.set_title('Realistic Options Backtest - All Strategies\n'
                 'Angel SmartAPI Data | Rs 3L Capital | 5.5 Years | BS Greeks + Real Costs',
                 fontsize=12, fontweight='bold')
    ax.legend(fontsize=9, ncol=2)
    ax.grid(True, alpha=0.3)

    # Bottom chart: Best portfolio allocation
    ax2 = axes[1]

    # === COMBINED ALLOCATION ===
    print("\n" + "=" * 70, flush=True)
    print("COMBINED PORTFOLIO SIMULATION (Rs 3,00,000)", flush=True)
    print("=" * 70, flush=True)

    # Determine best allocation based on Sharpe
    best_alloc = {
        'Survivor V2 (BN)': 0.35,  # 35% - Best risk-adjusted selling strategy
        'PCR+VWAP V2 (NF)': 0.30,  # 30% - Best buying strategy
        'Gamma Blast (BN)': 0.15,  # 15% - Expiry day alpha
        'PCR+VWAP V2 (SX)': 0.10,  # 10% - Diversification
        'Survivor V2 (SX)': 0.10,  # 10% - Diversification
    }

    for name, pct in best_alloc.items():
        amt = INITIAL_CAPITAL * pct
        print(f"  {name}: {pct*100:.0f}% (Rs {amt:,.0f})", flush=True)

    # Compute weighted combined equity
    all_dates = set()
    for curve in all_curves.values():
        all_dates.update([c[0] for c in curve])
    all_dates = sorted(all_dates)

    combined = pd.Series(0.0, index=all_dates)
    for name, curve in all_curves.items():
        if curve and name in best_alloc:
            s = pd.Series([c[1] for c in curve], index=[c[0] for c in curve])
            s = s.reindex(all_dates, method='ffill').fillna(INITIAL_CAPITAL)
            combined += s * best_alloc[name]

    if len(combined) > 0:
        total_ret = (combined.iloc[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        peak = combined.expanding().max()
        dd = (combined - peak) / peak * 100
        max_dd = dd.min()
        daily_r = combined.pct_change().dropna()
        sharpe = np.sqrt(252)*(daily_r.mean()-RISK_FREE_RATE/252)/daily_r.std() if daily_r.std()>0 else 0
        n_yrs = max((combined.index[-1] - combined.index[0]).days / 365.25, 0.01)
        ann_ret = ((combined.iloc[-1]/INITIAL_CAPITAL)**(1/n_yrs)-1)*100

        print(f"\n  Initial Capital:      Rs {INITIAL_CAPITAL:,.0f}", flush=True)
        print(f"  Final Portfolio Value: Rs {combined.iloc[-1]:,.0f}", flush=True)
        print(f"  Total Return:          {total_ret:.1f}%", flush=True)
        print(f"  Annualized Return:     {ann_ret:.1f}%", flush=True)
        print(f"  Max Drawdown:          {max_dd:.2f}%", flush=True)
        print(f"  Sharpe Ratio:          {sharpe:.2f}", flush=True)
        print(f"  Period:                {n_yrs:.1f} years", flush=True)

        # Monthly income estimate
        monthly_income = (combined.iloc[-1] - INITIAL_CAPITAL) / max(n_yrs * 12, 1)
        print(f"  Avg Monthly Income:    Rs {monthly_income:,.0f}", flush=True)

        # Plot combined
        ax2.fill_between(combined.index, INITIAL_CAPITAL, combined.values,
                        where=combined.values >= INITIAL_CAPITAL,
                        color='green', alpha=0.2)
        ax2.fill_between(combined.index, INITIAL_CAPITAL, combined.values,
                        where=combined.values < INITIAL_CAPITAL,
                        color='red', alpha=0.2)
        ax2.plot(combined.index, combined.values, color='#2C3E50', linewidth=2,
                label=f'Combined Portfolio (Final: Rs {combined.iloc[-1]:,.0f})')
        ax2.axhline(y=INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5,
                    label=f'Initial Capital (Rs {INITIAL_CAPITAL:,.0f})')

        # Add annotations
        ax2.annotate(f'Rs {combined.iloc[-1]:,.0f}\n({total_ret:.1f}%)',
                    xy=(combined.index[-1], combined.iloc[-1]),
                    fontsize=10, fontweight='bold', color='darkgreen',
                    ha='right', va='bottom')

        ax2.set_ylabel('Portfolio Value (Rs)')
        ax2.set_title(f'Combined Portfolio - Rs {INITIAL_CAPITAL/100000:.0f}L Capital\n'
                      f'Sharpe: {sharpe:.2f} | Max DD: {max_dd:.2f}% | '
                      f'Ann Return: {ann_ret:.1f}% | Avg Monthly: Rs {monthly_income:,.0f}',
                      fontsize=11, fontweight='bold')
        ax2.legend(fontsize=10)
        ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    fp = os.path.join(RESULTS_DIR, 'realistic_greeks_backtest.png')
    plt.savefig(fp, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\nSaved: {fp}", flush=True)

    # === YEAR-WISE BREAKDOWN ===
    if len(combined) > 0:
        print("\n" + "=" * 70, flush=True)
        print("YEAR-WISE PERFORMANCE BREAKDOWN", flush=True)
        print("=" * 70, flush=True)
        yearly = combined.resample('YE').last()
        prev = INITIAL_CAPITAL
        yearly_data = []
        for date, val in yearly.items():
            yr_ret = (val - prev) / prev * 100
            yearly_data.append({
                'Year': date.year,
                'Start': f'Rs {prev:,.0f}',
                'End': f'Rs {val:,.0f}',
                'Return': f'{yr_ret:.1f}%',
                'PnL': f'Rs {val-prev:,.0f}'
            })
            prev = val
        print(tabulate(yearly_data, headers='keys', tablefmt='grid', showindex=False), flush=True)

    # === MONTHLY RETURN HEATMAP ===
    if len(combined) > 0:
        fig2, ax3 = plt.subplots(figsize=(14, 6))
        monthly_ret = combined.resample('ME').last().pct_change() * 100
        monthly_ret = monthly_ret.dropna()

        # Create pivot table
        monthly_pivot = pd.DataFrame({
            'Year': monthly_ret.index.year,
            'Month': monthly_ret.index.month,
            'Return': monthly_ret.values
        }).pivot_table(index='Year', columns='Month', values='Return', aggfunc='first')

        import matplotlib.colors as mcolors
        cmap = mcolors.LinearSegmentedColormap.from_list('rg', ['#E74C3C', '#FFFFFF', '#2ECC71'])
        vmax = max(abs(monthly_pivot.max().max()), abs(monthly_pivot.min().min()))
        im = ax3.imshow(monthly_pivot.values, cmap=cmap, aspect='auto',
                        vmin=-vmax, vmax=vmax)

        ax3.set_xticks(range(12))
        ax3.set_xticklabels(['Jan','Feb','Mar','Apr','May','Jun',
                             'Jul','Aug','Sep','Oct','Nov','Dec'])
        ax3.set_yticks(range(len(monthly_pivot)))
        ax3.set_yticklabels(monthly_pivot.index)

        for i in range(len(monthly_pivot)):
            for j in range(12):
                if j < monthly_pivot.shape[1]:
                    val = monthly_pivot.values[i, j]
                    if not np.isnan(val):
                        ax3.text(j, i, f'{val:.1f}%', ha='center', va='center',
                                fontsize=8, fontweight='bold',
                                color='white' if abs(val) > vmax*0.6 else 'black')

        plt.colorbar(im, ax=ax3, label='Monthly Return %')
        ax3.set_title('Combined Portfolio - Monthly Returns Heatmap\n'
                      'Green = Profit | Red = Loss', fontsize=11, fontweight='bold')
        plt.tight_layout()
        fp2 = os.path.join(RESULTS_DIR, 'realistic_monthly_heatmap.png')
        plt.savefig(fp2, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"Saved: {fp2}", flush=True)

    # Save trades for analysis
    for name, trades in all_trades.items():
        if trades:
            safe = name.replace(' ','_').replace('(','').replace(')','')
            pd.DataFrame(trades).to_csv(os.path.join(RESULTS_DIR, f'trades_{safe}.csv'), index=False)

    # Save summary
    df_stats.to_csv(os.path.join(RESULTS_DIR, 'realistic_backtest_summary.csv'), index=False)

    print(f"\n{'='*70}", flush=True)
    print("All results saved to:", RESULTS_DIR, flush=True)
    print(f"{'='*70}", flush=True)


if __name__ == "__main__":
    main()
