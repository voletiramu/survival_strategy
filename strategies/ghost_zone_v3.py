"""
Ghost Zone V3 - Institutional Footprint Zone Strategy
======================================================
Based on Manish Maheshwari (Ghost Trader) F&O Masterclass methodology:

Zone Detection:
  - Injection (Pin Bar): Long wick + high volume = institutional money injection
  - Bat (Expansion Candle): Large body + volume spike = aggressive momentum
  - Belan (Doji/Mother Candle): Small body + big range + volume = whale fight
  - Zone = [Low, High] of trigger candle
  - Unmitigated zone: price moved away without revisiting

Entry Logic:
  - Wait for price to return to unmitigated zone
  - Fake Breakout Filter: detect retail trap / liquidity sweep
  - Enter at 50% of zone (institutional limit order level)

Risk Management:
  - SL: Below zone bottom (demand) / Above zone top (supply)
  - Target: Exhaustion-based trailing (300pts Nifty, 1000pts BankNifty)
  - For crypto: ATR-based exhaustion trailing

Market Hierarchy:
  - Grandfather: Candlestick structure (primary)
  - Father: Volume/Volatility (confirmation)
  - Mother: Open Interest (validation)
  - Siblings: Indicators (final confirmation only)
"""

import pandas as pd
import numpy as np
from collections import namedtuple

# ── Zone Data Structure ──────────────────────────────────────────────────────
GhostZone = namedtuple('GhostZone', [
    'zone_type',     # 'demand' or 'supply'
    'low', 'high',   # Zone boundaries (candle low/high)
    'mid',           # 50% level (institutional entry point)
    'created_at',    # Bar index when zone was created
    'created_time',  # Datetime of creation
    'quality',       # Quality score (1-5)
    'trigger_type',  # 'injection', 'bat', or 'belan'
    'retests',       # Number of retests
    'broken',        # Whether zone has been invalidated
    'volume_ratio',  # Volume / average volume ratio
    'unmitigated',   # Whether zone is still fresh (price hasn't returned)
])


class GhostZoneV3:
    """
    Institutional Footprint Zone Strategy.

    Uses single high-volume candle patterns to detect institutional zones,
    then enters at the 50% level after a fake breakout / liquidity sweep.
    """

    # ── Zone Detection Parameters ────────────────────────────────────────
    VOLUME_MULT_MIN = 2.0        # Minimum volume spike: 2x average (3x too strict for 4H)
    VOLUME_MULT_STRONG = 4.0     # Strong volume spike: 4x average
    VOL_AVG_PERIOD = 20          # Period for volume average

    # Injection (Pin Bar) parameters
    INJ_WICK_PCT = 0.50          # Wick must be > 50% of total range
    INJ_BODY_PCT = 0.35          # Body must be < 35% of range

    # Bat (Expansion Candle) parameters
    BAT_BODY_PCT = 0.55          # Body must be > 55% of range
    BAT_ATR_MULT = 1.0           # Range must be > 1.0x ATR

    # Belan (Doji/Mother Candle) parameters
    BEL_BODY_PCT = 0.25          # Body must be < 25% of range
    BEL_RANGE_ATR = 1.3          # Range must be > 1.3x ATR

    # Zone management
    MAX_ACTIVE_ZONES = 20        # Max zones to track
    ZONE_EXPIRY_BARS = 500       # Zone expires after 500 bars (~42 hours on 5m)
    ZONE_BREAK_ATR = 0.5         # Zone broken if close breaches by > 0.5x ATR

    # ── Entry Parameters ─────────────────────────────────────────────────
    FAKE_BREAKOUT_LOOKBACK = 20  # Bars to look back for fake breakout
    FAKE_BO_PENETRATION = 0.3    # Must penetrate 30% beyond zone to count
    ENTRY_AT_PCT = 0.50          # Enter at 50% of zone

    # ── Risk Management ──────────────────────────────────────────────────
    SL_BUFFER_ATR = 0.2          # SL buffer beyond zone edge (tighter)
    MIN_RR = 1.2                 # Minimum risk-reward ratio (realistic for 4H zones)
    TRAIL_TRIGGER_RR = 0.8       # Start trailing after 0.8:1 RR achieved
    TRAIL_ATR_MULT = 0.8         # Trail SL by 0.8x ATR (tighter trail)

    # Exhaustion targets (equity - points from trend origin)
    NIFTY_EXHAUSTION = 300
    BANKNIFTY_EXHAUSTION = 1000
    SENSEX_EXHAUSTION = 1000

    # Crypto exhaustion: % move from zone
    CRYPTO_EXHAUSTION_PCT = 3.0  # 3% move = exhaustion

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

    # ── Zone Detection ────────────────────────────────────────────────────

    def detect_zones(self, df):
        """
        Scan chart for institutional footprint candles and create zones.

        Detects 3 candle types:
        1. Injection (Pin Bar): Long wick = institutional money injection
        2. Bat (Expansion): Large body + volume = aggressive momentum
        3. Belan (Doji/Mother): Small body + big range = whale fight

        Returns list of GhostZone namedtuples.
        """
        atr_series = self.atr(df, 14)
        avg_vol = df['Volume'].rolling(self.VOL_AVG_PERIOD).mean()

        body = (df['Close'] - df['Open']).abs()
        candle_range = df['High'] - df['Low']
        upper_wick = df['High'] - df[['Open', 'Close']].max(axis=1)
        lower_wick = df[['Open', 'Close']].min(axis=1) - df['Low']

        zones = []
        n = len(df)

        for i in range(self.VOL_AVG_PERIOD + 14, n - 1):
            current_atr = atr_series.iloc[i]
            current_avg_vol = avg_vol.iloc[i]

            if pd.isna(current_atr) or current_atr <= 0:
                continue
            if pd.isna(current_avg_vol) or current_avg_vol <= 0:
                continue

            cr = candle_range.iloc[i]
            if cr <= 0:
                continue

            vol = df['Volume'].iloc[i]
            vol_ratio = vol / current_avg_vol
            b = body.iloc[i]
            uw = upper_wick.iloc[i]
            lw = lower_wick.iloc[i]

            # ── VOLUME FILTER: Must be >= 3x average ──
            if vol_ratio < self.VOLUME_MULT_MIN:
                continue

            trigger_type = None
            zone_type = None
            quality = 0

            # ── 1. INJECTION (Pin Bar) Detection ──
            # Long wick on one side = institutional injection
            if b / cr < self.INJ_BODY_PCT:
                if lw / cr > self.INJ_WICK_PCT:
                    # Long lower wick = BULLISH injection (demand zone)
                    # "A RED candle with long lower wick is actually highly bullish"
                    trigger_type = 'injection'
                    zone_type = 'demand'
                elif uw / cr > self.INJ_WICK_PCT:
                    # Long upper wick = BEARISH injection (supply zone)
                    trigger_type = 'injection'
                    zone_type = 'supply'

            # ── 2. BAT (Expansion Candle) Detection ──
            # Large body + volume spike = aggressive momentum dump
            if trigger_type is None and b / cr > self.BAT_BODY_PCT and cr > current_atr * self.BAT_ATR_MULT:
                close_p = df['Close'].iloc[i]
                open_p = df['Open'].iloc[i]
                if close_p > open_p:
                    # Bullish bat = demand zone (institutional buying)
                    trigger_type = 'bat'
                    zone_type = 'demand'
                else:
                    # Bearish bat = supply zone (institutional selling)
                    trigger_type = 'bat'
                    zone_type = 'supply'

            # ── 3. BELAN (Doji/Mother Candle) Detection ──
            # Small body + big range + high volume = whale fight
            if trigger_type is None and b / cr < self.BEL_BODY_PCT and cr > current_atr * self.BEL_RANGE_ATR:
                trigger_type = 'belan'
                # Direction determined by next candle's break
                if i + 1 < n:
                    next_close = df['Close'].iloc[i + 1]
                    mid_price = (df['High'].iloc[i] + df['Low'].iloc[i]) / 2
                    if next_close > mid_price:
                        zone_type = 'demand'  # Mother broke up -> demand
                    else:
                        zone_type = 'supply'  # Mother broke down -> supply

            if trigger_type is None or zone_type is None:
                continue

            # ── Create Zone ──
            # For Injection: use full candle (wick shows institutional level)
            # For Bat: use BODY area (tighter zone, more precise)
            # For Belan: use full candle (entire fight zone matters)
            if trigger_type == 'bat':
                # Use the body portion + small buffer for tighter zones
                body_low = min(df['Open'].iloc[i], df['Close'].iloc[i])
                body_high = max(df['Open'].iloc[i], df['Close'].iloc[i])
                buffer = (body_high - body_low) * 0.15
                zone_low = body_low - buffer
                zone_high = body_high + buffer
            else:
                zone_low = df['Low'].iloc[i]
                zone_high = df['High'].iloc[i]
            zone_mid = (zone_low + zone_high) / 2

            # ── Quality Scoring ──
            quality = self._score_zone(
                vol_ratio, cr, current_atr, trigger_type,
                zones, zone_type, zone_low, zone_high
            )

            # ── Check if unmitigated (price moved away) ──
            unmitigated = True
            if i + 5 < n:
                future = df.iloc[i + 1: i + 6]
                if zone_type == 'demand':
                    # Price should move UP after demand zone
                    if (future['Low'] < zone_low).any():
                        unmitigated = False
                else:
                    # Price should move DOWN after supply zone
                    if (future['High'] > zone_high).any():
                        unmitigated = False
            else:
                unmitigated = True  # At end of data, assume unmitigated

            zone = GhostZone(
                zone_type=zone_type,
                low=zone_low,
                high=zone_high,
                mid=zone_mid,
                created_at=i,
                created_time=df['Date'].iloc[i] if 'Date' in df.columns else i,
                quality=quality,
                trigger_type=trigger_type,
                retests=0,
                broken=False,
                volume_ratio=round(vol_ratio, 1),
                unmitigated=unmitigated,
            )
            zones.append(zone)

        return zones

    def _score_zone(self, vol_ratio, candle_range, atr, trigger_type,
                    existing_zones, zone_type, zone_low, zone_high):
        """Score zone quality (1-5) based on institutional footprint strength."""
        score = 1  # Base score

        # +1 if volume > 5x average (very strong institutional activity)
        if vol_ratio >= self.VOLUME_MULT_STRONG:
            score += 1

        # +1 for injection (pin bar) - most reliable institutional signal
        if trigger_type == 'injection':
            score += 1

        # +1 if range > 2x ATR (explosive institutional move)
        if candle_range > atr * 2.0:
            score += 1

        # +1 if no overlapping zone (fresh territory)
        overlapping = False
        for z in existing_zones:
            if z.zone_type == zone_type and not z.broken:
                if z.low <= zone_high and z.high >= zone_low:
                    overlapping = True
                    break
        if not overlapping:
            score += 1

        return min(score, 5)

    # ── Zone Management ───────────────────────────────────────────────────

    def update_zones(self, zones, df, current_idx):
        """
        Update zone status: check for breaks, retests, expiry.
        Returns active zones only.
        """
        atr_val = self.atr(df, 14).iloc[current_idx] if current_idx < len(df) else 0
        if pd.isna(atr_val) or atr_val <= 0:
            return [z for z in zones if not z.broken]

        active = []
        close = df['Close'].iloc[current_idx]

        for z in zones:
            if z.broken:
                continue

            # Expire old zones
            age = current_idx - z.created_at
            if age > self.ZONE_EXPIRY_BARS:
                continue

            # Check if zone is broken (close decisively beyond zone)
            if z.zone_type == 'demand':
                if close < z.low - atr_val * self.ZONE_BREAK_ATR:
                    continue  # Zone broken, skip
            elif z.zone_type == 'supply':
                if close > z.high + atr_val * self.ZONE_BREAK_ATR:
                    continue  # Zone broken, skip

            active.append(z)

        # Keep most recent zones per direction
        demand = [z for z in active if z.zone_type == 'demand']
        supply = [z for z in active if z.zone_type == 'supply']
        demand.sort(key=lambda z: z.created_at, reverse=True)
        supply.sort(key=lambda z: z.created_at, reverse=True)

        return demand[:self.MAX_ACTIVE_ZONES] + supply[:self.MAX_ACTIVE_ZONES]

    # ── Entry Signal Detection ────────────────────────────────────────────

    def find_entries(self, df, active_zones, bar_idx, atr_series):
        """
        Find entry signals at institutional zones.

        Entry conditions:
        1. Price reaches the zone
        2. Fake breakout / liquidity sweep detected (optional, boosts quality)
        3. Reversal candle at the 50% level of the zone
        4. Enter at 50% of zone
        """
        signals = []
        if bar_idx < self.FAKE_BREAKOUT_LOOKBACK or bar_idx >= len(df):
            return signals

        bar = df.iloc[bar_idx]
        prev_bar = df.iloc[bar_idx - 1]
        close = bar['Close']
        open_p = bar['Open']
        high = bar['High']
        low = bar['Low']
        vol = bar['Volume']

        current_atr = atr_series.iloc[bar_idx]
        if pd.isna(current_atr) or current_atr <= 0:
            return signals

        avg_vol = df['Volume'].iloc[max(0, bar_idx - 20):bar_idx].mean()

        for zone in active_zones:
            if zone.broken:
                continue

            # Only trade unmitigated or high-quality zones
            zone_width = zone.high - zone.low
            if zone_width <= 0:
                continue

            # Skip if zone is too narrow (< 0.1% of price) for meaningful entry
            if zone_width < close * 0.001:
                continue

            # ── DEMAND ZONE: Price enters zone from above ──
            if zone.zone_type == 'demand':
                # Price must touch/enter the zone
                if low <= zone.high and low >= zone.low * 0.995:

                    # ── Fake Breakout Detection (Liquidity Sweep) ──
                    # Check if price recently dipped BELOW zone then recovered
                    recent = df.iloc[max(0, bar_idx - self.FAKE_BREAKOUT_LOOKBACK):bar_idx]
                    fake_bo = (recent['Low'] < zone.low).any()
                    fake_bo_quality = 0
                    if fake_bo:
                        # How deep was the fake breakout?
                        extreme_low = recent['Low'].min()
                        penetration = (zone.low - extreme_low) / zone_width
                        if penetration > self.FAKE_BO_PENETRATION:
                            fake_bo_quality = 2  # Strong fake breakout

                    # ── Reversal Confirmation ──
                    # Bullish candle closing in upper half
                    is_bullish = close > open_p
                    body_pct = abs(close - open_p) / max(high - low, 0.001)

                    # Injection at zone: long lower wick
                    injection_at_zone = (
                        (min(open_p, close) - low) > (high - low) * 0.45 and
                        body_pct < 0.35
                    )
                    # Strong bullish engulfing
                    bullish_reversal = (
                        is_bullish and
                        body_pct > 0.4 and
                        close > zone.mid  # Close above 50% of zone
                    )
                    # Hammer
                    hammer = (
                        is_bullish and
                        (min(open_p, close) - low) > (high - low) * 0.5 and
                        body_pct < 0.35
                    )

                    if injection_at_zone or bullish_reversal or hammer:
                        # Entry at 50% of zone (or current close if already above)
                        entry = max(close, zone.mid)

                        # SL below zone bottom
                        sl = zone.low - current_atr * self.SL_BUFFER_ATR

                        # Ensure SL < entry
                        if sl >= entry:
                            sl = entry * 0.998

                        risk = entry - sl
                        if risk < entry * 0.001:
                            continue

                        # Target: MIN_RR + trailing takes care of bigger moves
                        target = entry + risk * (self.MIN_RR + 0.5)

                        # Find nearest supply zone as ceiling
                        supply_targets = [z.low for z in active_zones
                                         if z.zone_type == 'supply' and z.low > entry]
                        if supply_targets:
                            nearest_supply = min(supply_targets)
                            # Only use supply target if RR >= MIN_RR
                            if (nearest_supply - entry) / risk >= self.MIN_RR:
                                target = nearest_supply

                        if target <= entry:
                            continue

                        rr = (target - entry) / risk
                        if rr < self.MIN_RR:
                            continue

                        sig_quality = zone.quality + fake_bo_quality
                        if vol > avg_vol * 1.5:
                            sig_quality += 1

                        # TRP only if strong fake breakout, otherwise SE
                        signal_type = 'GHST_TRP' if fake_bo_quality >= 2 else 'GHST_SE'

                        signals.append({
                            'type': signal_type,
                            'direction': 1,
                            'zone': zone,
                            'entry': entry,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': round(rr, 2),
                            'quality': min(sig_quality, 7),
                            'trigger': zone.trigger_type,
                            'fake_breakout': fake_bo and fake_bo_quality >= 2,
                            'reason': (f"{signal_type}: {zone.trigger_type.upper()} demand "
                                      f"[{zone.low:.0f}-{zone.high:.0f}] "
                                      f"V={zone.volume_ratio}x Q={zone.quality}"
                                      f"{' +FakeBO' if fake_bo_quality >= 2 else ''}"),
                        })

            # ── SUPPLY ZONE: Price enters zone from below ──
            elif zone.zone_type == 'supply':
                if high >= zone.low and high <= zone.high * 1.005:

                    # Fake Breakout Detection
                    recent = df.iloc[max(0, bar_idx - self.FAKE_BREAKOUT_LOOKBACK):bar_idx]
                    fake_bo = (recent['High'] > zone.high).any()
                    fake_bo_quality = 0
                    if fake_bo:
                        extreme_high = recent['High'].max()
                        penetration = (extreme_high - zone.high) / zone_width
                        if penetration > self.FAKE_BO_PENETRATION:
                            fake_bo_quality = 2

                    # Reversal Confirmation
                    is_bearish = close < open_p
                    body_pct = abs(close - open_p) / max(high - low, 0.001)

                    # Injection at zone: long upper wick
                    injection_at_zone = (
                        (high - max(open_p, close)) > (high - low) * 0.45 and
                        body_pct < 0.35
                    )
                    # Bearish reversal
                    bearish_reversal = (
                        is_bearish and
                        body_pct > 0.4 and
                        close < zone.mid
                    )
                    # Shooting star
                    shooting_star = (
                        is_bearish and
                        (high - max(open_p, close)) > (high - low) * 0.5 and
                        body_pct < 0.35
                    )

                    if injection_at_zone or bearish_reversal or shooting_star:
                        entry = min(close, zone.mid)
                        sl = zone.high + current_atr * self.SL_BUFFER_ATR

                        if sl <= entry:
                            sl = entry * 1.002

                        risk = sl - entry
                        if risk < entry * 0.001:
                            continue

                        target = entry - risk * (self.MIN_RR + 0.5)

                        demand_targets = [z.high for z in active_zones
                                         if z.zone_type == 'demand' and z.high < entry]
                        if demand_targets:
                            nearest_demand = max(demand_targets)
                            if (entry - nearest_demand) / risk >= self.MIN_RR:
                                target = nearest_demand

                        if target >= entry:
                            continue

                        rr = (entry - target) / risk
                        if rr < self.MIN_RR:
                            continue

                        sig_quality = zone.quality + fake_bo_quality
                        if vol > avg_vol * 1.5:
                            sig_quality += 1

                        signal_type = 'GHST_TRP' if fake_bo_quality >= 2 else 'GHST_BE'

                        signals.append({
                            'type': signal_type,
                            'direction': -1,
                            'zone': zone,
                            'entry': entry,
                            'sl': sl,
                            'target': target,
                            'risk': risk,
                            'rr': round(rr, 2),
                            'quality': min(sig_quality, 7),
                            'trigger': zone.trigger_type,
                            'fake_breakout': fake_bo and fake_bo_quality >= 2,
                            'reason': (f"{signal_type}: {zone.trigger_type.upper()} supply "
                                      f"[{zone.low:.0f}-{zone.high:.0f}] "
                                      f"V={zone.volume_ratio}x Q={zone.quality}"
                                      f"{' +FakeBO' if fake_bo_quality >= 2 else ''}"),
                        })

        # Sort by quality (highest first)
        signals.sort(key=lambda s: s['quality'], reverse=True)
        return signals

    # ── Exhaustion-Based Trailing Stop ────────────────────────────────────

    def should_trail(self, position, current_price, current_atr):
        """
        Check if trailing stop should be activated / updated.
        Returns new_sl or None.
        """
        entry = position['entry']
        direction = position['direction']
        current_sl = position['sl']

        if direction == 1:  # LONG
            pnl_distance = current_price - entry
            risk = entry - position['original_sl']
            if risk <= 0:
                return None

            # Activate trailing after 1:1 RR
            if pnl_distance >= risk * self.TRAIL_TRIGGER_RR:
                # Trail SL to entry (breakeven) + cushion
                new_sl = max(
                    entry + risk * 0.2,  # At least breakeven + 20% of risk
                    current_price - current_atr * self.TRAIL_ATR_MULT
                )
                if new_sl > current_sl:
                    return new_sl

        else:  # SHORT
            pnl_distance = entry - current_price
            risk = position['original_sl'] - entry
            if risk <= 0:
                return None

            if pnl_distance >= risk * self.TRAIL_TRIGGER_RR:
                new_sl = min(
                    entry - risk * 0.2,
                    current_price + current_atr * self.TRAIL_ATR_MULT
                )
                if new_sl < current_sl:
                    return new_sl

        return None

    def check_exhaustion(self, position, current_price, symbol=''):
        """
        Check if exhaustion target reached.
        For equity: 300pts Nifty, 1000pts BankNifty
        For crypto: CRYPTO_EXHAUSTION_PCT move from zone
        """
        entry = position['entry']
        direction = position['direction']

        if direction == 1:
            move = current_price - entry
        else:
            move = entry - current_price

        # Equity exhaustion
        if 'NIFTY' in symbol.upper() and 'BANK' not in symbol.upper():
            return move >= self.NIFTY_EXHAUSTION
        elif 'BANKNIFTY' in symbol.upper():
            return move >= self.BANKNIFTY_EXHAUSTION
        elif 'SENSEX' in symbol.upper():
            return move >= self.SENSEX_EXHAUSTION

        # Crypto exhaustion
        pct_move = move / entry * 100
        return pct_move >= self.CRYPTO_EXHAUSTION_PCT


# ── Volume Profile Helper (Simplified) ────────────────────────────────────

def compute_volume_profile(df, start_idx, end_idx, num_bins=50):
    """
    Compute a simplified volume profile for a price range.
    Returns dict with 'poc' (Point of Control), 'value_area_high/low',
    and 'shape' ('P' for short-covering, 'b' for long-liquidation, 'D' for normal).
    """
    if end_idx <= start_idx or end_idx > len(df):
        return None

    subset = df.iloc[start_idx:end_idx]
    price_min = subset['Low'].min()
    price_max = subset['High'].max()
    price_range = price_max - price_min

    if price_range <= 0:
        return None

    bin_size = price_range / num_bins
    bins = {}

    for _, row in subset.iterrows():
        bar_low = row['Low']
        bar_high = row['High']
        bar_vol = row['Volume']

        # Distribute volume across price bins
        low_bin = int((bar_low - price_min) / bin_size)
        high_bin = int((bar_high - price_min) / bin_size)
        high_bin = min(high_bin, num_bins - 1)

        num_touched_bins = high_bin - low_bin + 1
        vol_per_bin = bar_vol / max(num_touched_bins, 1)

        for b in range(low_bin, high_bin + 1):
            bins[b] = bins.get(b, 0) + vol_per_bin

    if not bins:
        return None

    # Point of Control (highest volume price level)
    poc_bin = max(bins, key=bins.get)
    poc_price = price_min + (poc_bin + 0.5) * bin_size

    # Value Area (70% of total volume)
    total_vol = sum(bins.values())
    sorted_bins = sorted(bins.items(), key=lambda x: x[1], reverse=True)

    va_vol = 0
    va_bins = set()
    for b, v in sorted_bins:
        va_bins.add(b)
        va_vol += v
        if va_vol >= total_vol * 0.70:
            break

    va_low = price_min + min(va_bins) * bin_size
    va_high = price_min + (max(va_bins) + 1) * bin_size

    # Detect shape: P (top-heavy = short covering) vs b (bottom-heavy = liquidation)
    mid_bin = num_bins // 2
    upper_vol = sum(v for b, v in bins.items() if b >= mid_bin)
    lower_vol = sum(v for b, v in bins.items() if b < mid_bin)

    if upper_vol > lower_vol * 1.5:
        shape = 'P'  # Short covering - weak base
    elif lower_vol > upper_vol * 1.5:
        shape = 'b'  # Long liquidation - weak top
    else:
        shape = 'D'  # Normal distribution

    # Compression detection: value area < 30% of total range
    va_width = va_high - va_low
    is_compressed = va_width < price_range * 0.30

    return {
        'poc': poc_price,
        'va_low': va_low,
        'va_high': va_high,
        'shape': shape,
        'is_compressed': is_compressed,
        'total_volume': total_vol,
    }
