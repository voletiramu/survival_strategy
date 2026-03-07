"""
Shared utility functions for algo trading strategies.
All functions are instrument-agnostic — they take prices, quantities, and config dicts.
"""

import calendar
from datetime import datetime, date, timedelta

# ====================================================================
# DEFAULT CONSTANTS
# ====================================================================
RISK_FREE_RATE = 0.065

# Default brokerage structure (NSE equity options)
DEFAULT_COSTS = {
    'brokerage_per_side': 20,
    'stt_sell_pct': 0.000625,
    'exchange_pct': 0.0005,
    'gst_pct': 0.18,
}


# ====================================================================
# TRADING COST CALCULATIONS
# ====================================================================
def calc_costs(premium, qty, is_sell=False, cost_config=None):
    """Calculate real trading costs for an option trade.

    Args:
        premium: Option premium per unit.
        qty: Number of units (lot_size * num_lots).
        is_sell: True if selling (STT applies on sell side).
        cost_config: Dict with cost parameters. Uses DEFAULT_COSTS if None.
            Keys: brokerage_per_side, stt_sell_pct, exchange_pct, gst_pct.

    Returns:
        Total cost in Rs (brokerage + STT + exchange charges + GST).
    """
    cfg = cost_config if cost_config else DEFAULT_COSTS
    turnover = premium * qty
    brokerage = cfg['brokerage_per_side'] * 2  # Buy + Sell sides
    stt = turnover * cfg['stt_sell_pct'] if is_sell else 0
    exchange = turnover * cfg['exchange_pct']
    gst = brokerage * cfg['gst_pct']
    return brokerage + stt + exchange + gst


def passes_profit_filter(premium, lot_size, target_mult, is_sell=False,
                         min_ratio=2.0, cost_config=None):
    """Check if expected profit exceeds min_ratio x total brokerage cost.

    Args:
        premium: Option premium per unit.
        lot_size: Units per lot.
        target_mult: Target multiplier (e.g. 1.5 means 50% gain for BUY,
                     or 0.3 means 70% decay for SELL).
        is_sell: True for sell trades.
        min_ratio: Minimum profit-to-cost ratio required (default 2.0).
        cost_config: Cost parameters dict (uses DEFAULT_COSTS if None).

    Returns:
        (passes, expected_profit, total_cost) tuple.
    """
    total_cost = calc_costs(premium, lot_size, is_sell, cost_config)
    if is_sell:
        # SELL: profit = entry_premium - target (premium decays to target)
        expected_profit = premium * lot_size * (1 - target_mult)
    else:
        # BUY: profit = target - entry_premium
        expected_profit = premium * lot_size * (target_mult - 1)
    passes = expected_profit >= (total_cost * min_ratio)
    return passes, round(expected_profit, 2), round(total_cost, 2)


# ====================================================================
# EXPIRY DATE UTILITIES (NSE)
# ====================================================================
def get_monthly_expiry(year, month):
    """Get last Thursday of a given month (NSE stock options monthly expiry).

    Args:
        year: Calendar year (e.g. 2026).
        month: Calendar month (1-12).

    Returns:
        datetime.date of the last Thursday.
    """
    # Find the last day of the month
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    # Walk backward to find the last Thursday (weekday 3)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d


def get_nearest_monthly_expiry(ref_date=None):
    """Returns the nearest future monthly expiry (last Thursday of month).

    Args:
        ref_date: Reference date (defaults to today).

    Returns:
        datetime.date of the nearest future monthly expiry.
    """
    if ref_date is None:
        ref_date = date.today()

    # Try current month first
    exp = get_monthly_expiry(ref_date.year, ref_date.month)
    if exp >= ref_date:
        return exp

    # Current month expiry has passed; use next month
    if ref_date.month == 12:
        return get_monthly_expiry(ref_date.year + 1, 1)
    else:
        return get_monthly_expiry(ref_date.year, ref_date.month + 1)


def get_dte_monthly(ref_date=None):
    """Days to nearest monthly expiry.

    Args:
        ref_date: Reference date (defaults to today).

    Returns:
        int: Number of calendar days to expiry.
    """
    if ref_date is None:
        ref_date = date.today()
    exp = get_nearest_monthly_expiry(ref_date)
    return (exp - ref_date).days


def get_nearest_strike(spot, strike_interval):
    """Round spot to nearest strike price.

    Args:
        spot: Current spot price.
        strike_interval: Gap between strikes (e.g. 50 for NIFTY).

    Returns:
        Nearest strike price (int).
    """
    return round(spot / strike_interval) * strike_interval


def get_otm_strike(spot, opt_type, distance_pts, strike_interval):
    """Get OTM strike at approximately `distance_pts` away from spot.

    Args:
        spot: Current spot price.
        opt_type: 'CE' or 'PE'.
        distance_pts: Points away from spot.
        strike_interval: Gap between strikes.

    Returns:
        OTM strike price (int).
    """
    if opt_type == 'CE':
        return round((spot + distance_pts) / strike_interval) * strike_interval
    else:
        return round((spot - distance_pts) / strike_interval) * strike_interval
