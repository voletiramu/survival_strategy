"""
Ghost Zone v9 — Institutional Zone Detection with Market Phase Awareness
=========================================================================
Reverse-engineered from GTI (Ghost Trade India) methodology.

Key improvements over v8:
  1. Market Phase Detection — Compression/Accumulation/Expansion/Manipulation
  2. Bollinger Band squeeze for compression detection
  3. Zone-to-zone targeting (not fixed %)
  4. 3-4 Institutional Candle Theory (3 = high prob, 4 = sureshot)
  5. Fixed SL in spot points (NIFTY: 40, BNF: 100, SENSEX: 120) — MC-optimized
  6. Fair Value Gap (FVG) detection
  7. POC (Point of Control) from volume profile as key level

Algorithm:
  1. Build 3-min candles from spot ticks (or use TradingView 3-min data)
  2. Compute BB(20,2) for squeeze/expansion detection
  3. Identify market phase (Compression/Accumulation/Expansion)
  4. Detect institutional candles: body >= 1.8x rolling 20-bar average
  5. Count consecutive institutional candles (3-4 candle theory)
  6. Create DEMAND zones (bullish inst candles) and SUPPLY zones (bearish)
  7. Detect Fair Value Gaps (unfilled gaps between candles)
  8. Compute POC from volume profile (highest volume price level)
  9. Entry: Retest of zone + phase confirmation + BB confirmation
  10. Target: Next opposite zone or POC level
  11. SL: Fixed rupees below/above zone

Zone lifecycle:
  - Created when institutional candle detected
  - Active until violated (close beyond zone boundary)
  - Max 8 zones tracked per type per symbol
  - Multi-day zones carry forward (unfilled = stronger)
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────

# Institutional candle detection
BODY_MULT_THRESHOLD = 1.8       # Body >= 1.8x average (relaxed from 2.0)
LOOKBACK_BARS = 20              # Rolling average window
MIN_BODY_PTS = {'NIFTY': 12, 'BANKNIFTY': 35, 'SENSEX': 40}

# Bollinger Bands — BB(20, 2.0) confirmed from TradingView chart analysis
BB_PERIOD = 20
BB_STD = 2.0
# Per-symbol squeeze threshold (MC-optimized Mar 2026)
# BNF uses 0 = no squeeze filter (MC showed best without it)
BB_SQUEEZE_THRESHOLD_MAP = {'NIFTY': 0.8, 'BANKNIFTY': 0.0, 'SENSEX': 0.8}
BB_SQUEEZE_DEFAULT = 0.7

# Zone management
MAX_ZONES_PER_TYPE = 8
ZONE_CACHE_TTL = 7200           # 2 hours

# Fixed SL in spot points (MC-optimized Mar 2026, 1000 iterations)
FIXED_SL_RS = {'NIFTY': 40, 'BANKNIFTY': 100, 'SENSEX': 120}

# Bounce thresholds
MIN_BOUNCE_PCT = 0.03           # Min bounce from zone for valid retest

# Institutional candle theory
INST_CANDLE_WINDOW = 10         # Look back 10 bars for consecutive inst candles
INST_3_CANDLE_SCORE = 85        # Quality score for 3 consecutive inst candles
INST_4_CANDLE_SCORE = 95        # Quality score for 4 consecutive (sureshot)

# Cooldown (MC-optimized)
SIGNAL_COOLDOWN_SECS = {'NIFTY': 900, 'BANKNIFTY': 1800, 'SENSEX': 1800}
SIGNAL_COOLDOWN_DEFAULT = 900   # 15 min fallback

# Min R:R ratio (MC-optimized)
MIN_RR_RATIO = {'NIFTY': 2.5, 'BANKNIFTY': 2.0, 'SENSEX': 2.5}
MIN_RR_DEFAULT = 2.0

# Body mult threshold per symbol (MC-optimized)
BODY_MULT_THRESHOLD_MAP = {'NIFTY': 2.0, 'BANKNIFTY': 2.5, 'SENSEX': 2.0}

# Max hold time in minutes (MC-optimized)
MAX_HOLD_MINUTES = {'NIFTY': 45, 'BANKNIFTY': 45, 'SENSEX': 90}
MAX_HOLD_DEFAULT = 60

# POC
POC_WINDOW = 80                 # ~4 hours of 3-min bars for POC calculation


# ── Enums ─────────────────────────────────────────────────────────────────

class MarketPhase(Enum):
    COMPRESSION = 'COMPRESSION'         # BB squeeze, small candles, zones fail
    ACCUMULATION = 'ACCUMULATION'       # Range-bound, zones work, S/R holds
    EXPANSION = 'EXPANSION'             # Directional move, breakout trades
    MANIPULATION = 'MANIPULATION'       # Trap/SL hunting, fake breakouts


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class Zone:
    created_at: datetime
    zone_type: str          # 'DEMAND' or 'SUPPLY'
    top: float
    bot: float
    body: float
    body_mult: float
    inst_candle_count: int = 1      # How many consecutive inst candles formed this zone
    active: bool = True
    retest_count: int = 0
    last_retest: Optional[datetime] = None
    day_created: Optional[str] = None   # For multi-day zone tracking

    @property
    def mid(self):
        return (self.top + self.bot) / 2

    @property
    def height(self):
        return self.top - self.bot


@dataclass
class FVG:
    """Fair Value Gap — unfilled gap between candles."""
    timestamp: datetime
    fvg_type: str           # 'BULLISH' or 'BEARISH'
    top: float
    bot: float
    filled: bool = False


@dataclass
class GhostSignal:
    timestamp: datetime
    signal_type: str        # 'GHST_ST', 'GHST_3C', 'GHST_4C', 'GHST_FVG'
    zone_type: str
    zone_top: float
    zone_bot: float
    zone_mult: float
    direction: str          # 'BUY_CE' or 'BUY_PE'
    spot: float
    bounce_pct: float
    inst_candle_count: int
    market_phase: str
    target_price: float     # Next zone or POC target
    sl_price: float         # Fixed SL
    quality_score: int
    reason: str


# ── Ghost Zone v9 Engine ──────────────────────────────────────────────────

class GhostZoneV9:
    """Live Ghost Zone v9 detector with market phase awareness.

    Usage:
        gz9 = GhostZoneV9()

        # Feed 3-min candles (from Zerodha ticks or TradingView data)
        gz9.update_candle(symbol, timestamp, open, high, low, close, volume)

        # Get signals
        signals = gz9.get_signals(symbol)
    """

    def __init__(self):
        # Per-symbol 3-min candles
        self._candles = defaultdict(list)        # symbol -> [dict]
        # Per-symbol zones
        self._demand_zones = defaultdict(list)   # symbol -> [Zone]
        self._supply_zones = defaultdict(list)   # symbol -> [Zone]
        # Per-symbol FVGs
        self._fvgs = defaultdict(list)           # symbol -> [FVG]
        # Per-symbol signals
        self._pending_signals = defaultdict(list)
        # Cooldown
        self._last_signal_time = defaultdict(lambda: datetime.min)
        # Current market phase
        self._market_phase = defaultdict(lambda: MarketPhase.ACCUMULATION)
        # BB data
        self._bb_data = defaultdict(dict)
        # POC data
        self._poc = defaultdict(float)

        logger.info("[GhostZone v9] Initialized — phase-aware institutional zone detection")

    # ── Candle Input ──────────────────────────────────────────────────────

    def update_candle(self, symbol, timestamp, o, h, l, c, volume=0):
        """Feed a 3-min candle. Call every 3 minutes."""
        if not isinstance(timestamp, datetime):
            timestamp = pd.Timestamp(timestamp).to_pydatetime()

        candle = {
            'timestamp': timestamp,
            'open': float(o),
            'high': float(h),
            'low': float(l),
            'close': float(c),
            'volume': float(volume),
            'body': abs(float(c) - float(o)),
            'range': float(h) - float(l),
            'direction': 'BULL' if float(c) > float(o) else 'BEAR',
            'upper_wick': float(h) - max(float(o), float(c)),
            'lower_wick': min(float(o), float(c)) - float(l),
        }

        self._candles[symbol].append(candle)

        # Keep max 600 candles (~30 hours)
        if len(self._candles[symbol]) > 600:
            self._candles[symbol] = self._candles[symbol][-600:]

        # Compute rolling metrics
        self._compute_candle_metrics(symbol)

        # Run detection pipeline
        if len(self._candles[symbol]) >= LOOKBACK_BARS + 5:
            self._compute_bollinger_bands(symbol)
            self._detect_market_phase(symbol)
            self._compute_poc(symbol)
            self._detect_zones(symbol)
            self._detect_fvg(symbol)
            self._check_retests(symbol, float(c), timestamp)

    def update_tick(self, symbol, spot, timestamp=None):
        """Feed a spot tick (1-min). Builds 3-min candles internally."""
        if timestamp is None:
            timestamp = datetime.now()
        if not isinstance(timestamp, datetime):
            timestamp = pd.Timestamp(timestamp).to_pydatetime()

        # Aggregate ticks into 3-min candles
        if not hasattr(self, '_tick_buffer'):
            self._tick_buffer = defaultdict(list)

        self._tick_buffer[symbol].append((timestamp, float(spot)))

        # Check if we have 3 minutes of data
        buf = self._tick_buffer[symbol]
        if len(buf) >= 3:
            ts_start = buf[0][0]
            ts_end = buf[-1][0]
            if (ts_end - ts_start).total_seconds() >= 150:  # ~2.5 min
                prices = [t[1] for t in buf]
                self.update_candle(
                    symbol, ts_start,
                    o=prices[0],
                    h=max(prices),
                    l=min(prices),
                    c=prices[-1],
                    volume=0
                )
                self._tick_buffer[symbol] = []

    # ── Candle Metrics ────────────────────────────────────────────────────

    def _compute_candle_metrics(self, symbol):
        """Compute rolling body average and multiplier for each candle."""
        candles = self._candles[symbol]
        if len(candles) < LOOKBACK_BARS:
            return

        for i in range(max(0, len(candles) - 5), len(candles)):
            c = candles[i]
            start = max(0, i - LOOKBACK_BARS)
            bodies = [candles[j]['body'] for j in range(start, i)]
            avg_body = np.mean(bodies) if bodies else 1
            c['avg_body'] = avg_body
            c['body_mult'] = c['body'] / avg_body if avg_body > 0 else 0

    # ── Bollinger Bands ───────────────────────────────────────────────────

    def _compute_bollinger_bands(self, symbol):
        """Compute BB(20,2) for squeeze detection."""
        candles = self._candles[symbol]
        if len(candles) < BB_PERIOD + 5:
            return

        closes = np.array([c['close'] for c in candles[-BB_PERIOD - 10:]])

        # Current BB
        sma = np.mean(closes[-BB_PERIOD:])
        std = np.std(closes[-BB_PERIOD:])
        upper = sma + BB_STD * std
        lower = sma - BB_STD * std
        width = (upper - lower) / sma * 100  # Width as % of price

        # Average BB width over last 50 bars
        widths = []
        for i in range(max(0, len(closes) - 50), len(closes) - BB_PERIOD + 1):
            s = np.mean(closes[i:i + BB_PERIOD])
            st = np.std(closes[i:i + BB_PERIOD])
            w = ((s + BB_STD * st) - (s - BB_STD * st)) / s * 100
            widths.append(w)

        avg_width = np.mean(widths) if widths else width

        self._bb_data[symbol] = {
            'sma': sma,
            'upper': upper,
            'lower': lower,
            'width': width,
            'avg_width': avg_width,
            'squeeze': width < avg_width * BB_SQUEEZE_THRESHOLD_MAP.get(symbol, BB_SQUEEZE_DEFAULT),
            'expanding': width > avg_width * 1.3,
            'width_ratio': width / avg_width if avg_width > 0 else 1,
        }

        candles[-1]['bb_upper'] = upper
        candles[-1]['bb_lower'] = lower
        candles[-1]['bb_sma'] = sma
        candles[-1]['bb_squeeze'] = self._bb_data[symbol]['squeeze']

    # ── Market Phase Detection ────────────────────────────────────────────

    def _detect_market_phase(self, symbol):
        """Detect current market phase: Compression/Accumulation/Expansion/Manipulation."""
        candles = self._candles[symbol]
        bb = self._bb_data.get(symbol, {})
        if not bb or len(candles) < 30:
            return

        # Recent 10-bar metrics
        recent = candles[-10:]
        avg_range = np.mean([c['range'] for c in recent])
        avg_body = np.mean([c['body'] for c in recent])
        body_range_ratio = avg_body / avg_range if avg_range > 0 else 0

        # Count direction changes (choppiness)
        dir_changes = sum(1 for i in range(1, len(recent))
                          if recent[i]['direction'] != recent[i - 1]['direction'])

        squeeze = bb.get('squeeze', False)
        expanding = bb.get('expanding', False)
        width_ratio = bb.get('width_ratio', 1)

        # Phase detection logic
        if squeeze and dir_changes >= 5:
            # BB squeezed + choppy = COMPRESSION (zones fail, only levels work)
            phase = MarketPhase.COMPRESSION
        elif expanding and dir_changes <= 3 and body_range_ratio > 0.6:
            # BB expanding + directional + strong bodies = EXPANSION
            phase = MarketPhase.EXPANSION
        elif not squeeze and dir_changes >= 4 and avg_range > avg_body * 2:
            # Not squeezed + choppy + big wicks = MANIPULATION (trap trading)
            phase = MarketPhase.MANIPULATION
        else:
            # Default: ACCUMULATION (zones work, S/R holds)
            phase = MarketPhase.ACCUMULATION

        prev_phase = self._market_phase[symbol]
        if phase != prev_phase:
            logger.info("[GhostZone v9] %s Phase: %s → %s (BB_width=%.2f%%, squeeze=%s, chop=%d/10)" % (
                symbol, prev_phase.value, phase.value,
                bb.get('width', 0), squeeze, dir_changes))

        self._market_phase[symbol] = phase

    # ── POC (Point of Control) ────────────────────────────────────────────

    def _compute_poc(self, symbol):
        """Compute POC from volume profile of recent bars."""
        candles = self._candles[symbol]
        recent = candles[-POC_WINDOW:] if len(candles) >= POC_WINDOW else candles

        if not recent:
            return

        # Build volume profile: price → volume
        price_min = min(c['low'] for c in recent)
        price_max = max(c['high'] for c in recent)
        if price_max <= price_min:
            return

        # 50 price bins
        n_bins = 50
        bin_size = (price_max - price_min) / n_bins
        vol_profile = np.zeros(n_bins)

        for c in recent:
            vol = c.get('volume', 1)
            if vol <= 0:
                vol = 1
            low_bin = int((c['low'] - price_min) / bin_size)
            high_bin = int((c['high'] - price_min) / bin_size)
            low_bin = max(0, min(low_bin, n_bins - 1))
            high_bin = max(0, min(high_bin, n_bins - 1))
            bins_covered = high_bin - low_bin + 1
            for b in range(low_bin, high_bin + 1):
                vol_profile[b] += vol / bins_covered

        poc_bin = np.argmax(vol_profile)
        self._poc[symbol] = price_min + (poc_bin + 0.5) * bin_size

    # ── Zone Detection ────────────────────────────────────────────────────

    def _detect_zones(self, symbol):
        """Detect institutional candles and create zones."""
        candles = self._candles[symbol]
        if len(candles) < LOOKBACK_BARS + 5:
            return

        min_body = MIN_BODY_PTS.get(symbol, 15)

        # Check last 5 candles
        for i in range(max(len(candles) - 5, LOOKBACK_BARS), len(candles)):
            c = candles[i]

            mult_thresh = BODY_MULT_THRESHOLD_MAP.get(symbol, BODY_MULT_THRESHOLD)
            if c.get('body_mult', 0) < mult_thresh:
                continue
            if c['body'] < min_body:
                continue

            zone_top = max(c['open'], c['close'])
            zone_bot = min(c['open'], c['close'])

            # Duplicate check
            existing = self._demand_zones[symbol] + self._supply_zones[symbol]
            is_dup = any(abs(ez.top - zone_top) < min_body and abs(ez.bot - zone_bot) < min_body
                         for ez in existing)
            if is_dup:
                continue

            # Check immediate violation (next 2 bars)
            violated = False
            for j in range(i + 1, min(i + 3, len(candles))):
                nb = candles[j]
                if c['direction'] == 'BULL' and nb['close'] < zone_bot * 0.997:
                    violated = True
                    break
                if c['direction'] == 'BEAR' and nb['close'] > zone_top * 1.003:
                    violated = True
                    break
            if violated:
                continue

            # Count consecutive institutional candles (3-4 candle theory)
            inst_count = 1
            for k in range(i - 1, max(i - INST_CANDLE_WINDOW, 0) - 1, -1):
                prev = candles[k]
                if prev.get('body_mult', 0) >= BODY_MULT_THRESHOLD and prev['body'] >= min_body:
                    # Must be in DIFFERENT price range (not same zone)
                    prev_top = max(prev['open'], prev['close'])
                    prev_bot = min(prev['open'], prev['close'])
                    if abs(prev_top - zone_top) > min_body * 0.5:
                        inst_count += 1
                    else:
                        break
                else:
                    break

            zone = Zone(
                created_at=c['timestamp'],
                zone_type='DEMAND' if c['direction'] == 'BULL' else 'SUPPLY',
                top=zone_top, bot=zone_bot,
                body=c['body'], body_mult=c['body_mult'],
                inst_candle_count=inst_count,
                day_created=c['timestamp'].strftime('%Y-%m-%d') if isinstance(c['timestamp'], datetime) else '',
            )

            if zone.zone_type == 'DEMAND':
                self._demand_zones[symbol].append(zone)
                if len(self._demand_zones[symbol]) > MAX_ZONES_PER_TYPE:
                    self._demand_zones[symbol] = self._demand_zones[symbol][-MAX_ZONES_PER_TYPE:]
            else:
                self._supply_zones[symbol].append(zone)
                if len(self._supply_zones[symbol]) > MAX_ZONES_PER_TYPE:
                    self._supply_zones[symbol] = self._supply_zones[symbol][-MAX_ZONES_PER_TYPE:]

            ic_label = ''
            if inst_count >= 4:
                ic_label = ' [4-CANDLE SURESHOT]'
            elif inst_count >= 3:
                ic_label = ' [3-CANDLE HIGH PROB]'

            logger.info("[GZ v9] %s NEW %s zone: %.0f-%.0f (%.1fx, %d inst candles)%s" % (
                symbol, zone.zone_type, zone.bot, zone.top, zone.body_mult,
                inst_count, ic_label))

    # ── Fair Value Gap Detection ──────────────────────────────────────────

    def _detect_fvg(self, symbol):
        """Detect Fair Value Gaps (unfilled price gaps between candles)."""
        candles = self._candles[symbol]
        if len(candles) < 5:
            return

        # Check last 3 candles for FVG pattern
        for i in range(max(len(candles) - 3, 2), len(candles)):
            c1 = candles[i - 2]  # First candle
            c2 = candles[i - 1]  # Middle candle (the gap)
            c3 = candles[i]      # Third candle

            # Bullish FVG: c1 high < c3 low (gap up)
            if c1['high'] < c3['low'] and c2['direction'] == 'BULL':
                fvg = FVG(
                    timestamp=c2['timestamp'],
                    fvg_type='BULLISH',
                    top=c3['low'],
                    bot=c1['high'],
                )
                # Check not duplicate
                if not any(abs(f.top - fvg.top) < 5 and abs(f.bot - fvg.bot) < 5
                           for f in self._fvgs[symbol]):
                    self._fvgs[symbol].append(fvg)

            # Bearish FVG: c1 low > c3 high (gap down)
            if c1['low'] > c3['high'] and c2['direction'] == 'BEAR':
                fvg = FVG(
                    timestamp=c2['timestamp'],
                    fvg_type='BEARISH',
                    top=c1['low'],
                    bot=c3['high'],
                )
                if not any(abs(f.top - fvg.top) < 5 and abs(f.bot - fvg.bot) < 5
                           for f in self._fvgs[symbol]):
                    self._fvgs[symbol].append(fvg)

        # Mark filled FVGs
        spot = candles[-1]['close']
        for fvg in self._fvgs[symbol]:
            if not fvg.filled:
                if fvg.fvg_type == 'BULLISH' and spot <= fvg.bot:
                    fvg.filled = True
                elif fvg.fvg_type == 'BEARISH' and spot >= fvg.top:
                    fvg.filled = True

        # Keep max 20 FVGs
        self._fvgs[symbol] = [f for f in self._fvgs[symbol] if not f.filled][-20:]

    # ── Retest Detection ──────────────────────────────────────────────────

    def _check_retests(self, symbol, current_spot, timestamp):
        """Check if current price is retesting any active zone."""
        candles = self._candles[symbol]
        if len(candles) < 3:
            return

        latest = candles[-1]
        phase = self._market_phase[symbol]
        bb = self._bb_data.get(symbol, {})

        # Note: BB squeeze filter moved to get_signals() as entry gate.
        # Generating signals here, filtering at output — backtest shows this is optimal.

        # Check demand zones (BUY CE on bounce)
        for zone in self._demand_zones[symbol]:
            if not zone.active:
                continue

            # Zone violated?
            if latest['close'] < zone.bot * 0.995:
                zone.active = False
                continue

            # Retest: price dips into zone and closes above
            if (latest['low'] <= zone.top * 1.002 and
                    latest['low'] >= zone.bot * 0.995 and
                    latest['close'] > zone.top):

                bounce = (latest['close'] - zone.top) / zone.top * 100
                if bounce < MIN_BOUNCE_PCT:
                    continue

                # Confirmation: bullish candle closing above zone
                is_bullish = latest['close'] > latest['open']
                if not is_bullish:
                    continue

                # Find target: nearest supply zone above or POC
                target = self._find_target(symbol, current_spot, 'UP')

                # Fixed SL
                sl_pts = FIXED_SL_RS.get(symbol, 40)

                # Quality score from institutional candle count
                if zone.inst_candle_count >= 4:
                    q_score = INST_4_CANDLE_SCORE
                    sig_type = 'GHST_4C'
                elif zone.inst_candle_count >= 3:
                    q_score = INST_3_CANDLE_SCORE
                    sig_type = 'GHST_3C'
                else:
                    q_score = 70 + int(zone.body_mult * 5)
                    sig_type = 'GHST_ST'

                # Phase bonus
                if phase == MarketPhase.ACCUMULATION:
                    q_score = min(100, q_score + 5)
                elif phase == MarketPhase.EXPANSION:
                    q_score = min(100, q_score + 10)

                self._pending_signals[symbol].append(GhostSignal(
                    timestamp=timestamp,
                    signal_type=sig_type,
                    zone_type='DEMAND',
                    zone_top=zone.top, zone_bot=zone.bot,
                    zone_mult=zone.body_mult,
                    direction='BUY_CE',
                    spot=current_spot,
                    bounce_pct=bounce,
                    inst_candle_count=zone.inst_candle_count,
                    market_phase=phase.value,
                    target_price=target,
                    sl_price=sl_pts,
                    quality_score=q_score,
                    reason='GZ v9 DEMAND zone %.0f-%.0f (%.1fx, %dIC, %s) T=%.0f SL=%drs' % (
                        zone.bot, zone.top, zone.body_mult,
                        zone.inst_candle_count, phase.value, target, sl_pts),
                ))
                zone.retest_count += 1
                zone.last_retest = timestamp

        # Check supply zones (BUY PE on rejection)
        for zone in self._supply_zones[symbol]:
            if not zone.active:
                continue

            if latest['close'] > zone.top * 1.005:
                zone.active = False
                continue

            if (latest['high'] >= zone.bot * 0.998 and
                    latest['high'] <= zone.top * 1.005 and
                    latest['close'] < zone.bot):

                reject = (zone.bot - latest['close']) / zone.bot * 100
                if reject < MIN_BOUNCE_PCT:
                    continue

                is_bearish = latest['close'] < latest['open']
                if not is_bearish:
                    continue

                target = self._find_target(symbol, current_spot, 'DOWN')
                sl_pts = FIXED_SL_RS.get(symbol, 40)

                if zone.inst_candle_count >= 4:
                    q_score = INST_4_CANDLE_SCORE
                    sig_type = 'GHST_4C'
                elif zone.inst_candle_count >= 3:
                    q_score = INST_3_CANDLE_SCORE
                    sig_type = 'GHST_3C'
                else:
                    q_score = 70 + int(zone.body_mult * 5)
                    sig_type = 'GHST_ST'

                if phase == MarketPhase.ACCUMULATION:
                    q_score = min(100, q_score + 5)
                elif phase == MarketPhase.EXPANSION:
                    q_score = min(100, q_score + 10)

                self._pending_signals[symbol].append(GhostSignal(
                    timestamp=timestamp,
                    signal_type=sig_type,
                    zone_type='SUPPLY',
                    zone_top=zone.top, zone_bot=zone.bot,
                    zone_mult=zone.body_mult,
                    direction='BUY_PE',
                    spot=current_spot,
                    bounce_pct=reject,
                    inst_candle_count=zone.inst_candle_count,
                    market_phase=phase.value,
                    target_price=target,
                    sl_price=sl_pts,
                    quality_score=q_score,
                    reason='GZ v9 SUPPLY zone %.0f-%.0f (%.1fx, %dIC, %s) T=%.0f SL=%drs' % (
                        zone.bot, zone.top, zone.body_mult,
                        zone.inst_candle_count, phase.value, target, sl_pts),
                ))
                zone.retest_count += 1
                zone.last_retest = timestamp

    # ── Target Finding ────────────────────────────────────────────────────

    def _find_target(self, symbol, spot, direction):
        """Find zone-to-zone target or POC target."""
        poc = self._poc.get(symbol, 0)

        if direction == 'UP':
            # Find nearest supply zone above spot
            supply_targets = [z.bot for z in self._supply_zones[symbol]
                              if z.active and z.bot > spot]
            if supply_targets:
                return min(supply_targets)
            # Use POC if above spot
            if poc > spot:
                return poc
            # Fallback: spot + 0.5%
            return spot * 1.005
        else:
            # Find nearest demand zone below spot
            demand_targets = [z.top for z in self._demand_zones[symbol]
                              if z.active and z.top < spot]
            if demand_targets:
                return max(demand_targets)
            if poc < spot:
                return poc
            return spot * 0.995

    # ── Signal Output ─────────────────────────────────────────────────────

    def get_signals(self, symbol, timestamp=None):
        """Get pending Ghost Zone v9 signals."""
        if timestamp is None:
            timestamp = datetime.now()

        signals = []
        pending = self._pending_signals.get(symbol, [])
        if not pending:
            return signals

        # Per-symbol cooldown
        cooldown_secs = SIGNAL_COOLDOWN_SECS.get(symbol, SIGNAL_COOLDOWN_DEFAULT) if isinstance(SIGNAL_COOLDOWN_SECS, dict) else SIGNAL_COOLDOWN_SECS
        last = self._last_signal_time[symbol]
        if (timestamp - last).total_seconds() < cooldown_secs:
            self._pending_signals[symbol] = []
            return signals

        # BB squeeze filter: per-symbol threshold (MC-optimized)
        sq_thresh = BB_SQUEEZE_THRESHOLD_MAP.get(symbol, BB_SQUEEZE_DEFAULT)
        if sq_thresh > 0:
            bb = self._bb_data.get(symbol, {})
            width_ratio = bb.get('width_ratio', 1.0)
            if width_ratio < sq_thresh:
                self._pending_signals[symbol] = []
                logger.debug("[GZ v9] %s Signal blocked -- BB squeeze (ratio=%.2f < %.2f)" % (
                    symbol, width_ratio, sq_thresh))
                return signals

        # Take strongest signal
        best = max(pending, key=lambda s: (s.inst_candle_count, s.quality_score, s.zone_mult))

        # R:R filter
        sl_pts = FIXED_SL_RS.get(symbol, 40)
        reward_pts = abs(best.target_price - best.spot)
        min_rr = MIN_RR_RATIO.get(symbol, MIN_RR_DEFAULT) if isinstance(MIN_RR_RATIO, dict) else MIN_RR_RATIO
        rr = reward_pts / sl_pts if sl_pts > 0 else 0
        if rr < min_rr:
            self._pending_signals[symbol] = []
            return signals

        signals.append({
            'type': best.direction + '_GZ9',
            'strategy': 'Ghost Zone v9',
            'zone_type': best.zone_type,
            'zone_top': best.zone_top,
            'zone_bot': best.zone_bot,
            'zone_range': '%d-%d' % (int(best.zone_bot), int(best.zone_top)),
            'zone_mult': best.zone_mult,
            'spot': best.spot,
            'signal_type': best.signal_type,
            'bounce_pct': best.bounce_pct,
            'inst_candle_count': best.inst_candle_count,
            'market_phase': best.market_phase,
            'target_price': best.target_price,
            'sl_rs': sl_pts,
            'quality_score': best.quality_score,
            'target_pct': abs(best.target_price - best.spot) / best.spot * 100,
            'sl_pct': 10,  # Fallback for paper_trader compatibility
            'rr_ratio': round(rr, 2),
            'reason': best.reason,
        })

        self._last_signal_time[symbol] = timestamp
        self._pending_signals[symbol] = []
        return signals

    # ── Status / Debug ────────────────────────────────────────────────────

    def get_zones(self, symbol):
        """Get active zones for display."""
        demand = [z for z in self._demand_zones.get(symbol, []) if z.active]
        supply = [z for z in self._supply_zones.get(symbol, []) if z.active]
        return {
            'demand': [{'top': z.top, 'bot': z.bot, 'mult': z.body_mult,
                        'retests': z.retest_count, 'inst_candles': z.inst_candle_count}
                       for z in demand],
            'supply': [{'top': z.top, 'bot': z.bot, 'mult': z.body_mult,
                        'retests': z.retest_count, 'inst_candles': z.inst_candle_count}
                       for z in supply],
            'phase': self._market_phase[symbol].value,
            'poc': self._poc.get(symbol, 0),
            'bb': self._bb_data.get(symbol, {}),
            'fvg_count': len([f for f in self._fvgs.get(symbol, []) if not f.filled]),
        }

    def reset_day(self):
        """Reset for new trading day. Keep multi-day zones."""
        for sym in list(self._candles.keys()):
            # Keep unfilled zones (they carry forward as stronger zones)
            # But reset daily candles (keep last hour for warmup)
            if len(self._candles[sym]) > 20:
                self._candles[sym] = self._candles[sym][-20:]

        self._pending_signals.clear()
        self._fvgs.clear()
        logger.info("[GhostZone v9] Day reset — kept unfilled zones, cleared FVGs")
