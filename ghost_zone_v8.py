"""
Ghost Zone v8 — Reverse-Engineered Institutional Zone Detection
================================================================
Based on GTI Pro Scalping Zone indicator analysis.

Algorithm:
  1. Build 3-min candles from Zerodha 1-min spot ticks
  2. Detect institutional candles: body >= 2.0x rolling 20-bar average
  3. Create DEMAND zones (bullish inst candles) and SUPPLY zones (bearish)
  4. Track zone retests — GHST_ST = rejection candle at zone = ENTRY signal
  5. BUY CE at demand zone retest, BUY PE at supply zone retest

Zone lifecycle:
  - Created when institutional candle detected
  - Active until violated (close beyond zone boundary)
  - Max 5 zones tracked per type (demand/supply) per symbol

No volume requirement (indices have zero volume data).
No OI requirement (optional enhancement only).
"""

import logging
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BODY_MULT_THRESHOLD = 2.0
LOOKBACK_BARS = 20
MIN_BODY_PTS = {'NIFTY': 15, 'BANKNIFTY': 40, 'SENSEX': 50}
MIN_BOUNCE_PCT = 0.05
MAX_ZONES_PER_TYPE = 5
ZONE_CACHE_TTL = 1800  # 30 min


@dataclass
class Zone:
    created_at: datetime
    zone_type: str  # 'DEMAND' or 'SUPPLY'
    top: float
    bot: float
    body: float
    body_mult: float
    active: bool = True
    retest_count: int = 0
    last_retest: Optional[datetime] = None


@dataclass
class GhostSignal:
    timestamp: datetime
    signal_type: str  # 'GHST_ST' or 'GHST_LVT'
    zone_type: str
    zone_top: float
    zone_bot: float
    zone_mult: float
    direction: str  # 'BUY_CE' or 'BUY_PE'
    spot: float
    bounce_pct: float


class GhostZoneV8:
    """Live Ghost Zone v8 detector.

    Usage in paper_trader.py:
        self.ghost_v8 = GhostZoneV8()

        # In scan loop, after getting spot:
        self.ghost_v8.update_tick(symbol, spot, timestamp)
        signals = self.ghost_v8.get_signals(symbol, timestamp)

        for sig in signals:
            # sig has: direction ('BUY_CE'/'BUY_PE'), zone info, etc.
            # Create trade signal in standard format
    """

    def __init__(self):
        # Per-symbol tick history (1-min LTP readings)
        self._ticks = defaultdict(list)  # symbol -> [(timestamp, ltp)]
        # Per-symbol 3-min candles
        self._candles_3m = defaultdict(list)  # symbol -> [dict]
        # Per-symbol zones
        self._demand_zones = defaultdict(list)  # symbol -> [Zone]
        self._supply_zones = defaultdict(list)  # symbol -> [Zone]
        # Per-symbol signals (pending)
        self._pending_signals = defaultdict(list)
        # Cooldown tracking
        self._last_signal_time = defaultdict(lambda: datetime.min)
        self._signal_cooldown = 180  # 3 min between signals per symbol

        logger.info("[GhostZone v8] Initialized — institutional zone detection (no volume required)")

    def update_tick(self, symbol, spot, timestamp=None):
        """Feed a spot tick. Call every scan cycle (~every minute)."""
        if timestamp is None:
            timestamp = datetime.now()
        if not isinstance(timestamp, datetime):
            timestamp = pd.Timestamp(timestamp).to_pydatetime()

        self._ticks[symbol].append((timestamp, float(spot)))

        # Keep max 500 ticks (~8 hours of 1-min data)
        if len(self._ticks[symbol]) > 500:
            self._ticks[symbol] = self._ticks[symbol][-500:]

        # Rebuild 3-min candles
        self._rebuild_candles(symbol)

        # Detect new institutional candles and create zones
        self._detect_zones(symbol)

        # Check for zone retests
        self._check_retests(symbol, spot, timestamp)

    def _rebuild_candles(self, symbol):
        """Build 3-min OHLC from tick history."""
        ticks = self._ticks[symbol]
        if len(ticks) < 5:
            return

        df = pd.DataFrame(ticks, columns=['timestamp', 'ltp'])
        df['ts_3m'] = df['timestamp'].apply(
            lambda x: x.replace(second=0, microsecond=0,
                                minute=x.minute - x.minute % 3)
        )

        candles = []
        for ts, grp in df.groupby('ts_3m'):
            if len(grp) == 0:
                continue
            candles.append({
                'timestamp': ts,
                'open': grp['ltp'].iloc[0],
                'high': grp['ltp'].max(),
                'low': grp['ltp'].min(),
                'close': grp['ltp'].iloc[-1],
            })

        if len(candles) < 10:
            return

        # Compute body metrics
        for i, c in enumerate(candles):
            c['body'] = abs(c['close'] - c['open'])
            c['range'] = c['high'] - c['low']
            c['direction'] = 'BULL' if c['close'] > c['open'] else 'BEAR'

            # Rolling average body
            start = max(0, i - LOOKBACK_BARS)
            bodies = [candles[j]['body'] for j in range(start, i + 1)]
            c['avg_body'] = np.mean(bodies) if bodies else 0
            c['body_mult'] = c['body'] / c['avg_body'] if c['avg_body'] > 0 else 0

        self._candles_3m[symbol] = candles

    def _detect_zones(self, symbol):
        """Detect institutional candles and create zones."""
        candles = self._candles_3m[symbol]
        if len(candles) < 15:
            return

        min_body = MIN_BODY_PTS.get(symbol, 15)

        # Only check last 5 candles (avoid reprocessing old ones)
        for i in range(max(len(candles) - 5, LOOKBACK_BARS), len(candles)):
            c = candles[i]

            if c['body_mult'] < BODY_MULT_THRESHOLD:
                continue
            if c['body'] < min_body:
                continue

            zone_top = max(c['open'], c['close'])
            zone_bot = min(c['open'], c['close'])

            # Check if zone already exists (avoid duplicates)
            existing = self._demand_zones[symbol] + self._supply_zones[symbol]
            is_duplicate = False
            for ez in existing:
                if abs(ez.top - zone_top) < min_body and abs(ez.bot - zone_bot) < min_body:
                    is_duplicate = True
                    break
            if is_duplicate:
                continue

            # Check if immediately violated (next 3 bars)
            violated = False
            for j in range(i + 1, min(i + 4, len(candles))):
                nb = candles[j]
                if c['direction'] == 'BULL' and nb['close'] < zone_bot * 0.998:
                    violated = True
                    break
                if c['direction'] == 'BEAR' and nb['close'] > zone_top * 1.002:
                    violated = True
                    break

            if violated:
                continue

            zone = Zone(
                created_at=c['timestamp'],
                zone_type='DEMAND' if c['direction'] == 'BULL' else 'SUPPLY',
                top=zone_top, bot=zone_bot,
                body=c['body'], body_mult=c['body_mult'],
            )

            if zone.zone_type == 'DEMAND':
                self._demand_zones[symbol].append(zone)
                if len(self._demand_zones[symbol]) > MAX_ZONES_PER_TYPE:
                    self._demand_zones[symbol] = self._demand_zones[symbol][-MAX_ZONES_PER_TYPE:]
            else:
                self._supply_zones[symbol].append(zone)
                if len(self._supply_zones[symbol]) > MAX_ZONES_PER_TYPE:
                    self._supply_zones[symbol] = self._supply_zones[symbol][-MAX_ZONES_PER_TYPE:]

            logger.info("[GhostZone v8] %s NEW %s zone: %.0f-%.0f (body=%.0f, %.1fx)" % (
                symbol, zone.zone_type, zone.bot, zone.top, zone.body, zone.body_mult))

    def _check_retests(self, symbol, current_spot, timestamp):
        """Check if current price is retesting any active zone."""
        if len(self._candles_3m[symbol]) < 3:
            return

        # Get latest candle
        latest = self._candles_3m[symbol][-1]

        # Check demand zones (BUY CE on bounce)
        for zone in self._demand_zones[symbol]:
            if not zone.active:
                continue

            # Zone violated?
            if latest['close'] < zone.bot * 0.995:
                zone.active = False
                logger.debug("[GhostZone v8] %s DEMAND zone %.0f-%.0f VIOLATED" % (
                    symbol, zone.bot, zone.top))
                continue

            # Retest: price dips into zone and closes above
            if (latest['low'] <= zone.top * 1.002 and
                    latest['low'] >= zone.bot * 0.995 and
                    latest['close'] > zone.top):

                bounce = (latest['close'] - zone.top) / zone.top * 100
                if bounce >= MIN_BOUNCE_PCT:
                    is_bullish = latest['close'] > latest['open']
                    sig_type = 'GHST_ST' if is_bullish else 'GHST_LVT'

                    # Only generate GHST_ST (strong signal)
                    if sig_type == 'GHST_ST':
                        self._pending_signals[symbol].append(GhostSignal(
                            timestamp=timestamp,
                            signal_type=sig_type,
                            zone_type='DEMAND',
                            zone_top=zone.top, zone_bot=zone.bot,
                            zone_mult=zone.body_mult,
                            direction='BUY_CE',
                            spot=current_spot,
                            bounce_pct=bounce,
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
                if reject >= MIN_BOUNCE_PCT:
                    is_bearish = latest['close'] < latest['open']
                    sig_type = 'GHST_ST' if is_bearish else 'GHST_LVT'

                    if sig_type == 'GHST_ST':
                        self._pending_signals[symbol].append(GhostSignal(
                            timestamp=timestamp,
                            signal_type=sig_type,
                            zone_type='SUPPLY',
                            zone_top=zone.top, zone_bot=zone.bot,
                            zone_mult=zone.body_mult,
                            direction='BUY_PE',
                            spot=current_spot,
                            bounce_pct=reject,
                        ))
                        zone.retest_count += 1
                        zone.last_retest = timestamp

    def get_signals(self, symbol, timestamp=None):
        """Get pending Ghost Zone signals for a symbol.

        Returns list of dicts in standard signal format compatible with paper_trader.
        Applies cooldown and deduplication.
        """
        if timestamp is None:
            timestamp = datetime.now()

        signals = []
        pending = self._pending_signals.get(symbol, [])

        if not pending:
            return signals

        # Cooldown check
        last = self._last_signal_time[symbol]
        if (timestamp - last).total_seconds() < self._signal_cooldown:
            self._pending_signals[symbol] = []
            return signals

        # Take the strongest signal (highest zone_mult)
        best = max(pending, key=lambda s: s.zone_mult)

        signals.append({
            'type': best.direction + '_GZ8',
            'strategy': 'Ghost Zone v8',
            'zone_type': best.zone_type,
            'zone_top': best.zone_top,
            'zone_bot': best.zone_bot,
            'zone_range': str(int(best.zone_bot)) + '-' + str(int(best.zone_top)),
            'zone_mult': best.zone_mult,
            'spot': best.spot,
            'signal_type': best.signal_type,
            'bounce_pct': best.bounce_pct,
            'target_pct': 20,
            'sl_pct': 10,
            'reason': 'GZ v8 %s zone %d-%d (%.1fx) T=20%% SL=10%%' % (
                best.zone_type, int(best.zone_bot), int(best.zone_top),
                best.zone_mult),
        })

        self._last_signal_time[symbol] = timestamp
        self._pending_signals[symbol] = []

        return signals

    def get_zones(self, symbol):
        """Get current active zones for dashboard display."""
        demand = [z for z in self._demand_zones.get(symbol, []) if z.active]
        supply = [z for z in self._supply_zones.get(symbol, []) if z.active]
        return {
            'demand': [{'top': z.top, 'bot': z.bot, 'mult': z.body_mult,
                        'retests': z.retest_count} for z in demand],
            'supply': [{'top': z.top, 'bot': z.bot, 'mult': z.body_mult,
                        'retests': z.retest_count} for z in supply],
        }

    def reset_day(self):
        """Reset zones at start of new trading day."""
        self._demand_zones.clear()
        self._supply_zones.clear()
        self._pending_signals.clear()
        # Keep tick history (last hour) for warmup
        for sym in self._ticks:
            if len(self._ticks[sym]) > 60:
                self._ticks[sym] = self._ticks[sym][-60:]
        logger.info("[GhostZone v8] Day reset — zones cleared")
