# market_regime.py — v10.3
"""Classifies intraday market regime for adaptive trading parameters.

Detects whether each symbol is in a TRENDING, SIDEWAYS, or FLAT regime
based on price action, efficiency ratio, and VIX. Used by paper_trader.py
and commodity_paper_trader.py to dynamically adjust:
- TSL trail distance (wider on trending, tighter on flat)
- BREAKOUT_FAIL (disabled on trending days)
- Re-entry cooldowns (shorter on trending)

Usage:
    from market_regime import RegimeDetector, MarketRegime, get_regime_params
    detector = RegimeDetector()
    detector.update('NIFTY', spot, vix)
    regime = detector.get_regime('NIFTY')
    params = get_regime_params(regime)
"""

from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    TRENDING = 'TRENDING'       # Strong directional move (like Mar 11 BN -1000pts)
    SIDEWAYS = 'SIDEWAYS'       # Range-bound, choppy, mixed signals
    FLAT = 'FLAT'               # No movement, low volatility


class RegimeDetector:
    """Classifies market regime per symbol based on rolling price action.

    Call update() every scan cycle (~15s equity, ~10s commodity).
    Maintains rolling history and recalculates regime on each update.
    """

    def __init__(self):
        self._spot_history = {}      # {symbol: deque([price, ...], maxlen=100)}
        self._regime = {}            # {symbol: MarketRegime}
        self._vix = None
        self._regime_change_count = {}  # {symbol: int} — track stability

    def update(self, symbol, spot, vix=None):
        """Update regime for a symbol with latest spot price.

        Args:
            symbol: NIFTY, BANKNIFTY, SENSEX, GOLDM, etc.
            spot: Current spot/futures price
            vix: India VIX value (optional, shared across symbols)
        """
        if symbol not in self._spot_history:
            self._spot_history[symbol] = deque(maxlen=100)
        self._spot_history[symbol].append(spot)
        if vix is not None:
            self._vix = vix

        old_regime = self._regime.get(symbol)
        new_regime = self._classify(symbol)
        self._regime[symbol] = new_regime

        # Track regime changes for stability
        if old_regime and old_regime != new_regime:
            self._regime_change_count[symbol] = self._regime_change_count.get(symbol, 0) + 1

    def get_regime(self, symbol):
        """Get current regime for a symbol. Defaults to SIDEWAYS if unknown."""
        return self._regime.get(symbol, MarketRegime.SIDEWAYS)

    def _classify(self, symbol):
        """Classify regime based on 3 metrics: move %, efficiency, VIX."""
        prices = list(self._spot_history[symbol])
        if len(prices) < 10:
            return MarketRegime.SIDEWAYS  # Not enough data yet

        # --- Metric 1: Total directional move from session open ---
        session_open = prices[0]
        current = prices[-1]
        if session_open == 0:
            return MarketRegime.SIDEWAYS
        total_move_pct = abs(current - session_open) / session_open * 100

        # --- Metric 2: Efficiency ratio (net move / sum of absolute moves) ---
        # High efficiency = trending (moves in one direction)
        # Low efficiency = choppy (lots of back-and-forth)
        abs_moves = sum(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))
        net_move = abs(current - session_open)
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
