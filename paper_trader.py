"""
PAPER TRADING SYSTEM - ALL 5 STRATEGIES
========================================
Live paper trading with Angel One SmartAPI + Zerodha Sensibull (optional)
Runs all 5 strategies simultaneously on NIFTY, BANKNIFTY, SENSEX
Generates signals, tracks paper P&L, and logs everything

Usage:
    python paper_trader.py              # Run paper trading
    python paper_trader.py --backfill   # Backfill historical + run
    python paper_trader.py --status     # Show current positions
"""

import sys
import os
import json
import time
import signal
import threading
import logging
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OPTIONS_DIR = os.path.join(DATA_DIR, 'options')
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

for d in [PAPER_DIR, LOG_DIR, OPTIONS_DIR]:
    os.makedirs(d, exist_ok=True)

INITIAL_CAPITAL = 200000  # Rs 2L for equity (all strategies share this pool)
RISK_FREE_RATE = 0.065
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}  # NSE revised Jan 2026
MARGIN_PER_LOT = {'NIFTY': 180000, 'BANKNIFTY': 200000, 'SENSEX': 150000}  # Adjusted for new lot sizes
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18

# ====================================================================
# CAPITAL ALLOCATION & RISK MANAGEMENT
# ====================================================================
TOTAL_CAPITAL = 300000
EQUITY_CAPITAL = 200000        # Rs 2L for NIFTY, BANKNIFTY, SENSEX
COMMODITY_CAPITAL = 100000     # Rs 1L for GOLDM, SILVERM, CRUDEOILM
MAX_RISK_PCT = 25              # Max 25% of segment capital per trade
MAX_EQUITY_PER_TRADE = EQUITY_CAPITAL * MAX_RISK_PCT / 100    # Rs 50,000
MAX_COMMODITY_PER_TRADE = COMMODITY_CAPITAL * MAX_RISK_PCT / 100  # Rs 25,000
MAX_DAILY_LOSS_EQUITY = EQUITY_CAPITAL * 0.10    # 10% daily loss limit
MAX_DAILY_LOSS_COMMODITY = COMMODITY_CAPITAL * 0.10

# OI + IV Exit Thresholds
OI_SURGE_PCT = 15       # Exit if OI changes >15% from entry
OI_REVERSE_PCT = 25     # Reverse trade if OI changes >25%
IV_SPIKE_PCT = 25       # Exit if IV changes >25% from entry
IV_REVERSE_PCT = 40     # Reverse trade if IV changes >40%
OI_IV_COMBO_OI = 10     # Combined exit: OI >10% AND IV >15%
OI_IV_COMBO_IV = 15
GAMMA_SHIELD_THRESHOLD = 0.002  # Exit short positions if gamma > this

# Strategy weights from backtest (Sharpe-weighted)
STRATEGY_WEIGHTS = {
    'CPR': 0.88,          # 87.9% allocation (dominant)
    'Gamma Blast': 0.094, # 9.4%
    'Ghost Zone': 0.026,  # 2.7%
}

# Market hours (IST)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
PRE_MARKET = dtime(9, 0)

# Angel API config
ANGEL_CRED_FILE = os.environ.get('ANGEL_CRED_FILE', r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt")

# ====================================================================
# LOGGING SETUP
# ====================================================================
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'paper_trade_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('PaperTrader')


# ====================================================================
# BLACK-SCHOLES ENGINE
# ====================================================================
from scipy.stats import norm

def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option pricing with Greeks."""
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    S = max(S, 0.01)
    K = max(K, 0.01)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CE':
        price = S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
        delta = -norm.cdf(-d1)
    gamma = norm.pdf(d1) / (S*sigma*np.sqrt(T))
    theta = -(S*sigma*norm.pdf(d1))/(2*np.sqrt(T)) - r*K*np.exp(-r*T)*norm.cdf(d2 if opt_type=='CE' else -d2)
    theta /= 365
    vega = S*np.sqrt(T)*norm.pdf(d1) / 100
    return {'price': max(price, 0.05), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega}


def calc_costs(premium, qty, is_sell=False):
    """Real trading costs."""
    turnover = premium * qty
    brokerage = BROKERAGE * 2
    stt = turnover * STT_SELL if is_sell else 0
    exchange = turnover * EXCHANGE_CHARGES
    gst = brokerage * GST_RATE
    return brokerage + stt + exchange + gst


# ====================================================================
# ANGEL API CONNECTION
# ====================================================================
class AngelConnection:
    """Manages Angel One SmartAPI connection."""

    def __init__(self):
        self.obj = None
        self.session = None
        self.instruments = None
        self._connected = False

    def load_credentials(self):
        """Load Angel credentials."""
        creds = {}
        current_app = {}
        with open(ANGEL_CRED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    raw_val = val.strip()
                    if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                        parts = raw_val.split('#')
                        comment_val = parts[-1].strip()
                        main_val = parts[0].strip()
                        if 'your_' in main_val.lower() or 'secret' in main_val.lower():
                            val = comment_val
                        else:
                            val = main_val
                    else:
                        val = raw_val.split('#')[0].strip()
                    current_app[key] = val
                    if key == 'BROKER_NAME':
                        app_type = current_app.get('ANGEL_APP_TYPE', 'Unknown')
                        creds[app_type] = current_app.copy()
                        current_app = {}
        return creds

    def connect(self, app_type='Historical'):
        """Connect to Angel SmartAPI with retry on rate limit."""
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError:
            os.system("pip install smartapi-python pyotp logzero websocket-client")
            from SmartApi import SmartConnect
            import pyotp

        creds = self.load_credentials()
        app = creds.get(app_type, creds.get('Historical', {}))

        api_key = app.get('ANGEL_API_KEY', '')
        client = app.get('ANGEL_CLIENT_CODE', '')
        pin = app.get('ANGEL_PIN', '')
        totp_secret = app.get('ANGEL_TOTP_KEY', '')

        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to Angel One ({app_type})... attempt {attempt+1}/{max_retries}")
                self.obj = SmartConnect(api_key=api_key)
                totp = pyotp.TOTP(totp_secret).now()
                self.session = self.obj.generateSession(client, pin, totp)

                if self.session and self.session.get('status'):
                    self._connected = True
                    logger.info(f"Angel One connected: {client}")
                    return True
                else:
                    logger.warning(f"Angel login failed (attempt {attempt+1}): {self.session}")
            except Exception as e:
                logger.warning(f"Angel connection error (attempt {attempt+1}): {e}")

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s, 32s
                logger.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"Angel One connection failed after {max_retries} attempts")
        return False

    def get_ltp(self, exchange, symbol_token):
        """Get Last Traded Price."""
        if not self._connected:
            return None
        try:
            data = self.obj.ltpData(exchange, symbol_token, symbol_token)
            if data and data.get('data'):
                return data['data'].get('ltp')
        except Exception as e:
            logger.error(f"LTP error: {e}")
        return None

    def get_market_data(self, exchange, symbol_token):
        """Get full market data including OI."""
        if not self._connected:
            return None
        try:
            data = self.obj.getMarketData("FULL", {exchange: [symbol_token]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
        except Exception as e:
            logger.error(f"Market data error: {e}")
        return None

    def get_option_greeks(self, name, expiry_date):
        """Get option Greeks from Angel API."""
        if not self._connected:
            return None
        try:
            data = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_date
            })
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"Greeks error: {e}")
        return None

    def get_historical(self, exchange, token, interval, from_date, to_date):
        """Get historical candle data."""
        if not self._connected:
            return None
        try:
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date
            }
            data = self.obj.getCandleData(params)
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"Historical error: {e}")
        return None

    def load_instruments(self):
        """Load or download instrument master."""
        cache_file = os.path.join(OPTIONS_DIR, 'instrument_master.csv')
        if os.path.exists(cache_file):
            mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
            if (datetime.now() - mod_time).days < 1:
                self.instruments = pd.read_csv(cache_file, low_memory=False)
                logger.info(f"Loaded {len(self.instruments)} instruments from cache")
                return self.instruments

        logger.info("Downloading instrument master...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        import requests
        resp = requests.get(url, timeout=60)
        self.instruments = pd.DataFrame(resp.json())
        self.instruments.to_csv(cache_file, index=False)
        logger.info(f"Downloaded {len(self.instruments)} instruments")
        return self.instruments

    def find_token(self, symbol_name, exchange='NSE'):
        """Find symbol token from instrument master."""
        if self.instruments is None:
            self.load_instruments()
        mask = (self.instruments['name'] == symbol_name) & \
               (self.instruments['exch_seg'] == exchange)
        matches = self.instruments[mask]
        if len(matches) > 0:
            return str(matches.iloc[0]['token'])
        return None

    def find_option_tokens(self, name, expiry, strike, opt_type):
        """Find option contract token."""
        if self.instruments is None:
            self.load_instruments()

        exchange = 'BFO' if name == 'SENSEX' else 'NFO'
        mask = (self.instruments['name'] == name) & \
               (self.instruments['exch_seg'] == exchange) & \
               (self.instruments['instrumenttype'].isin(['OPTIDX', 'OPTSTK'])) & \
               (self.instruments['strike'].astype(float) == float(strike * 100))

        if opt_type:
            mask = mask & (self.instruments['symbol'].str.endswith(opt_type))

        matches = self.instruments[mask]
        if len(matches) > 0:
            return matches.iloc[0].to_dict()
        return None

    def get_pcr(self):
        """Get market PCR."""
        if not self._connected:
            return None
        try:
            data = self.obj.putCallRatio()
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"PCR error: {e}")
        return None


# ====================================================================
# PAPER POSITION TRACKER
# ====================================================================
class PaperPortfolio:
    """Track paper trading positions and P&L."""

    def __init__(self, capital=INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.positions = []      # Open positions
        self.closed_trades = []  # Completed trades
        self.signals = []        # All signals generated
        self.daily_pnl = {}      # Date -> PnL
        self._load_state()

    def _state_file(self):
        return os.path.join(PAPER_DIR, 'portfolio_state.json')

    def _load_state(self):
        """Load saved state."""
        sf = self._state_file()
        if os.path.exists(sf):
            try:
                with open(sf, 'r') as f:
                    state = json.load(f)
                self.capital = state.get('capital', self.initial_capital)
                self.positions = state.get('positions', [])
                self.closed_trades = state.get('closed_trades', [])
                self.daily_pnl = state.get('daily_pnl', {})
                logger.info(f"Loaded state: Capital Rs {self.capital:,.0f}, "
                           f"{len(self.positions)} open, {len(self.closed_trades)} closed")
            except Exception as e:
                logger.error(f"Error loading state: {e}")

    def save_state(self):
        """Save state to disk."""
        state = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat()
        }
        with open(self._state_file(), 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def add_signal(self, strategy, symbol, signal_type, strike, entry_premium,
                   delta, gamma, theta, iv, dte, details=None, oi=0, spot_price=0):
        """Record a trading signal with risk management and OI/IV tracking."""
        # ---- RISK CHECK ----
        lot_size = LOT_SIZES.get(symbol, 65)
        is_sell = 'SELL' in signal_type
        is_commodity = symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM', 'GOLD', 'SILVER', 'CRUDEOIL')
        max_per_trade = MAX_COMMODITY_PER_TRADE if is_commodity else MAX_EQUITY_PER_TRADE
        segment_capital = COMMODITY_CAPITAL if is_commodity else EQUITY_CAPITAL

        # Capital required: SELL = margin blocked, BUY = premium paid
        if is_sell:
            trade_cost = MARGIN_PER_LOT.get(symbol, 120000)
        else:
            trade_cost = entry_premium * lot_size

        # Check 1: Per-trade risk limit
        if trade_cost > max_per_trade:
            logger.warning(f"  RISK_LIMIT: {symbol} {strategy} trade cost Rs {trade_cost:,.0f} "
                          f"> max Rs {max_per_trade:,.0f}. SKIPPED.")
            return None

        # Check 2: Only trade with LEFTOVER available capital
        total_exposure = 0
        for p in self.positions:
            p_is_commodity = p['symbol'] in ('GOLDM', 'SILVERM', 'CRUDEOILM', 'GOLD', 'SILVER', 'CRUDEOIL')
            if p_is_commodity == is_commodity:
                if p.get('is_sell', False):
                    total_exposure += MARGIN_PER_LOT.get(p['symbol'], 120000)
                else:
                    total_exposure += p['entry_premium'] * p['lot_size']

        available_capital = segment_capital - total_exposure
        if trade_cost > available_capital:
            logger.warning(f"  RISK_LIMIT: {symbol} {strategy} needs Rs {trade_cost:,.0f} "
                          f"but only Rs {available_capital:,.0f} available. SKIPPED.")
            return None

        # Check 3: Daily loss limit
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.daily_pnl.get(today, 0)
        max_daily = MAX_DAILY_LOSS_COMMODITY if is_commodity else MAX_DAILY_LOSS_EQUITY
        if daily_loss < -max_daily:
            logger.warning(f"  RISK_LIMIT: Daily loss Rs {daily_loss:,.0f} exceeds limit Rs {max_daily:,.0f}. SKIPPED.")
            return None

        sig = {
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'symbol': symbol,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': entry_premium,
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'iv': iv,
            'dte': dte,
            'details': details or {},
            'status': 'SIGNAL'
        }
        self.signals.append(sig)

        is_sell = 'SELL' in signal_type
        cost = calc_costs(entry_premium, lot_size, is_sell)

        pos = {
            'id': f"{strategy}_{symbol}_{datetime.now().strftime('%H%M%S')}",
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'symbol': symbol,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': round(entry_premium, 2),
            'current_premium': round(entry_premium, 2),
            'lot_size': lot_size,
            'is_sell': is_sell,
            'entry_cost': round(cost, 2),
            'delta': round(delta, 4),
            'gamma': round(gamma, 6),
            'theta': round(theta, 2),
            'iv': round(iv * 100, 1),
            'dte': dte,
            'unrealized_pnl': 0,
            'status': 'OPEN',
            # OI+IV tracking for dynamic exits
            'entry_oi': oi,
            'entry_iv': round(iv * 100, 1),
            'entry_spot': round(spot_price, 2),
            'max_risk': round(max_per_trade, 2),
            'details': details or {},  # Store target/SL from strategy signals
        }
        self.positions.append(pos)

        logger.info(f"  PAPER TRADE: {signal_type} {symbol} {strike} "
                    f"@ Rs {entry_premium:.2f} | Strategy: {strategy} "
                    f"| Delta: {delta:.3f} | IV: {iv*100:.1f}% | OI: {oi}")

        self.save_state()
        return pos

    def update_position(self, pos_id, current_premium):
        """Update position with current market price."""
        for pos in self.positions:
            if pos['id'] == pos_id:
                pos['current_premium'] = round(current_premium, 2)
                if pos['is_sell']:
                    pos['unrealized_pnl'] = round(
                        (pos['entry_premium'] - current_premium) * pos['lot_size'] - pos['entry_cost'], 2)
                else:
                    pos['unrealized_pnl'] = round(
                        (current_premium - pos['entry_premium']) * pos['lot_size'] - pos['entry_cost'], 2)
                return pos
        return None

    def close_position(self, pos_id, exit_premium, reason=''):
        """Close a paper position."""
        for i, pos in enumerate(self.positions):
            if pos['id'] == pos_id:
                exit_cost = calc_costs(exit_premium, pos['lot_size'], not pos['is_sell'])
                if pos['is_sell']:
                    pnl = (pos['entry_premium'] - exit_premium) * pos['lot_size']
                else:
                    pnl = (exit_premium - pos['entry_premium']) * pos['lot_size']
                pnl -= (pos['entry_cost'] + exit_cost)

                trade = {
                    **pos,
                    'exit_premium': round(exit_premium, 2),
                    'exit_cost': round(exit_cost, 2),
                    'pnl': round(pnl, 2),
                    'exit_reason': reason,
                    'exit_time': datetime.now().isoformat(),
                    'status': 'CLOSED'
                }
                self.closed_trades.append(trade)
                self.positions.pop(i)

                today = datetime.now().strftime('%Y-%m-%d')
                self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl
                self.capital += pnl

                logger.info(f"  CLOSED: {pos['signal_type']} {pos['symbol']} {pos['strike']} "
                           f"| Entry: Rs {pos['entry_premium']:.2f} Exit: Rs {exit_premium:.2f} "
                           f"| PnL: Rs {pnl:,.2f} | Reason: {reason}")

                self.save_state()
                return trade
        return None

    def get_summary(self):
        """Get portfolio summary."""
        total_unrealized = sum(p['unrealized_pnl'] for p in self.positions)
        total_realized = sum(t['pnl'] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        losses = [t for t in self.closed_trades if t['pnl'] <= 0]

        return {
            'capital': self.capital,
            'initial_capital': self.initial_capital,
            'total_return_pct': (self.capital - self.initial_capital) / self.initial_capital * 100,
            'open_positions': len(self.positions),
            'total_trades': len(self.closed_trades),
            'win_rate': len(wins) / len(self.closed_trades) * 100 if self.closed_trades else 0,
            'total_realized': total_realized,
            'total_unrealized': total_unrealized,
            'avg_win': np.mean([t['pnl'] for t in wins]) if wins else 0,
            'avg_loss': np.mean([t['pnl'] for t in losses]) if losses else 0,
        }

    def print_status(self):
        """Print current portfolio status."""
        summary = self.get_summary()
        print("\n" + "=" * 70)
        print("PAPER TRADING PORTFOLIO STATUS")
        print("=" * 70)
        print(f"  Capital:           Rs {summary['capital']:,.0f}")
        print(f"  Initial:           Rs {summary['initial_capital']:,.0f}")
        print(f"  Return:            {summary['total_return_pct']:.2f}%")
        print(f"  Open Positions:    {summary['open_positions']}")
        print(f"  Closed Trades:     {summary['total_trades']}")
        print(f"  Win Rate:          {summary['win_rate']:.1f}%")
        print(f"  Realized PnL:      Rs {summary['total_realized']:,.0f}")
        print(f"  Unrealized PnL:    Rs {summary['total_unrealized']:,.0f}")

        if self.positions:
            print("\n  OPEN POSITIONS:")
            for p in self.positions:
                print(f"    {p['strategy']:12s} | {p['signal_type']:10s} | "
                      f"{p['symbol']:10s} | Strike: {p['strike']:>8.0f} | "
                      f"Entry: Rs {p['entry_premium']:>8.2f} | "
                      f"PnL: Rs {p['unrealized_pnl']:>8.2f}")

        if self.closed_trades:
            recent = self.closed_trades[-5:]
            print(f"\n  RECENT TRADES (last {len(recent)}):")
            for t in recent:
                print(f"    {t['strategy']:12s} | {t['signal_type']:10s} | "
                      f"{t['symbol']:10s} | PnL: Rs {t['pnl']:>8.2f} | "
                      f"{t['exit_reason']}")
        print("=" * 70)


# ====================================================================
# STRATEGY SIGNAL GENERATORS (LIVE)
# ====================================================================
class StrategyEngine:
    """Generate signals from all 5 strategies using live data."""

    def __init__(self, angel: AngelConnection, portfolio: PaperPortfolio):
        self.angel = angel
        self.portfolio = portfolio
        self.historical_data = {}  # symbol -> DataFrame

    def load_historical(self, symbol):
        """Load historical data for indicators. Auto-download if missing/stale."""
        angel_map = {
            'NIFTY': 'NIFTY_spot_one_day_2000d.csv',
            'BANKNIFTY': 'BANKNIFTY_spot_one_day_2000d.csv',
            'SENSEX': 'SENSEX_spot_one_day_2000d.csv',
        }
        fpath = os.path.join(OPTIONS_DIR, angel_map.get(symbol, ''))

        # Check if file needs downloading (missing or stale >7 days)
        needs_download = False
        if os.path.exists(fpath):
            mod_time = datetime.fromtimestamp(os.path.getmtime(fpath))
            age_days = (datetime.now() - mod_time).days
            if age_days >= 7:
                needs_download = True
                logger.info(f"Historical data for {symbol} is stale ({age_days}d old), refreshing...")
        else:
            needs_download = True
            logger.info(f"Historical data for {symbol} not found, downloading...")

        if needs_download and self.angel and self.angel._connected:
            self._download_historical_equity(symbol, fpath)

        if os.path.exists(fpath):
            df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            self.historical_data[symbol] = df
            logger.info(f"Loaded {len(df)} historical days for {symbol}")
            return df

        # Try Yahoo data
        yahoo_map = {
            'NIFTY': 'NIFTY50_1d_5y.csv',
            'BANKNIFTY': 'BANKNIFTY_1d_5y.csv',
            'SENSEX': 'SENSEX_1d_5y.csv',
        }
        ypath = os.path.join(DATA_DIR, yahoo_map.get(symbol, ''))
        if os.path.exists(ypath):
            df = pd.read_csv(ypath, parse_dates=['Date'], index_col='Date')
            self.historical_data[symbol] = df
            logger.info(f"Loaded {len(df)} Yahoo days for {symbol}")
            return df

        logger.error(f"No historical data found for {symbol} — indicators will not work")
        return None

    def _download_historical_equity(self, symbol, save_path):
        """Download daily OHLC data from Angel SmartAPI for equity indices."""
        try:
            # Well-known Angel One tokens for indices
            token_map = {'NIFTY': '99926000', 'BANKNIFTY': '99926009', 'SENSEX': '99919000'}
            token = token_map.get(symbol)

            if not token:
                # Fallback: look up from instrument master
                if self.angel.instruments is None:
                    self.angel.load_instruments()
                if self.angel.instruments is not None:
                    nse_df = self.angel.instruments[
                        (self.angel.instruments['name'] == symbol) &
                        (self.angel.instruments['exch_seg'] == 'NSE')
                    ]
                    if len(nse_df) > 0:
                        token = str(nse_df.iloc[0]['token'])

            if not token:
                logger.warning(f"Could not find Angel token for {symbol}, skipping download")
                return

            logger.info(f"Downloading historical data for {symbol} (token={token})...")
            all_data = []
            chunk_start = datetime.now() - timedelta(days=2000)

            while chunk_start < datetime.now():
                chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
                data = self.angel.get_historical(
                    'NSE', token, 'ONE_DAY',
                    chunk_start.strftime('%Y-%m-%d 09:15'),
                    chunk_end.strftime('%Y-%m-%d 15:30')
                )
                if data:
                    all_data.extend(data)
                chunk_start = chunk_end + timedelta(days=1)
                time.sleep(0.5)  # Rate limit between API calls

            if all_data:
                df = pd.DataFrame(all_data, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                df.to_csv(save_path, index=False)
                logger.info(f"Downloaded {len(df)} days for {symbol} → {save_path}")
            else:
                logger.warning(f"No data returned from Angel API for {symbol}")
        except Exception as e:
            logger.error(f"Download failed for {symbol}: {e}")

    def compute_indicators(self, symbol, current_ohlc=None):
        """Compute all indicators needed for strategies."""
        df = self.historical_data.get(symbol)
        if df is None:
            df = self.load_historical(symbol)
        if df is None:
            return None

        # If we have live OHLC, append it
        if current_ohlc:
            today = pd.Timestamp.now().normalize()
            new_row = pd.DataFrame({
                'Open': [current_ohlc['open']],
                'High': [current_ohlc['high']],
                'Low': [current_ohlc['low']],
                'Close': [current_ohlc['close']],
                'Volume': [current_ohlc.get('volume', 0)],
            }, index=[today])
            if today in df.index:
                df.loc[today] = new_row.iloc[0]
            else:
                df = pd.concat([df, new_row])

        # ATR
        atr = (df['High'].tail(14) - df['Low'].tail(14)).mean()

        # Historical Volatility
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        hv = log_ret.tail(20).std() * np.sqrt(252)
        iv = max(min(hv * 1.15, 0.60), 0.08)

        # VWAP (rolling 5-day)
        if 'Volume' in df.columns and df['Volume'].tail(5).sum() > 0:
            tp = (df['High'] + df['Low'] + df['Close']) / 3
            vwap = (tp * df['Volume']).tail(5).sum() / df['Volume'].tail(5).sum()
        else:
            vwap = (df['High'].tail(5) + df['Low'].tail(5) + df['Close'].tail(5)).mean() / 3

        # PCR proxy
        close_chg = df['Close'].pct_change()
        pcr_proxy = 1 + (close_chg.tail(5).mean() * 10)
        pcr_proxy = max(0.5, min(2.0, pcr_proxy))

        # CPR from previous day
        prev = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / prev['Close'] * 100

        # Camarilla
        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        # Demand/Supply zones
        recent_lows = df['Low'].tail(10)
        recent_highs = df['High'].tail(10)
        demand_zone = recent_lows.min()
        supply_zone = recent_highs.max()
        demand_strength = ((recent_lows - demand_zone).abs() < atr * 0.5).sum()
        supply_strength = ((supply_zone - recent_highs).abs() < atr * 0.5).sum()

        # Support/Resistance for Survivor
        resistance = prev['High']
        support = prev['Low']

        return {
            'atr': atr,
            'iv': iv,
            'hv': hv,
            'vwap': vwap,
            'pcr': pcr_proxy,
            'pivot': pivot,
            'bc': bc,
            'tc': tc,
            'cpr_width': cpr_width,
            'cam_r3': cam_r3,
            'cam_r4': cam_r4,
            'cam_s3': cam_s3,
            'cam_s4': cam_s4,
            'demand_zone': demand_zone,
            'supply_zone': supply_zone,
            'demand_strength': demand_strength,
            'supply_strength': supply_strength,
            'resistance': resistance,
            'support': support,
            'prev_high': prev['High'],
            'prev_low': prev['Low'],
            'prev_close': prev['Close'],
            'prev_range': (prev['High'] - prev['Low']) / max(atr, 1),
        }

    def check_cpr_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """CPR Strategy - Gomathi Shankar."""
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = 50 if symbol in ['NIFTY', 'BANKNIFTY'] else 100

        # NARROW CPR (< 0.3%) = BREAKOUT expected
        if ind['cpr_width'] < 0.3:
            if spot > ind['tc']:
                # Bullish breakout
                ce_strike = round(spot / strike_interval) * strike_interval
                g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
                if g['price'] > 3:
                    signals.append({
                        'type': 'BUY_CE_CPR',
                        'strike': ce_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Narrow CPR ({ind['cpr_width']:.3f}%) bullish breakout above TC={ind['tc']:.0f}",
                        'target': g['price'] * 1.8 if ohlc['high'] > ind['cam_r3'] else g['price'] * 1.4,
                        'sl': g['price'] * 0.4,
                    })

            elif spot < ind['bc']:
                # Bearish breakout
                pe_strike = round(spot / strike_interval) * strike_interval
                g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
                if g['price'] > 3:
                    signals.append({
                        'type': 'BUY_PE_CPR',
                        'strike': pe_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Narrow CPR ({ind['cpr_width']:.3f}%) bearish breakout below BC={ind['bc']:.0f}",
                        'target': g['price'] * 1.8 if ohlc['low'] < ind['cam_s3'] else g['price'] * 1.4,
                        'sl': g['price'] * 0.4,
                    })

        # WIDE CPR (> 0.5%) = MEAN REVERSION (sell)
        elif ind['cpr_width'] > 0.5:
            margin_ok = self.portfolio.capital >= MARGIN_PER_LOT.get(symbol, 120000)
            if ohlc['high'] >= ind['cam_r3'] * 0.998 and spot < ind['cam_r4'] and margin_ok:
                ce_strike = round(ind['cam_r4'] / strike_interval) * strike_interval
                g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
                if g['price'] > 5:
                    signals.append({
                        'type': 'SELL_CE_CPR',
                        'strike': ce_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at R3={ind['cam_r3']:.0f}",
                        'target': g['price'] * 0.3,
                        'sl': g['price'] * 2.0,
                    })

            if ohlc['low'] <= ind['cam_s3'] * 1.002 and spot > ind['cam_s4'] and margin_ok:
                pe_strike = round(ind['cam_s4'] / strike_interval) * strike_interval
                g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
                if g['price'] > 5:
                    signals.append({
                        'type': 'SELL_PE_CPR',
                        'strike': pe_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at S3={ind['cam_s3']:.0f}",
                        'target': g['price'] * 0.3,
                        'sl': g['price'] * 2.0,
                    })

        return signals

    def check_gamma_blast_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Gamma Blast - all days mode (paper trading test)."""
        signals = []
        ind = indicators

        # Calculate actual DTE to next expiry for IV/target adjustment
        if symbol == 'BANKNIFTY':
            days_to_expiry = (2 - dow) % 7  # Days to Wednesday
        else:
            days_to_expiry = (3 - dow) % 7  # Days to Thursday
        days_to_expiry = max(days_to_expiry, 1)
        is_expiry_day = (days_to_expiry == 7 or days_to_expiry == 0 or
                         (symbol == 'BANKNIFTY' and dow == 2) or
                         (symbol != 'BANKNIFTY' and dow == 3))

        T = max(dte, days_to_expiry) / 365 if not is_expiry_day else 1 / 365

        # IV multiplier: higher on expiry day (gamma spike), lower further out
        iv_mult = 1.3 if is_expiry_day else max(1.0, 1.3 - days_to_expiry * 0.08)

        # Target/SL adjustment: more conservative on non-expiry (less gamma boost)
        if is_expiry_day:
            target_mult = 2.5
            sl_mult = 0.3
        else:
            target_mult = max(1.8, 2.5 - days_to_expiry * 0.15)  # 1.8x-2.5x
            sl_mult = min(0.4, 0.3 + days_to_expiry * 0.02)      # 30%-40% SL

        # Need coiled spring
        if ind['prev_range'] > 1.5:
            return signals

        atr = ind['atr']
        body = spot - ohlc['open']
        day_range = (ohlc['high'] - ohlc['low']) / max(atr, 1)

        if day_range < 0.3:
            return signals

        strike_interval = 50 if symbol in ['NIFTY', 'BANKNIFTY'] else 100
        day_label = "EXPIRY" if is_expiry_day else f"{days_to_expiry}DTE"

        if abs(body) > atr * 0.15:
            if body > 0:  # Up breakout
                ce_strike = round(spot / strike_interval) * strike_interval
                g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'] * iv_mult, 'CE')
                if g['price'] > 3:
                    signals.append({
                        'type': 'BUY_CE_GAMMA',
                        'strike': ce_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Gamma Blast [{day_label}]: Up breakout body={body:.0f} "
                                  f"ATR={atr:.0f} range={day_range:.2f}x",
                        'target': g['price'] * target_mult,
                        'sl': g['price'] * sl_mult,
                    })
            else:  # Down breakout
                pe_strike = round(spot / strike_interval) * strike_interval
                g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'] * iv_mult, 'PE')
                if g['price'] > 3:
                    signals.append({
                        'type': 'BUY_PE_GAMMA',
                        'strike': pe_strike,
                        'premium': g['price'],
                        'greeks': g,
                        'reason': f"Gamma Blast [{day_label}]: Down breakout body={body:.0f} "
                                  f"ATR={atr:.0f} range={day_range:.2f}x",
                        'target': g['price'] * target_mult,
                        'sl': g['price'] * sl_mult,
                    })

        return signals

    def check_ghost_zone_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Ghost Zone - demand/supply zone trading."""
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = 50 if symbol in ['NIFTY', 'BANKNIFTY'] else 100

        # Demand zone retest (BUY CE)
        if (ohlc['low'] <= ind['demand_zone'] * 1.01 and
                spot > ind['demand_zone'] and
                ind['demand_strength'] >= 2):
            ce_strike = round(spot / strike_interval) * strike_interval
            g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > 3:
                bounce = (spot - ohlc['low']) / max(ind['atr'], 1)
                signals.append({
                    'type': 'BUY_CE_GTZ',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Ghost Zone: Demand zone retest {ind['demand_zone']:.0f} "
                              f"bounce={bounce:.2f}x strength={ind['demand_strength']}",
                    'target': g['price'] * 1.8 if bounce > 0.8 else g['price'] * 1.3,
                    'sl': g['price'] * 0.4,
                })

        # Supply zone retest (BUY PE)
        elif (ohlc['high'] >= ind['supply_zone'] * 0.99 and
              spot < ind['supply_zone'] and
              ind['supply_strength'] >= 2):
            pe_strike = round(spot / strike_interval) * strike_interval
            g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > 3:
                rejection = (ohlc['high'] - spot) / max(ind['atr'], 1)
                signals.append({
                    'type': 'BUY_PE_GTZ',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Ghost Zone: Supply zone retest {ind['supply_zone']:.0f} "
                              f"rejection={rejection:.2f}x strength={ind['supply_strength']}",
                    'target': g['price'] * 1.8 if rejection > 0.8 else g['price'] * 1.3,
                    'sl': g['price'] * 0.4,
                })

        return signals

    def check_pcr_vwap_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """PCR+VWAP Strategy - CA Nitin Muraka."""
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = 50 if symbol in ['NIFTY', 'BANKNIFTY'] else 100

        if ind['iv'] > 0.40:
            return signals

        tolerance = max(ind['atr'] * 0.1, spot * 0.003)
        vwap = ind['vwap']
        pcr = ind['pcr']

        # BUY CE: PCR > 1.05 (bullish), near VWAP
        if pcr > 1.05 and abs(spot - vwap) < tolerance * 2 and spot >= vwap * 0.995:
            ce_strike = round(spot / strike_interval) * strike_interval
            g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > 3 and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_CE',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bullish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.0,
                    'sl': g['price'] * 0.4,
                })

        # BUY PE: PCR < 0.95 (bearish), near VWAP
        elif pcr < 0.95 and abs(spot - vwap) < tolerance * 2 and spot <= vwap * 1.005:
            pe_strike = round(spot / strike_interval) * strike_interval
            g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > 3 and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_PE',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bearish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.0,
                    'sl': g['price'] * 0.4,
                })

        return signals

    def check_survivor_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Survivor V2 - Raahi Bhushan option selling."""
        signals = []
        ind = indicators
        T = dte / 365
        atr = ind['atr']

        if dow > 3:
            return signals

        margin_ok = self.portfolio.capital >= MARGIN_PER_LOT.get(symbol, 120000)
        if not margin_ok:
            return signals

        # Day-specific distances (RESTORED wider values)
        if dow == 0:  # Monday
            gap = max(atr * 0.4, 200)
            distance = max(atr * 0.8, 200)
        elif dow == 1:  # Tuesday
            gap = max(atr * 0.3, 120)
            distance = max(atr * 0.7, 150)
        elif dow == 2:  # Wednesday
            gap = max(atr * 0.2, 70)
            distance = max(atr * 0.5, 100)
        else:  # Thursday
            gap = max(atr * 0.15, 50)
            distance = max(atr * 0.4, 80)

        # PE SELLING - price breaks above resistance
        if ohlc['high'] > ind['resistance'] + gap:
            pe_strike = spot - distance
            g = bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > 5:
                signals.append({
                    'type': 'SELL_PE',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: PE sell at {pe_strike:.0f} "
                              f"dist={distance:.0f} gap={gap:.0f} res={ind['resistance']:.0f}",
                    'target': g['price'] * 0.2,
                    'sl': g['price'] * 1.5,
                })

        # CE SELLING - price breaks below support
        if ohlc['low'] < ind['support'] - gap:
            ce_strike = spot + distance
            g = bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > 5:
                signals.append({
                    'type': 'SELL_CE',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: CE sell at {ce_strike:.0f} "
                              f"dist={distance:.0f} gap={gap:.0f} sup={ind['support']:.0f}",
                    'target': g['price'] * 0.2,
                    'sl': g['price'] * 1.5,
                })

        return signals


# ====================================================================
# MAIN PAPER TRADING LOOP
# ====================================================================
class PaperTrader:
    """Main paper trading orchestrator."""

    def __init__(self):
        self.angel = AngelConnection()
        self.portfolio = PaperPortfolio()
        self.engine = StrategyEngine(self.angel, self.portfolio)
        self._running = False
        self._index_tokens = {
            'NIFTY': {'exchange': 'NSE', 'token': '99926000'},
            'BANKNIFTY': {'exchange': 'NSE', 'token': '99926009'},
            'SENSEX': {'exchange': 'BSE', 'token': '99919000'},
        }

    def connect(self):
        """Connect to Angel API."""
        # Try Historical first, then Market
        if self.angel.connect('Historical'):
            self.angel.load_instruments()
            return True
        if self.angel.connect('Market'):
            self.angel.load_instruments()
            return True
        logger.error("Failed to connect to Angel One")
        return False

    def get_index_spot(self, symbol):
        """Get current spot price for index."""
        info = self._index_tokens.get(symbol)
        if not info:
            return None

        ltp = self.angel.get_ltp(info['exchange'], info['token'])
        if ltp:
            return ltp

        # Fallback: use last close from historical data
        df = self.engine.historical_data.get(symbol)
        if df is not None and len(df) > 0:
            return df['Close'].iloc[-1]
        return None

    def get_intraday_ohlc(self, symbol):
        """Get today's OHLC so far."""
        info = self._index_tokens.get(symbol)
        if not info:
            return None

        try:
            # Try 5-min candles for today
            today = datetime.now()
            from_date = today.strftime('%Y-%m-%d 09:15')
            to_date = today.strftime('%Y-%m-%d %H:%M')
            data = self.angel.get_historical(
                info['exchange'], info['token'], 'FIVE_MINUTE',
                from_date, to_date
            )
            if data:
                opens = [c[1] for c in data]
                highs = [c[2] for c in data]
                lows = [c[3] for c in data]
                closes = [c[4] for c in data]
                volumes = [c[5] for c in data]
                return {
                    'open': opens[0],
                    'high': max(highs),
                    'low': min(lows),
                    'close': closes[-1],
                    'volume': sum(volumes),
                }
        except Exception as e:
            logger.error(f"Intraday OHLC error for {symbol}: {e}")

        # Fallback: use LTP as all OHLC
        spot = self.get_index_spot(symbol)
        if spot:
            return {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}
        return None

    def is_market_open(self):
        """Check if equity market is currently open (9:15 AM - 3:30 PM IST, Mon-Fri)."""
        now = datetime.now()
        if now.weekday() > 4:  # Saturday/Sunday
            return False
        current_time = now.time()
        return MARKET_OPEN <= current_time <= MARKET_CLOSE

    def scan_all_strategies(self):
        """Scan all 5 strategies across all 3 indices."""
        logger.info("\n" + "=" * 70)
        logger.info("SCANNING ALL STRATEGIES...")
        logger.info("=" * 70)

        now = datetime.now()
        dow = now.weekday()
        all_signals = []

        # CRITICAL: Block trading outside market hours
        if not self.is_market_open():
            logger.warning(f"  EQUITY MARKET CLOSED (current: {now.strftime('%H:%M:%S')})")
            logger.warning(f"  Market hours: {MARKET_OPEN} - {MARKET_CLOSE}, Mon-Fri")
            logger.warning(f"  Signals will be logged but NOT executed.")
            return all_signals  # Return empty - no trades outside hours

        for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            logger.info(f"\n--- {symbol} ---")

            spot = self.get_index_spot(symbol)
            if not spot:
                logger.warning(f"  No spot data for {symbol}")
                continue

            ohlc = self.get_intraday_ohlc(symbol)
            if not ohlc:
                ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}

            logger.info(f"  Spot: {spot:.2f} | O: {ohlc['open']:.2f} H: {ohlc['high']:.2f} "
                       f"L: {ohlc['low']:.2f} C: {ohlc['close']:.2f}")

            indicators = self.engine.compute_indicators(symbol, ohlc)
            if not indicators:
                logger.warning(f"  No indicators for {symbol}")
                continue

            logger.info(f"  ATR: {indicators['atr']:.2f} | IV: {indicators['iv']*100:.1f}% | "
                       f"CPR: {indicators['cpr_width']:.3f}% | PCR: {indicators['pcr']:.2f}")
            logger.info(f"  Pivot: {indicators['pivot']:.0f} | TC: {indicators['tc']:.0f} | "
                       f"BC: {indicators['bc']:.0f}")
            logger.info(f"  Cam R3: {indicators['cam_r3']:.0f} R4: {indicators['cam_r4']:.0f} | "
                       f"S3: {indicators['cam_s3']:.0f} S4: {indicators['cam_s4']:.0f}")

            dte = max(1, (3 - dow) + 1) if dow <= 3 else 1

            # Run all 5 strategies
            strategy_checks = [
                ('CPR', self.engine.check_cpr_signals),
                ('Gamma Blast', self.engine.check_gamma_blast_signals),
                ('Ghost Zone', self.engine.check_ghost_zone_signals),
                ('PCR+VWAP', self.engine.check_pcr_vwap_signals),
                ('Survivor', self.engine.check_survivor_signals),
            ]

            for strat_name, check_fn in strategy_checks:
                signals = check_fn(symbol, spot, ohlc, indicators, dow, dte)
                for sig in signals:
                    sig['strategy'] = strat_name
                    sig['symbol'] = symbol
                    sig['spot'] = spot
                    sig['dte'] = dte
                    all_signals.append(sig)

                    logger.info(f"  SIGNAL [{strat_name}]: {sig['type']} "
                               f"Strike={sig['strike']:.0f} Premium=Rs {sig['premium']:.2f} "
                               f"| {sig['reason']}")

        return all_signals

    def execute_paper_signals(self, signals):
        """Execute signals in paper mode."""
        if not signals:
            logger.info("\n  No signals generated this scan.")
            return

        # CRITICAL: Block execution outside market hours
        if not self.is_market_open():
            logger.warning(f"  {len(signals)} signals found but MARKET CLOSED - NOT executing")
            return

        logger.info(f"\n  {len(signals)} SIGNALS TO EXECUTE (PAPER MODE):")

        executed = 0
        skipped = 0
        for sig in signals:
            # Check for duplicate: same strategy + symbol + strike already open
            existing = [p for p in self.portfolio.positions
                        if p['strategy'] == sig['strategy']
                        and p['symbol'] == sig['symbol']
                        and p['strike'] == sig['strike']]
            if existing:
                logger.info(f"  SKIP (duplicate): {sig['strategy']} {sig['symbol']} "
                           f"{sig['strike']} already open")
                skipped += 1
                continue

            g = sig['greeks']
            self.portfolio.add_signal(
                strategy=sig['strategy'],
                symbol=sig['symbol'],
                signal_type=sig['type'],
                strike=sig['strike'],
                entry_premium=sig['premium'],
                delta=g['delta'],
                gamma=g['gamma'],
                theta=g['theta'],
                iv=g.get('iv', 0.15),
                dte=sig['dte'],
                details={
                    'reason': sig['reason'],
                    'target': sig.get('target'),
                    'sl': sig.get('sl'),
                    'spot': sig.get('spot'),
                }
            )
            executed += 1

            # Send Telegram notification
            try:
                from trade_notifier import notify_trade_entry
                lot_size = LOT_SIZES.get(sig['symbol'], 50)
                # Capital used: SELL = margin blocked, BUY = premium paid
                is_sell_sig = 'SELL' in sig['type']
                if is_sell_sig:
                    capital = MARGIN_PER_LOT.get(sig['symbol'], 120000)
                else:
                    capital = sig['premium'] * lot_size
                # Calculate total locked (BUY/SELL aware) and available capital
                locked = 0
                for p in self.portfolio.positions:
                    if p.get('is_sell', False):
                        locked += MARGIN_PER_LOT.get(p['symbol'], 120000)
                    else:
                        locked += p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                total_invested = locked
                capital_available = EQUITY_CAPITAL - locked
                notify_trade_entry(
                    market="EQUITY", strategy=sig['strategy'],
                    symbol=sig['symbol'], signal_type=sig['type'],
                    strike=sig['strike'], entry_price=sig['premium'],
                    spot=sig.get('spot', 0), lot_size=lot_size,
                    multiplier=1, delta=g['delta'],
                    target=sig.get('target', 0), sl=sig.get('sl', 0),
                    capital_used=capital, reason=sig['reason'],
                    capital_available=capital_available,
                    total_invested=total_invested,
                )
            except Exception as e:
                logger.warning(f"  Telegram notify failed: {e}")

        if skipped:
            logger.info(f"  Executed: {executed} | Skipped (duplicates): {skipped}")

    def fetch_current_oi_iv(self, pos):
        """Fetch current OI and IV from Angel SmartAPI for a position."""
        try:
            from live_scanner import LiveScanner
            scanner = getattr(self, '_live_scanner', None)
            if scanner is None:
                return None, None

            symbol = pos['symbol']
            strike = pos['strike']
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'

            # Try to get market data for this option
            # Use the scanner's existing API connection
            try:
                greeks_data = scanner.get_option_greeks_api(symbol, scanner.get_nearest_expiry(symbol))
                if greeks_data:
                    for row in greeks_data:
                        if abs(row.get('strike', 0) - strike) < 10 and row.get('type') == opt_type:
                            current_oi = row.get('oi', 0)
                            current_iv = row.get('iv', 0)
                            return current_oi, current_iv
            except Exception:
                pass

            return None, None
        except Exception as e:
            logger.debug(f"  OI/IV fetch failed for {pos['id']}: {e}")
            return None, None

    def check_oi_iv_exit(self, pos, current_premium):
        """Check if position should exit based on OI velocity + IV changes + Gamma risk.

        Returns: (exit_reason, should_reverse) or (None, False)
        """
        entry_oi = pos.get('entry_oi', 0)
        entry_iv = pos.get('entry_iv', 0)

        # Try to fetch current OI and IV
        current_oi, current_iv = self.fetch_current_oi_iv(pos)

        # If we can't get live data, use computed IV as proxy
        if current_iv is None or current_iv == 0:
            # Use the recalculated IV from BS model as proxy
            df = self.engine.historical_data.get(pos['symbol'])
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                current_iv = max(min(hv * 1.15 * 100, 60), 8)  # as percentage
            else:
                current_iv = entry_iv

        if current_oi is None:
            current_oi = entry_oi

        # Calculate changes
        oi_change = 0
        iv_change = 0

        if entry_oi and entry_oi > 0:
            oi_change = abs(current_oi - entry_oi) / entry_oi * 100

        if entry_iv and entry_iv > 0:
            iv_change = abs(current_iv - entry_iv) / entry_iv * 100

        # Rule 1: OI surge >15% from entry
        if oi_change > OI_SURGE_PCT:
            should_reverse = oi_change > OI_REVERSE_PCT
            logger.info(f"  OI_SURGE: {pos['id']} OI changed {oi_change:.1f}% "
                       f"(entry={entry_oi}, current={current_oi}). Reverse={should_reverse}")
            return 'OI_SURGE_EXIT', should_reverse

        # Rule 2: IV spike >25% from entry
        if iv_change > IV_SPIKE_PCT:
            should_reverse = iv_change > IV_REVERSE_PCT
            logger.info(f"  IV_SPIKE: {pos['id']} IV changed {iv_change:.1f}% "
                       f"(entry={entry_iv}%, current={current_iv}%). Reverse={should_reverse}")
            return 'IV_SPIKE_EXIT', should_reverse

        # Rule 3: Combined OI+IV (lower thresholds)
        if oi_change > OI_IV_COMBO_OI and iv_change > OI_IV_COMBO_IV:
            logger.info(f"  OI_IV_COMBO: {pos['id']} OI={oi_change:.1f}%, IV={iv_change:.1f}%. REVERSE!")
            return 'OI_IV_COMBINED_EXIT', True

        # Rule 4: Gamma shield for short positions
        if pos.get('is_sell') and abs(pos.get('gamma', 0)) > GAMMA_SHIELD_THRESHOLD:
            logger.info(f"  GAMMA_SHIELD: {pos['id']} gamma={pos.get('gamma', 0):.6f} > {GAMMA_SHIELD_THRESHOLD}")
            return 'GAMMA_SHIELD_EXIT', False

        return None, False

    def execute_reversal(self, pos, exit_reason):
        """After closing a position due to OI+IV exit, open a reverse trade."""
        try:
            symbol = pos['symbol']
            strategy = pos['strategy']
            original_type = pos['signal_type']

            # Determine reverse direction
            if 'CE' in original_type:
                if pos['is_sell']:
                    reverse_type = 'BUY_CE_' + strategy.upper().replace(' ', '_')
                else:
                    reverse_type = 'SELL_CE'
            else:
                if pos['is_sell']:
                    reverse_type = 'BUY_PE_' + strategy.upper().replace(' ', '_')
                else:
                    reverse_type = 'SELL_PE'

            logger.info(f"  REVERSAL: {pos['id']} → Opening {reverse_type} {symbol} "
                       f"@ strike {pos['strike']} due to {exit_reason}")

            # Open reverse trade using same strike and current premium
            reverse_pos = self.portfolio.add_signal(
                strategy=strategy + ' (Reversal)',
                symbol=symbol,
                signal_type=reverse_type,
                strike=pos['strike'],
                entry_premium=pos['current_premium'],
                delta=pos.get('delta', 0),
                gamma=pos.get('gamma', 0),
                theta=pos.get('theta', 0),
                iv=pos.get('iv', 15) / 100,
                dte=pos.get('dte', 1),
                details={'origin': 'REVERSAL', 'original_id': pos['id'], 'exit_reason': exit_reason},
                oi=pos.get('entry_oi', 0),
                spot_price=pos.get('entry_spot', 0),
            )

            if reverse_pos:
                # Telegram notification for reversal
                try:
                    from trade_notifier import send_message
                    msg = (f"<b>REVERSAL TRADE</b>\n"
                           f"Closed: {pos['signal_type']} {symbol}\n"
                           f"Opened: {reverse_type} {symbol}\n"
                           f"Reason: {exit_reason}\n"
                           f"Strike: {pos['strike']:.0f}\n"
                           f"Premium: Rs {pos['current_premium']:.2f}")
                    send_message(msg)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"  Reversal failed for {pos['id']}: {e}")

    def check_exits(self):
        """Check if any open positions should be exited.
        Now includes OI+IV dynamic exits with reversal capability.
        """
        if not self.portfolio.positions:
            return

        logger.info("\n  Checking exits for open positions...")

        for pos in list(self.portfolio.positions):
            # Skip positions opened less than 5 minutes ago (grace period)
            entry_time = datetime.fromisoformat(pos['timestamp'])
            if (datetime.now() - entry_time).total_seconds() < 300:
                logger.info(f"  GRACE: {pos['id']} opened {int((datetime.now() - entry_time).total_seconds())}s ago, skipping")
                continue

            symbol = pos['symbol']
            spot = self.get_index_spot(symbol)
            if not spot:
                continue

            T = max(pos['dte'] - 0.5, 0.01) / 365

            df = self.engine.historical_data.get(symbol)
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                iv = max(min(hv * 1.15, 0.60), 0.08)
            else:
                iv = pos['iv'] / 100

            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
            current_g = bs_greeks(spot, pos['strike'], T, RISK_FREE_RATE, iv, opt_type)
            current_premium = current_g['price']

            # Update position Greeks
            pos['gamma'] = round(current_g.get('gamma', pos.get('gamma', 0)), 6)
            pos['delta'] = round(current_g.get('delta', pos.get('delta', 0)), 4)

            self.portfolio.update_position(pos['id'], current_premium)

            # ---- OI+IV DYNAMIC EXIT CHECK (runs BEFORE static exit) ----
            oi_iv_reason, should_reverse = self.check_oi_iv_exit(pos, current_premium)
            if oi_iv_reason:
                self.portfolio.close_position(pos['id'], current_premium, oi_iv_reason)
                # Telegram notification
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = LOT_SIZES.get(symbol, 50)
                    # Capital used: SELL = margin, BUY = premium
                    if pos.get('is_sell', False):
                        capital = MARGIN_PER_LOT.get(symbol, 120000)
                    else:
                        capital = pos['entry_premium'] * lot_size
                    pnl = pos.get('unrealized_pnl', 0)
                    # Capital available AFTER closing (BUY/SELL aware)
                    locked = 0
                    for p in self.portfolio.positions:
                        if p.get('is_sell', False):
                            locked += MARGIN_PER_LOT.get(p['symbol'], 120000)
                        else:
                            locked += p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                    total_invested = locked
                    capital_available = EQUITY_CAPITAL - locked
                    notify_trade_exit(
                        market="EQUITY", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pnl, capital_used=capital,
                        exit_reason=oi_iv_reason,
                        capital_available=capital_available,
                        total_invested=total_invested,
                    )
                except Exception as e:
                    logger.warning(f"  Telegram exit notify failed: {e}")

                # Execute reversal if triggered
                if should_reverse:
                    self.execute_reversal(pos, oi_iv_reason)
                continue  # Skip static exit check for this position

            # ---- STATIC EXIT CHECK (original logic) ----
            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            target = details.get('target', pos['entry_premium'] * 2)
            sl = details.get('sl', pos['entry_premium'] * 0.3)

            exit_reason = None

            if pos['is_sell']:
                if current_premium <= target:
                    exit_reason = 'TARGET_HIT'
                elif current_premium >= sl:
                    exit_reason = 'SL_HIT'
                elif pos['dte'] <= 0:
                    exit_reason = 'EXPIRY'
            else:
                if current_premium >= target:
                    exit_reason = 'TARGET_HIT'
                elif current_premium <= sl:
                    exit_reason = 'SL_HIT'
                elif pos['dte'] <= 0:
                    exit_reason = 'EXPIRY'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current_premium, exit_reason)
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = LOT_SIZES.get(symbol, 50)
                    # Capital used: SELL = margin, BUY = premium
                    if pos.get('is_sell', False):
                        capital = MARGIN_PER_LOT.get(symbol, 120000)
                    else:
                        capital = pos['entry_premium'] * lot_size
                    pnl = pos.get('unrealized_pnl', 0)
                    # Capital available AFTER closing (BUY/SELL aware)
                    locked = 0
                    for p in self.portfolio.positions:
                        if p.get('is_sell', False):
                            locked += MARGIN_PER_LOT.get(p['symbol'], 120000)
                        else:
                            locked += p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                    total_invested = locked
                    capital_available = EQUITY_CAPITAL - locked
                    notify_trade_exit(
                        market="EQUITY", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pnl, capital_used=capital,
                        exit_reason=exit_reason,
                        capital_available=capital_available,
                        total_invested=total_invested,
                    )
                except Exception as e:
                    logger.warning(f"  Telegram exit notify failed: {e}")

    def run_once(self):
        """Run one scan cycle."""
        signals = self.scan_all_strategies()
        self.execute_paper_signals(signals)
        self.check_exits()
        self.portfolio.print_status()
        self.save_signals_log(signals)

    def save_signals_log(self, signals):
        """Save signals to CSV."""
        if not signals:
            return
        today = datetime.now().strftime('%Y%m%d')
        log_file = os.path.join(PAPER_DIR, f'signals_{today}.csv')
        rows = []
        for sig in signals:
            rows.append({
                'timestamp': datetime.now().isoformat(),
                'strategy': sig['strategy'],
                'symbol': sig['symbol'],
                'type': sig['type'],
                'strike': sig['strike'],
                'premium': sig['premium'],
                'delta': sig['greeks']['delta'],
                'gamma': sig['greeks']['gamma'],
                'theta': sig['greeks']['theta'],
                'spot': sig.get('spot'),
                'dte': sig.get('dte'),
                'reason': sig['reason'],
                'target': sig.get('target'),
                'sl': sig.get('sl'),
            })
        df = pd.DataFrame(rows)
        if os.path.exists(log_file):
            existing = pd.read_csv(log_file)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_csv(log_file, index=False)
        logger.info(f"  Signals logged: {log_file}")

    def run_continuous(self, interval_minutes=5):
        """Run continuous scanning during market hours."""
        self._running = True
        logger.info(f"\n{'='*70}")
        logger.info("PAPER TRADING SYSTEM STARTED")
        logger.info(f"Scanning every {interval_minutes} minutes during market hours")
        logger.info(f"Capital: Rs {self.portfolio.capital:,.0f}")
        logger.info(f"Strategies: CPR (88%), Gamma Blast (9.4%), Ghost Zone (2.7%)")
        logger.info(f"Indices: NIFTY, BANKNIFTY, SENSEX")
        logger.info(f"{'='*70}\n")

        def signal_handler(sig, frame):
            logger.info("\nShutting down paper trader...")
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)

        while self._running:
            now = datetime.now().time()

            if PRE_MARKET <= now <= MARKET_CLOSE:
                self.run_once()
            elif now > MARKET_CLOSE:
                logger.info("Market closed. Final status:")
                self.portfolio.print_status()
                self._save_daily_report()
                break
            else:
                next_open = datetime.combine(datetime.now().date(),
                                            PRE_MARKET)
                wait = (next_open - datetime.now()).total_seconds()
                if wait > 0:
                    logger.info(f"Market not open yet. Waiting {wait/60:.0f} minutes...")
                    time.sleep(min(wait, 300))
                    continue

            time.sleep(interval_minutes * 60)

    def _save_daily_report(self):
        """Save end-of-day report."""
        today = datetime.now().strftime('%Y%m%d')
        report = {
            'date': today,
            'summary': self.portfolio.get_summary(),
            'positions': self.portfolio.positions,
            'trades_today': [t for t in self.portfolio.closed_trades
                           if t.get('exit_time', '').startswith(datetime.now().strftime('%Y-%m-%d'))],
        }
        report_file = os.path.join(PAPER_DIR, f'daily_report_{today}.json')
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Daily report saved: {report_file}")


# ====================================================================
# CLI INTERFACE
# ====================================================================
def main():
    print("=" * 70)
    print("  ALGO TRADING - PAPER TRADING SYSTEM")
    print("  5 Strategies x 3 Indices | Angel One SmartAPI")
    print("=" * 70)

    import argparse
    parser = argparse.ArgumentParser(description='Paper Trading System')
    parser.add_argument('--status', action='store_true', help='Show current status')
    parser.add_argument('--once', action='store_true', help='Run single scan (no loop)')
    parser.add_argument('--interval', type=int, default=5, help='Scan interval (minutes)')
    parser.add_argument('--offline', action='store_true', help='Run offline (no API, use historical)')
    parser.add_argument('--reset', action='store_true', help='Reset paper portfolio')
    args = parser.parse_args()

    if args.reset:
        portfolio = PaperPortfolio()
        portfolio.capital = INITIAL_CAPITAL
        portfolio.positions = []
        portfolio.closed_trades = []
        portfolio.daily_pnl = {}
        portfolio.save_state()
        print("Portfolio reset to Rs 3,00,000")
        return

    if args.status:
        portfolio = PaperPortfolio()
        portfolio.print_status()
        return

    trader = PaperTrader()

    # Load historical data for all indices
    for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        trader.engine.load_historical(symbol)

    if args.offline:
        # Offline mode: use last available data, no API
        logger.info("Running in OFFLINE mode (no Angel API)...")
        trader.run_once()
        return

    # Connect to Angel API
    if trader.connect():
        if args.once:
            trader.run_once()
        else:
            trader.run_continuous(args.interval)
    else:
        logger.warning("Angel API connection failed. Running in offline mode...")
        trader.run_once()


if __name__ == '__main__':
    main()
