"""
STOCK DATA PIPELINE — Background Option Chain Fetcher (v1.0)
=============================================================
Background thread that continuously fetches option chain data
for today's narrow CPR watchlist stocks.

Provides:
- Real-time PCR (Put/Call Ratio) per stock
- OI tracking and changes since last fetch
- Volume spike detection
- Most active strike identification

Fetch frequency: Every 5 min for watchlist stocks (10-20 stocks).
Rate limited: 1 stock per second (respect Angel API limits).

Usage:
    pipeline = StockDataPipeline(angel, fno_config)
    pipeline.set_watchlist(['CIPLA', 'TITAN', 'SBIN'])
    pipeline.start()
    ...
    pcr = pipeline.get_pcr('CIPLA')
    oi = pipeline.get_oi_data('CIPLA')
    ...
    pipeline.stop()
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOCK_PAPER_DIR = os.path.join(BASE_DIR, 'stock_paper_trades')
os.makedirs(STOCK_PAPER_DIR, exist_ok=True)

logger = logging.getLogger('StockDataPipeline')

# Market hours
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


class StockDataPipeline:
    """Background thread fetching option chains for watchlist stocks.

    Runs in a daemon thread alongside StockPaperTrader. Provides
    real-time option chain data (OI, IV, PCR, volume) that the
    trading strategies consume for decision-making.

    Thread-safe: All data access goes through a threading.Lock.
    """

    def __init__(self, angel, fno_config):
        """Initialize pipeline.

        Args:
            angel: StockAngelConnection instance (shared with trader).
            fno_config: StockFnOConfig instance.
        """
        self.angel = angel
        self.fno_config = fno_config
        self.watchlist = []              # List of stock symbols to track

        # Data stores (thread-safe access via _lock)
        self._option_chains = {}         # {symbol: {strike: {ce: {...}, pe: {...}}}}
        self._stock_pcr = {}             # {symbol: float}
        self._stock_oi = {}              # {symbol: {total_call_oi, total_put_oi}}
        self._stock_oi_history = {}      # {symbol: [{time, call_oi, put_oi}]}
        self._stock_volume = {}          # {symbol: {total_call_vol, total_put_vol}}
        self._stock_iv_avg = {}          # {symbol: float (average IV)}
        self._most_active = {}           # {symbol: {ce: {strike, oi}, pe: {strike, oi}}}
        self._last_fetch_time = {}       # {symbol: datetime}

        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._fetch_interval = 300       # 5 minutes between full cycles

    def set_watchlist(self, symbols):
        """Update which stocks to track.

        Thread-safe — can be called from main thread while pipeline runs.

        Args:
            symbols: list of stock symbol strings (e.g. ['CIPLA', 'TITAN']).
        """
        with self._lock:
            old = set(self.watchlist)
            new = set(symbols)
            added = new - old
            removed = old - new

            self.watchlist = list(symbols)

            if added:
                logger.info(f"Pipeline watchlist +{len(added)}: {', '.join(added)}")
            if removed:
                logger.info(f"Pipeline watchlist -{len(removed)}: {', '.join(removed)}")
                # Clean up removed symbols
                for sym in removed:
                    self._option_chains.pop(sym, None)
                    self._stock_pcr.pop(sym, None)
                    self._stock_oi.pop(sym, None)
                    self._stock_volume.pop(sym, None)

    def start(self):
        """Start the background fetch thread.

        Thread is a daemon — dies when main process exits.
        """
        if self._running:
            logger.info("Pipeline already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name='StockDataPipeline')
        self._thread.start()
        logger.info("Stock data pipeline started (background thread)")

    def stop(self):
        """Stop the background fetch thread gracefully."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Stock data pipeline stopped")

    def _run_loop(self):
        """Main background loop. Runs continuously during market hours.

        Fetches option chains for all watchlist stocks every 5 minutes.
        Rate-limited to 1 stock per second to respect API limits.
        """
        logger.info("Pipeline thread started")

        while self._running:
            now = datetime.now()

            # Only fetch during market hours
            if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
                if now.time() > MARKET_CLOSE:
                    logger.info("Pipeline: Market closed, stopping loop")
                    break
                time.sleep(60)
                continue

            # Get current watchlist (thread-safe copy)
            with self._lock:
                symbols = list(self.watchlist)

            if not symbols:
                time.sleep(30)
                continue

            # Fetch each stock with rate limiting
            for symbol in symbols:
                if not self._running:
                    break

                try:
                    self._fetch_stock_option_chain(symbol)
                except Exception as e:
                    logger.error(f"Pipeline fetch error for {symbol}: {e}")

                time.sleep(1.5)  # Rate limit: 1.5s between stocks

            # Wait for next cycle
            elapsed = (datetime.now() - now).total_seconds()
            remaining_wait = max(0, self._fetch_interval - elapsed)
            logger.info(f"Pipeline cycle complete ({len(symbols)} stocks in {elapsed:.0f}s). "
                        f"Next in {remaining_wait:.0f}s")

            # Sleep in small chunks so we can stop quickly
            wait_end = time.time() + remaining_wait
            while self._running and time.time() < wait_end:
                time.sleep(5)

        logger.info("Pipeline thread ended")

    def _fetch_stock_option_chain(self, symbol):
        """Fetch option chain for a single stock from Angel API.

        Computes:
        - PCR (Put/Call OI ratio)
        - Total CE/PE OI
        - Total CE/PE volume
        - Average IV
        - Most active strikes

        Args:
            symbol: Stock symbol.
        """
        if not self.angel or not self.angel._connected:
            return

        # Get nearest monthly expiry
        expiry = self._get_stock_expiry(symbol)
        if not expiry:
            return

        # Fetch option chain via Angel API
        try:
            data = self.angel.get_option_greeks(symbol, expiry)
            if not data or not isinstance(data, list):
                logger.debug(f"Pipeline: No option chain data for {symbol}")
                return
        except Exception as e:
            logger.debug(f"Pipeline: Option chain API error for {symbol}: {e}")
            return

        # Parse and aggregate
        total_call_oi = 0
        total_put_oi = 0
        total_call_vol = 0
        total_put_vol = 0
        iv_values = []
        chain = {}
        best_ce = {'strike': 0, 'oi': 0}
        best_pe = {'strike': 0, 'oi': 0}

        for row in data:
            opt_type = row.get('optionType', '')
            strike = float(row.get('strikePrice', 0) or 0)
            oi = float(row.get('opnInterest', 0) or 0)
            vol = float(row.get('tradeVolume', 0) or 0)
            ltp = float(row.get('ltp', 0) or 0)
            iv = float(row.get('impliedVolatility', 0) or 0)
            delta = float(row.get('delta', 0) or 0)
            gamma = float(row.get('gamma', 0) or 0)
            theta = float(row.get('theta', 0) or 0)

            strike_key = str(int(strike))
            if strike_key not in chain:
                chain[strike_key] = {}

            chain[strike_key][opt_type.lower()] = {
                'strike': strike,
                'oi': oi,
                'volume': vol,
                'ltp': ltp,
                'iv': iv,
                'delta': delta,
                'gamma': gamma,
                'theta': theta,
            }

            if opt_type == 'CE':
                total_call_oi += oi
                total_call_vol += vol
                if oi > best_ce['oi']:
                    best_ce = {'strike': strike, 'oi': oi, 'ltp': ltp}
            elif opt_type == 'PE':
                total_put_oi += oi
                total_put_vol += vol
                if oi > best_pe['oi']:
                    best_pe = {'strike': strike, 'oi': oi, 'ltp': ltp}

            if iv > 0:
                iv_values.append(iv)

        # Compute PCR
        pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else 1.0

        # Average IV
        avg_iv = np.mean(iv_values) if iv_values else 0

        # Store data (thread-safe)
        with self._lock:
            self._option_chains[symbol] = chain
            self._stock_pcr[symbol] = round(pcr, 3)
            self._stock_oi[symbol] = {
                'total_call_oi': total_call_oi,
                'total_put_oi': total_put_oi,
            }
            self._stock_volume[symbol] = {
                'total_call_vol': total_call_vol,
                'total_put_vol': total_put_vol,
            }
            self._stock_iv_avg[symbol] = round(avg_iv, 2)
            self._most_active[symbol] = {'ce': best_ce, 'pe': best_pe}
            self._last_fetch_time[symbol] = datetime.now()

            # Track OI history for change detection
            if symbol not in self._stock_oi_history:
                self._stock_oi_history[symbol] = []
            self._stock_oi_history[symbol].append({
                'time': datetime.now().isoformat(),
                'call_oi': total_call_oi,
                'put_oi': total_put_oi,
                'pcr': pcr,
            })
            # Keep last 50 readings
            if len(self._stock_oi_history[symbol]) > 50:
                self._stock_oi_history[symbol] = self._stock_oi_history[symbol][-50:]

        logger.info(f"  PIPELINE: {symbol} PCR={pcr:.3f} | "
                    f"CE_OI={total_call_oi:,.0f} PE_OI={total_put_oi:,.0f} | "
                    f"Avg_IV={avg_iv:.1f}% | "
                    f"Best_CE={best_ce['strike']:.0f} Best_PE={best_pe['strike']:.0f}")

    def _get_stock_expiry(self, symbol):
        """Get nearest monthly expiry for a stock from instruments master.

        Args:
            symbol: Stock symbol.

        Returns:
            str: Expiry in 'ddMONyyyy' format or None.
        """
        if self.angel.instruments is None:
            return None

        try:
            import pandas as pd
            mask = ((self.angel.instruments['name'] == symbol) &
                    (self.angel.instruments['exch_seg'] == 'NFO') &
                    (self.angel.instruments['instrumenttype'] == 'OPTSTK'))
            matches = self.angel.instruments[mask]
            if len(matches) == 0:
                return None

            today = datetime.now().date()
            expiries = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True).dt.date
            future = expiries[expiries >= today]
            if len(future) == 0:
                return None
            nearest = future.min()
            return nearest.strftime('%d%b%Y').upper()
        except Exception:
            return None

    # ================================================================
    # PUBLIC API — Thread-safe getters
    # ================================================================

    def get_pcr(self, symbol):
        """Get current Put/Call Ratio for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            float: PCR value, or None if not available.
        """
        with self._lock:
            return self._stock_pcr.get(symbol)

    def get_oi_data(self, symbol):
        """Get current OI data for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            dict: {total_call_oi, total_put_oi} or None.
        """
        with self._lock:
            return self._stock_oi.get(symbol)

    def get_oi_change(self, symbol, lookback_minutes=30):
        """Get OI change over the last N minutes.

        Compares current OI to the reading from lookback_minutes ago.

        Args:
            symbol: Stock symbol.
            lookback_minutes: How far back to compare (default 30 min).

        Returns:
            dict: {call_oi_change_pct, put_oi_change_pct, pcr_change} or None.
        """
        with self._lock:
            history = self._stock_oi_history.get(symbol, [])
            if len(history) < 2:
                return None

            current = history[-1]
            cutoff = datetime.now() - timedelta(minutes=lookback_minutes)

            # Find the reading closest to cutoff
            old = None
            for reading in history:
                t = datetime.fromisoformat(reading['time'])
                if t <= cutoff:
                    old = reading

            if not old:
                old = history[0]  # Use oldest available

            call_change = 0
            put_change = 0
            if old['call_oi'] > 0:
                call_change = (current['call_oi'] - old['call_oi']) / old['call_oi'] * 100
            if old['put_oi'] > 0:
                put_change = (current['put_oi'] - old['put_oi']) / old['put_oi'] * 100

            return {
                'call_oi_change_pct': round(call_change, 2),
                'put_oi_change_pct': round(put_change, 2),
                'pcr_change': round(current['pcr'] - old['pcr'], 3),
            }

    def get_volume_data(self, symbol):
        """Get current volume data for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            dict: {total_call_vol, total_put_vol} or None.
        """
        with self._lock:
            return self._stock_volume.get(symbol)

    def get_most_active_strike(self, symbol, opt_type='CE'):
        """Get the most active (highest OI) strike for a stock.

        Args:
            symbol: Stock symbol.
            opt_type: 'CE' or 'PE'.

        Returns:
            dict: {strike, oi, ltp} or None.
        """
        with self._lock:
            active = self._most_active.get(symbol)
            if not active:
                return None
            key = 'ce' if opt_type == 'CE' else 'pe'
            return active.get(key)

    def get_avg_iv(self, symbol):
        """Get average implied volatility for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            float: Average IV percentage, or None.
        """
        with self._lock:
            return self._stock_iv_avg.get(symbol)

    def get_option_chain(self, symbol):
        """Get full option chain for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            dict: {strike: {ce: {...}, pe: {...}}} or None.
        """
        with self._lock:
            return self._option_chains.get(symbol)

    def get_volume_spike(self, symbol, threshold=2.0):
        """Detect if current volume is spiking vs historical average.

        Simple check: if current session volume > threshold × average
        of previous readings.

        Args:
            symbol: Stock symbol.
            threshold: Multiplier above average to flag as spike.

        Returns:
            dict: {is_spike, current_vol, avg_vol, ratio} or None.
        """
        with self._lock:
            vol = self._stock_volume.get(symbol)
            if not vol:
                return None

            current_total = vol['total_call_vol'] + vol['total_put_vol']
            if current_total == 0:
                return {'is_spike': False, 'current_vol': 0, 'avg_vol': 0, 'ratio': 0}

            # Simple heuristic: compare to OI as proxy for "normal" activity
            oi = self._stock_oi.get(symbol, {})
            total_oi = oi.get('total_call_oi', 0) + oi.get('total_put_oi', 0)
            if total_oi == 0:
                return {'is_spike': False, 'current_vol': current_total, 'avg_vol': 0, 'ratio': 0}

            # Volume/OI ratio > 0.1 is typically active, > 0.3 is very active
            vol_oi_ratio = current_total / total_oi
            is_spike = vol_oi_ratio > 0.3

            return {
                'is_spike': is_spike,
                'current_vol': current_total,
                'total_oi': total_oi,
                'vol_oi_ratio': round(vol_oi_ratio, 4),
            }

    def get_last_fetch_time(self, symbol):
        """Get when data was last fetched for a stock.

        Args:
            symbol: Stock symbol.

        Returns:
            datetime or None.
        """
        with self._lock:
            return self._last_fetch_time.get(symbol)

    def get_pipeline_status(self):
        """Get pipeline status summary.

        Returns:
            dict with running, watchlist_size, stocks_tracked, etc.
        """
        with self._lock:
            return {
                'running': self._running,
                'watchlist_size': len(self.watchlist),
                'stocks_tracked': len(self._stock_pcr),
                'watchlist': list(self.watchlist),
                'pcr_data': dict(self._stock_pcr),
                'fetch_interval': self._fetch_interval,
            }

    def save_snapshot(self):
        """Save current pipeline data to JSON for dashboard.

        Writes to stock_paper_trades/pipeline_snapshot.json.
        Called periodically by the main trader.
        """
        with self._lock:
            snapshot = {
                'timestamp': datetime.now().isoformat(),
                'watchlist': list(self.watchlist),
                'pcr': dict(self._stock_pcr),
                'oi': {k: v for k, v in self._stock_oi.items()},
                'volume': {k: v for k, v in self._stock_volume.items()},
                'avg_iv': dict(self._stock_iv_avg),
                'most_active': {k: v for k, v in self._most_active.items()},
            }

        try:
            fpath = os.path.join(STOCK_PAPER_DIR, 'pipeline_snapshot.json')
            with open(fpath, 'w') as f:
                json.dump(snapshot, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Pipeline snapshot save error: {e}")
