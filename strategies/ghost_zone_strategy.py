"""
Ghost Trade Zone (GTZ) v7 -- Manish Maheshwari "Ghost Trader" methodology.
Instrument-agnostic: takes spot, ohlc, indicators, config, and historical DataFrame.

Based on the Ghost Trader's Free F&O Masterclass:
    - Grandfather (Candlesticks): Institutional footprint candles
    - Father (Volume): 3x+ volume spike = institutional money injection
    - Mother (OI): Validates zone with OI support

Phase 1: Identify Ghost Zone from institutional candles + volume
Phase 2: Check exhaustion rule
Phase 3: Entry at 50% of zone on retest, SL below zone
"""

import numpy as np
import pandas as pd

from .greeks import bs_greeks
from .utils import RISK_FREE_RATE, get_nearest_strike


# ====================================================================
# GHOST ZONE DETECTION
# ====================================================================
def detect_ghost_zones(df, atr, lookback=40):
    """Detect demand and supply zones from daily OHLCV bars.

    Uses the Ghost Trader's candle classification:
        - Injection (Pin Bar): Small body (<35% of range), long wick (>55%)
        - Bat (Expansion): Large body (>70% of range, >0.8x ATR)
        - Belan (Doji): Tiny body (<15% of range), decent range (>0.5x ATR)

    Zones require volume confirmation (3x avg if volume available,
    or range > 1.2x ATR as proxy when no volume data).

    Args:
        df: Historical daily OHLCV DataFrame with Open, High, Low, Close, Volume.
        atr: Current ATR value.
        lookback: Number of bars to scan (default 40).

    Returns:
        (demand_zones, supply_zones): Each is a list of zone dicts with keys:
            low, high, mid, candle_type, vol_mult, timeframe.
    """
    demand_zones = []
    supply_zones = []

    if df is None or len(df) < 3 or atr <= 0:
        return demand_zones, supply_zones

    lookback_df = df.tail(lookback)
    avg_vol_20 = df['Volume'].tail(20).mean() if 'Volume' in df.columns else 0
    has_volume = avg_vol_20 > 0

    for j in range(1, len(lookback_df) - 2):
        bar = lookback_df.iloc[j]
        next_bar = lookback_df.iloc[j + 1]

        o, h, l, c = bar['Open'], bar['High'], bar['Low'], bar['Close']
        body = abs(c - o)
        full_range = h - l
        if full_range <= 0:
            continue

        body_ratio = body / full_range
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)

        # Father: Volume or Range confirmation
        if has_volume:
            vol = bar['Volume']
            vol_mult = vol / max(avg_vol_20, 1)
            if vol_mult < 2.5:
                continue
        else:
            # No volume: use range > 1.2x ATR as proxy
            vol_mult = full_range / max(atr, 1)
            if vol_mult < 1.2:
                continue

        candle_type = None
        bias = 'bullish' if c > o else 'bearish'

        # Injection (Pin Bar): small body, long wick
        if body_ratio < 0.35:
            if lower_wick > full_range * 0.55:
                candle_type = 'injection'
                bias = 'bullish'
            elif upper_wick > full_range * 0.55:
                candle_type = 'injection'
                bias = 'bearish'

        # Bat (Expansion): large body
        if candle_type is None and body_ratio > 0.70 and body > atr * 0.8:
            candle_type = 'bat'

        # Belan (Doji): tiny body, decent range
        if candle_type is None and body_ratio < 0.15 and full_range > atr * 0.5:
            candle_type = 'belan'

        if candle_type is None:
            continue

        # Impulse check: next bar must break beyond the zone
        impulse_up = next_bar['Close'] > h
        impulse_down = next_bar['Close'] < l

        zone = {
            'low': l,
            'high': h,
            'mid': (l + h) / 2,
            'candle_type': candle_type,
            'vol_mult': vol_mult,
            'timeframe': 'daily',
        }

        if (bias == 'bullish' or candle_type == 'belan') and impulse_up:
            demand_zones.append(zone)
        if (bias == 'bearish' or candle_type == 'belan') and impulse_down:
            supply_zones.append(zone)

    # Keep only the 5 most recent zones
    demand_zones = demand_zones[-5:]
    supply_zones = supply_zones[-5:]

    return demand_zones, supply_zones


def enrich_indicators_with_zones(indicators, demand_zones, supply_zones, atr):
    """Add Ghost Zone data to an indicators dict.

    Modifies indicators in-place, adding demand/supply zone fields.

    Args:
        indicators: Existing indicators dict from compute_all_indicators().
        demand_zones: List of demand zone dicts.
        supply_zones: List of supply zone dicts.
        atr: Current ATR value.
    """
    if demand_zones:
        indicators['demand_zone'] = demand_zones[-1]['low']
        indicators['demand_zone_high'] = demand_zones[-1]['high']
    else:
        low_10 = indicators.get('prev_low', 0)
        indicators['demand_zone'] = low_10
        indicators['demand_zone_high'] = low_10 + atr

    if supply_zones:
        indicators['supply_zone'] = supply_zones[-1]['high']
        indicators['supply_zone_low'] = supply_zones[-1]['low']
    else:
        high_10 = indicators.get('prev_high', 0)
        indicators['supply_zone'] = high_10
        indicators['supply_zone_low'] = high_10 - atr

    indicators['demand_strength'] = len(demand_zones)
    indicators['supply_strength'] = len(supply_zones)
    indicators['demand_zones'] = demand_zones
    indicators['supply_zones'] = supply_zones


# ====================================================================
# GHOST ZONE SIGNAL GENERATION
# ====================================================================
def check_ghost_zone_signals(spot, ohlc, indicators, config):
    """Ghost Zone signals -- buy at demand, sell at supply.

    Generates BUY CE when price retests a demand zone and bounces,
    BUY PE when price retests a supply zone and rejects.

    Args:
        spot: Current spot price.
        ohlc: Today's OHLC dict: {'open', 'high', 'low', 'close'}.
        indicators: dict with demand_zones, supply_zones, demand_strength,
            supply_strength, atr, iv, plus standard indicator keys.
        config: Strategy config dict:
            'strike_interval': int (e.g. 50),
            'min_premium_buy': float (e.g. 15),
            'dte': int (days to expiry),
            'r': float (risk-free rate, default RISK_FREE_RATE),
            'chain_ltp_ce': float (real LTP from chain, 0 if unavailable),
            'chain_iv_ce': float (real IV from chain, 0 if unavailable),
            'chain_ltp_pe': float (real LTP from chain, 0 if unavailable),
            'chain_iv_pe': float (real IV from chain, 0 if unavailable),
            'chain_strike_ce': int (chain-selected CE strike, 0 if unavailable),
            'chain_strike_pe': int (chain-selected PE strike, 0 if unavailable),
            'oi_demand_strike': float (OI-confirmed demand strike, 0 if unavailable),
            'oi_supply_strike': float (OI-confirmed supply strike, 0 if unavailable),
            'max_put_oi': float (max PUT OI near demand, 0 if unavailable),
            'max_call_oi': float (max CALL OI near supply, 0 if unavailable),
            'oi_available': bool (whether OI data was fetched, default False),
            'target_mult': float (target multiplier, default 1.5),
            'sl_mult': float (SL multiplier, default 0.35).

    Returns:
        list of signal dicts.
    """
    signals = []
    ind = indicators
    dte = config.get('dte', 7)
    T = dte / 365
    r = config.get('r', RISK_FREE_RATE)
    strike_interval = config.get('strike_interval', 50)
    min_premium_buy = config.get('min_premium_buy', 15)
    atr = ind.get('atr', 0)
    target_mult = config.get('target_mult', 1.5)
    sl_mult = config.get('sl_mult', 0.35)

    if atr <= 0:
        return signals

    # OI data (The Mother)
    oi_demand_strike = config.get('oi_demand_strike', 0)
    oi_supply_strike = config.get('oi_supply_strike', 0)
    max_put_oi = config.get('max_put_oi', 0)
    max_call_oi = config.get('max_call_oi', 0)
    oi_available = config.get('oi_available', False)

    demand_zones = ind.get('demand_zones', [])
    supply_zones = ind.get('supply_zones', [])

    # ---- DEMAND ZONE RETEST: BUY CE ----
    for dz in demand_zones:
        zone_low = dz['low']
        zone_high = dz['high']
        zone_mid = (zone_low + zone_high) / 2

        # Price dips INTO the zone and bounces (close above zone)
        if (ohlc['low'] <= zone_high
                and ohlc['low'] >= zone_low * 0.998
                and spot > zone_high):

            # OI validation (The Mother)
            oi_label = ""
            oi_score = 0
            if (oi_demand_strike > 0
                    and abs(oi_demand_strike - zone_low) <= strike_interval):
                oi_score = max_put_oi
                oi_label = f" [PUT OI={max_put_oi:,.0f}@{oi_demand_strike}]"

            # Zone strength checks
            if ind.get('demand_strength', 0) >= 2 or oi_score > 10000:
                pass  # Strong zone -- take it
            elif oi_score == 0 and oi_available:
                continue  # OI data available but no OI backing -- skip
            elif oi_score == 0 and not oi_available and ind.get('demand_strength', 0) < 1:
                continue  # No OI data AND no price-action strength -- skip

            # Strike selection
            chain_ltp = config.get('chain_ltp_ce', 0)
            chain_iv = config.get('chain_iv_ce', 0)
            chain_strike = config.get('chain_strike_ce', 0)

            ce_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
            use_iv = chain_iv if chain_iv > 0 else ind['iv']
            g = bs_greeks(spot, ce_strike, T, r, use_iv, 'CE')
            premium = chain_ltp if chain_ltp > 0 else g['price']

            if premium > min_premium_buy:
                signals.append({
                    'type': 'BUY_CE_GTZ',
                    'strike': ce_strike,
                    'premium': premium,
                    'greeks': g,
                    'reason': (f"GTZ v7 Demand: zone={zone_low:.0f}-{zone_high:.0f} "
                               f"entry@50%={zone_mid:.0f} spot={spot:.0f}"
                               f"{oi_label}"
                               f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                    'target': premium * target_mult,
                    'sl': premium * sl_mult,
                })
                break  # Only one demand signal per scan

    # ---- SUPPLY ZONE RETEST: BUY PE ----
    for sz in supply_zones:
        zone_low = sz['low']
        zone_high = sz['high']
        zone_mid = (zone_low + zone_high) / 2

        # Price spikes INTO the zone and rejects (close below zone)
        if (ohlc['high'] >= zone_low
                and ohlc['high'] <= zone_high * 1.002
                and spot < zone_low):

            oi_label = ""
            oi_score = 0
            if (oi_supply_strike > 0
                    and abs(oi_supply_strike - zone_high) <= strike_interval):
                oi_score = max_call_oi
                oi_label = f" [CALL OI={max_call_oi:,.0f}@{oi_supply_strike}]"

            if ind.get('supply_strength', 0) >= 2 or oi_score > 10000:
                pass
            elif oi_score == 0 and oi_available:
                continue
            elif oi_score == 0 and not oi_available and ind.get('supply_strength', 0) < 1:
                continue

            chain_ltp = config.get('chain_ltp_pe', 0)
            chain_iv = config.get('chain_iv_pe', 0)
            chain_strike = config.get('chain_strike_pe', 0)

            pe_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
            use_iv = chain_iv if chain_iv > 0 else ind['iv']
            g = bs_greeks(spot, pe_strike, T, r, use_iv, 'PE')
            premium = chain_ltp if chain_ltp > 0 else g['price']

            if premium > min_premium_buy:
                signals.append({
                    'type': 'BUY_PE_GTZ',
                    'strike': pe_strike,
                    'premium': premium,
                    'greeks': g,
                    'reason': (f"GTZ v7 Supply: zone={zone_low:.0f}-{zone_high:.0f} "
                               f"entry@50%={zone_mid:.0f} spot={spot:.0f}"
                               f"{oi_label}"
                               f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                    'target': premium * target_mult,
                    'sl': premium * sl_mult,
                })
                break  # Only one supply signal per scan

    return signals
