"""
Trade Intelligence Engine — Probabilistic trade monitoring & smart exits.
==========================================================================
Replaces fixed-percentage TSL with adaptive, mathematically-grounded system.

Three pillars:
  1. ENTRY FILTER: Bayesian quality gate (reduce trades from 15/day to 3-5)
  2. TRADE MONITOR: Real-time P(win) estimation using premium calculus
  3. SMART EXIT: ATR-based adaptive trailing + expected value optimization

Key concepts:
  - Premium velocity dp/dt: is the option premium accelerating in our favor?
  - P(win|data): Bayesian probability that this trade will hit target
  - E[V]: Expected value of holding vs. exiting NOW
  - ATR-multiple trailing: stop distance adapts to market volatility

Based on analysis of 59 trades:
  - Trades <10 min: -Rs 25,953 (100% losers) → need longer grace period
  - TSL captures 8.7% median of peak → need ATR-based adaptive trail
  - Win rate 30.5% → need stricter entry filter (target 45%+)
  - Holds >30 min: +Rs 28,304 → patience pays, protect it mathematically
"""

import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import logging

logger = logging.getLogger('trade_intelligence')


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

# Entry filter
MIN_QUALITY_SCORE = 65          # Was 50 → raise to 65 (proven: score>60 has 52% WR vs 18% for <60)
MIN_CONFLUENCE_SIGNALS = 2      # Need 2+ strategies agreeing on direction
MIN_DCI_SCORE = 55              # Direction Confidence Index minimum

# Grace period
GRACE_PERIOD_SECONDS = 300      # Was 180s (3 min) → 5 min. Trades <3 min are 100% losers.

# Premium calculus
PREMIUM_LOOKBACK = 10           # Number of premium readings for dp/dt
PREMIUM_ACCEL_LOOKBACK = 6      # Readings for d²p/dt² (acceleration)

# ATR-based trailing stop
ATR_TRAIL_WARMUP_BARS = 5       # Minimum bars before ATR trail activates
ATR_TRAIL_MULTIPLIER = 2.5      # Stop at 2.5× ATR below peak (Chandelier exit)
ATR_TIGHT_MULTIPLIER = 1.5      # Tighten to 1.5× ATR after 30%+ gain
ATR_MIN_DISTANCE_PCT = 3.0      # Never trail closer than 3% from current price

# Expected value thresholds
EV_EXIT_THRESHOLD = -0.02       # Exit if E[V] < -2% of position value
EV_RECHECK_INTERVAL = 60        # Recompute E[V] every 60 seconds
PWIN_EXIT_THRESHOLD = 0.20      # Exit if P(win) drops below 20%

# Hold-time based behavior
PATIENCE_WINDOW_MINS = 45       # Give trades 45 min before any momentum-based exit
MAX_HOLD_HOURS = 4              # Maximum hold before forced evaluation


class TradeIntelligence:
    """Real-time probabilistic trade management.

    Tracks each open position's premium history and computes:
    - dp/dt (premium momentum): is option premium moving in our favor?
    - d²p/dt² (premium acceleration): is momentum building or fading?
    - P(win): Bayesian probability of reaching target
    - E[V]: Expected value of continued holding vs. immediate exit
    - ATR-adaptive trailing stop: volatility-aware stop placement
    """

    def __init__(self):
        # Per-position tracking: {position_id: PremiumTracker}
        self._trackers = {}
        # Historical win rate data for Bayesian priors
        self._prior_stats = {
            'base_win_rate': 0.35,      # Prior from observed data
            'avg_win_pct': 0.25,        # Average winner gains 25%
            'avg_loss_pct': -0.15,      # Average loser drops 15%
            'momentum_win_boost': 0.15, # Positive momentum adds 15% to P(win)
            'accel_win_boost': 0.10,    # Positive acceleration adds 10%
        }

    def register_position(self, pos_id, entry_premium, target, sl, lot_size,
                          num_lots, is_buy=True, symbol='', strategy='',
                          spot_atr=None):
        """Register a new position for intelligent monitoring.

        Args:
            pos_id: Unique position identifier
            entry_premium: Entry price of the option
            target: Target premium
            sl: Initial stop loss premium
            lot_size: Contract lot size
            num_lots: Number of lots
            is_buy: True for BUY, False for SELL
            symbol: Underlying symbol (NIFTY, BANKNIFTY, etc.)
            strategy: Strategy name (CPR, Gamma, etc.)
            spot_atr: ATR of the underlying (for ATR-based trailing)
        """
        self._trackers[pos_id] = PremiumTracker(
            pos_id=pos_id,
            entry_premium=entry_premium,
            target=target,
            sl=sl,
            lot_size=lot_size,
            num_lots=num_lots,
            is_buy=is_buy,
            symbol=symbol,
            strategy=strategy,
            spot_atr=spot_atr or (entry_premium * 0.05),  # Default 5% of premium
        )
        logger.info(f"[TI] Registered {pos_id}: entry={entry_premium:.1f} "
                     f"target={target:.1f} SL={sl:.1f} ATR={spot_atr or 'est'}")

    def update(self, pos_id, current_premium, current_spot=None, spot_atr=None):
        """Feed new premium data for a position.

        Call this every scan cycle (10-15 seconds).
        Returns a TradeSignal with recommended action.
        """
        tracker = self._trackers.get(pos_id)
        if not tracker:
            return TradeSignal(action='HOLD', reason='unregistered')

        tracker.add_reading(current_premium, current_spot, spot_atr)
        return tracker.evaluate()

    def get_status(self, pos_id):
        """Get current intelligence status for a position."""
        tracker = self._trackers.get(pos_id)
        if not tracker:
            return {}
        return tracker.get_status()

    def remove_position(self, pos_id):
        """Remove a closed position from tracking."""
        self._trackers.pop(pos_id, None)

    # ─── ENTRY FILTER ─────────────────────────────────────────────────────────

    def should_enter(self, signal, existing_positions=None):
        """Bayesian entry filter: should we take this trade?

        Returns (allowed: bool, reason: str, adjusted_quality: float)
        """
        quality = signal.get('quality_score', 0)
        dci = signal.get('_dci', signal.get('dci', 0))
        symbol = signal.get('symbol', signal.get('commodity', ''))
        strategy = signal.get('strategy', '')
        signal_type = signal.get('type', signal.get('signal_type', ''))

        # Gate 1: Quality score minimum
        if quality < MIN_QUALITY_SCORE:
            return False, f"QUALITY_GATE: {quality} < {MIN_QUALITY_SCORE}", quality

        # Gate 2: DCI minimum
        if dci < MIN_DCI_SCORE:
            return False, f"DCI_GATE: {dci:.0f} < {MIN_DCI_SCORE}", quality

        # Gate 3: Time-of-day filter (first 10 min after open = noise)
        now = datetime.now()
        market_open = now.replace(hour=9, minute=15, second=0)
        if now - market_open < timedelta(minutes=10):
            return False, "TIME_GATE: first 10 min after open (noise)", quality

        # Gate 4: Last 30 min before close — only take if very high quality
        market_close = now.replace(hour=15, minute=30, second=0)
        if symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM'):
            market_close = now.replace(hour=23, minute=30, second=0)
        if market_close - now < timedelta(minutes=30) and quality < 80:
            return False, f"CLOSE_GATE: {quality} < 80 in last 30 min", quality

        # Gate 5: Max concurrent positions per symbol
        if existing_positions:
            same_symbol = [p for p in existing_positions
                          if p.get('symbol', p.get('commodity', '')) == symbol]
            if len(same_symbol) >= 2:
                return False, f"CONC_GATE: {len(same_symbol)} positions already in {symbol}", quality

        # Gate 6: Regime filter — don't enter in SIDEWAYS regime if quality < 70
        regime = signal.get('_regime', '')
        if 'SIDEWAYS' in str(regime).upper() and quality < 70:
            return False, f"REGIME_GATE: SIDEWAYS + quality {quality} < 70", quality

        # Bayesian adjustment: boost quality based on historical performance
        adjusted_quality = quality
        if symbol == 'NIFTY':
            adjusted_quality += 5   # NIFTY has positive edge historically
        if symbol in ('BANKNIFTY', 'SENSEX'):
            adjusted_quality -= 10  # These are losing symbols

        return True, f"ENTRY_OK: quality={adjusted_quality:.0f} dci={dci:.0f}", adjusted_quality


class PremiumTracker:
    """Tracks premium history and computes calculus-based signals for one position."""

    def __init__(self, pos_id, entry_premium, target, sl, lot_size, num_lots,
                 is_buy, symbol, strategy, spot_atr):
        self.pos_id = pos_id
        self.entry_premium = entry_premium
        self.target = target
        self.initial_sl = sl
        self.lot_size = lot_size
        self.num_lots = num_lots
        self.is_buy = is_buy
        self.symbol = symbol
        self.strategy = strategy
        self.spot_atr = spot_atr

        self.entry_time = datetime.now()
        self.readings = []          # [(timestamp, premium, spot)]
        self.peak_premium = entry_premium
        self.trough_premium = entry_premium

        # ATR-based adaptive trailing stop
        self.atr_trail_stop = sl    # Starts at initial SL
        self.trail_phase = 0        # 0=initial, 1=breakeven, 2=trailing, 3=tight

        # Probabilistic state
        self.p_win = 0.35           # Prior probability
        self.expected_value = 0.0
        self.last_ev_time = None

        # Premium ATR (volatility of the premium itself)
        self.premium_atr = 0.0
        self.premium_changes = []   # For computing option premium ATR

    def add_reading(self, premium, spot=None, spot_atr=None):
        """Add a new premium reading."""
        now = datetime.now()
        self.readings.append((now, premium, spot))

        # Update peak/trough
        if self.is_buy:
            self.peak_premium = max(self.peak_premium, premium)
            self.trough_premium = min(self.trough_premium, premium)
        else:
            self.peak_premium = min(self.peak_premium, premium)  # For sells, peak = lowest
            self.trough_premium = max(self.trough_premium, premium)

        # Update premium ATR (volatility of option price)
        if len(self.readings) >= 2:
            change = abs(premium - self.readings[-2][1])
            self.premium_changes.append(change)
            if len(self.premium_changes) > 20:
                self.premium_changes.pop(0)
            self.premium_atr = np.mean(self.premium_changes) if self.premium_changes else 0

        # Update spot ATR if provided
        if spot_atr:
            self.spot_atr = spot_atr

        # Keep only last 100 readings to save memory
        if len(self.readings) > 100:
            self.readings = self.readings[-100:]

    def evaluate(self):
        """Evaluate position and return recommended action.

        Returns TradeSignal with:
          action: 'HOLD', 'EXIT', 'TIGHTEN_SL'
          reason: Human-readable reason
          new_sl: Suggested new stop loss (if TIGHTEN_SL)
          p_win: Current win probability
          ev: Expected value of holding
          momentum: dp/dt of premium
          acceleration: d²p/dt² of premium
        """
        n = len(self.readings)
        if n < 3:
            return TradeSignal(action='HOLD', reason='warmup (need 3+ readings)')

        current_premium = self.readings[-1][1]
        hold_mins = (datetime.now() - self.entry_time).total_seconds() / 60

        # ─── 1. Premium Calculus ──────────────────────────────────────────
        momentum = self._compute_premium_momentum()
        acceleration = self._compute_premium_acceleration()
        trend_score = self._compute_trend_score()

        # ─── 2. ATR-Based Adaptive Trailing Stop ─────────────────────────
        new_trail = self._compute_atr_trail(current_premium, hold_mins)

        # ─── 3. Win Probability P(win|data) ──────────────────────────────
        self.p_win = self._compute_p_win(current_premium, momentum, acceleration, hold_mins)

        # ─── 4. Expected Value E[V] ──────────────────────────────────────
        self.expected_value = self._compute_expected_value(current_premium)

        # ─── 5. Decision Logic ────────────────────────────────────────────

        # During patience window (first 45 min) — only exit on hard SL hit
        if hold_mins < PATIENCE_WINDOW_MINS:
            if self._hit_hard_sl(current_premium):
                return TradeSignal(
                    action='EXIT', reason=f'HARD_SL_HIT (patience window)',
                    p_win=self.p_win, ev=self.expected_value,
                    momentum=momentum, acceleration=acceleration
                )
            # Update trail but don't exit on trail during patience
            if new_trail:
                return TradeSignal(
                    action='TIGHTEN_SL', reason=f'ATR_TRAIL_UPDATE (patience)',
                    new_sl=new_trail, p_win=self.p_win, ev=self.expected_value,
                    momentum=momentum, acceleration=acceleration
                )
            return TradeSignal(
                action='HOLD',
                reason=f'PATIENCE ({hold_mins:.0f}/{PATIENCE_WINDOW_MINS}min) '
                       f'mom={momentum:+.2f} p_win={self.p_win:.0%}',
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # After patience window — full intelligence active
        # Exit condition 1: ATR trailing stop hit
        if self._hit_atr_trail(current_premium):
            return TradeSignal(
                action='EXIT',
                reason=f'ATR_TRAIL_HIT phase={self.trail_phase} '
                       f'trail={self.atr_trail_stop:.1f} prem={current_premium:.1f}',
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # Exit condition 2: P(win) collapsed
        if self.p_win < PWIN_EXIT_THRESHOLD and self._is_losing(current_premium):
            return TradeSignal(
                action='EXIT',
                reason=f'PWIN_COLLAPSE: P(win)={self.p_win:.0%} < {PWIN_EXIT_THRESHOLD:.0%} + losing',
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # Exit condition 3: Negative expected value (after patience)
        if self.expected_value < EV_EXIT_THRESHOLD and hold_mins > 60:
            return TradeSignal(
                action='EXIT',
                reason=f'NEG_EV: E[V]={self.expected_value:+.1%} < {EV_EXIT_THRESHOLD:+.1%} after {hold_mins:.0f}min',
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # Exit condition 4: Momentum reversal (premium decelerating against us after big gain)
        gain_pct = self._gain_pct(current_premium)
        if gain_pct > 20 and momentum < 0 and acceleration < 0 and self.is_buy:
            # Premium was rising, now decelerating — lock profit
            return TradeSignal(
                action='EXIT',
                reason=f'MOMENTUM_REVERSAL: gain={gain_pct:.1f}% but dp/dt={momentum:+.2f} d²p/dt²={acceleration:+.3f}',
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # Tighten SL if warranted
        if new_trail:
            return TradeSignal(
                action='TIGHTEN_SL',
                reason=f'ATR_TRAIL phase={self.trail_phase} '
                       f'new_sl={new_trail:.1f} mom={momentum:+.2f} p_win={self.p_win:.0%}',
                new_sl=new_trail,
                p_win=self.p_win, ev=self.expected_value,
                momentum=momentum, acceleration=acceleration
            )

        # Default: HOLD
        return TradeSignal(
            action='HOLD',
            reason=f'HOLD mom={momentum:+.2f} accel={acceleration:+.3f} '
                   f'p_win={self.p_win:.0%} EV={self.expected_value:+.1%} '
                   f'gain={gain_pct:+.1f}% trend={trend_score:+.1f}',
            p_win=self.p_win, ev=self.expected_value,
            momentum=momentum, acceleration=acceleration
        )

    # ═══════════════════════════════════════════════════════════════════════
    # PREMIUM CALCULUS
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_premium_momentum(self):
        """dp/dt — rate of premium change in Rs/minute.

        Positive = premium rising (good for BUY, bad for SELL).
        Uses weighted backward difference over PREMIUM_LOOKBACK readings.
        """
        n = len(self.readings)
        lookback = min(PREMIUM_LOOKBACK, n - 1)
        if lookback < 2:
            return 0.0

        # Time-weighted momentum: recent readings matter more
        recent = self.readings[-lookback:]
        t0, p0, _ = recent[0]
        tn, pn, _ = recent[-1]
        dt_mins = max((tn - t0).total_seconds() / 60, 0.1)

        raw_momentum = (pn - p0) / dt_mins  # Rs/minute

        # Normalize by entry premium to make comparable across price levels
        if self.entry_premium > 0:
            normalized = raw_momentum / self.entry_premium * 100  # %/minute
        else:
            normalized = 0

        # For SELL positions, invert (falling premium = good)
        if not self.is_buy:
            normalized = -normalized

        return normalized

    def _compute_premium_acceleration(self):
        """d²p/dt² — rate of momentum change.

        Positive acceleration + positive momentum = trade strengthening.
        Negative acceleration + positive momentum = trade weakening (prepare to exit).
        """
        n = len(self.readings)
        lookback = min(PREMIUM_ACCEL_LOOKBACK, n - 1)
        if lookback < 4:
            return 0.0

        recent = self.readings[-lookback:]
        h = lookback // 2

        t0, p0, _ = recent[0]
        th, ph, _ = recent[h]
        tn, pn, _ = recent[-1]

        dt1 = max((th - t0).total_seconds() / 60, 0.1)
        dt2 = max((tn - th).total_seconds() / 60, 0.1)

        mom1 = (ph - p0) / dt1
        mom2 = (pn - ph) / dt2

        accel = (mom2 - mom1) / ((dt1 + dt2) / 2)

        # Normalize
        if self.entry_premium > 0:
            accel = accel / self.entry_premium * 100
        if not self.is_buy:
            accel = -accel

        return accel

    def _compute_trend_score(self):
        """Trend strength: what fraction of recent readings are in our favor?

        Returns -1 (fully against) to +1 (fully in favor).
        """
        n = len(self.readings)
        if n < 5:
            return 0.0

        lookback = min(20, n)
        recent = self.readings[-lookback:]

        favorable = 0
        for i in range(1, len(recent)):
            if self.is_buy:
                if recent[i][1] > recent[i-1][1]:
                    favorable += 1
                elif recent[i][1] < recent[i-1][1]:
                    favorable -= 1
            else:
                if recent[i][1] < recent[i-1][1]:
                    favorable += 1
                elif recent[i][1] > recent[i-1][1]:
                    favorable -= 1

        return favorable / (len(recent) - 1)

    # ═══════════════════════════════════════════════════════════════════════
    # ATR-BASED ADAPTIVE TRAILING STOP (Chandelier Exit variant)
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_atr_trail(self, current_premium, hold_mins):
        """Compute ATR-based trailing stop.

        Unlike fixed-percentage trailing:
        - Stop distance adapts to market volatility (wider in volatile, tighter in calm)
        - Uses Chandelier Exit: peak - (multiplier × ATR)
        - Three phases: Initial → Breakeven → Adaptive Trail

        Returns new trail stop or None if no change.
        """
        if len(self.readings) < ATR_TRAIL_WARMUP_BARS:
            return None

        # Compute premium ATR (volatility of the option price)
        prem_atr = self.premium_atr
        if prem_atr <= 0:
            prem_atr = self.entry_premium * 0.02  # Default 2% of entry

        gain_pct = self._gain_pct(current_premium)
        old_trail = self.atr_trail_stop

        if self.is_buy:
            # Phase transitions based on gain
            if gain_pct >= 30 and self.trail_phase < 3:
                # TIGHT phase: 1.5× premium ATR from peak
                self.trail_phase = 3
                new_trail = self.peak_premium - ATR_TIGHT_MULTIPLIER * prem_atr
                logger.info(f"[TI] {self.pos_id} → TIGHT trail: peak={self.peak_premium:.1f} "
                           f"ATR={prem_atr:.1f} trail={new_trail:.1f}")

            elif gain_pct >= 15 and self.trail_phase < 2:
                # ADAPTIVE phase: 2.5× premium ATR from peak
                self.trail_phase = 2
                new_trail = self.peak_premium - ATR_TRAIL_MULTIPLIER * prem_atr
                logger.info(f"[TI] {self.pos_id} → ADAPTIVE trail: peak={self.peak_premium:.1f} "
                           f"ATR={prem_atr:.1f} trail={new_trail:.1f}")

            elif gain_pct >= 8 and self.trail_phase < 1:
                # BREAKEVEN phase: lock at entry + 2% buffer
                self.trail_phase = 1
                new_trail = self.entry_premium * 1.02
                logger.info(f"[TI] {self.pos_id} → BREAKEVEN trail: {new_trail:.1f}")

            else:
                # Update existing trail based on current phase
                if self.trail_phase == 3:
                    new_trail = self.peak_premium - ATR_TIGHT_MULTIPLIER * prem_atr
                elif self.trail_phase == 2:
                    new_trail = self.peak_premium - ATR_TRAIL_MULTIPLIER * prem_atr
                elif self.trail_phase == 1:
                    new_trail = max(self.entry_premium * 1.02, old_trail)
                else:
                    return None  # Phase 0: use initial SL

            # Trail can never move down (ratchet up only)
            new_trail = max(new_trail, old_trail)

            # Minimum distance: never closer than 3% of current price
            min_stop = current_premium * (1 - ATR_MIN_DISTANCE_PCT / 100)
            new_trail = min(new_trail, min_stop)

            if new_trail > old_trail:
                self.atr_trail_stop = new_trail
                return new_trail

        else:
            # SELL position: mirror logic (trail above current price)
            if gain_pct >= 30 and self.trail_phase < 3:
                self.trail_phase = 3
                new_trail = self.peak_premium + ATR_TIGHT_MULTIPLIER * prem_atr
            elif gain_pct >= 15 and self.trail_phase < 2:
                self.trail_phase = 2
                new_trail = self.peak_premium + ATR_TRAIL_MULTIPLIER * prem_atr
            elif gain_pct >= 8 and self.trail_phase < 1:
                self.trail_phase = 1
                new_trail = self.entry_premium * 0.98
            else:
                if self.trail_phase >= 2:
                    mult = ATR_TIGHT_MULTIPLIER if self.trail_phase == 3 else ATR_TRAIL_MULTIPLIER
                    new_trail = self.peak_premium + mult * prem_atr
                else:
                    return None

            new_trail = min(new_trail, old_trail) if old_trail > 0 else new_trail
            max_stop = current_premium * (1 + ATR_MIN_DISTANCE_PCT / 100)
            new_trail = max(new_trail, max_stop)

            if new_trail < old_trail or old_trail <= 0:
                self.atr_trail_stop = new_trail
                return new_trail

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # BAYESIAN WIN PROBABILITY
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_p_win(self, current_premium, momentum, acceleration, hold_mins):
        """P(win|current_data) — Bayesian probability of reaching target.

        Starts with prior (base_win_rate=35%) and updates based on:
        1. Distance to target vs SL (risk-reward position)
        2. Premium momentum (dp/dt in favorable direction)
        3. Premium acceleration (d²p/dt² — momentum strengthening?)
        4. Time decay (longer hold without progress → lower P)
        5. Trend score (what fraction of recent bars are favorable)
        """
        prior = self._prior_stats['base_win_rate']

        # Factor 1: Distance ratio (how close to target vs SL?)
        if self.is_buy:
            dist_to_target = max(self.target - current_premium, 0)
            dist_to_sl = max(current_premium - self.atr_trail_stop, 0.01)
        else:
            dist_to_target = max(current_premium - self.target, 0)
            dist_to_sl = max(self.atr_trail_stop - current_premium, 0.01)

        rr_ratio = dist_to_target / dist_to_sl if dist_to_sl > 0 else 10
        # Closer to target = higher P(win)
        if rr_ratio < 0.5:
            distance_factor = 0.20    # Very close to target
        elif rr_ratio < 1.0:
            distance_factor = 0.10    # Closer to target than SL
        elif rr_ratio < 2.0:
            distance_factor = 0.0     # Neutral
        elif rr_ratio < 3.0:
            distance_factor = -0.10   # Closer to SL
        else:
            distance_factor = -0.20   # Very close to SL

        # Factor 2: Momentum (dp/dt in %/min)
        if momentum > 0.5:
            mom_factor = 0.15        # Strong favorable momentum
        elif momentum > 0.1:
            mom_factor = 0.08        # Moderate favorable
        elif momentum > -0.1:
            mom_factor = 0.0         # Neutral
        elif momentum > -0.5:
            mom_factor = -0.08       # Moderate adverse
        else:
            mom_factor = -0.15       # Strong adverse momentum

        # Factor 3: Acceleration
        if momentum > 0 and acceleration > 0:
            accel_factor = 0.10      # Accelerating in our favor
        elif momentum > 0 and acceleration < 0:
            accel_factor = -0.05     # Decelerating (momentum fading)
        elif momentum < 0 and acceleration > 0:
            accel_factor = 0.05      # Adverse momentum decelerating (potential reversal)
        elif momentum < 0 and acceleration < 0:
            accel_factor = -0.10     # Accelerating against us
        else:
            accel_factor = 0.0

        # Factor 4: Time decay (option trades have diminishing edge)
        if hold_mins < 15:
            time_factor = 0.05       # Early: slight boost (trade developing)
        elif hold_mins < 45:
            time_factor = 0.0        # Normal window
        elif hold_mins < 90:
            time_factor = -0.05      # Getting late
        elif hold_mins < 180:
            time_factor = -0.10      # Late — theta eating value
        else:
            time_factor = -0.15      # Very late

        # Factor 5: Trend score
        trend = self._compute_trend_score()
        trend_factor = trend * 0.10  # Max ±10%

        # Factor 6: Current gain position
        gain_pct = self._gain_pct(current_premium)
        if gain_pct > 15:
            gain_factor = 0.10       # Already profitable → momentum likely
        elif gain_pct > 5:
            gain_factor = 0.05
        elif gain_pct > -5:
            gain_factor = 0.0
        elif gain_pct > -15:
            gain_factor = -0.05
        else:
            gain_factor = -0.15      # Deep underwater

        # Combine: Bayesian update
        p_win = prior + distance_factor + mom_factor + accel_factor + time_factor + trend_factor + gain_factor

        # Clamp to [0.05, 0.95]
        p_win = max(0.05, min(0.95, p_win))

        return p_win

    # ═══════════════════════════════════════════════════════════════════════
    # EXPECTED VALUE
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_expected_value(self, current_premium):
        """E[V] = P(win) × potential_gain + P(loss) × potential_loss.

        Normalized as percentage of current position value.
        Tells us: is it mathematically worth holding?
        """
        if self.is_buy:
            potential_gain = (self.target - current_premium) * self.lot_size * self.num_lots
            potential_loss = (current_premium - self.atr_trail_stop) * self.lot_size * self.num_lots
        else:
            potential_gain = (current_premium - self.target) * self.lot_size * self.num_lots
            potential_loss = (self.atr_trail_stop - current_premium) * self.lot_size * self.num_lots

        potential_loss = abs(potential_loss)
        position_value = current_premium * self.lot_size * self.num_lots

        if position_value <= 0:
            return 0.0

        ev = (self.p_win * potential_gain - (1 - self.p_win) * potential_loss) / position_value
        return ev

    # ═══════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _gain_pct(self, current_premium):
        """Current gain as percentage of entry premium."""
        if self.entry_premium <= 0:
            return 0
        if self.is_buy:
            return (current_premium - self.entry_premium) / self.entry_premium * 100
        else:
            return (self.entry_premium - current_premium) / self.entry_premium * 100

    def _is_losing(self, current_premium):
        """Is the position currently underwater?"""
        return self._gain_pct(current_premium) < 0

    def _hit_hard_sl(self, current_premium):
        """Has the initial hard SL been hit?"""
        if self.is_buy:
            return current_premium <= self.initial_sl
        else:
            return current_premium >= self.initial_sl

    def _hit_atr_trail(self, current_premium):
        """Has the ATR trailing stop been hit?"""
        if self.trail_phase == 0:
            return False  # No trail active yet
        if self.is_buy:
            return current_premium <= self.atr_trail_stop
        else:
            return current_premium >= self.atr_trail_stop

    def get_status(self):
        """Get complete status dict for logging/display."""
        current = self.readings[-1][1] if self.readings else self.entry_premium
        hold_mins = (datetime.now() - self.entry_time).total_seconds() / 60

        return {
            'pos_id': self.pos_id,
            'symbol': self.symbol,
            'strategy': self.strategy,
            'entry_premium': self.entry_premium,
            'current_premium': current,
            'peak_premium': self.peak_premium,
            'gain_pct': self._gain_pct(current),
            'hold_mins': hold_mins,
            'trail_phase': self.trail_phase,
            'atr_trail_stop': self.atr_trail_stop,
            'premium_atr': self.premium_atr,
            'p_win': self.p_win,
            'expected_value': self.expected_value,
            'momentum': self._compute_premium_momentum(),
            'acceleration': self._compute_premium_acceleration(),
            'trend_score': self._compute_trend_score(),
            'n_readings': len(self.readings),
        }


class TradeSignal:
    """Action recommendation from TradeIntelligence."""

    def __init__(self, action='HOLD', reason='', new_sl=None,
                 p_win=0, ev=0, momentum=0, acceleration=0):
        self.action = action        # 'HOLD', 'EXIT', 'TIGHTEN_SL'
        self.reason = reason
        self.new_sl = new_sl
        self.p_win = p_win
        self.ev = ev
        self.momentum = momentum
        self.acceleration = acceleration

    def __repr__(self):
        return f"TradeSignal({self.action}: {self.reason})"
