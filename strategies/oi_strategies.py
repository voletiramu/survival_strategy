"""
OI-Based Options BUYING Strategies.
Three strategies that exploit per-strike open interest patterns
for high-probability directional option buying.

Strategies:
1. OI Wall Bouncer — BUY CE/PE at OI wall support/resistance
2. Max Pain Magnet — BUY toward max pain convergence
3. VWAP Bounce — BUY on VWAP mean-reversion with OI confirmation

All strategies are BUY-only (no selling). Designed for options buying
with defined risk (premium paid = max loss).
"""

import numpy as np
from strategies.greeks import bs_greeks

# ====================================================================
# STRATEGY TYPE CONSTANTS
# ====================================================================
OI_WALL_BOUNCER = 'OI Wall Bouncer'
MAX_PAIN_MAGNET = 'Max Pain Magnet'
VWAP_BOUNCE = 'VWAP Bounce'

ALL_OI_STRATEGIES = [OI_WALL_BOUNCER, MAX_PAIN_MAGNET, VWAP_BOUNCE]

# ====================================================================
# RISK-FREE RATE
# ====================================================================
RISK_FREE_RATE = 0.065  # India 10Y ~6.5%

# ====================================================================
# OI WALL BOUNCER PARAMETERS
# ====================================================================
OIW_WALL_THRESHOLD = 5_000_000    # Min OI to qualify as "wall" (5M contracts)
OIW_PROXIMITY_PCT = 0.005         # Spot within 0.5% of wall strike to trigger
OIW_TARGET_PCT = 0.45             # 45% premium gain target
OIW_SL_PCT = 0.30                 # 30% premium loss stop
OIW_MIN_PREMIUM = 15              # Minimum Rs 15 premium to buy
OIW_VIX_MAX = 22                  # Skip when VIX > 22 (too volatile)

# ====================================================================
# MAX PAIN MAGNET PARAMETERS
# ====================================================================
MP_MIN_DISTANCE_PCT = 0.003       # Spot must be > 0.3% from max pain
MP_MAX_DISTANCE_PCT = 0.015       # Spot must be < 1.5% from max pain
MP_TARGET_PCT = 0.40              # 40% premium gain target
MP_SL_PCT = 0.35                  # 35% premium loss stop
MP_MIN_PREMIUM = 15               # Minimum Rs 15 premium
MP_EXPIRY_BOOST = 1.25            # 25% higher target on expiry day

# ====================================================================
# VWAP BOUNCE PARAMETERS
# ====================================================================
VB_VWAP_PROXIMITY = 0.003         # Spot within 0.3% of VWAP to trigger
VB_BOUNCE_CONFIRM = 0.002         # Price must bounce 0.2% away from VWAP
VB_OI_CONFIRM_THRESHOLD = 1_000_000  # 1M OI at VWAP strike for confirmation
VB_TARGET_PCT = 0.40              # 40% premium gain target
VB_SL_PCT = 0.25                  # 25% premium loss stop (tight — VWAP break = invalid)
VB_MIN_PREMIUM = 15               # Minimum Rs 15 premium

# Strike intervals for Indian indices
STRIKE_INTERVALS = {'NIFTY': 50, 'NIFTY50': 50, 'BANKNIFTY': 100, 'SENSEX': 100}
LOT_SIZES = {'NIFTY': 75, 'NIFTY50': 75, 'BANKNIFTY': 30, 'SENSEX': 20}


# ====================================================================
# OI UTILITY FUNCTIONS
# ====================================================================
def get_nearest_strike(spot, strike_interval):
    """Round spot to nearest strike."""
    return round(spot / strike_interval) * strike_interval


def find_oi_walls(chain_data, spot, top_n=3):
    """Find highest-OI strikes from option chain data (LIVE TRADING).

    Args:
        chain_data: List of dicts with {strikePrice, optionType, openInterest, ltp, ...}
        spot: Current underlying spot price.
        top_n: Number of top OI walls to return per side.

    Returns:
        dict with 'call_walls' and 'put_walls', each a list of
        {strike, oi, ltp, distance_pct} sorted by OI descending.
    """
    if not chain_data:
        return {'call_walls': [], 'put_walls': []}

    ce_strikes = {}
    pe_strikes = {}

    for item in chain_data:
        strike = item.get('strikePrice', item.get('strike', 0))
        oi = item.get('openInterest', item.get('opnInterest', item.get('oi', 0)))
        opt_type = item.get('optionType', item.get('type', ''))
        ltp = item.get('ltp', item.get('lastPrice', 0))

        if not strike or not oi:
            continue

        distance_pct = abs(strike - spot) / spot

        if opt_type == 'CE':
            ce_strikes[strike] = {'strike': strike, 'oi': oi, 'ltp': ltp,
                                  'distance_pct': distance_pct, 'opt_type': 'CE'}
        elif opt_type == 'PE':
            pe_strikes[strike] = {'strike': strike, 'oi': oi, 'ltp': ltp,
                                  'distance_pct': distance_pct, 'opt_type': 'PE'}

    # Sort by OI descending, take top N
    call_walls = sorted(ce_strikes.values(), key=lambda x: x['oi'], reverse=True)[:top_n]
    put_walls = sorted(pe_strikes.values(), key=lambda x: x['oi'], reverse=True)[:top_n]

    return {'call_walls': call_walls, 'put_walls': put_walls}


def approximate_oi_walls_backtest(spot, strike_interval, symbol='NIFTY'):
    """Approximate OI walls for backtesting (no real OI data).

    Round-number strikes attract the most OI in Indian markets.
    We place synthetic walls at:
    - NIFTY: Every 500 points (e.g., 24000, 24500, 25000)
    - BANKNIFTY/SENSEX: Every 1000 points

    Args:
        spot: Current spot price.
        strike_interval: Strike interval for the symbol.
        symbol: Trading symbol.

    Returns:
        dict with 'call_walls' and 'put_walls'.
    """
    # Round number interval for OI concentration
    if symbol in ('BANKNIFTY', 'SENSEX'):
        round_interval = 1000
    else:
        round_interval = 500

    # Find nearest round-number strikes above and below
    base = round(spot / round_interval) * round_interval
    call_strikes = [base + round_interval, base + 2 * round_interval]
    put_strikes = [base - round_interval, base - 2 * round_interval]

    # Also add the base level itself
    if base > spot:
        call_strikes.insert(0, base)
    else:
        put_strikes.insert(0, base)

    call_walls = []
    for s in call_strikes:
        call_walls.append({
            'strike': s,
            'oi': 10_000_000,  # Synthetic high OI
            'ltp': 0,
            'distance_pct': abs(s - spot) / spot,
            'opt_type': 'CE',
        })

    put_walls = []
    for s in put_strikes:
        put_walls.append({
            'strike': s,
            'oi': 10_000_000,
            'ltp': 0,
            'distance_pct': abs(s - spot) / spot,
            'opt_type': 'PE',
        })

    return {'call_walls': call_walls[:3], 'put_walls': put_walls[:3]}


def calculate_max_pain(chain_data, spot, strike_interval):
    """Calculate max pain strike from option chain OI (LIVE TRADING).

    Max pain = strike where total OI pain is minimized.
    For each candidate strike K:
        pain(K) = sum of CE intrinsic × CE_OI for all strikes
                + sum of PE intrinsic × PE_OI for all strikes

    Args:
        chain_data: List of dicts with strike, OI, type data.
        spot: Current spot price.
        strike_interval: Strike interval.

    Returns:
        Max pain strike price (float), or ATM strike if no data.
    """
    if not chain_data:
        return get_nearest_strike(spot, strike_interval)

    # Collect OI per strike per type
    ce_oi = {}
    pe_oi = {}
    all_strikes = set()

    for item in chain_data:
        strike = item.get('strikePrice', item.get('strike', 0))
        oi = item.get('openInterest', item.get('opnInterest', item.get('oi', 0)))
        opt_type = item.get('optionType', item.get('type', ''))

        if not strike or not oi:
            continue

        all_strikes.add(strike)
        if opt_type == 'CE':
            ce_oi[strike] = oi
        elif opt_type == 'PE':
            pe_oi[strike] = oi

    if not all_strikes:
        return get_nearest_strike(spot, strike_interval)

    # Calculate pain at each strike
    min_pain = float('inf')
    max_pain_strike = get_nearest_strike(spot, strike_interval)

    for K in sorted(all_strikes):
        pain = 0
        # CE holders' pain: max(0, K_chain - K) × OI
        for s, oi in ce_oi.items():
            ce_intrinsic = max(0, s - K) * oi  # If closing at K, how much CE holders at strike s lose
            # Actually: CE pain at expiry price K = max(0, K - strike) for call buyers
            # Correction: CE buyer at strike s profits max(0, K-s), so writers pay max(0, K-s)*oi
            # We want minimum pain FOR WRITERS (max pain for buyers)
            pain += max(0, K - s) * oi

        # PE holders' pain: max(0, K - K_chain) × OI
        for s, oi in pe_oi.items():
            pain += max(0, s - K) * oi

        if pain < min_pain:
            min_pain = pain
            max_pain_strike = K

    return max_pain_strike


def approximate_max_pain_backtest(spot, strike_interval):
    """Approximate max pain for backtesting (no real OI).

    Max pain historically clusters near ATM, biased toward round numbers.

    Args:
        spot: Current spot price.
        strike_interval: Strike interval.

    Returns:
        Approximate max pain strike.
    """
    # Max pain tends to be near ATM, slightly biased to nearest round number
    atm = get_nearest_strike(spot, strike_interval)

    # Check if there's a round number within 1% of ATM
    if strike_interval == 50:
        round_num = round(spot / 500) * 500
    else:
        round_num = round(spot / 1000) * 1000

    # If round number is within 1% of spot, use it; otherwise use ATM
    if abs(round_num - spot) / spot < 0.01:
        return round_num
    return atm


def get_oi_at_strike(chain_data, strike, opt_type):
    """Lookup OI for a specific strike and option type from chain data.

    Args:
        chain_data: Option chain data list.
        strike: Strike price to look up.
        opt_type: 'CE' or 'PE'.

    Returns:
        OI value (int), or 0 if not found.
    """
    if not chain_data:
        return 0

    for item in chain_data:
        s = item.get('strikePrice', item.get('strike', 0))
        t = item.get('optionType', item.get('type', ''))
        if s == strike and t == opt_type:
            return item.get('openInterest', item.get('opnInterest',
                            item.get('oi', 0)))
    return 0


# ====================================================================
# SCORING FUNCTION
# ====================================================================
def compute_oi_score(strategy_type, vix, cpr_width, pcr, iv, dte,
                     distance_pct=0, chain_quality=True):
    """Score an OI-based signal (0-100).

    Args:
        strategy_type: One of OI_WALL_BOUNCER, MAX_PAIN_MAGNET, VWAP_BOUNCE.
        vix: India VIX value.
        cpr_width: CPR width as percentage.
        pcr: Put-Call Ratio.
        iv: Implied volatility (decimal).
        dte: Days to expiry.
        distance_pct: Distance of spot from trigger level (wall/MP/VWAP).
        chain_quality: True if using real chain data, False if approximated.

    Returns:
        Score 0-100.
    """
    score = 0

    # === 1. VIX regime (0-20) ===
    if strategy_type == OI_WALL_BOUNCER:
        # OI walls hold better in moderate VIX
        if 12 <= vix <= 18:
            score += 20
        elif vix < 12 or vix <= 22:
            score += 14
        else:
            score += 6

    elif strategy_type == MAX_PAIN_MAGNET:
        # Max pain convergence works best in low-moderate VIX
        if 10 <= vix <= 16:
            score += 20
        elif vix <= 20:
            score += 14
        else:
            score += 6

    elif strategy_type == VWAP_BOUNCE:
        # VWAP bounces work across VIX ranges
        if 10 <= vix <= 20:
            score += 18
        elif vix <= 25:
            score += 12
        else:
            score += 6

    # === 2. Distance from trigger level (0-25) ===
    if strategy_type == OI_WALL_BOUNCER:
        # Closer to wall = stronger signal
        if distance_pct < 0.002:
            score += 25  # Very close (< 0.2%)
        elif distance_pct < 0.004:
            score += 20
        elif distance_pct < 0.006:
            score += 14
        else:
            score += 8

    elif strategy_type == MAX_PAIN_MAGNET:
        # Sweet spot: not too close, not too far from max pain
        if 0.003 <= distance_pct <= 0.008:
            score += 25  # Ideal convergence zone
        elif 0.002 <= distance_pct <= 0.012:
            score += 18
        else:
            score += 10

    elif strategy_type == VWAP_BOUNCE:
        # Closer to VWAP = better entry
        if distance_pct < 0.002:
            score += 25
        elif distance_pct < 0.004:
            score += 18
        else:
            score += 10

    # === 3. PCR alignment (0-15) ===
    if strategy_type in (OI_WALL_BOUNCER, VWAP_BOUNCE):
        # Neutral PCR = rangebound = better for bounces
        if 0.85 <= pcr <= 1.15:
            score += 15
        elif 0.7 <= pcr <= 1.3:
            score += 10
        else:
            score += 5
    elif strategy_type == MAX_PAIN_MAGNET:
        # Any PCR is fine for max pain
        score += 12

    # === 4. DTE appropriateness (0-20) ===
    if strategy_type == MAX_PAIN_MAGNET:
        # Max pain works best near expiry (0-2 DTE)
        if dte <= 1:
            score += 20
        elif dte <= 3:
            score += 15
        elif dte <= 5:
            score += 10
        else:
            score += 5
    else:
        # OI Wall / VWAP: moderate DTE is fine
        if 1 <= dte <= 4:
            score += 18
        elif dte <= 6:
            score += 14
        else:
            score += 8

    # === 5. IV level — not too expensive to buy (0-20) ===
    if iv < 0.12:
        score += 20  # Cheap options — good for buying
    elif iv < 0.18:
        score += 16
    elif iv < 0.25:
        score += 12
    elif iv < 0.35:
        score += 8
    else:
        score += 4  # Expensive — less attractive for buying

    # Penalty for backtest approximation (no real chain data)
    if not chain_quality:
        score = int(score * 0.85)

    return min(100, max(0, score))


# ====================================================================
# SIGNAL GENERATORS
# ====================================================================

def check_oi_wall_signal(symbol, spot, chain_data, indicators, iv, dte,
                         lot_size, vix=15, r=RISK_FREE_RATE):
    """Generate OI Wall Bouncer signal.

    Logic:
    - Find highest-OI CE strikes above spot (resistance walls)
    - Find highest-OI PE strikes below spot (support walls)
    - If spot approaches PUT wall from above -> BUY CE (support bounce)
    - If spot approaches CALL wall from below -> BUY PE (resistance bounce)

    Args:
        symbol: Trading symbol (NIFTY, BANKNIFTY, SENSEX).
        spot: Current underlying price.
        chain_data: Option chain data (None for backtest).
        indicators: Dict from compute_all_indicators().
        iv: Implied volatility (decimal).
        dte: Days to expiry.
        lot_size: Lot size for the symbol.
        vix: India VIX value.
        r: Risk-free rate.

    Returns:
        Signal dict or None.
    """
    if vix > OIW_VIX_MAX:
        return None
    if dte < 1:
        return None

    strike_interval = STRIKE_INTERVALS.get(symbol, 50)
    T = max(dte, 0.5) / 365
    cpr_width = indicators.get('cpr_width', 0.5)
    pcr = indicators.get('pcr', 1.0)

    # Get OI walls
    if chain_data:
        walls = find_oi_walls(chain_data, spot, top_n=3)
        chain_quality = True
    else:
        walls = approximate_oi_walls_backtest(spot, strike_interval, symbol)
        chain_quality = False

    signal = None

    # Check PUT walls (support) — spot approaching from above -> BUY CE
    for wall in walls.get('put_walls', []):
        wall_strike = wall['strike']
        if wall_strike >= spot:
            continue  # Wall must be below spot

        distance_pct = (spot - wall_strike) / spot
        if distance_pct > OIW_PROXIMITY_PCT:
            continue  # Too far from wall

        # Wall OI check (skip for backtest approximation)
        if chain_quality and wall.get('oi', 0) < OIW_WALL_THRESHOLD:
            continue

        # BUY CE at ATM
        atm_strike = get_nearest_strike(spot, strike_interval)
        greeks = bs_greeks(spot, atm_strike, T, r, iv, 'CE')
        premium = greeks['price']

        # Use real LTP if available
        if chain_data and wall.get('ltp', 0) > 0:
            ce_ltp = _find_chain_ltp(chain_data, atm_strike, 'CE')
            if ce_ltp and ce_ltp > 0:
                premium = ce_ltp

        if premium < OIW_MIN_PREMIUM:
            continue

        target = round(premium * (1 + OIW_TARGET_PCT), 2)
        sl = round(premium * (1 - OIW_SL_PCT), 2)

        score = compute_oi_score(OI_WALL_BOUNCER, vix, cpr_width, pcr, iv, dte,
                                 distance_pct, chain_quality)

        signal = {
            'strategy_type': OI_WALL_BOUNCER,
            'signal_type': 'BUY_CE',
            'symbol': symbol,
            'spot': spot,
            'strike': atm_strike,
            'opt_type': 'CE',
            'entry_premium': round(premium, 2),
            'target': target,
            'sl': sl,
            'greeks': greeks,
            'lot_size': lot_size,
            'dte': dte,
            'iv': iv,
            'vix': vix,
            'quality_score': score,
            'wall_strike': wall_strike,
            'wall_oi': wall.get('oi', 0),
            'distance_pct': round(distance_pct * 100, 2),
            'reason': (f"OI Wall Bounce: PUT wall at {wall_strike:.0f} "
                       f"(OI {wall.get('oi', 0)/1e6:.1f}M), "
                       f"spot {distance_pct*100:.1f}% above -> BUY CE"),
        }
        break  # Take first (strongest) wall signal

    # Check CALL walls (resistance) — spot approaching from below -> BUY PE
    if signal is None:
        for wall in walls.get('call_walls', []):
            wall_strike = wall['strike']
            if wall_strike <= spot:
                continue  # Wall must be above spot

            distance_pct = (wall_strike - spot) / spot
            if distance_pct > OIW_PROXIMITY_PCT:
                continue

            if chain_quality and wall.get('oi', 0) < OIW_WALL_THRESHOLD:
                continue

            atm_strike = get_nearest_strike(spot, strike_interval)
            greeks = bs_greeks(spot, atm_strike, T, r, iv, 'PE')
            premium = greeks['price']

            if chain_data:
                pe_ltp = _find_chain_ltp(chain_data, atm_strike, 'PE')
                if pe_ltp and pe_ltp > 0:
                    premium = pe_ltp

            if premium < OIW_MIN_PREMIUM:
                continue

            target = round(premium * (1 + OIW_TARGET_PCT), 2)
            sl = round(premium * (1 - OIW_SL_PCT), 2)

            score = compute_oi_score(OI_WALL_BOUNCER, vix, cpr_width, pcr, iv, dte,
                                     distance_pct, chain_quality)

            signal = {
                'strategy_type': OI_WALL_BOUNCER,
                'signal_type': 'BUY_PE',
                'symbol': symbol,
                'spot': spot,
                'strike': atm_strike,
                'opt_type': 'PE',
                'entry_premium': round(premium, 2),
                'target': target,
                'sl': sl,
                'greeks': greeks,
                'lot_size': lot_size,
                'dte': dte,
                'iv': iv,
                'vix': vix,
                'quality_score': score,
                'wall_strike': wall_strike,
                'wall_oi': wall.get('oi', 0),
                'distance_pct': round(distance_pct * 100, 2),
                'reason': (f"OI Wall Bounce: CALL wall at {wall_strike:.0f} "
                           f"(OI {wall.get('oi', 0)/1e6:.1f}M), "
                           f"spot {distance_pct*100:.1f}% below -> BUY PE"),
            }
            break

    return signal


def check_max_pain_signal(symbol, spot, chain_data, indicators, iv, dte,
                          lot_size, vix=15, r=RISK_FREE_RATE):
    """Generate Max Pain Magnet signal.

    Logic:
    - Calculate max pain from option chain OI
    - If spot is above max pain (0.3-1.5%) -> BUY PE (expect drop toward MP)
    - If spot is below max pain (0.3-1.5%) -> BUY CE (expect rise toward MP)
    - Stronger signal on expiry day (DTE 0-1)

    Returns:
        Signal dict or None.
    """
    if dte < 0:
        return None

    strike_interval = STRIKE_INTERVALS.get(symbol, 50)
    T = max(dte, 0.5) / 365
    cpr_width = indicators.get('cpr_width', 0.5)
    pcr = indicators.get('pcr', 1.0)

    # Calculate max pain
    if chain_data:
        max_pain = calculate_max_pain(chain_data, spot, strike_interval)
        chain_quality = True
    else:
        max_pain = approximate_max_pain_backtest(spot, strike_interval)
        chain_quality = False

    # Distance from max pain
    distance = spot - max_pain
    distance_pct = abs(distance) / spot

    # Must be in the convergence zone
    if distance_pct < MP_MIN_DISTANCE_PCT:
        return None  # Too close to max pain already
    if distance_pct > MP_MAX_DISTANCE_PCT:
        return None  # Too far — unlikely to converge

    # Determine direction
    if distance > 0:
        # Spot ABOVE max pain -> expect drop -> BUY PE
        signal_type = 'BUY_PE'
        opt_type = 'PE'
    else:
        # Spot BELOW max pain -> expect rise -> BUY CE
        signal_type = 'BUY_CE'
        opt_type = 'CE'

    # Compute premium
    atm_strike = get_nearest_strike(spot, strike_interval)
    greeks = bs_greeks(spot, atm_strike, T, r, iv, opt_type)
    premium = greeks['price']

    # Use real LTP if available
    if chain_data:
        real_ltp = _find_chain_ltp(chain_data, atm_strike, opt_type)
        if real_ltp and real_ltp > 0:
            premium = real_ltp

    if premium < MP_MIN_PREMIUM:
        return None

    # Target: higher on expiry day
    target_mult = MP_EXPIRY_BOOST if dte <= 1 else 1.0
    target = round(premium * (1 + MP_TARGET_PCT * target_mult), 2)
    sl = round(premium * (1 - MP_SL_PCT), 2)

    score = compute_oi_score(MAX_PAIN_MAGNET, vix, cpr_width, pcr, iv, dte,
                             distance_pct, chain_quality)

    return {
        'strategy_type': MAX_PAIN_MAGNET,
        'signal_type': signal_type,
        'symbol': symbol,
        'spot': spot,
        'strike': atm_strike,
        'opt_type': opt_type,
        'entry_premium': round(premium, 2),
        'target': target,
        'sl': sl,
        'greeks': greeks,
        'lot_size': lot_size,
        'dte': dte,
        'iv': iv,
        'vix': vix,
        'quality_score': score,
        'max_pain': max_pain,
        'distance_pct': round(distance_pct * 100, 2),
        'reason': (f"Max Pain: MP={max_pain:.0f}, Spot={spot:.0f} "
                   f"({distance_pct*100:.1f}% {'above' if distance > 0 else 'below'}) "
                   f"-> {signal_type}, DTE={dte}"),
    }


def check_vwap_bounce_signal(symbol, spot, chain_data, indicators, iv, dte,
                              lot_size, vix=15, r=RISK_FREE_RATE):
    """Generate VWAP Bounce signal.

    Logic:
    - Compute VWAP from indicators
    - If spot pulls back to VWAP from above and bounces -> BUY CE
    - If spot pulls back to VWAP from below and bounces -> BUY PE
    - OI confirmation at VWAP-nearest strike (live only)

    For backtest: If day's low touched VWAP and close > VWAP -> bounce up
                  If day's high touched VWAP and close < VWAP -> bounce down

    Returns:
        Signal dict or None.
    """
    if dte < 1:
        return None

    vwap = indicators.get('vwap', 0)
    if vwap <= 0:
        return None

    strike_interval = STRIKE_INTERVALS.get(symbol, 50)
    T = max(dte, 0.5) / 365
    cpr_width = indicators.get('cpr_width', 0.5)
    pcr = indicators.get('pcr', 1.0)

    # Distance from VWAP
    distance_pct = abs(spot - vwap) / spot

    # Check proximity to VWAP
    if distance_pct > VB_VWAP_PROXIMITY:
        return None  # Too far from VWAP

    chain_quality = chain_data is not None

    # Determine direction based on spot vs VWAP
    # For backtest: use indicators to determine trend direction
    prev_close = indicators.get('prev_close', spot)
    support = indicators.get('support', 0)
    resistance = indicators.get('resistance', 0)

    signal_type = None
    opt_type = None

    if spot >= vwap:
        # Spot above VWAP — bouncing up from VWAP support
        # Confirm: spot was previously trending up (close > previous close)
        if prev_close < spot or spot > support:
            signal_type = 'BUY_CE'
            opt_type = 'CE'
    else:
        # Spot below VWAP — bouncing down from VWAP resistance
        if prev_close > spot or spot < resistance:
            signal_type = 'BUY_PE'
            opt_type = 'PE'

    if signal_type is None:
        return None

    # OI confirmation (live only)
    oi_confirmed = False
    vwap_strike = get_nearest_strike(vwap, strike_interval)
    if chain_data:
        if signal_type == 'BUY_CE':
            # Check if PUT OI is building at VWAP strike (support)
            put_oi = get_oi_at_strike(chain_data, vwap_strike, 'PE')
            oi_confirmed = put_oi >= VB_OI_CONFIRM_THRESHOLD
        else:
            # Check if CALL OI is building at VWAP strike (resistance)
            call_oi = get_oi_at_strike(chain_data, vwap_strike, 'CE')
            oi_confirmed = call_oi >= VB_OI_CONFIRM_THRESHOLD

    # Compute premium
    atm_strike = get_nearest_strike(spot, strike_interval)
    greeks = bs_greeks(spot, atm_strike, T, r, iv, opt_type)
    premium = greeks['price']

    if chain_data:
        real_ltp = _find_chain_ltp(chain_data, atm_strike, opt_type)
        if real_ltp and real_ltp > 0:
            premium = real_ltp

    if premium < VB_MIN_PREMIUM:
        return None

    target = round(premium * (1 + VB_TARGET_PCT), 2)
    sl = round(premium * (1 - VB_SL_PCT), 2)

    score = compute_oi_score(VWAP_BOUNCE, vix, cpr_width, pcr, iv, dte,
                             distance_pct, chain_quality)

    # Bonus for OI confirmation
    if oi_confirmed:
        score = min(100, score + 10)

    return {
        'strategy_type': VWAP_BOUNCE,
        'signal_type': signal_type,
        'symbol': symbol,
        'spot': spot,
        'strike': atm_strike,
        'opt_type': opt_type,
        'entry_premium': round(premium, 2),
        'target': target,
        'sl': sl,
        'greeks': greeks,
        'lot_size': lot_size,
        'dte': dte,
        'iv': iv,
        'vix': vix,
        'quality_score': score,
        'vwap': round(vwap, 2),
        'vwap_strike': vwap_strike,
        'oi_confirmed': oi_confirmed,
        'distance_pct': round(distance_pct * 100, 2),
        'reason': (f"VWAP Bounce: VWAP={vwap:.0f}, Spot={spot:.0f} "
                   f"({distance_pct*100:.2f}% away) -> {signal_type}"
                   f"{' [OI confirmed]' if oi_confirmed else ''}"),
    }


# ====================================================================
# HELPER
# ====================================================================
def _find_chain_ltp(chain_data, strike, opt_type):
    """Find LTP for a given strike and option type in chain data."""
    if not chain_data:
        return None
    for item in chain_data:
        s = item.get('strikePrice', item.get('strike', 0))
        t = item.get('optionType', item.get('type', ''))
        if s == strike and t == opt_type:
            return item.get('ltp', item.get('lastPrice', 0))
    return None


# ====================================================================
# MASTER ORCHESTRATOR
# ====================================================================
def check_all_oi_signals(symbol, spot, chain_data, indicators, iv, dte,
                          lot_size, vix=15, r=RISK_FREE_RATE):
    """Generate all viable OI-based signals for a symbol.

    Args:
        symbol: Trading symbol.
        spot: Current spot price.
        chain_data: Option chain data (None for backtest).
        indicators: Dict from compute_all_indicators().
        iv: Implied volatility.
        dte: Days to expiry.
        lot_size: Lot size.
        vix: India VIX.
        r: Risk-free rate.

    Returns:
        List of signal dicts sorted by quality_score descending.
    """
    generators = {
        OI_WALL_BOUNCER: check_oi_wall_signal,
        MAX_PAIN_MAGNET: check_max_pain_signal,
        VWAP_BOUNCE: check_vwap_bounce_signal,
    }

    signals = []
    for strategy_type, generator in generators.items():
        try:
            signal = generator(symbol, spot, chain_data, indicators, iv, dte,
                               lot_size, vix=vix, r=r)
            if signal and signal.get('quality_score', 0) >= 50:
                signals.append(signal)
        except Exception as e:
            continue  # Skip failed signal generators

    # Sort by quality score descending
    signals.sort(key=lambda s: s['quality_score'], reverse=True)
    return signals
