"""
Live Market Data Logger — Saves real-time market data to CSV for historical backtesting.

v10.2e: Captures option chain snapshots, market indicators, and spot ticks
from NSE/BSE MarketDataPipeline and Angel WebSocket feed.

Thread-safe, append-mode, restart-safe.
Data stored in: data/live/YYYY-MM-DD/

Usage:
    logger = LiveDataLogger()
    logger.log_option_chain('NIFTY', chain_data, spot=24250.35, expiry='13 Mar 2026')
    logger.log_market_indicators('NIFTY', spot=24250.35, pcr=0.89, vix=13.5, oi_data={...})
    logger.log_spot_tick('NIFTY', ltp=24250.35)
"""

import os
import csv
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# Base directory for the project
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class LiveDataLogger:
    """Saves live market data to CSV files for historical backtesting.

    Thread-safe. Append-mode (safe for restart during market hours).
    Creates one directory per trading day under data/live/.
    """

    # CSV column definitions
    OPTION_CHAIN_HEADERS = [
        'timestamp', 'symbol', 'expiry', 'strike', 'type',
        'ltp', 'oi', 'oi_change', 'volume', 'iv', 'spot'
    ]

    MARKET_INDICATORS_HEADERS = [
        'timestamp', 'symbol', 'spot', 'pcr', 'vix',
        'oi_call', 'oi_put', 'oi_change_call', 'oi_change_put'
    ]

    SPOT_TICK_HEADERS = ['timestamp', 'symbol', 'ltp']

    def __init__(self, base_dir=None):
        """Initialize LiveDataLogger.

        Args:
            base_dir: Directory for live data files. Defaults to data/live/ under project root.
        """
        self._base_dir = base_dir or os.path.join(BASE_DIR, 'data', 'live')
        self._file_locks = {}           # {filepath: threading.Lock()}
        self._global_lock = threading.Lock()  # For creating new per-file locks
        self._last_spot_minute = {}     # {symbol: 'YYYY-MM-DD HH:MM'} for 1-min dedup
        self._last_chain_ts = {}        # {symbol: datetime} for chain dedup (2-min gap)
        self._enabled = True

        # Create base directory
        try:
            os.makedirs(self._base_dir, exist_ok=True)
            logger.info(f"[LiveDataLogger] Initialized — saving to {self._base_dir}")
        except Exception as e:
            logger.error(f"[LiveDataLogger] Failed to create base dir {self._base_dir}: {e}")
            self._enabled = False

    def _get_date_dir(self, date_str=None):
        """Get or create the date-specific directory.

        Args:
            date_str: 'YYYY-MM-DD' string. Defaults to today.

        Returns:
            Full path to the date directory.
        """
        if not date_str:
            date_str = datetime.now().strftime('%Y-%m-%d')
        date_dir = os.path.join(self._base_dir, date_str)
        try:
            os.makedirs(date_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"[LiveDataLogger] Failed to create dir {date_dir}: {e}")
            self._enabled = False
        return date_dir

    def _get_file_path(self, date_str, filename):
        """Get full file path for a given date and filename."""
        date_dir = self._get_date_dir(date_str)
        return os.path.join(date_dir, filename)

    def _get_lock(self, filepath):
        """Get or create a per-file lock (thread-safe)."""
        if filepath not in self._file_locks:
            with self._global_lock:
                if filepath not in self._file_locks:
                    self._file_locks[filepath] = threading.Lock()
        return self._file_locks[filepath]

    def _write_rows(self, filepath, headers, rows):
        """Write rows to CSV file. Creates header if file is new. Append-mode.

        Args:
            filepath: Full path to CSV file.
            headers: List of column names.
            rows: List of dicts to write.
        """
        if not rows:
            return

        lock = self._get_lock(filepath)
        try:
            with lock:
                file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
                with open(filepath, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
                    if not file_exists:
                        writer.writeheader()
                    writer.writerows(rows)
        except Exception as e:
            logger.error(f"[LiveDataLogger] Write error {filepath}: {e}")

    # ================================================================
    # Option Chain Snapshots
    # ================================================================

    def log_option_chain(self, symbol, chain_data, spot, expiry=''):
        """Log all strikes from one option chain snapshot.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            chain_data: Dict with 'CE' and 'PE' lists from MarketDataPipeline._option_chains
                        Each item: {'strike': 24500, 'ltp': 150.5, 'oi': 234000, ...}
            spot: Current spot price.
            expiry: Expiry string (e.g. '13 Mar 2026').
        """
        if not self._enabled or not chain_data:
            return

        now = datetime.now()

        # 2-minute dedup: skip if logged within last 2 min for this symbol
        last_ts = self._last_chain_ts.get(symbol)
        if last_ts and (now - last_ts).total_seconds() < 120:
            return
        self._last_chain_ts[symbol] = now

        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        date_str = now.strftime('%Y-%m-%d')
        filepath = self._get_file_path(date_str, f'option_chain_{symbol}.csv')

        rows = []
        for opt_type in ['CE', 'PE']:
            contracts = chain_data.get(opt_type, [])
            for contract in contracts:
                rows.append({
                    'timestamp': timestamp,
                    'symbol': symbol,
                    'expiry': expiry or '',
                    'strike': contract.get('strike', 0),
                    'type': opt_type,
                    'ltp': contract.get('ltp', 0),
                    'oi': contract.get('oi', 0),
                    'oi_change': contract.get('oi_change', contract.get('changeinOpenInterest', 0)),
                    'volume': contract.get('volume', contract.get('totalTradedVolume', 0)),
                    'iv': contract.get('iv', contract.get('impliedVolatility', 0)),
                    'spot': spot or 0,
                })

        if rows:
            self._write_rows(filepath, self.OPTION_CHAIN_HEADERS, rows)
            logger.debug(f"[LiveDataLogger] Option chain {symbol}: {len(rows)} strikes logged")

    # ================================================================
    # Market Indicators
    # ================================================================

    def log_market_indicators(self, symbol, spot, pcr=None, vix=None, oi_data=None):
        """Log market indicators (PCR, VIX, OI) for one symbol.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            spot: Current spot price.
            pcr: Put-Call Ratio (float).
            vix: India VIX value (float).
            oi_data: Dict with OI totals: {'call_oi': int, 'put_oi': int, 'call_oi_change': int, 'put_oi_change': int}
        """
        if not self._enabled or not spot:
            return

        now = datetime.now()
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        date_str = now.strftime('%Y-%m-%d')
        filepath = self._get_file_path(date_str, 'market_indicators.csv')

        oi = oi_data or {}
        row = {
            'timestamp': timestamp,
            'symbol': symbol,
            'spot': round(spot, 2),
            'pcr': round(pcr, 4) if pcr else '',
            'vix': round(vix, 2) if vix else '',
            'oi_call': oi.get('call_oi', ''),
            'oi_put': oi.get('put_oi', ''),
            'oi_change_call': oi.get('call_oi_change', ''),
            'oi_change_put': oi.get('put_oi_change', ''),
        }

        self._write_rows(filepath, self.MARKET_INDICATORS_HEADERS, [row])

    # ================================================================
    # Spot Tick Logging (1-minute aggregation)
    # ================================================================

    def log_spot_tick(self, symbol, ltp):
        """Log spot LTP — one entry per minute per symbol (dedup).

        Args:
            symbol: Symbol name (e.g. 'NIFTY', 'GOLDM', 'SBIN')
            ltp: Last traded price (float).
        """
        if not self._enabled or not ltp:
            return

        now = datetime.now()
        current_minute = now.strftime('%Y-%m-%d %H:%M')

        # 1-minute dedup: only log once per minute per symbol
        if self._last_spot_minute.get(symbol) == current_minute:
            return
        self._last_spot_minute[symbol] = current_minute

        date_str = now.strftime('%Y-%m-%d')
        filepath = self._get_file_path(date_str, f'spot_1min_{symbol}.csv')

        row = {
            'timestamp': current_minute,
            'symbol': symbol,
            'ltp': round(ltp, 2),
        }

        self._write_rows(filepath, self.SPOT_TICK_HEADERS, [row])

    # ================================================================
    # Status / Info
    # ================================================================

    def get_status(self):
        """Return logger status for monitoring."""
        today = datetime.now().strftime('%Y-%m-%d')
        today_dir = os.path.join(self._base_dir, today)
        files = {}
        if os.path.exists(today_dir):
            for f in os.listdir(today_dir):
                fpath = os.path.join(today_dir, f)
                if os.path.isfile(fpath):
                    files[f] = os.path.getsize(fpath)

        return {
            'enabled': self._enabled,
            'base_dir': self._base_dir,
            'today_dir': today_dir,
            'files': files,
            'symbols_tracked': list(self._last_spot_minute.keys()),
            'chains_tracked': list(self._last_chain_ts.keys()),
        }
