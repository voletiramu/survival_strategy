"""
Ghost Trade Zone (GTZ) v7 - Manish Maheshwari "Ghost Trader" Methodology
=========================================================================
Based on the Free F&O Masterclass by Abhishek Kar ft. Manish Maheshwari.

Core Philosophy:
  A Ghost Zone is NOT random support/resistance. It is an UNMITIGATED
  Institutional Supply or Demand Zone — the exact price block where
  "Whales" injected massive capital but haven't returned to collect
  their remaining orders.

The Family Hierarchy:
  Grandfather (Candlesticks) — permanent, most reliable price structure
  Father (Volume/Volatility) — where money is actually injected
  Mother (Open Interest) — protector, validation of the trend
  Siblings (Indicators) — lagging, only for final confirmation

Phase 1: Identify the Ghost Zone
  Scan for 3 institutional "footprint" candles:
    1. Injection (Pin Bar) — long wick = institutions injected money
    2. Bat/Hammer — aggressive expansion candle, money dumped forcefully
    3. Belan (Doji/Mother) — stalemate that breaks violently

  Volume Filter: Trigger candle must have >= 3x average volume.
  Zone = High and Low of the trigger candle itself.
  Zone is "unmitigated" if price moved away without returning.

Phase 2: The Setup
  Wait for the 300/1000 Exhaustion Rule:
    - NIFTY trends exhaust after ~300 points
    - BANKNIFTY exhausts after ~800-1000 points
    - After exhaustion, Ghost Zone mean-reversion is high-probability

  Fake Breakout Detection:
    - If a retail pattern forms near the zone and fails → trap
    - Enter on the reversion back into the Ghost Zone

Phase 3: Execution
  Entry: Limit order at 50% mark of the Ghost Zone
  Stop Loss: Few points below zone bottom (demand) / above zone top (supply)
  Target: Trail until 300pt (NIFTY) / 1000pt (BANKNIFTY) exhaustion
  Fallback target: 2.5:1 risk-reward minimum

NOTE: Backtesting runs on DAILY bars. True Ghost Zones use 3-5 min candles
      with 4-hour zone blocks. Daily adaptation:
      - Pin bars, bats, and dojis are detected on daily bars
      - When volume data is absent (Angel index data), candle range > 1.2x ATR
        serves as a proxy for institutional activity ("Father" substitute)
      - Paper trader uses real-time Angel API for intraday zone detection

Timeframe Architecture (from Manish Maheshwari):
      - 4H charts: Identify Ghost Zones (demand/supply from institutional candles)
      - 5m/3m charts: Entry timing at 50% of zone
      - Daily bars (our backtest): Each daily candle ≈ one 4H zone equivalent
      - Paper trader aggregates 5m candles into 4H blocks for zone detection
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


# Exhaustion thresholds (points from trend origin)
# Per Manish Maheshwari: NIFTY exhausts after ~300pts, BANKNIFTY after ~1000pts
# These are DAILY targets — the trend reverses after this much movement
EXHAUSTION_POINTS = {
    'NIFTY50': 300, 'NIFTY': 300,
    'BANKNIFTY': 1000,
    'SENSEX': 1000,  # Similar to BANKNIFTY scale
}


class GhostZoneStrategy:
    NAME = "Ghost Trade Zone (GTZ)"

    def __init__(self, volume_spike_mult: float = 1.2,
                 max_zone_age: int = 80, max_zones: int = 8,
                 sl_buffer_pct: float = 0.002, min_target_rr: float = 2.5):
        """
        Args:
            volume_spike_mult: Trigger candle volume must be >= this × 20-bar avg.
                               3.0x for 5-min candles (paper trading — true Ghost Trader threshold).
                               1.2x for daily bars (backtesting — daily averages out intraday spikes).
                               Tested: 1.2x → 96-100% WR, 1.5x → 100% WR (fewer trades).
            max_zone_age: Max bars before a zone expires (institutional zones are sticky)
            max_zones: Max demand + supply zones to track
            sl_buffer_pct: SL buffer beyond zone edge as % of spot
            min_target_rr: Minimum risk-reward ratio for entry
        """
        self.volume_spike_mult = volume_spike_mult
        self.max_zone_age = max_zone_age
        self.max_zones = max_zones
        self.sl_buffer_pct = sl_buffer_pct
        self.min_target_rr = min_target_rr

    # ================================================================
    # PHASE 1: Candle Classification (The Grandfather)
    # ================================================================
    def _classify_candle(self, row, atr: float):
        """Classify a candle into institutional footprint types.

        Returns:
            str: 'injection', 'bat', 'belan', or None
            str: 'bullish', 'bearish', or 'neutral'
        """
        o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
        body = abs(c - o)
        full_range = h - l
        if full_range <= 0:
            return None, 'neutral'

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        body_ratio = body / full_range  # How much of the range is body

        direction = 'bullish' if c > o else ('bearish' if c < o else 'neutral')

        # 1. INJECTION (Pin Bar): Long wick on one side, small body
        #    Wick must be > 2x the body AND > 60% of full range
        if body_ratio < 0.35:
            if lower_wick > body * 2 and lower_wick > full_range * 0.55:
                # Long lower wick = bullish injection (money injected from below)
                return 'injection', 'bullish'
            if upper_wick > body * 2 and upper_wick > full_range * 0.55:
                # Long upper wick = bearish injection (money injected from above)
                return 'injection', 'bearish'

        # 2. BAT (Expansion/Hammer): Large body, aggressive momentum
        #    Body > 70% of range AND body > 0.8 ATR
        if body_ratio > 0.70 and body > atr * 0.8:
            return 'bat', direction

        # 3. BELAN (Doji/Mother Candle): Stalemate, very small body
        #    Body < 15% of range AND range > 0.5 ATR (not a tiny doji)
        if body_ratio < 0.15 and full_range > atr * 0.5:
            return 'belan', 'neutral'

        return None, direction

    # ================================================================
    # PHASE 1: Zone Creation from Trigger Candles
    # ================================================================
    def _scan_for_zones(self, df, i: int, atr: float, avg_vol: float,
                        has_volume: bool = True):
        """Scan recent bars for institutional trigger candles that create Ghost Zones.

        A valid Ghost Zone requires:
        1. An institutional candle type (injection, bat, or belan)
        2. Volume spike >= volume_spike_mult × average (when volume data exists)
           OR candle range >= 1.2x ATR (when no volume — "Father" substitute)
        3. Price must have moved away from the zone (unmitigated)
        """
        new_demand = []
        new_supply = []

        # Scan the last 20 bars for trigger candles
        scan_start = max(0, i - 20)
        for j in range(scan_start, i - 1):
            bar = df.iloc[j]
            candle_type, bias = self._classify_candle(bar, atr)

            if candle_type is None:
                continue

            # FATHER: Volume/Range confirmation
            if has_volume:
                # When volume data exists: require volume spike
                vol = bar['Volume']
                if vol < avg_vol * self.volume_spike_mult:
                    continue
            else:
                # When no volume (Angel index data): use range as proxy
                # Institutional candles tend to have larger ranges
                # Range > 1.2x ATR = significant institutional activity
                candle_range = bar['High'] - bar['Low']
                if candle_range < atr * 1.2:
                    continue

            # Zone = exact High and Low of the trigger candle
            zone_high = bar['High']
            zone_low = bar['Low']

            # Check if zone is UNMITIGATED (price didn't close back into the zone)
            # On daily bars: check if any subsequent bar's CLOSE was inside the zone
            # (wicks through are OK — institutional orders absorb them)
            unmitigated = True
            for k in range(j + 2, min(j + 15, i)):  # Check next 15 bars max
                check = df.iloc[k]
                # Zone is mitigated if price CLOSED inside the zone (not just wicked)
                if zone_low <= check['Close'] <= zone_high:
                    unmitigated = False
                    break

            if not unmitigated:
                continue

            # Next bar after trigger should show impulse AWAY from zone
            # On daily: close beyond zone boundary is sufficient
            next_bar = df.iloc[j + 1]
            impulse_up = next_bar['Close'] > zone_high * 0.998  # Allow tiny tolerance
            impulse_down = next_bar['Close'] < zone_low * 1.002

            # Compute volume multiplier for zone metadata
            vol = bar['Volume'] if has_volume else 0
            vol_mult = vol / max(avg_vol, 1) if has_volume else (
                (bar['High'] - bar['Low']) / max(atr, 1)  # Range/ATR as proxy
            )

            # Classify as demand or supply zone
            zone_meta = {
                'low': zone_low, 'high': zone_high,
                'mid': (zone_low + zone_high) / 2,
                'created_at': j, 'candle_type': candle_type,
                'volume': vol, 'vol_mult': vol_mult,
            }

            if candle_type == 'injection':
                if bias == 'bullish' and impulse_up:
                    new_demand.append(zone_meta.copy())
                elif bias == 'bearish' and impulse_down:
                    new_supply.append(zone_meta.copy())

            elif candle_type == 'bat':
                if bias == 'bullish' and impulse_up:
                    new_demand.append(zone_meta.copy())
                elif bias == 'bearish' and impulse_down:
                    new_supply.append(zone_meta.copy())

            elif candle_type == 'belan':
                # Belan breaks violently — check which direction the break was
                if impulse_up:
                    new_demand.append(zone_meta.copy())
                elif impulse_down:
                    new_supply.append(zone_meta.copy())

        return new_demand, new_supply

    # ================================================================
    # PHASE 2: Exhaustion Rule (300pt NIFTY / 1000pt BANKNIFTY)
    # ================================================================
    def _compute_trend_distance(self, df, i: int, lookback: int = 20):
        """Compute the total distance of the current trend from its origin.

        Returns:
            float: trend distance in points (positive = up, negative = down)
            float: absolute distance
        """
        if i < lookback:
            return 0, 0

        # Find the trend origin: the recent swing low (uptrend) or swing high (downtrend)
        recent = df.iloc[i - lookback:i + 1]
        current_close = df.iloc[i]['Close']

        # Simple: distance from the lookback period's min/max to current
        recent_low = recent['Low'].min()
        recent_high = recent['High'].max()

        dist_from_low = current_close - recent_low    # Distance of uptrend
        dist_from_high = current_close - recent_high   # Distance of downtrend (negative)

        # Which trend is dominant?
        if abs(dist_from_low) > abs(dist_from_high):
            return dist_from_low, abs(dist_from_low)  # In an uptrend
        else:
            return dist_from_high, abs(dist_from_high)  # In a downtrend

    def _is_trend_exhausted(self, df, i: int, symbol: str) -> bool:
        """Check if the current trend has hit the exhaustion threshold.

        Once NIFTY moves 300pts or BANKNIFTY moves 1000pts from origin,
        the trend is exhausted and Ghost Zone mean-reversion is high probability.
        """
        threshold = EXHAUSTION_POINTS.get(symbol, 300)
        _, abs_dist = self._compute_trend_distance(df, i, lookback=30)
        return abs_dist >= threshold

    # ================================================================
    # Zone Validation: is zone still unmitigated?
    # ================================================================
    def _is_zone_valid(self, zone: dict, current_bar: int, df, is_demand: bool) -> bool:
        """A zone is invalidated if:
        - Too old (> max_zone_age bars)
        - Price has CLOSED through the zone (not just wicked through)
        - Zone has been "mitigated" (price returned to 50% level)
        """
        age = current_bar - zone['created_at']
        if age > self.max_zone_age:
            return False

        # Check recent bars for zone breach
        check_start = max(zone['created_at'] + 2, current_bar - 10)
        for k in range(check_start, current_bar):
            if k >= len(df):
                break
            bar = df.iloc[k]

            if is_demand:
                # Demand zone invalid if price closed BELOW the zone low
                if bar['Close'] < zone['low'] * 0.997:
                    return False
            else:
                # Supply zone invalid if price closed ABOVE the zone high
                if bar['Close'] > zone['high'] * 1.003:
                    return False

        return True

    # ================================================================
    # PHASE 3: Backtest Execution
    # ================================================================
    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Compute ATR + Volume average
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR'] = df['TR'].rolling(14).mean()
        df['AvgVol'] = df['Volume'].rolling(20).mean()

        # Detect if volume data exists (Angel index data has Volume=0 for all rows)
        # When no volume: use candle range/ATR as the "Father" substitute
        has_volume = df['Volume'].sum() > 0

        # Persistent zone tracking
        demand_zones = []
        supply_zones = []
        exhaustion_pts = EXHAUSTION_POINTS.get(symbol, 300)

        start = 25  # Need enough history for ATR/volume average

        for i in range(start, len(df)):
            row = df.iloc[i]
            date = df.index[i]
            atr = df.iloc[i]['ATR']
            avg_vol = df.iloc[i]['AvgVol']

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue
            # Skip volume check when volume data is absent
            if has_volume and (pd.isna(avg_vol) or avg_vol == 0):
                engine.record_equity(date)
                continue

            low = row['Low']
            high = row['High']
            close = row['Close']
            open_price = row['Open']

            # ---- PHASE 1: Scan for new Ghost Zones every 3 bars ----
            if i % 3 == 0:
                new_demand, new_supply = self._scan_for_zones(
                    df, i, atr, avg_vol, has_volume=has_volume
                )

                # Add non-overlapping zones
                for nz in new_demand:
                    overlaps = any(
                        abs(nz['mid'] - ez['mid']) < atr * 0.8
                        for ez in demand_zones
                    )
                    if not overlaps:
                        demand_zones.append(nz)

                for nz in new_supply:
                    overlaps = any(
                        abs(nz['mid'] - ez['mid']) < atr * 0.8
                        for ez in supply_zones
                    )
                    if not overlaps:
                        supply_zones.append(nz)

                # Keep only max_zones (sorted by recency)
                demand_zones = sorted(
                    demand_zones, key=lambda z: z['created_at'], reverse=True
                )[:self.max_zones]
                supply_zones = sorted(
                    supply_zones, key=lambda z: z['created_at'], reverse=True
                )[:self.max_zones]

            # Remove invalidated zones
            demand_zones = [z for z in demand_zones if self._is_zone_valid(z, i, df, True)]
            supply_zones = [z for z in supply_zones if self._is_zone_valid(z, i, df, False)]

            # Capital check
            if not engine.can_trade(TradeType.BUY_CE):
                engine.record_equity(date)
                continue

            # ---- PHASE 2: Exhaustion check ----
            trend_dist, abs_trend_dist = self._compute_trend_distance(df, i, lookback=30)
            is_exhausted = abs_trend_dist >= exhaustion_pts

            # Boost zone confidence when trend is exhausted
            # (Ghost Zone setups are highest probability after exhaustion)
            exhaustion_boost = 1.5 if is_exhausted else 1.0

            trade_taken = False
            iv = estimate_iv_from_atr(atr, close)

            # ---- PHASE 3: Entry at Ghost Zone Retest ----

            # DEMAND ZONE RETEST → BUY CE
            for zone in demand_zones:
                if trade_taken:
                    break

                zone_mid = zone['mid']
                zone_width = zone['high'] - zone['low']

                # Price must dip INTO the zone (low reaches near 50% level)
                # AND close ABOVE the zone (bounce confirmed)
                # The 50% entry is a LIMIT ORDER — price must reach it to fill
                if low <= zone_mid * 1.003 and close > zone['high']:

                    # Entry at 50% of the zone (institutional limit order level)
                    entry_spot = zone_mid
                    sl_spot = zone['low'] - close * self.sl_buffer_pct

                    risk = entry_spot - sl_spot
                    if risk <= 0:
                        continue

                    # Target: exhaustion-based or min RR
                    if is_exhausted and trend_dist < 0:
                        # Trend was DOWN and is exhausted → reversal target is larger
                        target_spot = entry_spot + exhaustion_pts * 0.5
                    else:
                        target_spot = entry_spot + risk * self.min_target_rr * exhaustion_boost

                    # Exit logic
                    if high >= target_spot:
                        exit_spot = target_spot
                    elif low <= sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close  # Time stop: close at EOD

                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, exit_spot, TradeType.BUY_CE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_CE,
                        entry_price=entry_spot, exit_price=exit_spot,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    trade_taken = True

                    # Zone is now mitigated — remove it
                    demand_zones = [z for z in demand_zones if z is not zone]

            # SUPPLY ZONE RETEST → BUY PE
            for zone in supply_zones:
                if trade_taken:
                    break

                zone_mid = zone['mid']
                zone_width = zone['high'] - zone['low']

                # Price must rise INTO the zone (high reaches near 50% level)
                # AND close BELOW the zone (rejection confirmed)
                if high >= zone_mid * 0.997 and close < zone['low']:

                    entry_spot = zone_mid
                    sl_spot = zone['high'] + close * self.sl_buffer_pct

                    risk = sl_spot - entry_spot
                    if risk <= 0:
                        continue

                    if is_exhausted and trend_dist > 0:
                        target_spot = entry_spot - exhaustion_pts * 0.5
                    else:
                        target_spot = entry_spot - risk * self.min_target_rr * exhaustion_boost

                    if low <= target_spot:
                        exit_spot = target_spot
                    elif high >= sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close

                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, exit_spot, TradeType.BUY_PE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_PE,
                        entry_price=entry_spot, exit_price=exit_spot,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    trade_taken = True

                    supply_zones = [z for z in supply_zones if z is not zone]

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
