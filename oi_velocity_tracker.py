"""
OI Velocity Tracker — Real-time OI surge detection for Gamma Blast strategy.
==============================================================================
Replaces the price-action Gamma Blast (body > ATR * 0.15) with OI velocity
detection from Raahi Bhushan's article.

Architecture:
  - Fetches option chain OI from Zerodha (Source 1) every minute
  - Tracks ATM ± 2 strikes for CE and PE
  - Computes OI change % over 10/15/30 min windows
  - Triggers signal when >50% of cells on one side are flagged

Usage in paper_trader.py:
    from oi_velocity_tracker import OIVelocityTracker
    tracker = OIVelocityTracker(zerodha_feed, symbol_config)
    signal = tracker.check_signal(symbol, spot, dte)
"""

import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

# OI velocity thresholds (from backtest: "Aggressive" config was best for NIFTY)
OI_WINDOWS = [10, 15, 30]          # minutes
OI_THRESHOLDS = [0.10, 0.15, 0.25] # 10%/15%/25% change
BLAST_TRIGGER_PCT = 0.50            # 50% of cells must be flagged
MIN_OI = 1000                       # ignore strikes with OI < 1000 (noise)

# Strike config per symbol
STRIKE_CONFIG = {
    'NIFTY':     {'interval': 50,   'n_strikes': 2, 'lot_size': 75},
    'BANKNIFTY': {'interval': 100,  'n_strikes': 2, 'lot_size': 30},
    'SENSEX':    {'interval': 100,  'n_strikes': 2, 'lot_size': 20},
    'GOLDM':     {'interval': 1000, 'n_strikes': 1, 'lot_size': 10},
    'SILVERM':   {'interval': 1000, 'n_strikes': 1, 'lot_size': 5},
    'CRUDEOILM': {'interval': 100,  'n_strikes': 1, 'lot_size': 10},
}

# How often to fetch OI (seconds). Aligned with 1-min candle boundaries.
OI_FETCH_INTERVAL = 60

# Exit parameters (from backtest: Aggressive config best for NIFTY)
# Target 80%, SL 40%, Time exit 60min, Trail after 20% at 50% of peak
GAMMA_OI_TARGET_MULT = 1.80   # 80% gain
GAMMA_OI_SL_MULT = 0.60       # 40% loss
GAMMA_OI_TIME_EXIT_MIN = 60   # max 60 min hold
GAMMA_OI_TRAIL_TRIGGER = 0.20 # start trailing after 20% gain
GAMMA_OI_TRAIL_DISTANCE = 0.50 # trail at 50% of peak

# Exclude Fridays (consistently negative in backtest)
EXCLUDE_FRIDAY = True


@dataclass
class OISnapshot:
    """Single OI reading for a strike."""
    timestamp: datetime
    strike: int
    opt_type: str  # 'CE' or 'PE'
    oi: int
    ltp: float = 0.0


class OIVelocityTracker:
    """Tracks OI velocity across strikes and generates Gamma Blast signals.

    Integrates with the existing paper_trader data pipeline:
    - Uses zerodha_feed.get_option_chain() for OI data (Source 1)
    - Falls back to truedata_feed or market_pipeline
    - Caches internally to avoid redundant API calls
    """

    def __init__(self):
        # {symbol: {(strike, opt_type): deque of (timestamp, oi)}}
        self.oi_history: Dict[str, Dict[Tuple[int, str], deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=120))  # 2 hours of minute data
        )
        self.last_fetch: Dict[str, datetime] = {}
        self._signal_cooldown: Dict[str, datetime] = {}  # per-symbol cooldown

    def update_oi(self, symbol: str, spot: float, option_chain: dict, timestamp: datetime = None):
        """Update OI history from a live option chain snapshot.

        Args:
            symbol: e.g., 'NIFTY'
            spot: current spot price
            option_chain: {'CE': [{'strike': N, 'oi': N, 'ltp': N}, ...],
                           'PE': [{'strike': N, 'oi': N, 'ltp': N}, ...]}
            timestamp: override timestamp (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now()

        config = STRIKE_CONFIG.get(symbol)
        if not config:
            return

        interval = config['interval']
        n_strikes = config['n_strikes']
        atm = round(spot / interval) * interval

        # Target strikes: ATM ± n_strikes
        target_strikes = set()
        for i in range(-n_strikes, n_strikes + 1):
            target_strikes.add(int(atm + i * interval))

        history = self.oi_history[symbol]

        for opt_type in ['CE', 'PE']:
            chain_side = option_chain.get(opt_type, [])
            for entry in chain_side:
                strike = int(entry.get('strike', 0))
                oi = int(entry.get('oi', 0))
                if strike in target_strikes and oi > 0:
                    history[(strike, opt_type)].append((timestamp, oi))

        self.last_fetch[symbol] = timestamp

    def get_velocity(self, symbol: str, strike: int, opt_type: str,
                     timestamp: datetime, window_minutes: int) -> Optional[float]:
        """Calculate OI change % over a time window."""
        history = self.oi_history[symbol].get((strike, opt_type))
        if not history or len(history) < 2:
            return None

        target_time = timestamp - timedelta(minutes=window_minutes)
        current_oi = None
        past_oi = None

        # Walk backward through history
        for ts, oi in reversed(history):
            if ts <= timestamp and current_oi is None:
                current_oi = oi
            if ts <= target_time and past_oi is None:
                past_oi = oi
                break

        if current_oi is None or past_oi is None or past_oi < MIN_OI:
            return None

        return (current_oi - past_oi) / past_oi

    def check_blast(self, symbol: str, spot: float, timestamp: datetime = None) -> dict:
        """Check if gamma blast conditions are met for a symbol.

        Returns:
            {
                'signal': 'BUY_CE' | 'BUY_PE' | 'STRADDLE' | None,
                'ce_pct': float,  # % of CE cells flagged
                'pe_pct': float,  # % of PE cells flagged
                'ce_flagged': int,
                'pe_flagged': int,
                'ce_total': int,
                'pe_total': int,
                'details': list,
            }
        """
        if timestamp is None:
            timestamp = datetime.now()

        config = STRIKE_CONFIG.get(symbol)
        if not config:
            return {'signal': None}

        interval = config['interval']
        n_strikes = config['n_strikes']
        atm = round(spot / interval) * interval

        strikes = [int(atm + i * interval) for i in range(-n_strikes, n_strikes + 1)]

        ce_flags = 0
        pe_flags = 0
        ce_total = 0
        pe_total = 0
        details = []

        for strike in strikes:
            for opt_type in ['CE', 'PE']:
                for window, threshold in zip(OI_WINDOWS, OI_THRESHOLDS):
                    velocity = self.get_velocity(symbol, strike, opt_type, timestamp, window)
                    if velocity is None:
                        continue

                    if opt_type == 'CE':
                        ce_total += 1
                    else:
                        pe_total += 1

                    if abs(velocity) >= threshold:
                        if opt_type == 'CE':
                            ce_flags += 1
                        else:
                            pe_flags += 1
                        details.append({
                            'strike': strike,
                            'type': opt_type,
                            'window': window,
                            'velocity': velocity,
                            'threshold': threshold,
                        })

        ce_pct = ce_flags / ce_total if ce_total > 0 else 0
        pe_pct = pe_flags / pe_total if pe_total > 0 else 0

        signal = None
        if ce_pct >= BLAST_TRIGGER_PCT and pe_pct < BLAST_TRIGGER_PCT:
            signal = 'BUY_CE'
        elif pe_pct >= BLAST_TRIGGER_PCT and ce_pct < BLAST_TRIGGER_PCT:
            signal = 'BUY_PE'
        elif ce_pct >= BLAST_TRIGGER_PCT and pe_pct >= BLAST_TRIGGER_PCT:
            signal = 'STRADDLE'  # both sides — skip for now

        return {
            'signal': signal,
            'ce_pct': ce_pct, 'pe_pct': pe_pct,
            'ce_flagged': ce_flags, 'pe_flagged': pe_flags,
            'ce_total': ce_total, 'pe_total': pe_total,
            'details': details,
        }

    def should_fetch(self, symbol: str) -> bool:
        """Check if enough time has passed since last OI fetch."""
        last = self.last_fetch.get(symbol)
        if last is None:
            return True
        return (datetime.now() - last).total_seconds() >= OI_FETCH_INTERVAL

    def check_cooldown(self, symbol: str, cooldown_seconds: int = 600) -> bool:
        """Check if symbol is in post-signal cooldown."""
        cd = self._signal_cooldown.get(symbol)
        if cd is None:
            return False
        return (datetime.now() - cd).total_seconds() < cooldown_seconds

    def set_cooldown(self, symbol: str):
        """Set cooldown after signal generated."""
        self._signal_cooldown[symbol] = datetime.now()

    def cleanup(self, symbol: str, keep_minutes: int = 60):
        """Remove OI history older than keep_minutes."""
        cutoff = datetime.now() - timedelta(minutes=keep_minutes)
        history = self.oi_history.get(symbol, {})
        for key in list(history.keys()):
            while history[key] and history[key][0][0] < cutoff:
                history[key].popleft()

    def get_status(self, symbol: str) -> str:
        """Return human-readable status for logging."""
        history = self.oi_history.get(symbol, {})
        n_strikes = len(set(k[0] for k in history.keys()))
        n_points = sum(len(v) for v in history.values())
        last = self.last_fetch.get(symbol)
        last_str = last.strftime('%H:%M:%S') if last else 'never'
        return f"OI_TRACKER[{symbol}]: {n_strikes} strikes, {n_points} data points, last fetch: {last_str}"


# --- Helper: Generate signal dict compatible with paper_trader ---------------

def make_gamma_blast_signal(blast_result: dict, symbol: str, spot: float,
                            strike: int, premium: float, greeks: dict,
                            dte: float, is_expiry: bool) -> Optional[dict]:
    """Convert OI blast result into a paper_trader signal dict.

    Returns signal dict compatible with paper_trader's execute_signal() method,
    or None if conditions not met.
    """
    if blast_result['signal'] is None or blast_result['signal'] == 'STRADDLE':
        return None

    # Friday filter
    if EXCLUDE_FRIDAY and datetime.now().weekday() == 4:
        logger.info(f"  GAMMA_OI_SKIP: {symbol} Friday excluded (backtest shows negative)")
        return None

    opt_type = 'CE' if blast_result['signal'] == 'BUY_CE' else 'PE'
    sig_type = f'BUY_{opt_type}_GAMMA'

    surge_pct = blast_result['ce_pct'] if opt_type == 'CE' else blast_result['pe_pct']
    flagged = blast_result['ce_flagged'] if opt_type == 'CE' else blast_result['pe_flagged']
    total = blast_result['ce_total'] if opt_type == 'CE' else blast_result['pe_total']

    # Use backtest-optimized exit params
    if is_expiry:
        target_mult = GAMMA_OI_TARGET_MULT
        sl_mult = GAMMA_OI_SL_MULT
    else:
        # Slightly tighter on non-expiry
        target_mult = min(GAMMA_OI_TARGET_MULT, 1.60)
        sl_mult = max(GAMMA_OI_SL_MULT, 0.65)

    day_label = "EXPIRY" if is_expiry else f"{dte:.0f}DTE"

    return {
        'type': sig_type,
        'strike': strike,
        'premium': premium,
        'greeks': greeks,
        'reason': f"Gamma Blast OI [{day_label}]: {opt_type} surge {surge_pct:.0%} "
                  f"({flagged}/{total} cells) — OI velocity trigger [LIVE]",
        'target': premium * target_mult,
        'sl': premium * sl_mult,
    }
