# market_regime.py — v10.3c
"""Classifies intraday market regime for adaptive trading parameters.

Detects whether each symbol is in a TRENDING, SIDEWAYS, or FLAT regime
based on price action, efficiency ratio, and VIX. Used by paper_trader.py
and commodity_paper_trader.py to dynamically adjust:
- TSL trail distance (wider on trending, tighter on flat)
- BREAKOUT_FAIL (disabled on trending days)
- Re-entry cooldowns (shorter on trending)

v10.3c fixes:
- Session open is now stored separately (not lost when deque rolls)
- Hysteresis: regime must persist for MIN_REGIME_HOLD cycles before switching
- Larger rolling window (240 readings = ~60min equity, ~40min commodity)
- Added regime_info() for dashboard/logging

Usage:
    from market_regime import RegimeDetector, MarketRegime, get_regime_params
    detector = RegimeDetector()
    detector.update('NIFTY', spot, vix)
    regime = detector.get_regime('NIFTY')
    params = get_regime_params(regime)
"""

from enum import Enum
from collections import deque
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Minimum cycles a new regime must persist before we actually switch
# At ~15s per cycle, 20 cycles = ~5 minutes of confirmation
MIN_REGIME_HOLD = 20

# Rolling window size: 240 readings at 15s = ~60 min for equity
HISTORY_MAXLEN = 240


class MarketRegime(Enum):
    TRENDING = 'TRENDING'       # Strong directional move (like Mar 11 BN -1000pts)
    SIDEWAYS = 'SIDEWAYS'       # Range-bound, choppy, mixed signals
    FLAT = 'FLAT'               # No movement, low volatility


class RegimeDetector:
    """Classifies market regime per symbol based on rolling price action.

    Call update() every scan cycle (~15s equity, ~10s commodity).
    Maintains rolling history and recalculates regime on each update.

    Key improvements in v10.3c:
    - Session open stored separately (not lost from rolling deque)
    - Hysteresis: new regime must persist MIN_REGIME_HOLD cycles before switching
    - Larger history window for more stable classification
    """

    def __init__(self):
        self._spot_history = {}      # {symbol: deque([price, ...], maxlen=HISTORY_MAXLEN)}
        self._session_open = {}      # {symbol: float} — true session open price
        self._regime = {}            # {symbol: MarketRegime}
        self._pending_regime = {}    # {symbol: MarketRegime} — candidate regime
        self._pending_count = {}     # {symbol: int} — how many cycles candidate has persisted
        self._vix = None
        self._regime_change_count = {}  # {symbol: int} — track total transitions

    def update(self, symbol, spot, vix=None):
        """Update regime for a symbol with latest spot price.

        Args:
            symbol: NIFTY, BANKNIFTY, SENSEX, GOLDM, etc.
            spot: Current spot/futures price
            vix: India VIX value (optional, shared across symbols)
        """
        if symbol not in self._spot_history:
            self._spot_history[symbol] = deque(maxlen=HISTORY_MAXLEN)
            self._session_open[symbol] = spot  # Remember true session open
        self._spot_history[symbol].append(spot)
        if vix is not None:
            self._vix = vix

        # Calculate what the regime SHOULD be
        candidate = self._classify(symbol)

        # Apply hysteresis: only switch if candidate persists for MIN_REGIME_HOLD cycles
        current_regime = self._regime.get(symbol, MarketRegime.SIDEWAYS)

        if candidate != current_regime:
            # Candidate is different from current — track how long
            if self._pending_regime.get(symbol) == candidate:
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
            else:
                # New candidate, reset counter
                self._pending_regime[symbol] = candidate
                self._pending_count[symbol] = 1

            # Only switch if candidate has persisted long enough
            if self._pending_count.get(symbol, 0) >= MIN_REGIME_HOLD:
                self._regime[symbol] = candidate
                self._pending_regime[symbol] = None
                self._pending_count[symbol] = 0
                self._regime_change_count[symbol] = self._regime_change_count.get(symbol, 0) + 1
                logger.info(f"  REGIME_CHANGE: {symbol} -> {candidate.value} "
                           f"(change #{self._regime_change_count[symbol]})")
        else:
            # Candidate matches current — reset pending
            self._pending_regime[symbol] = None
            self._pending_count[symbol] = 0

        # Special case: if still at default SIDEWAYS (never classified), allow faster transition
        if symbol not in self._regime:
            self._regime[symbol] = candidate

    def get_regime(self, symbol):
        """Get current regime for a symbol. Defaults to SIDEWAYS if unknown."""
        return self._regime.get(symbol, MarketRegime.SIDEWAYS)

    def regime_info(self, symbol):
        """Get detailed regime info for logging/dashboard.

        Returns:
            dict with regime, move_pct, efficiency, vix, pending info
        """
        prices = list(self._spot_history.get(symbol, []))
        if len(prices) < 10:
            return {'regime': 'SIDEWAYS', 'move_pct': 0, 'efficiency': 0,
                    'vix': self._vix, 'data_points': len(prices)}

        session_open = self._session_open.get(symbol, prices[0])
        current = prices[-1]
        total_move_pct = abs(current - session_open) / session_open * 100 if session_open > 0 else 0

        abs_moves = sum(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))
        net_move = abs(current - session_open)
        efficiency = net_move / abs_moves if abs_moves > 0 else 0

        pending = self._pending_regime.get(symbol)
        pending_count = self._pending_count.get(symbol, 0)

        return {
            'regime': self.get_regime(symbol).value,
            'move_pct': round(total_move_pct, 3),
            'efficiency': round(efficiency, 3),
            'vix': self._vix,
            'data_points': len(prices),
            'session_open': session_open,
            'current': current,
            'pending_regime': pending.value if pending else None,
            'pending_cycles': pending_count,
            'total_changes': self._regime_change_count.get(symbol, 0),
        }

    def _classify(self, symbol):
        """Classify regime based on 3 metrics: move %, efficiency, VIX.

        Uses true session open (stored separately) rather than first element
        of rolling deque, which shifts as old data drops off.
        """
        prices = list(self._spot_history[symbol])
        if len(prices) < 10:
            return MarketRegime.SIDEWAYS  # Not enough data yet

        # --- Metric 1: Total directional move from TRUE session open ---
        session_open = self._session_open.get(symbol, prices[0])
        current = prices[-1]
        if session_open == 0:
            return MarketRegime.SIDEWAYS
        total_move_pct = abs(current - session_open) / session_open * 100

        # --- Metric 2: Efficiency ratio (net move / sum of absolute moves) ---
        # Use last 60 readings (~15 min equity) for efficiency calculation
        # This avoids very old data polluting the efficiency metric
        recent = prices[-min(60, len(prices)):]
        abs_moves = sum(abs(recent[i] - recent[i-1]) for i in range(1, len(recent)))
        net_move = abs(recent[-1] - recent[0])
        efficiency = net_move / abs_moves if abs_moves > 0 else 0

        # --- Metric 3: VIX level ---
        vix_high = self._vix is not None and self._vix > 18

        # Classification thresholds
        if total_move_pct >= 0.8 and efficiency >= 0.5:
            regime = MarketRegime.TRENDING
        elif total_move_pct < 0.3 and efficiency < 0.3:
            regime = MarketRegime.FLAT
        else:
            regime = MarketRegime.SIDEWAYS

        # VIX override: high VIX + decent move = TRENDING
        if vix_high and total_move_pct >= 0.6:
            regime = MarketRegime.TRENDING

        return regime


def get_regime_params(regime):
    """Return TSL/BREAKOUT_FAIL parameters adjusted for market regime.

    Args:
        regime: MarketRegime enum value

    Returns:
        dict with keys: tsl_trail_distance_pct, tsl_tight_distance_pct,
        breakout_fail_enabled, breakout_fail_min_gain_pct, reentry_cooldown_seconds
    """
    if regime == MarketRegime.TRENDING:
        return {
            'tsl_trail_distance_pct': 20,      # Wider trail (was 30) — let winners run
            'tsl_tight_distance_pct': 15,      # Wider tight (was 20)
            'breakout_fail_enabled': False,     # DISABLE breakout fail on trending days
            'breakout_fail_min_gain_pct': 0,   # N/A when disabled
            'reentry_cooldown_seconds': 300,   # Shorter cooldown (5 min vs 10)
        }
    elif regime == MarketRegime.FLAT:
        return {
            'tsl_trail_distance_pct': 40,      # Tighter trail — take what you can
            'tsl_tight_distance_pct': 25,
            'breakout_fail_enabled': True,
            'breakout_fail_min_gain_pct': 3,   # Stricter — need 3% in 5 min
            'reentry_cooldown_seconds': 900,   # Longer cooldown (15 min)
        }
    else:  # SIDEWAYS — current defaults
        return {
            'tsl_trail_distance_pct': 30,
            'tsl_tight_distance_pct': 20,
            'breakout_fail_enabled': True,
            'breakout_fail_min_gain_pct': 2,
            'reentry_cooldown_seconds': 600,
        }
