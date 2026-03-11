"""
STOCK OPTIONS PAPER TRADING ENGINE (v1.0)
==========================================
Paper trading for individual stock options (SBIN, RELIANCE, HDFCBANK, etc.)
Completely independent from the index paper_trader.py.

Uses stock F&O config (lot sizes, strike intervals) from stock_fno_config.py.
Monthly expiry only (last Thursday of each month).
3 strategies: CPR Breakout, Gamma Blast, PCR+VWAP.
Instrument type: OPTSTK on NFO exchange.

4-Class Architecture (mirrors paper_trader.py):
  1. StockAngelConnection — Angel One SmartAPI for stock data
  2. StockPortfolio — Position tracking, risk management, P&L
  3. StockStrategyEngine — Signal generation from 3 strategies
  4. StockPaperTrader — Main orchestrator, exit management, scheduling

Usage:
    python stock_paper_trader.py              # Run continuous paper trading
    python stock_paper_trader.py --once       # Single scan cycle
    python stock_paper_trader.py --status     # Show portfolio status
    python stock_paper_trader.py --reset      # Reset portfolio
    python stock_paper_trader.py --scanner    # Run morning CPR scanner only
"""

import sys
import os
import json
import time
import signal as signal_module
import logging
from datetime import datetime, timedelta, time as dtime, date
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
STOCK_PAPER_DIR = os.path.join(BASE_DIR, 'stock_paper_trades')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

for d in [STOCK_PAPER_DIR, LOG_DIR, OPTIONS_DIR]:
    os.makedirs(d, exist_ok=True)

# Capital & Risk
STOCK_CAPITAL = 200000         # Rs 2L for stock options
MAX_RISK_PCT = 25              # Max 25% of capital per trade
MAX_PER_TRADE = 50000          # Max Rs 50K per single trade
MAX_DAILY_LOSS = 20000         # Rs 20K daily loss limit (circuit breaker)
MAX_POSITIONS_PER_STOCK = 2    # Max open positions per stock
MAX_TOTAL_POSITIONS = 5        # Max total open positions across all stocks
MAX_TRADES_PER_DAY = 10        # Hard cap on daily trades
MAX_LOTS_PER_TRADE = 2         # Max lots per trade (stock options = larger lots)

# Brokerage (NSE equity options)
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18
RISK_FREE_RATE = 0.065

# Premium filters
MIN_PREMIUM_BUY = 15           # Min Rs 15 premium for BUY trades
MIN_PREMIUM_SELL = 20          # Min Rs 20 premium for SELL trades
MIN_SIGNAL_SCORE = 50          # Quality score 0-100, reject below 50
DIRECTION_FLIP_MIN_SCORE = 70  # Higher bar for direction flips

# Hold scoring thresholds
HOLD_SCORE_STRONG = 60
HOLD_SCORE_WEAK = 40
HOLD_SCORE_MIN_HOLD_MINS = 30

# Trailing Stop Loss config (same proven system as paper_trader.py)
TSL_BREAKEVEN_GAIN_PCT = 15    # Phase 1: Lock breakeven at +15%
TSL_TRAIL_GAIN_PCT = 25        # Phase 2: Start trailing at +25%
TSL_TRAIL_DISTANCE_PCT = 30    # Phase 2: 30% below peak profit
TSL_TIGHT_GAIN_PCT = 40        # Phase 3: Tight trail at +40%
TSL_TIGHT_DISTANCE_PCT = 20    # Phase 3: 20% below peak profit

# v10.1: Trailing Target (replaces hard TARGET_HIT exit)
TARGET_TRAIL_ENABLED = True
TARGET_TRAIL_EXTEND_PCT = 20          # Extend target by 20% of current premium when hit
TARGET_TRAIL_TSL_DISTANCE_PCT = 15    # TSL at 15% below peak after target hit
TARGET_TRAIL_MAX_EXTENSIONS = 5       # Max extensions (safety cap)

# v10.1: Breakout Failure Detection
BREAKOUT_FAIL_CHECK_MINUTES = 5
BREAKOUT_FAIL_MIN_GAIN_PCT = 2        # Must gain 2% from entry in 5 min
BREAKOUT_FAIL_REVERSE_DROP_PCT = 15   # Exit + reverse if drops >15% in 5 min
BREAKOUT_FAIL_REVERSE_ENABLED = True

# OI/IV exit thresholds
OI_SURGE_FIRST_HOUR_PCT = 50
OI_SURGE_MID_DAY_PCT = 35
OI_SURGE_LAST_HOUR_PCT = 40
IV_SPIKE_PCT = 30
GAMMA_SHIELD_THRESHOLD = 0.002

# VIX-Adaptive Trading
VIX_LOW_THRESHOLD = 14
VIX_HIGH_THRESHOLD = 20
VIX_LOW_MULTIPLIER = 1.5
VIX_HIGH_MULTIPLIER = 0.8

# Cooldowns & Limits
GRACE_PERIOD_SECONDS = 180     # 3 min grace period after entry
REENTRY_COOLDOWN_SECONDS = 600 # 10 min after exit before re-entry
MIN_PROFIT_TO_COST_RATIO = 2.0

# Lot sizing tiers (by signal quality score)
LOT_TIER_ELITE = 80
LOT_TIER_STRONG = 60

# Market hours (IST)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
PRE_MARKET = dtime(9, 0)
FIRST_TRADE_TIME = dtime(9, 30)
LAST_ENTRY_TIME = dtime(14, 30)
EOD_FORCE_CLOSE_TIME = dtime(15, 20)

# Angel API credentials
ANGEL_CRED_FILE = os.environ.get(
    'ANGEL_CRED_FILE',
    r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt"
)

# ====================================================================
# LOGGING SETUP
# ====================================================================
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'stock_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('StockPaperTrader')

# ====================================================================
# STRATEGY LIBRARY IMPORTS
# ====================================================================
from strategies.greeks import bs_greeks, implied_vol, greeks_from_market_price
from strategies.utils import (
    calc_costs, passes_profit_filter, RISK_FREE_RATE as _RFR,
    get_monthly_expiry, get_nearest_monthly_expiry, get_dte_monthly,
    get_nearest_strike, get_otm_strike,
)
from strategies.cpr_strategy import check_cpr_breakout
from strategies.gamma_blast_strategy import check_gamma_blast
from strategies.signal_scoring import compute_signal_score
from strategies.hold_scoring import compute_hold_score
from strategies.indicators import compute_all_indicators
from stock_fno_config import StockFnOConfig, TIER1_STOCKS, TIER2_STOCKS, TIER3_STOCKS


def validate_signal_direction(signal_type, spot, ohlc, indicators):
    """v10.1: Multi-factor direction validation — THE PRIMARY GATE for all signals.

    CE (bullish) requires majority of: spot>VWAP, spot>prev_close, spot>open,
    EMA(9)>EMA(20), positive 3-bar trend.
    PE (bearish) requires the opposite.

    Returns (is_valid, reason_str, score_adjustment).
    Must pass at least 3 of 5 checks for entry.
    """
    is_ce = 'CE' in signal_type
    is_sell = 'SELL' in signal_type
    if is_sell:
        return True, "SELL_EXEMPT", 0

    vwap = indicators.get('vwap', 0)
    prev_close = indicators.get('prev_close', spot)
    ema_9 = indicators.get('ema_9', 0)
    ema_20 = indicators.get('ema_20', 0)
    three_bar = indicators.get('three_bar_trend', 0)
    open_price = ohlc.get('open', spot) if ohlc else spot

    checks = []
    passed = 0

    # Check 1: VWAP alignment (spot vs 5-day VWAP)
    if vwap and vwap > 0:
        if (is_ce and spot > vwap) or (not is_ce and spot < vwap):
            passed += 1; checks.append('VWAP+')
        else:
            checks.append('VWAP-')

    # Check 2: Day trend (spot vs prev close)
    body = spot - prev_close
    if (is_ce and body > 0) or (not is_ce and body < 0):
        passed += 1; checks.append('TREND+')
    else:
        checks.append('TREND-')

    # Check 3: Intraday direction (spot vs today's open)
    intraday = spot - open_price
    if (is_ce and intraday > 0) or (not is_ce and intraday < 0):
        passed += 1; checks.append('INTRA+')
    else:
        checks.append('INTRA-')

    # Check 4: EMA trend (9 vs 20)
    if ema_9 > 0 and ema_20 > 0:
        if (is_ce and ema_9 > ema_20) or (not is_ce and ema_9 < ema_20):
            passed += 1; checks.append('EMA+')
        else:
            checks.append('EMA-')

    # Check 5: Multi-bar momentum (3-bar trend)
    if (is_ce and three_bar >= 2) or (not is_ce and three_bar <= -2):
        passed += 1; checks.append('MOM+')
    elif three_bar == 0:
        checks.append('MOM~')
    else:
        checks.append('MOM-')

    is_valid = passed >= 3
    score_adj = (passed - 3) * 5
    reason = f"DIR({passed}/5: {' '.join(checks)})"
    return is_valid, reason, score_adj


# ====================================================================
# STOCK CPR SCANNER -- Morning narrow-CPR stock selector
# ====================================================================
class StockCPRScanner:
    """Scans F&O stocks each morning and ranks by CPR width.

    Narrow CPR (< 0.3%) = high probability breakout setup.
    Runs once pre-market to identify the best 3-5 stocks for the day.

    Tier priority: TIER1 first (always scanned), then TIER2, then TIER3
    if TIER1 has no narrow CPR candidates.
    """

    def __init__(self, fno_config, angel_conn=None):
        """Initialize scanner.

        Args:
            fno_config: StockFnOConfig instance.
            angel_conn: StockAngelConnection instance (optional, for live data).
        """
        self.fno_config = fno_config
        self.angel = angel_conn
        self._scan_results = []      # [{symbol, cpr_width, tier, score, ...}]
        self._scan_date = None
        self._historical_cache = {}  # {symbol: DataFrame}

    def _load_stock_historical(self, symbol):
        """Load daily OHLC data for a stock. Uses cache or downloads.

        Args:
            symbol: Stock symbol (e.g. 'SBIN').

        Returns:
            DataFrame with Open, High, Low, Close, Volume columns, or None.
        """
        if symbol in self._historical_cache:
            return self._historical_cache[symbol]

        fpath = os.path.join(OPTIONS_DIR, f'{symbol}_spot_one_day.csv')

        # Check if file is fresh (<1 day old)
        if os.path.exists(fpath):
            mod_time = datetime.fromtimestamp(os.path.getmtime(fpath))
            if (datetime.now() - mod_time).days < 1:
                try:
                    df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
                    if df.index.tz is not None:
                        df.index = df.index.tz_localize(None)
                    df.index = df.index.normalize()
                    self._historical_cache[symbol] = df
                    return df
                except Exception:
                    pass

        # Download from Angel API if available
        if self.angel and self.angel._connected:
            df = self._download_stock_historical(symbol, fpath)
            if df is not None:
                self._historical_cache[symbol] = df
                return df

        # Try loading stale file as fallback
        if os.path.exists(fpath):
            try:
                df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                df.index = df.index.normalize()
                self._historical_cache[symbol] = df
                return df
            except Exception:
                pass

        return None

    def _download_stock_historical(self, symbol, save_path):
        """Download daily OHLC for a stock from Angel SmartAPI.

        Args:
            symbol: Stock symbol.
            save_path: Path to save CSV.

        Returns:
            DataFrame or None.
        """
        try:
            # Look up NSE cash segment token
            stock_info = self.fno_config.stocks.get(symbol, {})
            token = stock_info.get('spot_token')

            if not token and self.angel.instruments is not None:
                # Find from instruments master
                mask = ((self.angel.instruments['name'] == symbol) &
                        (self.angel.instruments['exch_seg'] == 'NSE') &
                        (self.angel.instruments['instrumenttype'] == ''))
                matches = self.angel.instruments[mask]
                if len(matches) == 0:
                    # Try EQ instrument type
                    mask = ((self.angel.instruments['name'] == symbol) &
                            (self.angel.instruments['exch_seg'] == 'NSE'))
                    matches = self.angel.instruments[mask]
                    # Filter to cash segment (no instrument type or EQ)
                    if len(matches) > 0:
                        eq_mask = matches['instrumenttype'].isin(['', 'EQ', np.nan])
                        eq_matches = matches[eq_mask]
                        if len(eq_matches) > 0:
                            matches = eq_matches
                if len(matches) > 0:
                    token = str(matches.iloc[0]['token'])
                    stock_info['spot_token'] = token

            if not token:
                logger.debug(f"No NSE token found for {symbol}")
                return None

            logger.info(f"Downloading historical for {symbol} (token={token})...")
            all_data = []
            chunk_start = datetime.now() - timedelta(days=500)

            while chunk_start < datetime.now():
                chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
                data = self.angel.get_historical(
                    'NSE', token, 'ONE_DAY',
                    chunk_start.strftime('%Y-%m-%d 09:15'),
                    chunk_end.strftime('%Y-%m-%d 15:30'),
                )
                if data:
                    all_data.extend(data)
                chunk_start = chunk_end + timedelta(days=1)
                time.sleep(2)

            if all_data:
                df = pd.DataFrame(all_data,
                                  columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['DateTime'] = pd.to_datetime(df['DateTime'])
                df.set_index('DateTime', inplace=True)
                df.index = df.index.normalize()
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                df.to_csv(save_path)
                logger.info(f"Downloaded {len(df)} days for {symbol}")
                return df

        except Exception as e:
            logger.error(f"Download failed for {symbol}: {e}")
        return None

    def compute_stock_cpr(self, symbol):
        """Compute CPR levels and width for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            dict with pivot, bc, tc, cpr_width, atr, prev_high, prev_low,
            prev_close, cam_r3, cam_r4, cam_s3, cam_s4, or None.
        """
        df = self._load_stock_historical(symbol)
        if df is None or len(df) < 15:
            return None

        # v10.1: CPR freshness check
        if len(df) >= 2:
            today = datetime.now().date()
            prev_date = pd.Timestamp(df.index[-1]).date()  # .date() strips timezone
            gap_days = (today - prev_date).days
            if gap_days > 4:
                logger.warning(f"  CPR_STALE: {symbol} historical data is {gap_days}d old")

        # v9.6b: Use last complete bar = yesterday's OHLC (for next-day CPR)
        # Historical data is ONE_DAY interval; last row IS yesterday's complete candle
        prev = df.iloc[-1]
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / prev['Close'] * 100

        # ATR (14-day average true range)
        tr_series = pd.concat([
            df['High'] - df['Low'],
            abs(df['High'] - df['Close'].shift(1)),
            abs(df['Low'] - df['Close'].shift(1)),
        ], axis=1).max(axis=1)
        atr = tr_series.tail(14).mean()

        # Camarilla levels
        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        # Historical Volatility -> IV estimate
        log_ret = np.log(df['Close'] / df['Close'].shift(1)).dropna()
        hv = log_ret.tail(20).std() * np.sqrt(252) if len(log_ret) >= 20 else 0.20
        iv = max(min(hv * 1.15, 0.60), 0.08)

        # Previous day range relative to ATR (coiled spring metric)
        prev_range = (prev['High'] - prev['Low']) / max(atr, 1) if atr > 0 else 1.0

        # PCR proxy from close-to-close changes
        close_chg = df['Close'].pct_change()
        pcr_proxy = 1 + (close_chg.tail(5).mean() * 10)
        pcr_proxy = max(0.5, min(2.0, pcr_proxy))

        # VWAP (rolling 5-day)
        if 'Volume' in df.columns and df['Volume'].tail(5).sum() > 0:
            tp = (df['High'] + df['Low'] + df['Close']) / 3
            vwap = (tp * df['Volume']).tail(5).sum() / df['Volume'].tail(5).sum()
        else:
            vwap = ((df['High'] + df['Low'] + df['Close']) / 3).tail(5).mean()  # v10.2c: Fixed VWAP fallback

        return {
            'pivot': pivot,
            'bc': bc,
            'tc': tc,
            'cpr_width': cpr_width,
            'atr': atr,
            'iv': iv,
            'hv': hv,
            'vwap': vwap,
            'pcr': pcr_proxy,
            'prev_high': prev['High'],
            'prev_low': prev['Low'],
            'prev_close': prev['Close'],
            'prev_range': prev_range,
            'cam_r3': cam_r3,
            'cam_r4': cam_r4,
            'cam_s3': cam_s3,
            'cam_s4': cam_s4,
            'resistance': prev['High'],
            'support': prev['Low'],
        }

    def scan_all_stocks(self, max_stocks=5, cpr_threshold=0.5):
        """v9.6b: Scan ALL F&O stocks (not just tiers) for narrow CPR.

        Scans the full universe of ~207 F&O stocks from instruments master.
        Scores by CPR narrowness + liquidity. Returns top candidates.

        Args:
            max_stocks: Maximum stocks to return (default 5).
            cpr_threshold: CPR width threshold % (default 0.5).

        Returns:
            List of dicts: [{symbol, cpr_width, tier, indicators, score}].
        """
        logger.info("=" * 60)
        logger.info("STOCK CPR SCANNER: Scanning ALL F&O stocks for narrow CPR...")
        logger.info("=" * 60)

        all_symbols = self.fno_config.get_all_fno_symbols()
        logger.info(f"  Total F&O stocks to scan: {len(all_symbols)}")

        candidates = []
        scanned = 0
        no_data = 0

        for symbol in all_symbols:
            indicators = self.compute_stock_cpr(symbol)
            if indicators is None:
                no_data += 1
                continue
            scanned += 1

            cpr_w = indicators['cpr_width']
            stock_info = self.fno_config.stocks.get(symbol, {})
            liquidity = stock_info.get('liquidity', 'LOW')
            tier_num = stock_info.get('tier', 4)

            # Score: lower CPR = better, higher liquidity = better
            liq_bonus = {'VERY_HIGH': 20, 'HIGH': 15, 'MEDIUM': 10, 'LOW': 5}.get(liquidity, 5)
            cpr_score = max(0, 100 - cpr_w * 200)  # 0% CPR = 100, 0.5% = 0
            total_score = cpr_score + liq_bonus

            if cpr_w <= cpr_threshold:
                entry = {
                    'symbol': symbol,
                    'cpr_width': cpr_w,
                    'tier': tier_num,
                    'liquidity': liquidity,
                    'score': total_score,
                    'indicators': indicators,
                    'lot_size': stock_info.get('lot_size', 0),
                    'strike_interval': stock_info.get('strike_interval', 0),
                    'sector': stock_info.get('sector', 'UNKNOWN'),
                }
                candidates.append(entry)

        logger.info(f"  Scanned: {scanned} | No data: {no_data} | Narrow CPR candidates: {len(candidates)}")

        # Log top narrow CPR stocks
        candidates.sort(key=lambda x: x['cpr_width'])
        for c in candidates[:20]:
            cpr_label = 'NARROW' if c['cpr_width'] < 0.3 else 'MODERATE'
            logger.info(f"    {c['symbol']:12s} CPR={c['cpr_width']:.3f}% [{cpr_label}] "
                        f"ATR={c['indicators']['atr']:.1f} Score={c['score']:.0f} "
                        f"[{c['liquidity']}]")

        # Sort by score (highest first) and take top N
        candidates.sort(key=lambda x: x['score'], reverse=True)
        top = candidates[:max_stocks * 2]  # Take extra for diversification

        # Sector diversification: max 2 stocks from same sector
        diversified = []
        sector_count = {}
        for c in top:
            sec = c['sector']
            if sector_count.get(sec, 0) < 2:
                diversified.append(c)
                sector_count[sec] = sector_count.get(sec, 0) + 1
            if len(diversified) >= max_stocks:
                break

        self._scan_results = diversified
        self._scan_date = datetime.now().strftime('%Y-%m-%d')

        logger.info(f"\n  SCANNER RESULT: {len(diversified)} stocks selected")
        for c in diversified:
            logger.info(f"    {c['symbol']:12s} CPR={c['cpr_width']:.3f}% "
                        f"Tier={c['tier']} Score={c['score']:.0f} "
                        f"[{c['sector']}] Lot={c['lot_size']}")

        return diversified

    def get_todays_stocks(self):
        """Return today's scanned stocks. Re-scans if stale.

        Returns:
            List of stock candidate dicts from scan_all_stocks().
        """
        today = datetime.now().strftime('%Y-%m-%d')
        if self._scan_date != today or not self._scan_results:
            self.scan_all_stocks()
        return self._scan_results


# ====================================================================
# STOCK ANGEL CONNECTION
# ====================================================================
class StockAngelConnection:
    """Manages Angel One SmartAPI connection for stock options.

    Mirrors AngelConnection from paper_trader.py but uses:
    - NFO exchange for stock options (not BFO)
    - OPTSTK instrument type (not OPTIDX)
    - Monthly expiry (last Thursday, not weekly)
    - NSE cash segment for spot prices
    """

    def __init__(self):
        self.obj = None
        self.session = None
        self.instruments = None
        self._connected = False
        self._last_api_call = 0
        self._api_min_interval = 1.0  # Min 1s between REST calls
        self._backoff_until = 0       # Exponential backoff timestamp
        self._auth_time = 0           # v9.2: Track when we last authenticated
        self._app_type = 'Historical' # v9.2: Remember app type for reconnect
        self._reconnecting = False    # v9.2: Prevent recursive reconnect loops

    def load_credentials(self):
        """Load Angel credentials from file.

        Returns:
            dict: {app_type: {ANGEL_API_KEY, ANGEL_CLIENT_CODE, ...}}.
        """
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

    def _throttle(self):
        """Enforce minimum interval between REST API calls."""
        now = time.time()
        if now < self._backoff_until:
            wait = self._backoff_until - now
            logger.debug(f"API backoff: waiting {wait:.1f}s")
            time.sleep(wait)
        elapsed = time.time() - self._last_api_call
        if elapsed < self._api_min_interval:
            time.sleep(self._api_min_interval - elapsed)
        self._last_api_call = time.time()

    def _handle_rate_limit(self, error_msg=''):
        """Exponential backoff on rate limit (AB1004/TooManyRequests)."""
        backoff = min(30, max(2, (self._backoff_until - time.time()) * 2 + 2))
        self._backoff_until = time.time() + backoff
        logger.warning(f"API RATE_LIMIT: backing off {backoff:.0f}s -- {error_msg}")
        try:
            from trade_notifier import notify_api_rate_limit
            notify_api_rate_limit('STOCK', 'REST API', error_msg)
        except Exception:
            pass

    def _is_token_expired(self, data):
        """v9.2: Check if API response indicates expired/invalid token."""
        if not data or not isinstance(data, dict):
            return False
        err_code = str(data.get('errorcode', ''))
        err_msg = str(data.get('message', '')).lower()
        return err_code in ('AG8001', 'AG8002', 'AB8050') or 'invalid token' in err_msg or 'token expired' in err_msg

    def reconnect(self):
        """v9.2: Re-authenticate to Angel API when token expires."""
        if self._reconnecting:
            return False
        self._reconnecting = True
        logger.warning("STOCK TOKEN_EXPIRED: Attempting re-authentication...")
        try:
            from trade_notifier import notify_system_event
            notify_system_event('STOCKS', 'Angel API token expired — reconnecting...')
        except Exception:
            pass
        try:
            self._connected = False
            result = self.connect(self._app_type)
            if result:
                logger.info("STOCK TOKEN_REFRESH: Re-authentication successful")
            else:
                logger.error("STOCK TOKEN_REFRESH: Re-authentication FAILED")
            return result
        finally:
            self._reconnecting = False

    def check_token_health(self):
        """v9.2: Proactive token health check — call from heartbeat."""
        if not self._connected:
            return False
        token_age_hours = (time.time() - self._auth_time) / 3600 if self._auth_time else 0
        if token_age_hours > 5:
            logger.info(f"STOCK TOKEN_HEALTH: Token age {token_age_hours:.1f}h > 5h, proactive refresh...")
            return self.reconnect()
        try:
            self._throttle()
            data = self.obj.ltpData('NSE', '99926000', '99926000')  # NIFTY 50 index
            if self._is_token_expired(data):
                logger.warning(f"STOCK TOKEN_HEALTH: Token invalid, reconnecting...")
                return self.reconnect()
            return True
        except Exception as e:
            logger.warning(f"STOCK TOKEN_HEALTH: Ping failed ({e}), reconnecting...")
            return self.reconnect()

    def connect(self, app_type='Historical'):
        """Connect to Angel SmartAPI with retry on rate limit.

        Args:
            app_type: Angel app type ('Historical' or 'Market').

        Returns:
            bool: True if connected successfully.
        """
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError:
            os.system("pip install smartapi-python pyotp logzero websocket-client")
            from SmartApi import SmartConnect
            import pyotp

        self._app_type = app_type  # v9.2: Remember for reconnect
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
                    self._auth_time = time.time()  # v9.2: Track auth time
                    logger.info(f"Angel One connected: {client}")
                    return True
                else:
                    logger.warning(f"Angel login failed (attempt {attempt+1}): {self.session}")
            except Exception as e:
                logger.warning(f"Angel connection error (attempt {attempt+1}): {e}")

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"Angel One connection failed after {max_retries} attempts")
        return False

    def load_instruments(self):
        """Load or download instrument master.

        Returns:
            DataFrame of all instruments.
        """
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

    def get_ltp(self, exchange, symbol_token):
        """Get Last Traded Price.

        Args:
            exchange: 'NSE' or 'NFO'.
            symbol_token: Angel symbol token string.

        Returns:
            float LTP or None.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.ltpData(exchange, symbol_token, symbol_token)
            if data and data.get('data'):
                return data['data'].get('ltp')
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"Stock LTP: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.ltpData(exchange, symbol_token, symbol_token)
                    if data and data.get('data'):
                        return data['data'].get('ltp')
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"LTP error: {e}")
        return None

    def get_market_data(self, exchange, symbol_token):
        """Get full market data including OI.

        Args:
            exchange: 'NSE' or 'NFO'.
            symbol_token: Angel symbol token string.

        Returns:
            dict with ltp, oi, etc. or None.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.getMarketData("FULL", {exchange: [symbol_token]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"Stock MarketData: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getMarketData("FULL", {exchange: [symbol_token]})
                    if data and data.get('data') and data['data'].get('fetched'):
                        return data['data']['fetched'][0]
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"Market data error: {e}")
        return None

    def get_option_greeks(self, name, expiry_date):
        """Get option Greeks from Angel API for a stock.

        Args:
            name: Stock symbol (e.g. 'SBIN').
            expiry_date: Expiry in 'ddMONyyyy' format (e.g. '27MAR2026').

        Returns:
            list of strike data dicts or None.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_date,
            })
            if data and data.get('data'):
                return data['data']
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"Stock Greeks: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.optionGreek({"name": name, "expirydate": expiry_date})
                    if data and data.get('data'):
                        return data['data']
            err_code = str(data.get('errorcode', '')) if data else ''
            if 'AB1004' in err_code:
                self._handle_rate_limit(f"optionGreek {name}")
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"Greeks error for {name}: {e}")
        return None

    def get_historical(self, exchange, token, interval, from_date, to_date):
        """Get historical candle data.

        Args:
            exchange: 'NSE' or 'NFO'.
            token: Angel symbol token.
            interval: 'ONE_DAY', 'FIVE_MINUTE', etc.
            from_date: Start datetime string.
            to_date: End datetime string.

        Returns:
            list of candle data or None.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date,
            }
            data = self.obj.getCandleData(params)
            if data and data.get('data'):
                return data['data']
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"Stock Historical: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getCandleData(params)
                    if data and data.get('data'):
                        return data['data']
            if data and 'AB1004' in str(data.get('errorcode', '')):
                self._handle_rate_limit(str(data.get('message', '')))
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"Historical error: {e}")
        return None

    def find_stock_token(self, symbol):
        """Find NSE cash segment token for a stock.

        Args:
            symbol: Stock symbol (e.g. 'SBIN').

        Returns:
            str token or None.
        """
        if self.instruments is None:
            self.load_instruments()
        if self.instruments is None:
            return None

        mask = ((self.instruments['name'] == symbol) &
                (self.instruments['exch_seg'] == 'NSE'))
        matches = self.instruments[mask]
        # Prefer EQ or blank instrument type (cash segment)
        if len(matches) > 0:
            eq_mask = matches['instrumenttype'].isin(['', 'EQ', np.nan])
            eq_matches = matches[eq_mask]
            if len(eq_matches) > 0:
                return str(eq_matches.iloc[0]['token'])
            return str(matches.iloc[0]['token'])
        return None

    def find_option_tokens(self, name, expiry, strike, opt_type):
        """Find stock option contract token.

        Uses OPTSTK instrument type on NFO exchange.

        Args:
            name: Stock symbol (e.g. 'SBIN').
            expiry: Expiry string or None (finds nearest).
            strike: Strike price.
            opt_type: 'CE' or 'PE'.

        Returns:
            dict with token, symbol, etc. or None.
        """
        if self.instruments is None:
            self.load_instruments()
        if self.instruments is None:
            return None

        mask = ((self.instruments['name'] == name) &
                (self.instruments['exch_seg'] == 'NFO') &
                (self.instruments['instrumenttype'] == 'OPTSTK') &
                (self.instruments['strike'].astype(float) == float(strike * 100)))

        if opt_type:
            mask = mask & (self.instruments['symbol'].str.endswith(opt_type))

        matches = self.instruments[mask]
        if len(matches) == 0:
            return None

        # Filter to nearest future expiry
        try:
            today = datetime.now().date()
            matches = matches.copy()
            matches['expiry_date'] = pd.to_datetime(
                matches['expiry'], format='mixed', dayfirst=True
            )
            future = matches[matches['expiry_date'].dt.date >= today]
            if len(future) > 0:
                nearest_expiry = future['expiry_date'].min()
                matches = future[future['expiry_date'] == nearest_expiry]
        except Exception:
            pass

        if len(matches) > 0:
            return matches.iloc[0].to_dict()
        return None

    def get_real_pcr(self, name, expiry_date):
        """Compute real PCR from option chain OI data for a stock.

        PCR = Total Put OI / Total Call OI.

        Args:
            name: Stock symbol.
            expiry_date: Expiry in ddMONyyyy format.

        Returns:
            float PCR or None.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_date,
            })
            if data and data.get('data'):
                total_put_oi = 0
                total_call_oi = 0
                for strike_data in data['data']:
                    opt_type = strike_data.get('optionType', '')
                    oi = float(strike_data.get('opnInterest', 0) or 0)
                    if opt_type == 'PE':
                        total_put_oi += oi
                    elif opt_type == 'CE':
                        total_call_oi += oi
                if total_call_oi > 0:
                    pcr = total_put_oi / total_call_oi
                    logger.info(f"  REAL_PCR: {name} Put OI={total_put_oi:,.0f} "
                                f"Call OI={total_call_oi:,.0f} PCR={pcr:.3f}")
                    return pcr
        except Exception as e:
            logger.error(f"Real PCR error for {name}: {e}")
        return None

    def select_optimal_strike(self, name, spot, opt_type, expiry_date=None, num_strikes=3):
        """Select best strike from live option chain for a stock.

        Picks strike with best OI among ATM +/- num_strikes.

        Args:
            name: Stock symbol.
            spot: Current spot price.
            opt_type: 'CE' or 'PE'.
            expiry_date: Expiry in ddMONyyyy format, or None for nearest.
            num_strikes: Number of strikes above/below ATM to consider.

        Returns:
            dict: {strike, ltp, oi, iv, volume} or None.
        """
        if not self._connected:
            return None

        stock_info = self.fno_config_ref.stocks.get(name, {}) if hasattr(self, 'fno_config_ref') else {}
        strike_interval = stock_info.get('strike_interval', 25)
        atm_strike = round(spot / strike_interval) * strike_interval

        candidates = [atm_strike + i * strike_interval
                      for i in range(-num_strikes, num_strikes + 1)]

        best = None
        best_score = -1

        try:
            if expiry_date:
                self._throttle()
                data = self.obj.optionGreek({
                    "name": name,
                    "expirydate": expiry_date,
                })
                # v10.2f: Diagnostic logging for strike selection debugging
                if not data or not data.get('data'):
                    logger.warning(f"  OPTGREEK_EMPTY: {name} expiry={expiry_date} — "
                                  f"API returned no data (response={str(data)[:200]})")
                else:
                    logger.debug(f"  OPTGREEK_OK: {name} expiry={expiry_date} — "
                                f"{len(data['data'])} strikes returned, candidates={candidates}")
                if data and data.get('data'):
                    for strike_data in data['data']:
                        sd_type = strike_data.get('optionType', '')
                        if sd_type != opt_type:
                            continue
                        sd_strike = float(strike_data.get('strikePrice', 0) or 0)
                        if sd_strike not in candidates:
                            continue

                        oi = float(strike_data.get('opnInterest', 0) or 0)
                        ltp = float(strike_data.get('ltp', 0) or 0)
                        iv = float(strike_data.get('impliedVolatility', 0) or 0)
                        volume = float(strike_data.get('tradeVolume', 0) or 0)
                        # v10.3: Extract delta + gamma from API (was ignored before!)
                        delta = abs(float(strike_data.get('delta', 0) or 0))
                        gamma = float(strike_data.get('gamma', 0) or 0)

                        # v10.3: Delta+Gamma optimized scoring (was OI-only)
                        delta_score = max(0, 1 - abs(delta - 0.50) * 3.33)
                        gamma_score = min(gamma * 10000, 5.0)
                        oi_norm = min(oi / 100000, 1.0)
                        vol_norm = min(volume / 10000, 1.0)
                        # Weighted: Delta(35%) + Gamma(25%) + OI(25%) + Volume(15%)
                        score = (delta_score * 35) + (gamma_score * 25) + (oi_norm * 25) + (vol_norm * 15)

                        # FILTER: Skip strikes outside delta range 0.20-0.70
                        if delta > 0 and (delta < 0.20 or delta > 0.70):
                            continue

                        if score > best_score and ltp > 0:
                            best_score = score
                            best = {
                                'strike': sd_strike,
                                'ltp': ltp,
                                'oi': oi,
                                'iv': iv / 100 if iv > 1 else iv,
                                'volume': volume,
                                'delta': delta,    # v10.3: store greeks
                                'gamma': gamma,
                            }

                    if best:
                        token_info = self.find_option_tokens(name, None, best['strike'], opt_type)
                        if token_info:
                            best['token'] = str(token_info.get('token', ''))
                            best['symbol'] = token_info.get('symbol', '')
                        logger.info(f"  OPTIMAL_STRIKE: {name} {opt_type} ATM={atm_strike} "
                                    f"Selected={best['strike']} Delta={best.get('delta',0):.3f} "
                                    f"Gamma={best.get('gamma',0):.6f} OI={best['oi']:,.0f} "
                                    f"LTP={best['ltp']:.2f} IV={best.get('iv', 0)*100:.1f}%")
                        return best

        except Exception as e:
            logger.error(f"Option chain strike selection error for {name}: {e}")

        # Fallback: mathematical derivation
        logger.info(f"  FALLBACK_STRIKE: {name} {opt_type} using ATM={atm_strike}")
        return {'strike': atm_strike, 'ltp': 0, 'oi': 0, 'iv': 0, 'volume': 0}


# ====================================================================
# STOCK PORTFOLIO — Position tracking, capital management, P&L
# ====================================================================
class StockPortfolio:
    """Manages stock option paper trades: positions, risk, capital, P&L.

    Mirrors PaperPortfolio from paper_trader.py (line 920) but adapted for:
    - Stock options with variable lot sizes per stock
    - Separate state file (paper_trades_stocks/stock_portfolio_state.json)
    - Stock-specific risk limits

    State is persisted to JSON after every modification.
    """

    STATE_FILE = os.path.join(STOCK_PAPER_DIR, 'stock_portfolio_state.json')

    def __init__(self, capital=STOCK_CAPITAL):
        """Initialize portfolio.

        Args:
            capital: Starting capital for stock options (default Rs 2L).
        """
        self.capital = capital
        self.positions = []
        self.closed_trades = []
        self.daily_pnl = {}          # {date_str: cumulative_pnl}
        self.trade_counter = 0       # Unique position ID counter
        self._load_state()

    def _load_state(self):
        """Load portfolio state from JSON file."""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.positions = state.get('positions', [])
                self.closed_trades = state.get('closed_trades', [])
                self.daily_pnl = state.get('daily_pnl', {})
                self.capital = state.get('capital', self.capital)
                self.trade_counter = state.get('trade_counter', 0)
                logger.info(f"Portfolio loaded: {len(self.positions)} open, "
                            f"{len(self.closed_trades)} closed, "
                            f"Capital Rs {self.capital:,.0f}")
            except Exception as e:
                logger.error(f"Error loading portfolio state: {e}")

    def save_state(self):
        """Persist portfolio state to JSON."""
        state = {
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'capital': self.capital,
            'trade_counter': self.trade_counter,
            'last_updated': datetime.now().isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving portfolio state: {e}")

    def get_invested_capital(self):
        """Calculate total capital locked in open positions.

        Returns:
            float: Total invested capital across all open stock positions.
        """
        total = 0
        for pos in self.positions:
            lot_size = pos.get('lot_size', 1)
            if pos.get('is_sell', False):
                # SELL positions lock margin
                total += pos.get('margin_required', pos['entry_premium'] * lot_size * 3)
            else:
                # BUY positions lock premium × lot_size
                total += pos['entry_premium'] * lot_size
        return total

    def get_available_capital(self):
        """Capital available for new trades.

        Returns:
            float: Available capital (total - invested).
        """
        return max(0, self.capital - self.get_invested_capital())

    def get_daily_pnl(self, date_str=None):
        """Get daily P&L for a specific date.

        Args:
            date_str: Date string 'YYYY-MM-DD'. Defaults to today.

        Returns:
            float: Cumulative P&L for the date.
        """
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
        return self.daily_pnl.get(date_str, 0)

    def count_positions_for_stock(self, symbol):
        """Count open positions for a specific stock.

        Args:
            symbol: Stock symbol.

        Returns:
            int: Number of open positions for the symbol.
        """
        return sum(1 for p in self.positions if p.get('stock_symbol') == symbol)

    def count_today_trades(self):
        """Count trades executed today.

        Returns:
            int: Number of trades (open + closed) today.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        open_today = sum(1 for p in self.positions if p['timestamp'].startswith(today))
        closed_today = sum(1 for t in self.closed_trades if t['timestamp'].startswith(today))
        return open_today + closed_today

    def add_signal(self, strategy, stock_symbol, signal_type, strike, entry_premium,
                   lot_size, delta=0, gamma=0, theta=0, iv=0, dte=30,
                   details=None, oi=0, spot_price=0, is_sell=False):
        """Open a new paper position.

        Risk checks:
        - Max positions per stock (MAX_POSITIONS_PER_STOCK)
        - Max total positions (MAX_TOTAL_POSITIONS)
        - Max trades per day (MAX_TRADES_PER_DAY)
        - Capital availability
        - Max per trade limit

        Args:
            strategy: Strategy name ('CPR', 'Gamma Blast', 'PCR+VWAP').
            stock_symbol: Stock symbol ('SBIN', 'CIPLA', etc.).
            signal_type: Signal type ('BUY_CE_CPR', 'BUY_PE_GAMMA', etc.).
            strike: Strike price.
            entry_premium: Option premium at entry.
            lot_size: Lot size for this stock.
            delta, gamma, theta, iv, dte: Greeks and DTE.
            details: dict with target, sl, option_token, spot, etc.
            oi: Open interest at entry.
            spot_price: Stock spot price at entry.
            is_sell: True if selling option (credit strategy).

        Returns:
            dict: The new position, or None if rejected.
        """
        # Risk check: Max positions per stock
        stock_positions = self.count_positions_for_stock(stock_symbol)
        if stock_positions >= MAX_POSITIONS_PER_STOCK:
            logger.warning(f"  REJECT: {stock_symbol} already has {stock_positions} positions "
                           f"(max {MAX_POSITIONS_PER_STOCK})")
            return None

        # Risk check: Max total positions
        if len(self.positions) >= MAX_TOTAL_POSITIONS:
            logger.warning(f"  REJECT: Already at max {MAX_TOTAL_POSITIONS} positions")
            return None

        # Risk check: Max daily trades
        day_trades = self.count_today_trades()
        if day_trades >= MAX_TRADES_PER_DAY:
            logger.warning(f"  REJECT: Max {MAX_TRADES_PER_DAY} trades per day reached")
            return None

        # Capital check
        if is_sell:
            capital_needed = entry_premium * lot_size * 3  # Approx margin for SELL
        else:
            capital_needed = entry_premium * lot_size

        if capital_needed > MAX_PER_TRADE:
            logger.warning(f"  REJECT: Capital needed Rs {capital_needed:,.0f} > "
                           f"max per trade Rs {MAX_PER_TRADE:,.0f}")
            return None

        available = self.get_available_capital()
        if capital_needed > available:
            logger.warning(f"  REJECT: Capital needed Rs {capital_needed:,.0f} > "
                           f"available Rs {available:,.0f}")
            return None

        self.trade_counter += 1
        pos_id = f"STK_{stock_symbol}_{self.trade_counter:04d}"

        position = {
            'id': pos_id,
            'strategy': strategy,
            'stock_symbol': stock_symbol,
            'symbol': stock_symbol,           # Alias for compatibility
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': round(entry_premium, 2),
            'current_premium': round(entry_premium, 2),
            'lot_size': lot_size,
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'iv': iv,
            'dte': dte,
            'is_sell': is_sell,
            'entry_oi': oi,
            'entry_iv': round(iv * 100, 1) if iv < 1 else round(iv, 1),
            'entry_spot': spot_price,
            'timestamp': datetime.now().isoformat(),
            'details': details or {},
            'unrealized_pnl': 0,
            'peak_premium': round(entry_premium, 2),
            'trough_premium': round(entry_premium, 2),
            'trailing_sl': None,
            'breakeven_locked': False,
            'hold_score': 50,
            'max_risk': capital_needed,
        }

        self.positions.append(position)
        self.save_state()

        logger.info(f"  ✅ POSITION OPENED: {pos_id} | {strategy} | {signal_type} | "
                    f"{stock_symbol} {strike} | Prem={entry_premium:.2f} × {lot_size} lots | "
                    f"Capital: Rs {capital_needed:,.0f}")
        return position

    def update_position(self, pos_id, current_premium):
        """Update a position's current premium and compute unrealized P&L.

        Args:
            pos_id: Position ID string.
            current_premium: Current option premium.
        """
        for pos in self.positions:
            if pos['id'] == pos_id:
                pos['current_premium'] = round(current_premium, 2)
                lot_size = pos.get('lot_size', 1)

                if pos.get('is_sell', False):
                    # SELL profit = (entry - current) × lot_size
                    pos['unrealized_pnl'] = round(
                        (pos['entry_premium'] - current_premium) * lot_size, 2
                    )
                else:
                    # BUY profit = (current - entry) × lot_size
                    pos['unrealized_pnl'] = round(
                        (current_premium - pos['entry_premium']) * lot_size, 2
                    )
                return

    def close_position(self, pos_id, exit_premium, exit_reason='MANUAL'):
        """Close a position and move to closed_trades.

        Computes final P&L, deducts estimated brokerage costs, updates daily PnL.

        Args:
            pos_id: Position ID string.
            exit_premium: Premium at exit.
            exit_reason: String reason for exit.
        """
        for i, pos in enumerate(self.positions):
            if pos['id'] == pos_id:
                lot_size = pos.get('lot_size', 1)

                if pos.get('is_sell', False):
                    raw_pnl = (pos['entry_premium'] - exit_premium) * lot_size
                else:
                    raw_pnl = (exit_premium - pos['entry_premium']) * lot_size

                # Estimate brokerage costs (entry + exit)
                cost_config = {
                    'brokerage': BROKERAGE,
                    'stt_sell_rate': STT_SELL,
                    'exchange_charges_rate': EXCHANGE_CHARGES,
                    'gst_rate': GST_RATE,
                }
                try:
                    entry_cost = calc_costs(pos['entry_premium'], lot_size,
                                            pos.get('is_sell', False), cost_config)
                    exit_cost = calc_costs(exit_premium, lot_size,
                                           not pos.get('is_sell', False), cost_config)
                    total_costs = entry_cost + exit_cost
                except Exception:
                    total_costs = BROKERAGE * 2 + exit_premium * lot_size * STT_SELL

                net_pnl = round(raw_pnl - total_costs, 2)

                closed = {
                    **pos,
                    'exit_premium': round(exit_premium, 2),
                    'exit_reason': exit_reason,
                    'exit_time': datetime.now().isoformat(),
                    'raw_pnl': round(raw_pnl, 2),
                    'costs': round(total_costs, 2),
                    'pnl': net_pnl,
                }
                self.closed_trades.append(closed)

                # Update daily PnL
                today = datetime.now().strftime('%Y-%m-%d')
                self.daily_pnl[today] = round(
                    self.daily_pnl.get(today, 0) + net_pnl, 2
                )

                # Remove from open positions
                self.positions.pop(i)
                self.save_state()

                pnl_icon = '🟢' if net_pnl >= 0 else '🔴'
                logger.info(f"  {pnl_icon} POSITION CLOSED: {pos_id} | {exit_reason} | "
                            f"Entry={pos['entry_premium']:.2f} → Exit={exit_premium:.2f} | "
                            f"Raw P&L={raw_pnl:+.0f} | Costs={total_costs:.0f} | "
                            f"Net P&L={net_pnl:+.0f} | Day={self.daily_pnl.get(today, 0):+.0f}")
                return closed
        return None

    def get_portfolio_summary(self):
        """Return portfolio summary dict for dashboard/API.

        Returns:
            dict with capital, invested, available, positions, trades, daily_pnl, etc.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        invested = self.get_invested_capital()
        open_pnl = sum(p.get('unrealized_pnl', 0) for p in self.positions)
        closed_today = [t for t in self.closed_trades if t.get('exit_time', '').startswith(today)]
        closed_pnl = sum(t.get('pnl', 0) for t in closed_today)

        # Win rate
        total_closed = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.get('pnl', 0) > 0)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

        return {
            'capital': self.capital,
            'invested': round(invested, 2),
            'available': round(self.capital - invested, 2),
            'open_positions': len(self.positions),
            'total_closed': total_closed,
            'wins': wins,
            'win_rate': round(win_rate, 1),
            'daily_pnl': round(self.daily_pnl.get(today, 0), 2),
            'open_pnl': round(open_pnl, 2),
            'closed_pnl_today': round(closed_pnl, 2),
            'total_pnl': round(sum(t.get('pnl', 0) for t in self.closed_trades), 2),
        }


# ====================================================================
# STOCK STRATEGY ENGINE — Signal generation from shared strategies
# ====================================================================
class StockStrategyEngine:
    """Generates trading signals for stock options using shared strategy library.

    Uses strategies/ module for instrument-agnostic signal generation.
    Adapted for stock-specific: lot sizes, strike intervals, monthly expiry.

    Strategies:
    1. CPR Breakout (40% allocation) — Primary, from narrow CPR scanner
    2. Gamma Blast (40% allocation) — Coiled spring breakout
    3. PCR+VWAP (20% allocation) — Mean reversion on extreme PCR

    ORB overlay: boosts/reduces quality score based on 15-min Opening Range Break.
    """

    # Strategy allocation weights (for position sizing)
    STRATEGY_ALLOCATION = {
        'CPR': 0.40,
        'Gamma Blast': 0.40,
        'PCR+VWAP': 0.20,
    }

    # Strategy-specific exit multipliers (same as equity bot)
    STRATEGY_EXIT_MULT = {
        'CPR': {'oi': 1.0, 'iv': 1.0},
        'Gamma Blast': {'oi': 1.0, 'iv': 1.0},
        'PCR+VWAP': {'oi': 1.0, 'iv': 1.0},
    }

    def __init__(self, angel, portfolio, scanner, fno_config):
        """Initialize strategy engine.

        Args:
            angel: StockAngelConnection instance.
            portfolio: StockPortfolio instance.
            scanner: StockCPRScanner instance.
            fno_config: StockFnOConfig instance.
        """
        self.angel = angel
        self.portfolio = portfolio
        self.scanner = scanner
        self.fno_config = fno_config
        self.historical_data = {}    # {symbol: DataFrame}
        self.intraday_data = {}      # {symbol: DataFrame}
        self._orb_levels = {}        # {symbol: {high, low, broken_up, broken_down}}

    def load_historical(self, symbol):
        """Load or download historical daily OHLC for a stock.

        Returns:
            DataFrame or None.
        """
        if symbol in self.historical_data:
            return self.historical_data[symbol]

        # Try scanner's cache first
        df = self.scanner._load_stock_historical(symbol)
        if df is not None:
            self.historical_data[symbol] = df
            return df

        return None

    def compute_indicators(self, symbol, current_ohlc=None):
        """Compute all technical indicators for a stock.

        Uses strategies/indicators.py compute_all_indicators() for instrument-agnostic
        calculation of CPR, Camarilla, ATR, IV, VWAP.

        Args:
            symbol: Stock symbol.
            current_ohlc: dict with open, high, low, close, volume for today.

        Returns:
            dict of indicators or None.
        """
        df = self.load_historical(symbol)
        if df is None or len(df) < 15:
            return None

        try:
            indicators = compute_all_indicators(df, current_ohlc)
            return indicators
        except Exception as e:
            logger.error(f"Indicator computation failed for {symbol}: {e}")
            return None

    def check_cpr_breakout_signals(self, symbol, spot, ohlc, indicators, config):
        """Check for CPR breakout signals on a stock.

        Uses shared strategies/cpr_strategy.py — instrument-agnostic.

        Args:
            symbol: Stock symbol.
            spot: Current spot price.
            ohlc: dict with today's open, high, low, close.
            indicators: dict from compute_indicators().
            config: dict with strike_interval, min_premium_buy, etc.

        Returns:
            list of signal dicts.
        """
        try:
            signals = check_cpr_breakout(spot, ohlc, indicators, config)
            # v10.1: VWAP direction filtering for CPR signals
            vwap = indicators.get('vwap', 0)
            filtered = []
            for sig in signals:
                sig['symbol'] = symbol
                sig['strategy'] = 'CPR'
                sig['lot_size'] = config.get('lot_size', 1)
                sig['spot'] = spot
                # Direction confirmation — skip CE if intraday bearish AND below VWAP
                if 'BUY_CE' in sig.get('type', ''):
                    intraday_body = spot - ohlc.get('open', spot)
                    if intraday_body < 0 and vwap > 0 and spot < vwap:
                        logger.info(f"  CPR_DIR_SKIP: {symbol} CE above TC but bearish intraday "
                                   f"(body={intraday_body:.0f}, spot={spot:.0f}<VWAP={vwap:.0f})")
                        continue
                # Direction confirmation — skip PE if intraday bullish AND above VWAP
                elif 'BUY_PE' in sig.get('type', ''):
                    intraday_body = spot - ohlc.get('open', spot)
                    if intraday_body > 0 and vwap > 0 and spot > vwap:
                        logger.info(f"  CPR_DIR_SKIP: {symbol} PE below BC but bullish intraday "
                                   f"(body={intraday_body:+.0f}, spot={spot:.0f}>VWAP={vwap:.0f})")
                        continue
                filtered.append(sig)
            return filtered
        except Exception as e:
            logger.error(f"CPR signal error for {symbol}: {e}")
            return []

    def check_gamma_blast_signals(self, symbol, spot, ohlc, indicators, config):
        """Check for Gamma Blast signals on a stock.

        Uses shared strategies/gamma_blast_strategy.py — coiled spring breakout.

        Args:
            symbol: Stock symbol.
            spot: Current spot price.
            ohlc: dict with today's open, high, low, close.
            indicators: dict from compute_indicators().
            config: dict with lot_size, strike_interval, etc.

        Returns:
            list of signal dicts.
        """
        try:
            signals = check_gamma_blast(spot, ohlc, indicators, config)
            # v10.1: VWAP direction filtering for Gamma Blast signals
            vwap = indicators.get('vwap', 0)
            filtered = []
            for sig in signals:
                sig['symbol'] = symbol
                sig['strategy'] = 'Gamma Blast'
                sig['lot_size'] = config.get('lot_size', 1)
                sig['spot'] = spot
                # VWAP confirmation for Gamma Blast CE
                if 'BUY_CE' in sig.get('type', '') and vwap > 0 and spot < vwap:
                    logger.info(f"  GAMMA_DIR_SKIP: {symbol} CE up but spot={spot:.0f}<VWAP={vwap:.0f}")
                    continue
                # VWAP confirmation for Gamma Blast PE
                elif 'BUY_PE' in sig.get('type', '') and vwap > 0 and spot > vwap:
                    logger.info(f"  GAMMA_DIR_SKIP: {symbol} PE down but spot={spot:.0f}>VWAP={vwap:.0f}")
                    continue
                filtered.append(sig)
            return filtered
        except Exception as e:
            logger.error(f"Gamma Blast signal error for {symbol}: {e}")
            return []

    def check_pcr_vwap_signals(self, symbol, spot, indicators, config):
        """Check for PCR+VWAP signals on a stock.

        PCR data comes from real option chain OI when available,
        otherwise uses proxy from price changes.

        Args:
            symbol: Stock symbol.
            spot: Current spot price.
            indicators: dict from compute_indicators() — includes pcr, vwap.
            config: dict with lot_size, strike_interval, etc.

        Returns:
            list of signal dicts.
        """
        try:
            from strategies.pcr_vwap_strategy import check_pcr_vwap
            signals = check_pcr_vwap(spot, indicators, config)
            for sig in signals:
                sig['symbol'] = symbol
                sig['strategy'] = 'PCR+VWAP'
                sig['lot_size'] = config.get('lot_size', 1)
            return signals
        except Exception as e:
            logger.error(f"PCR+VWAP signal error for {symbol}: {e}")
            return []

    def check_orb_confirmation(self, symbol, spot, signal_direction):
        """Check if Opening Range Breakout (ORB) confirms the signal direction.

        ORB = first 15 min candle (9:15-9:30). Break above = bullish, below = bearish.
        This is an OVERLAY — enhances/reduces quality score, not a standalone signal.

        Args:
            symbol: Stock symbol.
            spot: Current spot price.
            signal_direction: 'CE' (bullish) or 'PE' (bearish).

        Returns:
            int: Score adjustment (+15 for confirm, -10 for contradict, 0 for no data).
        """
        orb = self._orb_levels.get(symbol)
        if not orb:
            return 0

        orb_high = orb.get('high', 0)
        orb_low = orb.get('low', 0)

        if signal_direction == 'CE':
            # Bullish signal — ORB break above confirms
            if spot > orb_high:
                return 15
            elif spot < orb_low:
                return -10
        elif signal_direction == 'PE':
            # Bearish signal — ORB break below confirms
            if spot < orb_low:
                return 15
            elif spot > orb_high:
                return -10

        return 0

    def update_orb_levels(self, symbol, candle_high, candle_low):
        """Store Opening Range (9:15-9:30) for ORB overlay.

        Args:
            symbol: Stock symbol.
            candle_high: High of 9:15-9:30 candle.
            candle_low: Low of 9:15-9:30 candle.
        """
        self._orb_levels[symbol] = {
            'high': candle_high,
            'low': candle_low,
        }

    def score_signal(self, signal, spot, indicators, vix=None):
        """Compute quality score for a signal using shared scoring module.

        Args:
            signal: Signal dict from strategy check.
            spot: Current spot price.
            indicators: dict from compute_indicators().
            vix: Current VIX value (optional).

        Returns:
            int: Quality score 0-100.
        """
        try:
            score = compute_signal_score(signal, spot, indicators, vix)
            return score
        except Exception:
            return 50  # Default moderate


# ====================================================================
# STOCK PAPER TRADER — Main orchestrator with FULL exit system
# ====================================================================
class StockPaperTrader:
    """Main stock options paper trading engine.

    Orchestrates: Morning CPR scan → Signal generation → Execution → Exit management.

    Complete exit system matching paper_trader.py v8.0 EXACTLY:
    - Circuit breaker (daily loss limit)
    - Grace period (3 min after entry)
    - EOD force close (3:20 PM)
    - Premium update (market LTP → delta+gamma+theta fallback)
    - Hold score computation (signal-based exit decisions)
    - Signal weak exit (hold_score < 40, losing, held > 1h)
    - TIME_EXIT (4h with < 15% profit, hold_score override)
    - 3-phase TSL: breakeven (15%), trail (25%/30%), tight (40%/20%)
    - Signal-based TSL ratchet (hold_score >= 60: 85% of current)
    - OI/IV exit: v8.0 SL tightening (not forced exit)
    - Gamma shield (forced exit for short positions)
    - Static exit (TARGET_HIT, SL_HIT, EXPIRY)
    """

    def __init__(self):
        """Initialize the stock paper trader with all subsystems."""
        self.fno_config = StockFnOConfig()
        self.angel = StockAngelConnection()
        self.angel.fno_config_ref = self.fno_config  # Back-reference for strike selection
        self.portfolio = StockPortfolio()
        self.scanner = StockCPRScanner(self.fno_config, self.angel)
        self.engine = StockStrategyEngine(self.angel, self.portfolio, self.scanner, self.fno_config)

        # Exit tracking
        self.exit_history = []
        self.ghost_zone_losses = {}
        self.pending_reverses = {}      # v10.1: {symbol: {opt_type, spot, ...}} for breakout failure reversal
        self.daily_trade_count = 0
        self.current_vix = None

        # Caches
        self._option_ltp_cache = {}      # {cache_key: {ltp, time}}
        self._greeks_cache = {}          # {cache_key: {data, time}}
        self._spot_cache = {}            # {symbol: {price, time}}

        # Scanner watchlist for today
        self._todays_watchlist = []

        # State
        self._running = False
        self._initialized = False
        self.data_logger = None  # v10.2e: Live data logger for backtesting

    def initialize(self):
        """Connect to Angel API and load instruments.

        Returns:
            bool: True if initialization successful.
        """
        if self._initialized:
            return True

        logger.info("=" * 70)
        logger.info("STOCK OPTIONS PAPER TRADER v1.0 — Initializing")
        logger.info("=" * 70)

        # Connect to Angel One
        if not self.angel.connect('Historical'):
            logger.error("Failed to connect to Angel One API")
            return False

        # Load instruments master
        self.angel.load_instruments()
        if self.angel.instruments is None:
            logger.error("Failed to load instruments master")
            return False

        # Refresh F&O config from instruments
        self.fno_config.refresh_from_instruments(self.angel)

        self._initialized = True
        logger.info(f"Initialization complete. Capital: Rs {self.portfolio.capital:,.0f}")
        return True

    def get_stock_spot(self, symbol):
        """Get current stock spot price with 15s cache.

        Priority: Cache → Angel LTP API → last close from historical.

        Args:
            symbol: Stock symbol.

        Returns:
            float spot price or None.
        """
        # Check cache (15s TTL)
        cached = self._spot_cache.get(symbol)
        if cached and (datetime.now() - cached['time']).total_seconds() < 15:
            return cached['price']

        # Try Angel LTP
        token = self.angel.find_stock_token(symbol)
        if token:
            ltp = self.angel.get_ltp('NSE', token)
            if ltp and ltp > 0:
                self._spot_cache[symbol] = {'price': ltp, 'time': datetime.now()}
                return ltp

        # Fallback: last close from historical
        df = self.engine.load_historical(symbol)
        if df is not None and len(df) > 0:
            last_close = df['Close'].iloc[-1]
            self._spot_cache[symbol] = {'price': last_close, 'time': datetime.now()}
            return last_close

        return None

    def get_vix_multiplier(self):
        """Return threshold multiplier based on VIX level.

        Low VIX (<14): 1.5x (harder to enter, wider exits)
        Normal (14-20): 1.0x
        High VIX (>20): 0.8x (easier to enter)

        Returns:
            float: VIX multiplier.
        """
        vix = self.current_vix
        if vix is None:
            return 1.0
        if vix < VIX_LOW_THRESHOLD:
            return VIX_LOW_MULTIPLIER
        elif vix > VIX_HIGH_THRESHOLD:
            return VIX_HIGH_MULTIPLIER
        return 1.0

    def _get_nearest_expiry(self, symbol):
        """Get nearest monthly expiry for stock options.

        Stock options have monthly expiry (last Thursday of month).
        Uses instruments master to find exact expiry dates.

        Args:
            symbol: Stock symbol.

        Returns:
            str: Expiry in 'ddMONyyyy' format (e.g. '27MAR2026') or None.
        """
        if self.angel.instruments is None:
            return None

        mask = ((self.angel.instruments['name'] == symbol) &
                (self.angel.instruments['exch_seg'] == 'NFO') &
                (self.angel.instruments['instrumenttype'] == 'OPTSTK'))
        matches = self.angel.instruments[mask]
        if len(matches) == 0:
            return None

        # Parse expiry dates and find nearest future expiry
        today = datetime.now().date()
        try:
            expiries = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True).dt.date
            future = expiries[expiries >= today]
            if len(future) == 0:
                return None
            nearest = future.min()
            return nearest.strftime('%d%b%Y').upper()
        except Exception:
            return None

    def _get_option_ltp(self, pos):
        """Get real-time option LTP for a position, with 15s cache.

        Looks up option token from position details or instruments master.
        Falls back to None if unavailable.

        Args:
            pos: Position dict.

        Returns:
            float LTP or None.
        """
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        option_token = details.get('option_token')
        exchange = details.get('option_exchange', 'NFO')

        # Backfill for positions opened without stored token
        if not option_token:
            symbol = pos.get('stock_symbol', pos.get('symbol', ''))
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
            expiry = self._get_nearest_expiry(symbol)
            if expiry:
                option_info = self.angel.find_option_tokens(symbol, expiry, pos['strike'], opt_type)
                if option_info:
                    option_token = str(option_info.get('token', ''))
                    exchange = 'NFO'  # Stock options always on NFO
                    # Store in position so we don't re-lookup
                    if isinstance(details, dict):
                        details['option_token'] = option_token
                        details['option_exchange'] = exchange
                    else:
                        pos['details'] = {'option_token': option_token, 'option_exchange': exchange}

        if not option_token:
            return None

        # Check cache (15-second TTL)
        cache_key = f"{exchange}_{option_token}"
        cached = self._option_ltp_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 15:
            return cached['ltp']

        try:
            ltp = self.angel.get_ltp(exchange, option_token)
            if ltp and ltp > 0:
                self._option_ltp_cache[cache_key] = {'ltp': ltp, 'time': datetime.now()}
                return ltp
        except Exception as e:
            logger.debug(f"  Option LTP fetch failed for {cache_key}: {e}")

        return None

    def fetch_current_oi_iv(self, pos):
        """Fetch current OI and IV for a position using Angel SmartAPI.

        Uses market data for OI and option Greeks API for IV.
        Results cached for 60s to avoid rate limiting.

        Args:
            pos: Position dict.

        Returns:
            tuple: (current_oi, current_iv) — either may be None.
        """
        try:
            symbol = pos.get('stock_symbol', pos.get('symbol', ''))
            strike = pos['strike']
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'

            # Check cache (60s TTL)
            cache_key = f"{symbol}_{strike}_{opt_type}"
            cached = self._greeks_cache.get(cache_key)
            if cached and (datetime.now() - cached['time']).total_seconds() < 60:
                return cached['data']

            current_oi = None
            current_iv = None

            # Method 1: Market data for OI
            expiry = self._get_nearest_expiry(symbol)
            if expiry:
                option_info = self.angel.find_option_tokens(symbol, expiry, strike, opt_type)
                if option_info:
                    token = str(option_info.get('token', ''))
                    mkt_data = self.angel.get_market_data('NFO', token)
                    if mkt_data:
                        current_oi = mkt_data.get('opnInterest', mkt_data.get('oi', 0))
                        if mkt_data.get('impliedVolatility'):
                            current_iv = mkt_data['impliedVolatility']

            # Method 2: Option Greeks API for IV
            if current_iv is None and expiry:
                greeks_data = self.angel.get_option_greeks(symbol, expiry)
                if greeks_data and isinstance(greeks_data, list):
                    for row in greeks_data:
                        row_strike = float(row.get('strikePrice', row.get('strike', 0)))
                        row_type = row.get('optionType', row.get('type', ''))
                        if abs(row_strike - strike) < 10 and opt_type in str(row_type).upper():
                            current_iv = row.get('impliedVolatility', row.get('iv', None))
                            if current_oi is None:
                                current_oi = row.get('openInterest', row.get('oi', None))
                            break

            # Ensure numeric
            if current_oi is not None:
                current_oi = float(current_oi)
            if current_iv is not None:
                current_iv = float(current_iv)

            result = (current_oi, current_iv)
            self._greeks_cache[cache_key] = {'data': result, 'time': datetime.now()}

            if current_oi is not None or current_iv is not None:
                logger.info(f"  OI/IV fetched: {pos['id']} OI={current_oi} IV={current_iv}")

            return result
        except Exception as e:
            logger.info(f"  OI/IV fetch failed for {pos['id']}: {e}")
            return None, None

    def _track_exit(self, pos, exit_reason):
        """Track exit for cooldown logic. Called after every close_position.

        Args:
            pos: Position dict (before removal).
            exit_reason: String exit reason.
        """
        direction = 'CE' if 'CE' in pos['signal_type'] else 'PE'
        self.exit_history.append({
            'symbol': pos.get('stock_symbol', pos.get('symbol', '')),
            'direction': direction,
            'time': datetime.now(),
            'reason': exit_reason,
        })
        # Cleanup old exit history (keep last 2 hours only)
        cutoff = datetime.now() - timedelta(hours=2)
        self.exit_history = [eh for eh in self.exit_history if eh['time'] > cutoff]

    # ================================================================
    # CHECK_OI_IV_EXIT — v8.0: SL tightening instead of forced exit
    # ================================================================
    def check_oi_iv_exit(self, pos, current_premium, current_iv_pct=None):
        """Check if position should exit based on OI velocity + IV changes + Gamma risk.

        v8.0 Architecture: OI/IV signals TIGHTEN SL instead of forcing exit.
        Only GAMMA_SHIELD forces immediate exit (real gamma risk for shorts).

        Features:
        - Time-adaptive OI thresholds (first hour 50%, mid-day 35%, last hour 40%)
        - VIX multiplier applied to thresholds
        - Strategy-specific threshold multiplier
        - Direction-aware IV (only hurts if IV moves against position)
        - Profitable + moving toward target → hold despite OI/IV
        - Dynamic loss threshold (15% of entry cost)

        Args:
            pos: Position dict.
            current_premium: Current option premium.
            current_iv_pct: Implied vol from market (optional).

        Returns:
            tuple: (exit_reason, should_reverse) or (None, False).
        """
        entry_oi = pos.get('entry_oi', 0)
        entry_iv = pos.get('entry_iv', 0)

        # Fetch current OI and IV
        current_oi, fetched_iv = self.fetch_current_oi_iv(pos)

        # Prefer implied vol from market LTP, then fetched, then HV fallback
        current_iv = current_iv_pct if current_iv_pct and current_iv_pct > 0 else fetched_iv
        if current_iv is None or current_iv == 0:
            symbol = pos.get('stock_symbol', pos.get('symbol', ''))
            df = self.engine.historical_data.get(symbol)
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

        # Time-adaptive OI threshold
        now_time = datetime.now().time()
        if now_time < dtime(10, 15):
            oi_threshold = OI_SURGE_FIRST_HOUR_PCT   # 50% — first hour OI massive
        elif now_time < dtime(14, 0):
            oi_threshold = OI_SURGE_MID_DAY_PCT      # 35% — normal
        else:
            oi_threshold = OI_SURGE_LAST_HOUR_PCT    # 40% — close compression

        # Apply VIX multiplier
        vix_mult = self.get_vix_multiplier()
        oi_threshold *= vix_mult

        # Strategy-specific threshold multiplier
        strat_name = pos.get('strategy', '').replace(' (Reversal)', '')
        strat_mult = self.engine.STRATEGY_EXIT_MULT.get(strat_name, {'oi': 1.0, 'iv': 1.0})
        oi_threshold *= strat_mult['oi']

        # IV threshold (stock options use standard IV_SPIKE_PCT, not BANKNIFTY's 55%)
        iv_threshold = IV_SPIKE_PCT
        iv_threshold *= vix_mult
        iv_threshold *= strat_mult['iv']

        # If position is profitable AND moving toward target, hold despite OI/IV
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        target = details.get('target', 0)
        if target > 0 and pos.get('entry_premium', 0) > 0:
            is_sell = pos.get('is_sell', False)
            if is_sell:
                moving_toward_target = current_premium < pos['entry_premium']
            else:
                moving_toward_target = current_premium > pos['entry_premium']
            pnl = pos.get('unrealized_pnl', 0)
            if moving_toward_target and pnl > 0:
                logger.info(f"  OI_TREND_HOLD: {pos['id']} ({strat_name}) profitable Rs {pnl:.0f} "
                            f"& moving toward target. Holding despite OI={oi_change:.1f}%/IV={iv_change:.1f}%.")
                return None, False

        # Get unrealized PnL for loss-only exits
        unrealized_pnl = pos.get('unrealized_pnl', 0)

        # Dynamic loss threshold: 15% of entry cost, min -Rs 1500
        entry_premium = pos.get('entry_premium', 0)
        lot_size = pos.get('lot_size', 1)
        entry_cost = entry_premium * lot_size
        oi_loss_threshold = max(-entry_cost * 0.15, -1500)

        # Rule 1: OI surge — v8.0: tighten SL, never force exit
        if oi_change > oi_threshold:
            if unrealized_pnl >= oi_loss_threshold:
                logger.info(f"  OI_HOLD: {pos['id']} OI={oi_change:.1f}% > {oi_threshold:.0f}% "
                            f"but PnL Rs {unrealized_pnl:.0f} >= threshold Rs {oi_loss_threshold:.0f}. Holding.")
            else:
                logger.info(f"  OI_SL_TIGHTEN: {pos['id']} OI changed {oi_change:.1f}% > {oi_threshold:.0f}% "
                            f"PnL Rs {unrealized_pnl:.0f} < threshold Rs {oi_loss_threshold:.0f}. "
                            f"(entry={entry_oi}, current={current_oi}, time={now_time.strftime('%H:%M')}). Tightening SL.")
                return 'OI_SL_TIGHTEN', False

        # Rule 2: IV change — direction-aware, tighten SL
        is_sell = pos.get('is_sell', False)
        iv_raw_change = ((current_iv - entry_iv) / entry_iv * 100) if entry_iv > 0 else 0
        iv_hurts_position = (iv_raw_change < 0 and not is_sell) or (iv_raw_change > 0 and is_sell)

        if iv_change > iv_threshold:
            entry_premium = pos.get('entry_premium', 0)
            if entry_premium < 30:
                # Low premium options have noisy IV — skip
                logger.info(f"  IV_HOLD_LOW_PREM: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                            f"but entry premium Rs {entry_premium:.2f} < 30. Holding (noisy IV on cheap options).")
            elif not iv_hurts_position:
                # IV moving in our FAVOR — no need to tighten
                logger.info(f"  IV_FAVORABLE: {pos['id']} IV changed {iv_raw_change:+.1f}% "
                            f"({'DROP' if iv_raw_change < 0 else 'RISE'}) — favorable for "
                            f"{'SELL' if is_sell else 'BUY'} position. Holding.")
            elif unrealized_pnl >= oi_loss_threshold:
                logger.info(f"  IV_HOLD: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                            f"but PnL Rs {unrealized_pnl:.0f} >= threshold Rs {oi_loss_threshold:.0f}. Holding.")
            else:
                logger.info(f"  IV_SL_TIGHTEN: {pos['id']} IV changed {iv_raw_change:+.1f}% > {iv_threshold:.0f}% "
                            f"PnL Rs {unrealized_pnl:.0f} — hurting {'SELL' if is_sell else 'BUY'} position. "
                            f"(entry={entry_iv}%, current={current_iv}%). Tightening SL.")
                return 'IV_SL_TIGHTEN', False

        # Rule 3: Combined OI+IV — tighten SL aggressively (10% tighter)
        OI_IV_COMBO_OI = 20  # Combined exit: OI >20%
        OI_IV_COMBO_IV = 25  # AND IV >25%
        combo_oi = OI_IV_COMBO_OI * vix_mult
        combo_iv = OI_IV_COMBO_IV * vix_mult
        if oi_change > combo_oi and iv_change > combo_iv:
            logger.info(f"  OI_IV_COMBO_TIGHTEN: {pos['id']} OI={oi_change:.1f}%, IV={iv_change:.1f}%. "
                        f"Tightening SL aggressively.")
            return 'OI_IV_SL_TIGHTEN', False

        # Rule 4: Gamma shield for short positions
        if pos.get('is_sell') and abs(pos.get('gamma', 0)) > GAMMA_SHIELD_THRESHOLD:
            logger.info(f"  GAMMA_SHIELD: {pos['id']} gamma={pos.get('gamma', 0):.6f} > {GAMMA_SHIELD_THRESHOLD}")
            return 'GAMMA_SHIELD_EXIT', False

        return None, False

    # ================================================================
    # CHECK_EXITS — Complete exit system matching paper_trader.py v8.0
    # ================================================================
    def check_exits(self):
        """Check if any open positions should be exited.

        Complete exit flow (same as paper_trader.py v8.0):
        1. Circuit breaker (daily loss > MAX_DAILY_LOSS)
        2. For each position:
           a. Grace period (180s after entry)
           b. EOD force close (after 15:20)
           c. Get spot, compute premium (market LTP → delta+gamma fallback)
           d. Compute hold_score (after 30 min hold)
           e. Signal weak exit (hold_score < 40, losing, held > 1h)
           f. TIME_EXIT (4h with < 15% profit, hold_score override)
           g. TSL 3 phases for BUY (breakeven 15%, trail 25%/30%, tight 40%/20%)
           h. TSL 3 phases for SELL (inverted)
           i. Signal-based TSL ratchet (hold_score >= 60)
           j. TSL hit check
           k. OI/IV exit (v8.0: tighten SL, not force, except GAMMA_SHIELD)
           l. Static exit (TARGET_HIT, SL_HIT, EXPIRY)
        """
        if not self.portfolio.positions:
            return

        logger.info("\n  Checking exits for open stock positions...")

        # ---- CIRCUIT BREAKER: Close ALL if daily loss exceeds limit ----
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.portfolio.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"  CIRCUIT_BREAKER: Daily stock loss Rs {daily_loss:,.0f} > limit Rs {MAX_DAILY_LOSS:,.0f}")
            logger.warning(f"  Closing ALL {len(self.portfolio.positions)} open stock positions!")
            for pos in list(self.portfolio.positions):
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'CIRCUIT_BREAKER')
                self._track_exit(pos, 'CIRCUIT_BREAKER')
            try:
                from trade_notifier import send_message
                send_message(f"<b>🚨 STOCK CIRCUIT BREAKER</b>\n"
                             f"Daily stock loss: Rs {daily_loss:,.0f}\n"
                             f"All stock positions closed. Trading halted.")
            except Exception:
                pass
            self.portfolio.save_state()
            return

        for pos in list(self.portfolio.positions):
            # ---- Grace period: Skip positions opened < 3 min ago ----
            entry_time = datetime.fromisoformat(pos['timestamp'])
            elapsed_secs = (datetime.now() - entry_time).total_seconds()
            if elapsed_secs < GRACE_PERIOD_SECONDS:
                logger.info(f"  GRACE: {pos['id']} opened {int(elapsed_secs)}s ago (need {GRACE_PERIOD_SECONDS}s)")
                continue

            # ---- EOD FORCE CLOSE: 10 min before market close ----
            if datetime.now().time() > EOD_FORCE_CLOSE_TIME:
                logger.info(f"  EOD_CLOSE: {pos['id']} force close (market closing at 15:30)")
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'EOD_FORCE_CLOSE')
                self._track_exit(pos, 'EOD_FORCE_CLOSE')
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = pos.get('lot_size', 1)
                    cap = pos['entry_premium'] * lot_size
                    notify_trade_exit(
                        market="STOCKS", strategy=pos['strategy'],
                        symbol=pos.get('stock_symbol', pos.get('symbol', '')),
                        signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current, entry_time=pos['timestamp'],
                        pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                        exit_reason='EOD_FORCE_CLOSE')
                except Exception:
                    pass
                continue

            symbol = pos.get('stock_symbol', pos.get('symbol', ''))
            spot = self.get_stock_spot(symbol)
            if not spot:
                continue

            # ---- PREMIUM UPDATE: Real market LTP with fallback to delta+gamma+theta ----
            entry_spot = pos.get('entry_spot', 0)
            if entry_spot == 0 or entry_spot is None:
                entry_spot = pos.get('details', {}).get('spot', spot) if isinstance(pos.get('details'), dict) else spot
            entry_spot = float(entry_spot) if entry_spot else spot
            spot_change = (spot or 0) - (entry_spot or 0)
            delta_val = float(pos.get('delta', 0) or 0) if pos.get('delta') is not None else 0.5
            gamma_val = float(pos.get('gamma', 0) or 0)
            hours_held = (datetime.now() - entry_time).total_seconds() / 3600

            # Try real option LTP first (15s cached)
            real_option_ltp = self._get_option_ltp(pos)
            if real_option_ltp is not None:
                current_premium = real_option_ltp
                premium_source = 'MARKET'
            else:
                # Fallback: delta+gamma+theta approximation
                premium_delta = delta_val * spot_change + 0.5 * gamma_val * (spot_change ** 2)
                theta_val = pos.get('theta', 0)
                time_decay = theta_val * (hours_held / 24)
                current_premium = max(pos['entry_premium'] + premium_delta + time_decay, 0.05)
                premium_source = 'APPROX'

            self.portfolio.update_position(pos['id'], current_premium)

            # ---- COMPUTE HOLD SCORE for signal-based exit decisions ----
            hold_score = 50  # Default moderate
            hold_mins = (datetime.now() - entry_time).total_seconds() / 60
            if hold_mins >= HOLD_SCORE_MIN_HOLD_MINS:
                try:
                    hold_indicators = self.engine.compute_indicators(symbol,
                        {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0})
                    if hold_indicators:
                        hold_score = compute_hold_score(pos, spot, hold_indicators)
                except Exception:
                    hold_score = 50
            pos['hold_score'] = hold_score
            hold_label = ('STRONG' if hold_score >= HOLD_SCORE_STRONG
                          else 'MODERATE' if hold_score >= HOLD_SCORE_WEAK
                          else 'WEAK')

            # ---- SIGNAL WEAK EXIT — early exit for losing positions with weak signals ----
            if (hold_mins >= HOLD_SCORE_MIN_HOLD_MINS
                    and hold_score < HOLD_SCORE_WEAK
                    and pos.get('unrealized_pnl', 0) < 0
                    and hours_held > 1):
                logger.info(f"  SIGNAL_WEAK_EXIT: {pos['id']} hold_score={hold_score} ({hold_label}) "
                            f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} — signals reversed, early exit")
                self.portfolio.close_position(pos['id'], current_premium, 'SIGNAL_WEAK_EXIT')
                self._track_exit(pos, 'SIGNAL_WEAK_EXIT')
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = pos.get('lot_size', 1)
                    cap = pos['entry_premium'] * lot_size
                    notify_trade_exit(
                        market="STOCKS", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                        exit_reason='SIGNAL_WEAK_EXIT')
                except Exception:
                    pass
                continue

            # ---- TIME-BASED EXIT: Close stale positions (>4h with <15% profit) ----
            if hours_held > 4:
                max_risk = pos.get('max_risk', pos['entry_premium'] * pos.get('lot_size', 1))
                profit_pct = (pos['unrealized_pnl'] / max(max_risk, 1)) * 100
                if abs(profit_pct) < 15:
                    # Override TIME_EXIT if hold score is STRONG
                    if hold_score >= HOLD_SCORE_STRONG:
                        logger.info(f"  SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                                    f"— overriding TIME_EXIT (signals still valid)")
                    else:
                        logger.info(f"  TIME_EXIT: {pos['id']} held {hours_held:.1f}h with only "
                                    f"{profit_pct:.1f}% profit (hold_score={hold_score} {hold_label})")
                        self.portfolio.close_position(pos['id'], current_premium, 'TIME_EXIT_NO_PROGRESS')
                        self._track_exit(pos, 'TIME_EXIT_NO_PROGRESS')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = pos.get('lot_size', 1)
                            cap = pos['entry_premium'] * lot_size
                            notify_trade_exit(
                                market="STOCKS", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=pos['entry_premium'],
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='TIME_EXIT_NO_PROGRESS')
                        except Exception:
                            pass
                        continue

            # ---- v10.1: BREAKOUT FAILURE DETECTION ----
            # If premium never moved above entry or drops fast, exit early (+ optional reverse)
            if BREAKOUT_FAIL_REVERSE_ENABLED and GRACE_PERIOD_SECONDS < elapsed_secs <= BREAKOUT_FAIL_CHECK_MINUTES * 60:
                entry_prem_bf = pos['entry_premium']
                if entry_prem_bf > 0:
                    if not pos['is_sell']:
                        peak_bf = pos.get('peak_premium', entry_prem_bf)
                        gain_bf = (peak_bf - entry_prem_bf) / entry_prem_bf * 100
                        drop_bf = (entry_prem_bf - current_premium) / entry_prem_bf * 100
                    else:
                        peak_bf = pos.get('trough_premium', entry_prem_bf)
                        gain_bf = (entry_prem_bf - peak_bf) / entry_prem_bf * 100
                        drop_bf = (current_premium - entry_prem_bf) / entry_prem_bf * 100

                    # Rapid drop: exit + queue reverse
                    if drop_bf > BREAKOUT_FAIL_REVERSE_DROP_PCT:
                        logger.info(f"  BREAKOUT_FAIL_REVERSE: {pos['id']} dropped {drop_bf:.1f}% in {int(elapsed_secs)}s")
                        self.portfolio.close_position(pos['id'], current_premium, 'BREAKOUT_FAIL_REVERSE')
                        self._track_exit(pos, 'BREAKOUT_FAIL_REVERSE')
                        rev_type = 'PE' if 'CE' in pos['signal_type'] else 'CE'
                        self.pending_reverses[symbol] = {
                            'opt_type': rev_type, 'spot': spot,
                            'source_id': pos['id'], 'timestamp': datetime.now().isoformat()
                        }
                        logger.info(f"  REVERSE_QUEUED: {symbol} BUY_{rev_type}")
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = pos.get('lot_size', 1)
                            cap = pos.get('margin_required', entry_prem_bf * lot_size * 3) if pos['is_sell'] else entry_prem_bf * lot_size
                            notify_trade_exit(market="STOCKS", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=entry_prem_bf,
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='BREAKOUT_FAIL_REVERSE')
                        except Exception:
                            pass
                        continue

                    # Near 5-min mark: no movement — exit (no reverse)
                    if elapsed_secs >= (BREAKOUT_FAIL_CHECK_MINUTES * 60 - 15) and gain_bf < BREAKOUT_FAIL_MIN_GAIN_PCT:
                        logger.info(f"  BREAKOUT_FAIL: {pos['id']} peak gain={gain_bf:.1f}% < {BREAKOUT_FAIL_MIN_GAIN_PCT}% in {int(elapsed_secs)}s")
                        self.portfolio.close_position(pos['id'], current_premium, 'BREAKOUT_FAIL')
                        self._track_exit(pos, 'BREAKOUT_FAIL')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = pos.get('lot_size', 1)
                            cap = pos.get('margin_required', entry_prem_bf * lot_size * 3) if pos['is_sell'] else entry_prem_bf * lot_size
                            notify_trade_exit(market="STOCKS", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=entry_prem_bf,
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='BREAKOUT_FAIL')
                        except Exception:
                            pass
                        continue

            # ---- TRAILING STOP LOSS — 3-Phase System ----
            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            # v10.1: Use dynamically updated target (from trailing target) if available
            target = pos.get('target', details.get('target', pos['entry_premium'] * 1.5))
            sl = details.get('sl', pos['entry_premium'] * 0.5)

            if not pos['is_sell']:
                # ============================================
                # BUY positions: premium going UP is profit
                # ============================================
                entry_prem = pos['entry_premium']
                premium_gain_pct = ((current_premium - entry_prem) / entry_prem * 100
                                    if entry_prem > 0 else 0)

                # Update peak premium
                if current_premium > pos.get('peak_premium', entry_prem):
                    pos['peak_premium'] = round(current_premium, 2)

                peak = pos.get('peak_premium', current_premium)
                peak_gain_pct = ((peak - entry_prem) / entry_prem * 100
                                 if entry_prem > 0 else 0)
                profit_from_entry = peak - entry_prem

                # Phase 1: Lock breakeven when premium gains 15%+
                if peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 1.03, 2)  # entry + 3% buffer
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} gained {peak_gain_pct:.0f}% "
                                f"→ locked SL at Rs {pos['trailing_sl']:.2f}")

                # Phase 3: Tight trail when premium gained 40%+ (20% below peak profit)
                if peak_gain_pct >= TSL_TIGHT_GAIN_PCT:
                    new_trailing_sl = round(peak - (profit_from_entry * TSL_TIGHT_DISTANCE_PCT / 100), 2)
                    new_trailing_sl = max(new_trailing_sl, pos.get('trailing_sl') or 0)
                    if new_trailing_sl > (pos.get('trailing_sl') or 0):
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TIGHT: {pos['id']} SL→Rs {pos['trailing_sl']:.2f} "
                                    f"(peak={peak:.2f} +{peak_gain_pct:.0f}%, phase3)")

                # Phase 2: Trail when premium gained 25%+ (30% below peak profit)
                elif peak_gain_pct >= TSL_TRAIL_GAIN_PCT:
                    new_trailing_sl = round(peak - (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    new_trailing_sl = max(new_trailing_sl, pos.get('trailing_sl') or 0)
                    if new_trailing_sl > (pos.get('trailing_sl') or 0):
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SL→Rs {pos['trailing_sl']:.2f} "
                                    f"(peak={peak:.2f} +{peak_gain_pct:.0f}%)")

                # Signal-based TSL ratchet — if signals STRONG, raise TSL aggressively
                if (hold_score >= HOLD_SCORE_STRONG
                        and peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                        and pos.get('trailing_sl')):
                    ratchet_tsl = round(current_premium * 0.85, 2)
                    if ratchet_tsl > pos['trailing_sl']:
                        old_tsl = pos['trailing_sl']
                        pos['trailing_sl'] = ratchet_tsl
                        logger.info(f"  SIGNAL_HOLD_RATCHET: {pos['id']} hold_score={hold_score} "
                                    f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                    f"(85% of current Rs {current_premium:.2f})")

                # Check trailing SL hit (BUY: premium drops below trailing SL)
                if pos.get('trailing_sl') and current_premium <= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} premium {current_premium:.2f} "
                                f"<= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current_premium, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    try:
                        from trade_notifier import notify_trade_exit
                        lot_size = pos.get('lot_size', 1)
                        cap = entry_prem * lot_size
                        notify_trade_exit(
                            market="STOCKS", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=entry_prem,
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                            exit_reason='TRAILING_SL_HIT')
                    except Exception:
                        pass
                    continue

            else:
                # ============================================
                # SELL positions: premium going DOWN is profit
                # ============================================
                entry_prem = pos['entry_premium']
                premium_gain_pct = ((entry_prem - current_premium) / entry_prem * 100
                                    if entry_prem > 0 else 0)

                # Update trough premium (lowest seen)
                if current_premium < pos.get('trough_premium', entry_prem):
                    pos['trough_premium'] = round(current_premium, 2)

                trough = pos.get('trough_premium', current_premium)
                trough_gain_pct = ((entry_prem - trough) / entry_prem * 100
                                   if entry_prem > 0 else 0)
                profit_from_entry = entry_prem - trough

                # Phase 1: Lock breakeven when premium drops 15%+ from entry (SELL profit)
                if trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 0.97, 2)  # entry - 3% buffer
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} SELL gained {trough_gain_pct:.0f}% "
                                f"→ locked SL at Rs {pos['trailing_sl']:.2f}")

                # Phase 3: Tight trail when premium dropped 40%+ (20% above trough profit)
                if trough_gain_pct >= TSL_TIGHT_GAIN_PCT:
                    new_trailing_sl = round(trough + (profit_from_entry * TSL_TIGHT_DISTANCE_PCT / 100), 2)
                    if pos.get('trailing_sl') is None or new_trailing_sl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TIGHT: {pos['id']} SELL SL→Rs {pos['trailing_sl']:.2f} "
                                    f"(trough={trough:.2f} -{trough_gain_pct:.0f}%, phase3)")

                # Phase 2: Trail when premium dropped 25%+ (30% above trough profit)
                elif trough_gain_pct >= TSL_TRAIL_GAIN_PCT:
                    new_trailing_sl = round(trough + (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    if pos.get('trailing_sl') is None or new_trailing_sl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SELL SL→Rs {pos['trailing_sl']:.2f} "
                                    f"(trough={trough:.2f} -{trough_gain_pct:.0f}%)")

                # Signal-based TSL ratchet for SELL — if signals STRONG, tighten TSL
                if (hold_score >= HOLD_SCORE_STRONG
                        and trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                        and pos.get('trailing_sl')):
                    ratchet_tsl = round(current_premium * 1.15, 2)  # SELL: 15% above current
                    if ratchet_tsl < pos['trailing_sl']:
                        old_tsl = pos['trailing_sl']
                        pos['trailing_sl'] = ratchet_tsl
                        logger.info(f"  SIGNAL_HOLD_RATCHET: {pos['id']} SELL hold_score={hold_score} "
                                    f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                    f"(115% of current Rs {current_premium:.2f})")

                # Check trailing SL hit (SELL: premium rises above trailing SL)
                if pos.get('trailing_sl') and current_premium >= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} SELL premium {current_premium:.2f} "
                                f">= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current_premium, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    try:
                        from trade_notifier import notify_trade_exit
                        lot_size = pos.get('lot_size', 1)
                        cap = pos.get('margin_required', entry_prem * lot_size * 3)
                        notify_trade_exit(
                            market="STOCKS", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=pos['entry_premium'],
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                            exit_reason='TRAILING_SL_HIT')
                    except Exception:
                        pass
                    continue

            # ---- OI+IV DYNAMIC EXIT CHECK (runs BEFORE static exit) ----
            current_iv_pct = None
            if current_premium > 0 and spot > 0:
                opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
                T_iv = max(pos.get('dte', 1), 1) / 365
                iv_solved = implied_vol(current_premium, spot, pos['strike'], T_iv, RISK_FREE_RATE, opt_type)
                if iv_solved:
                    current_iv_pct = round(iv_solved * 100, 1)

            oi_iv_reason, should_reverse = self.check_oi_iv_exit(pos, current_premium, current_iv_pct)
            if oi_iv_reason:
                # Override if hold score STRONG + profitable
                if hold_score >= HOLD_SCORE_STRONG and pos.get('unrealized_pnl', 0) > 0:
                    logger.info(f"  SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                                f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} "
                                f"— overriding {oi_iv_reason} (signals strong + profitable)")
                    oi_iv_reason = None  # Cancel OI/IV signal

            if oi_iv_reason:
                # v8.0: OI/IV TIGHTEN SL instead of forcing exit. Only GAMMA_SHIELD forces exit.
                if 'SL_TIGHTEN' in oi_iv_reason:
                    # Tighten trailing SL — combo tightens 10%, single tightens 5%
                    tighten_pct = 0.10 if 'OI_IV_SL_TIGHTEN' == oi_iv_reason else 0.05
                    if pos['is_sell']:
                        # SELL: TSL is ABOVE current. Tighten by pulling closer.
                        new_tsl = round(current_premium * (1 + tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL "
                                        f"Rs {old_tsl}→{new_tsl:.2f} "
                                        f"({tighten_pct*100:.0f}% above current {current_premium:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL already tighter "
                                        f"Rs {pos['trailing_sl']:.2f} (proposed {new_tsl:.2f}). No change.")
                    else:
                        # BUY: TSL is BELOW current. Tighten by raising it.
                        new_tsl = round(current_premium * (1 - tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl > (pos.get('trailing_sl') or 0):
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL "
                                        f"Rs {old_tsl}→{new_tsl:.2f} "
                                        f"({tighten_pct*100:.0f}% below current {current_premium:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL already tighter "
                                        f"Rs {pos.get('trailing_sl'):.2f} (proposed {new_tsl:.2f}). No change.")
                    # v8.0: Don't close, let TSL handle exit naturally
                else:
                    # GAMMA_SHIELD_EXIT — forced exit (real gamma risk)
                    lot_size = pos.get('lot_size', 1)
                    self.portfolio.close_position(pos['id'], current_premium, oi_iv_reason)
                    self._track_exit(pos, oi_iv_reason)
                    try:
                        from trade_notifier import notify_trade_exit
                        if pos.get('is_sell', False):
                            capital = pos.get('margin_required', pos['entry_premium'] * lot_size * 3)
                        else:
                            capital = pos['entry_premium'] * lot_size
                        notify_trade_exit(
                            market="STOCKS", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=pos['entry_premium'],
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pos.get('unrealized_pnl', 0), capital_used=capital,
                            exit_reason=oi_iv_reason)
                    except Exception as e:
                        logger.warning(f"  Telegram exit notify failed: {e}")
                    continue  # Skip static exit check

            # ---- STATIC EXIT CHECK ----
            logger.info(f"  EXIT_CHECK: {pos['id']} | Entry: {pos['entry_premium']:.2f} "
                        f"→ Current: {current_premium:.2f} [{premium_source}] | "
                        f"Target: {target:.2f} SL: {sl:.2f} | "
                        f"TSL: {pos.get('trailing_sl', 'N/A')} | "
                        f"Spot: {entry_spot:.0f}→{spot:.0f} Δ={spot_change:+.0f} | "
                        f"Hold: {hold_score}/{hold_label}")

            exit_reason = None

            if not pos['is_sell']:
                # BUY positions
                if current_premium <= sl:
                    exit_reason = 'SL_HIT'
                elif pos.get('dte', 30) <= 0:
                    exit_reason = 'EXPIRY'
                elif current_premium >= target:
                    # v10.1: Trailing target — extend instead of hard exit
                    if TARGET_TRAIL_ENABLED and pos.get('target_extensions', 0) < TARGET_TRAIL_MAX_EXTENSIONS:
                        gain_pct = (current_premium - pos['entry_premium']) / pos['entry_premium'] * 100 if pos['entry_premium'] > 0 else 0
                        new_target = round(current_premium * (1 + TARGET_TRAIL_EXTEND_PCT / 100), 2)
                        new_tsl = round(pos.get('peak_premium', current_premium) * (1 - TARGET_TRAIL_TSL_DISTANCE_PCT / 100), 2)
                        new_tsl = max(new_tsl, pos.get('trailing_sl') or 0)
                        pos['target_extensions'] = pos.get('target_extensions', 0) + 1
                        pos['target'] = new_target
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TARGET_TRAIL: {pos['id']} BUY ext#{pos['target_extensions']} "
                                   f"gain={gain_pct:.0f}% -> target Rs {new_target:.2f}, TSL Rs {new_tsl:.2f}")
                    else:
                        exit_reason = 'TARGET_HIT'
            else:
                # SELL positions
                if current_premium >= sl:
                    exit_reason = 'SL_HIT'
                elif pos.get('dte', 30) <= 0:
                    exit_reason = 'EXPIRY'
                elif current_premium <= target:
                    # v10.1: Trailing target for SELL
                    if TARGET_TRAIL_ENABLED and pos.get('target_extensions', 0) < TARGET_TRAIL_MAX_EXTENSIONS:
                        gain_pct = (pos['entry_premium'] - current_premium) / pos['entry_premium'] * 100 if pos['entry_premium'] > 0 else 0
                        new_target = round(current_premium * (1 - TARGET_TRAIL_EXTEND_PCT / 100), 2)
                        new_tsl = round(pos.get('trough_premium', current_premium) * (1 + TARGET_TRAIL_TSL_DISTANCE_PCT / 100), 2)
                        new_tsl = min(new_tsl, pos.get('trailing_sl') or float('inf'))
                        pos['target_extensions'] = pos.get('target_extensions', 0) + 1
                        pos['target'] = new_target
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TARGET_TRAIL: {pos['id']} SELL ext#{pos['target_extensions']} "
                                   f"gain={gain_pct:.0f}% -> target Rs {new_target:.2f}, TSL Rs {new_tsl:.2f}")
                    else:
                        exit_reason = 'TARGET_HIT'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current_premium, exit_reason)
                self._track_exit(pos, exit_reason)
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = pos.get('lot_size', 1)
                    if pos.get('is_sell', False):
                        capital = pos.get('margin_required', pos['entry_premium'] * lot_size * 3)
                    else:
                        capital = pos['entry_premium'] * lot_size
                    invested = self.portfolio.get_invested_capital()
                    notify_trade_exit(
                        market="STOCKS", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pos.get('unrealized_pnl', 0), capital_used=capital,
                        exit_reason=exit_reason,
                        capital_available=self.portfolio.get_available_capital(),
                        total_invested=invested)
                except Exception as e:
                    logger.warning(f"  Telegram exit notify failed: {e}")

        # Persist updated PnL/Greeks to disk after exit check cycle
        self.portfolio.save_state()

    # ================================================================
    # SCAN ALL STRATEGIES — Generate signals from today's watchlist
    # ================================================================
    def scan_all_strategies(self):
        """Scan today's watchlist stocks across all 3 strategies.

        For each stock in today's narrow CPR watchlist:
        1. Get spot price
        2. Compute indicators
        3. Check CPR breakout signals
        4. Check Gamma Blast signals
        5. Check PCR+VWAP signals
        6. Apply ORB overlay to boost/reduce quality score
        7. Score and filter signals

        Returns:
            list of signal dicts sorted by quality score.
        """
        watchlist = self._todays_watchlist
        if not watchlist:
            logger.info("  No stocks in today's watchlist. Run morning_scan() first.")
            return []

        all_signals = []
        scan_data = {}  # v10: Collect indicator data for dashboard live display
        spots_ok = 0
        spots_fail = 0

        for stock_info in watchlist:
            symbol = stock_info['symbol']
            lot_size = stock_info.get('lot_size', 1)
            strike_interval = stock_info.get('strike_interval', 25)

            # Get current spot
            spot = self.get_stock_spot(symbol)
            if not spot:
                spots_fail += 1
                logger.debug(f"  {symbol}: No spot price available")
                continue
            spots_ok += 1

            # v10.2e: Log spot tick for backtesting
            if self.data_logger and spot:
                self.data_logger.log_spot_tick(symbol, spot)

            # Compute indicators
            indicators = stock_info.get('indicators')
            if not indicators:
                indicators = self.engine.compute_indicators(symbol)
            if not indicators:
                logger.info(f"  {symbol}: No indicators available (skipping)")
                continue

            # Compute DTE for monthly expiry
            dte = stock_info.get('dte', 20)  # default ~20 days for monthly
            try:
                from strategies.utils import get_dte_monthly
                dte = max(1, get_dte_monthly())
            except Exception:
                pass

            # v10: Collect live scan data for dashboard
            scan_data[symbol] = {
                'spot': spot,
                'atr': indicators.get('atr', 0),
                'iv': indicators.get('iv', 0),
                'hv': indicators.get('hv', 0),
                'vwap': indicators.get('vwap', 0),
                'pcr': indicators.get('pcr', 0),
                'pivot': indicators.get('pivot', 0),
                'tc': indicators.get('tc', 0),
                'bc': indicators.get('bc', 0),
                'cpr_width': indicators.get('cpr_width', 0),
                'cam_r3': indicators.get('cam_r3', 0),
                'cam_r4': indicators.get('cam_r4', 0),
                'cam_s3': indicators.get('cam_s3', 0),
                'cam_s4': indicators.get('cam_s4', 0),
                'resistance': indicators.get('resistance', 0),
                'support': indicators.get('support', 0),
            }

            # Strategy config (stock-specific)
            config = {
                'lot_size': lot_size,
                'strike_interval': strike_interval,
                'min_premium_buy': MIN_PREMIUM_BUY,
                'min_premium_sell': MIN_PREMIUM_SELL,
                'symbol': symbol,
                'dte': dte,
                'allow_sell': False,  # Stock options: BUY only (no margin for SELL)
            }

            # Build OHLC dict for today (uses intraday data if available, else spot)
            intra = self.engine.intraday_data.get(symbol)
            if intra is not None and len(intra) > 0:
                ohlc = {
                    'open': intra.iloc[0].get('open', spot),
                    'high': intra['high'].max() if 'high' in intra.columns else spot,
                    'low': intra['low'].min() if 'low' in intra.columns else spot,
                    'close': spot,
                    'volume': intra['volume'].sum() if 'volume' in intra.columns else 0,
                }
            else:
                ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}

            # Check CPR breakout
            cpr_signals = self.engine.check_cpr_breakout_signals(symbol, spot, ohlc, indicators, config)
            all_signals.extend(cpr_signals)

            # Check Gamma Blast
            gamma_signals = self.engine.check_gamma_blast_signals(symbol, spot, ohlc, indicators, config)
            all_signals.extend(gamma_signals)

            # Check PCR+VWAP
            pcr_signals = self.engine.check_pcr_vwap_signals(symbol, spot, indicators, config)
            all_signals.extend(pcr_signals)

        # v9.6: Diagnostic logging — spot/indicator summary
        if spots_fail > 0:
            logger.info(f"  [SPOT] OK={spots_ok} FAIL={spots_fail} Raw_signals={len(all_signals)}")
        elif datetime.now().minute % 5 == 0:
            logger.info(f"  [SPOT] OK={spots_ok} Raw_signals={len(all_signals)}")

        # v9.6: Build indicators lookup from watchlist (signals don't carry indicators)
        ind_lookup = {si['symbol']: si.get('indicators', {}) for si in watchlist}

        # Score all signals
        scored_signals = []
        rejected_scores = []
        for sig in all_signals:
            symbol = sig.get('symbol', '')
            spot = self.get_stock_spot(symbol) or 0
            indicators = ind_lookup.get(symbol, {})
            score = self.engine.score_signal(sig, spot, indicators, self.current_vix)

            # Apply ORB overlay
            direction = 'CE' if 'CE' in sig.get('type', '') else 'PE'
            orb_adj = self.engine.check_orb_confirmation(symbol, spot, direction)
            score += orb_adj

            sig['quality_score'] = max(0, min(100, score))
            if sig['quality_score'] >= MIN_SIGNAL_SCORE:
                scored_signals.append(sig)
            else:
                rejected_scores.append(f"{symbol}:{sig.get('type','')}={sig['quality_score']}")

        # v9.6: Log rejected signals for diagnosis
        if rejected_scores and not scored_signals:
            logger.info(f"  [SCORE] All {len(rejected_scores)} signals rejected (below {MIN_SIGNAL_SCORE}): "
                       f"{', '.join(rejected_scores[:5])}")

        # Sort by score descending
        scored_signals.sort(key=lambda s: s.get('quality_score', 0), reverse=True)

        if scored_signals:
            logger.info(f"\n  SIGNALS FOUND: {len(scored_signals)}")
            for sig in scored_signals[:5]:
                logger.info(f"    {sig.get('symbol', ''):10s} {sig.get('type', ''):20s} "
                            f"Score={sig.get('quality_score', 0)} Strategy={sig.get('strategy', '')}")

        # v10: Write live scan data for dashboard
        self._write_live_scan_data(scan_data)

        return scored_signals

    def _write_live_scan_data(self, scan_data):
        """v10: Write live indicator data for dashboard consumption."""
        try:
            live_file = os.path.join(STOCK_PAPER_DIR, 'live_scan_data.json')
            payload = {
                'last_updated': datetime.now().isoformat(),
                'vix': self.current_vix,
                'market': 'stocks',
                'symbols': scan_data,
            }
            with open(live_file, 'w') as f:
                json.dump(payload, f, indent=2, default=str)
            logger.debug(f"Live scan data written: {len(scan_data)} stocks")
        except Exception as e:
            logger.error(f"Error writing live scan data: {e}")

    # ================================================================
    # EXECUTE PAPER SIGNALS — Open positions from top signals
    # ================================================================
    def execute_paper_signals(self, signals):
        """Execute the best signals as paper trades.

        Applies all risk filters before opening positions:
        - Quality score >= MIN_SIGNAL_SCORE
        - Capital availability
        - Position limits
        - Direction flip check (higher score bar)
        - Premium filters (min/max)
        - Profit/cost ratio check

        Args:
            signals: list of signal dicts from scan_all_strategies().
        """
        if not signals:
            return

        executed = 0
        skipped = 0

        for sig in signals:
            # Position limits
            if len(self.portfolio.positions) >= MAX_TOTAL_POSITIONS:
                logger.info(f"  MAX_POSITIONS: Already at {MAX_TOTAL_POSITIONS}, skipping remaining")
                break

            if self.portfolio.count_today_trades() >= MAX_TRADES_PER_DAY:
                logger.info(f"  MAX_TRADES: Hit daily limit of {MAX_TRADES_PER_DAY}")
                break

            symbol = sig.get('symbol', '')
            signal_type = sig.get('type', '')
            strategy = sig.get('strategy', '')
            score = sig.get('quality_score', 0)
            lot_size = sig.get('lot_size', 1)

            # Stock-level position limit
            if self.portfolio.count_positions_for_stock(symbol) >= MAX_POSITIONS_PER_STOCK:
                logger.info(f"  SKIP: {symbol} already at max {MAX_POSITIONS_PER_STOCK} positions")
                skipped += 1
                continue

            # Direction check: existing positions for this stock
            existing_directions = set()
            for p in self.portfolio.positions:
                if p.get('stock_symbol') == symbol or p.get('symbol') == symbol:
                    existing_directions.add('CE' if 'CE' in p['signal_type'] else 'PE')
            new_direction = 'CE' if 'CE' in signal_type else 'PE'

            # v9.6: Same-direction duplicate check — skip if already holding same direction
            if new_direction in existing_directions:
                logger.info(f"  SKIP_SAME_DIR: {symbol} already has {new_direction} position open")
                skipped += 1
                continue

            # Direction flip check: opposite direction needs higher score
            if existing_directions and new_direction not in existing_directions:
                if score < DIRECTION_FLIP_MIN_SCORE:
                    logger.info(f"  SKIP: {symbol} direction flip needs score >= {DIRECTION_FLIP_MIN_SCORE} "
                                f"(got {score})")
                    skipped += 1
                    continue

            # ---- v10.1: Pending reverse check (breakout failure → opposite direction fast entry) ----
            is_reverse_signal = False
            pending_rev = self.pending_reverses.get(symbol)
            if pending_rev and pending_rev.get('opt_type') == new_direction:
                try:
                    rev_ts = datetime.fromisoformat(pending_rev['timestamp'])
                    rev_age = (datetime.now() - rev_ts).total_seconds()
                    if rev_age <= 600:  # 10 min
                        is_reverse_signal = True
                        logger.info(f"  REVERSE_MATCH: {symbol} BUY_{new_direction} matches pending reverse "
                                   f"from {pending_rev.get('source_id', '?')} ({int(rev_age)}s ago) — "
                                   f"skipping cooldown, score +15")
                        del self.pending_reverses[symbol]
                    else:
                        logger.info(f"  REVERSE_EXPIRED: {symbol} reverse was {int(rev_age)}s ago (>600s), ignoring")
                        del self.pending_reverses[symbol]
                except Exception as e:
                    logger.warning(f"  REVERSE_CHECK_ERR: {symbol} — {e}")

            # Re-entry cooldown check (10 min after exit)
            # v10.1: Reverse signals bypass cooldown entirely
            now = datetime.now()
            in_cooldown = False
            if not is_reverse_signal:
                for eh in self.exit_history:
                    if eh['symbol'] == symbol and eh['direction'] == new_direction:
                        cooldown_remaining = REENTRY_COOLDOWN_SECONDS - (now - eh['time']).total_seconds()
                        if cooldown_remaining > 0:
                            logger.info(f"  COOLDOWN: {symbol} {new_direction} — {cooldown_remaining:.0f}s remaining")
                            in_cooldown = True
                            break
            else:
                logger.info(f"  REVERSE_COOLDOWN_BYPASS: {symbol} {new_direction} — reverse signal, skipping cooldown")
            if in_cooldown:
                skipped += 1
                continue

            # ---- v10.1: DIRECTION VALIDATION — the primary filter ----
            dir_spot = self.get_stock_spot(symbol) or sig.get('spot', 0)
            if dir_spot:
                intra = self.engine.intraday_data.get(symbol)
                if intra is not None and len(intra) > 0:
                    dir_ohlc = {
                        'open': intra.iloc[0].get('open', dir_spot),
                        'high': intra['high'].max() if 'high' in intra.columns else dir_spot,
                        'low': intra['low'].min() if 'low' in intra.columns else dir_spot,
                        'close': dir_spot, 'volume': 0,
                    }
                else:
                    dir_ohlc = {'open': dir_spot, 'high': dir_spot, 'low': dir_spot,
                                'close': dir_spot, 'volume': 0}
                dir_indicators = self.engine.compute_indicators(symbol, dir_ohlc)
                if dir_indicators:
                    dir_valid, dir_reason, dir_adj = validate_signal_direction(
                        signal_type, dir_spot, dir_ohlc, dir_indicators)
                    if not dir_valid:
                        # v10.2b: Direction Flip — trade correct direction instead of just blocking
                        # Guard: Don't flip AGAINST the daily EMA trend
                        # If EMA bullish (9>20), only flip to CE, never to PE
                        # If EMA bearish (9<20), only flip to PE, never to CE
                        orig_type = signal_type
                        ema_9 = dir_indicators.get('ema_9', 0)
                        ema_20 = dir_indicators.get('ema_20', 0)

                        flipped_type = orig_type.replace('CE', 'PE') if 'CE' in orig_type else orig_type.replace('PE', 'CE')
                        flip_to_pe = 'PE' in flipped_type

                        if ema_9 > 0 and ema_20 > 0:
                            daily_bullish = ema_9 > ema_20
                            if (daily_bullish and flip_to_pe) or (not daily_bullish and not flip_to_pe):
                                logger.info(f"  DIR_REJECT: {symbol} {orig_type} -- {dir_reason} "
                                           f"(no flip to {'PE' if flip_to_pe else 'CE'}: "
                                           f"daily EMA {'bullish' if daily_bullish else 'bearish'})")
                                skipped += 1; continue
                        flip_valid, flip_reason, flip_adj = validate_signal_direction(
                            flipped_type, dir_spot, dir_ohlc, dir_indicators)
                        if flip_valid:
                            flipped_opt = 'CE' if 'CE' in flipped_type else 'PE'
                            existing_flipped = [p for p in self.portfolio.positions
                                if p['symbol'] == symbol
                                and ('CE' if 'CE' in p.get('signal_type', '') else 'PE') == flipped_opt]
                            if existing_flipped:
                                logger.info(f"  DIR_REJECT: {symbol} {orig_type} -- {dir_reason} "
                                           f"(flip to {flipped_type} blocked: already holding {flipped_opt})")
                                skipped += 1; continue
                            logger.info(f"  DIR_FLIP: {symbol} {orig_type} -> {flipped_type} "
                                       f"-- {dir_reason} | flipped: {flip_reason}")
                            signal_type = flipped_type
                            sig['type'] = flipped_type
                            dir_adj = flip_adj
                        else:
                            logger.info(f"  DIR_REJECT: {symbol} {orig_type} -- {dir_reason} "
                                       f"(flip {flipped_type}: {flip_reason})")
                            skipped += 1; continue
                    else:
                        logger.info(f"  DIR_PASS: {symbol} {signal_type} -- {dir_reason}")
                    score = max(0, min(100, score + dir_adj))
                    # v10.1: Reverse signal score boost (+15)
                    if is_reverse_signal:
                        score = min(100, score + 15)
                        logger.info(f"  REVERSE_BOOST: {symbol} score {score-15} → {score}")
                    sig['quality_score'] = score

            # Get spot and determine strike
            spot = self.get_stock_spot(symbol)
            if not spot:
                skipped += 1
                continue

            stock_info = self.fno_config.stocks.get(symbol, {})
            strike_interval = stock_info.get('strike_interval', 25)
            opt_type = 'CE' if 'CE' in signal_type else 'PE'

            # Select optimal strike from live option chain
            expiry = self._get_nearest_expiry(symbol)
            expiry_ddmon = expiry  # Already in ddMONyyyy format

            strike_data = None
            if expiry_ddmon:
                strike_data = self.angel.select_optimal_strike(symbol, spot, opt_type, expiry_ddmon)

            if strike_data and strike_data.get('ltp', 0) > 0:
                entry_premium = strike_data['ltp']
                strike = strike_data['strike']
                entry_oi = strike_data.get('oi', 0)
                entry_iv = strike_data.get('iv', 0)
                option_token = strike_data.get('token', '')
            else:
                # v10.2f: SKIP_NO_LTP — Do NOT trade with Black-Scholes phantom pricing.
                # Same safeguard as equity bot (paper_trader.py line ~3402).
                # Without real chain LTP, entry/exit premiums are unreliable.
                logger.info(f"  SKIP_NO_LTP: {symbol} {opt_type} — no real chain data "
                            f"(expiry={'found' if expiry_ddmon else 'MISSING'}, "
                            f"strike_data={'partial' if strike_data else 'None'}). "
                            f"Black-Scholes fallback DISABLED for stock trades.")
                skipped += 1
                continue

            # Premium filter
            is_sell = 'SELL' in signal_type
            min_prem = MIN_PREMIUM_SELL if is_sell else MIN_PREMIUM_BUY
            if entry_premium < min_prem:
                logger.info(f"  SKIP: {symbol} premium Rs {entry_premium:.2f} < min Rs {min_prem}")
                skipped += 1
                continue

            # Capital check
            if is_sell:
                capital_needed = entry_premium * lot_size * 3
            else:
                capital_needed = entry_premium * lot_size

            if capital_needed > self.portfolio.get_available_capital():
                logger.info(f"  SKIP: {symbol} needs Rs {capital_needed:,.0f}, "
                            f"only Rs {self.portfolio.get_available_capital():,.0f} available")
                skipped += 1
                continue

            if capital_needed > MAX_PER_TRADE:
                logger.info(f"  SKIP: {symbol} needs Rs {capital_needed:,.0f} > max Rs {MAX_PER_TRADE:,.0f}")
                skipped += 1
                continue

            # Compute Greeks for the selected strike
            dte = get_dte_monthly()
            T = max(dte, 1) / 365
            iv_for_greeks = entry_iv if entry_iv > 0 else 0.25
            if iv_for_greeks < 1:
                pass  # already in decimal
            else:
                iv_for_greeks = iv_for_greeks / 100  # convert from pct

            try:
                g = bs_greeks(spot, strike, T, RISK_FREE_RATE, iv_for_greeks, opt_type)
            except Exception:
                g = {'delta': 0.5, 'gamma': 0, 'theta': 0, 'price': entry_premium}

            # Set target and SL from signal
            target_mult = sig.get('target_mult', 1.5)
            sl_mult = sig.get('sl_mult', 0.5)

            if is_sell:
                target = round(entry_premium * (1 - (target_mult - 1)), 2)
                sl_price = round(entry_premium * (1 + (1 - sl_mult)), 2)
            else:
                target = round(entry_premium * target_mult, 2)
                sl_price = round(entry_premium * sl_mult, 2)

            # Open position
            position = self.portfolio.add_signal(
                strategy=strategy,
                stock_symbol=symbol,
                signal_type=signal_type,
                strike=strike,
                entry_premium=entry_premium,
                lot_size=lot_size,
                delta=g.get('delta', 0.5),
                gamma=g.get('gamma', 0),
                theta=g.get('theta', 0),
                iv=iv_for_greeks,
                dte=dte,
                details={
                    'target': target,
                    'sl': sl_price,
                    'option_token': option_token,
                    'option_exchange': 'NFO',
                    'spot': spot,
                    'quality_score': score,
                    'target_mult': target_mult,
                    'sl_mult': sl_mult,
                },
                oi=entry_oi,
                spot_price=spot,
                is_sell=is_sell,
            )

            if position:
                executed += 1
                # Telegram notification
                try:
                    from trade_notifier import notify_trade_entry
                    notify_trade_entry(
                        market="STOCKS", strategy=strategy,
                        symbol=symbol, signal_type=signal_type,
                        strike=strike, entry_price=entry_premium,
                        spot=spot, lot_size=lot_size, multiplier=1,
                        delta=g.get('delta', 0),
                        target=target, sl=sl_price,
                        capital_used=capital_needed,
                        reason=f"Stock CPR Bot | {symbol} | Score={score}",
                        capital_available=self.portfolio.get_available_capital(),
                        total_invested=self.portfolio.get_invested_capital(),
                    )
                except Exception as e:
                    logger.warning(f"  Telegram entry notify failed: {e}")
            else:
                skipped += 1

        if executed or skipped:
            logger.info(f"  Executed: {executed} | Skipped: {skipped} | "
                        f"Positions: {len(self.portfolio.positions)}/{MAX_TOTAL_POSITIONS}")

    # ================================================================
    # MORNING SCAN — Run CPR scanner pre-market
    # ================================================================
    def morning_scan(self):
        """Run pre-market CPR scanner to identify today's watchlist.

        Called at 08:30 AM. Scans all F&O stocks, identifies narrow CPR
        candidates, scores them, and selects top stocks for trading.

        Returns:
            list: Today's watchlist stocks.
        """
        logger.info("=" * 60)
        logger.info("MORNING SCAN: Scanning F&O stocks for narrow CPR...")
        logger.info(f"Time: {datetime.now().strftime('%H:%M:%S')}")
        logger.info("=" * 60)

        watchlist = self.scanner.scan_all_stocks(max_stocks=5, cpr_threshold=0.5)
        self._todays_watchlist = watchlist

        # Save scan results to file
        scan_file = os.path.join(STOCK_PAPER_DIR, f'cpr_scan_{datetime.now().strftime("%Y-%m-%d")}.json')
        scan_data = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'scan_time': datetime.now().strftime('%H:%M:%S'),
            'total_scanned': len(self.fno_config.get_all_fno_symbols()),
            'narrow_cpr_count': len(watchlist),
            'stocks': [],
        }
        for stock in watchlist:
            entry = {
                'symbol': stock['symbol'],
                'cpr_width': round(stock['cpr_width'], 4),
                'tier': stock['tier'],
                'score': round(stock['score'], 1),
                'lot_size': stock['lot_size'],
                'strike_interval': stock['strike_interval'],
                'sector': stock['sector'],
                'liquidity': stock['liquidity'],
            }
            if stock.get('indicators'):
                ind = stock['indicators']
                entry.update({
                    'pivot': round(ind.get('pivot', 0), 2),
                    'tc': round(ind.get('tc', 0), 2),
                    'bc': round(ind.get('bc', 0), 2),
                    'atr': round(ind.get('atr', 0), 2),
                    'iv': round(ind.get('iv', 0), 4),
                    'prev_close': round(ind.get('prev_close', 0), 2),
                })
            scan_data['stocks'].append(entry)

        try:
            with open(scan_file, 'w') as f:
                json.dump(scan_data, f, indent=2)
            logger.info(f"Scan results saved to {scan_file}")
        except Exception as e:
            logger.error(f"Error saving scan results: {e}")

        # v9.3: Send Telegram notification — ONCE per day only
        # Uses a flag file to prevent duplicate notifications on restarts
        notify_flag = os.path.join(STOCK_PAPER_DIR, f'.scan_notified_{datetime.now().strftime("%Y%m%d")}')
        if watchlist and not os.path.exists(notify_flag):
            try:
                from trade_notifier import send_info
                stock_list = '\n'.join(
                    f"  {s['symbol']:10s} CPR={s['cpr_width']:.3f}% Score={s['score']:.0f} "
                    f"[T{s['tier']}] [{s['sector']}]"
                    for s in watchlist
                )
                msg = (f"<b>📊 STOCK CPR SCANNER</b>\n"
                       f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
                       f"Narrow CPR stocks: {len(watchlist)}\n\n"
                       f"<pre>{stock_list}</pre>\n\n"
                       f"Capital: Rs {self.portfolio.capital:,.0f}\n"
                       f"Available: Rs {self.portfolio.get_available_capital():,.0f}")
                send_info(msg)
                # Write flag file to prevent duplicate Telegram on restart
                with open(notify_flag, 'w') as f:
                    f.write(datetime.now().isoformat())
                logger.info("Morning scan Telegram notification sent.")
            except Exception:
                pass
        elif os.path.exists(notify_flag):
            logger.info("Morning scan Telegram already sent today — skipping.")

        return watchlist

    # ================================================================
    # RUN ONCE — Single scan + exit cycle
    # ================================================================
    def run_once(self):
        """Run a single scan cycle: check exits + scan strategies + execute.

        This is the core loop body called every 30 seconds during market hours.
        """
        # Check exits first (before scanning for new signals)
        self.check_exits()

        # Only scan for new signals during trading hours
        now = datetime.now().time()
        if now < FIRST_TRADE_TIME:
            logger.info(f"  Waiting for first trade time ({FIRST_TRADE_TIME})")
            return
        if now > LAST_ENTRY_TIME:
            logger.info(f"  Past last entry time ({LAST_ENTRY_TIME}), exits only")
            return

        # v9.6: Diagnostic logging — positions + watchlist size
        pos_count = len(self.portfolio.positions)
        wl_count = len(self._todays_watchlist)
        if pos_count > 0 or now.minute % 5 == 0:
            logger.info(f"  [SCAN] Watchlist={wl_count} Positions={pos_count} "
                       f"Trades={self.daily_trade_count}")

        # Scan for new signals
        signals = self.scan_all_strategies()

        # Execute top signals
        if signals:
            self.execute_paper_signals(signals)

        # v10.2f: Save ALL signals to CSV for backtesting & debugging
        self.save_signals_log(signals)

    # ================================================================
    # SAVE SIGNALS LOG — Record all signals for backtesting
    # ================================================================
    def save_signals_log(self, signals):
        """Save ALL scored signals to CSV for future backtests & debugging.
        v10.2f: Added to stock bot — mirrors equity bot signal logging.
        Columns: timestamp, strategy, symbol, type, strike, premium, spot,
                 dte, delta, gamma, theta, vega, iv, oi, quality_score,
                 target, sl, reason, executed, data_source.
        """
        if not signals:
            return
        try:
            today = datetime.now().strftime('%Y%m%d')
            log_file = os.path.join(STOCK_PAPER_DIR, f'stock_signals_{today}.csv')

            # Build set of executed signal keys for status tracking
            executed_ids = set()
            for p in self.portfolio.positions:
                key = f"{p.get('strategy', '')}_{p.get('symbol', p.get('stock_symbol', ''))}_{p.get('strike', 0)}"
                executed_ids.add(key)

            rows = []
            for sig in signals:
                greeks = sig.get('greeks', {})
                symbol = sig.get('symbol', '')
                sig_key = f"{sig.get('strategy', '')}_{symbol}_{sig.get('strike', 0)}"

                # Determine data source (real chain vs Black-Scholes)
                oi_val = sig.get('oi', 0)
                data_source = 'REAL_CHAIN' if oi_val and oi_val > 0 else 'BLACK_SCHOLES'

                rows.append({
                    'timestamp': datetime.now().isoformat(),
                    'strategy': sig.get('strategy', ''),
                    'symbol': symbol,
                    'type': sig.get('type', ''),
                    'strike': sig.get('strike', 0),
                    'premium': sig.get('premium', 0),
                    'spot': sig.get('spot', 0),
                    'dte': sig.get('dte', 0),
                    # Greeks
                    'delta': greeks.get('delta', 0),
                    'gamma': greeks.get('gamma', 0),
                    'theta': greeks.get('theta', 0),
                    'vega': greeks.get('vega', 0),
                    'iv': greeks.get('iv', sig.get('iv', 0)),
                    # Market data
                    'oi': oi_val,
                    'quality_score': sig.get('quality_score', 0),
                    # Trade params
                    'target': sig.get('target', 0),
                    'sl': sig.get('sl', 0),
                    'reason': sig.get('reason', ''),
                    # Execution status & data source
                    'executed': sig_key in executed_ids,
                    'data_source': data_source,
                })

            df = pd.DataFrame(rows)
            if os.path.exists(log_file):
                existing = pd.read_csv(log_file)
                df = pd.concat([existing, df], ignore_index=True)
            df.to_csv(log_file, index=False)
            logger.info(f"  Stock signals logged ({len(rows)} rows): {log_file}")
        except Exception as e:
            logger.error(f"  Signal logging error: {e}")

    # ================================================================
    # RUN CONTINUOUS — Main trading loop
    # ================================================================
    def run_continuous(self, interval_seconds=30):
        """Run continuous paper trading loop.

        Args:
            interval_seconds: Seconds between scan cycles (default 30).
        """
        if not self.initialize():
            logger.error("Initialization failed. Exiting.")
            return

        self._running = True
        logger.info(f"Starting continuous trading loop (interval={interval_seconds}s)")

        # Run morning scan if before market open
        now = datetime.now().time()
        if now < MARKET_OPEN:
            logger.info("Pre-market: Running morning CPR scan...")
            self.morning_scan()
        elif not self._todays_watchlist:
            # Market already open but no watchlist — run scan now
            self.morning_scan()

        while self._running:
            now = datetime.now().time()

            if now < MARKET_OPEN:
                # Pre-market: wait
                logger.info(f"Pre-market. Waiting for {MARKET_OPEN}...")
                time.sleep(60)
                continue

            if now > MARKET_CLOSE:
                # After market: EOD summary and stop
                logger.info("Market closed. Running EOD summary...")
                self.eod_summary()
                break

            # Market hours: run scan cycle
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in scan cycle: {e}", exc_info=True)

            time.sleep(interval_seconds)

        logger.info("Trading loop ended.")

    # ================================================================
    # EOD SUMMARY — End-of-day report
    # ================================================================
    def eod_summary(self):
        """Generate and send end-of-day summary.

        Includes:
        - Today's trades (entries + exits)
        - P&L breakdown by strategy
        - Win rate
        - Capital usage
        """
        today = datetime.now().strftime('%Y-%m-%d')
        summary = self.portfolio.get_portfolio_summary()

        # Today's closed trades
        today_trades = [t for t in self.portfolio.closed_trades
                        if t.get('exit_time', '').startswith(today)]

        # Strategy breakdown
        strat_pnl = defaultdict(lambda: {'pnl': 0, 'trades': 0, 'wins': 0})
        for t in today_trades:
            strat = t.get('strategy', 'Unknown')
            strat_pnl[strat]['pnl'] += t.get('pnl', 0)
            strat_pnl[strat]['trades'] += 1
            if t.get('pnl', 0) > 0:
                strat_pnl[strat]['wins'] += 1

        logger.info("=" * 60)
        logger.info("END-OF-DAY SUMMARY — STOCK OPTIONS")
        logger.info("=" * 60)
        logger.info(f"Date: {today}")
        logger.info(f"Trades today: {len(today_trades)}")
        logger.info(f"Daily P&L: Rs {summary['daily_pnl']:+,.0f}")
        logger.info(f"Win rate: {summary['win_rate']:.1f}%")
        logger.info(f"Capital: Rs {summary['capital']:,.0f}")
        logger.info(f"Invested: Rs {summary['invested']:,.0f}")
        logger.info(f"Available: Rs {summary['available']:,.0f}")

        for strat, data in strat_pnl.items():
            wr = (data['wins'] / data['trades'] * 100) if data['trades'] > 0 else 0
            logger.info(f"  {strat}: {data['trades']} trades, "
                        f"P&L Rs {data['pnl']:+,.0f}, WR {wr:.0f}%")

        # Send Telegram EOD report
        try:
            from trade_notifier import send_info
            strat_lines = '\n'.join(
                f"  {strat}: {d['trades']} trades, Rs {d['pnl']:+,.0f}"
                for strat, d in strat_pnl.items()
            )
            msg = (f"<b>📊 STOCK EOD REPORT</b>\n"
                   f"Date: {today}\n"
                   f"Trades: {len(today_trades)}\n"
                   f"Daily P&L: Rs {summary['daily_pnl']:+,.0f}\n"
                   f"Win Rate: {summary['win_rate']:.1f}%\n\n"
                   f"<b>Strategy Breakdown:</b>\n"
                   f"<pre>{strat_lines}</pre>\n\n"
                   f"Capital: Rs {summary['capital']:,.0f}\n"
                   f"Available: Rs {summary['available']:,.0f}\n"
                   f"Total P&L: Rs {summary['total_pnl']:+,.0f}")
            send_info(msg)
        except Exception:
            pass

    # ================================================================
    # IS MARKET OPEN — Check NSE hours
    # ================================================================
    def is_market_open(self):
        """Check if NSE equity market is currently open.

        Returns:
            bool: True if within trading hours on a trading day.
        """
        now = datetime.now()

        # Weekend check
        if now.weekday() >= 5:
            return False

        # Holiday check
        try:
            from market_holidays import is_market_holiday
            if is_market_holiday(now.date()):
                return False
        except ImportError:
            pass

        # Time check
        return MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def get_status(self):
        """Print current portfolio status to logger.

        Returns:
            dict: Portfolio summary.
        """
        summary = self.portfolio.get_portfolio_summary()
        logger.info("=" * 60)
        logger.info("STOCK OPTIONS PORTFOLIO STATUS")
        logger.info("=" * 60)
        logger.info(f"Capital: Rs {summary['capital']:,.0f}")
        logger.info(f"Invested: Rs {summary['invested']:,.0f}")
        logger.info(f"Available: Rs {summary['available']:,.0f}")
        logger.info(f"Open Positions: {summary['open_positions']}")
        logger.info(f"Open P&L: Rs {summary['open_pnl']:+,.0f}")
        logger.info(f"Total Closed: {summary['total_closed']}")
        logger.info(f"Win Rate: {summary['win_rate']:.1f}%")
        logger.info(f"Total P&L: Rs {summary['total_pnl']:+,.0f}")

        if self.portfolio.positions:
            logger.info("\nOpen Positions:")
            for pos in self.portfolio.positions:
                logger.info(f"  {pos['id']} | {pos['strategy']} | "
                            f"{pos.get('stock_symbol', pos.get('symbol', ''))} "
                            f"{pos['strike']} {pos['signal_type']} | "
                            f"Entry={pos['entry_premium']:.2f} → "
                            f"Current={pos['current_premium']:.2f} | "
                            f"P&L={pos.get('unrealized_pnl', 0):+.0f} | "
                            f"TSL={pos.get('trailing_sl', 'N/A')}")

        return summary


# ====================================================================
# MAIN — CLI entry point
# ====================================================================
def main():
    """CLI entry point for stock paper trader.

    Usage:
        python stock_paper_trader.py              # Run continuous
        python stock_paper_trader.py --once       # Single scan cycle
        python stock_paper_trader.py --status     # Show portfolio status
        python stock_paper_trader.py --reset      # Reset portfolio
        python stock_paper_trader.py --scanner    # Run CPR scanner only
    """
    import argparse
    parser = argparse.ArgumentParser(description='Stock Options Paper Trader')
    parser.add_argument('--once', action='store_true', help='Run single scan cycle')
    parser.add_argument('--status', action='store_true', help='Show portfolio status')
    parser.add_argument('--reset', action='store_true', help='Reset portfolio')
    parser.add_argument('--scanner', action='store_true', help='Run CPR scanner only')
    parser.add_argument('--interval', type=float, default=30, help='Scan interval in seconds')
    args = parser.parse_args()

    trader = StockPaperTrader()

    if args.status:
        trader.portfolio._load_state()
        trader.get_status()
        return

    if args.reset:
        if os.path.exists(StockPortfolio.STATE_FILE):
            os.remove(StockPortfolio.STATE_FILE)
            logger.info("Portfolio state reset.")
        else:
            logger.info("No state file to reset.")
        return

    if not trader.initialize():
        logger.error("Failed to initialize. Exiting.")
        sys.exit(1)

    if args.scanner:
        trader.morning_scan()
        return

    if args.once:
        if not trader._todays_watchlist:
            trader.morning_scan()
        trader.run_once()
        return

    # Default: run continuous
    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received. Stopping...")
        trader._running = False

    signal_module.signal(signal_module.SIGINT, signal_handler)
    signal_module.signal(signal_module.SIGTERM, signal_handler)

    trader.run_continuous(interval_seconds=args.interval)


if __name__ == '__main__':
    main()
