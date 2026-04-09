"""
Crypto 4H Backtest — Improved Strategies with ATR Stops & Risk Management
BTCUSDT, ETHUSDT, SOLUSDT — Binance 4-Hour Candles

Tests the NEW strategy lineup:
  1. Filtered Trend (EMA10/30 + Vol filter) — replaces EMA Momentum
  2. MACD + RSI Combo (with histogram momentum filter)
  3. SuperTrend Rider

Each strategy tested WITH and WITHOUT ATR-based stop-loss + trailing stop.
Compares 4H vs Daily timeframe performance.

Usage:
    python crypto_backtest_4h.py
"""

import pandas as pd
import numpy as np
import requests
import time
import os
import warnings
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings('ignore')

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
INITIAL_CAPITAL = 300000  # Rs 3,00,000
FEE_RATE = 0.001          # 0.1% per trade (Binance standard)
BINANCE_BASE = "https://api.binance.com/api/v3/klines"

# Stop-loss parameters
STOP_ATR_MULT = 2.0       # Initial stop: 2x ATR from entry
TRAIL_ATR_MULT = 1.5      # Trailing stop: 1.5x ATR from best price

# Risk management
DAILY_LOSS_LIMIT_PCT = 0.02   # 2% daily loss limit
MAX_DRAWDOWN_PCT = 0.08       # 8% max drawdown circuit breaker
RISK_PER_TRADE_PCT = 0.01     # 1% risk per trade for position sizing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)


# ── DATA FETCH ─────────────────────────────────────────────────────────────────
def fetch_binance_data(symbol, interval='4h', years=2):
    """Fetch historical kline data from Binance public API."""
    print(f"  Fetching {symbol} ({years}yr {interval})...", end=" ", flush=True)
    all_data = []
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=years * 365)).timestamp() * 1000)

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
    print(f"{len(df)} candles ({df['Date'].iloc[0].strftime('%Y-%m-%d')} to {df['Date'].iloc[-1].strftime('%Y-%m-%d')})")
    return df


# ── INDICATOR HELPERS ──────────────────────────────────────────────────────────
def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def sma(series, window):
    return series.rolling(window).mean()

def atr_calc(df, period=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def macd_calc(series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def supertrend_calc(df, period=10, multiplier=3.0):
    hl2 = (df['High'] + df['Low']) / 2
    atr_val = atr_calc(df, period)
    upper_band = hl2 + (multiplier * atr_val)
    lower_band = hl2 - (multiplier * atr_val)
    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    st.iloc[0] = upper_band.iloc[0]
    direction.iloc[0] = -1
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > st.iloc[i-1]:
            direction.iloc[i] = 1
            st.iloc[i] = max(lower_band.iloc[i], st.iloc[i-1]) if direction.iloc[i-1] == 1 else lower_band.iloc[i]
        else:
            direction.iloc[i] = -1
            st.iloc[i] = min(upper_band.iloc[i], st.iloc[i-1]) if direction.iloc[i-1] == -1 else upper_band.iloc[i]
    return st, direction


# ── PRE-COMPUTE ALL INDICATORS ────────────────────────────────────────────────
def precompute(df):
    ind = pd.DataFrame(index=df.index)

    # EMAs
    ind['ema5'] = ema(df['Close'], 5)
    ind['ema10'] = ema(df['Close'], 10)
    ind['ema20'] = ema(df['Close'], 20)
    ind['ema30'] = ema(df['Close'], 30)

    # ATR
    ind['atr14'] = atr_calc(df, 14)
    ind['atr_avg50'] = ind['atr14'].rolling(50).mean()

    # RSI
    ind['rsi14'] = rsi(df['Close'], 14)

    # MACD
    ind['macd_line'], ind['macd_signal'], ind['macd_hist'] = macd_calc(df['Close'])

    # SuperTrend
    ind['supertrend'], ind['st_direction'] = supertrend_calc(df, 10, 3.0)

    return ind


# ── STRATEGY IMPLEMENTATIONS ──────────────────────────────────────────────────

def strat_ema_momentum(df, ind, i):
    """OLD Strategy: EMA Momentum (5-day) — for comparison baseline."""
    if pd.isna(ind['ema5'].iloc[i]):
        return 0
    return 1 if df['Close'].iloc[i] > ind['ema5'].iloc[i] else 0


def strat_filtered_trend(df, ind, i):
    """NEW Strategy: EMA10/30 crossover + ATR volatility filter.
    Only trades when ATR > 1.2x its 50-bar average (high volatility).
    Avoids whipsaw in calm markets. Can SHORT."""
    if pd.isna(ind['ema10'].iloc[i]) or pd.isna(ind['ema30'].iloc[i]):
        return 0
    if pd.isna(ind['atr_avg50'].iloc[i]) or ind['atr_avg50'].iloc[i] == 0:
        return 0

    high_vol = ind['atr14'].iloc[i] > 1.2 * ind['atr_avg50'].iloc[i]
    trend_up = ind['ema10'].iloc[i] > ind['ema30'].iloc[i]

    if high_vol and trend_up:
        return 1
    elif high_vol and not trend_up:
        return -1
    else:
        return 0


def strat_macd_rsi_original(df, ind, i):
    """OLD MACD+RSI — for comparison baseline."""
    if i < 1 or pd.isna(ind['macd_line'].iloc[i]):
        return 0
    macd_above = ind['macd_line'].iloc[i] > ind['macd_signal'].iloc[i]
    rsi_val = ind['rsi14'].iloc[i]
    if macd_above and rsi_val > 50:
        return 1
    elif not macd_above and rsi_val < 50:
        return -1
    else:
        return 0


def strat_macd_rsi_improved(df, ind, i):
    """IMPROVED MACD+RSI with histogram momentum filter.
    Requires histogram to be growing (increasing momentum) for entry."""
    if i < 1 or pd.isna(ind['macd_line'].iloc[i]):
        return 0

    macd_above = ind['macd_line'].iloc[i] > ind['macd_signal'].iloc[i]
    rsi_val = ind['rsi14'].iloc[i]
    hist = ind['macd_hist'].iloc[i]
    prev_hist = ind['macd_hist'].iloc[i-1]

    # Require histogram momentum (growing, not shrinking)
    hist_growing = abs(hist) > abs(prev_hist)

    if macd_above and rsi_val > 50 and hist > 0 and hist_growing:
        return 1
    elif not macd_above and rsi_val < 50 and hist < 0 and hist_growing:
        return -1
    else:
        return 0


def strat_supertrend(df, ind, i):
    """SuperTrend: LONG when direction=1, SHORT when direction=-1."""
    if pd.isna(ind['st_direction'].iloc[i]):
        return 0
    return int(ind['st_direction'].iloc[i])


# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────

def backtest(df, ind, strategy_fn, capital=INITIAL_CAPITAL, use_stops=False, use_position_sizing=False):
    """
    Backtest with optional ATR-based stops and position sizing.
    """
    start_idx = 120
    equity = capital
    peak = capital
    max_dd = 0
    position = 0
    equity_curve = []

    # Per-trade tracking
    trade_list = []
    entry_price = 0
    entry_date = None
    entry_equity = capital
    direction = 0

    # Stop tracking
    high_watermark = 0
    low_watermark = float('inf')
    stop_price = 0
    position_qty = 0
    position_alloc = 0

    # Risk management
    daily_pnl = 0
    current_day = None
    paused = False

    for i in range(start_idx, len(df)):
        current_price = df['Close'].iloc[i]
        current_atr = ind['atr14'].iloc[i] if not pd.isna(ind['atr14'].iloc[i]) else 0

        # Track daily PnL reset
        day = df['Date'].iloc[i].date()
        if day != current_day:
            daily_pnl = 0
            current_day = day
            paused = False

        # === CHECK STOPS (before signal) ===
        stop_hit = False
        if use_stops and position != 0 and current_atr > 0:
            if position == 1:  # LONG
                high_watermark = max(high_watermark, df['High'].iloc[i])
                initial_stop = entry_price - STOP_ATR_MULT * current_atr
                trail_stop = high_watermark - TRAIL_ATR_MULT * current_atr
                effective_stop = max(initial_stop, trail_stop)

                if df['Low'].iloc[i] <= effective_stop:
                    stop_hit = True
                    exit_price = effective_stop  # stopped out at stop level

            else:  # SHORT
                low_watermark = min(low_watermark, df['Low'].iloc[i])
                initial_stop = entry_price + STOP_ATR_MULT * current_atr
                trail_stop = low_watermark + TRAIL_ATR_MULT * current_atr
                effective_stop = min(initial_stop, trail_stop)

                if df['High'].iloc[i] >= effective_stop:
                    stop_hit = True
                    exit_price = effective_stop

        if stop_hit:
            # Close position at stop
            equity *= (1 - FEE_RATE)
            if position == 1:
                trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
            else:
                trade_pnl_pct = (entry_price - exit_price) / entry_price * 100

            trade_pnl = equity - entry_equity
            # Adjust equity for actual stop exit vs close price difference
            if position == 1:
                equity *= (exit_price / entry_price) / (current_price / entry_price) if current_price != entry_price else 1

            daily_pnl += trade_pnl
            trade_list.append({
                'entry_date': entry_date,
                'exit_date': df['Date'].iloc[i],
                'direction': 'LONG' if position == 1 else 'SHORT',
                'entry_price': round(entry_price, 2),
                'exit_price': round(exit_price, 2),
                'pnl_pct': round(trade_pnl_pct, 2),
                'pnl': round(trade_pnl, 2),
                'equity_after': round(equity, 2),
                'exit_type': 'STOP',
            })
            position = 0

            # Check daily loss limit
            if daily_pnl < -capital * DAILY_LOSS_LIMIT_PCT:
                paused = True
            continue

        # === GENERATE SIGNAL ===
        signal = strategy_fn(df, ind, i)

        # Check drawdown circuit breaker
        if equity < peak * (1 - MAX_DRAWDOWN_PCT):
            signal = 0  # Force close everything

        # Skip new entries if paused
        if paused and signal != 0 and position == 0:
            signal = 0

        # Position change
        if signal != position:
            # Close existing position
            if position != 0 and i > start_idx:
                exit_price = df['Close'].iloc[i]
                equity *= (1 - FEE_RATE)

                if position == 1:
                    trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    trade_pnl_pct = (entry_price - exit_price) / entry_price * 100

                trade_pnl = equity - entry_equity
                daily_pnl += trade_pnl
                trade_list.append({
                    'entry_date': entry_date,
                    'exit_date': df['Date'].iloc[i],
                    'direction': 'LONG' if position == 1 else 'SHORT',
                    'entry_price': round(entry_price, 2),
                    'exit_price': round(exit_price, 2),
                    'pnl_pct': round(trade_pnl_pct, 2),
                    'pnl': round(trade_pnl, 2),
                    'equity_after': round(equity, 2),
                    'exit_type': 'SIGNAL',
                })

            # Open new position
            if signal != 0:
                equity *= (1 - FEE_RATE)
                entry_price = df['Close'].iloc[i]
                entry_date = df['Date'].iloc[i]
                entry_equity = equity
                direction = signal
                high_watermark = entry_price
                low_watermark = entry_price

                if use_position_sizing and current_atr > 0:
                    risk_amount = equity * RISK_PER_TRADE_PCT
                    stop_distance = STOP_ATR_MULT * current_atr
                    position_qty = risk_amount / stop_distance
                    position_alloc = min(position_qty * entry_price, equity * 0.40)
                else:
                    position_alloc = equity

            position = signal

        # Apply daily return if in position
        elif position != 0 and i > start_idx:
            prev_close = df['Close'].iloc[i-1]
            cur_close = df['Close'].iloc[i]

            if position == 1:
                daily_ret = (cur_close - prev_close) / prev_close
            else:
                daily_ret = (prev_close - cur_close) / prev_close

            if use_position_sizing and position_alloc < equity:
                # Only portion of equity is deployed
                deploy_frac = position_alloc / equity
                equity *= (1 + daily_ret * deploy_frac)
            else:
                equity *= (1 + daily_ret)

        # Track drawdown
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    # Close final position
    if position != 0:
        exit_price = df['Close'].iloc[-1]
        if position == 1:
            trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            trade_pnl_pct = (entry_price - exit_price) / entry_price * 100
        trade_pnl = equity - entry_equity
        trade_list.append({
            'entry_date': entry_date,
            'exit_date': df['Date'].iloc[-1],
            'direction': 'LONG' if position == 1 else 'SHORT',
            'entry_price': round(entry_price, 2),
            'exit_price': round(exit_price, 2),
            'pnl_pct': round(trade_pnl_pct, 2),
            'pnl': round(trade_pnl, 2),
            'equity_after': round(equity, 2),
            'exit_type': 'EOD',
        })

    # ── METRICS ──
    total_return = (equity - capital) / capital * 100
    n_bars = len(df) - start_idx
    if '4h' in str(df['Date'].iloc[1] - df['Date'].iloc[0]) or n_bars > 2000:
        # 4h data: ~6 bars per day, ~2190 per year
        years = n_bars / 2190
    else:
        years = n_bars / 365.25
    years = max(years, 0.1)
    cagr = ((equity / capital) ** (1 / years) - 1) * 100 if equity > 0 else -100

    # Sharpe
    if len(equity_curve) > 1:
        daily_rets = pd.Series(equity_curve).pct_change().dropna()
        if '4h' in str(df['Date'].iloc[1] - df['Date'].iloc[0]) or n_bars > 2000:
            sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(2190)
        else:
            sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(365)
    else:
        sharpe = 0

    # Per-trade metrics
    num_trades = len(trade_list)
    winning = [t for t in trade_list if t['pnl'] > 0]
    losing = [t for t in trade_list if t['pnl'] <= 0]
    num_wins = len(winning)
    num_losses = len(losing)
    win_rate = (num_wins / num_trades * 100) if num_trades > 0 else 0

    gross_profit = sum(t['pnl'] for t in winning) if winning else 0
    gross_loss = abs(sum(t['pnl'] for t in losing)) if losing else 0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.99 if gross_profit > 0 else 0)

    avg_win_pct = np.mean([t['pnl_pct'] for t in winning]) if winning else 0
    avg_loss_pct = np.mean([abs(t['pnl_pct']) for t in losing]) if losing else 0

    # Stop vs signal exits
    stop_exits = len([t for t in trade_list if t.get('exit_type') == 'STOP'])
    signal_exits = len([t for t in trade_list if t.get('exit_type') == 'SIGNAL'])

    calmar = (cagr / max_dd) if max_dd > 0 else 0

    return {
        'final_equity': round(equity, 2),
        'total_return': round(total_return, 2),
        'total_pnl': round(equity - capital, 2),
        'cagr': round(cagr, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'calmar': round(calmar, 2),
        'num_trades': num_trades,
        'wins': num_wins,
        'losses': num_losses,
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'avg_win_pct': round(avg_win_pct, 2),
        'avg_loss_pct': round(avg_loss_pct, 2),
        'stop_exits': stop_exits,
        'signal_exits': signal_exits,
        'trade_list': trade_list,
    }


def buy_and_hold(df, capital=INITIAL_CAPITAL):
    """Buy-and-hold benchmark."""
    start_idx = 120
    start_price = df['Close'].iloc[start_idx]
    end_price = df['Close'].iloc[-1]
    equity = capital * (end_price / start_price)
    total_return = (equity - capital) / capital * 100
    n_bars = len(df) - start_idx
    if n_bars > 2000:
        years = n_bars / 2190
    else:
        years = n_bars / 365.25
    years = max(years, 0.1)
    cagr = ((equity / capital) ** (1 / years) - 1) * 100

    prices = df['Close'].iloc[start_idx:]
    cum_max = prices.cummax()
    dd = ((cum_max - prices) / cum_max * 100).max()

    daily_rets = df['Close'].iloc[start_idx:].pct_change().dropna()
    if n_bars > 2000:
        sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(2190)
    else:
        sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(365)

    return {
        'final_equity': round(equity, 2),
        'total_return': round(total_return, 2),
        'cagr': round(cagr, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(dd, 2),
    }


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 130)
    print("  CRYPTO 4H BACKTEST — IMPROVED STRATEGIES WITH ATR STOPS")
    print("  Pairs: BTCUSDT | ETHUSDT | SOLUSDT")
    print("  Timeframes: 4H (2 years) + Daily (5 years) comparison")
    print("  Capital: Rs 3,00,000 | Fees: 0.1%/trade")
    print("  Stop-Loss: 2x ATR | Trailing: 1.5x ATR")
    print("=" * 130)

    # ── STRATEGIES ──
    OLD_STRATEGIES = {
        'EMA Momentum (OLD)': strat_ema_momentum,
        'MACD+RSI (OLD)': strat_macd_rsi_original,
        'SuperTrend (OLD)': strat_supertrend,
    }

    NEW_STRATEGIES = {
        'Filtered Trend (NEW)': strat_filtered_trend,
        'MACD+RSI Improved (NEW)': strat_macd_rsi_improved,
        'SuperTrend (KEEP)': strat_supertrend,
    }

    ALL_STRATEGIES = {**OLD_STRATEGIES, **NEW_STRATEGIES}

    # ── FETCH 4H DATA (2 years) ──
    print("\n--- FETCHING 4-HOUR DATA (2 years) ---")
    data_4h = {}
    ind_4h = {}
    for sym in SYMBOLS:
        df = fetch_binance_data(sym, '4h', 2)
        if df is not None and len(df) > 200:
            data_4h[sym] = df
            ind_4h[sym] = precompute(df)
        time.sleep(0.5)

    # ── FETCH DAILY DATA (5 years) ──
    print("\n--- FETCHING DAILY DATA (5 years) ---")
    data_1d = {}
    ind_1d = {}
    for sym in SYMBOLS:
        df = fetch_binance_data(sym, '1d', 5)
        if df is not None and len(df) > 200:
            data_1d[sym] = df
            ind_1d[sym] = precompute(df)
        time.sleep(0.5)

    if not data_4h and not data_1d:
        print("ERROR: No data fetched!")
        return

    # ── BUY-AND-HOLD ──
    print("\n" + "=" * 130)
    print("  BUY-AND-HOLD BENCHMARK")
    print("=" * 130)
    benchmarks_4h = {}
    benchmarks_1d = {}
    for sym in SYMBOLS:
        if sym in data_4h:
            bh = buy_and_hold(data_4h[sym])
            benchmarks_4h[sym] = bh
            print(f"  {sym} (4H 2yr): Return={bh['total_return']:>+10.1f}%  CAGR={bh['cagr']:>+8.1f}%  Sharpe={bh['sharpe']:>5.2f}  MaxDD={bh['max_drawdown']:>6.1f}%")
        if sym in data_1d:
            bh = buy_and_hold(data_1d[sym])
            benchmarks_1d[sym] = bh
            print(f"  {sym} (1D 5yr): Return={bh['total_return']:>+10.1f}%  CAGR={bh['cagr']:>+8.1f}%  Sharpe={bh['sharpe']:>5.2f}  MaxDD={bh['max_drawdown']:>6.1f}%")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1: 4H BACKTEST — OLD vs NEW (without stops)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  SECTION 1: 4H BACKTEST — OLD vs NEW STRATEGIES (NO STOPS)")
    print("=" * 130)

    header = (f"  {'Symbol':<10} {'Strategy':<30} {'Trades':>6} {'WinR%':>6} {'PnL':>14} "
              f"{'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'PF':>6} {'AvgW%':>7} {'AvgL%':>7}")
    print(header)
    print("  " + "-" * 120)

    results_4h_no_stops = {}
    for sname, sfn in ALL_STRATEGIES.items():
        results_4h_no_stops[sname] = {}
        for sym in SYMBOLS:
            if sym not in data_4h:
                continue
            r = backtest(data_4h[sym], ind_4h[sym], sfn, use_stops=False)
            results_4h_no_stops[sname][sym] = r
            print(f"  {sym:<10} {sname:<30} {r['num_trades']:>5} {r['win_rate']:>5.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['cagr']:>+7.1f}% {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% {r['profit_factor']:>5.1f} {r['avg_win_pct']:>+6.1f}% "
                  f"{r['avg_loss_pct']:>6.1f}%")
        print()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2: 4H BACKTEST — NEW STRATEGIES WITH ATR STOPS
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  SECTION 2: 4H BACKTEST — NEW STRATEGIES WITH ATR STOPS (2x SL + 1.5x Trail)")
    print("=" * 130)

    header2 = (f"  {'Symbol':<10} {'Strategy':<30} {'Trades':>6} {'WinR%':>6} {'PnL':>14} "
               f"{'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'PF':>6} {'StopX':>6} {'SigX':>6}")
    print(header2)
    print("  " + "-" * 120)

    results_4h_stops = {}
    for sname, sfn in NEW_STRATEGIES.items():
        results_4h_stops[sname] = {}
        for sym in SYMBOLS:
            if sym not in data_4h:
                continue
            r = backtest(data_4h[sym], ind_4h[sym], sfn, use_stops=True)
            results_4h_stops[sname][sym] = r
            print(f"  {sym:<10} {sname:<30} {r['num_trades']:>5} {r['win_rate']:>5.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['cagr']:>+7.1f}% {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% {r['profit_factor']:>5.1f} {r['stop_exits']:>5} "
                  f"{r['signal_exits']:>5}")
        print()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3: 4H WITH STOPS + POSITION SIZING
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  SECTION 3: 4H — NEW STRATEGIES WITH STOPS + ATR POSITION SIZING (1% risk/trade)")
    print("=" * 130)
    print(header2)
    print("  " + "-" * 120)

    results_4h_full = {}
    for sname, sfn in NEW_STRATEGIES.items():
        results_4h_full[sname] = {}
        for sym in SYMBOLS:
            if sym not in data_4h:
                continue
            r = backtest(data_4h[sym], ind_4h[sym], sfn, use_stops=True, use_position_sizing=True)
            results_4h_full[sname][sym] = r
            print(f"  {sym:<10} {sname:<30} {r['num_trades']:>5} {r['win_rate']:>5.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['cagr']:>+7.1f}% {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% {r['profit_factor']:>5.1f} {r['stop_exits']:>5} "
                  f"{r['signal_exits']:>5}")
        print()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 4: DAILY vs 4H COMPARISON (same strategies)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  SECTION 4: DAILY (5yr) vs 4H (2yr) COMPARISON — NEW STRATEGIES WITHOUT STOPS")
    print("=" * 130)

    print(f"\n  {'Symbol':<10} {'Strategy':<30} {'TF':>4} {'Trades':>6} {'WinR%':>6} "
          f"{'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'PF':>6}")
    print("  " + "-" * 100)

    for sname, sfn in NEW_STRATEGIES.items():
        for sym in SYMBOLS:
            # 4H result
            if sym in data_4h:
                r4 = results_4h_no_stops.get(sname, {}).get(sym, None)
                if r4:
                    print(f"  {sym:<10} {sname:<30} {'4H':>4} {r4['num_trades']:>5} {r4['win_rate']:>5.1f}% "
                          f"{r4['cagr']:>+7.1f}% {r4['sharpe']:>6.2f} {r4['max_drawdown']:>6.1f}% "
                          f"{r4['profit_factor']:>5.1f}")
            # Daily result
            if sym in data_1d:
                r1 = backtest(data_1d[sym], ind_1d[sym], sfn, use_stops=False)
                print(f"  {'':10} {'':30} {'1D':>4} {r1['num_trades']:>5} {r1['win_rate']:>5.1f}% "
                      f"{r1['cagr']:>+7.1f}% {r1['sharpe']:>6.2f} {r1['max_drawdown']:>6.1f}% "
                      f"{r1['profit_factor']:>5.1f}")
            print()

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL RANKING
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  FINAL RANKING — BEST CONFIGURATIONS (4H, with stops)")
    print("=" * 130)

    ranking = []
    for sname in NEW_STRATEGIES:
        cagrs, sharpes, dds, wrs, pfs = [], [], [], [], []
        for sym in SYMBOLS:
            if sym in results_4h_stops.get(sname, {}):
                r = results_4h_stops[sname][sym]
                cagrs.append(r['cagr'])
                sharpes.append(r['sharpe'])
                dds.append(r['max_drawdown'])
                wrs.append(r['win_rate'])
                pfs.append(r['profit_factor'])

        if cagrs:
            ranking.append({
                'strategy': sname,
                'avg_cagr': np.mean(cagrs),
                'avg_sharpe': np.mean(sharpes),
                'worst_dd': max(dds),
                'avg_dd': np.mean(dds),
                'avg_wr': np.mean(wrs),
                'avg_pf': np.mean(pfs),
            })

    # Also add old strategies for comparison
    for sname in OLD_STRATEGIES:
        cagrs, sharpes, dds, wrs = [], [], [], []
        for sym in SYMBOLS:
            if sym in results_4h_no_stops.get(sname, {}):
                r = results_4h_no_stops[sname][sym]
                cagrs.append(r['cagr'])
                sharpes.append(r['sharpe'])
                dds.append(r['max_drawdown'])
                wrs.append(r['win_rate'])

        if cagrs:
            ranking.append({
                'strategy': sname + ' [no stops]',
                'avg_cagr': np.mean(cagrs),
                'avg_sharpe': np.mean(sharpes),
                'worst_dd': max(dds),
                'avg_dd': np.mean(dds),
                'avg_wr': np.mean(wrs),
                'avg_pf': 0,
            })

    ranking.sort(key=lambda x: x['avg_sharpe'], reverse=True)

    print(f"\n  {'Rank':<5} {'Strategy':<35} {'CAGR':>8} {'Sharpe':>8} {'WorstDD':>8} "
          f"{'AvgDD':>7} {'WinR%':>7} {'PF':>6}")
    print("  " + "-" * 90)

    for rank, s in enumerate(ranking, 1):
        marker = " *** BEST" if rank == 1 else ""
        print(f"  {rank:<3}   {s['strategy']:<35} {s['avg_cagr']:>+7.1f}% {s['avg_sharpe']:>7.2f} "
              f"{s['worst_dd']:>7.1f}% {s['avg_dd']:>6.1f}% {s['avg_wr']:>6.1f}% "
              f"{s['avg_pf']:>5.1f}{marker}")

    # ── IMPROVEMENT SUMMARY ──
    print(f"\n{'='*130}")
    print(f"  IMPROVEMENT SUMMARY — OLD vs NEW")
    print(f"{'='*130}")

    for sym in SYMBOLS:
        old_total_dd = 0
        new_total_dd = 0
        old_strats = 0
        new_strats = 0

        for sname in OLD_STRATEGIES:
            if sym in results_4h_no_stops.get(sname, {}):
                old_total_dd += results_4h_no_stops[sname][sym]['max_drawdown']
                old_strats += 1

        for sname in NEW_STRATEGIES:
            if sym in results_4h_stops.get(sname, {}):
                new_total_dd += results_4h_stops[sname][sym]['max_drawdown']
                new_strats += 1

        if old_strats and new_strats:
            old_avg_dd = old_total_dd / old_strats
            new_avg_dd = new_total_dd / new_strats
            dd_improvement = old_avg_dd - new_avg_dd
            print(f"  {sym}: Avg MaxDD OLD={old_avg_dd:.1f}% → NEW={new_avg_dd:.1f}% (improved by {dd_improvement:+.1f}pp)")

    # ── SAVE RESULTS ──
    csv_rows = []
    for section_name, section_results in [
        ('4H_NoStops', results_4h_no_stops),
        ('4H_Stops', results_4h_stops),
        ('4H_Full', results_4h_full),
    ]:
        for sname, sym_results in section_results.items():
            for sym, r in sym_results.items():
                csv_rows.append({
                    'Section': section_name,
                    'Instrument': sym,
                    'Strategy': sname,
                    'Trades': r['num_trades'],
                    'Win_Rate_%': r['win_rate'],
                    'Total_PnL': r['total_pnl'],
                    'CAGR_%': r['cagr'],
                    'Sharpe': r['sharpe'],
                    'Max_Drawdown_%': r['max_drawdown'],
                    'Profit_Factor': r['profit_factor'],
                    'Avg_Win_%': r['avg_win_pct'],
                    'Avg_Loss_%': r['avg_loss_pct'],
                    'Stop_Exits': r.get('stop_exits', 0),
                    'Signal_Exits': r.get('signal_exits', 0),
                })

    csv_df = pd.DataFrame(csv_rows)
    csv_path = os.path.join(REPORT_DIR, 'crypto_4h_backtest_results.csv')
    csv_df.to_csv(csv_path, index=False)
    print(f"\n  Results saved to: {csv_path}")

    # ── TELEGRAM SUMMARY ──
    try:
        from trade_notifier import send_message
        best = ranking[0] if ranking else None
        if best:
            msg = (
                f"<b>CRYPTO 4H BACKTEST RESULTS</b>\n\n"
                f"<b>Best: {best['strategy']}</b>\n"
                f"CAGR: {best['avg_cagr']:+.1f}% | Sharpe: {best['avg_sharpe']:.2f}\n"
                f"WinRate: {best['avg_wr']:.1f}% | MaxDD: {best['worst_dd']:.1f}%\n\n"
            )
            for rank, s in enumerate(ranking, 1):
                msg += f"{rank}. {s['strategy']}: CAGR {s['avg_cagr']:+.1f}% Sharpe {s['avg_sharpe']:.2f}\n"
            send_message(msg)
    except Exception:
        pass

    print(f"\n{'='*130}")
    print("  DONE!")
    print(f"{'='*130}")


if __name__ == "__main__":
    main()
