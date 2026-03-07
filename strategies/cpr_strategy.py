"""
CPR (Central Pivot Range) Breakout Strategy -- Gomathi Shankar methodology.
Instrument-agnostic: takes spot, indicators, config dicts.

BUY breakout signals for all CPR widths, target/SL scaled by CPR width.
SELL mean reversion only for very wide CPR (> 0.6%).
"""

from .greeks import bs_greeks
from .utils import RISK_FREE_RATE, get_nearest_strike


def check_cpr_breakout(spot, ohlc, indicators, config):
    """CPR Breakout -- instrument-agnostic signal generator.

    Generates BUY breakout signals when spot breaks above TC (bullish) or
    below BC (bearish). SELL mean reversion signals for wide CPR only.

    Args:
        spot: Current spot price.
        ohlc: Today's OHLC dict: {'open', 'high', 'low', 'close'}.
        indicators: dict from compute_all_indicators() with keys:
            tc, bc, cpr_width, cam_r3, cam_r4, cam_s3, cam_s4, iv.
        config: Strategy config dict:
            'strike_interval': int (e.g. 50 for NIFTY),
            'min_premium_buy': float (e.g. 15),
            'min_premium_sell': float (e.g. 20),
            'dte': int (days to expiry),
            'r': float (risk-free rate, default RISK_FREE_RATE),
            'allow_sell': bool (default True, set False if no margin),
            'chain_ltp_ce': float (real LTP from option chain, 0 if unavailable),
            'chain_iv_ce': float (real IV from chain, 0 if unavailable),
            'chain_ltp_pe': float (real LTP from option chain, 0 if unavailable),
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
    T = dte / 365
    r = config.get('r', RISK_FREE_RATE)
    strike_interval = config.get('strike_interval', 50)
    min_premium_buy = config.get('min_premium_buy', 15)
    min_premium_sell = config.get('min_premium_sell', 20)
    cpr_w = ind['cpr_width']

    # ---- CPR Width Tiers: Scale targets by width ----
    # Narrow CPR = strong breakout potential, moderate targets
    # Moderate CPR = directional trade, standard targets
    # Wide CPR = breakout less likely, conservative targets
    if cpr_w < 0.3:
        cpr_label = "Narrow"
        target_hit_mult = 1.5   # 50% gain
        target_base_mult = 1.3  # 30% gain
        sl_mult = 0.5           # 50% SL
    elif cpr_w <= 0.6:
        cpr_label = "Moderate"
        target_hit_mult = 1.4   # 40% gain
        target_base_mult = 1.25 # 25% gain
        sl_mult = 0.5
    else:
        cpr_label = "Wide"
        target_hit_mult = 1.3   # 30% gain
        target_base_mult = 1.2  # 20% gain
        sl_mult = 0.5

    # ---- BUY BREAKOUT: All CPR widths ----
    # Spot above TC --> Bullish breakout --> Buy CE
    if spot > ind['tc']:
        chain_ltp = config.get('chain_ltp_ce', 0)
        chain_iv = config.get('chain_iv_ce', 0)
        chain_strike = config.get('chain_strike_ce', 0)

        ce_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
        use_iv = chain_iv if chain_iv > 0 else ind['iv']
        g = bs_greeks(spot, ce_strike, T, r, use_iv, 'CE')
        premium = chain_ltp if chain_ltp > 0 else g['price']

        if premium > min_premium_buy:
            # Target: higher if price confirmed beyond Camarilla R3
            target = (premium * target_hit_mult
                      if ohlc['high'] > ind['cam_r3']
                      else premium * target_base_mult)
            signals.append({
                'type': 'BUY_CE_CPR',
                'strike': ce_strike,
                'premium': premium,
                'greeks': g,
                'reason': (f"{cpr_label} CPR ({cpr_w:.3f}%) bullish breakout "
                           f"above TC={ind['tc']:.0f}"
                           f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                'target': target,
                'sl': premium * sl_mult,
            })

    # Spot below BC --> Bearish breakdown --> Buy PE
    elif spot < ind['bc']:
        chain_ltp = config.get('chain_ltp_pe', 0)
        chain_iv = config.get('chain_iv_pe', 0)
        chain_strike = config.get('chain_strike_pe', 0)

        pe_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
        use_iv = chain_iv if chain_iv > 0 else ind['iv']
        g = bs_greeks(spot, pe_strike, T, r, use_iv, 'PE')
        premium = chain_ltp if chain_ltp > 0 else g['price']

        if premium > min_premium_buy:
            target = (premium * target_hit_mult
                      if ohlc['low'] < ind['cam_s3']
                      else premium * target_base_mult)
            signals.append({
                'type': 'BUY_PE_CPR',
                'strike': pe_strike,
                'premium': premium,
                'greeks': g,
                'reason': (f"{cpr_label} CPR ({cpr_w:.3f}%) bearish breakout "
                           f"below BC={ind['bc']:.0f}"
                           f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                'target': target,
                'sl': premium * sl_mult,
            })

    # ---- SELL MEAN REVERSION: Only for very wide CPR (> 0.6%) ----
    allow_sell = config.get('allow_sell', True)
    if cpr_w > 0.6 and allow_sell:
        # Sell CE at R4 when price touches R3
        if (ohlc['high'] >= ind['cam_r3'] * 0.998
                and spot < ind['cam_r4']):
            ce_sell_strike = round(ind['cam_r4'] / strike_interval) * strike_interval
            g = bs_greeks(spot, ce_sell_strike, T, r, ind['iv'], 'CE')
            if g['price'] > min_premium_sell:
                signals.append({
                    'type': 'SELL_CE_CPR',
                    'strike': ce_sell_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': (f"Wide CPR ({cpr_w:.3f}%) mean reversion "
                               f"at R3={ind['cam_r3']:.0f}"),
                    'target': g['price'] * 0.3,
                    'sl': g['price'] * 1.2,
                })

        # Sell PE at S4 when price touches S3
        if (ohlc['low'] <= ind['cam_s3'] * 1.002
                and spot > ind['cam_s4']):
            pe_sell_strike = round(ind['cam_s4'] / strike_interval) * strike_interval
            g = bs_greeks(spot, pe_sell_strike, T, r, ind['iv'], 'PE')
            if g['price'] > min_premium_sell:
                signals.append({
                    'type': 'SELL_PE_CPR',
                    'strike': pe_sell_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': (f"Wide CPR ({cpr_w:.3f}%) mean reversion "
                               f"at S3={ind['cam_s3']:.0f}"),
                    'target': g['price'] * 0.3,
                    'sl': g['price'] * 1.2,
                })

    return signals
