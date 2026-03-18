"""
Market Calculus Engine — Differential & Integral calculus on live market data.
=============================================================================
Replaces stale 5-day VWAP and static DCI with real-time calculus:
  - Intraday VWAP (integral): sum(TP*Vol) / sum(Vol) from 09:15
  - Momentum dp/dt (differential): rate of spot price change
  - Acceleration d2p/dt2 (2nd derivative): is momentum increasing or decreasing?
  - Direction signal: composite score from VWAP position + momentum + acceleration + EMA
"""

import numpy as np
import pandas as pd
from datetime import datetime, time as dtime
from collections import defaultdict
import logging

logger = logging.getLogger('market_calculus')


class MarketCalculus:
    """Maintains per-symbol intraday bar history and computes calculus-based signals."""

    def __init__(self, lookback_momentum=5, lookback_accel=10, ema_fast=9, ema_slow=21):
        self.lookback_momentum = lookback_momentum
        self.lookback_accel = lookback_accel
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow

        # Per-symbol intraday bar storage: {symbol: {'times': [], 'close': [], 'volume': []}}
        self._bars = defaultdict(lambda: {'times': [], 'close': [], 'volume': [],
                                          'high': [], 'low': [], 'open': []})
        self._last_bar_date = {}  # {symbol: date} — reset bars on new day

    def reset_day(self, symbol):
        """Clear intraday bars for a new trading day."""
        self._bars[symbol] = {'times': [], 'close': [], 'volume': [],
                              'high': [], 'low': [], 'open': []}
        self._last_bar_date[symbol] = datetime.now().date()

    def add_bar(self, symbol, timestamp, open_p, high, low, close, volume=0):
        """Add a new price bar (called every scan cycle or on candle close).

        Auto-resets bars on new day.
        """
        today = datetime.now().date()
        if self._last_bar_date.get(symbol) != today:
            self.reset_day(symbol)

        bars = self._bars[symbol]
        bars['times'].append(timestamp)
        bars['open'].append(open_p)
        bars['high'].append(high)
        bars['low'].append(low)
        bars['close'].append(close)
        bars['volume'].append(volume)

    def add_spot_tick(self, symbol, spot, volume=0):
        """Convenience: add a spot price as a 1-bar candle (OHLC = spot)."""
        now = datetime.now()
        today = now.date()
        if self._last_bar_date.get(symbol) != today:
            self.reset_day(symbol)

        bars = self._bars[symbol]
        # Deduplicate: skip if last bar was < 30s ago
        if bars['times'] and (now - bars['times'][-1]).total_seconds() < 30:
            # Update close of last bar instead
            bars['close'][-1] = spot
            bars['high'][-1] = max(bars['high'][-1], spot)
            bars['low'][-1] = min(bars['low'][-1], spot)
            return

        bars['times'].append(now)
        bars['open'].append(spot)
        bars['high'].append(spot)
        bars['low'].append(spot)
        bars['close'].append(spot)
        bars['volume'].append(volume)

    def load_intraday_candles(self, symbol, candle_data):
        """Bulk-load intraday candles from Angel API 5-min data.

        candle_data: list of [timestamp, open, high, low, close, volume]
        """
        today = datetime.now().date()
        if self._last_bar_date.get(symbol) != today:
            self.reset_day(symbol)

        bars = self._bars[symbol]
        for c in candle_data:
            ts = c[0] if isinstance(c[0], datetime) else pd.Timestamp(c[0]).to_pydatetime()
            # Strip timezone info to avoid naive vs aware datetime errors
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            bars['times'].append(ts)
            bars['open'].append(c[1])
            bars['high'].append(c[2])
            bars['low'].append(c[3])
            bars['close'].append(c[4])
            bars['volume'].append(c[5] if len(c) > 5 else 0)

    def bar_count(self, symbol):
        """Number of intraday bars stored for symbol."""
        return len(self._bars[symbol]['close'])

    # ========================================================================
    # INTEGRAL CALCULUS: Intraday VWAP
    # ========================================================================

    def compute_intraday_vwap(self, symbol):
        """VWAP = integral(P*V)dt / integral(V)dt from market open.

        Falls back to cumulative mean if no volume data.
        Returns current VWAP value or None if no data.
        """
        bars = self._bars[symbol]
        if not bars['close']:
            return None

        closes = np.array(bars['close'])
        volumes = np.array(bars['volume'])
        highs = np.array(bars['high'])
        lows = np.array(bars['low'])

        # Typical price = (H + L + C) / 3
        tp = (highs + lows + closes) / 3

        total_vol = volumes.sum()
        if total_vol > 0:
            vwap = (tp * volumes).sum() / total_vol
        else:
            # Equal-weighted cumulative mean (uniform volume assumption)
            vwap = tp.mean()

        return float(vwap)

    # ========================================================================
    # DIFFERENTIAL CALCULUS: dp/dt (momentum) and d2p/dt2 (acceleration)
    # ========================================================================

    def compute_momentum(self, symbol):
        """dp/dt — rate of price change in points/bar.

        Uses backward difference: dp/dt = (P[t] - P[t-h]) / h
        Returns momentum value or 0 if insufficient data.
        """
        bars = self._bars[symbol]
        closes = bars['close']
        h = self.lookback_momentum

        if len(closes) < h + 1:
            return 0.0

        momentum = (closes[-1] - closes[-h - 1]) / h
        return float(momentum)

    def compute_acceleration(self, symbol):
        """d2p/dt2 — rate of momentum change.

        d2p/dt2 = (P[t] - 2*P[t-h] + P[t-2h]) / h^2
        Returns acceleration value or 0 if insufficient data.
        """
        bars = self._bars[symbol]
        closes = bars['close']
        h = self.lookback_accel // 2
        if h < 1:
            h = 1

        if len(closes) < 2 * h + 1:
            return 0.0

        accel = (closes[-1] - 2 * closes[-h - 1] + closes[-2 * h - 1]) / (h ** 2)
        return float(accel)

    def compute_ema(self, symbol, span):
        """EMA of close prices."""
        bars = self._bars[symbol]
        if len(bars['close']) < 2:
            return bars['close'][-1] if bars['close'] else 0.0

        closes = pd.Series(bars['close'])
        return float(closes.ewm(span=span, adjust=False).mean().iloc[-1])

    # ========================================================================
    # COMPOSITE DIRECTION SIGNAL
    # ========================================================================

    def direction_signal(self, symbol, spot=None):
        """Compute calculus-based direction signal.

        Returns dict with:
          direction: 'CE' or 'PE' or None (no clear signal)
          confidence: 0-100
          score: raw score (positive=bullish, negative=bearish)
          momentum, acceleration, vwap_position, ema_diff, range_position
        """
        bars = self._bars[symbol]
        n_bars = len(bars['close'])

        if n_bars < 15:
            return {'direction': None, 'confidence': 0, 'score': 0,
                    'momentum': 0, 'acceleration': 0, 'vwap_position': 0,
                    'ema_diff': 0, 'range_position': 0.5}

        current_price = spot if spot else bars['close'][-1]

        # 1. Intraday VWAP position
        vwap = self.compute_intraday_vwap(symbol)
        if vwap and vwap > 0:
            vwap_pct = (current_price - vwap) / vwap * 100
        else:
            vwap_pct = 0

        # 2. Momentum dp/dt
        momentum = self.compute_momentum(symbol)

        # 3. Acceleration d2p/dt2
        acceleration = self.compute_acceleration(symbol)

        # 4. EMA crossover
        ema_fast = self.compute_ema(symbol, self.ema_fast)
        ema_slow = self.compute_ema(symbol, self.ema_slow)
        ema_diff = (ema_fast - ema_slow) / ema_slow * 100 if ema_slow > 0 else 0

        # 5. Price relative to day's range
        day_high = max(bars['high'])
        day_low = min(bars['low'])
        day_range = day_high - day_low
        range_position = (current_price - day_low) / day_range if day_range > 0 else 0.5

        # === DIRECTION SCORING ===
        score = 0  # positive = bullish (CE), negative = bearish (PE)

        # VWAP position: above VWAP = bullish, below = bearish
        if vwap_pct > 0.15:
            score += min(vwap_pct * 8, 20)
        elif vwap_pct < -0.15:
            score += max(vwap_pct * 8, -20)

        # Momentum (dp/dt): normalize by price level
        mom_normalized = momentum / (current_price / 1000)
        score += np.clip(mom_normalized * 15, -25, 25)

        # Acceleration (d2p/dt2): momentum direction + change
        if momentum > 0 and acceleration > 0:
            score += 10   # Strong bull: momentum up and accelerating
        elif momentum < 0 and acceleration < 0:
            score -= 10   # Strong bear: momentum down and accelerating
        elif momentum < 0 and acceleration > 0:
            score += 5    # Decelerating sell-off: potential reversal UP
        elif momentum > 0 and acceleration < 0:
            score -= 5    # Decelerating rally: potential reversal DOWN

        # EMA crossover
        score += np.clip(ema_diff * 10, -15, 15)

        # Range position
        if range_position > 0.7:
            score += 10
        elif range_position < 0.3:
            score -= 10

        # Determine direction
        confidence = min(abs(score), 100)
        if score > 10:
            direction = 'CE'
        elif score < -10:
            direction = 'PE'
        else:
            direction = None

        result = {
            'direction': direction,
            'confidence': confidence,
            'score': float(score),
            'momentum': float(momentum),
            'acceleration': float(acceleration),
            'vwap_position': float(vwap_pct),
            'ema_diff': float(ema_diff),
            'range_position': float(range_position),
        }

        return result

    def get_intraday_vwap_for_indicators(self, symbol):
        """Return intraday VWAP value for use in compute_indicators().

        This replaces the stale 5-day VWAP.
        Returns None if insufficient data (caller should fall back).
        """
        if self.bar_count(symbol) < 3:
            return None
        return self.compute_intraday_vwap(symbol)

    def should_allow_direction(self, symbol, signal_type, spot):
        """Replace SKIP_FLIP_DCI: use momentum to decide if direction flip is allowed.

        Returns (allowed: bool, reason: str, calculus_data: dict)
        """
        sig = self.direction_signal(symbol, spot)

        if sig['direction'] is None:
            return True, "CALC_NEUTRAL (no strong direction)", sig

        is_ce = 'CE' in signal_type
        signal_dir = 'CE' if is_ce else 'PE'

        if sig['direction'] == signal_dir:
            return True, f"CALC_ALIGNED (score={sig['score']:+.0f}, dir={sig['direction']})", sig

        # Signal opposes calculus direction — block if confidence is strong
        if sig['confidence'] > 40:
            return False, (f"CALC_BLOCK: signal={signal_dir} but calculus={sig['direction']} "
                          f"(score={sig['score']:+.0f}, conf={sig['confidence']:.0f}, "
                          f"mom={sig['momentum']:+.2f}, vwap={sig['vwap_position']:+.2f}%)"), sig

        # Weak opposing signal — allow with warning
        return True, (f"CALC_WEAK_OPP: calculus={sig['direction']} but confidence={sig['confidence']:.0f} < 40, "
                      f"allowing {signal_dir}"), sig

    # ========================================================================
    # V12 PHYSICS GATE — Mathematical signal quality filter
    # ========================================================================
    # Backtested: blocks entries where math says momentum is dying, price at
    # wave peak, or VWAP stretched. PF 2.65 -> 3.18, blocks 346 bad signals.

    def physics_gate(self, symbol, direction, spot=None):
        """Compute physics-based entry quality score.

        Uses wave theory, calculus derivatives, and mean reversion to
        mathematically determine if NOW is the right time to enter.

        Returns (score: int, diagnostics: dict)
          score >= 0: PASS — safe to enter
          score < 0:  FAIL — math says don't enter

        Physics computed:
          1. d²p/dt² acceleration aligned with direction?
          2. Wave position — are we at a local peak about to reverse?
          3. VWAP stretch — rubber band mean reversion pressure
          4. Energy — is momentum building or dissipating?
          5. RSI extremes — overbought/oversold detection
        """
        bars = self._bars[symbol]
        prices = bars['close']
        n = len(prices)

        if n < 6:
            return 0, {'reason': 'insufficient_bars'}

        if spot is None:
            spot = prices[-1]

        score = 0
        diag = {}

        # ── 1. ACCELERATION (d²p/dt²) ──
        # Is momentum accelerating in our direction or dying?
        if n >= 5:
            v1 = prices[-3] - prices[-5]   # velocity 2 bars ago
            v2 = prices[-1] - prices[-3]   # velocity now
            accel = (v2 - v1) / spot * 10000  # normalized
            diag['accel'] = round(accel, 2)

            if direction == 'CE' and accel > 0:
                score += min(15, abs(accel) * 3)
            elif direction == 'PE' and accel < 0:
                score += min(15, abs(accel) * 3)
            else:
                # Check if momentum is DYING (1st deriv positive but 2nd negative)
                mom3 = (prices[-1] - prices[-4]) / prices[-4] * 100
                if direction == 'CE' and mom3 > 0 and accel < -2:
                    score -= 15
                    diag['mom_dying'] = True
                elif direction == 'PE' and mom3 < 0 and accel > 2:
                    score -= 15
                    diag['mom_dying'] = True

        # ── 2. WAVE POSITION ──
        # Are we at a local peak (about to mean-revert)?
        if n >= 10:
            recent = prices[-10:]
            local_max = max(recent)
            local_min = min(recent)
            wave_range = local_max - local_min
            if wave_range > 0:
                wave_pos = (spot - local_min) / wave_range
                if direction == 'CE':
                    wave_risk = wave_pos    # CE at top = risky
                else:
                    wave_risk = 1 - wave_pos  # PE at bottom = risky
                diag['wave_risk'] = round(wave_risk, 3)

                if wave_risk < 0.5:
                    score += 10   # Room to move
                elif wave_risk > 0.85:
                    score -= 15   # At wave extreme
                    diag['wave_peak'] = True

        # ── 3. VWAP STRETCH (mean reversion pressure) ──
        vwap = self.compute_intraday_vwap(symbol)
        if vwap and vwap > 0:
            vwap_dist = (spot - vwap) / vwap * 100
            if direction == 'CE':
                stretch = vwap_dist
            else:
                stretch = -vwap_dist
            diag['vwap_stretch'] = round(stretch, 3)

            if abs(stretch) < 0.1:
                score += 5    # Near equilibrium
            elif abs(stretch) > 0.3:
                score -= 10   # Stretched — rubber band will snap
                diag['stretched'] = True

        # ── 4. ENERGY (kinetic building or dissipating?) ──
        if n >= 8:
            mom_now = (prices[-1] - prices[-4]) / prices[-4] * 100
            mom_prev = (prices[-4] - prices[-7]) / prices[-7] * 100
            if abs(mom_now) > abs(mom_prev):
                score += 5    # Energy building
            else:
                score -= 5    # Energy dissipating

        # ── 5. RSI EXTREME ──
        if n >= 15:
            deltas = np.diff(prices[-15:])
            gains = deltas[deltas > 0]
            losses_arr = -deltas[deltas < 0]
            avg_gain = np.mean(gains) if len(gains) > 0 else 0
            avg_loss = np.mean(losses_arr) if len(losses_arr) > 0 else 0.001
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            diag['rsi'] = round(rsi, 1)
            if direction == 'CE' and rsi > 75:
                score -= 10
                diag['rsi_extreme'] = True
            elif direction == 'PE' and rsi < 25:
                score -= 10
                diag['rsi_extreme'] = True

        diag['score'] = score
        return score, diag

    # ---- v13.0: LIQUIDITY SWEEP (SL HUNT) DETECTOR ----
    # Detects institutional stop-loss hunting patterns:
    #   1. Velocity spike (Z >= 2.0 of recent velocity std)
    #   2. Price reversal (>= 40% retrace within 15 bars)
    #   3. Acceleration flip confirmation
    # Backtested 6 days: Q>=45 → 70 trades, WR=70%, PF=1.98, DD=0.84%
    #                    Q>=60 → 27 trades, WR=78%, PF=2.95, DD=0.88%
    # Hybrid: Q>=45 half-lot, Q>=60 full-lot

    def detect_liquidity_sweep(self, symbol, spot=None):
        """Detect SL hunt / liquidity sweep in real-time from bar data.

        Returns list of sweep signals with quality scores.
        Each signal: {'direction': 'CE'|'PE', 'quality': int, 'spike_pct': float,
                      'reversal_ratio': float, 'z_score': float, 'accel_flip': bool,
                      'diagnostics': dict}
        """
        bars = self._bars[symbol]
        prices = bars['close']
        n = len(prices)

        if n < 25:
            return []

        import numpy as np

        prices_arr = np.array(prices, dtype=float)
        velocity = np.diff(prices_arr, prepend=prices_arr[0])
        accel = np.diff(velocity, prepend=velocity[0])

        # Rolling velocity std (20-bar)
        if n >= 20:
            recent_vel = velocity[-20:]
            vel_std = np.std(recent_vel)
        else:
            vel_std = np.std(velocity[-n:])

        if vel_std == 0:
            return []

        signals = []
        # Check last 5 bars for fresh spikes
        check_start = max(20, n - 5)

        for i in range(check_start, n - 2):
            z = abs(velocity[i]) / vel_std
            if z < 2.0:
                continue

            spike_dir = 'UP' if velocity[i] > 0 else 'DOWN'

            # Multi-bar spike accumulation (1-3 bars)
            cum = 0
            spike_bars = 0
            for j in range(min(3, n - i)):
                m = velocity[i + j]
                if (spike_dir == 'UP' and m > 0) or (spike_dir == 'DOWN' and m < 0):
                    cum += m
                    spike_bars += 1
                else:
                    break
            if spike_bars == 0:
                spike_bars = 1
                cum = velocity[i]

            spike_size = abs(cum)
            spike_pct = spike_size / prices_arr[i] * 100
            if spike_pct < 0.08:
                continue

            peak_idx = i + spike_bars - 1
            if peak_idx >= n:
                continue
            peak = prices_arr[peak_idx]

            # Check reversal in remaining bars
            best_rev = 0
            rev_time = None
            for k in range(1, n - peak_idx):
                if spike_dir == 'UP':
                    ra = peak - prices_arr[peak_idx + k]
                else:
                    ra = prices_arr[peak_idx + k] - peak
                if ra > best_rev:
                    best_rev = ra
                    rev_time = k

            rev_ratio = best_rev / spike_size if spike_size > 0 else 0
            if rev_ratio < 0.4:
                continue

            # Acceleration flip
            accel_during = accel[i]
            accel_after_idx = min(peak_idx + 2, n - 1)
            accel_after = accel[accel_after_idx]
            accel_flip = (accel_during > 0 and accel_after < 0) or                          (accel_during < 0 and accel_after > 0)

            # Pre-spike range (tightness = accumulation)
            pre_range = np.ptp(prices_arr[max(0, i-10):i])
            pre_range_pct = pre_range / prices_arr[i] * 100

            # Quality score
            q = 0
            if z >= 4.0: q += 25
            elif z >= 3.0: q += 15
            elif z >= 2.5: q += 10
            if rev_ratio >= 0.8: q += 25
            elif rev_ratio >= 0.6: q += 15
            elif rev_ratio >= 0.5: q += 10
            if accel_flip: q += 20
            if pre_range_pct < 0.15: q += 15
            if rev_time and rev_time <= 5: q += 15
            elif rev_time and rev_time <= 10: q += 8
            q = min(q, 100)

            # Trade direction: opposite to spike
            trade_dir = 'PE' if spike_dir == 'UP' else 'CE'

            signals.append({
                'direction': trade_dir,
                'quality': q,
                'spike_dir': spike_dir,
                'spike_pct': round(spike_pct, 4),
                'spike_size': round(spike_size, 2),
                'reversal_ratio': round(rev_ratio, 2),
                'z_score': round(z, 1),
                'accel_flip': accel_flip,
                'pre_range_pct': round(pre_range_pct, 3),
                'peak_price': round(peak, 2),
            })

        return signals
