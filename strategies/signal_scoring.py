"""
Signal quality scoring for algo trading strategies.
Instrument-agnostic -- scores any signal based on Greeks, indicators, and market conditions.
"""


def compute_signal_score(signal, spot, indicators, vix=None):
    """Compute quality score (0-100) for a trading signal.

    Five-factor scoring:
        1. Delta positioning (0-25)
        2. ATR-premium ratio (0-20)
        3. VIX level (0-20)
        4. Momentum confirmation (0-20)
        5. PCR confirmation (0-15)

    Penalties applied for missing data quality indicators.

    Args:
        signal: Signal dict with keys:
            'type': str containing 'BUY' or 'SELL' and 'CE' or 'PE',
            'premium': float,
            'greeks': dict with 'delta', etc.
        spot: Current spot price of the underlying.
        indicators: dict from compute_all_indicators() with keys:
            atr, prev_close, pcr, and optional _data_quality dict.
        vix: Current VIX value (optional, None if unavailable).

    Returns:
        int: Quality score clamped to 0-100.
    """
    score = 0
    greeks = signal.get('greeks', {})
    premium = signal.get('premium', 0)
    atr = indicators.get('atr', 1)
    is_buy = 'BUY' in signal.get('type', '')
    is_ce = 'CE' in signal.get('type', '')

    # ---- 1. DELTA (0-25): sweet spot scoring ----
    delta = abs(greeks.get('delta', 0))
    if is_buy:
        # BUY: sweet spot is ATM-ish (0.35-0.65)
        if 0.35 <= delta <= 0.65:
            score += 25
        elif 0.25 <= delta < 0.35 or 0.65 < delta <= 0.75:
            score += 18
        elif 0.15 <= delta < 0.25:
            score += 10
        else:
            score += 5
    else:
        # SELL: sweet spot is OTM (0.10-0.25)
        if 0.10 <= delta <= 0.25:
            score += 25
        elif 0.25 < delta <= 0.35:
            score += 18
        else:
            score += 10

    # ---- 2. ATR-PREMIUM RATIO (0-20) ----
    if atr > 0:
        ratio = premium / atr
        if is_buy:
            if 0.05 < ratio < 0.3:
                score += 20
            elif 0.3 <= ratio < 0.5:
                score += 12
            else:
                score += 5
        else:
            if ratio > 0.3:
                score += 20
            elif ratio > 0.15:
                score += 12
            else:
                score += 5

    # ---- 3. VIX LEVEL (0-20) ----
    if vix is not None:
        if vix > 20:
            score += 20
        elif vix > 16:
            score += 15
        elif vix > 14:
            score += 10
        elif vix > 12:
            score += 5
        else:
            score += 2

    # ---- 4. MOMENTUM (0-20): body direction matches signal ----
    prev_close = indicators.get('prev_close', spot)
    body = spot - prev_close
    if is_buy:
        if (is_ce and body > 0) or (not is_ce and body < 0):
            score += 20
        elif abs(body) < atr * 0.1:
            score += 10
        else:
            score += 3
    else:
        # SELL prefers flat/range-bound
        if abs(body) < atr * 0.2:
            score += 20
        elif abs(body) < atr * 0.5:
            score += 12
        else:
            score += 5

    # ---- 5. PCR CONFIRMATION (0-15) ----
    pcr = indicators.get('pcr', 1.0)
    if is_buy:
        # BUY CE wants bullish PCR (>1), BUY PE wants bearish PCR (<1)
        if (is_ce and pcr > 1.0) or (not is_ce and pcr < 1.0):
            score += 15
        elif 0.9 < pcr < 1.1:
            score += 8
        else:
            score += 3
    else:
        # SELL prefers neutral PCR
        if 0.9 < pcr < 1.1:
            score += 15
        else:
            score += 8

    # ---- DATA QUALITY PENALTY ----
    data_quality = indicators.get('_data_quality', {})
    if not data_quality.get('has_iv', True):
        score -= 15  # No IV data = blind entry
    if not data_quality.get('has_pcr', True):
        score -= 10  # No PCR = no sentiment confirmation
    if not data_quality.get('has_vwap', True):
        score -= 5   # Missing VWAP is less critical

    return max(min(score, 100), 0)
