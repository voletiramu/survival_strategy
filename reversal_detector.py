"""
V-REVERSAL DETECTOR v1.0
=========================
Replaces fixed 900s DIRECTION_FLIP cooldown with smart 5-step confirmation.

5-Step Confirmation:
  1. V-Shape Detection — find trough/peak, measure recovery %
  2. Minimum Thresholds — 0.3% drop, 50% recovery, 4 bars, 90s
  3. Calculus Confirmation — momentum + acceleration must flip and sustain
  4. OI Heatmap Confirmation — directional bias from OI must align
  5. Confidence Scoring — 0-100, entry threshold >= 55

Safety: max 3 flips/symbol/day, 45s minimum between flips.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# === V-Reversal Parameters ===
MIN_DROP_PCT = 0.30            # Minimum initial drop to qualify as V-pattern (%)
MIN_RECOVERY_PCT = 50.0        # % of drop that must be recovered
MIN_CONFIRMATION_BARS = 4      # Consecutive bars above recovery threshold
MIN_CONFIRMATION_SECS = 90     # Minimum wall-clock confirmation time (seconds)
MAX_CONFIRMATION_WAIT = 300    # Max seconds to wait before marking FAILED
REVERSAL_CONFIDENCE_THRESHOLD = 60  # v24: MC-optimal 55-65 range (CPR+Wave=60)

# === Safety Limits ===
MAX_FLIPS_PER_SYMBOL_PER_DAY = 3
MIN_GAP_BETWEEN_FLIPS_SECS = 45

# === OI Weights ===
OI_CONFIRM_BONUS = 20          # Confidence bonus when OI aligns
OI_CONTRADICT_PENALTY = -10    # Confidence penalty when OI contradicts


class ReversalDetector:
    """Detects genuine V-reversals using price action + momentum + OI confirmation."""

    def __init__(self, calculus=None, oi_heatmap=None, dhan_feed=None):
        """
        Args:
            calculus: MarketCalculus instance (provides _bars, momentum, acceleration, direction_signal)
            oi_heatmap: OIHeatmap instance (provides build_heatmap, get_directional_bias)
            dhan_feed: DhanFeed instance (optional, for direct chain access)
        """
        self.calculus = calculus
        self.oi_heatmap = oi_heatmap
        self.dhan_feed = dhan_feed

        # Per-symbol reversal tracking state
        self._reversal_state = {}  # symbol -> dict
        self._flip_counts = {}     # symbol -> int (today's count)
        self._last_flip_time = {}  # symbol -> datetime
        self._last_spot = {}       # symbol -> float

    # ------------------------------------------------------------------ #
    #  PUBLIC API                                                         #
    # ------------------------------------------------------------------ #

    def check_reversal(self, symbol, spot, direction_after_flip):
        """Check if a V-reversal is confirmed for re-entry after DIRECTION_FLIP.

        Args:
            symbol: 'NIFTY', 'BANKNIFTY', 'SENSEX'
            spot: current spot price
            direction_after_flip: 'CE' or 'PE' — the direction we want to re-enter

        Returns:
            {
                'is_reversal': bool,
                'confidence': 0-100,
                'direction': str,
                'recovery_pct': float,
                'confirmation_time_secs': int,
                'oi_confirmed': bool,
                'diagnostics': dict
            }
        """
        result = {
            'is_reversal': False,
            'confidence': 0,
            'direction': direction_after_flip,
            'recovery_pct': 0.0,
            'confirmation_time_secs': 0,
            'oi_confirmed': False,
            'diagnostics': {},
        }

        try:
            # Step 1: V-Shape Detection
            v_shape = self._detect_v_shape(symbol, spot, direction_after_flip)
            result['diagnostics']['v_shape'] = v_shape

            if not v_shape['valid']:
                return result

            result['recovery_pct'] = v_shape['recovery_pct']

            # Step 2: Minimum Thresholds
            thresholds = self._check_thresholds(symbol, v_shape)
            result['diagnostics']['thresholds'] = thresholds

            if not thresholds['passed']:
                return result

            result['confirmation_time_secs'] = thresholds.get('confirmation_secs', 0)

            # Step 3: Calculus Confirmation
            calc_ok, calc_diag = self._check_calculus_confirmation(symbol, spot, direction_after_flip)
            result['diagnostics']['calculus'] = calc_diag

            # Step 4: OI Heatmap Confirmation
            oi_ok, oi_bonus, oi_diag = self._check_oi_confirmation(symbol, spot, direction_after_flip)
            result['diagnostics']['oi'] = oi_diag
            result['oi_confirmed'] = oi_ok

            # Step 5: Confidence Scoring
            confidence = self._compute_confidence(
                v_shape, thresholds, calc_diag, oi_bonus)
            result['confidence'] = confidence
            result['diagnostics']['confidence_breakdown'] = {
                'recovery': min(30, v_shape['recovery_pct'] * 0.4),
                'momentum': 15 if calc_diag.get('momentum_aligned') else 0,
                'acceleration': 15 if calc_diag.get('accel_aligned') else 0,
                'direction_signal': calc_diag.get('dir_signal_pts', 0),
                'oi_bonus': oi_bonus,
                'sustain_bonus': min(10, thresholds.get('confirmation_secs', 0) // 15),
            }

            if confidence >= REVERSAL_CONFIDENCE_THRESHOLD:
                result['is_reversal'] = True

        except Exception as e:
            logger.debug(f"[ReversalDetector] {symbol} check failed: {e}")
            result['diagnostics']['error'] = str(e)

        return result

    def update_spot(self, symbol, spot):
        """Called every scan cycle (1s) to update internal tracking state."""
        self._last_spot[symbol] = spot

        state = self._reversal_state.get(symbol)
        if not state or state['phase'] == 'FAILED' or state['phase'] == 'CONFIRMED':
            return

        # Update recovery tracking
        if state['initial_direction'] == 'DOWN':
            # Bullish V: recovery = how far above trough
            if state['peak_before'] != state['trough_price']:
                recovery = (spot - state['trough_price']) / (state['peak_before'] - state['trough_price']) * 100
            else:
                recovery = 0
        else:
            # Bearish V: recovery = how far below peak
            if state['peak_before'] != state['trough_price']:
                recovery = (state['trough_price'] - spot) / (state['trough_price'] - state['peak_before']) * 100
            else:
                recovery = 0

        state['recovery_pct'] = max(0, recovery)

        # Update confirmation tracking
        if recovery >= MIN_RECOVERY_PCT:
            state['confirmation_checks'] += 1
            if state['phase'] == 'DETECTED':
                state['phase'] = 'CONFIRMING'
                state['confirming_since'] = datetime.now()
        else:
            # Recovery dropped below threshold — reset confirmation
            state['confirmation_checks'] = 0
            if state['phase'] == 'CONFIRMING':
                state['phase'] = 'DETECTED'

        # Timeout check
        elapsed = (datetime.now() - state['detected_at']).total_seconds()
        if elapsed > MAX_CONFIRMATION_WAIT and state['phase'] != 'CONFIRMED':
            state['phase'] = 'FAILED'

    def is_flip_allowed(self, symbol):
        """Check if another flip is allowed for this symbol today."""
        count = self._flip_counts.get(symbol, 0)
        if count >= MAX_FLIPS_PER_SYMBOL_PER_DAY:
            return False
        last = self._last_flip_time.get(symbol)
        if last and (datetime.now() - last).total_seconds() < MIN_GAP_BETWEEN_FLIPS_SECS:
            return False
        return True

    def record_flip(self, symbol):
        """Record that a flip occurred (call after confirmed re-entry)."""
        self._flip_counts[symbol] = self._flip_counts.get(symbol, 0) + 1
        self._last_flip_time[symbol] = datetime.now()
        # Mark state as confirmed
        if symbol in self._reversal_state:
            self._reversal_state[symbol]['phase'] = 'CONFIRMED'

    def reset_day(self):
        """Reset all state for a new trading day."""
        self._reversal_state.clear()
        self._flip_counts.clear()
        self._last_flip_time.clear()
        self._last_spot.clear()

    # ------------------------------------------------------------------ #
    #  STEP 1: V-SHAPE DETECTION                                         #
    # ------------------------------------------------------------------ #

    def _detect_v_shape(self, symbol, spot, direction):
        """Find V-shape pattern from bar data.

        For CE (bullish V): sharp drop followed by recovery upward.
        For PE (bearish V): sharp rally followed by recovery downward.
        """
        result = {'valid': False, 'drop_pct': 0, 'recovery_pct': 0,
                  'trough_price': 0, 'peak_before': 0}

        if not self.calculus:
            return result

        bars = self.calculus._bars.get(symbol)
        if not bars or len(bars.get('close', [])) < 10:
            return result

        prices = bars['close']
        n = len(prices)
        lookback = min(20, n)  # Last ~10 minutes of 30s-deduped bars
        recent = prices[-lookback:]

        if direction == 'CE':
            # Bullish V: find the trough (low point) and peak before it
            trough_idx = 0
            trough_val = recent[0]
            for i, p in enumerate(recent):
                if p < trough_val:
                    trough_val = p
                    trough_idx = i

            # Peak before trough
            peak_val = max(recent[:max(trough_idx, 1)])
            drop_pct = (peak_val - trough_val) / peak_val * 100 if peak_val > 0 else 0
            recovery_pct = (spot - trough_val) / (peak_val - trough_val) * 100 if (peak_val - trough_val) > 0 else 0

        else:  # PE — bearish V
            # Bearish V: find the peak (high point) and trough before it
            peak_idx = 0
            peak_val = recent[0]
            for i, p in enumerate(recent):
                if p > peak_val:
                    peak_val = p
                    peak_idx = i

            trough_val = min(recent[:max(peak_idx, 1)])
            drop_pct = (peak_val - trough_val) / trough_val * 100 if trough_val > 0 else 0
            recovery_pct = (trough_val - spot) / (trough_val - peak_val) * 100 if (trough_val - peak_val) != 0 else 0
            # Swap for naming consistency (trough = starting point)
            trough_val, peak_val = peak_val, trough_val

        if drop_pct < MIN_DROP_PCT:
            return result

        result['valid'] = True
        result['drop_pct'] = round(drop_pct, 3)
        result['recovery_pct'] = round(max(0, recovery_pct), 1)
        result['trough_price'] = round(trough_val, 2)
        result['peak_before'] = round(peak_val, 2)

        # Initialize or update tracking state
        if symbol not in self._reversal_state or self._reversal_state[symbol]['phase'] in ('FAILED', 'CONFIRMED'):
            self._reversal_state[symbol] = {
                'phase': 'DETECTED',
                'detected_at': datetime.now(),
                'trough_price': trough_val,
                'peak_before': peak_val,
                'initial_direction': 'DOWN' if direction == 'CE' else 'UP',
                'recovery_pct': recovery_pct,
                'confirmation_checks': 0,
                'confirming_since': None,
            }

        return result

    # ------------------------------------------------------------------ #
    #  STEP 2: MINIMUM THRESHOLDS                                        #
    # ------------------------------------------------------------------ #

    def _check_thresholds(self, symbol, v_shape):
        """Check if V-shape meets minimum confirmation thresholds."""
        result = {'passed': False, 'confirmation_secs': 0, 'confirmation_bars': 0}

        if v_shape['recovery_pct'] < MIN_RECOVERY_PCT:
            result['reason'] = f"recovery {v_shape['recovery_pct']:.1f}% < {MIN_RECOVERY_PCT}%"
            return result

        state = self._reversal_state.get(symbol)
        if not state:
            result['reason'] = 'no tracking state'
            return result

        # Check confirmation bars
        result['confirmation_bars'] = state.get('confirmation_checks', 0)
        if result['confirmation_bars'] < MIN_CONFIRMATION_BARS:
            result['reason'] = f"bars {result['confirmation_bars']} < {MIN_CONFIRMATION_BARS}"
            return result

        # Check confirmation time
        confirming_since = state.get('confirming_since')
        if confirming_since:
            result['confirmation_secs'] = int((datetime.now() - confirming_since).total_seconds())
        else:
            result['confirmation_secs'] = 0

        if result['confirmation_secs'] < MIN_CONFIRMATION_SECS:
            result['reason'] = f"time {result['confirmation_secs']}s < {MIN_CONFIRMATION_SECS}s"
            return result

        result['passed'] = True
        return result

    # ------------------------------------------------------------------ #
    #  STEP 3: CALCULUS CONFIRMATION                                     #
    # ------------------------------------------------------------------ #

    def _check_calculus_confirmation(self, symbol, spot, direction):
        """Check momentum, acceleration, and direction signal alignment."""
        diag = {
            'momentum_aligned': False,
            'accel_aligned': False,
            'dir_signal_aligned': False,
            'dir_signal_pts': 0,
        }

        if not self.calculus:
            return False, diag

        try:
            momentum = self.calculus.compute_momentum(symbol)
            acceleration = self.calculus.compute_acceleration(symbol)

            # Momentum must align with new direction
            if direction == 'CE':
                diag['momentum_aligned'] = momentum > 0
                diag['accel_aligned'] = acceleration > 0
            else:  # PE
                diag['momentum_aligned'] = momentum < 0
                diag['accel_aligned'] = acceleration < 0

            diag['momentum'] = round(momentum, 4)
            diag['acceleration'] = round(acceleration, 6)

            # Direction signal composite check
            dir_sig = self.calculus.direction_signal(symbol, spot)
            if dir_sig:
                diag['dir_signal_direction'] = dir_sig.get('direction')
                diag['dir_signal_confidence'] = dir_sig.get('confidence', 0)
                if dir_sig.get('direction') == direction:
                    diag['dir_signal_aligned'] = True
                    diag['dir_signal_pts'] = min(20, int(dir_sig.get('confidence', 0) * 0.3))

        except Exception as e:
            diag['error'] = str(e)

        all_ok = diag['momentum_aligned'] and diag['accel_aligned']
        return all_ok, diag

    # ------------------------------------------------------------------ #
    #  STEP 4: OI HEATMAP CONFIRMATION                                   #
    # ------------------------------------------------------------------ #

    def _check_oi_confirmation(self, symbol, spot, direction):
        """Check if OI heatmap directional bias aligns with reversal direction."""
        diag = {'available': False, 'bias': 'UNKNOWN'}
        oi_bonus = 0

        if not self.oi_heatmap:
            return False, 0, diag

        try:
            heatmap = self.oi_heatmap.build_heatmap(symbol, spot)
            if not heatmap:
                return False, 0, diag

            diag['available'] = True
            bias = self.oi_heatmap.get_directional_bias(heatmap)
            diag['bias'] = bias

            expected_bias = 'BULLISH' if direction == 'CE' else 'BEARISH'
            opposite_bias = 'BEARISH' if direction == 'CE' else 'BULLISH'

            if bias == expected_bias:
                oi_bonus = OI_CONFIRM_BONUS
                diag['aligned'] = True
            elif bias == opposite_bias:
                oi_bonus = OI_CONTRADICT_PENALTY
                diag['aligned'] = False
            else:
                oi_bonus = 0
                diag['aligned'] = None  # neutral

        except Exception as e:
            diag['error'] = str(e)

        return oi_bonus > 0, oi_bonus, diag

    # ------------------------------------------------------------------ #
    #  STEP 5: CONFIDENCE SCORING                                        #
    # ------------------------------------------------------------------ #

    def _compute_confidence(self, v_shape, thresholds, calc_diag, oi_bonus):
        """Compute final confidence score (0-100)."""
        confidence = 0

        # Recovery strength: 0-30 pts (50% = 20pts, 75% = 30pts cap)
        confidence += min(30, v_shape['recovery_pct'] * 0.4)

        # Momentum aligned: 15 pts
        if calc_diag.get('momentum_aligned'):
            confidence += 15

        # Acceleration aligned: 15 pts
        if calc_diag.get('accel_aligned'):
            confidence += 15

        # Direction signal: 0-20 pts
        confidence += calc_diag.get('dir_signal_pts', 0)

        # OI confirmation: -10 to +20 pts
        confidence += oi_bonus

        # Sustained confirmation: 0-10 pts (1pt per 15s held)
        confirmation_secs = thresholds.get('confirmation_secs', 0)
        confidence += min(10, confirmation_secs // 15)

        return int(max(0, min(100, confidence)))
