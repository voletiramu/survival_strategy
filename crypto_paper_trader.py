"""
CRYPTO PAPER TRADING SYSTEM
============================
Paper trading for BTCUSDT, ETHUSDT, SOLUSDT using Top 3 strategies:
  1. EMA Momentum (5d) - CAGR +594.9%, Sharpe 4.70, Winner
  2. MACD + RSI Combo  - CAGR +239.1%, Sharpe 2.18
  3. SuperTrend Rider  - CAGR +121.5%, Sharpe 1.32

Data Source: Binance Public API (free, no API key needed)
Market: 24/7 (crypto never sleeps)
Capital: Rs 3,00,000 per strategy allocation

Usage:
    python crypto_paper_trader.py              # Run continuous (24/7)
    python crypto_paper_trader.py --once       # Single scan
    python crypto_paper_trader.py --status     # Show portfolio
    python crypto_paper_trader.py --reset      # Reset portfolio
"""

import sys
import os
import json
import time
import signal
import logging
import argparse
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_crypto')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(PAPER_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

INITIAL_CAPITAL = 300000  # Rs 3,00,000
BINANCE_BASE = "https://api.binance.com/api/v3"
FEE_RATE = 0.001  # 0.1% per trade

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
SYMBOL_NAMES = {'BTCUSDT': 'Bitcoin', 'ETHUSDT': 'Ethereum', 'SOLUSDT': 'Solana'}

# Strategy allocation (equal weight among 3 strategies)
STRATEGY_ALLOCATION = {
    'EMA Momentum': 1.0 / 3,
    'MACD+RSI': 1.0 / 3,
    'SuperTrend': 1.0 / 3,
}

STATE_FILE = os.path.join(PAPER_DIR, 'crypto_portfolio_state.json')
SIGNAL_LOG = os.path.join(LOG_DIR, 'crypto_signal_log.csv')
TRADE_LOG = os.path.join(LOG_DIR, 'crypto_trading_log.csv')

today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'crypto_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('CryptoPaperTrader')


# ====================================================================
# INDICATOR HELPERS (same as backtest)
# ====================================================================
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

def supertrend_calc(df, period=10, multiplier=3.0):
    hl2 = (df['High'] + df['Low']) / 2
    atr_val = atr(df, period)
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


# ====================================================================
# DATA FETCHING
# ====================================================================
def fetch_current_price(symbol):
    """Get current price from Binance."""
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price", params={'symbol': symbol}, timeout=10)
        data = r.json()
        return float(data['price'])
    except Exception as e:
        logger.error(f"Price fetch error {symbol}: {e}")
        return None


def fetch_daily_candles(symbol, limit=200):
    """Fetch last N daily candles from Binance."""
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                        params={'symbol': symbol, 'interval': '1d', 'limit': limit},
                        timeout=15)
        data = r.json()
        if not data or isinstance(data, dict):
            return None

        df = pd.DataFrame(data, columns=[
            'timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
            'close_time', 'quote_vol', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['Date'] = pd.to_datetime(df['timestamp'], unit='ms')
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = df[col].astype(float)
        df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].drop_duplicates('Date')
        df = df.sort_values('Date').reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"Candle fetch error {symbol}: {e}")
        return None


def compute_indicators(df):
    """Compute all indicators needed for 3 strategies."""
    ind = {}
    ind['ema5'] = ema(df['Close'], 5)
    ind['ema10'] = ema(df['Close'], 10)
    ind['ema30'] = ema(df['Close'], 30)

    ind['rsi14'] = rsi(df['Close'], 14)
    ind['macd_line'], ind['macd_signal'], ind['macd_hist'] = macd(df['Close'])

    ind['atr14'] = atr(df, 14)
    ind['supertrend'], ind['st_direction'] = supertrend_calc(df, 10, 3.0)

    return ind


# ====================================================================
# STRATEGY SIGNAL GENERATORS
# ====================================================================
def signal_ema_momentum(df, ind):
    """EMA Momentum: LONG when Close > 5-day EMA, else CASH."""
    i = len(df) - 1
    if pd.isna(ind['ema5'].iloc[i]):
        return 0, "EMA5 not ready"
    if df['Close'].iloc[i] > ind['ema5'].iloc[i]:
        return 1, f"Close {df['Close'].iloc[i]:.2f} > EMA5 {ind['ema5'].iloc[i]:.2f}"
    else:
        return 0, f"Close {df['Close'].iloc[i]:.2f} < EMA5 {ind['ema5'].iloc[i]:.2f}"


def signal_macd_rsi(df, ind):
    """MACD+RSI: LONG when MACD above signal AND RSI>50, SHORT when below."""
    i = len(df) - 1
    if pd.isna(ind['macd_line'].iloc[i]):
        return 0, "MACD not ready"

    macd_above = ind['macd_line'].iloc[i] > ind['macd_signal'].iloc[i]
    rsi_val = ind['rsi14'].iloc[i]

    if macd_above and rsi_val > 50:
        return 1, f"MACD bullish + RSI {rsi_val:.1f} > 50"
    elif not macd_above and rsi_val < 50:
        return -1, f"MACD bearish + RSI {rsi_val:.1f} < 50"
    else:
        return 0, f"Mixed: MACD {'above' if macd_above else 'below'}, RSI {rsi_val:.1f}"


def signal_supertrend(df, ind):
    """SuperTrend: LONG when direction=1, SHORT when direction=-1."""
    i = len(df) - 1
    if pd.isna(ind['st_direction'].iloc[i]):
        return 0, "SuperTrend not ready"
    direction = int(ind['st_direction'].iloc[i])
    return direction, f"SuperTrend direction={direction}, ST={ind['supertrend'].iloc[i]:.2f}"


STRATEGIES = {
    'EMA Momentum': signal_ema_momentum,
    'MACD+RSI': signal_macd_rsi,
    'SuperTrend': signal_supertrend,
}


# ====================================================================
# PORTFOLIO MANAGEMENT
# ====================================================================
class CryptoPortfolio:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.positions = []
        self.closed_trades = []
        self.daily_pnl = {}
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self.capital = state.get('capital', INITIAL_CAPITAL)
                self.positions = state.get('positions', [])
                self.closed_trades = state.get('closed_trades', [])
                self.daily_pnl = state.get('daily_pnl', {})
            except Exception as e:
                logger.warning(f"Load state error: {e}")

    def save_state(self):
        state = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat(),
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def get_position(self, symbol, strategy):
        """Find existing position for symbol+strategy."""
        for p in self.positions:
            if p['symbol'] == symbol and p['strategy'] == strategy:
                return p
        return None

    def open_position(self, symbol, strategy, direction, price, reason):
        """Open a new position."""
        # Check if position already exists
        existing = self.get_position(symbol, strategy)
        if existing:
            if existing['direction'] == direction:
                return None  # Already in same direction
            else:
                # Close existing and open opposite
                self.close_position(symbol, strategy, price, "Signal reversal")

        # Calculate allocation
        allocation = self.capital * STRATEGY_ALLOCATION.get(strategy, 0.33)
        allocation_after_fee = allocation * (1 - FEE_RATE)
        quantity = allocation_after_fee / price  # fractional crypto units

        position = {
            'id': f"{strategy}_{symbol}_{datetime.now().strftime('%H%M%S')}",
            'symbol': symbol,
            'strategy': strategy,
            'direction': direction,  # 1=LONG, -1=SHORT
            'entry_price': price,
            'current_price': price,
            'quantity': quantity,
            'allocation': allocation,
            'entry_time': datetime.now().isoformat(),
            'unrealized_pnl': 0,
            'status': 'OPEN',
            'reason': reason,
        }
        self.positions.append(position)
        self.save_state()

        # Telegram notification
        try:
            from trade_notifier import send_message
            dir_text = "LONG" if direction == 1 else "SHORT"
            emoji = "🟢" if direction == 1 else "🔴"
            msg = (
                f"<b>{emoji} CRYPTO TRADE ENTRY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Strategy:</b> {strategy}\n"
                f"<b>Symbol:</b> {symbol} ({SYMBOL_NAMES.get(symbol, '')})\n"
                f"<b>Direction:</b> {dir_text}\n"
                f"<b>Entry Price:</b> ${price:,.2f}\n"
                f"<b>Allocation:</b> Rs {allocation:,.0f}\n"
                f"<b>Qty:</b> {quantity:.6f}\n"
                f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"<b>Reason:</b> {reason}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            send_message(msg)
        except Exception as e:
            logger.warning(f"Telegram entry notify failed: {e}")

        return position

    def close_position(self, symbol, strategy, current_price, reason):
        """Close an existing position."""
        pos = self.get_position(symbol, strategy)
        if not pos:
            return None

        # Calculate PnL
        if pos['direction'] == 1:  # LONG
            pnl_pct = (current_price - pos['entry_price']) / pos['entry_price'] * 100
        else:  # SHORT
            pnl_pct = (pos['entry_price'] - current_price) / pos['entry_price'] * 100

        pnl = pos['allocation'] * (pnl_pct / 100)
        pnl -= pos['allocation'] * FEE_RATE  # exit fee

        # Update capital
        self.capital += pnl

        # Move to closed
        pos['status'] = 'CLOSED'
        pos['exit_price'] = current_price
        pos['exit_time'] = datetime.now().isoformat()
        pos['pnl'] = round(pnl, 2)
        pos['pnl_pct'] = round(pnl_pct, 2)
        pos['exit_reason'] = reason
        self.closed_trades.append(pos)
        self.positions = [p for p in self.positions if p['id'] != pos['id']]

        # Track daily PnL
        today = datetime.now().strftime('%Y-%m-%d')
        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl

        self.save_state()

        # Log to CSV
        self._log_trade(pos)

        # Telegram notification
        try:
            from trade_notifier import send_message
            pnl_emoji = "💰" if pnl > 0 else "📉"
            msg = (
                f"<b>{pnl_emoji} CRYPTO TRADE EXIT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Strategy:</b> {strategy}\n"
                f"<b>Symbol:</b> {symbol}\n"
                f"<b>Entry:</b> ${pos['entry_price']:,.2f}\n"
                f"<b>Exit:</b> ${current_price:,.2f}\n"
                f"<b>PnL:</b> Rs {pnl:,.2f} ({pnl_pct:+.1f}%)\n"
                f"<b>Capital:</b> Rs {self.capital:,.0f}\n"
                f"<b>Reason:</b> {reason}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            send_message(msg)
        except Exception as e:
            logger.warning(f"Telegram exit notify failed: {e}")

        return pos

    def update_prices(self, prices):
        """Update current prices and unrealized PnL."""
        for pos in self.positions:
            if pos['symbol'] in prices:
                current_price = prices[pos['symbol']]
                pos['current_price'] = current_price
                if pos['direction'] == 1:
                    pnl_pct = (current_price - pos['entry_price']) / pos['entry_price'] * 100
                else:
                    pnl_pct = (pos['entry_price'] - current_price) / pos['entry_price'] * 100
                pos['unrealized_pnl'] = round(pos['allocation'] * pnl_pct / 100, 2)
        self.save_state()

    def _log_trade(self, trade):
        """Log trade to CSV."""
        header_needed = not os.path.exists(TRADE_LOG) or os.path.getsize(TRADE_LOG) == 0
        with open(TRADE_LOG, 'a') as f:
            if header_needed:
                f.write("timestamp,symbol,strategy,direction,entry_price,exit_price,"
                        "pnl,pnl_pct,allocation,entry_time,exit_time,reason\n")
            f.write(f"{datetime.now().isoformat()},{trade['symbol']},{trade['strategy']},"
                    f"{'LONG' if trade['direction']==1 else 'SHORT'},{trade['entry_price']},"
                    f"{trade.get('exit_price','')},"
                    f"{trade.get('pnl',0)},{trade.get('pnl_pct',0)},{trade['allocation']},"
                    f"{trade['entry_time']},{trade.get('exit_time','')},{trade.get('exit_reason','')}\n")

    def print_status(self):
        """Print portfolio status."""
        print(f"\n{'='*70}")
        print(f"  CRYPTO PORTFOLIO STATUS")
        print(f"{'='*70}")
        print(f"  Capital: Rs {self.capital:,.0f}")
        print(f"  Open Positions: {len(self.positions)}")
        print(f"  Closed Trades: {len(self.closed_trades)}")

        total_unrealized = 0
        if self.positions:
            print(f"\n  {'Symbol':<10} {'Strategy':<16} {'Dir':<6} {'Entry':>12} {'Current':>12} {'PnL':>12}")
            print(f"  {'-'*68}")
            for p in self.positions:
                d = "LONG" if p['direction'] == 1 else "SHORT"
                total_unrealized += p.get('unrealized_pnl', 0)
                print(f"  {p['symbol']:<10} {p['strategy']:<16} {d:<6} "
                      f"${p['entry_price']:>10,.2f} ${p['current_price']:>10,.2f} "
                      f"Rs {p.get('unrealized_pnl', 0):>+10,.0f}")

        print(f"\n  Unrealized PnL: Rs {total_unrealized:+,.0f}")

        if self.closed_trades:
            total_pnl = sum(t.get('pnl', 0) for t in self.closed_trades)
            wins = sum(1 for t in self.closed_trades if t.get('pnl', 0) > 0)
            wr = wins / len(self.closed_trades) * 100 if self.closed_trades else 0
            print(f"  Realized PnL:   Rs {total_pnl:+,.0f}")
            print(f"  Win Rate:       {wr:.0f}% ({wins}/{len(self.closed_trades)})")

        print(f"{'='*70}")


# ====================================================================
# SIGNAL LOGGING
# ====================================================================
def log_signals(symbol, price, indicators, signals):
    """Log scan data to crypto_signal_log.csv."""
    header_needed = not os.path.exists(SIGNAL_LOG) or os.path.getsize(SIGNAL_LOG) == 0
    ind = indicators
    i = -1  # last row

    row = {
        'scan_time': datetime.now().isoformat(),
        'symbol': symbol,
        'price': price,
        'ema5': round(ind['ema5'].iloc[i], 2) if not pd.isna(ind['ema5'].iloc[i]) else '',
        'ema10': round(ind['ema10'].iloc[i], 2) if not pd.isna(ind['ema10'].iloc[i]) else '',
        'ema30': round(ind['ema30'].iloc[i], 2) if not pd.isna(ind['ema30'].iloc[i]) else '',
        'rsi14': round(ind['rsi14'].iloc[i], 2) if not pd.isna(ind['rsi14'].iloc[i]) else '',
        'macd': round(ind['macd_line'].iloc[i], 4) if not pd.isna(ind['macd_line'].iloc[i]) else '',
        'macd_signal': round(ind['macd_signal'].iloc[i], 4) if not pd.isna(ind['macd_signal'].iloc[i]) else '',
        'macd_hist': round(ind['macd_hist'].iloc[i], 4) if not pd.isna(ind['macd_hist'].iloc[i]) else '',
        'atr14': round(ind['atr14'].iloc[i], 2) if not pd.isna(ind['atr14'].iloc[i]) else '',
        'supertrend': round(ind['supertrend'].iloc[i], 2) if not pd.isna(ind['supertrend'].iloc[i]) else '',
        'st_direction': int(ind['st_direction'].iloc[i]) if not pd.isna(ind['st_direction'].iloc[i]) else '',
    }

    # Add strategy signals
    for sname, (sig, reason) in signals.items():
        row[f'signal_{sname}'] = sig

    with open(SIGNAL_LOG, 'a') as f:
        if header_needed:
            f.write(','.join(row.keys()) + '\n')
        f.write(','.join(str(v) for v in row.values()) + '\n')


# ====================================================================
# MAIN SCANNER
# ====================================================================
def run_scan(portfolio):
    """Run a single scan across all symbols and strategies."""
    logger.info(f"\n{'='*60}")
    logger.info(f"CRYPTO SCAN | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}")

    prices = {}
    for symbol in SYMBOLS:
        price = fetch_current_price(symbol)
        if price:
            prices[symbol] = price
            logger.info(f"  {symbol}: ${price:,.2f}")
        time.sleep(0.2)

    if not prices:
        logger.error("No prices fetched!")
        return

    # Update portfolio prices
    portfolio.update_prices(prices)

    for symbol in SYMBOLS:
        if symbol not in prices:
            continue

        # Fetch daily candles for indicator calculation
        df = fetch_daily_candles(symbol, 200)
        if df is None or len(df) < 50:
            logger.warning(f"  {symbol}: Not enough candle data")
            continue

        # Compute indicators
        ind = compute_indicators(df)

        # Generate signals
        signals = {}
        for strat_name, strat_fn in STRATEGIES.items():
            sig, reason = strat_fn(df, ind)
            signals[strat_name] = (sig, reason)

            # Check for position changes
            existing = portfolio.get_position(symbol, strat_name)
            current_dir = existing['direction'] if existing else 0

            if sig != current_dir:
                # Close existing if direction changed
                if existing:
                    portfolio.close_position(symbol, strat_name, prices[symbol],
                                           f"Signal change to {'LONG' if sig==1 else 'SHORT' if sig==-1 else 'CASH'}")
                    logger.info(f"  CLOSED {symbol} {strat_name}: {reason}")

                # Open new if signal is non-zero
                if sig != 0:
                    pos = portfolio.open_position(symbol, strat_name, sig, prices[symbol], reason)
                    if pos:
                        dir_text = "LONG" if sig == 1 else "SHORT"
                        logger.info(f"  OPENED {symbol} {strat_name} {dir_text}: {reason}")

            else:
                if existing:
                    logger.info(f"  {symbol} {strat_name}: HOLD {'LONG' if sig==1 else 'SHORT'} — {reason}")
                else:
                    logger.info(f"  {symbol} {strat_name}: CASH — {reason}")

        # Log signals
        log_signals(symbol, prices[symbol], ind, signals)
        time.sleep(0.3)

    # Print status
    portfolio.print_status()


def run_continuous(interval=5):
    """Run crypto scanner continuously."""
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("\nShutting down crypto paper trader...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)

    portfolio = CryptoPortfolio()

    logger.info(f"Crypto paper trader started — scanning every {interval} minutes")
    logger.info(f"Symbols: {', '.join(SYMBOLS)}")
    logger.info(f"Strategies: {', '.join(STRATEGIES.keys())}")
    logger.info(f"Capital: Rs {portfolio.capital:,.0f}")
    logger.info(f"Market: 24/7 (no market hours restriction)")

    # Send Telegram startup notification
    try:
        from trade_notifier import send_message
        send_message(
            f"<b>CRYPTO PAPER TRADER STARTED</b>\n"
            f"Symbols: {', '.join(SYMBOLS)}\n"
            f"Strategies: EMA Momentum, MACD+RSI, SuperTrend\n"
            f"Capital: Rs {portfolio.capital:,.0f}\n"
            f"Scan interval: {interval} min"
        )
    except Exception:
        pass

    scan_count = 0
    while running:
        scan_count += 1
        logger.info(f"\n{'#'*60}")
        logger.info(f"SCAN #{scan_count}")
        logger.info(f"{'#'*60}")

        try:
            run_scan(portfolio)
        except Exception as e:
            logger.error(f"Scan error: {e}")
            import traceback
            traceback.print_exc()

        logger.info(f"\nNext scan in {interval} minutes...")
        time.sleep(interval * 60)


def main():
    print("\n" + "=" * 60)
    print("  CRYPTO PAPER TRADING SYSTEM")
    print("  " + "-" * 54)
    print("  BTCUSDT | ETHUSDT | SOLUSDT")
    print("  Strategies: EMA Momentum, MACD+RSI, SuperTrend")
    print("  Market: 24/7 | Capital: Rs 3,00,000")
    print("=" * 60)

    parser = argparse.ArgumentParser(description='Crypto Paper Trading')
    parser.add_argument('--once', action='store_true', help='Single scan')
    parser.add_argument('--status', action='store_true', help='Show portfolio')
    parser.add_argument('--reset', action='store_true', help='Reset portfolio')
    parser.add_argument('--interval', type=int, default=5, help='Scan interval (min)')
    args = parser.parse_args()

    portfolio = CryptoPortfolio()

    if args.reset:
        portfolio.capital = INITIAL_CAPITAL
        portfolio.positions = []
        portfolio.closed_trades = []
        portfolio.daily_pnl = {}
        portfolio.save_state()
        print("  Portfolio reset to Rs 3,00,000")
        return

    if args.status:
        portfolio.print_status()
        return

    if args.once:
        run_scan(portfolio)
        return

    run_continuous(args.interval)


if __name__ == '__main__':
    main()
