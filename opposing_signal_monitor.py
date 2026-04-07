"""
OPPOSING SIGNAL MONITOR — Real-time Direction Flip Detection
=============================================================
Philosophy: "A crash is an opportunity. Money lost goes to someone's hands.
            Find WHO is winning and FLIP to their side."

Instead of emergency stop-losses, this module monitors the SIGNAL FLOW:
- Count CE vs PE signals per symbol per minute
- Track quality scores of opposing direction signals
- Monitor CE IV vs PE IV ratio (smart money indicator)
- Detect when Wave/GammaBlast/CPR flip direction
- Track PCR as leading indicator (PCR < 0.95 = bullish, > 1.05 = bearish)

When opposing signals dominate for N consecutive windows:
  1. EXIT current position (reason: SIGNAL_FLIP)
  2. WAIT X seconds (configurable cooldown)
  3. Next scan naturally picks up the correct direction

March 30 case study:
  09:25 — Bot enters PE based on gap-down signals (110 PE, 0 CE)
  09:26 — First CE signal appears (quality 59)
  09:30 — CE signals explode: 12 CE vs 8 PE, CE quality 85+
  09:34 — Wave flips to CE, CE IV 67% vs PE IV 13%
  If this monitor existed: EXIT PE at 09:30-09:32, saving Rs 25K+
  Next scan at 09:33+ would have entered CE (the profitable side)

v1.0 — 2026-03-30
"""

import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger('opposing_signal_monitor')


# ====================================================================
# CONFIGURATION — Tunable parameters (backtest these!)
# ====================================================================
# Rolling window for signal counting
SIGNAL_WINDOW_SECONDS = 180          # 3-minute rolling window (was 120s, too short)

# Opposing signal dominance thresholds
MIN_OPPOSING_RATIO = 1.5             # Opposing must be 1.5x current direction (was 2.0, too strict for mixed signals)
MIN_OPPOSING_QUALITY = 80            # Opposing signals must have avg quality >= 80
OPPOSING_QUALITY_EDGE = 5            # Opposing quality must exceed current by this much
CONSECUTIVE_WINDOWS_TO_FLIP = 2      # Must dominate for 2 consecutive checks
MIN_OPPOSING_COUNT = 8               # Need at least 8 opposing signals
MIN_CURRENT_COUNT = 1                # Current direction must also have signals (avoid "no data" flips)

# IV ratio threshold (smart money indicator)
IV_RATIO_THRESHOLD = 1.5             # Opposing IV must be 1.5x current direction IV

# PCR thresholds (leading indicator)
PCR_BULLISH_THRESHOLD = 0.98         # PCR < 0.98 = bullish tendency (was 0.95, too strict — Mar 30 PCR was 0.96)
PCR_BEARISH_THRESHOLD = 1.02         # PCR > 1.02 = bearish tendency (was 1.05)

# Strategy flip weights (some strategies flipping is more significant)
STRATEGY_FLIP_WEIGHT = {
    'Wave': 3.0,           # Wave trend flip is very significant
    'CPR': 2.0,            # CPR breakout direction change
    'Gamma Blast': 2.5,    # Gamma spike direction = strong signal
    'Ghost Zone': 1.5,     # GZ zone flip
    'PCR+VWAP': 1.0,       # PCR+VWAP direction
    'Trend Rider': 2.0,    # Trend direction
}

# Cooldown after SIGNAL_FLIP exit (seconds before allowing re-entry)
SIGNAL_FLIP_COOLDOWN_SECONDS = 300   # Wait 5 min after flip exit (was 60s — too short, caused whipsaw)

# Minimum position age before flip check (don't flip brand new entries)
MIN_POSITION_AGE_FOR_FLIP = 120      # At least 2 min old before checking (was 30s — too aggressive)

# Maximum flips per symbol per day (prevent infinite whipsaw)
MAX_FLIPS_PER_SYMBOL_PER_DAY = 2     # Max 2 flips per symbol (after 2, stop flipping and hold)


class SignalRecord:
    """Single recorded signal with metadata."""
    __slots__ = ['timestamp', 'symbol', 'direction', 'quality', 'strategy', 'iv', 'pcr', 'premium']

    def __init__(self, timestamp, symbol, direction, quality, strategy, iv=0, pcr=0, premium=0):
        self.timestamp = timestamp
        self.symbol = symbol
        self.direction = direction  # 'CE' or 'PE'
        self.quality = quality
        self.strategy = strategy
        self.iv = iv
        self.pcr = pcr
        self.premium = premium


class OpposingSignalMonitor:
    """
    Tracks real-time signal flow to detect direction flips.

    Usage in paper_trader.py:
        # Init
        self.signal_monitor = OpposingSignalMonitor()

        # After scan_all_strategies():
        self.signal_monitor.record_signals(signals)

        # In check_exits(), for each position:
        should_flip, reason, data = self.signal_monitor.should_flip(
            symbol='NIFTY', current_direction='PE', position_age_secs=120
        )
        if should_flip:
            close_position(pos, reason='SIGNAL_FLIP')
    """

    def __init__(self, config=None, clock=None):
        # Optional clock override for backtesting (callable returning epoch seconds)
        self._clock = clock or time.time

        # Signal buffer: {symbol: [SignalRecord, ...]}
        self.signal_buffer = defaultdict(list)

        # Consecutive opposing dominance counter: {(symbol, direction): streak_count}
        self.opposing_streak = defaultdict(int)

        # Last flip time per symbol: {(symbol, direction): timestamp}
        self.last_flip_time = {}

        # Latest PCR per symbol
        self.latest_pcr = {}

        # Latest IV per symbol per direction: {(symbol, 'CE'/'PE'): iv}
        self.latest_iv = {}

        # Strategy direction tracker: {(symbol, strategy): direction}
        self.strategy_direction = {}

        # Strategy flip events: [(timestamp, symbol, strategy, old_dir, new_dir)]
        self.strategy_flips = []

        # Daily flip count per symbol: {symbol: count} — prevents whipsaw
        self.daily_flip_count = defaultdict(int)

        # Override config
        if config:
            for key, val in config.items():
                if hasattr(self, key):
                    setattr(self, key, val)

        # Stats for logging
        self.total_signals_recorded = 0
        self.flip_recommendations = 0

    def record_signals(self, signals):
        """Record a batch of signals from one scan cycle.

        Args:
            signals: list of signal dicts from scan_all_strategies()
                     Each has: symbol, type, quality_score, strategy, greeks.iv, pcr
        """
        now = self._clock()

        for sig in signals:
            symbol = sig.get('symbol', '')
            sig_type = sig.get('type', '')
            direction = 'CE' if 'CE' in sig_type else 'PE'
            quality = sig.get('quality_score', 0)
            strategy = sig.get('strategy', '')
            greeks = sig.get('greeks', {})
            iv = greeks.get('iv', 0) if isinstance(greeks, dict) else 0
            pcr = sig.get('pcr', 0)
            premium = sig.get('premium', 0)

            record = SignalRecord(
                timestamp=now,
                symbol=symbol,
                direction=direction,
                quality=quality,
                strategy=strategy,
                iv=iv,
                pcr=pcr,
                premium=premium
            )
            self.signal_buffer[symbol].append(record)
            self.total_signals_recorded += 1

            # Update latest IV and PCR
            self.latest_iv[(symbol, direction)] = iv
            if pcr > 0:
                self.latest_pcr[symbol] = pcr

            # Track strategy direction changes
            strat_key = (symbol, strategy)
            old_dir = self.strategy_direction.get(strat_key)
            if old_dir and old_dir != direction:
                self.strategy_flips.append((now, symbol, strategy, old_dir, direction))
                logger.info(f"  [OSM] STRATEGY_FLIP: {strategy} on {symbol} flipped {old_dir} -> {direction}")
            self.strategy_direction[strat_key] = direction

        # Prune old signals (keep last 5 minutes)
        self._prune_old_signals(now, max_age=300)

    def record_pcr(self, symbol, pcr_value):
        """Record latest PCR for a symbol (called from indicator computation)."""
        if pcr_value and pcr_value > 0:
            self.latest_pcr[symbol] = pcr_value

    def should_flip(self, symbol, current_direction, position_age_secs=0):
        """Check if opposing signals suggest flipping direction.

        Args:
            symbol: e.g. 'NIFTY'
            current_direction: 'CE' or 'PE' (what we currently hold)
            position_age_secs: how long the position has been open

        Returns:
            (should_flip: bool, reason: str, data: dict)
        """
        # Don't flip brand-new positions
        if position_age_secs < MIN_POSITION_AGE_FOR_FLIP:
            return False, f"position too new ({position_age_secs}s < {MIN_POSITION_AGE_FOR_FLIP}s)", {}

        # Max flips per symbol per day (anti-whipsaw)
        if self.daily_flip_count.get(symbol, 0) >= MAX_FLIPS_PER_SYMBOL_PER_DAY:
            return False, f"max flips reached ({self.daily_flip_count[symbol]}/{MAX_FLIPS_PER_SYMBOL_PER_DAY})", {}

        # Check cooldown from recent flip
        flip_key = (symbol, current_direction)
        last_flip = self.last_flip_time.get(flip_key, 0)
        if self._clock() - last_flip < SIGNAL_FLIP_COOLDOWN_SECONDS:
            return False, f"flip cooldown ({int(self._clock() - last_flip)}s < {SIGNAL_FLIP_COOLDOWN_SECONDS}s)", {}

        opposing_direction = 'CE' if current_direction == 'PE' else 'PE'
        now = self._clock()

        # Get signals in the rolling window
        window_signals = [
            s for s in self.signal_buffer.get(symbol, [])
            if now - s.timestamp <= SIGNAL_WINDOW_SECONDS
        ]

        if not window_signals:
            self.opposing_streak[flip_key] = 0
            return False, "no signals in window", {}

        # Count and score by direction
        current_signals = [s for s in window_signals if s.direction == current_direction]
        opposing_signals = [s for s in window_signals if s.direction == opposing_direction]

        current_count = len(current_signals)
        opposing_count = len(opposing_signals)

        # Weighted counts (some strategies matter more)
        current_weighted = sum(STRATEGY_FLIP_WEIGHT.get(s.strategy, 1.0) for s in current_signals)
        opposing_weighted = sum(STRATEGY_FLIP_WEIGHT.get(s.strategy, 1.0) for s in opposing_signals)

        # Quality scores
        current_avg_quality = sum(s.quality for s in current_signals) / max(len(current_signals), 1)
        opposing_avg_quality = sum(s.quality for s in opposing_signals) / max(len(opposing_signals), 1)
        opposing_max_quality = max((s.quality for s in opposing_signals), default=0)

        # IV comparison
        current_iv = self.latest_iv.get((symbol, current_direction), 0)
        opposing_iv = self.latest_iv.get((symbol, opposing_direction), 0)
        iv_ratio = opposing_iv / max(current_iv, 0.01) if current_iv > 0 else 0

        # PCR check
        pcr = self.latest_pcr.get(symbol, 1.0)
        pcr_supports_current = True
        if current_direction == 'PE' and pcr < PCR_BULLISH_THRESHOLD:
            pcr_supports_current = False  # PCR says bullish but we hold PE
        elif current_direction == 'CE' and pcr > PCR_BEARISH_THRESHOLD:
            pcr_supports_current = False  # PCR says bearish but we hold CE

        # Strategy flips in last 2 minutes
        recent_strat_flips = [
            f for f in self.strategy_flips
            if f[1] == symbol and f[3] == current_direction and now - f[0] <= SIGNAL_WINDOW_SECONDS
        ]
        wave_flipped = any(f[2] == 'Wave' for f in recent_strat_flips)
        strategies_flipped = [f[2] for f in recent_strat_flips]

        # Build diagnostic data
        data = {
            'current_count': current_count,
            'opposing_count': opposing_count,
            'current_weighted': round(current_weighted, 1),
            'opposing_weighted': round(opposing_weighted, 1),
            'current_avg_quality': round(current_avg_quality, 1),
            'opposing_avg_quality': round(opposing_avg_quality, 1),
            'opposing_max_quality': round(opposing_max_quality, 1),
            'current_iv': round(current_iv, 2),
            'opposing_iv': round(opposing_iv, 2),
            'iv_ratio': round(iv_ratio, 2),
            'pcr': round(pcr, 3),
            'pcr_supports_current': pcr_supports_current,
            'wave_flipped': wave_flipped,
            'strategies_flipped': strategies_flipped,
            'opposing_direction': opposing_direction,
            'window_seconds': SIGNAL_WINDOW_SECONDS,
        }

        # ================================================================
        # FLIP DECISION LOGIC — Multiple confirming factors
        # Key fix: require BOTH directions to have signals (avoid "no data" flips)
        # ================================================================
        flip_score = 0
        reasons = []

        # GATE: Both directions must have signals in the window
        # If current direction has 0 signals, that's "no data" not "losing"
        if current_count < MIN_CURRENT_COUNT and not wave_flipped:
            self.opposing_streak[flip_key] = 0
            return False, f"current direction has {current_count} signals (need {MIN_CURRENT_COUNT}+, no flip on silence)", data

        # Factor 1: Opposing signal count dominance (need real ratio, not X/0)
        if opposing_count >= MIN_OPPOSING_COUNT and current_count > 0:
            ratio = opposing_weighted / max(current_weighted, 1.0)  # Floor at 1.0, not 0.1
            if ratio >= MIN_OPPOSING_RATIO:
                flip_score += 30
                reasons.append(f"opposing {opposing_count} vs current {current_count} (weighted ratio {ratio:.1f}x)")

        # Factor 2: Opposing quality dominance
        if opposing_avg_quality >= MIN_OPPOSING_QUALITY and opposing_avg_quality > current_avg_quality + OPPOSING_QUALITY_EDGE:
            flip_score += 20
            reasons.append(f"opposing quality {opposing_avg_quality:.0f} > current {current_avg_quality:.0f}")

        # Factor 3: IV ratio (smart money)
        if iv_ratio >= IV_RATIO_THRESHOLD:
            flip_score += 15
            reasons.append(f"IV ratio {iv_ratio:.1f}x (opposing IV {opposing_iv:.1f}% vs current {current_iv:.1f}%)")

        # Factor 4: PCR doesn't support current direction
        if not pcr_supports_current:
            flip_score += 15
            reasons.append(f"PCR {pcr:.3f} contradicts {current_direction} position")

        # Factor 5: Wave strategy flipped (strongest single indicator)
        if wave_flipped:
            flip_score += 25
            reasons.append(f"Wave strategy flipped from {current_direction} to {opposing_direction}")

        # Factor 6: Multiple strategies flipped (at least 2 DIFFERENT strategies)
        unique_strat_flips = list(set(strategies_flipped))
        if len(unique_strat_flips) >= 2:
            flip_score += 15
            reasons.append(f"{len(unique_strat_flips)} strategies flipped: {', '.join(unique_strat_flips)}")

        data['flip_score'] = flip_score
        data['reasons'] = reasons

        # Need flip_score >= 50 to trigger (at least 2-3 confirming factors)
        FLIP_THRESHOLD = 50

        if flip_score >= FLIP_THRESHOLD:
            # Increment streak
            self.opposing_streak[flip_key] += 1
            streak = self.opposing_streak[flip_key]

            if streak >= CONSECUTIVE_WINDOWS_TO_FLIP:
                # FLIP confirmed
                reason_str = f"SIGNAL_FLIP (score={flip_score}, streak={streak}): " + " | ".join(reasons)
                self.last_flip_time[flip_key] = now
                self.opposing_streak[flip_key] = 0
                self.daily_flip_count[symbol] += 1
                self.flip_recommendations += 1

                logger.warning(f"  [OSM] FLIP RECOMMENDED: {symbol} {current_direction} -> {opposing_direction} | {reason_str}")
                return True, reason_str, data
            else:
                logger.info(f"  [OSM] Flip building: {symbol} {current_direction} score={flip_score} streak={streak}/{CONSECUTIVE_WINDOWS_TO_FLIP} | {' | '.join(reasons)}")
                return False, f"streak building ({streak}/{CONSECUTIVE_WINDOWS_TO_FLIP})", data
        else:
            # Reset streak
            self.opposing_streak[flip_key] = 0
            return False, f"flip_score {flip_score} < {FLIP_THRESHOLD}", data

    def get_signal_flow_summary(self, symbol):
        """Get a human-readable summary of current signal flow for a symbol.
        Used for logging and diagnostics.
        """
        now = self._clock()
        window_signals = [
            s for s in self.signal_buffer.get(symbol, [])
            if now - s.timestamp <= SIGNAL_WINDOW_SECONDS
        ]

        if not window_signals:
            return f"{symbol}: No signals in last {SIGNAL_WINDOW_SECONDS}s"

        ce_signals = [s for s in window_signals if s.direction == 'CE']
        pe_signals = [s for s in window_signals if s.direction == 'PE']

        ce_strats = set(s.strategy for s in ce_signals)
        pe_strats = set(s.strategy for s in pe_signals)

        ce_avg_q = sum(s.quality for s in ce_signals) / max(len(ce_signals), 1)
        pe_avg_q = sum(s.quality for s in pe_signals) / max(len(pe_signals), 1)

        pcr = self.latest_pcr.get(symbol, 0)
        ce_iv = self.latest_iv.get((symbol, 'CE'), 0)
        pe_iv = self.latest_iv.get((symbol, 'PE'), 0)

        return (f"{symbol}: CE={len(ce_signals)}(q={ce_avg_q:.0f},{','.join(ce_strats) or 'none'}) "
                f"PE={len(pe_signals)}(q={pe_avg_q:.0f},{','.join(pe_strats) or 'none'}) "
                f"PCR={pcr:.3f} CE_IV={ce_iv:.1f}% PE_IV={pe_iv:.1f}%")

    def get_stats(self):
        """Get monitor statistics."""
        return {
            'total_signals_recorded': self.total_signals_recorded,
            'flip_recommendations': self.flip_recommendations,
            'active_symbols': len(self.signal_buffer),
            'strategy_flips_today': len(self.strategy_flips),
            'active_streaks': {k: v for k, v in self.opposing_streak.items() if v > 0},
        }

    def _prune_old_signals(self, now, max_age=300):
        """Remove signals older than max_age seconds."""
        for symbol in list(self.signal_buffer.keys()):
            self.signal_buffer[symbol] = [
                s for s in self.signal_buffer[symbol]
                if now - s.timestamp <= max_age
            ]
            if not self.signal_buffer[symbol]:
                del self.signal_buffer[symbol]

        # Prune old strategy flips
        self.strategy_flips = [
            f for f in self.strategy_flips
            if now - f[0] <= max_age
        ]
