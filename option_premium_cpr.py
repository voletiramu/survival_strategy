"""
Option Premium CPR — Live Module
==================================
Computes CPR levels (Pivot, BC, TC, R1, R2, S1, S2) on the OPTION PREMIUM
using previous day's option H/L/C from intraday chain snapshots or Zerodha API.

Used by WaveBC and WaveBCElite strategies in paper_trader.py.

Usage:
    cpr = OptionPremiumCPR(zerodha_feed)
    cpr.compute_all_levels()  # Call once at 9:15
    allowed, reason, levels = cpr.should_allow_entry('NIFTY', 'CE', 150.0, 'WaveBC')
    target = cpr.get_target('NIFTY', 'CE', 'WaveBC')
    sl = cpr.get_sl('NIFTY', 'CE')
"""

import logging
import os
import json
import time as _time
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path

logger = logging.getLogger(__name__)

SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}

# Data directories
DATA_DIR = Path(os.environ.get('ALGO_DATA_DIR', '/root/algo_trading/data'))
LIVE_DIR = DATA_DIR / 'live'
CACHE_FILE = DATA_DIR / 'option_cpr_cache.json'


def compute_cpr_levels(high, low, close):
    """Compute full CPR + pivot levels from option premium H/L/C."""
    if high <= 0 or low <= 0 or close <= 0 or high <= low:
        return None
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * pivot - bc
    h_range = high - low
    return {
        'pivot': round(pivot, 2),
        'bc': round(bc, 2),
        'tc': round(tc, 2),
        'r1': round(2 * pivot - low, 2),
        'r2': round(pivot + h_range, 2),
        'r3': round(close + h_range * 1.1 / 4, 2),
        's1': round(2 * pivot - high, 2),
        's2': round(pivot - h_range, 2),
        's3': round(close - h_range * 1.1 / 4, 2),
        'prev_high': round(high, 2),
        'prev_low': round(low, 2),
        'prev_close': round(close, 2),
    }


class OptionPremiumCPR:
    """Compute and serve option premium CPR levels for all indices."""

    def __init__(self, zerodha_feed=None):
        self.zerodha = zerodha_feed
        # levels[(symbol, strike, opt_type)] = {pivot, bc, tc, r1, s1, ...}
        self.levels = {}
        self.computed_date = None
        self._premium_history = {}  # (symbol, strike, opt_type) -> list of (time, ltp)

    def compute_all_levels(self, force=False):
        """Compute CPR levels for all symbols x ATM +/- 2 strikes x CE/PE.

        Called once at market open (9:15 AM).
        Sources (priority):
          1. Previous day's chain CSV from data/live/
          2. Zerodha historical API
          3. Cached levels from previous computation
        """
        today = date.today()
        if self.computed_date == today and not force:
            return  # Already computed today

        logger.info("OPTION_CPR: Computing option premium levels for %s", today)

        # Find previous trading day
        prev_date = self._find_prev_trading_day(today)
        if prev_date is None:
            logger.warning("OPTION_CPR: No previous trading day found, trying cache")
            self._load_cache()
            return

        logger.info("OPTION_CPR: Using prev day %s for CPR computation", prev_date)
        count = 0

        for symbol in SYMBOLS:
            # Method 1: Use saved chain CSV
            chain_levels = self._compute_from_chain_csv(prev_date, symbol)
            if chain_levels:
                self.levels.update(chain_levels)
                count += len(chain_levels)
                continue

            # Method 2: Use Zerodha historical API
            if self.zerodha:
                api_levels = self._compute_from_zerodha(prev_date, symbol)
                if api_levels:
                    self.levels.update(api_levels)
                    count += len(api_levels)

        self.computed_date = today
        logger.info("OPTION_CPR: Computed %d levels across %d symbols", count, len(SYMBOLS))

        # Log key levels
        for symbol in SYMBOLS:
            for ot in ['CE', 'PE']:
                lv = self._get_atm_levels(symbol, ot)
                if lv:
                    logger.info("OPTION_CPR: %s %s Pivot=%.0f BC=%.0f TC=%.0f R1=%.0f S1=%.0f",
                                symbol, ot, lv['pivot'], lv['bc'], lv['tc'], lv['r1'], lv['s1'])

        self._save_cache()

    def _find_prev_trading_day(self, today):
        """Find the most recent trading day before today from live data."""
        if LIVE_DIR.exists():
            dates = sorted([d.name for d in LIVE_DIR.iterdir()
                           if d.is_dir() and d.name < today.strftime('%Y-%m-%d')], reverse=True)
            if dates:
                return dates[0]
        # Fallback: yesterday or Friday if Monday
        delta = 1 if today.weekday() != 0 else 3
        return (today - timedelta(days=delta)).strftime('%Y-%m-%d')

    def _compute_from_chain_csv(self, date_str, symbol):
        """Compute levels from saved option chain CSV."""
        import pandas as pd
        path = LIVE_DIR / date_str / f'option_chain_{symbol}.csv'
        if not path.exists():
            return {}

        try:
            df = pd.read_csv(path, parse_dates=['timestamp'])
            if len(df) < 10:
                return {}

            levels = {}
            for (strike, opt_type), grp in df.groupby(['strike', 'type']):
                valid = grp[grp['ltp'] > 0]
                if len(valid) < 3:
                    continue
                high = valid['ltp'].max()
                low = valid['ltp'].min()
                close = valid['ltp'].iloc[-1]
                cpr = compute_cpr_levels(high, low, close)
                if cpr:
                    levels[(symbol, strike, opt_type)] = cpr

            logger.info("OPTION_CPR: %s from CSV: %d levels computed", symbol, len(levels))
            return levels
        except Exception as e:
            logger.error("OPTION_CPR: Error reading chain CSV for %s: %s", symbol, e)
            return {}

    def _compute_from_zerodha(self, date_str, symbol):
        """Compute levels using Zerodha historical API for option contracts."""
        if not self.zerodha:
            return {}

        try:
            spot = self.zerodha.get_spot(symbol)
            if not spot or spot <= 0:
                return {}

            si = STRIKE_INTERVALS.get(symbol, 100)
            atm = round(spot / si) * si
            levels = {}

            for offset in range(-2, 3):
                strike = atm + offset * si
                for ot in ['CE', 'PE']:
                    try:
                        hist = self.zerodha.get_option_historical(
                            symbol, strike, ot, interval='day', days=2)
                        if hist and len(hist) >= 1:
                            bar = hist[-1]  # Last complete day
                            cpr = compute_cpr_levels(bar['high'], bar['low'], bar['close'])
                            if cpr:
                                levels[(symbol, strike, ot)] = cpr
                        _time.sleep(0.3)  # Rate limit
                    except Exception as e:
                        logger.debug("OPTION_CPR: Zerodha hist failed %s %d %s: %s",
                                     symbol, strike, ot, e)

            logger.info("OPTION_CPR: %s from Zerodha API: %d levels", symbol, len(levels))
            return levels
        except Exception as e:
            logger.error("OPTION_CPR: Zerodha error for %s: %s", symbol, e)
            return {}

    def _get_atm_levels(self, symbol, opt_type):
        """Get levels for the nearest ATM strike."""
        matches = [(k, v) for k, v in self.levels.items()
                   if k[0] == symbol and k[2] == opt_type]
        if not matches:
            return None
        # Return the one with highest prev_close (most actively traded = ATM)
        best = max(matches, key=lambda x: x[1].get('prev_close', 0))
        return best[1]

    def get_levels(self, symbol, strike, opt_type):
        """Get CPR levels for a specific strike. Falls back to nearest."""
        key = (symbol, strike, opt_type)
        if key in self.levels:
            return self.levels[key]

        si = STRIKE_INTERVALS.get(symbol, 100)
        for off in [si, -si, 2*si, -2*si]:
            k2 = (symbol, strike + off, opt_type)
            if k2 in self.levels:
                return self.levels[k2]

        return self._get_atm_levels(symbol, opt_type)

    def should_allow_wavebc(self, symbol, strike, opt_type, premium):
        """WaveBC entry gate: premium must be above BC (just above support).

        Returns (allowed, reason, levels_dict)
        """
        lv = self.get_levels(symbol, strike, opt_type)
        if lv is None:
            return True, "no_levels", {}

        bc = lv['bc']
        if premium >= bc * 1.02:  # 2% above BC = just above support
            return True, "premium %.0f >= BC %.0f" % (premium, bc), lv
        return False, "premium %.0f < BC %.0f" % (premium, bc), lv

    def should_allow_wavebc_elite(self, symbol, strike, opt_type, premium, quality_score):
        """WaveBCElite: BC gate + quality >= 80.

        Momentum check is done separately in the caller (needs chain data).
        Returns (allowed, reason, levels_dict)
        """
        if quality_score < 80:
            return False, "quality %.0f < 80" % quality_score, {}

        return self.should_allow_wavebc(symbol, strike, opt_type, premium)

    def get_target_wavebc(self, symbol, strike, opt_type, entry_premium):
        """WaveBC target: Pivot level (conservative)."""
        lv = self.get_levels(symbol, strike, opt_type)
        if lv:
            pivot = lv['pivot']
            if pivot > entry_premium:
                return pivot
        return entry_premium * 1.2  # 20% fallback

    def get_target_wavebc_elite(self, symbol, strike, opt_type, entry_premium):
        """WaveBCElite target: R1 (more aggressive, higher quality entries)."""
        lv = self.get_levels(symbol, strike, opt_type)
        if lv:
            r1 = lv['r1']
            if r1 > entry_premium:
                return r1
        return entry_premium * 1.3  # 30% fallback

    def get_sl(self, symbol, strike, opt_type, entry_premium):
        """SL for both WaveBC strategies: just below BC (3% buffer)."""
        lv = self.get_levels(symbol, strike, opt_type)
        if lv:
            bc_sl = lv['bc'] * 0.97  # 3% below BC
            hard_sl = entry_premium * 0.75  # 25% hard SL
            return max(bc_sl, hard_sl, 0.01)
        return entry_premium * 0.75

    def update_premium_tick(self, symbol, strike, opt_type, ltp, timestamp=None):
        """Track premium ticks for momentum calculation."""
        if timestamp is None:
            timestamp = datetime.now()
        key = (symbol, strike, opt_type)
        if key not in self._premium_history:
            self._premium_history[key] = []
        hist = self._premium_history[key]
        hist.append((timestamp, ltp))
        # Keep last 20 readings
        if len(hist) > 20:
            self._premium_history[key] = hist[-20:]

    def is_premium_rising(self, symbol, strike, opt_type, n_bars=3):
        """Check if premium is rising (for WaveBCElite momentum filter)."""
        key = (symbol, strike, opt_type)
        hist = self._premium_history.get(key, [])
        if len(hist) < n_bars + 1:
            return True  # Not enough data, allow (fail-open)
        recent = [h[1] for h in hist[-(n_bars+1):]]
        return recent[-1] > recent[0]  # Last > First = rising

    def _save_cache(self):
        """Save computed levels to disk for recovery."""
        try:
            cache = {
                'date': str(self.computed_date),
                'levels': {f"{k[0]}_{k[1]}_{k[2]}": v for k, v in self.levels.items()},
            }
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(cache, indent=2))
        except Exception as e:
            logger.debug("OPTION_CPR: Cache save failed: %s", e)

    def get_ladder_exit(self, symbol, strike, opt_type, entry_premium, current_premium, phase):
        """Level-Locking Ladder exit logic.

        Phase 1: SL=BC-5%, Target=Pivot
        Phase 2: SL=Pivot-2%, Target=R1
        Phase 3: SL=R1-2%, Target=R2
        Phase 4: Hit R2 -> EXIT

        Returns: (new_phase, sl_price, target_price, exit_now, exit_reason)
        """
        lv = self.get_levels(symbol, strike, opt_type)
        if lv is None:
            # Fallback to percentage-based
            return phase, entry_premium * 0.85, entry_premium * 1.30, False, None

        # Sort levels ascending (fix inverted CPR for some options)
        bc, pivot, r1, r2 = lv['bc'], lv['pivot'], lv['r1'], lv['r2']
        sorted_lvls = sorted([bc, pivot, r1, r2])
        bc, pivot, r1, r2 = sorted_lvls[0], sorted_lvls[1], sorted_lvls[2], sorted_lvls[3]

        lock_buffer = 0.02  # 2% below locked level

        new_phase = phase
        sl_price = bc * (1 - 0.05)  # Default: BC - 5%
        target_price = pivot
        exit_now = False
        exit_reason = None

        if phase == 1:
            sl_price = bc * (1 - 0.05)
            target_price = pivot
            if current_premium >= pivot:
                new_phase = 2
                sl_price = pivot * (1 - lock_buffer)
                target_price = r1
        elif phase == 2:
            sl_price = pivot * (1 - lock_buffer)
            target_price = r1
            if current_premium >= r1:
                new_phase = 3
                sl_price = r1 * (1 - lock_buffer)
                target_price = r2
        elif phase == 3:
            sl_price = r1 * (1 - lock_buffer)
            target_price = r2
            if current_premium >= r2:
                exit_now = True
                exit_reason = 'TARGET_R2'

        # Check SL hit
        if current_premium <= sl_price:
            exit_now = True
            exit_reason = 'SL_P%d' % phase

        # Hard SL backup (25%)
        if current_premium <= entry_premium * 0.75:
            exit_now = True
            exit_reason = 'HARD_SL'

        return new_phase, sl_price, target_price, exit_now, exit_reason

    def _load_cache(self):
        """Load levels from cache."""
        try:
            if CACHE_FILE.exists():
                cache = json.loads(CACHE_FILE.read_text())
                for k_str, v in cache.get('levels', {}).items():
                    parts = k_str.rsplit('_', 2)
                    if len(parts) == 3:
                        sym, strike, ot = parts[0], float(parts[1]), parts[2]
                        self.levels[(sym, strike, ot)] = v
                logger.info("OPTION_CPR: Loaded %d cached levels from %s",
                           len(self.levels), cache.get('date'))
        except Exception as e:
            logger.debug("OPTION_CPR: Cache load failed: %s", e)
