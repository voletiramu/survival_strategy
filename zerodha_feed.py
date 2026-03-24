"""
Zerodha Kite Connect Feed — Primary data source for spot + option chain + LTP.
v1.0: Integrated as Source 1 in all trading bots.

Priority: Zerodha Kite > TrueData > NSE/BSE Pipeline > Angel API

Requirements:
    pip install kiteconnect pyotp

Setup:
    1. Subscribe at https://developers.kite.trade (Rs 2,000/month)
    2. Get API key and API secret
    3. Store credentials in D:/AlgoTrading/config/zerodha_creds.json
    4. Run token_gen.py daily (or automate via Task Scheduler)
"""
import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Default paths ───────────────────────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get('ZERODHA_CONFIG_DIR', 'D:/AlgoTrading/config'))
CREDS_FILE = CONFIG_DIR / 'zerodha_creds.json'
TOKEN_FILE = CONFIG_DIR / 'zerodha_token.json'   # daily access token

# VPS paths (auto-detect)
if not CONFIG_DIR.exists():
    CONFIG_DIR = Path('/root/algo_trading/config')
    CREDS_FILE = CONFIG_DIR / 'zerodha_creds.json'
    TOKEN_FILE = CONFIG_DIR / 'zerodha_token.json'

# ─── Symbol mapping ──────────────────────────────────────────────────────────
# Internal bot names → Kite instrument exchange & tradingsymbol patterns
INDEX_MAP = {
    'NIFTY': {'exchange': 'NSE', 'symbol': 'NIFTY 50', 'opt_exchange': 'NFO', 'opt_prefix': 'NIFTY'},
    'BANKNIFTY': {'exchange': 'NSE', 'symbol': 'NIFTY BANK', 'opt_exchange': 'NFO', 'opt_prefix': 'BANKNIFTY'},
    'SENSEX': {'exchange': 'BSE', 'symbol': 'SENSEX', 'opt_exchange': 'BFO', 'opt_prefix': 'SENSEX'},
}

MCX_MAP = {
    'GOLDM': {'exchange': 'MCX', 'symbol': 'GOLDM', 'opt_exchange': 'MCX', 'opt_prefix': 'GOLDM'},
    'SILVERM': {'exchange': 'MCX', 'symbol': 'SILVERM', 'opt_exchange': 'MCX', 'opt_prefix': 'SILVERM'},
    'CRUDEOILM': {'exchange': 'MCX', 'symbol': 'CRUDEOILM', 'opt_exchange': 'MCX', 'opt_prefix': 'CRUDEOILM'},
}


class ZerodhaFeed:
    """Real-time data feed from Zerodha Kite Connect API.

    Provides (same interface as TrueDataFeed):
    - get_spot(symbol)        → float LTP
    - get_option_chain(symbol) → {'CE': [...], 'PE': [...]}
    - get_option_ltp(symbol, strike, opt_type) → float LTP
    - is_connected property

    Thread-safe with internal caching.
    """

    def __init__(self, api_key=None, access_token=None):
        self.api_key = api_key
        self.access_token = access_token
        self.kite = None
        self._connected = False
        self._lock = threading.Lock()

        # Instrument cache (loaded once at connect)
        self._instruments = {}    # exchange -> list of instruments
        self._inst_by_token = {}  # instrument_token -> instrument dict
        self._inst_lookup = {}    # "EXCHANGE:TRADINGSYMBOL" -> instrument dict

        # Data caches
        self._spot_cache = {}     # symbol -> {ltp, high, low, open, _time}
        self._chain_cache = {}    # symbol -> {CE: [...], PE: [...], _cache_time}
        self._ltp_cache = {}      # "exchange:token" -> {ltp, _time}

        # Cache TTLs (seconds) — v15: reduced for tick-by-tick scanning
        self.SPOT_CACHE_TTL = 2       # was 3s
        self.CHAIN_CACHE_TTL = 10     # was 15s — chain includes OI for PCR
        self.LTP_CACHE_TTL = 3        # was 5s — critical for exit decisions

    def connect(self):
        """Connect to Kite Connect using stored credentials + access token."""
        try:
            from kiteconnect import KiteConnect

            # Load API key from creds file if not provided
            if not self.api_key or not self.access_token:
                self._load_credentials()

            if not self.api_key:
                logger.error("[Zerodha] No API key found. Run zerodha_token_gen.py first.")
                return False

            if not self.access_token:
                logger.error("[Zerodha] No access token found. Run zerodha_token_gen.py to generate daily token.")
                return False

            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(self.access_token)

            # Verify connection by fetching profile
            profile = self.kite.profile()
            user_name = profile.get('user_name', 'Unknown')
            logger.info(f"[Zerodha] Connected: {user_name} (API key: {self.api_key[:8]}...)")

            # Load instruments for NFO, BFO, MCX, NSE, BSE
            self._load_instruments()

            self._connected = True
            return True

        except Exception as e:
            logger.error(f"[Zerodha] Connection failed: {e}")
            self._connected = False
            return False

    def _load_credentials(self):
        """Load API key and access token from config files."""
        # Load API key + secret
        if CREDS_FILE.exists():
            try:
                creds = json.loads(CREDS_FILE.read_text())
                self.api_key = self.api_key or creds.get('api_key', '')
            except Exception as e:
                logger.error(f"[Zerodha] Failed to read {CREDS_FILE}: {e}")

        # Load daily access token
        if TOKEN_FILE.exists():
            try:
                token_data = json.loads(TOKEN_FILE.read_text())
                token_date = token_data.get('date', '')
                today = datetime.now().strftime('%Y-%m-%d')

                if token_date == today:
                    self.access_token = self.access_token or token_data.get('access_token', '')
                else:
                    logger.warning(f"[Zerodha] Token is stale (from {token_date}). Run zerodha_token_gen.py.")
            except Exception as e:
                logger.error(f"[Zerodha] Failed to read {TOKEN_FILE}: {e}")

    def _load_instruments(self):
        """Load instrument master for required exchanges."""
        exchanges = ['NFO', 'BFO', 'MCX', 'NSE', 'BSE']
        total = 0

        for exchange in exchanges:
            try:
                instruments = self.kite.instruments(exchange)
                self._instruments[exchange] = instruments
                for inst in instruments:
                    key = f"{exchange}:{inst['tradingsymbol']}"
                    self._inst_lookup[key] = inst
                    self._inst_by_token[inst['instrument_token']] = inst
                total += len(instruments)
            except Exception as e:
                logger.warning(f"[Zerodha] Failed to load {exchange} instruments: {e}")

        logger.info(f"[Zerodha] Loaded {total} instruments across {len(self._instruments)} exchanges")

    def disconnect(self):
        """Disconnect from Kite Connect."""
        self._connected = False
        self.kite = None
        logger.info("[Zerodha] Disconnected")

    # ─── Spot Price ───────────────────────────────────────────────────────────

    def get_spot(self, symbol):
        """Get spot price for a symbol. Returns float or None.

        Works for indices (NIFTY, BANKNIFTY, SENSEX),
        commodities (GOLDM, SILVERM, CRUDEOILM), and stocks.
        """
        if not self._connected:
            return None

        # Check cache
        with self._lock:
            cached = self._spot_cache.get(symbol)
            if cached and (time.time() - cached.get('_time', 0)) < self.SPOT_CACHE_TTL:
                return cached.get('ltp')

        try:
            kite_sym = self._get_spot_symbol(symbol)
            if not kite_sym:
                return None

            quote = self.kite.quote([kite_sym])
            if kite_sym in quote:
                data = quote[kite_sym]
                ltp = data.get('last_price', 0)
                if ltp > 0:
                    with self._lock:
                        self._spot_cache[symbol] = {
                            'ltp': ltp,
                            'open': data.get('ohlc', {}).get('open', 0),
                            'high': data.get('ohlc', {}).get('high', 0),
                            'low': data.get('ohlc', {}).get('low', 0),
                            'prev_close': data.get('ohlc', {}).get('close', 0),
                            'volume': data.get('volume', 0),
                            'oi': data.get('oi', 0),
                            '_time': time.time(),
                        }
                    return ltp
        except Exception as e:
            logger.debug(f"[Zerodha] Spot fetch failed for {symbol}: {e}")

        return None

    def _get_spot_symbol(self, symbol):
        """Get Kite quote key for spot data (e.g., 'NSE:NIFTY 50')."""
        if symbol in INDEX_MAP:
            m = INDEX_MAP[symbol]
            return f"{m['exchange']}:{m['symbol']}"
        elif symbol in MCX_MAP:
            # MCX futures — need to find nearest month contract
            m = MCX_MAP[symbol]
            fut_sym = self._find_nearest_future(m['exchange'], m['symbol'])
            return f"{m['exchange']}:{fut_sym}" if fut_sym else None
        else:
            # Stock — assume NSE
            return f"NSE:{symbol}"

    def _find_nearest_future(self, exchange, base_symbol):
        """Find nearest month futures contract for MCX."""
        instruments = self._instruments.get(exchange, [])
        today = datetime.now().date()
        candidates = []

        for inst in instruments:
            if (inst.get('name', '') == base_symbol and
                    inst.get('instrument_type', '') == 'FUT' and
                    inst.get('expiry')):
                exp = inst['expiry']
                if isinstance(exp, datetime):
                    exp = exp.date()
                if exp >= today:
                    candidates.append((exp, inst['tradingsymbol']))

        if candidates:
            candidates.sort()
            return candidates[0][1]  # nearest expiry future
        return None

    # ─── Option Chain ─────────────────────────────────────────────────────────

    def get_option_chain(self, symbol, expiry_dt=None, chain_length=10):
        """Get option chain in same format as TrueDataFeed/market_data_pipeline.

        Returns:
            dict: {'CE': [{strike, ltp, oi, iv, volume}], 'PE': [...], 'expiry': str}
            or None if unavailable.
        """
        if not self._connected:
            return None

        # Check cache
        with self._lock:
            cached = self._chain_cache.get(symbol)
            if cached and (time.time() - cached.get('_cache_time', 0)) < self.CHAIN_CACHE_TTL:
                return cached

        try:
            # Get spot to center the chain
            spot = self.get_spot(symbol)
            if not spot or spot <= 0:
                return None

            # Find option exchange and prefix
            config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol)
            if config:
                opt_exchange = config['opt_exchange']
                opt_prefix = config['opt_prefix']
            else:
                # Stock F&O
                opt_exchange = 'NFO'
                opt_prefix = symbol

            # Determine expiry
            if expiry_dt is None:
                expiry_dt = self._get_nearest_expiry(symbol)
            if expiry_dt is None:
                return None

            # Get strike interval
            strike_interval = self._get_strike_interval(symbol, spot)

            # ATM strike
            atm = round(spot / strike_interval) * strike_interval

            # Build list of strikes around ATM
            strikes = []
            for i in range(-chain_length, chain_length + 1):
                strikes.append(atm + i * strike_interval)

            # Find instrument tokens for all CE and PE at these strikes
            instruments_to_quote = []
            strike_inst_map = {}  # "exchange:symbol" -> {strike, opt_type, inst}

            all_exchange_insts = self._instruments.get(opt_exchange, [])
            for inst in all_exchange_insts:
                if inst.get('name', '') != opt_prefix:
                    continue
                inst_exp = inst.get('expiry')
                if inst_exp:
                    if isinstance(inst_exp, datetime):
                        inst_exp = inst_exp.date()
                    if inst_exp != expiry_dt:
                        continue

                inst_strike = inst.get('strike', 0)
                inst_type = inst.get('instrument_type', '')

                if inst_strike in strikes and inst_type in ('CE', 'PE'):
                    key = f"{opt_exchange}:{inst['tradingsymbol']}"
                    instruments_to_quote.append(key)
                    strike_inst_map[key] = {
                        'strike': inst_strike,
                        'opt_type': inst_type,
                        'token': inst['instrument_token'],
                        'tradingsymbol': inst['tradingsymbol'],
                    }

            if not instruments_to_quote:
                logger.debug(f"[Zerodha] No option instruments found for {symbol} exp={expiry_dt}")
                return None

            # Kite quote API allows max 500 instruments per call
            ce_list = []
            pe_list = []

            # Fetch in batches of 200
            for i in range(0, len(instruments_to_quote), 200):
                batch = instruments_to_quote[i:i + 200]
                try:
                    quotes = self.kite.quote(batch)
                    for key, data in quotes.items():
                        meta = strike_inst_map.get(key)
                        if not meta:
                            continue

                        ltp = data.get('last_price', 0)
                        if ltp <= 0:
                            continue

                        entry = {
                            'strike': meta['strike'],
                            'ltp': ltp,
                            'oi': data.get('oi', 0),
                            'volume': data.get('volume', 0),
                            'iv': 0,  # Kite doesn't provide IV directly — calculated by greeks.py
                            'bid': data.get('depth', {}).get('buy', [{}])[0].get('price', 0),
                            'ask': data.get('depth', {}).get('sell', [{}])[0].get('price', 0),
                            'token': meta['token'],
                            'tradingsymbol': meta['tradingsymbol'],
                        }

                        if meta['opt_type'] == 'CE':
                            ce_list.append(entry)
                        else:
                            pe_list.append(entry)
                except Exception as e:
                    logger.warning(f"[Zerodha] Quote batch failed: {e}")

            if not ce_list and not pe_list:
                return None

            # Sort by strike
            ce_list.sort(key=lambda x: x['strike'])
            pe_list.sort(key=lambda x: x['strike'])

            result = {
                'CE': ce_list,
                'PE': pe_list,
                'expiry': str(expiry_dt),
                '_fetch_time': datetime.now(),
                '_cache_time': time.time(),
                '_source': 'zerodha',
            }

            with self._lock:
                self._chain_cache[symbol] = result

            logger.info(f"[Zerodha] Chain: {symbol} {len(ce_list)} CE + {len(pe_list)} PE strikes (exp={expiry_dt})")
            return result

        except Exception as e:
            logger.warning(f"[Zerodha] Chain fetch failed for {symbol}: {e}")
            return None

    # ─── Option LTP ───────────────────────────────────────────────────────────

    def get_option_ltp(self, symbol, strike, opt_type, expiry_dt=None):
        """Get LTP for a specific option contract.

        Args:
            symbol: Internal symbol (NIFTY, BANKNIFTY, GOLDM, etc.)
            strike: Strike price (float or int)
            opt_type: 'CE' or 'PE'
            expiry_dt: Expiry date or None for nearest

        Returns:
            float: LTP or 0 if unavailable
        """
        # Try from cached chain first
        chain = self.get_option_chain(symbol, expiry_dt)
        if chain:
            contracts = chain.get(opt_type, [])
            for c in contracts:
                if c['strike'] == strike and c.get('ltp', 0) > 0:
                    return c['ltp']

        # Direct lookup if not in chain
        if not self._connected:
            return 0

        try:
            config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol)
            if config:
                opt_exchange = config['opt_exchange']
                opt_prefix = config['opt_prefix']
            else:
                opt_exchange = 'NFO'
                opt_prefix = symbol

            if expiry_dt is None:
                expiry_dt = self._get_nearest_expiry(symbol)

            # Find the specific instrument
            tradingsymbol = self._find_option_instrument(
                opt_exchange, opt_prefix, strike, opt_type, expiry_dt
            )
            if not tradingsymbol:
                return 0

            key = f"{opt_exchange}:{tradingsymbol}"

            # Check LTP cache
            with self._lock:
                cached = self._ltp_cache.get(key)
                if cached and (time.time() - cached.get('_time', 0)) < self.LTP_CACHE_TTL:
                    return cached.get('ltp', 0)

            quote = self.kite.quote([key])
            if key in quote:
                ltp = quote[key].get('last_price', 0)
                if ltp > 0:
                    with self._lock:
                        self._ltp_cache[key] = {'ltp': ltp, '_time': time.time()}
                    return ltp

        except Exception as e:
            logger.debug(f"[Zerodha] Option LTP failed for {symbol} {strike}{opt_type}: {e}")

        return 0

    def _find_option_instrument(self, exchange, name, strike, opt_type, expiry_dt):
        """Find tradingsymbol for a specific option contract."""
        instruments = self._instruments.get(exchange, [])
        strike = float(strike)

        for inst in instruments:
            if (inst.get('name', '') == name and
                    inst.get('instrument_type', '') == opt_type and
                    inst.get('strike', 0) == strike):
                inst_exp = inst.get('expiry')
                if inst_exp:
                    if isinstance(inst_exp, datetime):
                        inst_exp = inst_exp.date()
                    if inst_exp == expiry_dt:
                        return inst['tradingsymbol']
        return None

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get_strike_interval(self, symbol, spot):
        """Get strike interval for a symbol."""
        intervals = {
            'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100,
            'GOLDM': 100, 'SILVERM': 500, 'CRUDEOILM': 50,
        }
        if symbol in intervals:
            return intervals[symbol]
        # Stock — dynamic based on price
        if spot > 5000:
            return 50
        elif spot > 1000:
            return 20
        elif spot > 500:
            return 10
        else:
            return 5

    def _get_nearest_expiry(self, symbol):
        """Get nearest expiry date for a symbol by scanning loaded instruments."""
        config = INDEX_MAP.get(symbol) or MCX_MAP.get(symbol)
        if config:
            opt_exchange = config['opt_exchange']
            opt_prefix = config['opt_prefix']
        else:
            opt_exchange = 'NFO'
            opt_prefix = symbol

        today = datetime.now().date()
        instruments = self._instruments.get(opt_exchange, [])
        expiries = set()

        for inst in instruments:
            if (inst.get('name', '') == opt_prefix and
                    inst.get('instrument_type', '') in ('CE', 'PE') and
                    inst.get('expiry')):
                exp = inst['expiry']
                if isinstance(exp, datetime):
                    exp = exp.date()
                if exp >= today:
                    expiries.add(exp)

        if expiries:
            return min(expiries)  # nearest expiry

        # Fallback: calculate like truedata_feed
        return self._calc_nearest_expiry(symbol)

    def _calc_nearest_expiry(self, symbol):
        """Calculate nearest expiry (fallback if instruments not loaded)."""
        today = datetime.now().date()
        weekday = today.weekday()

        if symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            days_until_thu = (3 - weekday) % 7
            if days_until_thu == 0 and datetime.now().hour >= 15:
                days_until_thu = 7
            return today + timedelta(days=days_until_thu)
        elif symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM'):
            if today.day > 25:
                if today.month == 12:
                    return datetime(today.year + 1, 1, 25).date()
                else:
                    return datetime(today.year, today.month + 1, 25).date()
            return datetime(today.year, today.month, 25).date()
        else:
            # Stock — last Thursday of month
            if today.month == 12:
                next_month_start = datetime(today.year + 1, 1, 1).date()
            else:
                next_month_start = datetime(today.year, today.month + 1, 1).date()
            last_day = next_month_start - timedelta(days=1)
            while last_day.weekday() != 3:
                last_day -= timedelta(days=1)
            if last_day <= today:
                if next_month_start.month == 12:
                    nm2 = datetime(next_month_start.year + 1, 1, 1).date()
                else:
                    nm2 = datetime(next_month_start.year, next_month_start.month + 1, 1).date()
                last_day = nm2 - timedelta(days=1)
                while last_day.weekday() != 3:
                    last_day -= timedelta(days=1)
            return last_day

    # ─── OHLC / Historical ───────────────────────────────────────────────────

    def get_historical_data(self, symbol, interval='minute', days=1):
        """Get historical OHLCV candles for intraday VWAP computation.

        Args:
            symbol: Internal symbol name
            interval: 'minute', '5minute', '15minute', 'day'
            days: Number of days of data

        Returns:
            list of dicts: [{date, open, high, low, close, volume}, ...]
        """
        if not self._connected:
            return []

        try:
            kite_sym = self._get_spot_symbol(symbol)
            if not kite_sym:
                return []

            # Need instrument_token for historical API
            inst = self._inst_lookup.get(kite_sym)
            if not inst:
                return []

            token = inst['instrument_token']
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days)

            candles = self.kite.historical_data(
                token, from_date, to_date, interval
            )
            return candles

        except Exception as e:
            logger.debug(f"[Zerodha] Historical data failed for {symbol}: {e}")
            return []


    def get_option_historical(self, symbol, strike, opt_type, expiry_dt=None, interval="day", days=2):
        """Get historical OHLCV for a specific option contract."""
        if not self._connected:
            return []
        try:
            cfg = {**INDEX_MAP, **MCX_MAP}.get(symbol)
            if not cfg:
                return []
            exchange = cfg.get("opt_exchange", "NFO")
            name = cfg.get("opt_prefix", symbol)
            if expiry_dt is None:
                from datetime import date as _date
                today = _date.today()
                dow = today.weekday()
                if symbol == "BANKNIFTY":
                    exp_dow = 2
                elif symbol == "SENSEX":
                    exp_dow = 0
                else:
                    exp_dow = 3
                days_ahead = (exp_dow - dow) % 7
                if days_ahead == 0:
                    days_ahead = 7
                expiry_dt = today + __import__("datetime").timedelta(days=days_ahead)
            inst = self._find_option_instrument(exchange, name, strike, opt_type, expiry_dt)
            if not inst:
                return []
            key = f"{exchange}:{inst}"
            token = None
            with self._lock:
                if key in self._inst_lookup:
                    token = self._inst_lookup[key]
            if not token:
                return []
            from_date = __import__("datetime").datetime.now() - __import__("datetime").timedelta(days=days)
            to_date = __import__("datetime").datetime.now()
            candles = self.kite.historical_data(token, from_date, to_date, interval)
            return candles
        except Exception as e:
            logger.debug(f"[Zerodha] Option historical failed {symbol} {strike} {opt_type}: {e}")
            return []

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def is_connected(self):
        return self._connected
