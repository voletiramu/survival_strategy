"""
Shared strategy library — instrument-agnostic signal generation.
Used by both the index/commodity bot (paper_trader.py) and stock bot (stock_paper_trader.py).
"""

# Core indicators
from .indicators import (
    compute_cpr, compute_camarilla, compute_atr, compute_atr_wilder,
    compute_historical_volatility,
)

# Strategy signal generators (function-based — instrument-agnostic)
from .cpr_strategy import check_cpr_breakout
from .gamma_blast_strategy import check_gamma_blast
from .pcr_vwap_strategy import check_pcr_vwap
from .ghost_zone_strategy import detect_ghost_zones, enrich_indicators_with_zones, check_ghost_zone_signals

# Scoring
from .signal_scoring import compute_signal_score
from .hold_scoring import compute_hold_score

# Greeks
from .greeks import bs_greeks, black76_greeks, implied_vol, greeks_from_market_price

# Exit engine
from .exit_engine import compute_tsl, check_tsl_hit, check_target_hit, check_max_loss, check_sl_hit

# Utilities
from .utils import calc_costs, passes_profit_filter, get_monthly_expiry, get_nearest_monthly_expiry, get_dte_monthly

# Backtest strategy classes (used by backtest runners, not paper trader)
try:
    from .pcr_vwap_strategy import PCRVWAPStrategy
    from .survivor_strategy import SurvivorStrategy
    from .wave_strategy import WaveStrategy
except ImportError:
    pass  # Optional — backtest_engine may not be on path
