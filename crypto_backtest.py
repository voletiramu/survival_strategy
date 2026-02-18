"""
Crypto Futures Backtest — 7 World-Class Strategies vs Buy-and-Hold
BTCUSDT, ETHUSDT, SOLUSDT — 5 Years of Daily Data from Binance

Strategies researched from:
  - Grayscale Research (20/100 MA crossover, 116% CAGR, 1.7 Sharpe)
  - QuantifiedStrategies.com (5-day EMA momentum, 145% CAGR)
  - Academic papers (volatility-filtered EMA, Sharpe > 2)
  - Michael Chalek / Larry Williams (Dual Thrust breakout)
  - Elder's Triple Screen (multi-timeframe momentum)

Key improvements over previous backtest:
  - Full capital deployment (not 2% risk per trade)
  - Compound returns (reinvest profits)
  - Transaction fees included (0.1% per trade)
  - Crypto-native indicators (EMA, SuperTrend, MACD, RSI)
  - Proper buy-and-hold benchmark comparison
"""

import pandas as pd
import numpy as np
import requests
import time
import warnings
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings('ignore')

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
INITIAL_CAPITAL = 300000  # Rs 3,00,000
FEE_RATE = 0.001          # 0.1% per trade (Binance standard)
BINANCE_BASE = "https://api.binance.com/api/v3/klines"


# ── DATA FETCH ─────────────────────────────────────────────────────────────────
def fetch_binance_data(symbol, interval='1d', years=5):
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

def atr(df, period=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def supertrend(df, period=10, multiplier=3.0):
    """Calculate SuperTrend indicator."""
    hl2 = (df['High'] + df['Low']) / 2
    atr_val = atr(df, period)

    upper_band = hl2 + (multiplier * atr_val)
    lower_band = hl2 - (multiplier * atr_val)

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    supertrend.iloc[0] = upper_band.iloc[0]
    direction.iloc[0] = -1

    for i in range(1, len(df)):
        if df['Close'].iloc[i] > supertrend.iloc[i-1]:
            direction.iloc[i] = 1
            supertrend.iloc[i] = max(lower_band.iloc[i], supertrend.iloc[i-1]) if direction.iloc[i-1] == 1 else lower_band.iloc[i]
        else:
            direction.iloc[i] = -1
            supertrend.iloc[i] = min(upper_band.iloc[i], supertrend.iloc[i-1]) if direction.iloc[i-1] == -1 else upper_band.iloc[i]

    return supertrend, direction


# ── PRE-COMPUTE ALL INDICATORS ────────────────────────────────────────────────
def precompute(df):
    """Pre-compute all indicators for the entire dataframe."""
    ind = pd.DataFrame(index=df.index)

    # EMAs
    ind['ema5'] = ema(df['Close'], 5)
    ind['ema10'] = ema(df['Close'], 10)
    ind['ema20'] = ema(df['Close'], 20)
    ind['ema30'] = ema(df['Close'], 30)
    ind['ema50'] = ema(df['Close'], 50)

    # SMAs
    ind['sma20'] = sma(df['Close'], 20)
    ind['sma100'] = sma(df['Close'], 100)

    # ATR
    ind['atr14'] = atr(df, 14)
    ind['atr_avg50'] = ind['atr14'].rolling(50).mean()

    # RSI
    ind['rsi14'] = rsi(df['Close'], 14)

    # MACD
    ind['macd_line'], ind['macd_signal'], ind['macd_hist'] = macd(df['Close'])

    # SuperTrend
    ind['supertrend'], ind['st_direction'] = supertrend(df, 10, 3.0)

    # Weekly EMA for multi-timeframe (approximation using 5-day rolling)
    ind['weekly_close'] = df['Close'].rolling(5).mean()
    ind['weekly_ema50'] = ema(ind['weekly_close'], 50)

    # Dual Thrust range
    for n in [4]:
        hh = df['High'].rolling(n).max()
        ll = df['Low'].rolling(n).min()
        hc = df['Close'].rolling(n).max()
        lc = df['Close'].rolling(n).min()
        ind[f'dt_range_{n}'] = pd.concat([hh - lc, hc - ll], axis=1).max(axis=1)

    return ind


# ── STRATEGY IMPLEMENTATIONS ──────────────────────────────────────────────────

def strat_ema_momentum(df, ind, i):
    """Strategy 1: EMA Momentum (5-day) — "The Sprint"
    Source: QuantifiedStrategies.com — 145% CAGR backtested
    LONG when Close > 5-day EMA. CASH when Close < 5-day EMA.
    """
    if pd.isna(ind['ema5'].iloc[i]):
        return 0
    return 1 if df['Close'].iloc[i] > ind['ema5'].iloc[i] else 0


def strat_golden_cross(df, ind, i):
    """Strategy 2: Golden Cross (20d/100d SMA) — "The Grayscale"
    Source: Grayscale Research — 116% CAGR, 1.7 Sharpe
    LONG when 20-SMA > 100-SMA. CASH otherwise.
    """
    if pd.isna(ind['sma100'].iloc[i]):
        return 0
    return 1 if ind['sma20'].iloc[i] > ind['sma100'].iloc[i] else 0


def strat_filtered_trend(df, ind, i):
    """Strategy 3: EMA Trend + Volatility Filter — "The Filtered Trend"
    Source: YouQuant / academic research — Sharpe > 2
    10-EMA/30-EMA crossover. Only trade when ATR > 1.5x its 50-day average.
    """
    if pd.isna(ind['ema30'].iloc[i]) or pd.isna(ind['atr_avg50'].iloc[i]):
        return 0
    if ind['atr_avg50'].iloc[i] == 0:
        return 0

    high_vol = ind['atr14'].iloc[i] > 1.5 * ind['atr_avg50'].iloc[i]
    trend_up = ind['ema10'].iloc[i] > ind['ema30'].iloc[i]

    if high_vol and trend_up:
        return 1   # LONG
    elif high_vol and not trend_up:
        return -1  # SHORT
    else:
        return 0   # CASH (low volatility — sit out)


def strat_macd_rsi(df, ind, i):
    """Strategy 4: MACD + RSI Combo — "The Confirmation"
    Source: Multiple backtests (Medium, GitHub, CoinGecko)
    LONG: MACD crosses above signal AND RSI > 50
    SHORT: MACD crosses below signal AND RSI < 50
    """
    if i < 1 or pd.isna(ind['macd_line'].iloc[i]):
        return 0

    macd_cross_up = (ind['macd_line'].iloc[i] > ind['macd_signal'].iloc[i] and
                     ind['macd_line'].iloc[i-1] <= ind['macd_signal'].iloc[i-1])
    macd_cross_down = (ind['macd_line'].iloc[i] < ind['macd_signal'].iloc[i] and
                       ind['macd_line'].iloc[i-1] >= ind['macd_signal'].iloc[i-1])
    macd_above = ind['macd_line'].iloc[i] > ind['macd_signal'].iloc[i]
    macd_below = ind['macd_line'].iloc[i] < ind['macd_signal'].iloc[i]

    rsi_val = ind['rsi14'].iloc[i]

    if macd_above and rsi_val > 50:
        return 1
    elif macd_below and rsi_val < 50:
        return -1
    else:
        return 0


def strat_supertrend(df, ind, i):
    """Strategy 5: SuperTrend Trend Follower — "The Rider"
    Source: Popular crypto algo strategy
    LONG when SuperTrend direction = 1. SHORT when direction = -1.
    """
    if pd.isna(ind['st_direction'].iloc[i]):
        return 0
    return int(ind['st_direction'].iloc[i])  # 1 = long, -1 = short


def strat_dual_thrust(df, ind, i):
    """Strategy 6: Dual Thrust / Range Breakout — "The Breakout King"
    Source: Michael Chalek / Larry Williams
    Upper = Open + k*range. Lower = Open - k*range. k=0.5, N=4 days.
    """
    if pd.isna(ind['dt_range_4'].iloc[i]):
        return 0

    k = 0.5
    range_val = ind['dt_range_4'].iloc[i]
    open_price = df['Open'].iloc[i]
    close_price = df['Close'].iloc[i]

    upper = open_price + k * range_val
    lower = open_price - k * range_val

    if close_price > upper:
        return 1   # LONG breakout
    elif close_price < lower:
        return -1  # SHORT breakdown
    else:
        return 0   # Inside range


def strat_triple_screen(df, ind, i):
    """Strategy 7: Multi-Timeframe Momentum — "The Triple Screen"
    Source: Elder's Triple Screen adapted for crypto
    Weekly trend (50-EMA of weekly) sets bias.
    Daily RSI pullback for entry: RSI < 40 in uptrend = buy, RSI > 60 in downtrend = sell.
    """
    if pd.isna(ind['weekly_ema50'].iloc[i]) or pd.isna(ind['rsi14'].iloc[i]):
        return 0

    weekly_trend_up = ind['weekly_close'].iloc[i] > ind['weekly_ema50'].iloc[i]
    rsi_val = ind['rsi14'].iloc[i]

    if weekly_trend_up and rsi_val < 40:
        return 1   # Buy pullback in uptrend
    elif not weekly_trend_up and rsi_val > 60:
        return -1  # Sell rally in downtrend
    elif weekly_trend_up:
        return 1   # Stay long in uptrend
    elif not weekly_trend_up:
        return -1  # Stay short in downtrend
    else:
        return 0


# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────

ALL_STRATEGIES = {
    '1. EMA Momentum (5d)':      strat_ema_momentum,
    '2. Golden Cross (20/100)':  strat_golden_cross,
    '3. Filtered Trend (Vol)':   strat_filtered_trend,
    '4. MACD + RSI Combo':       strat_macd_rsi,
    '5. SuperTrend Rider':       strat_supertrend,
    '6. Dual Thrust Breakout':   strat_dual_thrust,
    '7. Triple Screen (MTF)':    strat_triple_screen,
}


def backtest(df, ind, strategy_fn, capital=INITIAL_CAPITAL):
    """
    Backtest a strategy with full capital deployment, compounding, and per-trade tracking.
    Returns comprehensive metrics including win rate, profit factor, per-trade details.
    """
    start_idx = 120  # Skip warmup for all indicators
    equity = capital
    peak = capital
    max_dd = 0
    position = 0  # 0=cash, 1=long, -1=short
    equity_curve = []
    yearly_pnl = defaultdict(float)

    # Per-trade tracking
    trade_list = []
    entry_price = 0
    entry_date = None
    entry_equity = capital
    direction = 0

    for i in range(start_idx, len(df)):
        signal = strategy_fn(df, ind, i)

        # Position change
        if signal != position:
            # Close existing position — record the trade
            if position != 0 and i > start_idx:
                exit_price = df['Close'].iloc[i]
                equity *= (1 - FEE_RATE)  # closing fee

                if position == 1:
                    trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    trade_pnl_pct = (entry_price - exit_price) / entry_price * 100

                trade_pnl = equity - entry_equity  # actual equity change
                trade_list.append({
                    'entry_date': entry_date,
                    'exit_date': df['Date'].iloc[i],
                    'direction': 'LONG' if position == 1 else 'SHORT',
                    'entry_price': round(entry_price, 2),
                    'exit_price': round(exit_price, 2),
                    'pnl_pct': round(trade_pnl_pct, 2),
                    'pnl': round(trade_pnl, 2),
                    'equity_after': round(equity, 2),
                })

            # Open new position
            if signal != 0:
                equity *= (1 - FEE_RATE)  # opening fee
                entry_price = df['Close'].iloc[i]
                entry_date = df['Date'].iloc[i]
                entry_equity = equity
                direction = signal

            position = signal

        # Apply daily return if in position
        elif position != 0 and i > start_idx:
            prev_close = df['Close'].iloc[i-1]
            cur_close = df['Close'].iloc[i]

            if position == 1:
                daily_ret = (cur_close - prev_close) / prev_close
            else:  # short
                daily_ret = (prev_close - cur_close) / prev_close

            equity *= (1 + daily_ret)
            yearly_pnl[df['Date'].iloc[i].year] += equity * daily_ret

        # Track drawdown
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    # Close final position if open
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
        })

    # Calculate comprehensive metrics
    total_return = (equity - capital) / capital * 100
    years = (df['Date'].iloc[-1] - df['Date'].iloc[start_idx]).days / 365.25
    cagr = ((equity / capital) ** (1 / max(years, 0.1)) - 1) * 100 if equity > 0 else -100

    # Sharpe ratio (annualized from daily returns)
    if len(equity_curve) > 1:
        daily_rets = pd.Series(equity_curve).pct_change().dropna()
        sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(365)
    else:
        sharpe = 0

    # Per-trade metrics
    num_trades = len(trade_list)
    winning_trades = [t for t in trade_list if t['pnl'] > 0]
    losing_trades = [t for t in trade_list if t['pnl'] <= 0]
    num_wins = len(winning_trades)
    num_losses = len(losing_trades)
    win_rate = (num_wins / num_trades * 100) if num_trades > 0 else 0

    gross_profit = sum(t['pnl'] for t in winning_trades) if winning_trades else 0
    gross_loss = abs(sum(t['pnl'] for t in losing_trades)) if losing_trades else 0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.99 if gross_profit > 0 else 0)

    avg_win = (gross_profit / num_wins) if num_wins > 0 else 0
    avg_loss = (gross_loss / num_losses) if num_losses > 0 else 0
    avg_win_pct = np.mean([t['pnl_pct'] for t in winning_trades]) if winning_trades else 0
    avg_loss_pct = np.mean([abs(t['pnl_pct']) for t in losing_trades]) if losing_trades else 0

    best_trade = max(trade_list, key=lambda t: t['pnl'])['pnl'] if trade_list else 0
    worst_trade = min(trade_list, key=lambda t: t['pnl'])['pnl'] if trade_list else 0
    best_trade_pct = max(trade_list, key=lambda t: t['pnl_pct'])['pnl_pct'] if trade_list else 0
    worst_trade_pct = min(trade_list, key=lambda t: t['pnl_pct'])['pnl_pct'] if trade_list else 0

    # Win/loss streaks
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for t in trade_list:
        if t['pnl'] > 0:
            cur_win += 1; cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    # Calmar ratio
    calmar = (cagr / max_dd) if max_dd > 0 else 0

    # Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss) if num_trades > 0 else 0

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
        'gross_profit': round(gross_profit, 2),
        'gross_loss': round(gross_loss, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'avg_win_pct': round(avg_win_pct, 2),
        'avg_loss_pct': round(avg_loss_pct, 2),
        'best_trade': round(best_trade, 2),
        'worst_trade': round(worst_trade, 2),
        'best_trade_pct': round(best_trade_pct, 2),
        'worst_trade_pct': round(worst_trade_pct, 2),
        'max_win_streak': max_win_streak,
        'max_loss_streak': max_loss_streak,
        'expectancy': round(expectancy, 2),
        'yearly_pnl': dict(yearly_pnl),
        'trade_list': trade_list,
    }


def buy_and_hold(df, capital=INITIAL_CAPITAL):
    """Calculate buy-and-hold benchmark."""
    start_idx = 120
    start_price = df['Close'].iloc[start_idx]
    end_price = df['Close'].iloc[-1]
    equity = capital * (end_price / start_price)
    total_return = (equity - capital) / capital * 100
    years = (df['Date'].iloc[-1] - df['Date'].iloc[start_idx]).days / 365.25
    cagr = ((equity / capital) ** (1 / max(years, 0.1)) - 1) * 100

    # Drawdown
    prices = df['Close'].iloc[start_idx:]
    cum_max = prices.cummax()
    dd = ((cum_max - prices) / cum_max * 100).max()

    # Sharpe
    daily_rets = df['Close'].iloc[start_idx:].pct_change().dropna()
    sharpe = (daily_rets.mean() / max(daily_rets.std(), 1e-10)) * np.sqrt(365)

    return {
        'final_equity': round(equity, 2),
        'total_return': round(total_return, 2),
        'cagr': round(cagr, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(dd, 2),
        'trades': 1,
    }


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 110)
    print("  CRYPTO FUTURES BACKTEST — 7 WORLD-CLASS STRATEGIES vs BUY-AND-HOLD")
    print("  Pairs: BTCUSDT | ETHUSDT | SOLUSDT")
    print("  Data: 5 Years Daily (Binance) | Capital: Rs 3,00,000 | Fees: 0.1%/trade")
    print("=" * 110)

    # ── FETCH DATA ──
    print("\n--- FETCHING 5-YEAR DATA ---")
    data = {}
    indicators = {}
    for sym in SYMBOLS:
        df = fetch_binance_data(sym, '1d', 5)
        if df is not None and len(df) > 200:
            data[sym] = df
            indicators[sym] = precompute(df)

    if not data:
        print("ERROR: No data fetched!")
        return

    # ── BUY-AND-HOLD BENCHMARK ──
    print("\n--- BUY-AND-HOLD BENCHMARK ---")
    benchmarks = {}
    for sym in SYMBOLS:
        if sym in data:
            bh = buy_and_hold(data[sym])
            benchmarks[sym] = bh
            print(f"  {sym:10s}: Return={bh['total_return']:>+10.1f}%  CAGR={bh['cagr']:>+8.1f}%  "
                  f"Sharpe={bh['sharpe']:>5.2f}  MaxDD={bh['max_drawdown']:>6.1f}%  "
                  f"Final=Rs {bh['final_equity']:>15,.2f}")

    # ── BACKTEST ALL STRATEGIES ──
    print("\n--- BACKTESTING 7 STRATEGIES (with detailed metrics) ---")
    results = {}
    csv_rows = []

    for strat_name, strat_fn in ALL_STRATEGIES.items():
        results[strat_name] = {}

        for sym in SYMBOLS:
            if sym not in data:
                continue

            res = backtest(data[sym], indicators[sym], strat_fn)
            results[strat_name][sym] = res
            bh = benchmarks[sym]

            csv_rows.append({
                'Instrument': sym,
                'Strategy': strat_name,
                'Num_Trades': res['num_trades'],
                'Wins': res['wins'],
                'Losses': res['losses'],
                'Win_Rate_%': res['win_rate'],
                'Total_PnL': res['total_pnl'],
                'Total_Return_%': res['total_return'],
                'CAGR_%': res['cagr'],
                'Sharpe': res['sharpe'],
                'Max_Drawdown_%': res['max_drawdown'],
                'Calmar': res['calmar'],
                'Profit_Factor': res['profit_factor'],
                'Gross_Profit': res['gross_profit'],
                'Gross_Loss': res['gross_loss'],
                'Avg_Win': res['avg_win'],
                'Avg_Loss': res['avg_loss'],
                'Avg_Win_%': res['avg_win_pct'],
                'Avg_Loss_%': res['avg_loss_pct'],
                'Best_Trade': res['best_trade'],
                'Worst_Trade': res['worst_trade'],
                'Best_Trade_%': res['best_trade_pct'],
                'Worst_Trade_%': res['worst_trade_pct'],
                'Max_Win_Streak': res['max_win_streak'],
                'Max_Loss_Streak': res['max_loss_streak'],
                'Expectancy': res['expectancy'],
                'Final_Equity': res['final_equity'],
                'BH_CAGR_%': bh['cagr'],
                'Beats_BH': 'YES' if res['cagr'] > bh['cagr'] else 'NO',
            })

    # Save to CSV
    import os
    REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
    os.makedirs(REPORT_DIR, exist_ok=True)
    csv_df = pd.DataFrame(csv_rows)
    csv_path = os.path.join(REPORT_DIR, 'crypto_backtest_results.csv')
    csv_df.to_csv(csv_path, index=False)
    print(f"\n  Detailed results saved to: {csv_path}")

    # ── DETAILED METRICS TABLE ──
    print("\n" + "=" * 140)
    print("  DETAILED BACKTEST METRICS — ALL STRATEGIES x ALL INSTRUMENTS")
    print("=" * 140)

    header = (f"{'Instrument':<10} {'Strategy':<26} {'Trades':>6} {'WinR%':>6} {'PnL':>14} "
              f"{'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'PF':>6} {'AvgW%':>7} {'AvgL%':>7} "
              f"{'Best%':>8} {'Worst%':>8} {'WStr':>5} {'LStr':>5} {'B&H?':>5}")
    print(header)
    print("-" * 140)

    for strat_name in ALL_STRATEGIES:
        for sym in SYMBOLS:
            if sym not in results[strat_name]:
                continue
            r = results[strat_name][sym]
            bh = benchmarks[sym]
            beats = "YES" if r['cagr'] > bh['cagr'] else "no"
            print(f"  {sym:<10} {strat_name:<26} {r['num_trades']:>5} {r['win_rate']:>5.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['cagr']:>+7.1f}% {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% {r['profit_factor']:>5.1f} {r['avg_win_pct']:>+6.1f}% "
                  f"{r['avg_loss_pct']:>6.1f}% {r['best_trade_pct']:>+7.1f}% "
                  f"{r['worst_trade_pct']:>+7.1f}% {r['max_win_streak']:>4} {r['max_loss_streak']:>4} "
                  f"{beats:>5}")
        print()  # blank line between strategies

    # ── AGGREGATE RANKING ──
    print("=" * 140)
    print("  STRATEGY RANKING — SORTED BY AVERAGE CAGR")
    print("=" * 140)

    ranking = []
    for strat_name in ALL_STRATEGIES:
        cagrs, sharpes, max_dds, win_rates, pfs = [], [], [], [], []
        beats_count = total_symbols = 0
        total_trades = 0

        for sym in SYMBOLS:
            if sym in results[strat_name]:
                r = results[strat_name][sym]
                cagrs.append(r['cagr'])
                sharpes.append(r['sharpe'])
                max_dds.append(r['max_drawdown'])
                win_rates.append(r['win_rate'])
                pfs.append(r['profit_factor'])
                total_trades += r['num_trades']
                if r['cagr'] > benchmarks[sym]['cagr']:
                    beats_count += 1
                total_symbols += 1

        ranking.append({
            'strategy': strat_name,
            'avg_cagr': np.mean(cagrs),
            'avg_sharpe': np.mean(sharpes),
            'worst_dd': max(max_dds),
            'avg_win_rate': np.mean(win_rates),
            'avg_pf': np.mean(pfs),
            'total_trades': total_trades,
            'beats_bh': f"{beats_count}/{total_symbols}",
        })

    ranking.sort(key=lambda x: x['avg_cagr'], reverse=True)

    bh_cagrs = [benchmarks[s]['cagr'] for s in SYMBOLS if s in benchmarks]
    bh_sharpes = [benchmarks[s]['sharpe'] for s in SYMBOLS if s in benchmarks]
    bh_dds = [benchmarks[s]['max_drawdown'] for s in SYMBOLS if s in benchmarks]

    print(f"\n{'Rank':<5} {'Strategy':<28} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>7} "
          f"{'WinR%':>7} {'PF':>6} {'Trades':>7} {'B&H':>6}")
    print("-" * 90)

    for rank, s in enumerate(ranking, 1):
        marker = " *** WINNER" if rank == 1 else ""
        print(f"  {rank:<3} {s['strategy']:<28} {s['avg_cagr']:>+7.1f}% {s['avg_sharpe']:>7.2f} "
              f"{s['worst_dd']:>6.1f}% {s['avg_win_rate']:>6.1f}% {s['avg_pf']:>5.1f} "
              f"{s['total_trades']:>6} {s['beats_bh']:>5}{marker}")

    print(f"  {'---':3} {'BUY & HOLD (benchmark)':<28} {np.mean(bh_cagrs):>+7.1f}% "
          f"{np.mean(bh_sharpes):>7.2f} {max(bh_dds):>6.1f}%  {'---':>5}   {'---':>4}    {'1':>4}   {'---':>4}")

    # ── FINAL VERDICT ──
    winner = ranking[0]
    bh_avg = np.mean(bh_cagrs)

    print(f"\n{'='*140}")
    print(f"  FINAL VERDICT")
    print(f"{'='*140}")
    print(f"\n  WINNER: {winner['strategy']}")
    print(f"  Average CAGR:      {winner['avg_cagr']:>+.1f}% vs Buy-Hold {bh_avg:>+.1f}%")
    print(f"  Average Sharpe:    {winner['avg_sharpe']:.2f}")
    print(f"  Average Win Rate:  {winner['avg_win_rate']:.1f}%")
    print(f"  Profit Factor:     {winner['avg_pf']:.2f}")
    print(f"  Worst Drawdown:    {winner['worst_dd']:.1f}%")
    print(f"  Total Trades:      {winner['total_trades']}")
    print(f"  Beats Buy-Hold:    {winner['beats_bh']} symbols")

    if winner['avg_cagr'] > bh_avg:
        diff = winner['avg_cagr'] - bh_avg
        print(f"\n  The {winner['strategy']} BEATS buy-and-hold by +{diff:.1f}% CAGR on average!")

    # Send detailed Telegram summary
    try:
        from trade_notifier import send_message
        msg = (
            f"<b>CRYPTO BACKTEST RESULTS (5Y)</b>\n\n"
            f"<b>Winner: {winner['strategy']}</b>\n"
            f"CAGR: {winner['avg_cagr']:+.1f}% | Sharpe: {winner['avg_sharpe']:.2f}\n"
            f"Win Rate: {winner['avg_win_rate']:.1f}% | PF: {winner['avg_pf']:.2f}\n"
            f"MaxDD: {winner['worst_dd']:.1f}% | Trades: {winner['total_trades']}\n"
            f"Beats B&H: {winner['beats_bh']}\n\n"
            f"<b>B&H Avg CAGR: {bh_avg:+.1f}%</b>\n\n"
        )
        for rank, s in enumerate(ranking, 1):
            msg += (f"{rank}. {s['strategy']}: CAGR {s['avg_cagr']:+.1f}% "
                    f"WR:{s['avg_win_rate']:.0f}% PF:{s['avg_pf']:.1f}\n")

        # Per-instrument details for top 3
        msg += f"\n<b>PER-INSTRUMENT (Top 3)</b>\n"
        for s in ranking[:3]:
            sn = s['strategy']
            for sym in SYMBOLS:
                if sym in results[sn]:
                    r = results[sn][sym]
                    msg += (f"{sym} {sn[:15]}: {r['num_trades']}tr "
                            f"WR:{r['win_rate']:.0f}% PF:{r['profit_factor']:.1f} "
                            f"CAGR:{r['cagr']:+.0f}%\n")

        send_message(msg)
        print("\n  Detailed results sent to Telegram!")
    except Exception as e:
        print(f"\n  Telegram send failed: {e}")

    print(f"\n{'='*140}")


if __name__ == "__main__":
    main()
