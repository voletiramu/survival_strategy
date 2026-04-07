"""
Dhan Feed — Market data source for option chain, spot LTP, quotes.
v1.1: Fixed response parsing for Dhan API v2 format (nested data.data).

Dhan API advantages over Zerodha:
  - Native IV + Greeks (delta, gamma, theta, vega) in option chain response
  - Bid/Ask prices included natively
  - OI + previous OI for OI change tracking
  - 25,000 instruments via websocket (vs 3,000 Zerodha)
  - No local BS computation needed

Requirements:
    pip install dhanhq

Setup:
    Store credentials in D:/AlgoTrading/config/dhan_creds.json:
    {"client_id": "YOUR_CLIENT_ID", "access_token": "YOUR_ACCESS_TOKEN"}
"""
import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta, date
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Default paths ───────────────────────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get('DHAN_CONFIG_DIR', 'D:/AlgoTrading/config'))
CREDS_FILE = CONFIG_DIR / 'dhan_creds.json'

# VPS paths (auto-detect)
if not CONFIG_DIR.exists():
    CONFIG_DIR = Path('/root/algo_trading/config')
    CREDS_FILE = CONFIG_DIR / 'dhan_creds.json'

# ─── Security ID mapping ────────────────────────────────────────────────────
# Dhan uses numeric security IDs for underlyings
# Index segment: IDX_I, Options: NSE_FNO / BSE_FNO / MCX_FNO
INDEX_MAP = {
    'NIFTY':     {'security_id': 13,    'exchange_segment': 'IDX_I', 'opt_segment': 'NSE_FNO'},
    'BANKNIFTY': {'security_id': 25,    'exchange_segment': 'IDX_I', 'opt_segment': 'NSE_FNO'},
    'SENSEX':    {'security_id': 51,    'exchange_segment': 'IDX_I', 'opt_segment': 'BSE_FNO'},
    'FINNIFTY':  {'security_id': 27,    'exchange_segment': 'IDX_I', 'opt_segment': 'NSE_FNO'},
    'MIDCPNIFTY':{'security_id': 442,   'exchange_segment': 'IDX_I', 'opt_segment': 'NSE_FNO'},
}

MCX_MAP = {
    'GOLDM':      {'security_id': 0, 'exchange_segment': 'MCX_COMM', 'opt_segment': 'MCX_FNO'},
    'SILVERM':    {'security_id': 0, 'exchange_segment': 'MCX_COMM', 'opt_segment': 'MCX_FNO'},
    'CRUDEOILM':  {'security_id': 0, 'exchange_segment': 'MCX_COMM', 'opt_segment': 'MCX_FNO'},
}

# Scrip master CSV URL for dynamic MCX security ID resolution
SCRIP_MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master.csv'
SCRIP_MASTER_CACHE = Path(os.environ.get('DHAN_CONFIG_DIR', 'D:/AlgoTrading/config')) / 'dhan_scrip_master.csv'
if not SCRIP_MASTER_CACHE.parent.exists():
    SCRIP_MASTER_CACHE = Path('/root/algo_trading/config') / 'dhan_scrip_master.csv'

# Strike intervals
STRIKE_INTERVALS = {
    'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100,
    'FINNIFTY': 50, 'MIDCPNIFTY': 25,
    'GOLDM': 100, 'SILVERM': 500, 'CRUDEOILM': 50,
}

# ─── Stock Security IDs (NSE_EQ) ──────────────────────────────────────────
# Top F&O stocks — Dhan security_id for underlying equity
# Option chain uses: under_security_id=X, under_exchange_segment='NSE_EQ'
STOCK_MAP = {
    'RELIANCE': {'security_id': 2885, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 20},
    'TCS': {'security_id': 11536, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'HDFCBANK': {'security_id': 1333, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 20},
    'INFY': {'security_id': 1594, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'ICICIBANK': {'security_id': 4963, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 15},
    'SBIN': {'security_id': 3045, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'BHARTIARTL': {'security_id': 10604, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 20},
    'ITC': {'security_id': 1660, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'KOTAKBANK': {'security_id': 1922, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'LT': {'security_id': 11483, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'AXISBANK': {'security_id': 5900, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 15},
    'TATAMOTORS': {'security_id': 3456, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'MARUTI': {'security_id': 10999, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 100},
    'SUNPHARMA': {'security_id': 3351, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'TITAN': {'security_id': 3506, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'BAJFINANCE': {'security_id': 317, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 100},
    'TATASTEEL': {'security_id': 3499, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'WIPRO': {'security_id': 3787, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'HCLTECH': {'security_id': 7229, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'ADANIENT': {'security_id': 25, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'POWERGRID': {'security_id': 14977, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'NTPC': {'security_id': 11630, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'ONGC': {'security_id': 2475, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'COALINDIA': {'security_id': 20374, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'JSWSTEEL': {'security_id': 11723, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'HINDALCO': {'security_id': 1363, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'TECHM': {'security_id': 13538, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 20},
    'ULTRACEMCO': {'security_id': 11532, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 100},
    'ASIANPAINT': {'security_id': 236, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'M&M': {'security_id': 2031, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'BAJAJFINSV': {'security_id': 16675, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'HDFCLIFE': {'security_id': 467, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'DIVISLAB': {'security_id': 10940, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'DRREDDY': {'security_id': 881, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'CIPLA': {'security_id': 694, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 20},
    'APOLLOHOSP': {'security_id': 157, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'BPCL': {'security_id': 526, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'GRASIM': {'security_id': 1232, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'TATACONSUM': {'security_id': 3432, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'INDUSINDBK': {'security_id': 5258, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'EICHERMOT': {'security_id': 910, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'NESTLEIND': {'security_id': 17963, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
    'HEROMOTOCO': {'security_id': 1348, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'TRENT': {'security_id': 3551, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    'BEL': {'security_id': 383, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'HAL': {'security_id': 2303, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 50},
    # v30.4: Added missing watchlist stocks
    'CGPOWER': {'security_id': 760, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'DLF': {'security_id': 14732, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 10},
    'TATAPOWER': {'security_id': 3426, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 5},
    'LUPIN': {'security_id': 10440, 'exchange_segment': 'NSE_EQ', 'opt_segment': 'NSE_FNO', 'strike_interval': 25},
}


class DhanFeed:
    """Real-time data feed from Dhan API.

    Same interface as ZerodhaFeed for drop-in replacement:
    - get_spot(symbol)         -> float LTP
    - get_option_chain(symbol) -> {'CE': [...], 'PE': [...]}
    - get_option_ltp(symbol, strike, opt_type) -> float LTP
    - get_option_quote(symbol, strike, opt_type) -> {ltp, bid, ask, oi, iv, ...}
    - is_connected property

    Bonus (not in Zerodha):
    - Native IV and Greeks in option chain
    - Bid/Ask in option chain
    - OI change (current vs previous day)

    Dhan API v2 response format:
    - All responses: {'status': 'success', 'data': {'data': <payload>, 'status': 'success'}}
    - Option chain: data.data = {'last_price': N, 'oc': {'STRIKE': {'ce': {...}, 'pe': {...}}}}
    - Expiry list: data.data = ['2026-03-30', '2026-04-07', ...]
    """

    # Cache TTLs (seconds)
    SPOT_CACHE_TTL = 5       # 5s for spot
    CHAIN_CACHE_TTL = 10     # 10s for option chain (Dhan rate limit: 1 per 3s per underlying)
    QUOTE_CACHE_TTL = 5      # 5s for individual quotes

    def __init__(self, client_id=None, access_token=None):
        self.client_id = client_id
        self.access_token = access_token
        self.dhan = None
        self._connected = False
        self._lock = threading.Lock()

        # Caches
        self._spot_cache = {}      # symbol -> {ltp, _time}
        self._chain_cache = {}     # symbol -> full chain response + _cache_time
        self._quote_cache = {}     # "symbol:strike:CE/PE" -> {data, _time}
        self._expiry_cache = {}    # symbol -> {expiries: [...], _time}

        # Auto-load credentials
        if not client_id or not access_token:
            self._load_credentials()

    def _load_credentials(self):
        """Load credentials from config file."""
        if CREDS_FILE.exists():
            try:
                creds = json.loads(CREDS_FILE.read_text())
                self.client_id = creds.get('client_id', self.client_id)
                self.access_token = creds.get('access_token', self.access_token)
                logger.info(f"[DhanFeed] Loaded credentials from {CREDS_FILE}")
            except Exception as e:
                logger.error(f"[DhanFeed] Failed to load credentials: {e}")
        else:
            logger.warning(f"[DhanFeed] No credentials file at {CREDS_FILE}")

    def _unwrap(self, result):
        """Unwrap Dhan's nested response format: {status, data: {data: <payload>, status}}."""
        if not result or result.get('status') != 'success':
            return None
        data = result.get('data', {})
        if isinstance(data, dict) and 'data' in data:
            return data['data']
        return data

    def _try_auto_refresh_token(self):
        """Auto-refresh expired access token using TOTP if credentials available."""
        try:
            # Load config to get TOTP secret and PIN
            creds_path = CREDS_FILE
            if not creds_path.exists():
                return False
            creds = json.loads(creds_path.read_text())
            totp_secret = creds.get('totp_secret')
            pin = creds.get('pin')
            client_id = creds.get('client_id', self.client_id)
            if not totp_secret or not pin:
                logger.warning("[DhanFeed] Cannot auto-refresh: totp_secret or pin missing from config")
                return False

            import pyotp
            import requests
            totp_code = pyotp.TOTP(totp_secret).now()
            url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={client_id}&pin={pin}&totp={totp_code}"
            resp = requests.post(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get('accessToken')
                if token:
                    self.access_token = token
                    # Update config file
                    creds['access_token'] = token
                    creds['token_expiry'] = data.get('expiryTime', '')
                    creds['token_generated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    creds_path.write_text(json.dumps(creds, indent=4))
                    logger.info(f"[DhanFeed] AUTO-REFRESHED token, expires: {data.get('expiryTime', 'unknown')}")
                    return True
            logger.warning(f"[DhanFeed] Auto-refresh failed: HTTP {resp.status_code}")
            return False
        except Exception as e:
            logger.warning(f"[DhanFeed] Auto-refresh error: {e}")
            return False

    def connect(self):
        """Initialize Dhan API connection."""
        if not self.client_id or not self.access_token:
            logger.error("[DhanFeed] Missing client_id or access_token")
            return False

        try:
            from dhanhq import dhanhq
            self.dhan = dhanhq(self.client_id, self.access_token)
            self._connected = True
            logger.info(f"[DhanFeed] Connected (client: {self.client_id})")

            # Test connection with a simple call
            try:
                result = self.dhan.expiry_list(13, 'IDX_I')  # NIFTY expiry test
                expiries = self._unwrap(result)
                if isinstance(expiries, list) and expiries:
                    logger.info(f"[DhanFeed] Verified — NIFTY has {len(expiries)} expiries, nearest: {expiries[0]}")
                else:
                    # Token might be expired — try auto-refresh
                    logger.warning(f"[DhanFeed] Connection test failed — attempting auto-refresh...")
                    if self._try_auto_refresh_token():
                        self.dhan = dhanhq(self.client_id, self.access_token)
                        result = self.dhan.expiry_list(13, 'IDX_I')
                        expiries = self._unwrap(result)
                        if isinstance(expiries, list) and expiries:
                            logger.info(f"[DhanFeed] Verified after refresh — NIFTY {len(expiries)} expiries")
                        else:
                            logger.error(f"[DhanFeed] Still failing after token refresh: {result}")
                    else:
                        logger.warning(f"[DhanFeed] Auto-refresh unavailable: {result}")
            except Exception as e:
                logger.warning(f"[DhanFeed] Connection test failed: {e}")

            # Resolve MCX commodity security IDs from scrip master
            self._resolve_mcx_security_ids()

            return True
        except ImportError:
            logger.error("[DhanFeed] dhanhq package not installed. Run: pip install dhanhq")
            return False
        except Exception as e:
            logger.error(f"[DhanFeed] Connection failed: {e}")
            return False

    @property
    def is_connected(self):
        return self._connected and self.dhan is not None

    def _resolve_mcx_security_ids(self):
        """Download scrip master CSV and resolve MCX futures security IDs dynamically.
        MCX commodity options are OPTFUT (options on futures), so the underlying
        for option_chain() is the nearest-expiry FUTCOM contract.
        """
        import csv
        import urllib.request

        # Download scrip master if not cached today
        need_download = True
        if SCRIP_MASTER_CACHE.exists():
            age_hours = (time.time() - SCRIP_MASTER_CACHE.stat().st_mtime) / 3600
            if age_hours < 18:  # Refresh once per day
                need_download = False

        if need_download:
            try:
                logger.info("[DhanFeed] Downloading scrip master for MCX security IDs...")
                urllib.request.urlretrieve(SCRIP_MASTER_URL, str(SCRIP_MASTER_CACHE))
                logger.info(f"[DhanFeed] Scrip master saved to {SCRIP_MASTER_CACHE}")
            except Exception as e:
                logger.warning(f"[DhanFeed] Scrip master download failed: {e}")
                if not SCRIP_MASTER_CACHE.exists():
                    return

        # Parse CSV and find nearest FUTCOM for each MCX commodity
        now = datetime.now()
        mcx_futures = {}  # {symbol: [(security_id, expiry_dt), ...]}

        try:
            with open(str(SCRIP_MASTER_CACHE), 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get('SM_SYMBOL_NAME', '')
                    inst = row.get('SEM_INSTRUMENT_NAME', '')
                    if sym in MCX_MAP and inst == 'FUTCOM':
                        exp_str = row.get('SEM_EXPIRY_DATE', '')
                        try:
                            exp = datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
                            if exp >= now:
                                mcx_futures.setdefault(sym, []).append(
                                    (int(row['SEM_SMST_SECURITY_ID']), exp))
                        except (ValueError, KeyError):
                            continue
        except Exception as e:
            logger.error(f"[DhanFeed] Scrip master parse error: {e}")
            return

        # Pick nearest expiry futures for each commodity
        for sym, entries in mcx_futures.items():
            entries.sort(key=lambda x: x[1])
            nearest_sid, nearest_exp = entries[0]
            MCX_MAP[sym]['security_id'] = nearest_sid
            logger.info(f"[DhanFeed] MCX {sym}: security_id={nearest_sid} "
                       f"(futures expiry {nearest_exp.strftime('%Y-%m-%d')})")

        # Log any commodities that couldn't be resolved
        for sym, config in MCX_MAP.items():
            if config['security_id'] == 0:
                logger.warning(f"[DhanFeed] MCX {sym}: no active futures found in scrip master")

    # ─── SPOT DATA ───────────────────────────────────────────────────────────

    def get_spot(self, symbol):
        """Get current spot price for an index/underlying.

        Uses option_chain response (last_price field) since ticker_data
        doesn't support IDX_I segment. Falls back to cache.
        """
        if not self.is_connected:
            return None

        with self._lock:
            cached = self._spot_cache.get(symbol)
            if cached and (time.time() - cached.get('_time', 0)) < self.SPOT_CACHE_TTL:
                return cached.get('ltp')

        # Spot comes from option_chain response (last_price field)
        # Trigger a chain fetch which populates spot as side effect
        chain = self.get_option_chain(symbol)
        if chain:
            with self._lock:
                cached = self._spot_cache.get(symbol)
                if cached:
                    return cached.get('ltp')
        return None

    # ─── OPTION CHAIN ────────────────────────────────────────────────────────

    def get_nearest_expiry(self, symbol, skip_today=False):
        """Get nearest expiry date for the symbol."""
        if not self.is_connected:
            return None

        with self._lock:
            cached = self._expiry_cache.get(symbol)
            if cached and (time.time() - cached.get('_time', 0)) < 3600:  # 1 hour cache
                expiries = cached.get('expiries', [])
                if expiries:
                    if skip_today:
                        today = datetime.now().strftime('%Y-%m-%d')
                        expiries = [e for e in expiries if e > today]
                    return expiries[0] if expiries else None

        config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol) or STOCK_MAP.get(symbol)
        if not config:
            return None

        try:
            result = self.dhan.expiry_list(
                config['security_id'],
                config['exchange_segment']
            )
            expiries = self._unwrap(result)
            if isinstance(expiries, list) and expiries:
                expiries = sorted(expiries)
                with self._lock:
                    self._expiry_cache[symbol] = {'expiries': expiries, '_time': time.time()}
                if skip_today:
                    today = datetime.now().strftime('%Y-%m-%d')
                    expiries = [e for e in expiries if e > today]
                return expiries[0] if expiries else None
        except Exception as e:
            logger.debug(f"[DhanFeed] get_nearest_expiry({symbol}) error: {e}")
        return None

    def get_option_chain(self, symbol, expiry=None, chain_length=10):
        """Get option chain with IV, Greeks, OI, bid/ask.
        v30.2: If DHAN_SKIP_CHAIN=1, return None immediately (let caller use Zerodha).
        This prevents rate-limit clash when multiple bots share same Dhan account.

        Dhan response format:
            data.data = {
                'last_price': 22819.6,
                'oc': {
                    '22800.000000': {
                        'ce': {ltp, oi, iv, greeks: {delta,gamma,theta,vega}, bid/ask, ...},
                        'pe': {ltp, oi, iv, greeks: {delta,gamma,theta,vega}, bid/ask, ...}
                    }, ...
                }
            }

        Returns normalized format (same as ZerodhaFeed):
            {
                'CE': [{'strike': N, 'ltp': N, 'oi': N, 'iv': N, ...}],
                'PE': [...],
                'expiry': 'YYYY-MM-DD',
                'spot': N
            }
        """
        if not self.is_connected:
            return None
        # v30.2: Skip Dhan chain API when env set (shadow bots use Zerodha to avoid rate limits)
        if os.environ.get('DHAN_SKIP_CHAIN') == '1':
            return None

        # Check cache
        with self._lock:
            cached = self._chain_cache.get(symbol)
            if cached and (time.time() - cached.get('_cache_time', 0)) < self.CHAIN_CACHE_TTL:
                return cached

        config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol) or STOCK_MAP.get(symbol)
        if not config:
            logger.debug(f"[DhanFeed] Unknown symbol: {symbol} — not in INDEX/MCX/STOCK maps")
            return None

        # Get expiry
        if expiry is None:
            expiry = self.get_nearest_expiry(symbol)
        if expiry is None:
            logger.warning(f"[DhanFeed] No expiry found for {symbol}")
            return None

        try:
            result = self.dhan.option_chain(
                under_security_id=config['security_id'],
                under_exchange_segment=config['exchange_segment'],
                expiry=expiry
            )

            payload = self._unwrap(result)
            if not payload or not isinstance(payload, dict):
                logger.warning(f"[DhanFeed] option_chain({symbol}) failed: {result}")
                return None

            spot = payload.get('last_price', 0)
            oc_data = payload.get('oc', {})

            # Update spot cache from option chain response
            if spot and spot > 0:
                with self._lock:
                    self._spot_cache[symbol] = {'ltp': spot, '_time': time.time()}

            chain = {'CE': [], 'PE': [], 'expiry': expiry, 'spot': spot, '_cache_time': time.time()}

            for strike_str, sides in oc_data.items():
                try:
                    strike = float(strike_str)
                    # Round to integer if it's a whole number
                    if strike == int(strike):
                        strike = int(strike)
                except (ValueError, TypeError):
                    continue

                for side_key, side_label in [('ce', 'CE'), ('pe', 'PE')]:
                    row = sides.get(side_key, {})
                    if not row:
                        continue

                    greeks = row.get('greeks', {})
                    entry = {
                        'strike': strike,
                        'ltp': row.get('last_price', 0),
                        'oi': row.get('oi', 0),
                        'oi_prev': row.get('previous_oi', 0),
                        'oi_change': row.get('oi', 0) - row.get('previous_oi', 0),
                        'iv': row.get('implied_volatility', 0),
                        'volume': row.get('volume', 0),
                        'prev_volume': row.get('previous_volume', 0),
                        'bid': row.get('top_bid_price', 0),
                        'ask': row.get('top_ask_price', 0),
                        'bid_qty': row.get('top_bid_quantity', 0),
                        'ask_qty': row.get('top_ask_quantity', 0),
                        'avg_price': row.get('average_price', 0),
                        'prev_close': row.get('previous_close_price', 0),
                        # Native Greeks from Dhan (no BS computation needed!)
                        'delta': greeks.get('delta', 0),
                        'gamma': greeks.get('gamma', 0),
                        'theta': greeks.get('theta', 0),
                        'vega': greeks.get('vega', 0),
                        # Security ID for downstream quote tracking
                        'security_id': row.get('security_id', ''),
                    }
                    chain[side_label].append(entry)

            # Sort by strike
            chain['CE'].sort(key=lambda x: x['strike'])
            chain['PE'].sort(key=lambda x: x['strike'])

            # Trim to chain_length around ATM
            if spot and spot > 0:
                interval = STRIKE_INTERVALS.get(symbol, 50)
                atm = round(spot / interval) * interval
                for side in ('CE', 'PE'):
                    chain[side] = [
                        c for c in chain[side]
                        if abs(c['strike'] - atm) <= chain_length * interval
                    ]

            with self._lock:
                self._chain_cache[symbol] = chain

            ce_count = len(chain['CE'])
            pe_count = len(chain['PE'])
            logger.debug(f"[DhanFeed] option_chain({symbol}): "
                        f"{ce_count} CE + {pe_count} PE strikes, spot={spot}, expiry={expiry}")
            return chain

        except Exception as e:
            logger.error(f"[DhanFeed] option_chain({symbol}) error: {e}")
            return None

    # ─── INDIVIDUAL OPTION QUOTE ─────────────────────────────────────────────

    def get_option_ltp(self, symbol, strike, opt_type, expiry=None):
        """Get live LTP for a specific option contract."""
        quote = self.get_option_quote(symbol, strike, opt_type, expiry)
        if quote:
            return quote.get('ltp', 0)
        return 0

    def get_option_quote(self, symbol, strike, opt_type, expiry=None):
        """Get full quote for a specific option contract.

        Returns:
            dict: {ltp, bid, ask, oi, iv, delta, gamma, theta, vega, volume, ...}
            or None
        """
        # Try from cached chain first
        chain = self.get_option_chain(symbol, expiry)
        if chain:
            side = 'CE' if opt_type.upper() in ('CE', 'CALL') else 'PE'
            for c in chain.get(side, []):
                if c['strike'] == strike:
                    return c
        return None

    # ─── QUOTE DATA (multi-instrument) ───────────────────────────────────────

    def get_quote(self, exchange_segment, security_ids):
        """Get full market depth quotes for multiple instruments.

        Args:
            exchange_segment: e.g., 'NSE_FNO', 'BSE_FNO'
            security_ids: list of security ID strings

        Returns:
            dict keyed by security_id with full quote data
        """
        if not self.is_connected:
            return None

        try:
            result = self.dhan.quote_data({exchange_segment: security_ids})
            return self._unwrap(result)
        except Exception as e:
            logger.debug(f"[DhanFeed] get_quote error: {e}")
        return None

    # ─── HISTORICAL DATA ─────────────────────────────────────────────────────

    def get_intraday_ohlc(self, symbol, security_id=None, exchange_segment=None, interval='1'):
        """Get intraday OHLCV data (last 5 trading days)."""
        if not self.is_connected:
            return None

        if security_id is None:
            config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol) or STOCK_MAP.get(symbol)
            if not config:
                return None
            security_id = config['security_id']
            exchange_segment = config['exchange_segment']

        # Determine instrument type
        inst_type = 'INDEX' if symbol in INDEX_MAP else 'EQUITY'
        try:
            from datetime import datetime, timedelta
            to_date = datetime.now().strftime('%Y-%m-%d')
            from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            result = self.dhan.intraday_minute_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=inst_type,
                from_date=from_date,
                to_date=to_date
            )
            return self._unwrap(result)
        except Exception as e:
            logger.debug(f"[DhanFeed] get_intraday_ohlc({symbol}) error: {e}")
            return None

    # ─── UTILITY ─────────────────────────────────────────────────────────────

    def get_stock_spot(self, symbol):
        """Get stock spot price via Dhan ticker_data (NSE_EQ).
        Unlike indices, stocks work with ticker_data.
        """
        if not self.is_connected:
            return None

        config = STOCK_MAP.get(symbol)
        if not config:
            return None

        # Check cache
        with self._lock:
            cached = self._spot_cache.get(symbol)
            if cached and (time.time() - cached.get('_time', 0)) < self.SPOT_CACHE_TTL:
                return cached.get('ltp')

        try:
            result = self.dhan.ticker_data(
                security_id=config['security_id'],
                exchange_segment=config['exchange_segment']
            )
            data = self._unwrap(result)
            if data and isinstance(data, dict):
                ltp = data.get('last_price', 0)
                if ltp and ltp > 0:
                    with self._lock:
                        self._spot_cache[symbol] = {'ltp': ltp, '_time': time.time()}
                    return ltp
        except Exception as e:
            logger.debug(f"[DhanFeed] get_stock_spot({symbol}) error: {e}")

        # Fallback: get from option chain's last_price
        return self.get_spot(symbol)

    def get_atm_strike(self, symbol, spot=None):
        """Get ATM strike for a symbol."""
        if spot is None:
            spot = self.get_spot(symbol)
        if not spot or spot <= 0:
            return None
        interval = STRIKE_INTERVALS.get(symbol, 50)
        return round(spot / interval) * interval

    def get_pcr(self, symbol):
        """Compute PCR from option chain OI."""
        chain = self.get_option_chain(symbol)
        if not chain:
            return None
        total_ce_oi = sum(c.get('oi', 0) for c in chain.get('CE', []))
        total_pe_oi = sum(c.get('oi', 0) for c in chain.get('PE', []))
        return round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else None

    def get_chain_summary(self, symbol):
        """Get a summary of the current option chain (for logging)."""
        chain = self.get_option_chain(symbol)
        if not chain:
            return f"{symbol}: no chain data"

        total_ce_oi = sum(c.get('oi', 0) for c in chain.get('CE', []))
        total_pe_oi = sum(c.get('oi', 0) for c in chain.get('PE', []))
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0

        # Max pain: strike with highest total OI
        oi_by_strike = {}
        for c in chain.get('CE', []) + chain.get('PE', []):
            s = c['strike']
            oi_by_strike[s] = oi_by_strike.get(s, 0) + c.get('oi', 0)
        max_pain = max(oi_by_strike, key=oi_by_strike.get) if oi_by_strike else 0

        return (f"{symbol}: spot={chain.get('spot', '?')} CE_OI={total_ce_oi:,} PE_OI={total_pe_oi:,} "
                f"PCR={pcr:.2f} MaxPain={max_pain} Expiry={chain.get('expiry', '?')}")

    def disconnect(self):
        """Cleanup."""
        self._connected = False
        self.dhan = None
        logger.info("[DhanFeed] Disconnected")


# ─── Quick test ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    feed = DhanFeed()
    if feed.connect():
        print("\n=== Dhan Feed Test ===\n")

        for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            chain = feed.get_option_chain(sym)
            if chain:
                spot = chain.get('spot', 0)
                atm = feed.get_atm_strike(sym, spot)
                interval = STRIKE_INTERVALS.get(sym, 50)
                print(f"{sym} | Spot: {spot} | ATM: {atm} | Expiry: {chain.get('expiry')}")
                print(f"  CE: {len(chain['CE'])} strikes | PE: {len(chain['PE'])} strikes")
                print(f"  {'Strike':>8} | {'CE LTP':>8} {'CE IV':>6} {'CE OI':>10} {'CE Dlt':>7} "
                      f"| {'PE LTP':>8} {'PE IV':>6} {'PE OI':>10} {'PE Dlt':>7} "
                      f"| {'Bid':>7} {'Ask':>7}")
                for offset in range(-3, 4):
                    s = atm + offset * interval
                    ce = next((c for c in chain['CE'] if c['strike'] == s), None)
                    pe = next((c for c in chain['PE'] if c['strike'] == s), None)
                    tag = ' <--' if offset == 0 else ''
                    print(f"  {s:>8} | "
                          f"{ce['ltp'] if ce else 0:>8.2f} {ce['iv'] if ce else 0:>5.1f}% "
                          f"{ce['oi'] if ce else 0:>10,} {ce['delta'] if ce else 0:>7.3f} | "
                          f"{pe['ltp'] if pe else 0:>8.2f} {pe['iv'] if pe else 0:>5.1f}% "
                          f"{pe['oi'] if pe else 0:>10,} {pe['delta'] if pe else 0:>7.3f} | "
                          f"{ce['bid'] if ce else 0:>7.2f} {ce['ask'] if ce else 0:>7.2f}{tag}")
                print(f"  {feed.get_chain_summary(sym)}")
                print()
            else:
                print(f"{sym}: No chain data\n")

        feed.disconnect()
    else:
        print("Failed to connect. Check credentials in config/dhan_creds.json")
