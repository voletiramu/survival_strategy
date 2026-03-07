"""
Hold strength scoring for open positions.
Instrument-agnostic -- scores how strongly signals support staying in a trade.
"""


def compute_hold_score(pos, spot, indicators, current_oi=None, current_iv=None):
    """Compute hold strength score (0-100) for an open position.

    Determines whether market conditions still support the current trade.

    Five-factor scoring:
        1. Signal alignment (0-30): Is spot on our side of CPR pivot?
        2. Spot momentum    (0-25): Is spot moving in our direction?
        3. Premium health   (0-20): Is premium growing or stable?
        4. OI trend         (0-15): Is OI accumulating (increasing)?
        5. IV stability     (0-10): Is IV stable (not spiking against us)?

    Interpretation:
        >= 60 (STRONG):   Raise TSL aggressively, override TIME_EXIT.
        40-59 (MODERATE): Normal TSL behaviour.
        < 40 (WEAK):      Allow early exit on losing positions.

    Args:
        pos: Position dict with keys:
            signal_type (str, e.g. 'BUY_CE_CPR'),
            entry_spot (float),
            entry_premium (float),
            current_premium (float),
            is_sell (bool),
            entry_oi (float, optional),
            current_oi (float, optional),
            entry_iv (float, optional),
            current_iv (float, optional).
        spot: Current spot price of the underlying.
        indicators: dict from compute_all_indicators() with keys:
            pivot, bc, tc, atr.
        current_oi: Live OI value (overrides pos['current_oi'] if provided).
        current_iv: Live IV value (overrides pos['current_iv'] if provided).

    Returns:
        int: Hold score clamped to 0-100.
    """
    score = 0
    is_bullish = 'CE' in pos.get('signal_type', '')

    # ---- 1. Signal alignment (0-30): Is spot on our side of pivot? ----
    if indicators:
        pivot = indicators.get('pivot', spot)
        bc = indicators.get('bc', pivot)
        tc = indicators.get('tc', pivot)
        if is_bullish:
            if spot > pivot:
                score += 30
            elif spot > bc:
                score += 15
            # Spot below CPR = signal reversed, 0 points
        else:
            if spot < pivot:
                score += 30
            elif spot < tc:
                score += 15

    # ---- 2. Spot momentum (0-25): Is spot moving in our direction? ----
    entry_spot = pos.get('entry_spot', spot)
    atr = indicators.get('atr', 100) if indicators else 100
    spot_move = spot - entry_spot
    # Normalize move by ATR: 1 ATR move = 25 points
    if atr > 0:
        normalized = abs(spot_move) / atr
        if (is_bullish and spot_move > 0) or (not is_bullish and spot_move < 0):
            score += min(25, int(normalized * 25))
        # Spot moving AGAINST us = 0 points (no penalty, just no bonus)

    # ---- 3. Premium health (0-20): Is premium gaining? ----
    entry_prem = pos.get('entry_premium', 0)
    current_prem = pos.get('current_premium', entry_prem)
    if entry_prem > 0:
        if pos.get('is_sell'):
            # SELL: premium going DOWN is profit
            gain_pct = (entry_prem - current_prem) / entry_prem * 100
        else:
            # BUY: premium going UP is profit
            gain_pct = (current_prem - entry_prem) / entry_prem * 100
        if gain_pct > 20:
            score += 20
        elif gain_pct > 10:
            score += 15
        elif gain_pct > 0:
            score += 10
        elif gain_pct > -5:
            score += 5

    # ---- 4. OI trend (0-15): Increasing OI = accumulation ----
    entry_oi = pos.get('entry_oi', 0)
    oi = current_oi if current_oi is not None else pos.get('current_oi', entry_oi)
    if entry_oi and entry_oi > 0 and oi and oi > 0:
        oi_change = (oi - entry_oi) / entry_oi
        if oi_change > 0.10:
            score += 15
        elif oi_change > 0:
            score += 10
        elif oi_change > -0.10:
            score += 5

    # ---- 5. IV stability (0-10): Stable IV = predictable environment ----
    entry_iv_val = pos.get('entry_iv', 0)
    iv_val = current_iv if current_iv is not None else pos.get('current_iv', entry_iv_val)
    if entry_iv_val and entry_iv_val > 0 and iv_val and iv_val > 0:
        iv_change = abs(iv_val - entry_iv_val) / entry_iv_val
        if iv_change < 0.10:
            score += 10
        elif iv_change < 0.20:
            score += 5

    return max(0, min(100, score))
