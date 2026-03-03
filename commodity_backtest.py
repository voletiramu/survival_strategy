"""
COMMODITY OPTIONS STRATEGY BACKTEST
====================================
Adapted from equity index strategies for MCX commodity options
Key differences from equity:
1. Uses Black-76 model (options on futures, not spot)
2. Different trading hours (9:00 AM - 11:30 PM)
3. Different expiry cycles (monthly, not weekly)
4. Higher volatility for commodities like Crude Oil, Natural Gas
5. Different lot sizes and margin requirements
6. Commodities have global macro drivers (USD, geopolitics, supply)

Commodities: Gold, Gold Mini, Silver, Silver Mini, Crude Oil, Natural Gas, Copper
Strategies adapted: CPR, Gamma Blast, Ghost Zone (top 3 from equity)
"""

import sys
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from scipy.stats import norm
from tabulate import tabulate
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS_DIR, exist_ok=True)
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data', 'options')

INITIAL_CAPITAL = 300000
RISK_FREE_RATE = 0.065

# MCX Commodity specifications
COMMODITIES = {
    'GOLD': {
        'lot_size': 1,       # 1 kg (but price per 10g, so multiplier = 100)
        'multiplier': 100,   # 1 kg = 100 x 10g units
        'tick_size': 1,
        'margin': 100000,    # ~Rs 1L margin for gold futures/options sell
        'strike_interval': 500,
        'file': 'GOLD_spot_one_day_2000d.csv',
        'vol_adj': 1.0,      # Moderate vol
        'description': 'Gold Standard (1 kg)',
    },
    'GOLDM': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 10,    # 100g = 10 x 10g units → Rs 10 PnL per Re 1 premium move
        'tick_size': 1,
        'margin': 15000,     # Lower margin for mini
        'strike_interval': 100,
        'file': 'GOLDM_spot_one_day_2000d.csv',
        'vol_adj': 1.0,
        'description': 'Gold Mini (100g)',
    },
    'SILVER': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 30,    # 30 kg per lot → Rs 30 PnL per Re 1 premium move
        'tick_size': 1,
        'margin': 80000,
        'strike_interval': 500,
        'file': 'SILVER_spot_one_day_2000d.csv',
        'vol_adj': 1.15,     # Silver more volatile
        'description': 'Silver Standard (30 kg)',
    },
    'SILVERM': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 5,     # 5 kg per lot → Rs 5 PnL per Re 1 premium move
        'tick_size': 1,
        'margin': 15000,
        'strike_interval': 500,
        'file': 'SILVERM_spot_one_day_2000d.csv',
        'vol_adj': 1.15,
        'description': 'Silver Mini (5 kg)',
    },
    'CRUDEOIL': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 100,   # 100 bbl per lot → Rs 100 PnL per Re 1 premium move
        'tick_size': 1,
        'margin': 60000,
        'strike_interval': 50,
        'file': 'CRUDEOIL_spot_one_day_2000d.csv',
        'vol_adj': 1.4,      # Crude is very volatile
        'description': 'Crude Oil (100 bbl)',
    },
    'NATURALGAS': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 1250,  # 1250 mmBtu per lot → Rs 1250 PnL per Re 1 premium move
        'tick_size': 0.1,
        'margin': 70000,
        'strike_interval': 5,
        'file': 'NATURALGAS_spot_one_day_2000d.csv',
        'vol_adj': 1.8,      # NatGas is extremely volatile
        'description': 'Natural Gas (1250 mmBtu)',
    },
    'COPPER': {
        'lot_size': 1,       # 1 lot (trading 1 lot at a time)
        'multiplier': 2500,  # 2500 kg per lot → Rs 2500 PnL per Re 1 premium move
        'tick_size': 0.05,
        'margin': 60000,
        'strike_interval': 5,
        'file': 'COPPER_spot_one_day_2000d.csv',
        'vol_adj': 1.1,      # Copper moderate vol
        'description': 'Copper (2500 kg)',
    },
}

# Trading costs for MCX
MCX_BROKERAGE = 20        # Per order
MCX_CTT = 0.0001          # CTT 0.01% on sell (commodity)
MCX_EXCHANGE = 0.00026    # Exchange charges
MCX_GST = 0.18
MCX_STAMP = 0.00003


# ====================================================================
# BLACK-76 MODEL (for commodity options on futures)
# ====================================================================
def black76_greeks(F, K, T, r, sigma, opt_type='CE'):
    """
    Black-76 model for options on futures.
    F = futures price (forward price)
    K = strike price
    T = time to expiry (years)
    r = risk-free rate
    sigma = implied volatility
    """
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    F = max(F, 0.01)
    K = max(K, 0.01)

    d1 = (np.log(F/K) + 0.5*sigma**2*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)

    discount = np.exp(-r * T)

    if opt_type == 'CE':
        price = discount * (F * norm.cdf(d1) - K * norm.cdf(d2))
        delta = discount * norm.cdf(d1)
    else:
        price = discount * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        delta = -discount * norm.cdf(-d1)

    gamma = discount * norm.pdf(d1) / (F * sigma * np.sqrt(T))
    theta = -(F * sigma * norm.pdf(d1) * discount) / (2 * np.sqrt(T))
    theta /= 365
    vega = F * np.sqrt(T) * norm.pdf(d1) * discount / 100

    return {'price': max(price, 0.01), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega}


def compute_hv(df, window=20):
    """Historical volatility (annualized)."""
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    return log_ret.rolling(window).std() * np.sqrt(252)


def calc_mcx_costs(premium, qty, multiplier, is_sell=False):
    """MCX trading costs."""
    turnover = premium * qty * multiplier
    brokerage = MCX_BROKERAGE * 2
    ctt = turnover * MCX_CTT if is_sell else 0
    exchange = turnover * MCX_EXCHANGE
    gst = brokerage * MCX_GST
    stamp = turnover * MCX_STAMP
    return brokerage + ctt + exchange + gst + stamp


# ====================================================================
# DATA LOADING
# ====================================================================
def load_commodity_data(commodity):
    """Load commodity historical data."""
    spec = COMMODITIES[commodity]
    fpath = os.path.join(DATA_DIR, spec['file'])
    if not os.path.exists(fpath):
        return None
    df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    return df


def compute_stats(trades, equity_curve, capital, name):
    """Compute comprehensive stats."""
    if not trades:
        return {'Strategy': name, 'Trades': 0, 'Win Rate': '0%', 'Total PnL': 'Rs 0',
                'Final Capital': f'Rs {capital:,.0f}', 'Return': '0.0%',
                'Ann Return': '0.0%', 'Max DD': '0.0%', 'Sharpe': 0,
                'Sortino': 0, 'Profit Factor': 0,
                'Avg Win': '0', 'Avg Loss': '0'}

    wins = [t['pnl'] for t in trades if t['pnl'] > 0]
    losses = [t['pnl'] for t in trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in trades)
    final = capital + total_pnl

    vals = [c[1] for c in equity_curve]
    if len(vals) < 2:
        return {'Strategy': name, 'Trades': len(trades), 'Win Rate': '0%',
                'Total PnL': f'Rs {total_pnl:,.0f}', 'Final Capital': f'Rs {final:,.0f}',
                'Return': f'{total_pnl/capital*100:.1f}%', 'Ann Return': '0.0%',
                'Max DD': '0.0%', 'Sharpe': 0, 'Sortino': 0,
                'Profit Factor': 0, 'Avg Win': '0', 'Avg Loss': '0'}

    series = pd.Series(vals)
    peak = series.expanding().max()
    dd = ((series - peak) / peak * 100)
    max_dd = dd.min()

    daily_ret = series.pct_change().dropna()
    n_yrs = max((equity_curve[-1][0] - equity_curve[0][0]).days / 365.25, 0.01)
    if final > 0:
        ann_ret = ((final/capital)**(1/n_yrs) - 1) * 100
    else:
        ann_ret = -100.0

    sharpe = 0
    if daily_ret.std() > 0:
        sharpe = np.sqrt(252) * (daily_ret.mean() - RISK_FREE_RATE/252) / daily_ret.std()

    sortino = 0
    downside = daily_ret[daily_ret < 0]
    if len(downside) > 0 and downside.std() > 0:
        sortino = np.sqrt(252) * (daily_ret.mean() - RISK_FREE_RATE/252) / downside.std()

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 1
    pf = gross_profit / max(gross_loss, 1)

    return {
        'Strategy': name,
        'Trades': len(trades),
        'Win Rate': f'{len(wins)/len(trades)*100:.1f}%',
        'Total PnL': f'Rs {total_pnl:,.0f}',
        'Final Capital': f'Rs {final:,.0f}',
        'Return': f'{total_pnl/capital*100:.1f}%',
        'Ann Return': f'{ann_ret:.1f}%',
        'Max DD': f'{max_dd:.2f}%',
        'Sharpe': round(sharpe, 2),
        'Sortino': round(sortino, 2),
        'Profit Factor': round(pf, 2),
        'Avg Win': f'Rs {np.mean(wins):,.0f}' if wins else '0',
        'Avg Loss': f'Rs {np.mean(losses):,.0f}' if losses else '0',
    }


# ====================================================================
# STRATEGY 1: CPR FOR COMMODITIES
# Adapted: Monthly expiry (not weekly), wider CPR thresholds
# ====================================================================
def backtest_cpr_commodity(df, capital, commodity):
    """CPR strategy adapted for commodities."""
    spec = COMMODITIES[commodity]
    lot = spec['lot_size']
    multiplier = spec['multiplier']
    margin = spec['margin']
    strike_int = spec['strike_interval']
    vol_adj = spec['vol_adj']

    print(f"\n  === CPR ({commodity}) ===", flush=True)
    hv = compute_hv(df)
    equity = capital
    trades = []
    equity_curve = [(df.index[0], equity)]

    for i in range(21, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        dow = date.weekday()

        if dow > 4:
            equity_curve.append((date, equity))
            continue

        prev = df.iloc[i-1]
        spot = row['Close']
        atr = (df['High'].iloc[i-14:i] - df['Low'].iloc[i-14:i]).mean()
        iv = hv.iloc[i] if not pd.isna(hv.iloc[i]) else 0.20
        iv = max(min(iv * 1.15 * vol_adj, 0.80), 0.08)

        # CPR Calculation
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / spot * 100

        # Camarilla levels
        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        # Commodities have monthly expiry - average ~15 days to expiry
        days_to_expiry = max(5, 15 - (date.day % 28))
        T = days_to_expiry / 365
        trade_pnl = 0

        # NARROW CPR (< 0.4% for commodities - slightly wider than equity 0.3%)
        if cpr_width < 0.4:
            if spot > tc:
                # Bullish breakout
                ce_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                entry_premium = g['price']

                if entry_premium > 1:
                    if row['High'] > cam_r3:
                        exit_premium = entry_premium * 1.8
                    elif row['High'] > tc + atr * 0.3:
                        exit_premium = entry_premium * 1.4
                    elif spot < pivot:
                        exit_premium = entry_premium * 0.4
                    else:
                        exit_premium = entry_premium * 0.85

                    pnl = (exit_premium - entry_premium) * lot * multiplier
                    pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                    pnl = max(pnl, -equity * 0.01)
                    trade_pnl += pnl
                    trades.append({
                        'date': date, 'type': 'BUY_CE_CPR', 'strike': ce_strike,
                        'spot': spot, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                        'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                        'dte': days_to_expiry, 'cpr_width': round(cpr_width, 3)
                    })

            elif spot < bc:
                # Bearish breakout
                pe_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                entry_premium = g['price']

                if entry_premium > 1:
                    if row['Low'] < cam_s3:
                        exit_premium = entry_premium * 1.8
                    elif row['Low'] < bc - atr * 0.3:
                        exit_premium = entry_premium * 1.4
                    elif spot > pivot:
                        exit_premium = entry_premium * 0.4
                    else:
                        exit_premium = entry_premium * 0.85

                    pnl = (exit_premium - entry_premium) * lot * multiplier
                    pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                    pnl = max(pnl, -equity * 0.01)
                    trade_pnl += pnl
                    trades.append({
                        'date': date, 'type': 'BUY_PE_CPR', 'strike': pe_strike,
                        'spot': spot, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                        'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                        'dte': days_to_expiry, 'cpr_width': round(cpr_width, 3)
                    })

        # WIDE CPR (> 0.6% for commodities) = MEAN REVERSION (sell)
        elif cpr_width > 0.6:
            margin_ok = equity >= margin
            if row['High'] >= cam_r3 * 0.998 and spot < cam_r4 and margin_ok:
                ce_strike = round(cam_r4 / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                entry_premium = g['price']

                if entry_premium > 2:
                    if row['High'] > cam_r4:
                        exit_g = black76_greeks(row['High'], ce_strike, max(T-1/365, 1e-4),
                                               RISK_FREE_RATE, iv*1.2, 'CE')
                        exit_premium = min(exit_g['price'], entry_premium * 2)
                    elif spot < cam_r3:
                        exit_g = black76_greeks(pivot, ce_strike, max(T-1/365, 1e-4),
                                               RISK_FREE_RATE, iv*0.9, 'CE')
                        exit_premium = exit_g['price']
                    else:
                        exit_premium = entry_premium * 0.8

                    pnl = (entry_premium - exit_premium) * lot * multiplier
                    pnl -= calc_mcx_costs(entry_premium, lot, multiplier, is_sell=True)
                    pnl = max(pnl, -equity * 0.01)
                    trade_pnl += pnl
                    trades.append({
                        'date': date, 'type': 'SELL_CE_CPR', 'strike': ce_strike,
                        'spot': spot, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                        'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                        'dte': days_to_expiry, 'cpr_width': round(cpr_width, 3)
                    })

            if row['Low'] <= cam_s3 * 1.002 and spot > cam_s4 and margin_ok:
                pe_strike = round(cam_s4 / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                entry_premium = g['price']

                if entry_premium > 2:
                    if row['Low'] < cam_s4:
                        exit_g = black76_greeks(row['Low'], pe_strike, max(T-1/365, 1e-4),
                                               RISK_FREE_RATE, iv*1.2, 'PE')
                        exit_premium = min(exit_g['price'], entry_premium * 2)
                    elif spot > cam_s3:
                        exit_g = black76_greeks(pivot, pe_strike, max(T-1/365, 1e-4),
                                               RISK_FREE_RATE, iv*0.9, 'PE')
                        exit_premium = exit_g['price']
                    else:
                        exit_premium = entry_premium * 0.8

                    pnl = (entry_premium - exit_premium) * lot * multiplier
                    pnl -= calc_mcx_costs(entry_premium, lot, multiplier, is_sell=True)
                    pnl = max(pnl, -equity * 0.01)
                    trade_pnl += pnl
                    trades.append({
                        'date': date, 'type': 'SELL_PE_CPR', 'strike': pe_strike,
                        'spot': spot, 'entry_premium': round(entry_premium, 2),
                        'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                        'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                        'dte': days_to_expiry, 'cpr_width': round(cpr_width, 3)
                    })

        trade_pnl = max(trade_pnl, -equity * 0.02)
        equity += trade_pnl
        equity_curve.append((date, equity))

    print(f"    Trades: {len(trades)} | PnL: Rs {equity-capital:,.0f}", flush=True)
    return trades, equity_curve, equity


# ====================================================================
# STRATEGY 2: GAMMA BLAST FOR COMMODITIES
# Adapted: Use high-vol days instead of specific expiry days
# ====================================================================
def backtest_gamma_blast_commodity(df, capital, commodity):
    """Gamma Blast adapted for commodities - breakout on high momentum days."""
    spec = COMMODITIES[commodity]
    lot = spec['lot_size']
    multiplier = spec['multiplier']
    strike_int = spec['strike_interval']
    vol_adj = spec['vol_adj']

    print(f"\n  === Gamma Blast ({commodity}) ===", flush=True)
    hv = compute_hv(df)
    equity = capital
    trades = []
    equity_curve = [(df.index[0], equity)]

    for i in range(21, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        dow = date.weekday()

        if dow > 4:
            equity_curve.append((date, equity))
            continue

        spot = row['Close']
        open_p = row['Open']
        high = row['High']
        low = row['Low']
        atr = (df['High'].iloc[i-14:i] - df['Low'].iloc[i-14:i]).mean()
        iv = hv.iloc[i] if not pd.isna(hv.iloc[i]) else 0.20
        iv = max(min(iv * 1.15 * vol_adj, 0.80), 0.08)

        # For commodities: look for coiled spring + breakout
        # (similar to expiry day gamma, but any day with coiled-then-explode pattern)
        prev_range = (df.iloc[i-1]['High'] - df.iloc[i-1]['Low']) / max(atr, 1)
        day_range = (high - low) / max(atr, 1)

        # Need coiled spring (prev day < 1.2x ATR) AND expansion today (> 0.5x)
        if prev_range > 1.2 or day_range < 0.5:
            equity_curve.append((date, equity))
            continue

        body = spot - open_p
        days_to_expiry = max(5, 15 - (date.day % 28))
        T = max(3, days_to_expiry) / 365  # Min 3 DTE for commodity

        trade_pnl = 0

        if abs(body) > atr * 0.2:  # Directional move
            if body > 0:  # Up breakout
                ce_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv * 1.2, 'CE')
                entry_premium = g['price']

                if entry_premium < 1:
                    equity_curve.append((date, equity))
                    continue

                reversal = (high - spot) / max(high - open_p, 1) if high > open_p else 0

                if reversal > 0.6:
                    exit_premium = entry_premium * max(0.3, 1 - reversal)
                elif reversal > 0.3:
                    net_move = spot - open_p
                    exit_premium = entry_premium * (1 + net_move / max(atr, 1) * 0.5)
                    exit_premium = max(exit_premium, entry_premium * 0.5)
                else:
                    move = high - (open_p + spot) / 2
                    gamma_boost = 1 + min(move / max(atr, 1), 1.5)
                    exit_premium = entry_premium * (1 + abs(g['delta']) * gamma_boost * 0.8)
                    exit_premium = min(exit_premium, entry_premium * 3)

                pnl = (exit_premium - entry_premium) * lot * multiplier
                pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                pnl = max(pnl, -equity * 0.015)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_CE_GAMMA', 'strike': ce_strike,
                    'spot': spot, 'entry_premium': round(entry_premium, 2),
                    'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                    'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                    'dte': days_to_expiry
                })

            else:  # Down breakout
                pe_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv * 1.2, 'PE')
                entry_premium = g['price']

                if entry_premium < 1:
                    equity_curve.append((date, equity))
                    continue

                reversal = (spot - low) / max(open_p - low, 1) if open_p > low else 0

                if reversal > 0.6:
                    exit_premium = entry_premium * max(0.3, 1 - reversal)
                elif reversal > 0.3:
                    net_move = open_p - spot
                    exit_premium = entry_premium * (1 + net_move / max(atr, 1) * 0.5)
                    exit_premium = max(exit_premium, entry_premium * 0.5)
                else:
                    move = (open_p + spot) / 2 - low
                    gamma_boost = 1 + min(move / max(atr, 1), 1.5)
                    exit_premium = entry_premium * (1 + abs(g['delta']) * gamma_boost * 0.8)
                    exit_premium = min(exit_premium, entry_premium * 3)

                pnl = (exit_premium - entry_premium) * lot * multiplier
                pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                pnl = max(pnl, -equity * 0.015)

                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_PE_GAMMA', 'strike': pe_strike,
                    'spot': spot, 'entry_premium': round(entry_premium, 2),
                    'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                    'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                    'dte': days_to_expiry
                })

        trade_pnl = max(trade_pnl, -equity * 0.015)
        equity += trade_pnl
        equity_curve.append((date, equity))

    print(f"    Trades: {len(trades)} | PnL: Rs {equity-capital:,.0f}", flush=True)
    return trades, equity_curve, equity


# ====================================================================
# STRATEGY 3: GHOST ZONE FOR COMMODITIES
# Adapted: Demand/supply zones from commodity price action
# ====================================================================
def backtest_ghost_zone_commodity(df, capital, commodity):
    """Ghost Zone adapted for commodities."""
    spec = COMMODITIES[commodity]
    lot = spec['lot_size']
    multiplier = spec['multiplier']
    strike_int = spec['strike_interval']
    vol_adj = spec['vol_adj']

    print(f"\n  === Ghost Zone ({commodity}) ===", flush=True)
    hv = compute_hv(df)
    equity = capital
    trades = []
    equity_curve = [(df.index[0], equity)]

    for i in range(21, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        dow = date.weekday()

        if dow > 4:
            equity_curve.append((date, equity))
            continue

        spot = row['Close']
        atr = (df['High'].iloc[i-14:i] - df['Low'].iloc[i-14:i]).mean()
        iv = hv.iloc[i] if not pd.isna(hv.iloc[i]) else 0.20
        iv = max(min(iv * 1.15 * vol_adj, 0.80), 0.08)

        days_to_expiry = max(5, 15 - (date.day % 28))
        T = days_to_expiry / 365
        trade_pnl = 0

        # Demand/supply zones from 10-day price action
        recent_lows = df['Low'].iloc[max(0,i-10):i]
        recent_highs = df['High'].iloc[max(0,i-10):i]
        demand_zone = recent_lows.min()
        supply_zone = recent_highs.max()
        demand_strength = ((recent_lows - demand_zone).abs() < atr * 0.5).sum()
        supply_strength = ((supply_zone - recent_highs).abs() < atr * 0.5).sum()

        # Volume spike
        vol_avg = df['Volume'].iloc[max(0,i-20):i].mean() if 'Volume' in df.columns else 1
        vol_today = row['Volume'] if 'Volume' in df.columns else 1
        vol_spike = vol_today > vol_avg * 1.1 if vol_avg > 0 else True

        # Demand zone retest (BUY CE)
        if row['Low'] <= demand_zone * 1.01 and spot > demand_zone and \
           demand_strength >= 2 and vol_spike:
            ce_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
            entry_premium = g['price']

            if entry_premium > 1:
                bounce = (spot - row['Low']) / max(atr, 1)
                if bounce > 0.8:
                    exit_premium = entry_premium * 1.8
                elif bounce > 0.2:
                    exit_premium = entry_premium * 1.3
                elif row['Low'] < demand_zone * 0.99:
                    exit_premium = entry_premium * 0.4
                else:
                    exit_premium = entry_premium * 0.95

                pnl = (exit_premium - entry_premium) * lot * multiplier
                pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                pnl = max(pnl, -equity * 0.015)
                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_CE_GTZ', 'strike': ce_strike,
                    'spot': spot, 'entry_premium': round(entry_premium, 2),
                    'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                    'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                    'dte': days_to_expiry
                })

        # Supply zone retest (BUY PE)
        elif row['High'] >= supply_zone * 0.99 and spot < supply_zone and \
             supply_strength >= 2 and vol_spike:
            pe_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
            entry_premium = g['price']

            if entry_premium > 1:
                rejection = (row['High'] - spot) / max(atr, 1)
                if rejection > 0.8:
                    exit_premium = entry_premium * 1.8
                elif rejection > 0.2:
                    exit_premium = entry_premium * 1.3
                elif row['High'] > supply_zone * 1.01:
                    exit_premium = entry_premium * 0.4
                else:
                    exit_premium = entry_premium * 0.95

                pnl = (exit_premium - entry_premium) * lot * multiplier
                pnl -= calc_mcx_costs(entry_premium, lot, multiplier)
                pnl = max(pnl, -equity * 0.015)
                trade_pnl += pnl
                trades.append({
                    'date': date, 'type': 'BUY_PE_GTZ', 'strike': pe_strike,
                    'spot': spot, 'entry_premium': round(entry_premium, 2),
                    'exit_premium': round(exit_premium, 2), 'pnl': round(pnl, 2),
                    'delta': round(g['delta'], 3), 'iv': round(iv*100, 1),
                    'dte': days_to_expiry
                })

        trade_pnl = max(trade_pnl, -equity * 0.02)
        equity += trade_pnl
        equity_curve.append((date, equity))

    print(f"    Trades: {len(trades)} | PnL: Rs {equity-capital:,.0f}", flush=True)
    return trades, equity_curve, equity


# ====================================================================
# MAIN
# ====================================================================
def main():
    print("=" * 80, flush=True)
    print("COMMODITY OPTIONS STRATEGY BACKTEST", flush=True)
    print("MCX Data | 5.5 Years | Rs 3L Capital", flush=True)
    print("Black-76 Model | Real MCX Costs (CTT+Brokerage+Exchange)", flush=True)
    print("=" * 80, flush=True)

    all_results = []
    all_curves = {}
    all_trades = {}

    strategy_fns = {
        'CPR': backtest_cpr_commodity,
        'Gamma Blast': backtest_gamma_blast_commodity,
        'Ghost Zone': backtest_ghost_zone_commodity,
    }

    # Focus on most liquid commodities for options
    target_commodities = ['GOLD', 'GOLDM', 'SILVER', 'SILVERM', 'CRUDEOIL', 'NATURALGAS', 'COPPER']

    for commodity in target_commodities:
        spec = COMMODITIES[commodity]
        print(f"\n{'='*80}", flush=True)
        print(f"  {commodity} - {spec['description']}", flush=True)
        print(f"  Lot: {spec['lot_size']} | Multiplier: {spec['multiplier']} | "
              f"Margin: Rs {spec['margin']:,}", flush=True)
        print(f"{'='*80}", flush=True)

        df = load_commodity_data(commodity)
        if df is None:
            print(f"  No data for {commodity}", flush=True)
            continue
        print(f"  Data: {len(df)} days ({df.index[0].date()} to {df.index[-1].date()})", flush=True)
        print(f"  Last: {df['Close'].iloc[-1]:,.2f}", flush=True)

        for strat_name, strat_fn in strategy_fns.items():
            label = f"{strat_name} ({commodity})"
            short = f"{strat_name[:8]}({commodity[:4]})"

            trades_list, curve, final = strat_fn(df, INITIAL_CAPITAL, commodity)
            stats = compute_stats(trades_list, curve, INITIAL_CAPITAL, label)
            all_results.append(stats)
            all_curves[short] = curve
            all_trades[label] = trades_list

    # === RESULTS TABLE ===
    print("\n" + "=" * 80, flush=True)
    print("COMMODITY OPTIONS - RESULTS TABLE", flush=True)
    print("=" * 80, flush=True)
    df_results = pd.DataFrame(all_results)
    print(tabulate(df_results, headers='keys', tablefmt='grid', showindex=False), flush=True)

    # === RANK BY SHARPE ===
    df_results['Sharpe_num'] = pd.to_numeric(df_results['Sharpe'], errors='coerce')
    df_results['Trades_num'] = pd.to_numeric(df_results['Trades'], errors='coerce')
    valid = df_results[df_results['Trades_num'] >= 10].copy()
    valid = valid.sort_values('Sharpe_num', ascending=False)

    print("\n" + "=" * 80, flush=True)
    print("RANKING BY SHARPE (min 10 trades)", flush=True)
    print("=" * 80, flush=True)
    rank_cols = ['Strategy', 'Trades', 'Win Rate', 'Total PnL', 'Ann Return',
                 'Max DD', 'Sharpe', 'Sortino', 'Profit Factor']
    print(tabulate(valid[rank_cols], headers='keys', tablefmt='grid', showindex=False), flush=True)

    # === WINNER ===
    if len(valid) > 0:
        winner = valid.iloc[0]
        print(f"\n{'*'*80}", flush=True)
        print(f"  WINNER: {winner['Strategy']}", flush=True)
        print(f"  Sharpe: {winner['Sharpe']} | Return: {winner['Ann Return']} | "
              f"Max DD: {winner['Max DD']} | Win Rate: {winner['Win Rate']}", flush=True)
        print(f"{'*'*80}", flush=True)

    # === EQUITY CURVES ===
    fig, axes = plt.subplots(2, 1, figsize=(18, 14))

    colors = ['#E74C3C', '#3498DB', '#2ECC71', '#9B59B6', '#F39C12',
              '#1ABC9C', '#E67E22', '#34495E', '#16A085', '#C0392B',
              '#2980B9', '#27AE60', '#8E44AD', '#D35400', '#7F8C8D',
              '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEAA7']

    ax = axes[0]
    cidx = 0
    for label, curve in sorted(all_curves.items()):
        if curve and len(curve) > 10:
            dates = [c[0] for c in curve]
            vals = [c[1] for c in curve]
            ax.plot(dates, vals, label=f"{label} (Rs {vals[-1]:,.0f})",
                   linewidth=1.2, color=colors[cidx % len(colors)], alpha=0.8)
            cidx += 1

    ax.axhline(y=INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5)
    ax.set_ylabel('Portfolio Value (Rs)')
    ax.set_title('Commodity Options - 3 Strategies x 7 Commodities\n'
                 'MCX 5.5yr | Black-76 | Real Costs', fontsize=12, fontweight='bold')
    ax.legend(fontsize=6, ncol=3, loc='upper left')
    ax.grid(True, alpha=0.3)

    # Strategy performance by commodity
    ax2 = axes[1]
    comm_data = {}
    for strat in ['CPR', 'Gamma Blast', 'Ghost Zone']:
        vals = []
        for comm in target_commodities:
            match = [r for r in all_results if r['Strategy'] == f"{strat} ({comm})"]
            if match:
                ret_str = match[0]['Ann Return'].replace('%', '')
                vals.append(float(ret_str))
            else:
                vals.append(0)
        comm_data[strat] = vals

    x = np.arange(len(target_commodities))
    width = 0.25
    for idx, strat in enumerate(comm_data.keys()):
        vals = comm_data[strat]
        bars = ax2.bar(x + idx*width, vals, width, label=strat, alpha=0.8)
        for bar, v in zip(bars, vals):
            if v != 0:
                ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                        f'{v:.1f}%', ha='center', va='bottom', fontsize=6, rotation=45)

    ax2.set_xticks(x + width)
    ax2.set_xticklabels(target_commodities, rotation=30)
    ax2.set_ylabel('Annualized Return %')
    ax2.set_title('Strategy Performance by Commodity', fontsize=12, fontweight='bold')
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3, axis='y')
    ax2.axhline(y=0, color='black', linewidth=0.5)

    plt.tight_layout()
    fp = os.path.join(RESULTS_DIR, 'commodity_strategy_comparison.png')
    plt.savefig(fp, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\nSaved: {fp}", flush=True)

    # Save CSV
    df_results.to_csv(os.path.join(RESULTS_DIR, 'commodity_strategy_comparison.csv'), index=False)

    # Save all trades
    for name, trades_list in all_trades.items():
        if trades_list:
            safe = name.replace(' ', '_').replace('(', '').replace(')', '')
            pd.DataFrame(trades_list).to_csv(
                os.path.join(RESULTS_DIR, f'trades_commodity_{safe}.csv'), index=False)

    # === OPTIMAL COMMODITY PORTFOLIO ===
    if len(valid) > 0:
        profitable = valid[valid['Sharpe_num'] > 0].head(10)
        if len(profitable) > 0:
            print(f"\n{'='*80}", flush=True)
            print("OPTIMAL COMMODITY PORTFOLIO", flush=True)
            print(f"{'='*80}", flush=True)

            weights = {}
            total_sharpe = profitable['Sharpe_num'].sum()
            for _, row in profitable.iterrows():
                w = row['Sharpe_num'] / total_sharpe if total_sharpe > 0 else 1/len(profitable)
                sname = row['Strategy']
                # Find matching curve key
                for strat in ['CPR', 'Gamma Blast', 'Ghost Zone']:
                    for comm in target_commodities:
                        if strat in sname and comm in sname:
                            short = f"{strat[:8]}({comm[:4]})"
                            if short in all_curves:
                                weights[short] = w

            for name, w in sorted(weights.items(), key=lambda x: -x[1]):
                amt = INITIAL_CAPITAL * w
                print(f"  {name}: {w*100:.1f}% (Rs {amt:,.0f})", flush=True)

            # Combined equity
            all_dates = set()
            for k, curve in all_curves.items():
                if k in weights:
                    all_dates.update([c[0] for c in curve])
            all_dates = sorted(all_dates)

            if all_dates:
                combined = pd.Series(0.0, index=all_dates)
                for name, curve in all_curves.items():
                    if name in weights and curve:
                        s = pd.Series([c[1] for c in curve], index=[c[0] for c in curve])
                        s = s.reindex(all_dates, method='ffill').fillna(INITIAL_CAPITAL)
                        combined += s * weights[name]

                total_ret = (combined.iloc[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
                n_yrs = max((combined.index[-1] - combined.index[0]).days / 365.25, 0.01)
                ann_ret = ((combined.iloc[-1]/INITIAL_CAPITAL)**(1/n_yrs)-1)*100
                peak = combined.expanding().max()
                max_dd = ((combined - peak) / peak * 100).min()
                daily_r = combined.pct_change().dropna()
                sharpe = np.sqrt(252)*(daily_r.mean()-RISK_FREE_RATE/252)/daily_r.std() if daily_r.std()>0 else 0

                print(f"\n  Initial:      Rs {INITIAL_CAPITAL:,.0f}", flush=True)
                print(f"  Final:        Rs {combined.iloc[-1]:,.0f}", flush=True)
                print(f"  Total Return: {total_ret:.1f}%", flush=True)
                print(f"  Ann Return:   {ann_ret:.1f}%", flush=True)
                print(f"  Max DD:       {max_dd:.2f}%", flush=True)
                print(f"  Sharpe:       {sharpe:.2f}", flush=True)

    print(f"\n{'='*80}", flush=True)
    print("COMMODITY BACKTEST COMPLETE!", flush=True)
    print(f"Results: {RESULTS_DIR}", flush=True)
    print(f"{'='*80}", flush=True)


if __name__ == "__main__":
    main()
