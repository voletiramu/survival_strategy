"""
Gamma Blast Strategy -- coiled spring breakout detection.
Instrument-agnostic: takes spot, ohlc, indicators, config dicts.

Detects compressed ranges (coiled spring) followed by directional breakout.
Gamma spike on expiry days amplifies option moves.
"""

from .greeks import bs_greeks
from .utils import RISK_FREE_RATE, get_nearest_strike


def check_gamma_blast(spot, ohlc, indicators, config):
    """Gamma Blast -- coiled spring breakout signal generator.

    Checks for a compressed previous day (coiled spring) followed by
    an intraday directional move. Expiry day gets boosted IV and wider targets.

    Args:
        spot: Current spot price.
        ohlc: Today's OHLC dict: {'open', 'high', 'low', 'close', 'volume'}.
        indicators: dict from compute_all_indicators() with keys:
            prev_range, atr, iv.
        config: Strategy config dict:
            'strike_interval': int (e.g. 50),
            'min_premium_buy': float (e.g. 15),
            'dte': int (days to expiry),
            'r': float (risk-free rate, default RISK_FREE_RATE),
            'coiled_threshold': float (default 1.5; use 2.0 for volatile instruments),
            'is_expiry_day': bool (default False),
            'days_to_expiry': int (days to nearest weekly/monthly expiry),
            'iv_expiry_mult': float (IV multiplier on expiry day, default 1.3),
            'body_atr_threshold': float (min body/ATR ratio, default 0.15),
            'range_atr_threshold': float (min day_range/ATR, default 0.3),
            'chain_ltp_ce': float (real LTP from chain, 0 if unavailable),
            'chain_iv_ce': float (real IV from chain, 0 if unavailable),
            'chain_ltp_pe': float (real LTP from chain, 0 if unavailable),
            'chain_iv_pe': float (real IV from chain, 0 if unavailable),
            'chain_strike_ce': int (chain-selected CE strike, 0 if unavailable),
            'chain_strike_pe': int (chain-selected PE strike, 0 if unavailable).

    Returns:
        list of signal dicts, each with:
            type, strike, premium, greeks, reason, target, sl.
    """
    signals = []
    ind = indicators
    dte = config.get('dte', 7)
    r = config.get('r', RISK_FREE_RATE)
    strike_interval = config.get('strike_interval', 50)
    min_premium_buy = config.get('min_premium_buy', 15)

    # Coiled spring filter: reject if previous day had a large range
    coiled_threshold = config.get('coiled_threshold', 1.5)
    if ind['prev_range'] > coiled_threshold:
        return signals

    atr = ind['atr']
    if atr <= 0:
        return signals

    # Day range filter: need some movement today
    body = spot - ohlc['open']
    day_range = (ohlc['high'] - ohlc['low']) / max(atr, 1)

    range_atr_threshold = config.get('range_atr_threshold', 0.3)
    if day_range < range_atr_threshold:
        return signals

    # Expiry detection
    is_expiry_day = config.get('is_expiry_day', False)
    days_to_expiry = config.get('days_to_expiry', max(dte, 1))
    days_to_expiry = max(days_to_expiry, 1)

    T = max(dte, days_to_expiry) / 365 if not is_expiry_day else 1 / 365

    # IV multiplier: higher on expiry day (gamma spike)
    iv_expiry_mult = config.get('iv_expiry_mult', 1.3)
    iv_mult = iv_expiry_mult if is_expiry_day else max(1.0, iv_expiry_mult - days_to_expiry * 0.08)

    # Target/SL based on expiry proximity
    if is_expiry_day:
        target_mult = 1.6   # 60% gain on expiry gamma spike
        sl_mult = 0.4       # 60% SL (wider for gamma moves)
    else:
        target_mult = max(1.3, 1.5 - days_to_expiry * 0.05)  # 30%-50% gain
        sl_mult = 0.5       # 50% SL

    day_label = "EXPIRY" if is_expiry_day else f"{days_to_expiry}DTE"

    # Body must exceed threshold
    body_atr_threshold = config.get('body_atr_threshold', 0.15)
    if abs(body) > atr * body_atr_threshold:
        if body > 0:
            # ---- Up breakout --> BUY CE ----
            chain_ltp = config.get('chain_ltp_ce', 0)
            chain_iv = config.get('chain_iv_ce', 0)
            chain_strike = config.get('chain_strike_ce', 0)

            ce_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
            use_iv = chain_iv if chain_iv > 0 else ind['iv'] * iv_mult
            g = bs_greeks(spot, ce_strike, T, r, use_iv, 'CE')
            premium = chain_ltp if chain_ltp > 0 else g['price']

            if premium > min_premium_buy:
                signals.append({
                    'type': 'BUY_CE_GAMMA',
                    'strike': ce_strike,
                    'premium': premium,
                    'greeks': g,
                    'reason': (f"Gamma Blast [{day_label}]: Up breakout body={body:.0f} "
                               f"ATR={atr:.0f} range={day_range:.2f}x"
                               f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                    'target': premium * target_mult,
                    'sl': premium * sl_mult,
                })
        else:
            # ---- Down breakout --> BUY PE ----
            chain_ltp = config.get('chain_ltp_pe', 0)
            chain_iv = config.get('chain_iv_pe', 0)
            chain_strike = config.get('chain_strike_pe', 0)

            pe_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
            use_iv = chain_iv if chain_iv > 0 else ind['iv'] * iv_mult
            g = bs_greeks(spot, pe_strike, T, r, use_iv, 'PE')
            premium = chain_ltp if chain_ltp > 0 else g['price']

            if premium > min_premium_buy:
                signals.append({
                    'type': 'BUY_PE_GAMMA',
                    'strike': pe_strike,
                    'premium': premium,
                    'greeks': g,
                    'reason': (f"Gamma Blast [{day_label}]: Down breakout body={body:.0f} "
                               f"ATR={atr:.0f} range={day_range:.2f}x"
                               f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                    'target': premium * target_mult,
                    'sl': premium * sl_mult,
                })

    return signals
