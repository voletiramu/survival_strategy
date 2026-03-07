"""
MARKET DATA PIPELINE v1.0
=========================
Real-time option chain data from NSE (NIFTY, BANKNIFTY) and BSE (SENSEX).
Runs as a background thread, fetching every 3 minutes (NSE OI update frequency).

Data Streams:
1. Real PCR (Put OI / Call OI) from live option chains
2. Intraday PCR trend (timestamped history for shift detection)
3. Most Active Contracts with premium %change
4. India VIX from NSE indices
5. OI change direction (accumulation vs unwinding)

Integration:
    from market_data_pipeline import MarketDataPipeline
    pipeline = MarketDataPipeline()
    pipeline.start()  # Background thread

    # In paper_trader.py scan loop:
    pcr = pipeline.get_pcr('NIFTY')           # Real PCR or None
    trend = pipeline.get_pcr_trend('NIFTY')    # [(timestamp, pcr), ...]
    vix = pipeline.get_vix()                    # India VIX float
    active = pipeline.get_most_active('NIFTY')  # Most active CE/PE contracts
    snapshot = pipeline.get_snapshot('NIFTY')    # Full market snapshot

    pipeline.stop()  # Clean shutdown

Author: Ram's Algo Trading System
"""

import os
import sys
import json
import time
import logging
import threading
import tempfile
import requests
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict, deque

logger = logging.getLogger('MarketDataPipeline')


def _safe_float(val, default=0.0):
    """Convert BSE string values to float safely.
    Handles: None, '', '70,900.00', '3', normal floats.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    val = str(val).strip()
    if not val:
        return default
    try:
        return float(val.replace(',', ''))
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    """Convert BSE string values to int safely."""
    return int(_safe_float(val, default))

# ============================================================
# CONFIGURATION
# ============================================================

# Fetch intervals
NSE_FETCH_INTERVAL = 180      # 3 minutes (NSE updates OI every 3 min)
BSE_FETCH_INTERVAL = 180      # 3 minutes
VIX_FETCH_INTERVAL = 120      # 2 minutes

# PCR trend history
PCR_HISTORY_MAX = 100          # Keep last 100 PCR readings (~5 hours at 3-min intervals)

# Market hours
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 35)  # 5 min buffer past 3:30

# BSE API endpoints
BSE_BASE_URL = 'https://api.bseindia.com/BseIndiaAPI/api'
BSE_OPTION_CHAIN_URL = f'{BSE_BASE_URL}/DerivOptionChain_IV/w'
BSE_EXPIRY_URL = f'{BSE_BASE_URL}/ddlExpiry_IV/w'
BSE_UNDERLYING_URL = f'{BSE_BASE_URL}/ddlUnderlyingAsset/w'

# BSE scrip codes (discovered from API)
BSE_SCRIP_CODES = {
    'SENSEX': 1,
    'BANKEX': 12,
}

# NSE symbols
NSE_SYMBOLS = ['NIFTY', 'BANKNIFTY']

# All pipeline symbols
ALL_SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']


class MarketDataPipeline:
    """Background data pipeline for real-time option chain metrics.

    Thread-safe: All data access methods use locks for concurrent reads.
    Resilient: Errors in one feed don't block others. Failed fetches
               are logged and retried on next cycle.
    """

    def __init__(self):
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # ---- NSE client (lazy init) ----
        self._nse = None
        self._nse_init_attempted = False

        # ---- BSE session (lazy init) ----
        self._bse_session = None
        self._bse_expiries = {}    # {symbol: [expiry_str, ...]}

        # ---- Data stores (thread-safe via _lock) ----

        # PCR: {symbol: float}
        self._pcr = {}

        # PCR trend: {symbol: deque of (datetime, float)}
        self._pcr_trend = defaultdict(lambda: deque(maxlen=PCR_HISTORY_MAX))

        # VIX: float
        self._vix = None
        self._vix_change_pct = None  # % change from previous close
        self._vix_time = None

        # Most active contracts: {symbol: {'CE': [...], 'PE': [...]}}
        self._most_active = {}

        # OI totals: {symbol: {'call_oi': int, 'put_oi': int, 'call_oi_change': int, 'put_oi_change': int}}
        self._oi_data = {}

        # Full option chain snapshot: {symbol: [{strike data}, ...]}
        self._option_chains = {}

        # Spot prices from option chain
        self._spots = {}

        # Timestamps of last successful fetch
        self._last_fetch = {}

        # Error tracking
        self._errors = defaultdict(int)
        self._last_error = {}

        # Fetch cycle count
        self._cycle_count = 0

        logger.info("[Pipeline] MarketDataPipeline initialized")

    # ============================================================
    # LIFECYCLE
    # ============================================================

    def start(self):
        """Start background data fetching thread."""
        if self._running:
            logger.warning("[Pipeline] Already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="MarketDataPipeline",
            daemon=True
        )
        self._thread.start()
        logger.info("[Pipeline] Background thread started (fetch every 3 min)")

    def stop(self):
        """Stop background thread gracefully."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("[Pipeline] Stopped")

    @property
    def is_running(self):
        return self._running and self._thread and self._thread.is_alive()

    # ============================================================
    # BACKGROUND LOOP
    # ============================================================

    def _run_loop(self):
        """Main background loop. Fetches all data every 3 minutes during market hours."""
        logger.info("[Pipeline] Fetch loop started")

        while self._running:
            try:
                now = datetime.now()
                current_time = now.time()
                is_weekday = now.weekday() <= 4

                # Only fetch during equity market hours (+ small buffer)
                if is_weekday and EQUITY_OPEN <= current_time <= EQUITY_CLOSE:
                    self._cycle_count += 1
                    logger.info(f"[Pipeline] === Fetch cycle #{self._cycle_count} | {now.strftime('%H:%M:%S')} ===")

                    # Fetch all feeds (errors in one don't block others)
                    self._fetch_all()

                    # Log summary
                    self._log_summary()
                else:
                    # Outside market hours - sleep longer
                    if is_weekday and current_time < EQUITY_OPEN:
                        wait_secs = min(300, (datetime.combine(now.date(), EQUITY_OPEN) - now).total_seconds())
                        logger.debug(f"[Pipeline] Pre-market. Sleeping {wait_secs:.0f}s until {EQUITY_OPEN}")
                        time.sleep(max(10, wait_secs))
                        continue
                    elif not is_weekday:
                        logger.debug("[Pipeline] Weekend. Sleeping 5 min.")
                        time.sleep(300)
                        continue
                    else:
                        # Post market
                        logger.debug("[Pipeline] Post-market. Sleeping 5 min.")
                        time.sleep(300)
                        continue

            except Exception as e:
                logger.error(f"[Pipeline] Fetch loop error: {e}")
                import traceback
                traceback.print_exc()

            # Wait for next cycle
            time.sleep(NSE_FETCH_INTERVAL)

        logger.info("[Pipeline] Fetch loop exited")

    def _fetch_all(self):
        """Fetch all data feeds. Each feed is independent — errors don't cascade."""

        # 1. NSE: NIFTY, BANKNIFTY option chains
        for symbol in NSE_SYMBOLS:
            try:
                self._fetch_nse_option_chain(symbol)
            except Exception as e:
                self._record_error(f'NSE_{symbol}', e)

        # 2. BSE: SENSEX option chain
        try:
            self._fetch_bse_option_chain('SENSEX')
        except Exception as e:
            self._record_error('BSE_SENSEX', e)

        # 3. India VIX
        try:
            self._fetch_vix()
        except Exception as e:
            self._record_error('VIX', e)

    # ============================================================
    # NSE DATA FETCHING
    # ============================================================

    def _init_nse(self):
        """Lazy-initialize NSE client. Only called once."""
        if self._nse_init_attempted:
            return self._nse is not None

        self._nse_init_attempted = True
        try:
            from nse import NSE
            download_dir = os.path.join(tempfile.gettempdir(), 'nse_pipeline_data')
            os.makedirs(download_dir, exist_ok=True)
            self._nse = NSE(download_dir)
            logger.info("[Pipeline] NSE client initialized successfully")
            return True
        except ImportError:
            logger.error("[Pipeline] NSE library not installed. Run: pip install nse")
            return False
        except Exception as e:
            logger.error(f"[Pipeline] NSE client init failed: {e}")
            return False

    def _fetch_nse_option_chain(self, symbol):
        """Fetch option chain from NSE for NIFTY or BANKNIFTY.

        Returns data with: strikePrice, openInterest, changeinOpenInterest,
        pchangeinOpenInterest, totalTradedVolume, impliedVolatility,
        lastPrice, change, pChange
        """
        if not self._init_nse():
            return

        try:
            data = self._nse.optionChain(symbol)
        except Exception as e:
            raise RuntimeError(f"NSE optionChain({symbol}) failed: {e}")

        if not data:
            raise RuntimeError(f"NSE optionChain({symbol}) returned empty data")

        # Parse the nested structure
        # NSE returns: {'records': {'data': [...], 'strikePrices': [...], 'expiryDates': [...]}}
        records = data.get('records', data) if isinstance(data, dict) else data
        if isinstance(records, dict):
            chain_data = records.get('data', [])
            expiry_dates = records.get('expiryDates', [])
        else:
            chain_data = records if isinstance(records, list) else []
            expiry_dates = []

        if not chain_data:
            raise RuntimeError(f"NSE {symbol}: No chain data in response")

        # Use nearest expiry only (current week)
        nearest_expiry = expiry_dates[0] if expiry_dates else None

        # Get spot from records level (always available)
        spot = records.get('underlyingValue') if isinstance(records, dict) else None

        # Filter for nearest expiry
        # NSE structure: each row has 'expiryDates' (top-level) matching the expiry
        # Also CE/PE sub-dicts have 'expiryDate' in DD-MM-YYYY format
        if nearest_expiry:
            filtered = [r for r in chain_data
                        if r.get('expiryDates') == nearest_expiry
                        or r.get('CE', {}).get('expiryDate', '') == nearest_expiry
                        or r.get('PE', {}).get('expiryDate', '') == nearest_expiry]
            if not filtered:
                # Fallback: use all data (single expiry might already be filtered)
                filtered = chain_data
        else:
            filtered = chain_data

        # Compute metrics
        total_call_oi = 0
        total_put_oi = 0
        total_call_oi_change = 0
        total_put_oi_change = 0
        ce_contracts = []
        pe_contracts = []

        for row in filtered:
            # Fallback spot from individual row
            if spot is None or spot == 0:
                spot = row.get('PE', {}).get('underlyingValue') or row.get('CE', {}).get('underlyingValue')

            strike = row.get('strikePrice', 0)

            # CE data
            ce = row.get('CE', {})
            if ce and ce.get('openInterest', 0) > 0:
                ce_oi = ce.get('openInterest', 0)
                ce_oi_change = ce.get('changeinOpenInterest', 0)
                total_call_oi += ce_oi
                total_call_oi_change += ce_oi_change
                ce_contracts.append({
                    'strike': strike,
                    'ltp': ce.get('lastPrice', 0),
                    'change': ce.get('change', 0),
                    'pChange': ce.get('pChange', 0),
                    'oi': ce_oi,
                    'oi_change': ce_oi_change,
                    'oi_pchange': ce.get('pchangeinOpenInterest', 0),
                    'volume': ce.get('totalTradedVolume', 0),
                    'iv': ce.get('impliedVolatility', 0),
                })

            # PE data
            pe = row.get('PE', {})
            if pe and pe.get('openInterest', 0) > 0:
                pe_oi = pe.get('openInterest', 0)
                pe_oi_change = pe.get('changeinOpenInterest', 0)
                total_put_oi += pe_oi
                total_put_oi_change += pe_oi_change
                pe_contracts.append({
                    'strike': strike,
                    'ltp': pe.get('lastPrice', 0),
                    'change': pe.get('change', 0),
                    'pChange': pe.get('pChange', 0),
                    'oi': pe_oi,
                    'oi_change': pe_oi_change,
                    'oi_pchange': pe.get('pchangeinOpenInterest', 0),
                    'volume': pe.get('totalTradedVolume', 0),
                    'iv': pe.get('impliedVolatility', 0),
                })

        # Compute PCR
        pcr = total_put_oi / total_call_oi if total_call_oi > 0 else None

        # Sort by volume for "most active"
        ce_most_active = sorted(ce_contracts, key=lambda x: x['volume'], reverse=True)[:5]
        pe_most_active = sorted(pe_contracts, key=lambda x: x['volume'], reverse=True)[:5]

        # Store results (thread-safe)
        with self._lock:
            now = datetime.now()

            if pcr is not None:
                self._pcr[symbol] = pcr
                self._pcr_trend[symbol].append((now, pcr))

            self._oi_data[symbol] = {
                'call_oi': total_call_oi,
                'put_oi': total_put_oi,
                'call_oi_change': total_call_oi_change,
                'put_oi_change': total_put_oi_change,
            }

            self._most_active[symbol] = {
                'CE': ce_most_active,
                'PE': pe_most_active,
            }

            if spot:
                self._spots[symbol] = spot

            self._option_chains[symbol] = {
                'CE': ce_contracts,
                'PE': pe_contracts,
                'expiry': nearest_expiry,
            }

            self._last_fetch[f'NSE_{symbol}'] = now

        pcr_str = f"{pcr:.3f}" if pcr is not None else "N/A"
        logger.info(f"[Pipeline] NSE {symbol}: PCR={pcr_str} | "
                    f"Call OI={total_call_oi:,} | Put OI={total_put_oi:,} | "
                    f"OI Change: CE={total_call_oi_change:+,} PE={total_put_oi_change:+,} | "
                    f"Spot={spot} | {len(ce_contracts)} CE + {len(pe_contracts)} PE strikes")

    # ============================================================
    # BSE DATA FETCHING
    # ============================================================

    def _init_bse_session(self):
        """Initialize BSE API session with required headers/cookies."""
        if self._bse_session:
            return True

        try:
            self._bse_session = requests.Session()
            self._bse_session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.bseindia.com/',
                'Origin': 'https://www.bseindia.com',
            })

            # Get session cookie from BSE homepage
            resp = self._bse_session.get('https://www.bseindia.com/', timeout=10)
            if resp.status_code == 200:
                logger.info("[Pipeline] BSE session initialized")
                return True
            else:
                logger.warning(f"[Pipeline] BSE homepage returned {resp.status_code}")
                self._bse_session = None
                return False

        except Exception as e:
            logger.error(f"[Pipeline] BSE session init failed: {e}")
            self._bse_session = None
            return False

    def _get_bse_nearest_expiry(self, symbol):
        """Get nearest active expiry for BSE symbol."""
        scrip_cd = BSE_SCRIP_CODES.get(symbol)
        if not scrip_cd:
            return None

        # Check cache first
        cached = self._bse_expiries.get(symbol)
        if cached:
            return cached[0]  # Nearest

        try:
            resp = self._bse_session.get(
                BSE_EXPIRY_URL,
                params={'ProductType': 'IO', 'scrip_cd': scrip_cd},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                table = data.get('Table1', data.get('Table', []))
                if table:
                    expiries = [row.get('ExpiryDate', '') for row in table if row.get('ExpiryDate')]
                    # Filter out expired/past dates
                    today = datetime.now().date()
                    active = []
                    for e in expiries:
                        try:
                            exp_date = datetime.strptime(e, '%d %b %Y').date()
                            if exp_date >= today:
                                active.append(e)
                        except ValueError:
                            active.append(e)  # Keep unparseable dates
                    if not active:
                        active = expiries  # Use all if filtering removed everything
                    self._bse_expiries[symbol] = active
                    logger.info(f"[Pipeline] BSE {symbol} expiries: {active[:3]}")
                    return active[0] if active else None
        except Exception as e:
            logger.error(f"[Pipeline] BSE expiry fetch failed: {e}")
        return None

    def _fetch_bse_option_chain(self, symbol):
        """Fetch SENSEX option chain from BSE API.

        API: DerivOptionChain_IV/w?scrip_cd=1&Expiry=12 Mar 2026
        Fields: C_Open_Interest, C_Absolute_Change_OI, C_Last_Trd_Price, C_NetChange,
                C_Vol_Traded, Strike_Price, Open_Interest, Last_Trd_Price, NetChange,
                Absolute_Change_OI, IV, C_IV, UlaValue (spot)
        """
        if not self._init_bse_session():
            return

        scrip_cd = BSE_SCRIP_CODES.get(symbol)
        if not scrip_cd:
            raise RuntimeError(f"Unknown BSE symbol: {symbol}")

        # Get nearest expiry
        expiry = self._get_bse_nearest_expiry(symbol)
        if not expiry:
            raise RuntimeError(f"No BSE expiry found for {symbol}")

        # Fetch option chain
        resp = self._bse_session.get(
            BSE_OPTION_CHAIN_URL,
            params={'scrip_cd': scrip_cd, 'Expiry': expiry, 'strprice': ''},
            timeout=15
        )

        if resp.status_code != 200:
            # Session may have expired — reset and retry
            self._bse_session = None
            raise RuntimeError(f"BSE API returned {resp.status_code}")

        data = resp.json()
        table = data.get('Table', [])

        if not table:
            # Try Table1
            table = data.get('Table1', [])

        if not table:
            raise RuntimeError(f"BSE {symbol}: Empty option chain for expiry {expiry}")

        # Parse BSE option chain
        # BSE format: Each row has BOTH CE and PE data for a strike
        # CE fields: C_Open_Interest, C_Absolute_Change_OI, C_Last_Trd_Price, C_NetChange, C_Vol_Traded, C_IV
        # PE fields: Open_Interest, Absolute_Change_OI, Last_Trd_Price, NetChange, Vol_Traded (if exists), IV

        total_call_oi = 0
        total_put_oi = 0
        total_call_oi_change = 0
        total_put_oi_change = 0
        ce_contracts = []
        pe_contracts = []
        spot = None

        for row in table:
            strike = _safe_float(row.get('Strike_Price'))
            if strike <= 0:
                continue

            # Get spot (BSE values are strings, may have commas)
            if spot is None or spot == 0:
                ula = _safe_float(row.get('UlaValue'))
                if ula > 0:
                    spot = ula

            # CE data (BSE prefixes CE fields with C_)
            ce_oi = _safe_int(row.get('C_Open_Interest'))
            ce_oi_change = _safe_int(row.get('C_Absolute_Change_OI'))
            ce_ltp = _safe_float(row.get('C_Last_Trd_Price'))
            ce_change = _safe_float(row.get('C_NetChange'))
            ce_volume = _safe_int(row.get('C_Vol_Traded'))
            ce_iv = _safe_float(row.get('C_IV'))

            if ce_oi > 0:
                total_call_oi += ce_oi
                total_call_oi_change += ce_oi_change
                prev_ltp = ce_ltp - ce_change
                ce_pchange = (ce_change / prev_ltp * 100) if prev_ltp > 0 else 0
                ce_contracts.append({
                    'strike': strike,
                    'ltp': ce_ltp,
                    'change': ce_change,
                    'pChange': round(ce_pchange, 2),
                    'oi': ce_oi,
                    'oi_change': ce_oi_change,
                    'volume': ce_volume,
                    'iv': ce_iv,
                })

            # PE data (BSE uses unprefixed fields for PE)
            pe_oi = _safe_int(row.get('Open_Interest'))
            pe_oi_change = _safe_int(row.get('Absolute_Change_OI'))
            pe_ltp = _safe_float(row.get('Last_Trd_Price'))
            pe_change = _safe_float(row.get('NetChange'))
            pe_volume = _safe_int(row.get('Vol_Traded', row.get('Traded_Qty', '')))
            pe_iv = _safe_float(row.get('IV'))

            if pe_oi > 0:
                total_put_oi += pe_oi
                total_put_oi_change += pe_oi_change
                prev_ltp = pe_ltp - pe_change
                pe_pchange = (pe_change / prev_ltp * 100) if prev_ltp > 0 else 0
                pe_contracts.append({
                    'strike': strike,
                    'ltp': pe_ltp,
                    'change': pe_change,
                    'pChange': round(pe_pchange, 2),
                    'oi': pe_oi,
                    'oi_change': pe_oi_change,
                    'volume': pe_volume,
                    'iv': pe_iv,
                })

        # Compute PCR
        pcr = total_put_oi / total_call_oi if total_call_oi > 0 else None

        # Sort by volume for "most active"
        ce_most_active = sorted(ce_contracts, key=lambda x: x['volume'], reverse=True)[:5]
        pe_most_active = sorted(pe_contracts, key=lambda x: x['volume'], reverse=True)[:5]

        # Store results (thread-safe)
        with self._lock:
            now = datetime.now()

            if pcr is not None:
                self._pcr[symbol] = pcr
                self._pcr_trend[symbol].append((now, pcr))

            self._oi_data[symbol] = {
                'call_oi': total_call_oi,
                'put_oi': total_put_oi,
                'call_oi_change': total_call_oi_change,
                'put_oi_change': total_put_oi_change,
            }

            self._most_active[symbol] = {
                'CE': ce_most_active,
                'PE': pe_most_active,
            }

            if spot:
                self._spots[symbol] = spot

            self._option_chains[symbol] = {
                'CE': ce_contracts,
                'PE': pe_contracts,
                'expiry': expiry,
            }

            self._last_fetch[f'BSE_{symbol}'] = now

        pcr_str = f"{pcr:.3f}" if pcr is not None else "N/A"
        logger.info(f"[Pipeline] BSE {symbol}: PCR={pcr_str} | "
                    f"Call OI={total_call_oi:,} | Put OI={total_put_oi:,} | "
                    f"OI Change: CE={total_call_oi_change:+,} PE={total_put_oi_change:+,} | "
                    f"Spot={spot} | {len(ce_contracts)} CE + {len(pe_contracts)} PE strikes")

    # ============================================================
    # VIX FETCHING
    # ============================================================

    def _fetch_vix(self):
        """Fetch India VIX from NSE indices API."""
        if not self._init_nse():
            return

        try:
            indices = self._nse.listIndices()
        except Exception as e:
            raise RuntimeError(f"NSE listIndices failed: {e}")

        if not indices:
            return

        # Find VIX in indices list
        vix_data = None
        indices_list = indices.get('data', indices) if isinstance(indices, dict) else indices

        if isinstance(indices_list, list):
            for idx in indices_list:
                name = idx.get('index', idx.get('indexSymbol', ''))
                if 'VIX' in name.upper():
                    vix_data = idx
                    break

        if vix_data:
            vix_value = float(vix_data.get('last', vix_data.get('lastPrice', 0)))
            vix_change = float(vix_data.get('percentChange', vix_data.get('pChange', 0)))

            if vix_value > 0:
                with self._lock:
                    self._vix = vix_value
                    self._vix_change_pct = vix_change
                    self._vix_time = datetime.now()

                logger.info(f"[Pipeline] VIX: {vix_value:.2f} ({vix_change:+.2f}%)")
            else:
                logger.warning(f"[Pipeline] VIX value invalid: {vix_value}")

    # ============================================================
    # PUBLIC API (Thread-Safe Read Access)
    # ============================================================

    def get_pcr(self, symbol):
        """Get current real PCR for symbol.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'

        Returns:
            float PCR value or None if not available.
            PCR > 1.0 = more puts = bullish signal
            PCR < 1.0 = more calls = bearish signal
        """
        with self._lock:
            return self._pcr.get(symbol)

    def get_pcr_trend(self, symbol, minutes=60):
        """Get PCR trend over last N minutes.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            minutes: How many minutes of history (default 60)

        Returns:
            List of (datetime, pcr) tuples, oldest first.
            Empty list if no data.
        """
        cutoff = datetime.now() - timedelta(minutes=minutes)
        with self._lock:
            trend = self._pcr_trend.get(symbol, deque())
            return [(t, p) for t, p in trend if t >= cutoff]

    def get_pcr_shift(self, symbol, window_minutes=30):
        """Detect PCR shift direction over a time window.

        Like Murarka's PCR shift detection: 0.80 → 1.29 = bullish.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            window_minutes: Time window for shift detection

        Returns:
            Dict with:
                'current': float - current PCR
                'start': float - PCR at start of window
                'shift': float - change (positive = more puts = bullish)
                'direction': 'BULLISH' / 'BEARISH' / 'NEUTRAL'
                'strength': 'STRONG' / 'MODERATE' / 'WEAK'
            Or None if insufficient data.
        """
        trend = self.get_pcr_trend(symbol, minutes=window_minutes)
        if len(trend) < 2:
            return None

        start_pcr = trend[0][1]
        current_pcr = trend[-1][1]
        shift = current_pcr - start_pcr

        # Determine direction
        if shift > 0.15:
            direction = 'BULLISH'
            strength = 'STRONG'
        elif shift > 0.05:
            direction = 'BULLISH'
            strength = 'MODERATE'
        elif shift < -0.15:
            direction = 'BEARISH'
            strength = 'STRONG'
        elif shift < -0.05:
            direction = 'BEARISH'
            strength = 'MODERATE'
        else:
            direction = 'NEUTRAL'
            strength = 'WEAK'

        return {
            'current': current_pcr,
            'start': start_pcr,
            'shift': round(shift, 3),
            'direction': direction,
            'strength': strength,
        }

    def get_vix(self):
        """Get current India VIX value.

        Returns:
            float VIX value or None.
            VIX < 14: Low vol (cautious entry, wide SL)
            VIX 14-20: Normal
            VIX > 20: High vol (more opportunities, tighter SL)
        """
        with self._lock:
            return self._vix

    def get_vix_regime(self):
        """Get VIX regime classification.

        Like Murarka: VIX > 20 + falling = contrarian BUY opportunity

        Returns:
            Dict with:
                'value': float VIX
                'change_pct': float % change
                'regime': 'LOW_VOL' / 'NORMAL' / 'HIGH_VOL' / 'EXTREME'
                'contrarian_signal': 'BUY' / 'SELL' / None
            Or None if VIX unavailable.
        """
        with self._lock:
            if self._vix is None:
                return None

            vix = self._vix
            change = self._vix_change_pct or 0

        # Classify regime
        if vix > 30:
            regime = 'EXTREME'
        elif vix > 20:
            regime = 'HIGH_VOL'
        elif vix > 14:
            regime = 'NORMAL'
        else:
            regime = 'LOW_VOL'

        # Contrarian signal: high VIX + falling = buy opportunity (fear fading)
        contrarian = None
        if vix > 20 and change < -5:
            contrarian = 'BUY'   # Fear fading → buy
        elif vix < 12 and change > 5:
            contrarian = 'SELL'  # Complacency rising → caution

        return {
            'value': vix,
            'change_pct': change,
            'regime': regime,
            'contrarian_signal': contrarian,
        }

    def get_most_active(self, symbol):
        """Get most active contracts by volume.

        Like Murarka's "Most Active" table showing premium %change.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'

        Returns:
            Dict with 'CE' and 'PE' lists, each entry having:
                strike, ltp, change, pChange, oi, oi_change, volume, iv
            Or None if not available.
        """
        with self._lock:
            return self._most_active.get(symbol)

    def get_oi_data(self, symbol):
        """Get OI summary (total call/put OI and changes).

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', or 'SENSEX'

        Returns:
            Dict with: call_oi, put_oi, call_oi_change, put_oi_change
            Or None if not available.
        """
        with self._lock:
            return self._oi_data.get(symbol)

    def get_oi_sentiment(self, symbol):
        """Analyze OI change direction for market sentiment.

        OI analysis like Murarka:
        - Put OI increasing + Call OI decreasing = BULLISH (put writers confident)
        - Call OI increasing + Put OI decreasing = BEARISH (call writers confident)
        - Both increasing = RANGE_BOUND
        - Both decreasing = UNWINDING (trend change coming)

        Returns:
            Dict with 'sentiment' and details, or None.
        """
        with self._lock:
            oi = self._oi_data.get(symbol)

        if not oi:
            return None

        ce_chg = oi['call_oi_change']
        pe_chg = oi['put_oi_change']

        if pe_chg > 0 and ce_chg < 0:
            sentiment = 'BULLISH'
            reason = f'Put OI +{pe_chg:,} (support) + Call OI {ce_chg:,} (unwinding)'
        elif ce_chg > 0 and pe_chg < 0:
            sentiment = 'BEARISH'
            reason = f'Call OI +{ce_chg:,} (resistance) + Put OI {pe_chg:,} (unwinding)'
        elif ce_chg > 0 and pe_chg > 0:
            sentiment = 'RANGE_BOUND'
            reason = f'Both building: CE OI +{ce_chg:,} + PE OI +{pe_chg:,}'
        elif ce_chg < 0 and pe_chg < 0:
            sentiment = 'UNWINDING'
            reason = f'Both unwinding: CE OI {ce_chg:,} + PE OI {pe_chg:,} (trend change)'
        else:
            sentiment = 'NEUTRAL'
            reason = f'CE OI chg={ce_chg:,}, PE OI chg={pe_chg:,}'

        return {
            'sentiment': sentiment,
            'reason': reason,
            'call_oi': oi['call_oi'],
            'put_oi': oi['put_oi'],
            'call_oi_change': ce_chg,
            'put_oi_change': pe_chg,
        }

    def get_premium_sentiment(self, symbol):
        """Analyze premium changes of most active contracts.

        Like Murarka's "Most Active" showing PE premiums -40% to -57%.
        When PEs are bleeding heavily → market is bullish (sell PE / buy CE).

        Returns:
            Dict with CE/PE avg premium change and signal.
        """
        with self._lock:
            active = self._most_active.get(symbol)

        if not active:
            return None

        # Average premium %change for top 5 CE and PE
        ce_changes = [c['pChange'] for c in active.get('CE', []) if c.get('pChange')]
        pe_changes = [c['pChange'] for c in active.get('PE', []) if c.get('pChange')]

        avg_ce_change = sum(ce_changes) / len(ce_changes) if ce_changes else 0
        avg_pe_change = sum(pe_changes) / len(pe_changes) if pe_changes else 0

        # Interpret
        if avg_pe_change < -20 and avg_ce_change > 10:
            signal = 'STRONG_BULLISH'
            reason = f'PEs bleeding {avg_pe_change:.1f}% while CEs gaining {avg_ce_change:.1f}%'
        elif avg_pe_change < -10:
            signal = 'BULLISH'
            reason = f'PEs losing {avg_pe_change:.1f}%'
        elif avg_ce_change < -20 and avg_pe_change > 10:
            signal = 'STRONG_BEARISH'
            reason = f'CEs bleeding {avg_ce_change:.1f}% while PEs gaining {avg_pe_change:.1f}%'
        elif avg_ce_change < -10:
            signal = 'BEARISH'
            reason = f'CEs losing {avg_ce_change:.1f}%'
        else:
            signal = 'NEUTRAL'
            reason = f'CE: {avg_ce_change:.1f}%, PE: {avg_pe_change:.1f}%'

        return {
            'signal': signal,
            'reason': reason,
            'avg_ce_change': round(avg_ce_change, 2),
            'avg_pe_change': round(avg_pe_change, 2),
        }

    def get_snapshot(self, symbol):
        """Get complete market snapshot for a symbol.

        Combines all data streams into one dict for easy consumption.

        Returns:
            Dict with all available data, or empty dict.
        """
        with self._lock:
            snapshot = {
                'symbol': symbol,
                'timestamp': datetime.now().isoformat(),
                'pcr': self._pcr.get(symbol),
                'spot': self._spots.get(symbol),
                'vix': self._vix,
                'vix_change': self._vix_change_pct,
            }

            oi = self._oi_data.get(symbol)
            if oi:
                snapshot.update({
                    'call_oi': oi['call_oi'],
                    'put_oi': oi['put_oi'],
                    'call_oi_change': oi['call_oi_change'],
                    'put_oi_change': oi['put_oi_change'],
                })

            last_fetch_key = f'NSE_{symbol}' if symbol in NSE_SYMBOLS else f'BSE_{symbol}'
            snapshot['last_fetch'] = self._last_fetch.get(last_fetch_key, None)
            snapshot['data_age_sec'] = (
                (datetime.now() - snapshot['last_fetch']).total_seconds()
                if snapshot['last_fetch'] else None
            )

        # Add derived metrics (outside lock for perf)
        snapshot['pcr_shift'] = self.get_pcr_shift(symbol)
        snapshot['oi_sentiment'] = self.get_oi_sentiment(symbol)
        snapshot['premium_sentiment'] = self.get_premium_sentiment(symbol)
        snapshot['vix_regime'] = self.get_vix_regime()

        return snapshot

    def get_data_status(self):
        """Get pipeline health status for monitoring.

        Returns:
            Dict with status of all feeds, error counts, last fetch times.
        """
        with self._lock:
            status = {
                'running': self._running,
                'cycles': self._cycle_count,
                'feeds': {},
            }

            for symbol in ALL_SYMBOLS:
                key = f'NSE_{symbol}' if symbol in NSE_SYMBOLS else f'BSE_{symbol}'
                last = self._last_fetch.get(key)
                age = (datetime.now() - last).total_seconds() if last else None

                status['feeds'][symbol] = {
                    'source': 'NSE' if symbol in NSE_SYMBOLS else 'BSE',
                    'last_fetch': last.isoformat() if last else None,
                    'age_seconds': round(age) if age else None,
                    'pcr': self._pcr.get(symbol),
                    'healthy': age is not None and age < 600,  # < 10 min = healthy
                    'errors': self._errors.get(key, 0),
                    'last_error': str(self._last_error.get(key, ''))[:100],
                }

            status['vix'] = {
                'value': self._vix,
                'change': self._vix_change_pct,
                'last_fetch': self._vix_time.isoformat() if self._vix_time else None,
            }

        return status

    # ============================================================
    # INTERNAL HELPERS
    # ============================================================

    def _record_error(self, feed_name, error):
        """Record error for monitoring."""
        self._errors[feed_name] += 1
        self._last_error[feed_name] = str(error)
        logger.error(f"[Pipeline] {feed_name} error (#{self._errors[feed_name]}): {error}")

    def _log_summary(self):
        """Log summary of all fetched data."""
        with self._lock:
            pcr_summary = ' | '.join(
                f"{sym}={self._pcr[sym]:.3f}"
                for sym in ALL_SYMBOLS if sym in self._pcr
            )
            vix_str = f"VIX={self._vix:.2f}" if self._vix else "VIX=N/A"

        logger.info(f"[Pipeline] Summary: {pcr_summary} | {vix_str}")

    def fetch_once(self):
        """Manual one-time fetch (for testing). Blocks until complete."""
        logger.info("[Pipeline] Manual fetch triggered")
        self._fetch_all()
        self._log_summary()


# ============================================================
# STANDALONE TESTING
# ============================================================

def main():
    """Test the pipeline standalone."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    print("=" * 70)
    print("  MARKET DATA PIPELINE - Standalone Test")
    print("=" * 70)

    pipeline = MarketDataPipeline()

    # Single fetch
    print("\nFetching all data (one-time)...")
    pipeline.fetch_once()

    # Print results
    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)

    for symbol in ALL_SYMBOLS:
        snapshot = pipeline.get_snapshot(symbol)
        print(f"\n--- {symbol} ---")
        print(f"  PCR: {snapshot.get('pcr', 'N/A')}")
        print(f"  Spot: {snapshot.get('spot', 'N/A')}")

        oi_sent = snapshot.get('oi_sentiment')
        if oi_sent:
            print(f"  OI Sentiment: {oi_sent['sentiment']} — {oi_sent['reason']}")

        prem_sent = snapshot.get('premium_sentiment')
        if prem_sent:
            print(f"  Premium Sentiment: {prem_sent['signal']} — {prem_sent['reason']}")

        active = pipeline.get_most_active(symbol)
        if active:
            print(f"  Most Active CE: ", end='')
            for c in (active.get('CE', [])[:3]):
                print(f"{c['strike']}@{c['ltp']:.1f}({c['pChange']:+.1f}%) ", end='')
            print()
            print(f"  Most Active PE: ", end='')
            for c in (active.get('PE', [])[:3]):
                print(f"{c['strike']}@{c['ltp']:.1f}({c['pChange']:+.1f}%) ", end='')
            print()

    vix_regime = pipeline.get_vix_regime()
    if vix_regime:
        print(f"\n--- VIX ---")
        print(f"  Value: {vix_regime['value']:.2f} ({vix_regime['change_pct']:+.2f}%)")
        print(f"  Regime: {vix_regime['regime']}")
        print(f"  Contrarian: {vix_regime['contrarian_signal'] or 'None'}")

    # Status
    print("\n--- Pipeline Status ---")
    status = pipeline.get_data_status()
    for sym, info in status['feeds'].items():
        health = 'OK' if info['healthy'] else 'STALE/ERROR'
        print(f"  {sym} ({info['source']}): {health} | PCR={info['pcr']} | Errors={info['errors']}")

    print("\n" + "=" * 70)
    print("  Test complete!")
    print("=" * 70)


if __name__ == '__main__':
    main()
