"""
Ghost Zone V2 — Multi-Timeframe Demand/Supply Zone Strategy
=============================================================
Redesigned based on Ghost Trade India's zone indicator approach:
  - 4H zones (RBD/DBR pattern detection) plotted on 5m/3m entries
  - Signal types: GHST_SE, GHST_BE, GHST_TRP, GHST_LVT
  - Zone quality scoring, polarity flip, institutional volume confirmation

Zone Detection (4H timeframe):
  - Drop-Base-Rally (DBR) → Demand Zone (institutional buying)
  - Rally-Base-Drop (RBD) → Supply Zone (institutional selling)

Entry Logic (5m timeframe):
  - GHST_SE: Setup Entry at demand zone with bullish confirmation
  - GHST_BE: Bearish Entry at supply zone with bearish confirmation
  - GHST_TRP: Trap signal — fakeout/stop hunt reversal
  - GHST_LVT: Level Validation Test — zone freshness confirmation
"""

import pandas as pd
import numpy as np
from collections import namedtuple

# ── Zone Data Structures ─────────────────────────────────────────────────────
Zone = namedtuple('Zone', [
    'zone_type',     # 'demand' or 'supply'
    'low', 'high',   # Zone boundaries
    'created_at',    # Bar index when zone was created
    'created_time',  # Datetime of creation
    'quality',       # Quality score (1-5)
    'retests',       # Number of times zone has been retested
    'broken',        # Whether zone has been decisively broken
    'flipped',       # Whether zone has undergone polarity flip
    'impulse_vol',   # Volume during impulse candle
    'base_candles',  # Number of base candles
])


class GhostZoneV2:
    """Multi-timeframe Ghost Zone strategy with RBD/DBR zone detection."""

    # Zone detection parameters (4H timeframe)
    IMPULSE_ATR_MULT = 0.8      # Body > 0.8x ATR = impulse candle
    BASE_BODY_ATR_MULT = 0.6    # Body < 0.6x ATR = base candle
    VOLUME_THRESHOLD = 1.1       # Impulse volume > 1.1x average
    MIN_ZONE_QUALITY = 1         # Minimum score to use zone
    MAX_ACTIVE_ZONES = 15        # Max zones to track per direction
    ZONE_BREAK_ATR_MULT = 0.5   # Close beyond zone by 0.5x ATR = broken

    # Entry parameters (5m timeframe)
    RSI_OVERSOLD = 45            # RSI < 45 at demand = strong setup
    RSI_OVERBOUGHT = 55          # RSI > 55 at supply = strong setup
    SL_ATR_MULT = 0.5            # SL beyond zone edge by 0.5x ATR
    TARGET_RR = 2.0              # Target = 2:1 risk-reward
    TRAP_LOOKBACK = 15           # Candles to detect trap reversal (~75min on 5m)
    LVT_PROXIMITY_ATR = 0.15    # Within 0.15x ATR of zone edge = LVT
    MIN_SL_DISTANCE_PCT = 0.002  # SL must be at least 0.2% from entry

    # V3 Enhancements: Trailing Stop & Exhaustion (from Ghost Trader Masterclass)
    TRAIL_TRIGGER_RR = 1.0       # Activate trailing after 1:1 RR achieved
    TRAIL_ATR_MULT = 0.8         # Trail SL by 0.8x ATR behind price
    CRYPTO_EXHAUSTION_PCT = 3.0  # Exit if 3% move from entry (exhaustion)
    NIFTY_EXHAUSTION = 300       # Nifty: 300 point exhaustion
    BANKNIFTY_EXHAUSTION = 1000  # BankNifty: 1000 point exhaustion

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k.upper()):
                setattr(self, k.upper(), v)

    # ── Indicator Helpers ─────────────────────────────────────────────────
    @staticmethod
    def ema(series, span):
        return series.ewm(span=span, adjust=False).mean()

    @staticmethod
    def atr(df, period=14):
        h, l, c = df['High'], df['Low'], df['Close']
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, 1e-10)
        return 100 - (100 / (1 + rs))

    # ── Zone Detection (4H Timeframe) ─────────────────────────────────────

    def detect_zones(self, df_4h):
        """
        Detect demand and supply zones from 4H candles using RBD/DBR patterns.

        Drop-Base-Rally (DBR) → Demand Zone:
          [Bearish impulse] → [Base candles (small body)] → [Bullish impulse]

        Rally-Base-Drop (RBD) → Supply Zone:
          [Bullish impulse] → [Base candles (small body)] → [Bearish impulse]

        Returns list of Zone namedtuples.
        """
        atr_series = self.atr(df_4h, 14)
        avg_vol = df_4h['Volume'].rolling(20).mean()
        body = (df_4h['Close'] - df_4h['Open']).abs()

        zones = []
        n = len(df_4h)

        for i in range(2, n - 2):
            current_atr = atr_series.iloc[i]
            if pd.isna(current_atr) or current_atr <= 0:
                continue

            current_avg_vol = avg_vol.iloc[i]
            if pd.isna(current_avg_vol) or current_avg_vol <= 0:
                continue

            # Check for base candle (small body)
            is_base = body.iloc[i] < current_atr * self.BASE_BODY_ATR_MULT

            if not is_base:
                continue

            # Look for impulse BEFORE the base (1-2 bars back)
            # Look for impulse AFTER the base (1-2 bars forward)
            # Check multiple base lengths (1-4 candles)

            for base_len in range(1, 5):
                if i - 1 < 0 or i + base_len >= n:
                    continue

                # Check all base candles are small body
                all_base = True
                for b in range(base_len):
                    if i + b >= n:
                        all_base = False
                        break
                    if body.iloc[i + b] >= current_atr * self.BASE_BODY_ATR_MULT:
                        all_base = False
                        break
                if not all_base:
                    break  # If this base candle fails, longer ones will too

                # Pre-base candle (impulse before base)
                pre_idx = i - 1
                # Post-base candle (impulse after base)
                post_idx = i + base_len
                if post_idx >= n:
                    continue

                pre_body = df_4h['Close'].iloc[pre_idx] - df_4h['Open'].iloc[pre_idx]
                post_body = df_4h['Close'].iloc[post_idx] - df_4h['Open'].iloc[post_idx]

                pre_is_impulse = abs(pre_body) > current_atr * self.IMPULSE_ATR_MULT
                post_is_impulse = abs(post_body) > current_atr * self.IMPULSE_ATR_MULT

                if not (pre_is_impulse and post_is_impulse):
                    continue

                # DBR: bearish impulse → base → bullish impulse = DEMAND
                if pre_body < 0 and post_body > 0:
                    # Demand zone = base area
                    zone_low = min(df_4h['Low'].iloc[i:i + base_len].min(),
                                   df_4h['Low'].iloc[pre_idx])
                    zone_high = max(df_4h['High'].iloc[i:i + base_len].max(),
                                    df_4h['Open'].iloc[i])  # Use open of first base

                    # Don't let zone be too narrow
                    if zone_high - zone_low < current_atr * 0.1:
                        zone_high = zone_low + current_atr * 0.3

                    # Quality scoring
                    quality = self._score_zone(
                        df_4h, i, base_len, pre_idx, post_idx,
                        current_atr, current_avg_vol, zones, 'demand'
                    )

                    if quality >= self.MIN_ZONE_QUALITY:
                        z = Zone(
                            zone_type='demand',
                            low=zone_low, high=zone_high,
                            created_at=i,
                            created_time=df_4h['Date'].iloc[i] if 'Date' in df_4h.columns else i,
                            quality=quality,
                            retests=0, broken=False, flipped=False,
                            impulse_vol=df_4h['Volume'].iloc[post_idx],
                            base_candles=base_len,
                        )
                        zones.append(z)

                # RBD: bullish impulse → base → bearish impulse = SUPPLY
                elif pre_body > 0 and post_body < 0:
                    zone_high = max(df_4h['High'].iloc[i:i + base_len].max(),
                                    df_4h['High'].iloc[pre_idx])
                    zone_low = min(df_4h['Low'].iloc[i:i + base_len].min(),
                                   df_4h['Close'].iloc[i])

                    if zone_high - zone_low < current_atr * 0.1:
                        zone_low = zone_high - current_atr * 0.3

                    quality = self._score_zone(
                        df_4h, i, base_len, pre_idx, post_idx,
                        current_atr, current_avg_vol, zones, 'supply'
                    )

                    if quality >= self.MIN_ZONE_QUALITY:
                        z = Zone(
                            zone_type='supply',
                            low=zone_low, high=zone_high,
                            created_at=i,
                            created_time=df_4h['Date'].iloc[i] if 'Date' in df_4h.columns else i,
                            quality=quality,
                            retests=0, broken=False, flipped=False,
                            impulse_vol=df_4h['Volume'].iloc[post_idx],
                            base_candles=base_len,
                        )
                        zones.append(z)

                break  # Use first valid base length found

        return zones

    def _score_zone(self, df, base_start, base_len, pre_idx, post_idx,
                    atr, avg_vol, existing_zones, zone_type):
        """Score zone quality (1-5)."""
        score = 0

        # +1 if impulse volume > 1.5x average (institutional activity)
        impulse_vol = df['Volume'].iloc[post_idx]
        if impulse_vol > avg_vol * 1.5:
            score += 1

        # +1 if base has >= 2 candles (stronger consolidation)
        if base_len >= 2:
            score += 1

        # +1 if impulse move > 1.5x ATR (strong breakaway)
        post_body = abs(df['Close'].iloc[post_idx] - df['Open'].iloc[post_idx])
        if post_body > atr * 1.5:
            score += 1

        # +1 if impulse volume > 1.3x avg (minimum institutional threshold)
        if impulse_vol > avg_vol * self.VOLUME_THRESHOLD:
            score += 1

        # +1 if no overlapping zone exists (fresh area)
        base_low = df['Low'].iloc[base_start:base_start + base_len].min()
        base_high = df['High'].iloc[base_start:base_start + base_len].max()
        overlapping = False
        for z in existing_zones:
            if z.zone_type == zone_type:
                if z.low <= base_high and z.high >= base_low:
                    overlapping = True
                    break
        if not overlapping:
            score += 1

        return score

    def update_zones(self, zones, df_4h, current_idx):
        """
        Update zone status: check for breaks and polarity flips.
        Returns updated list of active zones.
        """
        atr_val = self.atr(df_4h, 14).iloc[current_idx] if current_idx < len(df_4h) else 0
        if pd.isna(atr_val) or atr_val <= 0:
            return zones

        active_zones = []
        for z in zones:
            if z.created_at >= current_idx:
                active_zones.append(z)
                continue

            close = df_4h['Close'].iloc[current_idx]

            if z.zone_type == 'demand' and not z.broken:
                # Check if demand zone is broken (close below zone by > threshold)
                if close < z.low - atr_val * self.ZONE_BREAK_ATR_MULT:
                    # Broken demand → becomes supply (polarity flip)
                    flipped = z._replace(
                        zone_type='supply', broken=True, flipped=True
                    )
                    active_zones.append(flipped)
                else:
                    active_zones.append(z)

            elif z.zone_type == 'supply' and not z.broken:
                if close > z.high + atr_val * self.ZONE_BREAK_ATR_MULT:
                    flipped = z._replace(
                        zone_type='demand', broken=True, flipped=True
                    )
                    active_zones.append(flipped)
                else:
                    active_zones.append(z)
            else:
                active_zones.append(z)

        # Keep only most recent zones per direction
        demand = [z for z in active_zones if z.zone_type == 'demand' and not z.broken]
        supply = [z for z in active_zones if z.zone_type == 'supply' and not z.broken]
        demand.sort(key=lambda z: z.created_at, reverse=True)
        supply.sort(key=lambda z: z.created_at, reverse=True)

        return demand[:self.MAX_ACTIVE_ZONES] + supply[:self.MAX_ACTIVE_ZONES]

    # ── Entry Signal Detection (5m Timeframe) ─────────────────────────────

    def find_entries(self, df_5m, active_zones, bar_idx, atr_5m, rsi_5m, avg_vol_5m):
        """
        Find entry signals on 5m chart against 4H zones.

        Returns list of signal dicts with type, direction, zone, entry, sl, target.
        """
        signals = []
        if bar_idx < 2 or bar_idx >= len(df_5m):
            return signals

        bar = df_5m.iloc[bar_idx]
        prev_bar = df_5m.iloc[bar_idx - 1]
        close = bar['Close']
        open_p = bar['Open']
        high = bar['High']
        low = bar['Low']
        vol = bar['Volume']

        current_atr = atr_5m.iloc[bar_idx] if not pd.isna(atr_5m.iloc[bar_idx]) else 0
        current_rsi = rsi_5m.iloc[bar_idx] if not pd.isna(rsi_5m.iloc[bar_idx]) else 50
        current_avg_vol = avg_vol_5m.iloc[bar_idx] if not pd.isna(avg_vol_5m.iloc[bar_idx]) else 1

        if current_atr <= 0:
            return signals

        for zone in active_zones:
            # ── GHST_SE: Setup Entry at Demand Zone (BUY/LONG) ──
            if zone.zone_type == 'demand':
                # Price enters or touches demand zone (relaxed: within 0.5% below zone low)
                if low <= zone.high and low >= zone.low * 0.99:
                    # Reversal confirmation: bullish candle at zone
                    bullish_confirm = (
                        close > open_p and                          # Green candle
                        (close - open_p) > (high - low) * 0.3      # Body > 30% of range
                    )
                    # Alternative: hammer pattern
                    hammer = (
                        close > open_p and
                        (min(open_p, close) - low) > (high - low) * 0.4 and  # Long lower wick
                        abs(close - open_p) < (high - low) * 0.4            # Small body
                    )
                    # Alternative: doji with long lower shadow at zone
                    doji_at_zone = (
                        abs(close - open_p) < (high - low) * 0.2 and  # Very small body
                        (min(open_p, close) - low) > (high - low) * 0.5  # Long lower wick
                    )
                    # Previous candle context (relaxed: bearish OR came from above)
                    momentum_confirm = (prev_bar['Close'] < prev_bar['Open'] or
                                       prev_bar['Close'] < zone.high * 1.005)

                    if (bullish_confirm or hammer or doji_at_zone) and momentum_confirm:
                        # Volume and RSI confirmation (optional, boosts quality)
                        vol_confirm = vol > current_avg_vol * 0.8
                        rsi_confirm = current_rsi < self.RSI_OVERSOLD

                        sl = zone.low - current_atr * self.SL_ATR_MULT

                        # CRITICAL: SL must be below entry for LONG
                        if sl >= close:
                            sl = close * (1 - self.MIN_SL_DISTANCE_PCT)

                        risk = close - sl
                        # Skip if risk is too small (< 0.1%)
                        if risk < close * 0.001:
                            continue

                        target = close + risk * self.TARGET_RR

                        # Find nearest supply zone as alternative target
                        supply_targets = [z.low for z in active_zones
                                         if z.zone_type == 'supply' and z.low > close]
                        if supply_targets:
                            target = min(min(supply_targets), target)

                        # Skip if target is below entry (invalid RR)
                        if target <= close:
                            continue

                        sig_quality = zone.quality
                        if vol_confirm:
                            sig_quality += 1
                        if rsi_confirm:
                            sig_quality += 1

                        signals.append({
                            'type': 'GHST_SE',
                            'direction': 1,  # LONG / BUY CE
                            'zone': zone,
                            'entry': close,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': (target - close) / max(risk, 0.01),
                            'quality': sig_quality,
                            'reason': f"GHST_SE: Demand zone retest [{zone.low:.0f}-{zone.high:.0f}] "
                                     f"Q={zone.quality} RSI={current_rsi:.0f}",
                        })

            # ── GHST_BE: Bearish Entry at Supply Zone (SELL/SHORT) ──
            elif zone.zone_type == 'supply':
                if high >= zone.low and high <= zone.high * 1.01:
                    bearish_confirm = (
                        close < open_p and
                        (open_p - close) > (high - low) * 0.3
                    )
                    shooting_star = (
                        close < open_p and
                        (high - max(open_p, close)) > (high - low) * 0.4 and
                        abs(close - open_p) < (high - low) * 0.4
                    )
                    # Bearish doji at supply zone
                    doji_at_zone = (
                        abs(close - open_p) < (high - low) * 0.2 and
                        (high - max(open_p, close)) > (high - low) * 0.5
                    )
                    momentum_confirm = (prev_bar['Close'] > prev_bar['Open'] or
                                       prev_bar['Close'] > zone.low * 0.995)

                    if (bearish_confirm or shooting_star or doji_at_zone) and momentum_confirm:
                        vol_confirm = vol > current_avg_vol * 0.8
                        rsi_confirm = current_rsi > self.RSI_OVERBOUGHT

                        sl = zone.high + current_atr * self.SL_ATR_MULT

                        # CRITICAL: SL must be above entry for SHORT
                        if sl <= close:
                            sl = close * (1 + self.MIN_SL_DISTANCE_PCT)

                        risk = sl - close
                        if risk < close * 0.001:
                            continue

                        target = close - risk * self.TARGET_RR

                        demand_targets = [z.high for z in active_zones
                                         if z.zone_type == 'demand' and z.high < close]
                        if demand_targets:
                            target = max(max(demand_targets), target)

                        # Skip if target is above entry (invalid RR)
                        if target >= close:
                            continue

                        sig_quality = zone.quality
                        if vol_confirm:
                            sig_quality += 1
                        if rsi_confirm:
                            sig_quality += 1

                        signals.append({
                            'type': 'GHST_BE',
                            'direction': -1,  # SHORT / BUY PE
                            'zone': zone,
                            'entry': close,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': (close - target) / max(risk, 0.01),
                            'quality': sig_quality,
                            'reason': f"GHST_BE: Supply zone retest [{zone.low:.0f}-{zone.high:.0f}] "
                                     f"Q={zone.quality} RSI={current_rsi:.0f}",
                        })

        # ── GHST_TRP: Trap Detection (Fakeout / Stop Hunt) ──
        # Check if price recently broke through a zone and reversed back inside
        if bar_idx >= self.TRAP_LOOKBACK:
            for zone in active_zones:
                recent = df_5m.iloc[bar_idx - self.TRAP_LOOKBACK:bar_idx]

                if zone.zone_type == 'demand':
                    # Trap: price broke BELOW demand then reversed back inside/above
                    broke_below = (recent['Low'] < zone.low).any()
                    # Relaxed: price just needs to be back inside the zone or above
                    now_recovered = close > zone.low
                    # Must be bullish candle with decent body
                    is_bullish = close > open_p and (close - open_p) > (high - low) * 0.25

                    if broke_below and now_recovered and is_bullish:
                        extreme_low = recent['Low'].min()
                        sl = extreme_low - current_atr * 0.3

                        # SL sanity check
                        if sl >= close:
                            sl = close * (1 - self.MIN_SL_DISTANCE_PCT)

                        risk = close - sl
                        if risk < close * 0.001:
                            continue

                        target = close + risk * self.TARGET_RR

                        signals.append({
                            'type': 'GHST_TRP',
                            'direction': 1,
                            'zone': zone,
                            'entry': close,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': self.TARGET_RR,
                            'quality': zone.quality + 2,  # Traps are high quality
                            'reason': f"GHST_TRP: Bull trap at demand [{zone.low:.0f}-{zone.high:.0f}] "
                                     f"broke to {extreme_low:.0f} then reversed",
                        })

                elif zone.zone_type == 'supply':
                    broke_above = (recent['High'] > zone.high).any()
                    now_recovered = close < zone.high
                    is_bearish = close < open_p and (open_p - close) > (high - low) * 0.25

                    if broke_above and now_recovered and is_bearish:
                        extreme_high = recent['High'].max()
                        sl = extreme_high + current_atr * 0.3

                        if sl <= close:
                            sl = close * (1 + self.MIN_SL_DISTANCE_PCT)

                        risk = sl - close
                        if risk < close * 0.001:
                            continue

                        target = close - risk * self.TARGET_RR

                        signals.append({
                            'type': 'GHST_TRP',
                            'direction': -1,
                            'zone': zone,
                            'entry': close,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': self.TARGET_RR,
                            'quality': zone.quality + 2,
                            'reason': f"GHST_TRP: Bear trap at supply [{zone.low:.0f}-{zone.high:.0f}] "
                                     f"broke to {extreme_high:.0f} then reversed",
                        })

        # Sort by quality (highest first)
        signals.sort(key=lambda s: s['quality'], reverse=True)
        return signals

    def check_lvt(self, df_5m, active_zones, bar_idx, atr_5m):
        """
        Check for Level Validation Test (GHST_LVT).
        Price approaches zone boundary but doesn't enter fully, then bounces.
        Used to validate zone freshness, not direct entry.
        """
        if bar_idx < 2 or bar_idx >= len(df_5m):
            return []

        bar = df_5m.iloc[bar_idx]
        current_atr = atr_5m.iloc[bar_idx] if not pd.isna(atr_5m.iloc[bar_idx]) else 0
        if current_atr <= 0:
            return []

        lvts = []
        for zone in active_zones:
            proximity = current_atr * self.LVT_PROXIMITY_ATR

            if zone.zone_type == 'demand':
                # Price came close to zone top but didn't enter
                if (bar['Low'] <= zone.high + proximity and
                    bar['Low'] > zone.high and
                    bar['Close'] > zone.high + proximity):
                    lvts.append({
                        'type': 'GHST_LVT',
                        'zone': zone,
                        'reason': f"GHST_LVT: Demand zone validated [{zone.low:.0f}-{zone.high:.0f}]"
                    })

            elif zone.zone_type == 'supply':
                if (bar['High'] >= zone.low - proximity and
                    bar['High'] < zone.low and
                    bar['Close'] < zone.low - proximity):
                    lvts.append({
                        'type': 'GHST_LVT',
                        'zone': zone,
                        'reason': f"GHST_LVT: Supply zone validated [{zone.low:.0f}-{zone.high:.0f}]"
                    })

        return lvts

    # ── V3 Enhancements: Trailing Stop & Exhaustion ─────────────────────

    def should_trail(self, position, current_price, current_atr):
        """
        Check if trailing stop should be activated/updated.
        Activates after TRAIL_TRIGGER_RR achieved, then trails by TRAIL_ATR_MULT.
        Returns new SL or None.
        """
        entry = position['entry']
        direction = position['direction']
        current_sl = position['sl']

        if direction == 1:  # LONG
            pnl_dist = current_price - entry
            risk = entry - position.get('original_sl', current_sl)
            if risk <= 0:
                return None
            if pnl_dist >= risk * self.TRAIL_TRIGGER_RR:
                new_sl = max(
                    entry + risk * 0.1,  # At least breakeven + 10%
                    current_price - current_atr * self.TRAIL_ATR_MULT
                )
                if new_sl > current_sl:
                    return new_sl
        else:  # SHORT
            pnl_dist = entry - current_price
            risk = position.get('original_sl', current_sl) - entry
            if risk <= 0:
                return None
            if pnl_dist >= risk * self.TRAIL_TRIGGER_RR:
                new_sl = min(
                    entry - risk * 0.1,
                    current_price + current_atr * self.TRAIL_ATR_MULT
                )
                if new_sl < current_sl:
                    return new_sl
        return None

    def check_exhaustion(self, position, current_price, symbol=''):
        """
        Check if exhaustion target reached (Ghost Trader 300/1000 rule).
        For equity: 300pts Nifty, 1000pts BankNifty
        For crypto: CRYPTO_EXHAUSTION_PCT move from entry
        """
        entry = position['entry']
        if position['direction'] == 1:
            move = current_price - entry
        else:
            move = entry - current_price

        if 'NIFTY' in symbol.upper() and 'BANK' not in symbol.upper():
            return move >= self.NIFTY_EXHAUSTION
        elif 'BANKNIFTY' in symbol.upper():
            return move >= self.BANKNIFTY_EXHAUSTION

        pct_move = move / entry * 100
        return pct_move >= self.CRYPTO_EXHAUSTION_PCT


def aggregate_to_4h(df_1h):
    """
    Aggregate 1-hour candles to 4-hour candles.
    Groups by 4-hour blocks starting from market open.
    """
    df = df_1h.copy()
    if 'Date' not in df.columns:
        df['Date'] = df.index

    df['Date'] = pd.to_datetime(df['Date'])
    df = df.set_index('Date')

    # Group by 4-hour blocks
    df_4h = df.resample('4h').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum',
    }).dropna(subset=['Open'])

    df_4h = df_4h.reset_index()
    df_4h = df_4h.rename(columns={'Date': 'Date'})
    return df_4h


def align_5m_to_4h_zones(df_5m, df_4h, zones):
    """
    For each 5m bar, find which 4H zones were active at that time.
    Returns dict mapping 5m index → list of active zones.
    """
    if 'Date' not in df_5m.columns:
        return {}

    # Sort zones by creation time
    zone_times = {}
    for z in zones:
        if isinstance(z.created_time, (pd.Timestamp, np.datetime64)):
            zone_times[z] = pd.Timestamp(z.created_time)
        else:
            # created_time is an index
            if z.created_at < len(df_4h) and 'Date' in df_4h.columns:
                zone_times[z] = pd.Timestamp(df_4h['Date'].iloc[z.created_at])
            else:
                zone_times[z] = pd.Timestamp.min

    active_map = {}
    for i in range(len(df_5m)):
        bar_time = pd.Timestamp(df_5m['Date'].iloc[i])
        # Active zones are those created before this 5m bar
        active = [z for z, t in zone_times.items()
                  if t <= bar_time and not z.broken]
        active_map[i] = active

    return active_map
