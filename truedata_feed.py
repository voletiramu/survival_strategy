"""
TrueData Live Feed — Primary data source for option chain + spot data.
v11.2: Integrated as Source 1 in all trading bots.

Priority: TrueData > NSE/BSE Pipeline > Angel API

Trial account: Trial138 / rambabu138 (50 symbols, expires 2026-03-23)
After upgrade: Optima plan for 250+ symbols.
"""
import os
import time
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# TrueData symbol mapping (internal name -> TrueData name)
SYMBOL_MAP = {
    # Indices
    'NIFTY': 'NIFTY 50',
    'BANKNIFTY': 'NIFTY BANK',
    'SENSEX': 'SENSEX',
    # MCX continuous futures
    'GOLDM': 'GOLDM-I',
    'SILVERM': 'SILVERM-I',
    'CRUDEOILM': 'CRUDEOIL-I',
}

# Reverse map
REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}

# TrueData credentials
TD_USERNAME = os.environ.get('TRUEDATA_USER', 'Trial138')
TD_PASSWORD = os.environ.get('TRUEDATA_PASS', 'rambabu138')
TD_PORT = int(os.environ.get('TRUEDATA_PORT', '8086'))


class TrueDataFeed:
    """Real-time data feed from TrueData WebSocket.

    Provides:
    - Spot prices via touchline data
    - Option chain with LTP, OI, volume per strike
    - Greeks (delta, gamma, theta, vega, IV) via greek_feed (if available)

    Thread-safe with internal caching.
    """

    def __init__(self, username=None, password=None, port=None):
        self.username = username or TD_USERNAME
        self.password = password or TD_PASSWORD
        self.port = port or TD_PORT
        self.td = None
        self._connected = False
        self._lock = threading.Lock()

        # Caches
        self._spot_cache = {}       # symbol -> {ltp, high, low, open, oi, timestamp}
        self._chain_cache = {}      # symbol -> {CE: [...], PE: [...], expiry, _fetch_time}
        self._greek_cache = {}      # option_symbol -> {delta, gamma, theta, vega, iv}
        self._active_symbols = set()
        self._active_chains = set()

        # Cache TTLs
        self.SPOT_CACHE_TTL = 5       # 5 seconds
        self.CHAIN_CACHE_TTL = 30     # 30 seconds
        self.GREEK_CACHE_TTL = 10     # 10 seconds

    def connect(self):
        """Connect to TrueData WebSocket."""
        try:
            from truedata_ws.websocket.TD import TD
            self.td = TD(self.username, self.password, live_port=self.port)
            time.sleep(2)
            self._connected = True
            logger.info(f"[TrueData] Connected: user={self.username} port={self.port}")
            return True
        except Exception as e:
            logger.error(f"[TrueData] Connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Disconnect from TrueData."""
        if self.td:
            try:
                self.td.disconnect()
            except Exception:
                pass
        self._connected = False
        logger.info("[TrueData] Disconnected")

    def _td_symbol(self, symbol):
        """Convert internal symbol to TrueData symbol."""
        return SYMBOL_MAP.get(symbol, symbol)

    def _internal_symbol(self, td_symbol):
        """Convert TrueData symbol to internal symbol."""
        return REVERSE_SYMBOL_MAP.get(td_symbol, td_symbol)

    def get_spot(self, symbol):
        """Get spot price for a symbol. Returns float or None."""
        if not self._connected:
            return None

        td_sym = self._td_symbol(symbol)

        # Check cache
        with self._lock:
            cached = self._spot_cache.get(symbol)
            if cached and (time.time() - cached.get('_time', 0)) < self.SPOT_CACHE_TTL:
                return cached.get('ltp')

        try:
            # Start live data if not already subscribed
            if td_sym not in self._active_symbols:
                self.td.start_live_data([td_sym])
                self._active_symbols.add(td_sym)
                time.sleep(1)

            # Read touchline data
            for req_id, data in self.td.touchline_data.items():
                if data and data.symbol == td_sym and data.ltp:
                    with self._lock:
                        self._spot_cache[symbol] = {
                            'ltp': data.ltp,
                            'open': data.open,
                            'high': data.high,
                            'low': data.low,
                            'prev_close': data.prev_close,
                            'oi': getattr(data, 'oi', 0),
                            '_time': time.time(),
                        }
                    return data.ltp
        except Exception as e:
            logger.debug(f"[TrueData] Spot fetch failed for {symbol}: {e}")

        return None

    def get_option_chain(self, symbol, expiry_dt=None, chain_length=10):
        """Get option chain in the same format as market_data_pipeline.

        Returns:
            dict: {'CE': [{strike, ltp, oi, iv, volume, ...}], 'PE': [...], 'expiry': str, '_fetch_time': datetime}
            or None if unavailable.
        """
        if not self._connected:
            return None

        td_sym = self._td_symbol(symbol)

        # Check cache
        with self._lock:
            cached = self._chain_cache.get(symbol)
            if cached and (time.time() - cached.get('_cache_time', 0)) < self.CHAIN_CACHE_TTL:
                return cached

        # Determine expiry if not provided
        if expiry_dt is None:
            expiry_dt = self._get_nearest_expiry(symbol)
            if expiry_dt is None:
                return None

        try:
            chain_obj = self.td.start_option_chain(td_sym, expiry_dt, chain_length=chain_length)
            time.sleep(3)  # Wait for chain data to populate

            df = chain_obj.chain_dataframe
            if df is None or df.empty:
                logger.debug(f"[TrueData] Empty chain for {symbol}")
                return None

            # Convert DataFrame to our standard format
            ce_list = []
            pe_list = []

            for _, row in df.iterrows():
                strike = float(row.get('strike_price', row.get('Strike', 0)))
                if strike <= 0:
                    continue

                # CE side
                ce_ltp = float(row.get('call_ltp', row.get('Call LTP', 0)) or 0)
                ce_oi = float(row.get('call_oi', row.get('Call OI', 0)) or 0)
                ce_vol = float(row.get('call_volume', row.get('Call Volume', 0)) or 0)
                ce_iv = float(row.get('call_iv', row.get('Call IV', 0)) or 0)
                if ce_ltp > 0:
                    ce_list.append({
                        'strike': strike,
                        'ltp': ce_ltp,
                        'oi': ce_oi,
                        'volume': ce_vol,
                        'iv': ce_iv / 100 if ce_iv > 1 else ce_iv,
                    })

                # PE side
                pe_ltp = float(row.get('put_ltp', row.get('Put LTP', 0)) or 0)
                pe_oi = float(row.get('put_oi', row.get('Put OI', 0)) or 0)
                pe_vol = float(row.get('put_volume', row.get('Put Volume', 0)) or 0)
                pe_iv = float(row.get('put_iv', row.get('Put IV', 0)) or 0)
                if pe_ltp > 0:
                    pe_list.append({
                        'strike': strike,
                        'ltp': pe_ltp,
                        'oi': pe_oi,
                        'volume': pe_vol,
                        'iv': pe_iv / 100 if pe_iv > 1 else pe_iv,
                    })

            if not ce_list and not pe_list:
                return None

            result = {
                'CE': ce_list,
                'PE': pe_list,
                'expiry': str(expiry_dt),
                '_fetch_time': datetime.now(),
                '_cache_time': time.time(),
            }

            with self._lock:
                self._chain_cache[symbol] = result

            logger.info(f"[TrueData] Chain: {symbol} {len(ce_list)} CE + {len(pe_list)} PE strikes")
            return result

        except Exception as e:
            logger.debug(f"[TrueData] Chain fetch failed for {symbol}: {e}")
            return None

    def get_option_ltp(self, symbol, strike, opt_type, expiry_dt=None):
        """Get LTP for a specific option contract.

        Args:
            symbol: Internal symbol (NIFTY, BANKNIFTY, etc.)
            strike: Strike price
            opt_type: 'CE' or 'PE'
            expiry_dt: Expiry date or None for nearest

        Returns:
            float: LTP or 0 if unavailable
        """
        # First try from cached chain
        chain = self.get_option_chain(symbol, expiry_dt)
        if chain:
            contracts = chain.get(opt_type, [])
            for c in contracts:
                if c['strike'] == strike and c.get('ltp', 0) > 0:
                    return c['ltp']

        # Direct symbol lookup (for specific contracts)
        if not self._connected:
            return 0

        try:
            option_sym = self._build_option_symbol(symbol, strike, opt_type, expiry_dt)
            if option_sym:
                req_ids = self.td.start_live_data([option_sym])
                time.sleep(2)
                for req_id in req_ids:
                    data = self.td.touchline_data.get(req_id)
                    if data and data.ltp and data.ltp > 0:
                        return data.ltp
        except Exception as e:
            logger.debug(f"[TrueData] Option LTP failed for {symbol} {strike}{opt_type}: {e}")

        return 0

    def _get_nearest_expiry(self, symbol):
        """Get nearest expiry date for a symbol."""
        today = datetime.now().date()
        weekday = today.weekday()

        if symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            # Weekly expiry on Thursday (3)
            days_until_thu = (3 - weekday) % 7
            if days_until_thu == 0 and datetime.now().hour >= 15:
                days_until_thu = 7
            expiry = today + timedelta(days=days_until_thu)
            return expiry
        elif symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM'):
            # MCX monthly expiry — approximate last working day of month
            if today.day > 25:
                # Next month
                if today.month == 12:
                    expiry = datetime(today.year + 1, 1, 25).date()
                else:
                    expiry = datetime(today.year, today.month + 1, 25).date()
            else:
                expiry = datetime(today.year, today.month, 25).date()
            return expiry
        else:
            # Stock F&O — last Thursday of month
            if today.month == 12:
                next_month_start = datetime(today.year + 1, 1, 1).date()
            else:
                next_month_start = datetime(today.year, today.month + 1, 1).date()
            last_day = next_month_start - timedelta(days=1)
            # Find last Thursday
            while last_day.weekday() != 3:
                last_day -= timedelta(days=1)
            if last_day <= today:
                # Already past this month's expiry, get next month
                if next_month_start.month == 12:
                    nm2_start = datetime(next_month_start.year + 1, 1, 1).date()
                else:
                    nm2_start = datetime(next_month_start.year, next_month_start.month + 1, 1).date()
                last_day = nm2_start - timedelta(days=1)
                while last_day.weekday() != 3:
                    last_day -= timedelta(days=1)
            return last_day

    def _build_option_symbol(self, symbol, strike, opt_type, expiry_dt=None):
        """Build TrueData option symbol name.

        Format: NIFTY26MAR23500CE, GOLDM260326159000CE
        """
        if expiry_dt is None:
            expiry_dt = self._get_nearest_expiry(symbol)
        if expiry_dt is None:
            return None

        td_sym = self._td_symbol(symbol)
        # Remove suffix for option symbols
        base = td_sym.replace('-I', '').replace(' 50', '').replace(' BANK', ' BANK')

        # Format depends on segment
        if symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            # NSE F&O format: NIFTY2632023500CE (YYMDDStrikeCE)
            exp_str = expiry_dt.strftime('%y%b%d').upper()  # e.g., 26MAR20
            return f"{symbol}{exp_str}{int(strike)}{opt_type}"
        elif symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM'):
            # MCX format: GOLDM260326159000CE
            exp_str = expiry_dt.strftime('%y%m%d')
            return f"{symbol}{exp_str}{int(strike)}{opt_type}"
        else:
            # Stock F&O
            exp_str = expiry_dt.strftime('%y%b%d').upper()
            return f"{symbol}{exp_str}{int(strike)}{opt_type}"

    @property
    def is_connected(self):
        return self._connected
