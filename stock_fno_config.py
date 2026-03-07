"""Stock F&O Configuration Database — Central config for all NSE F&O stocks.
Manages lot sizes, strike intervals, tokens, liquidity tiers.
Auto-refreshes from Angel One instruments master.

Usage:
    config = StockFnOConfig()
    config.refresh_from_instruments(angel_data_fetcher)  # optional, auto-discovers all F&O stocks

    lot = config.get_lot_size('SBIN')
    si  = config.get_strike_interval('SBIN')
    score = config.compute_stock_score('SBIN', cpr_width=0.12)
"""

import os
import json
import logging
import time
import requests
import pandas as pd
from datetime import datetime

logger = logging.getLogger('StockFnOConfig')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'stock_fno_master.json')

# Instrument master URLs (same as angel_data_fetcher.py)
INSTRUMENT_MASTER_URLS = [
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
]

# ====================================================================
# LIQUIDITY TIERS — based on average daily option volume / OI
# ====================================================================
LIQUIDITY_VERY_HIGH = 'VERY_HIGH'  # Top 15 most liquid: SBIN, RELIANCE, HDFCBANK, etc.
LIQUIDITY_HIGH = 'HIGH'            # Next 30 liquid stocks
LIQUIDITY_MEDIUM = 'MEDIUM'        # Tradeable with wider spreads
LIQUIDITY_LOW = 'LOW'              # Avoid — wide bid-ask spreads

# Ordered list for comparison
_LIQUIDITY_RANK = {
    LIQUIDITY_VERY_HIGH: 4,
    LIQUIDITY_HIGH: 3,
    LIQUIDITY_MEDIUM: 2,
    LIQUIDITY_LOW: 1,
}

# ====================================================================
# PRE-CONFIGURED TIER 1 STOCKS — always trade if narrow CPR
# ====================================================================
TIER1_STOCKS = {
    'CIPLA': {'lot_size': 375, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'PHARMA'},
    'ASIANPAINT': {'lot_size': 250, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'FMCG'},
    'TITAN': {'lot_size': 175, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'CONSUMER'},
    'LT': {'lot_size': 175, 'strike_interval': 50, 'liquidity': 'HIGH', 'sector': 'INFRA'},
    'CGPOWER': {'lot_size': 850, 'strike_interval': 10, 'liquidity': 'MEDIUM', 'sector': 'POWER'},
}

# ====================================================================
# TIER 2 STOCKS — trade when Tier 1 has no narrow CPR
# ====================================================================
TIER2_STOCKS = {
    'HDFCBANK': {'lot_size': 550, 'strike_interval': 25, 'liquidity': 'VERY_HIGH', 'sector': 'BANKING'},
    'ITC': {'lot_size': 1600, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'FMCG'},
    'DLF': {'lot_size': 825, 'strike_interval': 10, 'liquidity': 'HIGH', 'sector': 'REALESTATE'},
    'INFY': {'lot_size': 400, 'strike_interval': 25, 'liquidity': 'VERY_HIGH', 'sector': 'IT'},
    'TATAPOWER': {'lot_size': 1450, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'POWER'},
    'SBIN': {'lot_size': 750, 'strike_interval': 10, 'liquidity': 'VERY_HIGH', 'sector': 'BANKING'},
    'RELIANCE': {'lot_size': 500, 'strike_interval': 20, 'liquidity': 'VERY_HIGH', 'sector': 'CONGLOMERATE'},
    'BAJFINANCE': {'lot_size': 750, 'strike_interval': 25, 'liquidity': 'VERY_HIGH', 'sector': 'NBFC'},
}

# ====================================================================
# TIER 3 — additional popular F&O stocks (for full scanner)
# ====================================================================
TIER3_STOCKS = {
    'AXISBANK': {'lot_size': 625, 'strike_interval': 25, 'liquidity': 'VERY_HIGH', 'sector': 'BANKING'},
    'ICICIBANK': {'lot_size': 700, 'strike_interval': 25, 'liquidity': 'VERY_HIGH', 'sector': 'BANKING'},
    'KOTAKBANK': {'lot_size': 2000, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'BANKING'},
    'MARUTI': {'lot_size': 50, 'strike_interval': 100, 'liquidity': 'HIGH', 'sector': 'AUTO'},
    'TATAMOTORS': {'lot_size': 575, 'strike_interval': 10, 'liquidity': 'HIGH', 'sector': 'AUTO'},
    'M&M': {'lot_size': 350, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'AUTO'},
    'SUNPHARMA': {'lot_size': 350, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'PHARMA'},
    'BHARTIARTL': {'lot_size': 475, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'TELECOM'},
    'TCS': {'lot_size': 175, 'strike_interval': 50, 'liquidity': 'HIGH', 'sector': 'IT'},
    'HCLTECH': {'lot_size': 350, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'IT'},
    'NTPC': {'lot_size': 1500, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'POWER'},
    'COALINDIA': {'lot_size': 1350, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'MINING'},
    'BPCL': {'lot_size': 1975, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'OIL'},
    'HINDPETRO': {'lot_size': 2025, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'OIL'},
    'INDUSINDBK': {'lot_size': 700, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'BANKING'},
    'TATASTEEL': {'lot_size': 1400, 'strike_interval': 2.5, 'liquidity': 'HIGH', 'sector': 'METALS'},
    'HINDALCO': {'lot_size': 1075, 'strike_interval': 10, 'liquidity': 'HIGH', 'sector': 'METALS'},
    'JSWSTEEL': {'lot_size': 600, 'strike_interval': 10, 'liquidity': 'HIGH', 'sector': 'METALS'},
    'ADANIENT': {'lot_size': 500, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'CONGLOMERATE'},
    'ADANIPORTS': {'lot_size': 500, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'INFRA'},
    'DIVISLAB': {'lot_size': 200, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'PHARMA'},
    'LUPIN': {'lot_size': 425, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'PHARMA'},
    'WIPRO': {'lot_size': 1500, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'IT'},
    'TECHM': {'lot_size': 600, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'IT'},
    'POWERGRID': {'lot_size': 2700, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'POWER'},
    'HAL': {'lot_size': 150, 'strike_interval': 50, 'liquidity': 'HIGH', 'sector': 'DEFENCE'},
    'BEL': {'lot_size': 1425, 'strike_interval': 5, 'liquidity': 'HIGH', 'sector': 'DEFENCE'},
    'BRITANNIA': {'lot_size': 100, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'FMCG'},
    'NESTLEIND': {'lot_size': 50, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'FMCG'},
    'HINDUNILVR': {'lot_size': 300, 'strike_interval': 25, 'liquidity': 'HIGH', 'sector': 'FMCG'},
    'EICHERMOT': {'lot_size': 150, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'AUTO'},
    'HEROMOTOCO': {'lot_size': 150, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'AUTO'},
    'BAJAJ-AUTO': {'lot_size': 75, 'strike_interval': 100, 'liquidity': 'MEDIUM', 'sector': 'AUTO'},
    'TVSMOTOR': {'lot_size': 350, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'AUTO'},
    'UPL': {'lot_size': 1355, 'strike_interval': 5, 'liquidity': 'MEDIUM', 'sector': 'AGRI'},
    'GRASIM': {'lot_size': 275, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'CEMENT'},
    'ULTRACEMCO': {'lot_size': 100, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'CEMENT'},
    'SHREECEM': {'lot_size': 25, 'strike_interval': 100, 'liquidity': 'LOW', 'sector': 'CEMENT'},
    'TRENT': {'lot_size': 100, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'RETAIL'},
    'DMART': {'lot_size': 150, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'RETAIL'},
    'SIEMENS': {'lot_size': 175, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'INDUSTRIAL'},
    'ABB': {'lot_size': 250, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'INDUSTRIAL'},
    'PIDILITIND': {'lot_size': 250, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'CHEMICALS'},
    'SRF': {'lot_size': 375, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'CHEMICALS'},
    'HAVELLS': {'lot_size': 500, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'ELECTRICAL'},
    'POLYCAB': {'lot_size': 150, 'strike_interval': 50, 'liquidity': 'MEDIUM', 'sector': 'ELECTRICAL'},
    'CONCOR': {'lot_size': 1250, 'strike_interval': 10, 'liquidity': 'MEDIUM', 'sector': 'LOGISTICS'},
    'BHARATFORG': {'lot_size': 500, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'INDUSTRIAL'},
    'GLENMARK': {'lot_size': 375, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'PHARMA'},
    'INDIANB': {'lot_size': 1000, 'strike_interval': 10, 'liquidity': 'MEDIUM', 'sector': 'BANKING'},
    'GODREJPROP': {'lot_size': 325, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'REALESTATE'},
    'OBEROIRLTY': {'lot_size': 400, 'strike_interval': 25, 'liquidity': 'MEDIUM', 'sector': 'REALESTATE'},
}


class StockFnOConfig:
    """Central F&O stock configuration database.

    Provides lot sizes, strike intervals, Angel tokens, liquidity tiers,
    and stock suitability scoring for the narrow CPR stock options strategy.

    The config is bootstrapped from pre-configured tiers (TIER1/2/3) and
    can auto-refresh from the Angel One instruments master to discover all
    ~180+ NSE F&O stocks dynamically.
    """

    def __init__(self, auto_load_cache=True):
        self.stocks = {}           # {symbol: {lot_size, strike_interval, ...}}
        self._token_map = {}       # {symbol: angel_token}
        self._last_refresh = None  # datetime of last instruments refresh
        self._load_preconfig()
        if auto_load_cache:
            self._load_cached_config()

    # ================================================================
    # INITIALIZATION
    # ================================================================

    def _load_preconfig(self):
        """Load pre-configured tier stocks into the internal store."""
        all_tiers = {**TIER1_STOCKS, **TIER2_STOCKS, **TIER3_STOCKS}
        for symbol, config in all_tiers.items():
            self.stocks[symbol] = {
                'symbol': symbol,
                'lot_size': config['lot_size'],
                'strike_interval': config['strike_interval'],
                'liquidity': config['liquidity'],
                'sector': config['sector'],
                'exchange': 'NFO',
                'spot_exchange': 'NSE',
                'token': None,       # filled from instruments master
                'spot_token': None,  # NSE cash segment token
                'tier': (1 if symbol in TIER1_STOCKS
                         else (2 if symbol in TIER2_STOCKS else 3)),
                'source': 'preconfig',
            }

    def _load_cached_config(self):
        """Load cached config from JSON (includes auto-discovered stocks + tokens).

        Merges with pre-configured data: cached values for token, lot_size, etc.
        override preconfig only if they are non-null. Any new symbols discovered
        by the instruments master scan are added as tier=4.
        """
        if not os.path.exists(CONFIG_PATH):
            logger.debug("No cached stock_fno_master.json found — using preconfig only")
            return

        try:
            with open(CONFIG_PATH, 'r') as f:
                cached = json.load(f)

            meta = cached.get('_meta', {})
            self._last_refresh = meta.get('last_refresh')
            stock_data = cached.get('stocks', {})

            for symbol, data in stock_data.items():
                if symbol in self.stocks:
                    # Merge: update token + any non-null fields from cache
                    existing = self.stocks[symbol]
                    if data.get('token'):
                        existing['token'] = data['token']
                    if data.get('spot_token'):
                        existing['spot_token'] = data['spot_token']
                    # Update lot_size / strike_interval if cache is from instruments master
                    if data.get('source') == 'instruments_master':
                        if data.get('lot_size'):
                            existing['lot_size'] = data['lot_size']
                        if data.get('strike_interval'):
                            existing['strike_interval'] = data['strike_interval']
                else:
                    # New symbol discovered from instruments master
                    self.stocks[symbol] = {
                        'symbol': symbol,
                        'lot_size': data.get('lot_size', 0),
                        'strike_interval': data.get('strike_interval', 0),
                        'liquidity': data.get('liquidity', LIQUIDITY_LOW),
                        'sector': data.get('sector', 'UNKNOWN'),
                        'exchange': 'NFO',
                        'spot_exchange': 'NSE',
                        'token': data.get('token'),
                        'spot_token': data.get('spot_token'),
                        'tier': data.get('tier', 4),
                        'source': data.get('source', 'cache'),
                    }

            # Rebuild token map
            for symbol, info in self.stocks.items():
                if info.get('token'):
                    self._token_map[symbol] = info['token']

            logger.info(f"Loaded {len(stock_data)} stocks from cache "
                        f"(last refresh: {self._last_refresh})")

        except Exception as e:
            logger.warning(f"Failed to load cached config: {e}")

    def _save_cached_config(self):
        """Persist current config to JSON for next startup."""
        os.makedirs(DATA_DIR, exist_ok=True)

        payload = {
            '_meta': {
                'last_refresh': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_stocks': len(self.stocks),
                'version': '1.0',
            },
            'stocks': self.stocks,
        }

        try:
            with open(CONFIG_PATH, 'w') as f:
                json.dump(payload, f, indent=2, default=str)
            logger.info(f"Saved {len(self.stocks)} stocks to {CONFIG_PATH}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    # ================================================================
    # INSTRUMENTS MASTER REFRESH
    # ================================================================

    def refresh_from_instruments(self, angel_connection=None, force=False):
        """Parse Angel instruments master to discover all F&O stocks + tokens.

        Can work in two modes:
        1. With angel_connection: uses its instruments_df if already downloaded.
        2. Without angel_connection: downloads instruments master directly.

        Steps:
        1. Load instruments master (DataFrame with columns:
           token, symbol, name, expiry, strike, lotsize, instrumenttype, exch_seg)
        2. Filter for exch_seg == 'NFO' and instrumenttype == 'OPTSTK'
        3. Extract unique symbols with lot_size, strike_interval
        4. Map each symbol to its NSE cash token
        5. Save to CONFIG_PATH for caching
        """
        # Skip if refreshed recently (< 12 hours) unless forced
        if not force and self._last_refresh:
            try:
                last_dt = datetime.strptime(self._last_refresh, '%Y-%m-%d %H:%M:%S')
                age_hours = (datetime.now() - last_dt).total_seconds() / 3600
                if age_hours < 12:
                    logger.info(f"Instruments master is {age_hours:.1f}h old, skipping refresh")
                    return True
            except (ValueError, TypeError):
                pass

        logger.info("Refreshing F&O stock config from instruments master...")
        df = self._get_instruments_df(angel_connection)
        if df is None or df.empty:
            logger.error("Could not load instruments master — refresh failed")
            return False

        # ----------------------------------------------------------
        # 1. Filter NFO OPTSTK entries (stock options)
        # ----------------------------------------------------------
        nfo_opts = df[
            (df['exch_seg'] == 'NFO') &
            (df['instrumenttype'] == 'OPTSTK')
        ].copy()

        if nfo_opts.empty:
            logger.warning("No OPTSTK entries found in instruments master")
            return False

        # Ensure numeric types
        nfo_opts['strike'] = pd.to_numeric(nfo_opts['strike'], errors='coerce') / 100
        nfo_opts['lotsize'] = pd.to_numeric(nfo_opts['lotsize'], errors='coerce')

        logger.info(f"Found {len(nfo_opts)} OPTSTK entries across "
                    f"{nfo_opts['name'].nunique()} unique symbols")

        # ----------------------------------------------------------
        # 2. Extract unique symbols + lot sizes
        # ----------------------------------------------------------
        symbols_discovered = set()

        for symbol_name in nfo_opts['name'].unique():
            sym_data = nfo_opts[nfo_opts['name'] == symbol_name]

            # Lot size: take the mode (most common) value
            lot_sizes = sym_data['lotsize'].dropna()
            if lot_sizes.empty:
                continue
            lot_size = int(lot_sizes.mode().iloc[0])

            # Strike interval: compute from sorted unique strikes
            strike_interval = self._compute_strike_interval(sym_data)

            symbols_discovered.add(symbol_name)

            if symbol_name in self.stocks:
                # Update existing entry with fresh data
                self.stocks[symbol_name]['lot_size'] = lot_size
                if strike_interval > 0:
                    self.stocks[symbol_name]['strike_interval'] = strike_interval
                self.stocks[symbol_name]['source'] = 'instruments_master'
            else:
                # New symbol — add as tier 4 with LOW liquidity default
                self.stocks[symbol_name] = {
                    'symbol': symbol_name,
                    'lot_size': lot_size,
                    'strike_interval': strike_interval if strike_interval > 0 else 5,
                    'liquidity': LIQUIDITY_LOW,
                    'sector': 'UNKNOWN',
                    'exchange': 'NFO',
                    'spot_exchange': 'NSE',
                    'token': None,
                    'spot_token': None,
                    'tier': 4,
                    'source': 'instruments_master',
                }

        # ----------------------------------------------------------
        # 3. Map NSE cash segment tokens
        # ----------------------------------------------------------
        nse_cash = df[
            (df['exch_seg'] == 'NSE') &
            (df['instrumenttype'].isin(['', 'EQ', 'EQUITY']))
        ] if 'instrumenttype' in df.columns else df[df['exch_seg'] == 'NSE']

        # Also try: symbol matches directly (e.g., SBIN-EQ or just SBIN)
        for symbol_name in symbols_discovered:
            # Try exact name match in NSE cash
            cash_rows = nse_cash[nse_cash['name'] == symbol_name]
            if cash_rows.empty:
                # Try symbol column (format might be "SBIN-EQ")
                cash_rows = nse_cash[
                    nse_cash['symbol'].str.replace('-EQ', '', regex=False) == symbol_name
                ] if 'symbol' in nse_cash.columns else pd.DataFrame()

            if not cash_rows.empty:
                spot_token = str(cash_rows.iloc[0].get('token', ''))
                if spot_token and symbol_name in self.stocks:
                    self.stocks[symbol_name]['spot_token'] = spot_token
                    self._token_map[symbol_name] = spot_token

            # Also grab an NFO option token for reference
            opt_rows = nfo_opts[nfo_opts['name'] == symbol_name]
            if not opt_rows.empty and symbol_name in self.stocks:
                self.stocks[symbol_name]['token'] = str(opt_rows.iloc[0].get('token', ''))

        # ----------------------------------------------------------
        # 4. Save to cache
        # ----------------------------------------------------------
        self._last_refresh = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._save_cached_config()

        logger.info(f"Refresh complete: {len(symbols_discovered)} F&O symbols, "
                    f"{len(self.stocks)} total in config")
        return True

    def _get_instruments_df(self, angel_connection=None):
        """Get instruments DataFrame — from angel_connection or direct download.

        Args:
            angel_connection: An AngelDataFetcher instance (has instruments_df attribute)
                              or a SmartConnect instance (has no instruments_df).
        """
        # Option A: use existing angel_connection's cached instruments
        if angel_connection is not None:
            # AngelDataFetcher style
            if hasattr(angel_connection, 'instruments_df') and angel_connection.instruments_df is not None:
                return angel_connection.instruments_df

            # Try calling download_instruments if available
            if hasattr(angel_connection, 'download_instruments'):
                try:
                    df = angel_connection.download_instruments()
                    if df is not None:
                        return df
                except Exception as e:
                    logger.warning(f"angel_connection.download_instruments() failed: {e}")

        # Option B: check for cached instrument_master.csv
        cache_file = os.path.join(DATA_DIR, 'instrument_master.csv')
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 24 * 3600:  # Use if less than 24h old
                try:
                    df = pd.read_csv(cache_file)
                    logger.info(f"Using cached instrument_master.csv ({age/3600:.1f}h old)")
                    return df
                except Exception as e:
                    logger.warning(f"Failed to read cached instruments: {e}")

        # Option C: download directly
        return self._download_instruments_master()

    def _download_instruments_master(self):
        """Download instruments master JSON from Angel One."""
        for url in INSTRUMENT_MASTER_URLS:
            try:
                logger.info(f"Downloading instruments master from {url}...")
                resp = requests.get(url, timeout=90)
                if resp.status_code == 200:
                    instruments = resp.json()
                    df = pd.DataFrame(instruments)
                    df['expiry'] = pd.to_datetime(
                        df['expiry'], format='mixed', dayfirst=True, errors='coerce'
                    )
                    logger.info(f"Downloaded {len(df)} instruments")
                    return df
            except Exception as e:
                logger.warning(f"Failed to download from {url}: {e}")
                continue

        logger.error("Could not download instruments master from any URL")
        return None

    @staticmethod
    def _compute_strike_interval(sym_data):
        """Compute strike interval from a DataFrame of option entries for one symbol.

        Uses the most recent expiry and finds the minimum gap between
        consecutive sorted strikes. This handles irregular strike listings.
        """
        # Use the nearest expiry for cleaner strike grid
        expiries = sym_data['expiry'].dropna().unique()
        if len(expiries) == 0:
            return 0

        try:
            nearest_expiry = sorted(expiries)[0]
            expiry_data = sym_data[sym_data['expiry'] == nearest_expiry]
        except (TypeError, ValueError):
            expiry_data = sym_data

        strikes = expiry_data['strike'].dropna().sort_values().unique()
        if len(strikes) < 2:
            return 0

        # Compute differences between consecutive strikes
        diffs = []
        for i in range(1, len(strikes)):
            diff = round(strikes[i] - strikes[i - 1], 2)
            if diff > 0:
                diffs.append(diff)

        if not diffs:
            return 0

        # The strike interval is the most common (mode) difference
        from collections import Counter
        diff_counts = Counter(diffs)
        interval = diff_counts.most_common(1)[0][0]

        # Sanity check: interval should be reasonable (0.5 to 500)
        if 0.5 <= interval <= 500:
            return interval

        # Fallback to minimum diff
        min_diff = min(diffs)
        return min_diff if 0.5 <= min_diff <= 500 else 0

    # ================================================================
    # GETTERS — individual stock attributes
    # ================================================================

    def get_lot_size(self, symbol):
        """Return lot size for a symbol, or None if not found."""
        info = self.stocks.get(symbol)
        return info['lot_size'] if info else None

    def get_strike_interval(self, symbol):
        """Return strike interval for a symbol, or None if not found."""
        info = self.stocks.get(symbol)
        return info['strike_interval'] if info else None

    def get_token(self, symbol):
        """Return the Angel One token (NFO option segment) for a symbol."""
        info = self.stocks.get(symbol)
        return info.get('token') if info else None

    def get_spot_token(self, symbol):
        """Return the NSE cash segment token for a symbol."""
        info = self.stocks.get(symbol)
        return info.get('spot_token') if info else None

    def get_spot_exchange(self, symbol):
        """Return spot exchange for a symbol (always 'NSE' for F&O stocks)."""
        info = self.stocks.get(symbol)
        return info.get('spot_exchange', 'NSE') if info else 'NSE'

    def get_liquidity_tier(self, symbol):
        """Return liquidity tier string: VERY_HIGH / HIGH / MEDIUM / LOW."""
        info = self.stocks.get(symbol)
        return info.get('liquidity', LIQUIDITY_LOW) if info else LIQUIDITY_LOW

    def get_sector(self, symbol):
        """Return sector classification for a symbol."""
        info = self.stocks.get(symbol)
        return info.get('sector', 'UNKNOWN') if info else 'UNKNOWN'

    def get_tier(self, symbol):
        """Return tier number (1-4) for a symbol."""
        info = self.stocks.get(symbol)
        return info.get('tier', 4) if info else 4

    def get_stock_info(self, symbol):
        """Return full info dict for a symbol, or None."""
        return self.stocks.get(symbol)

    # ================================================================
    # SYMBOL LISTS
    # ================================================================

    def get_all_fno_symbols(self):
        """Return sorted list of ALL known F&O symbols."""
        return sorted(self.stocks.keys())

    def get_tier1_symbols(self):
        """Return sorted list of Tier 1 symbols."""
        return sorted(s for s, info in self.stocks.items() if info.get('tier') == 1)

    def get_tier2_symbols(self):
        """Return sorted list of Tier 2 symbols."""
        return sorted(s for s, info in self.stocks.items() if info.get('tier') == 2)

    def get_tier3_symbols(self):
        """Return sorted list of Tier 3 symbols."""
        return sorted(s for s, info in self.stocks.items() if info.get('tier') == 3)

    def get_tradeable_symbols(self):
        """Return symbols suitable for trading: Tier 1 + Tier 2 + liquid Tier 3.

        Tier 3 stocks are included only if their liquidity is HIGH or VERY_HIGH.
        Tier 4 (auto-discovered) are excluded unless manually upgraded.
        """
        tradeable = []
        for symbol, info in self.stocks.items():
            tier = info.get('tier', 4)
            liquidity = info.get('liquidity', LIQUIDITY_LOW)

            if tier <= 2:
                # Tier 1 and 2 are always tradeable
                tradeable.append(symbol)
            elif tier == 3:
                # Tier 3 only if sufficiently liquid
                if _LIQUIDITY_RANK.get(liquidity, 0) >= _LIQUIDITY_RANK[LIQUIDITY_HIGH]:
                    tradeable.append(symbol)

        return sorted(tradeable)

    def is_tradeable(self, symbol):
        """Check if a symbol is in the tradeable set."""
        info = self.stocks.get(symbol)
        if not info:
            return False

        tier = info.get('tier', 4)
        liquidity = info.get('liquidity', LIQUIDITY_LOW)

        if tier <= 2:
            return True
        if tier == 3 and _LIQUIDITY_RANK.get(liquidity, 0) >= _LIQUIDITY_RANK[LIQUIDITY_HIGH]:
            return True
        return False

    def get_symbols_by_sector(self, sector):
        """Return all symbols in a given sector."""
        return sorted(
            s for s, info in self.stocks.items()
            if info.get('sector', '').upper() == sector.upper()
        )

    def get_symbols_by_liquidity(self, min_liquidity=LIQUIDITY_HIGH):
        """Return symbols with liquidity >= min_liquidity."""
        min_rank = _LIQUIDITY_RANK.get(min_liquidity, 0)
        return sorted(
            s for s, info in self.stocks.items()
            if _LIQUIDITY_RANK.get(info.get('liquidity', LIQUIDITY_LOW), 0) >= min_rank
        )

    # ================================================================
    # MARGIN ESTIMATION
    # ================================================================

    def get_margin_estimate(self, symbol, spot_price):
        """Estimate margin required for a single lot of stock options.

        For stock option selling, SPAN margin is typically 15-20% of notional.
        For buying, margin = premium * lot_size (no additional margin).

        Args:
            symbol: Stock symbol
            spot_price: Current spot price of the stock

        Returns:
            dict with sell_margin and buy_estimate, or None if symbol unknown
        """
        info = self.stocks.get(symbol)
        if not info:
            return None

        lot_size = info['lot_size']
        notional = spot_price * lot_size

        # SPAN margin for selling: ~15-20% of notional
        # Higher for volatile / less liquid stocks
        liquidity = info.get('liquidity', LIQUIDITY_LOW)
        if liquidity == LIQUIDITY_VERY_HIGH:
            margin_pct = 0.15
        elif liquidity == LIQUIDITY_HIGH:
            margin_pct = 0.17
        elif liquidity == LIQUIDITY_MEDIUM:
            margin_pct = 0.19
        else:
            margin_pct = 0.22

        sell_margin = round(notional * margin_pct, 0)

        # For buying: estimate premium ~ 1-2% of spot for ATM
        atm_premium_estimate = round(spot_price * 0.015 * lot_size, 0)

        return {
            'symbol': symbol,
            'lot_size': lot_size,
            'notional': round(notional, 0),
            'sell_margin': sell_margin,
            'buy_estimate': atm_premium_estimate,
            'margin_pct': margin_pct,
        }

    # ================================================================
    # STOCK SUITABILITY SCORING
    # ================================================================

    def compute_stock_score(self, symbol, cpr_width, recurring_days=1,
                            existing_sectors=None, available_capital=200000):
        """Score a stock 0-100 for suitability in narrow CPR option trading.

        Components (total = 100):
          CPR Narrowness  (0-25): How tight the CPR is (percentage of spot)
          Option Liquidity (0-25): Based on liquidity tier
          Capital Fit      (0-20): Can we afford a lot with available capital?
          Recurring CPR    (0-15): Has narrow CPR appeared multiple days?
          Sector Diversity (0-15): Is this a different sector from existing positions?

        Args:
            symbol: Stock symbol (must exist in config)
            cpr_width: CPR width as percentage of spot price
                       (e.g., 0.12 means CPR is 0.12% of spot)
            recurring_days: Number of consecutive days with narrow CPR (1 = today only)
            existing_sectors: set of sectors already in open positions
                              (for diversity bonus)
            available_capital: Capital available for new positions (Rs)

        Returns:
            dict with total_score, component scores, and details
        """
        info = self.stocks.get(symbol)
        if not info:
            return {
                'symbol': symbol,
                'total_score': 0,
                'reason': 'Symbol not found in F&O config',
                'tradeable': False,
            }

        if existing_sectors is None:
            existing_sectors = set()

        # ----- Component 1: CPR Narrowness (0-25) -----
        if cpr_width < 0.15:
            cpr_score = 25
            cpr_label = 'ULTRA_NARROW'
        elif cpr_width < 0.20:
            cpr_score = 20
            cpr_label = 'VERY_NARROW'
        elif cpr_width < 0.30:
            cpr_score = 15
            cpr_label = 'NARROW'
        elif cpr_width < 0.50:
            cpr_score = 10
            cpr_label = 'MODERATE'
        elif cpr_width < 0.80:
            cpr_score = 5
            cpr_label = 'WIDE'
        else:
            cpr_score = 0
            cpr_label = 'TOO_WIDE'

        # ----- Component 2: Option Liquidity (0-25) -----
        liquidity = info.get('liquidity', LIQUIDITY_LOW)
        liquidity_scores = {
            LIQUIDITY_VERY_HIGH: 25,
            LIQUIDITY_HIGH: 20,
            LIQUIDITY_MEDIUM: 15,
            LIQUIDITY_LOW: 5,
        }
        liquidity_score = liquidity_scores.get(liquidity, 5)

        # ----- Component 3: Capital Fit (0-20) -----
        # Estimate capital needed: assume ATM premium ~ 1.5% of a rough spot estimate
        # We use lot_size * strike_interval * 10 as a rough spot proxy if we don't
        # have the actual spot price. This is intentionally approximate.
        lot_size = info['lot_size']
        strike_interval = info['strike_interval']

        # Rough premium estimate: small lot + low strike_interval = cheaper
        # Use notional proxy: lot_size * strike_interval * 10 * 1.5%
        # This is a heuristic; real premium depends on IV, time, etc.
        rough_spot = strike_interval * 100  # Very rough estimate
        rough_premium_per_lot = rough_spot * 0.015 * lot_size

        if rough_premium_per_lot <= 0:
            capital_score = 10  # Neutral if we can't estimate
        elif rough_premium_per_lot <= available_capital * 0.10:
            capital_score = 20  # Easily affordable (< 10% of capital)
        elif rough_premium_per_lot <= available_capital * 0.20:
            capital_score = 15  # Affordable (< 20%)
        elif rough_premium_per_lot <= available_capital * 0.35:
            capital_score = 10  # Moderate fit (< 35%)
        elif rough_premium_per_lot <= available_capital * 0.50:
            capital_score = 5   # Tight fit (< 50%)
        else:
            capital_score = 0   # Too expensive for available capital

        # ----- Component 4: Recurring CPR (0-15) -----
        if recurring_days >= 3:
            recurring_score = 15
        elif recurring_days == 2:
            recurring_score = 10
        elif recurring_days == 1:
            recurring_score = 5
        else:
            recurring_score = 0

        # ----- Component 5: Sector Diversity (0-15) -----
        sector = info.get('sector', 'UNKNOWN')
        if not existing_sectors:
            # No existing positions — full diversity bonus
            diversity_score = 15
        elif sector not in existing_sectors:
            # Different sector — diversity bonus
            diversity_score = 15
        else:
            # Same sector as existing position — no bonus
            diversity_score = 0

        # ----- Total Score -----
        total_score = cpr_score + liquidity_score + capital_score + recurring_score + diversity_score

        # ----- Tradeable check -----
        tradeable = self.is_tradeable(symbol)

        return {
            'symbol': symbol,
            'total_score': total_score,
            'tradeable': tradeable,
            'tier': info.get('tier', 4),
            'lot_size': lot_size,
            'strike_interval': strike_interval,
            'sector': sector,
            'liquidity': liquidity,
            'components': {
                'cpr_narrowness': {'score': cpr_score, 'max': 25, 'label': cpr_label,
                                   'cpr_width_pct': round(cpr_width, 4)},
                'option_liquidity': {'score': liquidity_score, 'max': 25,
                                     'tier': liquidity},
                'capital_fit': {'score': capital_score, 'max': 20,
                                'est_premium_per_lot': round(rough_premium_per_lot, 0)},
                'recurring_cpr': {'score': recurring_score, 'max': 15,
                                  'days': recurring_days},
                'sector_diversity': {'score': diversity_score, 'max': 15,
                                     'sector': sector,
                                     'already_held': sector in existing_sectors},
            },
        }

    def rank_stocks_for_trading(self, candidates, available_capital=200000,
                                existing_sectors=None, min_score=40):
        """Rank a list of candidate stocks by suitability score.

        Args:
            candidates: list of dicts, each with keys:
                        'symbol', 'cpr_width' (pct), optional 'recurring_days'
            available_capital: Rs available for new positions
            existing_sectors: set of sectors already in open positions
            min_score: minimum score to include in results

        Returns:
            Sorted list of score dicts (highest score first), filtered by min_score.
        """
        if existing_sectors is None:
            existing_sectors = set()

        results = []
        for cand in candidates:
            symbol = cand.get('symbol', '')
            cpr_width = cand.get('cpr_width', 1.0)
            recurring_days = cand.get('recurring_days', 1)

            score_info = self.compute_stock_score(
                symbol=symbol,
                cpr_width=cpr_width,
                recurring_days=recurring_days,
                existing_sectors=existing_sectors,
                available_capital=available_capital,
            )

            if score_info['total_score'] >= min_score and score_info['tradeable']:
                results.append(score_info)

                # Update existing_sectors for subsequent diversity scoring
                # (later candidates get penalized for same sector)
                sector = score_info.get('sector', 'UNKNOWN')
                existing_sectors = existing_sectors | {sector}

        # Sort by total_score descending, then by tier ascending
        results.sort(key=lambda x: (-x['total_score'], x.get('tier', 4)))
        return results

    # ================================================================
    # UTILITY / DISPLAY
    # ================================================================

    def summary(self):
        """Return a summary string of the current config."""
        total = len(self.stocks)
        tier_counts = {}
        liquidity_counts = {}
        sector_counts = {}

        for info in self.stocks.values():
            tier = info.get('tier', 4)
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

            liq = info.get('liquidity', LIQUIDITY_LOW)
            liquidity_counts[liq] = liquidity_counts.get(liq, 0) + 1

            sec = info.get('sector', 'UNKNOWN')
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        lines = [
            f"Stock F&O Config: {total} stocks",
            f"  Tier 1 (always trade): {tier_counts.get(1, 0)}",
            f"  Tier 2 (secondary):    {tier_counts.get(2, 0)}",
            f"  Tier 3 (scanner):      {tier_counts.get(3, 0)}",
            f"  Tier 4 (discovered):   {tier_counts.get(4, 0)}",
            f"  Tradeable:             {len(self.get_tradeable_symbols())}",
            f"",
            f"  Liquidity: VERY_HIGH={liquidity_counts.get(LIQUIDITY_VERY_HIGH, 0)}, "
            f"HIGH={liquidity_counts.get(LIQUIDITY_HIGH, 0)}, "
            f"MEDIUM={liquidity_counts.get(LIQUIDITY_MEDIUM, 0)}, "
            f"LOW={liquidity_counts.get(LIQUIDITY_LOW, 0)}",
            f"",
            f"  Sectors: {', '.join(f'{k}({v})' for k, v in sorted(sector_counts.items()))}",
            f"",
            f"  Last refresh: {self._last_refresh or 'never'}",
        ]
        return '\n'.join(lines)

    def to_dict(self):
        """Export entire config as a plain dict (for JSON serialization)."""
        return {
            'meta': {
                'total_stocks': len(self.stocks),
                'tradeable': len(self.get_tradeable_symbols()),
                'last_refresh': self._last_refresh,
            },
            'stocks': dict(self.stocks),
        }

    def __repr__(self):
        return (f"<StockFnOConfig: {len(self.stocks)} stocks, "
                f"{len(self.get_tradeable_symbols())} tradeable>")

    def __len__(self):
        return len(self.stocks)

    def __contains__(self, symbol):
        return symbol in self.stocks


# ====================================================================
# MODULE-LEVEL CONVENIENCE
# ====================================================================

# Singleton instance (lazy-loaded)
_instance = None


def get_config():
    """Get the singleton StockFnOConfig instance."""
    global _instance
    if _instance is None:
        _instance = StockFnOConfig()
    return _instance


# ====================================================================
# CLI / MAIN
# ====================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    )

    config = StockFnOConfig()
    print(config.summary())
    print()

    # Example: score a few stocks
    test_cases = [
        {'symbol': 'SBIN', 'cpr_width': 0.12, 'recurring_days': 2},
        {'symbol': 'RELIANCE', 'cpr_width': 0.18, 'recurring_days': 1},
        {'symbol': 'CIPLA', 'cpr_width': 0.25, 'recurring_days': 3},
        {'symbol': 'HDFCBANK', 'cpr_width': 0.10, 'recurring_days': 1},
        {'symbol': 'SHREECEM', 'cpr_width': 0.15, 'recurring_days': 1},
    ]

    print("=== Stock Suitability Scoring ===")
    ranked = config.rank_stocks_for_trading(test_cases, available_capital=200000)
    for r in ranked:
        print(f"  {r['symbol']:12s} Score={r['total_score']:3d}  "
              f"Tier={r['tier']}  Liq={r['liquidity']:10s}  "
              f"Sector={r['sector']:12s}  "
              f"CPR={r['components']['cpr_narrowness']['label']}")

    print(f"\nAll tradeable symbols ({len(config.get_tradeable_symbols())}):")
    for i, sym in enumerate(config.get_tradeable_symbols()):
        info = config.get_stock_info(sym)
        print(f"  {sym:12s} Lot={info['lot_size']:5d}  "
              f"Strike={info['strike_interval']:6.1f}  "
              f"Liq={info['liquidity']:10s}  T{info['tier']}")
