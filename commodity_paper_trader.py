"""
COMMODITY PAPER TRADING SYSTEM (v7.1)
=======================================
Paper trading for MCX commodity options using Angel One SmartAPI
3 ACTIVE STRATEGIES: CPR (45%), Gamma Blast (35%), Ghost Zone (20%)
Uses Black-76 model (options on futures)

Focus: Gold Mini, Crude Oil Mini (affordable with Rs 3L)

Per-Strategy Capital Allocation (Rs 3L total):
  CPR:          45% = Rs 1,35,000 — best Sharpe on commodities
  Gamma Blast:  35% = Rs 1,05,000 — solid across all commodities
  Ghost Zone:   20% = Rs 60,000 — good WR, lower Sharpe

Trading Hours: MCX 9:00 AM - 11:30 PM (extended vs equity 9:15-3:30)

Usage:
    python commodity_paper_trader.py              # Live continuous
    python commodity_paper_trader.py --once       # Single scan
    python commodity_paper_trader.py --offline    # Offline test
    python commodity_paper_trader.py --status     # Show positions
    python commodity_paper_trader.py --reset      # Reset portfolio
"""

import sys
import os
import json
import time
import signal
import logging
from datetime import datetime, timedelta, time as dtime
import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_commodity')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

for d in [PAPER_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

INITIAL_CAPITAL = 300000  # Rs 3L for commodities (all strategies share this pool)
RISK_FREE_RATE = 0.065

# Focus on MINI contracts (affordable with Rs 3L capital)
COMMODITIES = {
    'GOLDM': {
        'lot_size': 1, 'multiplier': 10, 'margin': 15000,
        'strike_interval': 100, 'vol_adj': 1.0,
        'file': 'GOLDM_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Gold Mini (100g)',
    },
    'SILVERM': {
        'lot_size': 1, 'multiplier': 5, 'margin': 15000,
        'strike_interval': 500, 'vol_adj': 1.15,
        'file': 'SILVERM_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Silver Mini (5kg)',
    },
    'CRUDEOILM': {
        'lot_size': 1, 'multiplier': 10, 'margin': 8000,
        'strike_interval': 50, 'vol_adj': 1.4,
        'file': 'CRUDEOILM_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Crude Oil Mini (10 bbl)',
    },
    'GOLD': {
        'lot_size': 1, 'multiplier': 100, 'margin': 100000,
        'strike_interval': 500, 'vol_adj': 1.0,
        'file': 'GOLD_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Gold Standard (1kg)',
    },
    'SILVER': {
        'lot_size': 1, 'multiplier': 30, 'margin': 80000,
        'strike_interval': 500, 'vol_adj': 1.15,
        'file': 'SILVER_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Silver (30kg)',
    },
    'NATURALGAS': {
        'lot_size': 1, 'multiplier': 1250, 'margin': 70000,
        'strike_interval': 5, 'vol_adj': 1.8,
        'file': 'NATURALGAS_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Natural Gas (1250 mmBtu)',
    },
    'COPPER': {
        'lot_size': 1, 'multiplier': 2500, 'margin': 60000,
        'strike_interval': 5, 'vol_adj': 1.1,
        'file': 'COPPER_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Copper (2500kg)',
    },
}

# For paper trading with Rs 3L, focus on affordable mini contracts
# v2.5.2: Removed SILVERM — premium Rs 18K × lot 5 × mult 5 = Rs 450K+ per trade,
# far exceeds Rs 75K per-trade limit. Needs Rs 15L+ capital to trade.
PAPER_TRADE_COMMODITIES = ['GOLDM', 'CRUDEOILM']

# ====================================================================
# CAPITAL & RISK MANAGEMENT FOR COMMODITY TRADING
# ====================================================================
COMMODITY_CAPITAL = 300000                              # Rs 3L for commodities
MAX_RISK_PCT = 25                                       # Max 25% of capital per trade
MAX_PER_TRADE = COMMODITY_CAPITAL * MAX_RISK_PCT / 100  # Rs 75,000
MAX_POSITIONS_PER_COMMODITY = 3                         # Prevent cascade (e.g., 16 SILVERM trades)
MAX_DAILY_LOSS = COMMODITY_CAPITAL * 0.10               # Rs 30,000 (10% daily loss limit)

# ====================================================================
# PER-STRATEGY TRACKING FOR COMMODITIES (v7.2) — MONITORING ONLY, NO HARD CAPS
# ====================================================================
# Rs 3L is a SHARED POOL across all commodity strategies.
# Strategies compete for the same capital — no per-strategy hard limits.
MCX_STRATEGY_ALLOCATION = {
    'CPR':          0.45,    # 45% target — best Sharpe on commodities
    'Gamma Blast':  0.35,    # 35% target — solid across all commodities
    'Ghost Zone':   0.20,    # 20% target — good WR but lower Sharpe
}

def get_mcx_strategy_used_capital(positions, strategy_name, commodities_spec=None):
    """Calculate capital currently used by a specific commodity strategy (for logging only)."""
    used = 0
    for p in positions:
        if p.get('strategy', '') != strategy_name:
            continue
        spec = (commodities_spec or COMMODITIES).get(p['commodity'], {})
        if p.get('is_sell', False):
            used += spec.get('margin', 15000) * p.get('num_lots', 1)
        else:
            used += p['entry_premium'] * p.get('lot_size', spec.get('lot_size', 1)) * p.get('multiplier', spec.get('multiplier', 1))
    return used

# v2.5.3: Tiered lot sizing — scale lots by signal quality + available capital
MCX_MAX_LOTS = {'GOLDM': 5, 'SILVERM': 3, 'CRUDEOILM': 5}
MCX_LOT_TIER_ELITE = 80     # Score >= 80: allocate 30% of available capital
MCX_LOT_TIER_STRONG = 60    # Score >= 60: allocate 20% of available capital
MCX_LOT_TIER_STANDARD = 50  # Score >= 50: allocate 10% of available capital

# v7.6: TSL based on PREMIUM GAIN % (not target distance %) — matches equity v7.5
TSL_BREAKEVEN_GAIN_PCT = 15      # Phase 1: Lock breakeven when premium gains 15%
TSL_TRAIL_GAIN_PCT = 25          # Phase 2: Start trailing when premium gains 25%
TSL_TRAIL_DISTANCE_PCT = 30      # Phase 2 trail: 30% below peak profit
TSL_TIGHT_GAIN_PCT = 40          # Phase 3: Tight trail at 40%+ gain
TSL_TIGHT_DISTANCE_PCT = 20      # Phase 3 trail: 20% below peak profit

# Commodity OI/IV Exit Thresholds (wider than equity due to lower liquidity)
MCX_OI_SURGE_PCT = 40           # Exit if OI changes >40% from entry (was 20%)
MCX_OI_SURGE_FIRST_HOUR_PCT = 55  # First hour: wider threshold
MCX_OI_SURGE_MID_DAY_PCT = 40     # Mid session
MCX_OI_SURGE_LAST_HOUR_PCT = 45   # Last 2 hours
MCX_OI_REVERSE_PCT = 60         # Reverse trade if OI changes >60% (was 30%)
MCX_IV_SPIKE_PCT = 40           # Exit if IV changes >40% from entry (was 30%)
MCX_IV_REVERSE_PCT = 60         # Reverse trade if IV changes >60% (was 50%)
MCX_OI_IV_COMBO_OI = 25         # Combined exit: OI >25% AND IV >30% (was 15%/20%)
MCX_OI_IV_COMBO_IV = 30
MCX_GAMMA_SHIELD_THRESHOLD = 0.003  # Exit short if gamma > this

# v2.4: Strategy-specific OI/IV exit multipliers for commodities
MCX_STRATEGY_EXIT_MULT = {
    'CPR':          {'oi': 1.0, 'iv': 1.0},
    'Gamma Blast':  {'oi': 1.0, 'iv': 1.0},
    'Ghost Zone':   {'oi': 0.7, 'iv': 0.8},    # 30% LOWER threshold → exits faster
    'PCR+VWAP':     {'oi': 1.0, 'iv': 1.0},
    'Survivor':     {'oi': 2.0, 'iv': 2.0},    # 2x HIGHER threshold → tolerates swings
}

# Trade Quality Filters (commodity — eliminate brokerage-losing weak trades)
MCX_MIN_PREMIUM_BUY = 5         # Min Rs 5 premium for commodity BUY trades
MCX_MIN_PREMIUM_SELL = 10       # Min Rs 10 premium for commodity SELL trades
MCX_MIN_SIGNAL_SCORE = 50       # v2.5: Quality score 0-100, reject below 50 (was 40)
MCX_DIRECTION_FLIP_MIN_SCORE = 70  # v7.6.2: Higher bar for DIRECTION_FLIP (closing existing to flip)

# v7.7: Signal-Based Hold Score — controls exits
MCX_HOLD_SCORE_STRONG = 60        # >= 60: Raise TSL aggressively, override TIME_EXIT/OI_SURGE
MCX_HOLD_SCORE_WEAK = 40          # < 40: Allow early exit on losing positions
MCX_HOLD_SCORE_MIN_HOLD_MINS = 30 # Minimum hold time before computing hold score
MCX_MIN_PROFIT_TO_COST_RATIO = 2.0  # Expected profit must be >= 2x total cost

# Cooldowns & Limits (commodity)
MCX_GRACE_PERIOD_SECONDS = 180     # v7.6: 3 min (was 10 min — missed fast spikes)
MCX_GHOST_ZONE_COOLDOWN_SECONDS = 1800  # 30 min after Ghost Zone loss
MCX_REENTRY_COOLDOWN_SECONDS = 600     # 10 min after any exit, same symbol
MCX_MAX_TRADES_PER_DAY = 12           # Hard cap on daily commodity trades
MCX_MAX_SAME_DIRECTION_PER_COMMODITY = 5  # v9.5: Max BUY_CE or BUY_PE per commodity per day
MCX_SCORE_ESCALATION_PER_REENTRY = 15    # v9.5: Each re-entry same strat+dir needs +15 score
MCX_MIN_OI_EXIT_PNL = 50              # Min Rs 50 PnL to allow OI/IV exit (covers 2x MCX brokerage)

# v9.6: VIX adaptation (ported from equity — same VIX affects commodity sentiment)
MCX_VIX_LOW_THRESHOLD = 14            # Below 14 = low vol regime → stricter filters 1.5x
MCX_VIX_HIGH_THRESHOLD = 20           # Above 20 = high vol regime → relax filters 0.8x
MCX_VIX_LOW_MULTIPLIER = 1.5
MCX_VIX_HIGH_MULTIPLIER = 0.8

# MCX Trading costs
MCX_BROKERAGE = 20
MCX_CTT = 0.0001
MCX_EXCHANGE = 0.00026
MCX_GST = 0.18

# MCX Market hours (extended)
MCX_OPEN = dtime(9, 0)
MCX_CLOSE = dtime(23, 30)  # 11:30 PM
COMMODITY_TRADE_START = dtime(9, 15)  # Commodity trades from 9:15 AM (no time barrier)
MCX_FIRST_TRADE_TIME = dtime(9, 30)  # v8.0: No trades before 09:30 — first 30 min OI spikes/wide spreads
MCX_LAST_ENTRY_TIME = dtime(22, 30)  # v2.5.2: No new entries after 10:30 PM (60 min before close)

ANGEL_CRED_FILE = os.environ.get('ANGEL_CRED_FILE', r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt")

# Logging
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'commodity_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('CommodityPaperTrader')


# ====================================================================
# BLACK-76 MODEL
# ====================================================================
def black76_greeks(F, K, T, r, sigma, opt_type='CE'):
    """Black-76 for commodity options on futures."""
    T = max(T, 1e-6); sigma = max(sigma, 0.01)
    F = max(F, 0.01); K = max(K, 0.01)
    d1 = (np.log(F/K) + 0.5*sigma**2*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    disc = np.exp(-r * T)
    if opt_type == 'CE':
        price = disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
        delta = disc * norm.cdf(d1)
    else:
        price = disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        delta = -disc * norm.cdf(-d1)
    gamma = disc * norm.pdf(d1) / (F * sigma * np.sqrt(T))
    theta = -(F * sigma * norm.pdf(d1) * disc) / (2 * np.sqrt(T)) / 365
    vega = F * np.sqrt(T) * norm.pdf(d1) * disc / 100
    return {'price': max(price, 0.01), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega, 'iv': sigma}


def select_optimal_mcx_strike(spot, strike_int, T, r, iv, opt_type, min_premium=5.0):
    """v9.5: Select best strike using delta+gamma optimization.

    Evaluates 5 candidate strikes (ATM-2 to ATM+2) and picks the one with:
    - Delta closest to 0.50 (sweet spot for directional trades)
    - Highest gamma (maximum acceleration)
    - Premium above minimum threshold

    Args:
        spot: Current spot/futures price
        strike_int: Strike interval (e.g., 50 for CRUDEOILM)
        T: Time to expiry in years
        r: Risk-free rate
        iv: Implied volatility (decimal)
        opt_type: 'CE' or 'PE'
        min_premium: Minimum premium filter (default Rs 5)

    Returns:
        dict: {strike, greeks} where greeks is the black76_greeks output for the chosen strike
    """
    atm = round(spot / strike_int) * strike_int
    candidates = [atm + i * strike_int for i in range(-2, 3)]

    best_strike = atm
    best_score = -1
    best_greeks = None

    for strike in candidates:
        g = black76_greeks(spot, strike, T, r, iv, opt_type)
        delta = abs(g['delta'])
        gamma = g['gamma']
        premium = g['price']

        if premium < min_premium:
            continue  # too cheap, skip

        # Delta score: peak at 0.50, drops off linearly
        # delta=0.50 → score=1.0, delta=0.30 or 0.70 → score=0.0
        delta_score = max(0, 1 - abs(delta - 0.50) * 5)

        # Gamma score: normalize (gamma typically 0.0001-0.001 for commodities)
        gamma_score = gamma * 10000

        # Premium penalty: slightly penalize very expensive deep ITM options
        premium_penalty = max(0, (premium - 500) * 0.001) if premium > 500 else 0

        # Weighted score: delta (40%) + gamma (40%) + premium reasonableness (20%)
        score = delta_score * 40 + gamma_score * 40 + max(0, 1 - premium_penalty) * 20

        if score > best_score:
            best_score = score
            best_strike = strike
            best_greeks = g

    # Fallback: if no candidate passed, use ATM
    if best_greeks is None:
        best_greeks = black76_greeks(spot, atm, T, r, iv, opt_type)
        best_strike = atm
        logger.debug(f"  GREEKS_STRIKE: Fallback to ATM={atm} (no candidate passed min_premium)")

    return {'strike': best_strike, 'greeks': best_greeks}


def implied_vol_b76(market_price, F, K, T, r, opt_type='CE', max_iter=50, tol=1e-4):
    """Back-solve for implied volatility from real market price using Black-76.
    Returns IV (decimal) or None if convergence fails.
    """
    if market_price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    # Initial guess: ATM approximation
    sigma = market_price / F * np.sqrt(2 * np.pi / T)
    sigma = max(min(sigma, 3.0), 0.01)
    for _ in range(max_iter):
        g = black76_greeks(F, K, T, r, sigma, opt_type)
        diff = g['price'] - market_price
        vega_full = g['vega'] * 100  # vega was divided by 100
        if abs(vega_full) < 1e-10:
            break
        sigma -= diff / vega_full
        sigma = max(min(sigma, 3.0), 0.005)
        if abs(diff) < tol:
            return sigma
    return sigma if 0.005 < sigma < 3.0 else None


def greeks_from_market_price_b76(market_price, F, K, T, r, opt_type='CE'):
    """Compute accurate commodity Greeks from real market price using Black-76."""
    iv = implied_vol_b76(market_price, F, K, T, r, opt_type)
    if iv is None:
        return None
    g = black76_greeks(F, K, T, r, iv, opt_type)
    g['iv'] = iv
    g['price'] = market_price
    return g


def calc_mcx_costs(premium, qty, multiplier, is_sell=False):
    """MCX trading costs."""
    turnover = premium * qty * multiplier
    brokerage = MCX_BROKERAGE * 2
    ctt = turnover * MCX_CTT if is_sell else 0
    exchange = turnover * MCX_EXCHANGE
    gst = brokerage * MCX_GST
    return brokerage + ctt + exchange + gst


def mcx_passes_profit_filter(premium, lot_size, multiplier, target_multiplier, is_sell=False):
    """Check if expected profit exceeds 2x brokerage cost (commodity version)."""
    total_cost = calc_mcx_costs(premium, lot_size, multiplier, is_sell)
    if is_sell:
        expected_profit = premium * lot_size * multiplier * (1 - target_multiplier)
    else:
        expected_profit = premium * lot_size * multiplier * (target_multiplier - 1)
    passes = expected_profit >= (total_cost * MCX_MIN_PROFIT_TO_COST_RATIO)
    return passes, round(expected_profit, 2), round(total_cost, 2)


def mcx_compute_signal_score(signal, spot, indicators, vix=None):
    """Compute quality score (0-100) for a commodity trading signal."""
    score = 0
    greeks = signal.get('greeks', {})
    premium = signal.get('premium', 0)
    atr = indicators.get('atr', 1)
    is_buy = 'BUY' in signal.get('type', '')
    is_ce = 'CE' in signal.get('type', '')

    # 1. DELTA (0-25)
    delta = abs(greeks.get('delta', 0))
    if is_buy:
        if 0.30 <= delta <= 0.65:
            score += 25
        elif 0.20 <= delta < 0.30 or 0.65 < delta <= 0.75:
            score += 18
        elif 0.15 <= delta < 0.20:
            score += 10
        else:
            score += 5
    else:
        if 0.10 <= delta <= 0.30:
            score += 25
        elif 0.30 < delta <= 0.40:
            score += 18
        else:
            score += 10

    # 2. ATR-PREMIUM RATIO (0-20)
    if atr > 0:
        ratio = premium / atr
        if is_buy:
            if 0.03 < ratio < 0.25:
                score += 20
            elif 0.25 <= ratio < 0.5:
                score += 12
            else:
                score += 5
        else:
            if ratio > 0.2:
                score += 20
            elif ratio > 0.1:
                score += 12
            else:
                score += 5

    # 3. VOLATILITY (0-20) — commodity uses historical vol
    hv = indicators.get('iv', 0.2) * 100  # as percentage
    if hv > 30:
        score += 20
    elif hv > 20:
        score += 15
    elif hv > 15:
        score += 10
    else:
        score += 5

    # 4. MOMENTUM (0-20)
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
        if abs(body) < atr * 0.2:
            score += 20
        elif abs(body) < atr * 0.5:
            score += 12
        else:
            score += 5

    # 5. TREND STRENGTH (0-15)
    pcr = indicators.get('pcr', 1.0)
    if is_buy:
        if (is_ce and pcr > 1.0) or (not is_ce and pcr < 1.0):
            score += 15
        elif 0.9 < pcr < 1.1:
            score += 8
        else:
            score += 3
    else:
        if 0.9 < pcr < 1.1:
            score += 15
        else:
            score += 8

    # v9.6: DATA QUALITY PENALTY — penalize when critical market data is missing
    data_quality = indicators.get('_data_quality', {})
    if not data_quality.get('has_iv', True):
        score -= 15  # No IV data = blind entry, heavy penalty
    if not data_quality.get('has_pcr', True):
        score -= 10  # No PCR = no sentiment confirmation
    if not data_quality.get('has_vwap', True):
        score -= 5   # Missing VWAP is less critical

    return max(min(score, 100), 0)


def mcx_compute_hold_score(pos, spot, indicators, current_oi=None, current_iv=None):
    """v7.7: Score 0-100 — how strongly signals support STAYING in this commodity trade.

    Factors:
      Signal alignment (0-30): Is spot still on our side of CPR pivot?
      Spot momentum    (0-25): Is spot moving in our trade's direction?
      Premium health   (0-20): Is premium growing or stable?
      OI trend         (0-15): Is OI accumulating (increasing)?
      IV stability     (0-10): Is IV stable (not spiking against us)?

    Thresholds:
      >= MCX_HOLD_SCORE_STRONG (60): Raise TSL, override TIME_EXIT/OI_SURGE
      40-59: Moderate hold, normal TSL behaviour
      < MCX_HOLD_SCORE_WEAK (40): Allow early exit on losers
    """
    score = 0
    is_bullish = 'CE' in pos.get('signal_type', '')

    # 1. Signal alignment (0-30): Is spot on our side of pivot?
    if indicators:
        pivot = indicators.get('pivot', spot)
        bc = indicators.get('bc', pivot)
        tc = indicators.get('tc', pivot)
        if is_bullish:
            if spot > pivot:
                score += 30
            elif spot > bc:
                score += 15
        else:
            if spot < pivot:
                score += 30
            elif spot < tc:
                score += 15

    # 2. Spot momentum (0-25): Is spot moving in our direction?
    entry_spot = pos.get('entry_spot', spot)
    atr = indicators.get('atr', 100) if indicators else 100
    spot_move = spot - entry_spot
    if atr > 0:
        normalized = abs(spot_move) / atr
        if (is_bullish and spot_move > 0) or (not is_bullish and spot_move < 0):
            score += min(25, int(normalized * 25))

    # 3. Premium health (0-20)
    entry_prem = pos.get('entry_premium', 0)
    current_prem = pos.get('current_premium', entry_prem)
    if entry_prem > 0:
        if pos.get('is_sell'):
            gain_pct = (entry_prem - current_prem) / entry_prem * 100
        else:
            gain_pct = (current_prem - entry_prem) / entry_prem * 100
        if gain_pct > 20:
            score += 20
        elif gain_pct > 10:
            score += 15
        elif gain_pct > 0:
            score += 10
        elif gain_pct > -5:
            score += 5

    # 4. OI trend (0-15)
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

    # 5. IV stability (0-10)
    entry_iv_val = pos.get('entry_iv', 0)
    iv_val = current_iv if current_iv is not None else pos.get('current_iv', entry_iv_val)
    if entry_iv_val and entry_iv_val > 0 and iv_val and iv_val > 0:
        iv_change = abs(iv_val - entry_iv_val) / entry_iv_val
        if iv_change < 0.10:
            score += 10
        elif iv_change < 0.20:
            score += 5

    return max(0, min(100, score))


# ====================================================================
# PORTFOLIO TRACKER
# ====================================================================
class CommodityPortfolio:
    """Track commodity paper positions."""

    def __init__(self, capital=INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.positions = []
        self.closed_trades = []
        self.daily_pnl = {}
        self._load_state()

    def _state_file(self):
        return os.path.join(PAPER_DIR, 'commodity_portfolio_state.json')

    def _load_state(self):
        sf = self._state_file()
        if os.path.exists(sf):
            try:
                with open(sf, 'r') as f:
                    state = json.load(f)
                self.capital = state.get('capital', self.initial_capital)
                self.positions = state.get('positions', [])
                self.closed_trades = state.get('closed_trades', [])
                self.daily_pnl = state.get('daily_pnl', {})
                logger.info(f"Loaded state: Capital Rs {self.capital:,.0f}, "
                           f"{len(self.positions)} open, {len(self.closed_trades)} closed")
            except Exception as e:
                logger.error(f"Error loading state: {e}")

    def save_state(self):
        state = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat()
        }
        with open(self._state_file(), 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def add_signal(self, strategy, commodity, signal_type, strike, entry_premium,
                   greeks, dte, details=None, oi=0, iv=0):
        spec = COMMODITIES[commodity]
        base_lot = spec['lot_size']
        mult = spec['multiplier']
        is_sell = 'SELL' in signal_type

        # ---- RISK CHECKS: Only trade with leftover capital ----

        # Compute available capital first (needed for dynamic lot sizing)
        total_locked = 0
        for p in self.positions:
            p_spec = COMMODITIES[p['commodity']]
            if p['is_sell']:
                total_locked += p_spec['margin'] * p.get('num_lots', 1)
            else:
                total_locked += p['entry_premium'] * p['lot_size'] * p['multiplier']
        available_capital = self.capital - total_locked

        # v2.5.3: Tiered lot sizing — scale lots by signal quality + available capital
        quality_score = (details or {}).get('quality_score', 0) if details else 0
        cost_per_lot = spec['margin'] if is_sell else entry_premium * base_lot * mult

        # Drawdown check
        hwm = max(self.initial_capital, self.capital)
        dd_pct = (hwm - self.capital) / hwm * 100

        # Determine number of lots using tiered allocation
        max_lots_cap = MCX_MAX_LOTS.get(commodity, 3)
        if dd_pct > 5 or cost_per_lot <= 0:
            num_lots = 1
            tier_name = 'DD_CAP' if dd_pct > 5 else 'MIN'
        elif quality_score >= MCX_LOT_TIER_ELITE:
            capital_alloc = available_capital * 0.30  # Elite: 30%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'ELITE'
        elif quality_score >= MCX_LOT_TIER_STRONG:
            capital_alloc = available_capital * 0.20  # Strong: 20%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'STRONG'
        else:
            capital_alloc = available_capital * 0.10  # Standard: 10%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'STANDARD'

        logger.info(f"  MCX_LOT_TIER: {commodity} {strategy} score={quality_score} tier={tier_name} "
                    f"lots={num_lots}/{max_lots_cap} cost/lot=Rs {cost_per_lot:,.0f} "
                    f"avail=Rs {available_capital:,.0f} DD={dd_pct:.1f}%")

        lot = base_lot * num_lots

        # Capital required for this trade
        if is_sell:
            trade_capital = spec['margin'] * num_lots
        else:
            trade_capital = entry_premium * lot * mult

        # Check 1: Per-trade limit (25% of commodity capital for BUY; margin check for SELL)
        if not is_sell and trade_capital > MAX_PER_TRADE * num_lots:
            logger.warning(f"  RISK_LIMIT: {commodity} {strategy} trade capital Rs {trade_capital:,.0f} "
                          f"> max Rs {MAX_PER_TRADE * num_lots:,.0f}. SKIPPED.")
            return None

        # Check 2: Available capital
        if trade_capital > available_capital:
            # Try with fewer lots
            if num_lots > 1 and cost_per_lot > 0:
                num_lots = max(1, int(available_capital // cost_per_lot))
                lot = base_lot * num_lots
                trade_capital = cost_per_lot * num_lots
            if trade_capital > available_capital:
                logger.warning(f"  RISK_LIMIT: {commodity} {strategy} needs Rs {trade_capital:,.0f} "
                              f"but only Rs {available_capital:,.0f} available. SKIPPED.")
                return None

        # Check 3: Daily loss limit (10% of commodity capital)
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"  RISK_LIMIT: Daily loss Rs {daily_loss:,.0f} exceeds "
                          f"limit Rs {MAX_DAILY_LOSS:,.0f}. SKIPPED.")
            return None

        # Check 4: Max positions per commodity (prevent cascade)
        same_commodity = [p for p in self.positions if p['commodity'] == commodity]
        if len(same_commodity) >= MAX_POSITIONS_PER_COMMODITY:
            logger.warning(f"  RISK_LIMIT: Max {MAX_POSITIONS_PER_COMMODITY} positions "
                          f"for {commodity} reached. SKIPPED.")
            return None

        # Check 5: Drawdown-based position scaling (for single-lot trades too)
        if dd_pct > 5:
            scale = max(0.5, 1.0 - (dd_pct - 5) * 0.05)
            scaled_max = MAX_PER_TRADE * scale
            if trade_capital > scaled_max:
                logger.warning(f"  RISK_SCALE: DD={dd_pct:.1f}%, trade Rs {trade_capital:,.0f} > "
                              f"scaled Rs {scaled_max:,.0f}. SKIPPED.")
                return None

        # ---- END RISK CHECKS ----

        cost = calc_mcx_costs(entry_premium, lot, mult, is_sell)

        pos = {
            'id': f"{strategy}_{commodity}_{datetime.now().strftime('%H%M%S')}",
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'commodity': commodity,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': round(entry_premium, 2),
            'current_premium': round(entry_premium, 2),
            'lot_size': lot,
            'num_lots': num_lots,  # v2.4: How many exchange lots
            'multiplier': mult,
            'is_sell': is_sell,
            'entry_cost': round(cost, 2),
            'delta': round(greeks['delta'], 4),
            'gamma': round(greeks['gamma'], 6),
            'theta': round(greeks['theta'], 2),
            'dte': dte,
            'unrealized_pnl': 0,
            'status': 'OPEN',
            'details': details or {},
            'entry_spot': details.get('spot', 0) if details else 0,
            # OI/IV tracking for dynamic exits
            'entry_oi': oi,
            'entry_iv': round((greeks.get('iv') or iv or 0) * 100, 1),
            # Trailing Stop Loss tracking
            'peak_premium': round(entry_premium, 2),
            'trough_premium': round(entry_premium, 2),
            'trailing_sl': None,
            'breakeven_locked': False,
            'capital_available': round(available_capital - trade_capital, 2),  # v2.4
        }
        self.positions.append(pos)

        if num_lots > 1:
            logger.info(f"  MCX_MULTI_LOT: {num_lots} lots ({lot} qty) for {commodity} "
                       f"| Score={quality_score} | Available Rs {available_capital:,.0f}")
        logger.info(f"  PAPER TRADE: {signal_type} {commodity} {strike} "
                    f"@ Rs {entry_premium:.2f} x{num_lots}lot | {strategy} | Delta: {greeks['delta']:.3f}")
        self.save_state()
        return pos

    def close_position(self, pos_id, exit_premium, reason=''):
        for i, pos in enumerate(self.positions):
            if pos['id'] == pos_id:
                exit_cost = calc_mcx_costs(exit_premium, pos['lot_size'],
                                           pos['multiplier'], not pos['is_sell'])
                if pos['is_sell']:
                    pnl = (pos['entry_premium'] - exit_premium) * pos['lot_size'] * pos['multiplier']
                else:
                    pnl = (exit_premium - pos['entry_premium']) * pos['lot_size'] * pos['multiplier']
                pnl -= (pos['entry_cost'] + exit_cost)

                trade = {**pos, 'exit_premium': round(exit_premium, 2),
                         'pnl': round(pnl, 2), 'exit_reason': reason,
                         'exit_time': datetime.now().isoformat(), 'status': 'CLOSED'}
                self.closed_trades.append(trade)
                self.positions.pop(i)
                today = datetime.now().strftime('%Y-%m-%d')
                self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl
                self.capital += pnl
                trade['capital_after'] = round(self.capital, 2)  # v2.4
                logger.info(f"  CLOSED: {pos['signal_type']} {pos['commodity']} "
                           f"| PnL: Rs {pnl:,.2f} | {reason} | Capital: Rs {self.capital:,.0f}")
                self.save_state()
                return trade
        return None

    def print_status(self):
        total_unrealized = sum(p['unrealized_pnl'] for p in self.positions)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        total_realized = sum(t['pnl'] for t in self.closed_trades)

        print("\n" + "=" * 70)
        print("COMMODITY PAPER TRADING STATUS")
        print("=" * 70)
        print(f"  Capital:         Rs {self.capital:,.0f}")
        print(f"  Return:          {(self.capital-self.initial_capital)/self.initial_capital*100:.2f}%")
        print(f"  Open Positions:  {len(self.positions)}")
        print(f"  Closed Trades:   {len(self.closed_trades)}")
        print(f"  Win Rate:        {len(wins)/len(self.closed_trades)*100:.1f}%" if self.closed_trades else "  Win Rate:        N/A")
        print(f"  Realized PnL:    Rs {total_realized:,.0f}")
        print(f"  Unrealized PnL:  Rs {total_unrealized:,.0f}")

        if self.positions:
            print("\n  OPEN POSITIONS:")
            for p in self.positions:
                print(f"    {p['strategy']:12s} | {p['signal_type']:12s} | "
                      f"{p['commodity']:12s} | Strike: {p['strike']:>10.0f} | "
                      f"Entry: {p['entry_premium']:>8.2f} | PnL: Rs {p['unrealized_pnl']:>8.2f}")

        if self.closed_trades:
            recent = self.closed_trades[-5:]
            print(f"\n  RECENT TRADES:")
            for t in recent:
                print(f"    {t['strategy']:12s} | {t['commodity']:12s} | "
                      f"PnL: Rs {t['pnl']:>10.2f} | {t['exit_reason']}")
        print("=" * 70)


# ====================================================================
# STRATEGY ENGINE FOR COMMODITIES
# ====================================================================
class CommodityStrategyEngine:
    """Generate signals from strategies adapted for commodities."""

    def __init__(self, portfolio, angel=None):
        self.portfolio = portfolio
        self.angel = angel
        self.historical_data = {}

    def load_historical(self, commodity):
        """Load historical data for commodity. Auto-download if missing/stale."""
        spec = COMMODITIES[commodity]
        fpath = os.path.join(DATA_DIR, spec['file'])

        # v9.6d: Check if file needs downloading (missing or stale >1 day)
        # Changed from 7 days to 1 day — CPR needs yesterday's OHLC to be accurate
        needs_download = False
        if os.path.exists(fpath):
            mod_time = datetime.fromtimestamp(os.path.getmtime(fpath))
            age_days = (datetime.now() - mod_time).days
            if age_days >= 1:
                needs_download = True
                logger.info(f"Historical data for {commodity} is stale ({age_days}d old), refreshing...")
        else:
            needs_download = True
            logger.info(f"Historical data for {commodity} not found, downloading...")

        if needs_download and self.angel and self.angel._connected:
            self._download_historical_commodity(commodity, fpath)

        if os.path.exists(fpath):
            df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            self.historical_data[commodity] = df
            logger.info(f"Loaded {len(df)} days for {commodity}")
            return df

        logger.error(f"No historical data found for {commodity} — indicators will not work")
        return None

    def _download_historical_commodity(self, commodity, save_path):
        """Download daily OHLC data from Angel SmartAPI for MCX commodity.
        v2.5.2: If current-month futures token has no data (just rolled),
        try next-month token from instrument master as fallback.
        """
        try:
            token = self.angel._futures_tokens.get(commodity)
            if not token:
                logger.warning(f"No futures token for {commodity}, skipping download")
                return

            # v2.5.2: Collect all futures tokens for this commodity (current + next months)
            tokens_to_try = [token]
            try:
                master_path = os.path.join(DATA_DIR, 'instrument_master.csv')
                if os.path.exists(master_path):
                    import csv
                    with open(master_path, 'r') as f:
                        reader = csv.reader(f)
                        for row in reader:
                            if len(row) >= 7 and row[1].startswith(commodity) and 'FUT' in row[1] and row[6] == 'FUTCOM':
                                t = row[0]
                                if t not in tokens_to_try:
                                    tokens_to_try.append(t)
            except Exception:
                pass

            all_data = []
            for try_token in tokens_to_try:
                logger.info(f"Downloading historical data for {commodity} (token={try_token})...")
                chunk_start = datetime.now() - timedelta(days=2000)
                while chunk_start < datetime.now():
                    chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
                    data = self.angel.get_historical(
                        'MCX', try_token, 'ONE_DAY',
                        chunk_start.strftime('%Y-%m-%d 09:00'),
                        chunk_end.strftime('%Y-%m-%d 23:30')
                    )
                    if data:
                        all_data.extend(data)
                    chunk_start = chunk_end + timedelta(days=1)
                    time.sleep(2)

                if all_data:
                    logger.info(f"Got {len(all_data)} rows from token {try_token}")
                    break  # Found data, stop trying other tokens
                else:
                    logger.info(f"Token {try_token} returned no data, trying next...")

            if all_data:
                df = pd.DataFrame(all_data, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                df.to_csv(save_path, index=False)
                logger.info(f"Downloaded {len(df)} days for {commodity} → {save_path}")
            else:
                logger.warning(f"No data returned from Angel API for {commodity} (tried {len(tokens_to_try)} tokens)")
        except Exception as e:
            logger.error(f"Download failed for {commodity}: {e}")

    def compute_indicators(self, commodity, current_ohlc=None):
        df = self.historical_data.get(commodity)
        if df is None:
            df = self.load_historical(commodity)
        if df is None:
            return None

        spec = COMMODITIES[commodity]

        if current_ohlc:
            today = pd.Timestamp.now().normalize()
            new_row = pd.DataFrame({
                'Open': [current_ohlc['open']], 'High': [current_ohlc['high']],
                'Low': [current_ohlc['low']], 'Close': [current_ohlc['close']],
                'Volume': [current_ohlc.get('volume', 0)],
            }, index=[today])
            if today in df.index:
                df.loc[today] = new_row.iloc[0]
            else:
                df = pd.concat([df, new_row])

        atr = (df['High'].tail(14) - df['Low'].tail(14)).mean()
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        hv = log_ret.tail(20).std() * np.sqrt(252)
        iv = max(min(hv * 1.15 * spec['vol_adj'], 0.80), 0.08)

        prev = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / prev['Close'] * 100

        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        recent_lows = df['Low'].tail(10)
        recent_highs = df['High'].tail(10)
        demand_zone = recent_lows.min()
        supply_zone = recent_highs.max()
        demand_strength = ((recent_lows - demand_zone).abs() < atr * 0.5).sum()
        supply_strength = ((supply_zone - recent_highs).abs() < atr * 0.5).sum()

        prev_range = (prev['High'] - prev['Low']) / max(atr, 1)

        # VWAP calculation (for PCR+VWAP strategy)
        if 'Volume' in df.columns and df['Volume'].tail(5).sum() > 0:
            tp = (df['High'] + df['Low'] + df['Close']) / 3
            vwap = (tp * df['Volume']).tail(5).sum() / df['Volume'].tail(5).sum()
        else:
            vwap = (df['High'].tail(5) + df['Low'].tail(5) + df['Close'].tail(5)).mean() / 3

        # PCR proxy from recent momentum direction
        pcr_proxy = 1 + (log_ret.tail(5).mean() * 10 if len(log_ret) > 5 else 0)
        pcr_proxy = max(0.5, min(2.0, pcr_proxy))

        # Support/Resistance for Survivor strategy
        resistance = prev['High']
        support = prev['Low']

        return {
            'atr': atr, 'iv': iv, 'hv': hv,
            'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_width,
            'cam_r3': cam_r3, 'cam_r4': cam_r4, 'cam_s3': cam_s3, 'cam_s4': cam_s4,
            'demand_zone': demand_zone, 'supply_zone': supply_zone,
            'demand_strength': demand_strength, 'supply_strength': supply_strength,
            'prev_range': prev_range,
            'vwap': vwap, 'pcr': pcr_proxy,
            'resistance': resistance, 'support': support,
        }

    def check_cpr_signals(self, commodity, spot, ohlc, ind, dow, dte):
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        T = dte / 365
        cpr_w = ind['cpr_width']

        # v9.6: Multi-tier CPR targets (ported from equity)
        # Narrow (<0.3%): strong breakout → highest targets
        # Moderate (0.3-0.6%): directional trade → standard targets
        # Wide (>0.6%): mean reversion → SELL only (conservative targets)
        if cpr_w < 0.3:
            cpr_label = "Narrow"
            target_hit_mult = 1.5    # 50% gain (Camarilla confirmed)
            target_base_mult = 1.3   # 30% gain (no Camarilla confirmation)
            sl_mult = 0.5
        elif cpr_w <= 0.6:
            cpr_label = "Moderate"
            target_hit_mult = 1.4    # 40% gain
            target_base_mult = 1.25  # 25% gain
            sl_mult = 0.5
        else:
            cpr_label = "Wide"
            target_hit_mult = 1.3    # 30% gain
            target_base_mult = 1.2   # 20% gain
            sl_mult = 0.5

        # BUY breakout for Narrow + Moderate CPR (cpr_w <= 0.6)
        if cpr_w <= 0.6:
            if spot > ind['tc']:
                # v9.5: Greeks-based strike selection (best delta + highest gamma)
                opt = select_optimal_mcx_strike(spot, strike_int, T, RISK_FREE_RATE, iv, 'CE', MCX_MIN_PREMIUM_BUY)
                ce_strike = opt['strike']
                g = opt['greeks']
                if g['price'] > MCX_MIN_PREMIUM_BUY:
                    # v9.6: Camarilla confirmation — higher target if R3 already breached
                    use_target = target_hit_mult if ohlc['high'] > ind['cam_r3'] else target_base_mult
                    logger.info(f"  GREEKS_STRIKE: {commodity} CE {cpr_label} CPR ATM={round(spot/strike_int)*strike_int} "
                               f"Selected={ce_strike} delta={g['delta']:.3f} gamma={g['gamma']:.6f}")
                    signals.append({
                        'type': 'BUY_CE_CPR', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"{cpr_label} CPR ({cpr_w:.3f}%) bullish breakout",
                        'target': g['price'] * use_target, 'sl': g['price'] * sl_mult,
                    })
            elif spot < ind['bc']:
                # v9.5: Greeks-based strike selection (best delta + highest gamma)
                opt = select_optimal_mcx_strike(spot, strike_int, T, RISK_FREE_RATE, iv, 'PE', MCX_MIN_PREMIUM_BUY)
                pe_strike = opt['strike']
                g = opt['greeks']
                if g['price'] > MCX_MIN_PREMIUM_BUY:
                    # v9.6: Camarilla confirmation — higher target if S3 already breached
                    use_target = target_hit_mult if ohlc['low'] < ind['cam_s3'] else target_base_mult
                    logger.info(f"  GREEKS_STRIKE: {commodity} PE {cpr_label} CPR ATM={round(spot/strike_int)*strike_int} "
                               f"Selected={pe_strike} delta={g['delta']:.3f} gamma={g['gamma']:.6f}")
                    signals.append({
                        'type': 'BUY_PE_CPR', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"{cpr_label} CPR ({cpr_w:.3f}%) bearish breakout",
                        'target': g['price'] * use_target, 'sl': g['price'] * sl_mult,
                    })

        # SELL mean reversion: Wide CPR only (> 0.6%) — unchanged from v9.5
        if cpr_w > 0.6:
            margin_ok = self.portfolio.capital >= spec['margin']
            if ohlc['high'] >= ind['cam_r3'] * 0.998 and spot < ind['cam_r4'] and margin_ok:
                ce_strike = round(ind['cam_r4'] / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                if g['price'] > MCX_MIN_PREMIUM_SELL:
                    signals.append({
                        'type': 'SELL_CE_CPR', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at R3",
                        'target': g['price'] * 0.3, 'sl': g['price'] * 1.2,  # v2.5: Was 0.15/1.8
                    })
            if ohlc['low'] <= ind['cam_s3'] * 1.002 and spot > ind['cam_s4'] and margin_ok:
                pe_strike = round(ind['cam_s4'] / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                if g['price'] > MCX_MIN_PREMIUM_SELL:
                    signals.append({
                        'type': 'SELL_PE_CPR', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at S3",
                        'target': g['price'] * 0.3, 'sl': g['price'] * 1.2,  # v2.5: Was 0.15/1.8
                    })
        return signals

    def check_gamma_blast_signals(self, commodity, spot, ohlc, ind, dow, dte):
        """Gamma Blast — fires on ALL trading days with DTE-aware parameters.
        v9.6: IV multiplier and targets scale based on DTE (MCX monthly expiry).
        """
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        atr = ind['atr']

        # v9.6: DTE-aware parameter scaling (runs ALL days, adjusts targets/IV)
        # MCX has monthly expiry — DTE ranges 5-15 (from formula max(5, 15-(day%28)))
        actual_dte = max(1, dte)
        T = actual_dte / 365

        # IV multiplier: higher when closer to expiry (gamma spike)
        if actual_dte <= 3:
            iv_mult = 1.3      # Near expiry: gamma spike
            target_mult = 1.6  # 60% gain target
            sl_mult = 0.4      # Wider SL for gamma moves
        elif actual_dte <= 7:
            iv_mult = 1.2
            target_mult = 1.5  # 50% gain
            sl_mult = 0.5
        else:
            iv_mult = max(1.0, 1.3 - actual_dte * 0.02)   # Gradually decrease
            target_mult = max(1.3, 1.5 - actual_dte * 0.01)  # 30-50% gain
            sl_mult = 0.5

        if ind['prev_range'] > 1.2:
            return signals

        day_range = (ohlc['high'] - ohlc['low']) / max(atr, 1)
        if day_range < 0.5:
            return signals

        body = spot - ohlc['open']
        if abs(body) > atr * 0.2:
            if body > 0:
                # v9.5: Greeks-based strike selection (best delta + highest gamma)
                opt = select_optimal_mcx_strike(spot, strike_int, T, RISK_FREE_RATE, iv * iv_mult, 'CE', MCX_MIN_PREMIUM_BUY)
                ce_strike = opt['strike']
                g = opt['greeks']
                if g['price'] > MCX_MIN_PREMIUM_BUY:
                    logger.info(f"  GREEKS_STRIKE: {commodity} CE(Gamma) ATM={round(spot/strike_int)*strike_int} "
                               f"Selected={ce_strike} delta={g['delta']:.3f} gamma={g['gamma']:.6f} "
                               f"DTE={actual_dte} iv_mult={iv_mult:.2f}")
                    signals.append({
                        'type': 'BUY_CE_GAMMA', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Gamma Blast: Up breakout body={body:.1f} DTE={actual_dte}",
                        'target': g['price'] * target_mult, 'sl': g['price'] * sl_mult,
                    })
            else:
                # v9.5: Greeks-based strike selection (best delta + highest gamma)
                opt = select_optimal_mcx_strike(spot, strike_int, T, RISK_FREE_RATE, iv * iv_mult, 'PE', MCX_MIN_PREMIUM_BUY)
                pe_strike = opt['strike']
                g = opt['greeks']
                if g['price'] > MCX_MIN_PREMIUM_BUY:
                    logger.info(f"  GREEKS_STRIKE: {commodity} PE(Gamma) ATM={round(spot/strike_int)*strike_int} "
                               f"Selected={pe_strike} delta={g['delta']:.3f} gamma={g['gamma']:.6f} "
                               f"DTE={actual_dte} iv_mult={iv_mult:.2f}")
                    signals.append({
                        'type': 'BUY_PE_GAMMA', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Gamma Blast: Down breakout body={body:.1f} DTE={actual_dte}",
                        'target': g['price'] * target_mult, 'sl': g['price'] * sl_mult,
                    })
        return signals

    def check_ghost_zone_signals(self, commodity, spot, ohlc, ind, dow, dte):
        """v2.5: DISABLED — weak zone detection causing consistent losses on backtest."""
        return []  # v2.5: Disabled pending redesign
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        atr = ind['atr']
        T = dte / 365

        if ohlc['low'] <= ind['demand_zone'] * 1.01 and spot > ind['demand_zone'] and ind['demand_strength'] >= 2:
            ce_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
            if g['price'] > MCX_MIN_PREMIUM_BUY:
                bounce = (spot - ohlc['low']) / max(atr, 1)
                signals.append({
                    'type': 'BUY_CE_GTZ', 'strike': ce_strike,
                    'premium': g['price'], 'greeks': g,
                    'reason': f"Ghost Zone: Demand retest bounce={bounce:.2f}x",
                    'target': g['price'] * 2.5 if bounce > 0.8 else g['price'] * 1.8,
                    'sl': g['price'] * 0.4,
                })

        elif ohlc['high'] >= ind['supply_zone'] * 0.99 and spot < ind['supply_zone'] and ind['supply_strength'] >= 2:
            pe_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
            if g['price'] > MCX_MIN_PREMIUM_BUY:
                signals.append({
                    'type': 'BUY_PE_GTZ', 'strike': pe_strike,
                    'premium': g['price'], 'greeks': g,
                    'reason': f"Ghost Zone: Supply retest",
                    'target': g['price'] * 2.5, 'sl': g['price'] * 0.4,
                })
        return signals

    def check_pcr_vwap_signals(self, commodity, spot, ohlc, indicators, dow, dte):
        """PCR+VWAP Strategy adapted for commodities.
        v2.5: DISABLED — zero signals ever generated. Proxy PCR stays between 0.95-1.05.
        """
        return []  # v2.5: Disabled
        signals = []
        ind = indicators
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        T = dte / 365

        # Skip in high IV environment
        if ind['iv'] > 0.50:
            return signals

        tolerance = max(ind['atr'] * 0.1, spot * 0.003)
        vwap = ind.get('vwap', spot)
        pcr = ind.get('pcr', 1.0)

        # BUY CE: PCR > 1.05 (bullish momentum), near VWAP
        if pcr > 1.05 and abs(spot - vwap) < tolerance * 2 and spot >= vwap * 0.995:
            ce_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > MCX_MIN_PREMIUM_BUY and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_CE_PCRVWAP',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bullish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.5,
                    'sl': g['price'] * 0.4,
                })

        # BUY PE: PCR < 0.95 (bearish momentum), near VWAP
        elif pcr < 0.95 and abs(spot - vwap) < tolerance * 2 and spot <= vwap * 1.005:
            pe_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > MCX_MIN_PREMIUM_BUY and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_PE_PCRVWAP',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bearish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.5,
                    'sl': g['price'] * 0.4,
                })

        return signals

    def check_survivor_signals(self, commodity, spot, ohlc, indicators, dow, dte):
        """Survivor V2 option selling adapted for commodities.
        Sells OTM options after breakout/breakdown from support/resistance.
        Uses DTE-based filter instead of day-of-week (MCX has monthly expiry).
        v2.5: SILVERM disabled — 0% WR, premium never moves (6/6 trades zero movement on Feb 27).
        """
        signals = []

        # v2.5: Disable SILVERM — 0% WR, premium never moves
        if commodity == 'SILVERM':
            return signals

        ind = indicators
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        T = dte / 365
        atr = ind['atr']

        # Skip if too close to expiry (< 3 DTE) or too far (> 5 DTE — negligible theta)
        if dte < 3 or dte > 5:
            return signals

        # Check margin availability
        margin_ok = self.portfolio.capital >= spec['margin']
        if not margin_ok:
            return signals

        # DTE-based distance scaling (wider for more DTE)
        if dte >= 15:
            gap = max(atr * 0.4, spot * 0.005)
            distance = max(atr * 0.8, spot * 0.01)
        elif dte >= 10:
            gap = max(atr * 0.3, spot * 0.004)
            distance = max(atr * 0.7, spot * 0.008)
        elif dte >= 5:
            gap = max(atr * 0.2, spot * 0.003)
            distance = max(atr * 0.5, spot * 0.006)
        else:
            gap = max(atr * 0.15, spot * 0.002)
            distance = max(atr * 0.4, spot * 0.004)

        resistance = ind.get('resistance', ind.get('supply_zone', spot + atr))
        support = ind.get('support', ind.get('demand_zone', spot - atr))

        # PE SELLING — price breaks above resistance (bullish → sell OTM PEs)
        if ohlc['high'] > resistance + gap:
            pe_strike = round((spot - distance) / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > MCX_MIN_PREMIUM_SELL:
                signals.append({
                    'type': 'SELL_PE_SURV',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: PE sell at {pe_strike:.0f} dist={distance:.0f} "
                              f"gap={gap:.0f} res={resistance:.0f}",
                    'target': g['price'] * 0.3,
                    'sl': g['price'] * 0.8,
                })

        # CE SELLING — price breaks below support (bearish → sell OTM CEs)
        if ohlc['low'] < support - gap:
            ce_strike = round((spot + distance) / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > MCX_MIN_PREMIUM_SELL:
                signals.append({
                    'type': 'SELL_CE_SURV',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: CE sell at {ce_strike:.0f} dist={distance:.0f} "
                              f"gap={gap:.0f} sup={support:.0f}",
                    'target': g['price'] * 0.3,
                    'sl': g['price'] * 0.8,
                })

        return signals


# ====================================================================
# ANGEL API CONNECTION FOR MCX
# ====================================================================
class AngelMCXConnection:
    """Angel One SmartAPI for MCX data."""

    def __init__(self):
        self.obj = None
        self._connected = False
        self.instruments = None
        # MCX futures tokens for spot proxy
        self._futures_tokens = {}
        self._last_api_call = 0  # Timestamp of last REST API call
        self._api_min_interval = 1.0  # v7.7: Minimum 1s between REST calls (was 0.5s, caused AB1004)
        self._backoff_until = 0  # v7.7: Exponential backoff timestamp
        self._auth_time = 0  # v9.2: Track when we last authenticated
        self._reconnecting = False  # v9.2: Prevent recursive reconnect loops
        self._creds_cache = None  # v9.2: Cache credentials for reconnect

    def _throttle(self):
        """Enforce minimum interval between REST API calls to avoid rate limiting."""
        now = time.time()
        if now < self._backoff_until:
            wait = self._backoff_until - now
            logger.debug(f"MCX API backoff: waiting {wait:.1f}s")
            time.sleep(wait)
        elapsed = time.time() - self._last_api_call
        if elapsed < self._api_min_interval:
            time.sleep(self._api_min_interval - elapsed)
        self._last_api_call = time.time()

    def _handle_rate_limit(self, error_msg=''):
        """v7.7: Exponential backoff on rate limit (AB1004/TooManyRequests)."""
        backoff = min(30, max(2, (self._backoff_until - time.time()) * 2 + 2))
        self._backoff_until = time.time() + backoff
        logger.warning(f"MCX API RATE_LIMIT: backing off {backoff:.0f}s — {error_msg}")
        try:
            from trade_notifier import notify_api_rate_limit
            notify_api_rate_limit('COMMODITY', 'REST API', error_msg)
        except Exception:
            pass

    def _is_token_expired(self, data):
        """v9.2: Check if API response indicates expired/invalid token."""
        if not data or not isinstance(data, dict):
            return False
        err_code = str(data.get('errorcode', ''))
        err_msg = str(data.get('message', '')).lower()
        return err_code in ('AG8001', 'AG8002', 'AB8050') or 'invalid token' in err_msg or 'token expired' in err_msg

    def reconnect(self):
        """v9.2: Re-authenticate to Angel MCX API when token expires."""
        if self._reconnecting:
            return False
        self._reconnecting = True
        logger.warning("MCX TOKEN_EXPIRED: Attempting re-authentication...")
        try:
            from trade_notifier import notify_system_event
            notify_system_event('COMMODITY', 'Angel MCX API token expired — reconnecting...')
        except Exception:
            pass
        try:
            self._connected = False
            result = self.connect()
            if result:
                logger.info("MCX TOKEN_REFRESH: Re-authentication successful")
                try:
                    from trade_notifier import notify_system_event
                    notify_system_event('COMMODITY', 'Angel MCX API reconnected successfully')
                except Exception:
                    pass
            else:
                logger.error("MCX TOKEN_REFRESH: Re-authentication FAILED")
            return result
        finally:
            self._reconnecting = False

    def check_token_health(self):
        """v9.2: Proactive token health check — call from heartbeat."""
        if not self._connected:
            return False
        token_age_hours = (time.time() - self._auth_time) / 3600 if self._auth_time else 0
        if token_age_hours > 5:
            logger.info(f"MCX TOKEN_HEALTH: Token age {token_age_hours:.1f}h > 5h, proactive refresh...")
            return self.reconnect()
        try:
            self._throttle()
            # Ping with a known MCX token (GOLDM futures)
            if self._futures_tokens:
                first_token = next(iter(self._futures_tokens.values()))
                data = self.obj.ltpData('MCX', first_token, first_token)
                if self._is_token_expired(data):
                    logger.warning(f"MCX TOKEN_HEALTH: Token invalid, reconnecting...")
                    return self.reconnect()
            return True
        except Exception as e:
            logger.warning(f"MCX TOKEN_HEALTH: Ping failed ({e}), reconnecting...")
            return self.reconnect()

    def connect(self):
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError:
            os.system("pip install smartapi-python pyotp logzero websocket-client")
            from SmartApi import SmartConnect
            import pyotp

        creds = {}; current_app = {}
        with open(ANGEL_CRED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip(); raw_val = val.strip()
                    if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                        parts = raw_val.split('#')
                        val = parts[-1].strip() if 'your_' in parts[0].strip().lower() else parts[0].strip()
                    else:
                        val = raw_val.split('#')[0].strip()
                    current_app[key] = val
                    if key == 'BROKER_NAME':
                        creds[current_app.get('ANGEL_APP_TYPE', 'Unknown')] = current_app.copy()
                        current_app = {}

        app = creds.get('Historical', creds.get('Market', {}))

        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to Angel MCX... attempt {attempt+1}/{max_retries}")
                self.obj = SmartConnect(api_key=app['ANGEL_API_KEY'])
                totp = pyotp.TOTP(app['ANGEL_TOTP_KEY']).now()
                session = self.obj.generateSession(app['ANGEL_CLIENT_CODE'], app['ANGEL_PIN'], totp)

                if session and session.get('status'):
                    self._connected = True
                    self._auth_time = time.time()  # v9.2: Track auth time
                    logger.info(f"Angel MCX connected")
                    self._load_mcx_tokens()
                    return True
                else:
                    logger.warning(f"Angel MCX login failed (attempt {attempt+1}): {session}")
            except Exception as e:
                logger.warning(f"Angel MCX connection error (attempt {attempt+1}): {e}")

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s, 32s
                logger.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"Angel MCX connection failed after {max_retries} attempts")
        return False

    def _load_mcx_tokens(self):
        """Find nearest futures tokens for each commodity."""
        cache = os.path.join(DATA_DIR, 'instrument_master.csv')
        if os.path.exists(cache):
            df = pd.read_csv(cache, low_memory=False)
            mcx = df[df['exch_seg'] == 'MCX']
            for comm in COMMODITIES:
                futs = mcx[(mcx['name'] == comm) & (mcx['instrumenttype'] == 'FUTCOM')]
                if len(futs) > 0:
                    futs_sorted = futs.sort_values('expiry')
                    self._futures_tokens[comm] = str(futs_sorted.iloc[0]['token'])

    def get_historical(self, exchange, token, interval, from_date, to_date):
        """Get historical candle data from Angel SmartAPI."""
        if not self._connected:
            return None
        try:
            self._throttle()
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date
            }
            data = self.obj.getCandleData(params)
            if data and data.get('data'):
                return data['data']
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"MCX Historical: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getCandleData(params)
                    if data and data.get('data'):
                        return data['data']
            if data and 'AB1004' in str(data.get('errorcode', '')):
                self._handle_rate_limit(str(data.get('message', '')))
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'rate' in err_str.lower() or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"MCX Historical error: {e}")
        return None

    def get_ltp(self, commodity):
        if not self._connected or commodity not in self._futures_tokens:
            return None
        try:
            self._throttle()
            data = self.obj.ltpData('MCX', self._futures_tokens[commodity],
                                    self._futures_tokens[commodity])
            if data and data.get('data'):
                return data['data'].get('ltp')
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"MCX LTP: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.ltpData('MCX', self._futures_tokens[commodity],
                                            self._futures_tokens[commodity])
                    if data and data.get('data'):
                        return data['data'].get('ltp')
        except Exception as e:
            logger.error(f"LTP error {commodity}: {e}")
        return None

    def get_market_data(self, exchange, symbol_token):
        """Get full market data including OI for MCX option."""
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.getMarketData("FULL", {exchange: [str(symbol_token)]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"MCX MarketData: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getMarketData("FULL", {exchange: [str(symbol_token)]})
                    if data and data.get('data') and data['data'].get('fetched'):
                        return data['data']['fetched'][0]
        except Exception as e:
            logger.error(f"MCX Market data error: {e}")
        return None

    def find_option_tokens(self, commodity, expiry, strike, opt_type):
        """Find MCX option contract token from instrument master.
        Filters by nearest future expiry to avoid matching wrong/stale contracts.
        """
        cache = os.path.join(DATA_DIR, 'instrument_master.csv')
        if not os.path.exists(cache):
            return None
        try:
            df = pd.read_csv(cache, low_memory=False)
            # MCX options store strikes as strike*100 in some formats
            mask = (df['name'] == commodity) & \
                   (df['exch_seg'] == 'MCX') & \
                   (df['instrumenttype'] == 'OPTFUT')
            if opt_type:
                mask = mask & (df['symbol'].str.endswith(opt_type))
            matches = df[mask]
            if len(matches) == 0:
                return None

            # Filter by nearest future expiry to avoid stale contracts
            today = datetime.now().date()
            matches = matches.copy()
            matches['expiry_date'] = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True)
            matches = matches[matches['expiry_date'].dt.date >= today]
            if len(matches) == 0:
                return None
            nearest_expiry = matches['expiry_date'].min()
            matches = matches[matches['expiry_date'] == nearest_expiry]

            # Try matching strike (MCX may store as strike*100)
            exact = matches[matches['strike'].astype(float) == float(strike * 100)]
            if len(exact) == 0:
                exact = matches[matches['strike'].astype(float) == float(strike)]
            if len(exact) > 0:
                return exact.iloc[0].to_dict()
        except Exception as e:
            logger.error(f"MCX option token lookup error: {e}")
        return None

    def get_option_greeks(self, commodity, expiry=None):
        """Get option greeks including IV from Angel API."""
        if not self._connected:
            return None
        try:
            self._throttle()
            params = {"name": commodity, "expirydate": expiry} if expiry else {"name": commodity}
            data = self.obj.optionGreek(params)
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"MCX option greeks error: {e}")
        return None


# ====================================================================
# MAIN PAPER TRADER
# ====================================================================
class CommodityPaperTrader:

    def __init__(self, ws_feed=None):
        self.angel = AngelMCXConnection()
        self.portfolio = CommodityPortfolio()
        self.engine = CommodityStrategyEngine(self.portfolio, self.angel)
        self._running = False
        self.ws_feed = ws_feed  # Real-time WebSocket price feed (optional)
        # Caches to reduce REST API calls
        self._option_ltp_cache = {}  # {cache_key: {'ltp': float, 'time': datetime}}
        self._ohlc_cache = {}  # v2.5.2: {commodity: {'data': ohlc_dict, 'time': datetime}}
        # EOD signal tracking
        self.daily_signal_count = 0
        self.daily_signals_all = []  # ALL signals (including skipped) for dummy PnL
        # v2.3: Trade quality tracking
        # v9.5: Reconstruct daily_trade_count from portfolio state (restart-safe)
        today_str = datetime.now().strftime('%Y-%m-%d')
        self.daily_trade_count = sum(1 for t in self.portfolio.closed_trades
            if t.get('exit_time', '').startswith(today_str)
            or t.get('timestamp', '').startswith(today_str))
        self.daily_trade_count += len(self.portfolio.positions)
        if self.daily_trade_count > 0:
            logger.info(f"  MCX_RESTART_SAFE: Recovered daily_trade_count={self.daily_trade_count} from portfolio state")
        self.exit_history = []          # [{commodity, direction, time, reason}] for re-entry cooldown
        self.ghost_zone_losses = {}     # {(commodity, 'CE'/'PE'): datetime}
        # v9.6: VIX infrastructure (ported from equity)
        self.current_vix = None
        self._vix_cache = {'value': None, 'time': None}

    def connect(self):
        connected = self.angel.connect()
        # Share MCX futures tokens with WebSocket feed for subscription
        if connected and self.ws_feed and self.angel._futures_tokens:
            self.ws_feed.set_mcx_tokens(self.angel._futures_tokens)
        return connected

    def _fetch_india_vix(self):
        """v9.6: Fetch India VIX. Cached 120s. Same VIX affects commodity sentiment.
        Uses Angel API with NSE VIX tokens (same API, different exchange segment).
        """
        cached = self._vix_cache
        if cached['value'] and cached['time'] and (datetime.now() - cached['time']).total_seconds() < 120:
            return cached['value']
        for token in ['26017', '99926004']:
            try:
                ltp = self.angel.get_ltp('NSE', token)
                if ltp and ltp > 0:
                    vix = ltp
                    # Normalize: Angel may return VIX x100 or x1000
                    if vix > 1000:
                        vix = vix / 1000
                    elif vix > 100:
                        vix = vix / 100
                    if 5 < vix < 80:
                        self._vix_cache = {'value': vix, 'time': datetime.now()}
                        self.current_vix = vix
                        return vix
            except Exception:
                pass
        return self.current_vix  # Return last known or None

    def get_vix_multiplier(self):
        """v9.6: Return threshold multiplier based on VIX level.
        Low VIX (<14): 1.5x (harder to enter, wider exits)
        Normal (14-20): 1.0x
        High VIX (>20): 0.8x (easier to enter)
        """
        vix = self.current_vix
        if vix is None:
            return 1.0
        if vix < MCX_VIX_LOW_THRESHOLD:
            return MCX_VIX_LOW_MULTIPLIER
        elif vix > MCX_VIX_HIGH_THRESHOLD:
            return MCX_VIX_HIGH_MULTIPLIER
        return 1.0

    def get_spot(self, commodity):
        """Get current spot price for commodity.
        Priority: WebSocket cache -> REST API -> historical close.
        """
        # 1. Try WebSocket cache (instant, no API call)
        if self.ws_feed and commodity in self.angel._futures_tokens:
            ws_ltp = self.ws_feed.get_ltp(self.angel._futures_tokens[commodity])
            if ws_ltp:
                return ws_ltp

        # 2. Fallback: REST API
        ltp = self.angel.get_ltp(commodity)
        if ltp:
            return ltp

        # 3. Fallback: historical close
        df = self.engine.historical_data.get(commodity)
        if df is not None and len(df) > 0:
            return df['Close'].iloc[-1]
        return None

    def get_intraday_ohlc(self, commodity):
        """v2.5.2: Get today's intraday OHLC from 5-min candles (like equity does).
        Without this, Gamma Blast never fires (body = spot - open = 0 always).
        Cached for 60s to avoid rate limiting.
        """
        token = self.angel._futures_tokens.get(commodity)
        if not token:
            return None

        # Check cache (60-second TTL)
        cached = self._ohlc_cache.get(commodity)
        if cached and (datetime.now() - cached['time']).total_seconds() < 60:
            return cached['data']

        try:
            today = datetime.now()
            from_date = today.strftime('%Y-%m-%d 09:00')
            to_date = today.strftime('%Y-%m-%d %H:%M')
            data = self.angel.get_historical(
                'MCX', token, 'FIVE_MINUTE', from_date, to_date
            )
            if data:
                opens = [c[1] for c in data]
                highs = [c[2] for c in data]
                lows = [c[3] for c in data]
                closes = [c[4] for c in data]
                volumes = [c[5] for c in data]
                ohlc = {
                    'open': opens[0],
                    'high': max(highs),
                    'low': min(lows),
                    'close': closes[-1],
                    'volume': sum(volumes),
                }
                self._ohlc_cache[commodity] = {'data': ohlc, 'time': datetime.now()}
                return ohlc
        except Exception as e:
            logger.error(f"MCX intraday OHLC error for {commodity}: {e}")

        # Fallback: use spot for all (old behavior)
        spot = self.get_spot(commodity)
        if spot:
            return {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}
        return None

    def _get_commodity_option_ltp(self, pos):
        """Get real-time option LTP for a commodity position, with 15s cache.
        Returns LTP float or None if unavailable.
        """
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        option_token = details.get('option_token')

        # Backfill for positions opened before this fix (no stored token)
        if not option_token:
            commodity = pos['commodity']
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
            expiry = self._get_nearest_mcx_expiry(commodity)
            if expiry:
                option_info = self.angel.find_option_tokens(commodity, expiry, pos['strike'], opt_type)
                if option_info:
                    option_token = str(option_info.get('token', ''))
                    # Store in position so we don't re-lookup every cycle
                    if isinstance(details, dict):
                        details['option_token'] = option_token
                    else:
                        pos['details'] = {'option_token': option_token}

        if not option_token:
            return None

        # Check cache (5-second TTL — v7.6.2: reduced from 15s to minimize price lag)
        cache_key = f"MCX_{option_token}"
        cached = self._option_ltp_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 5:
            return cached['ltp']

        try:
            # Use get_market_data for MCX option LTP (commodity get_ltp is futures only)
            mkt_data = self.angel.get_market_data('MCX', option_token)
            if mkt_data:
                ltp = float(mkt_data.get('ltp', 0) or 0)
                if ltp > 0:
                    self._option_ltp_cache[cache_key] = {'ltp': ltp, 'time': datetime.now()}
                    return ltp
        except Exception as e:
            logger.debug(f"  MCX Option LTP fetch failed for {cache_key}: {e}")

        return None

    def is_mcx_open(self):
        """Check if MCX trading is allowed (after 3:30 PM until 11:30 PM, Mon-Fri)."""
        now = datetime.now()
        if now.weekday() > 4:  # Saturday/Sunday
            return False
        return COMMODITY_TRADE_START <= now.time() <= MCX_CLOSE

    def _get_nearest_mcx_expiry(self, commodity):
        """Get nearest MCX option expiry for a commodity."""
        if self.angel.instruments is None:
            cache = os.path.join(DATA_DIR, 'instrument_master.csv')
            if os.path.exists(cache):
                self.angel.instruments = pd.read_csv(cache, low_memory=False)
            else:
                return None
        mask = (self.angel.instruments['name'] == commodity) & \
               (self.angel.instruments['exch_seg'] == 'MCX') & \
               (self.angel.instruments['instrumenttype'] == 'OPTFUT')
        matches = self.angel.instruments[mask]
        if len(matches) == 0:
            return None
        today = datetime.now().date()
        expiries = []
        for exp_str in matches['expiry'].unique():
            try:
                exp_date = pd.to_datetime(exp_str).date()
                if exp_date >= today:
                    expiries.append(exp_date)
            except Exception:
                continue
        if not expiries:
            return None
        nearest = min(expiries)
        return nearest.strftime('%d%b%Y').upper()

    def fetch_current_oi_iv(self, pos):
        """Fetch current OI and IV for a commodity option position."""
        if not hasattr(self, '_greeks_cache'):
            self._greeks_cache = {}

        commodity = pos['commodity']
        strike = pos['strike']
        opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
        cache_key = f"{commodity}_{strike}_{opt_type}"

        # Check cache (60s TTL)
        cached = self._greeks_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 60:
            return cached['data']

        current_oi = None
        current_iv = None

        try:
            # Method 1: get_market_data for OI
            expiry = self._get_nearest_mcx_expiry(commodity)
            if expiry:
                option_info = self.angel.find_option_tokens(commodity, expiry, strike, opt_type)
                if option_info:
                    token = str(option_info.get('token', ''))
                    mkt_data = self.angel.get_market_data('MCX', token)
                    if mkt_data:
                        current_oi = mkt_data.get('opnInterest', mkt_data.get('oi'))
                        if current_oi is not None:
                            current_oi = float(current_oi)

            # Method 2: get_option_greeks for IV
            greeks_data = self.angel.get_option_greeks(commodity, expiry)
            if greeks_data:
                for item in (greeks_data if isinstance(greeks_data, list) else [greeks_data]):
                    item_strike = float(item.get('strikePrice', 0))
                    item_type = item.get('optionType', '')
                    if abs(item_strike - strike) < 1 and item_type == opt_type:
                        iv_val = item.get('impliedVolatility')
                        if iv_val:
                            current_iv = float(iv_val)
                        break
        except Exception as e:
            logger.info(f"  MCX OI/IV fetch failed for {pos['id']}: {e}")

        # Fallback IV from historical volatility
        if current_iv is None or current_iv == 0:
            df = self.engine.historical_data.get(commodity)
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                current_iv = max(min(hv * 1.15 * 100, 60), 8)
            else:
                current_iv = pos.get('entry_iv', 0)

        if current_oi is None:
            current_oi = pos.get('entry_oi', 0)

        result = (current_oi, current_iv)
        self._greeks_cache[cache_key] = {'data': result, 'time': datetime.now()}

        if current_oi is not None or current_iv is not None:
            logger.info(f"  MCX OI/IV: {pos['id']} OI={current_oi} IV={current_iv}")

        return result

    def check_oi_iv_exit(self, pos, current_premium, current_iv_pct=None):
        """Check if commodity position should exit based on OI/IV changes.
        v2.3: Time-adaptive OI thresholds.
        Returns: (exit_reason, should_reverse) or (None, False)
        """
        entry_oi = pos.get('entry_oi', 0)
        entry_iv = pos.get('entry_iv', 0)

        current_oi, fetched_iv = self.fetch_current_oi_iv(pos)

        # Prefer implied vol from market LTP (passed in), then fetched, then entry
        current_iv = current_iv_pct if current_iv_pct and current_iv_pct > 0 else fetched_iv
        if current_iv is None or current_iv == 0:
            current_iv = entry_iv
        if current_oi is None:
            current_oi = entry_oi

        oi_change = 0
        iv_change = 0

        if entry_oi and entry_oi > 0:
            oi_change = abs(current_oi - entry_oi) / entry_oi * 100
        if entry_iv and entry_iv > 0:
            iv_change = abs(current_iv - entry_iv) / entry_iv * 100

        # v2.3: Time-adaptive OI threshold for MCX
        now_time = datetime.now().time()
        if now_time < dtime(10, 0):
            oi_threshold = MCX_OI_SURGE_FIRST_HOUR_PCT   # 55% — first hour
        elif now_time < dtime(21, 0):
            oi_threshold = MCX_OI_SURGE_MID_DAY_PCT      # 40% — normal session
        else:
            oi_threshold = MCX_OI_SURGE_LAST_HOUR_PCT    # 45% — close

        # v2.4: Strategy-specific threshold multiplier
        strat_name = pos.get('strategy', '').replace(' (Reversal)', '')
        strat_mult = MCX_STRATEGY_EXIT_MULT.get(strat_name, {'oi': 1.0, 'iv': 1.0})
        oi_threshold *= strat_mult['oi']

        # v7.6: Only hold through OI/IV noise if premium gain is significant (>25%)
        # Previously held ANY profitable position — caused stuck positions with old 2.5x targets
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        entry_prem = pos.get('entry_premium', 0)
        if entry_prem > 0:
            is_sell = pos.get('is_sell', False)
            if is_sell:
                prem_gain_pct = ((entry_prem - current_premium) / entry_prem * 100)
            else:
                prem_gain_pct = ((current_premium - entry_prem) / entry_prem * 100)
            if prem_gain_pct >= TSL_TRAIL_GAIN_PCT:  # Only hold if 25%+ premium gain
                logger.info(f"  MCX_OI_TREND_HOLD: {pos['id']} ({strat_name}) gain {prem_gain_pct:.1f}% "
                           f"(>{TSL_TRAIL_GAIN_PCT}%). Holding despite OI={oi_change:.1f}%/IV={iv_change:.1f}%.")
                return None, False

        # v2.5.2: Get unrealized PnL for loss-only exits
        unrealized_pnl = pos.get('unrealized_pnl', 0)

        # v9.6: Dynamic OI loss threshold (ported from equity)
        # Fixed -200 was too tight for expensive options, too loose for cheap ones
        spec = COMMODITIES.get(pos.get('commodity', ''), {})
        trade_value = pos.get('entry_premium', 0) * spec.get('lot_size', 1) * spec.get('multiplier', 1)
        oi_loss_threshold = max(-(trade_value * 0.15), -1500)  # 15% of trade value, capped at -1500

        # v8.0: OI/IV signals TIGHTEN SL instead of forcing exit
        # Rule 1: OI surge (time-adaptive) — v8.0: tighten SL, never force exit
        if oi_change > oi_threshold:
            if unrealized_pnl >= oi_loss_threshold:
                logger.info(f"  MCX_OI_HOLD: {pos['id']} OI={oi_change:.1f}% > {oi_threshold:.0f}% "
                           f"but PnL Rs {unrealized_pnl:.0f} >= {oi_loss_threshold:.0f} (dynamic). Holding.")
            else:
                logger.info(f"  MCX_OI_SL_TIGHTEN: {pos['id']} OI changed {oi_change:.1f}% > {oi_threshold:.0f}% "
                           f"PnL Rs {unrealized_pnl:.0f} (LOSING). "
                           f"(entry={entry_oi}, current={current_oi}, time={now_time.strftime('%H:%M')}). Tightening SL.")
                return 'OI_SL_TIGHTEN', False

        # Rule 2: IV change — v8.0: direction-aware, tighten SL instead of forced exit
        # IV direction: IV DROP hurts BUY positions, IV RISE hurts SELL positions
        is_sell = pos.get('is_sell', False)
        iv_raw_change = ((current_iv - entry_iv) / entry_iv * 100) if entry_iv > 0 else 0
        iv_hurts_position = (iv_raw_change < 0 and not is_sell) or (iv_raw_change > 0 and is_sell)

        iv_threshold = MCX_IV_SPIKE_PCT * strat_mult['iv']
        if iv_change > iv_threshold:
            entry_premium = pos.get('entry_premium', 0)
            if entry_premium < 30:
                logger.info(f"  MCX_IV_HOLD_LOW_PREM: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                           f"but entry premium Rs {entry_premium:.2f} < 30. Holding.")
            elif not iv_hurts_position:
                # v8.0: IV moving in our FAVOR — no need to tighten
                logger.info(f"  MCX_IV_FAVORABLE: {pos['id']} IV changed {iv_raw_change:+.1f}% "
                           f"({'DROP' if iv_raw_change < 0 else 'RISE'}) — favorable for "
                           f"{'SELL' if is_sell else 'BUY'} position. Holding.")
            elif unrealized_pnl >= oi_loss_threshold:
                logger.info(f"  MCX_IV_HOLD: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                           f"but PnL Rs {unrealized_pnl:.0f} >= {oi_loss_threshold:.0f} (dynamic). Holding.")
            else:
                logger.info(f"  MCX_IV_SL_TIGHTEN: {pos['id']} IV changed {iv_raw_change:+.1f}% > {iv_threshold:.0f}% "
                           f"PnL Rs {unrealized_pnl:.0f} — hurting {'SELL' if is_sell else 'BUY'} position. "
                           f"(entry={entry_iv}%, current={current_iv}%). Tightening SL.")
                return 'IV_SL_TIGHTEN', False

        # Rule 3: Combined OI+IV — v8.0: tighten SL aggressively (10% tighter)
        if oi_change > MCX_OI_IV_COMBO_OI and iv_change > MCX_OI_IV_COMBO_IV:
            logger.info(f"  MCX_OI_IV_COMBO_TIGHTEN: {pos['id']} OI={oi_change:.1f}%, IV={iv_change:.1f}%. Tightening SL aggressively.")
            return 'OI_IV_SL_TIGHTEN', False

        # Rule 4: Gamma shield for short positions
        if pos.get('is_sell') and abs(pos.get('gamma', 0)) > MCX_GAMMA_SHIELD_THRESHOLD:
            logger.info(f"  MCX_GAMMA_SHIELD: {pos['id']} gamma={pos.get('gamma', 0):.6f} > {MCX_GAMMA_SHIELD_THRESHOLD}")
            return 'GAMMA_SHIELD_EXIT', False

        return None, False

    def _track_exit(self, pos, exit_reason):
        """Track exit for cooldown logic. Called after every close_position."""
        direction = 'CE' if 'CE' in pos['signal_type'] else 'PE'
        self.exit_history.append({
            'commodity': pos['commodity'],
            'direction': direction,
            'time': datetime.now(),
            'reason': exit_reason,
        })
        # Track Ghost Zone losses for extended cooldown
        if ('GTZ' in pos['signal_type'] or 'Ghost' in pos.get('strategy', '')) and \
           pos.get('unrealized_pnl', 0) < 0:
            gz_key = (pos['commodity'], direction)
            self.ghost_zone_losses[gz_key] = datetime.now()
            logger.info(f"  GZ_LOSS_TRACKED: {pos['commodity']} {direction} — 30min cooldown active")
        # Cleanup old exit history (keep last 2 hours only)
        cutoff = datetime.now() - timedelta(hours=2)
        self.exit_history = [eh for eh in self.exit_history if eh['time'] > cutoff]

    def execute_reversal(self, pos, exit_reason):
        """After closing a commodity position due to OI+IV exit, open reverse trade.
        v2.5: DISABLED — CPR Reversal -Rs 5,825 on Feb 27. SELL targets bugged at 0.1x/1.5x.
        """
        return  # v2.5: Disabled
        try:
            commodity = pos['commodity']
            strategy = pos['strategy']
            original_type = pos['signal_type']

            # Don't chain reversals
            if '(Reversal)' in strategy:
                logger.info(f"  REVERSAL_SKIP: {pos['id']} already a reversal trade. No chain.")
                return

            # Determine reverse direction
            if 'CE' in original_type:
                reverse_type = ('BUY_CE_' if pos['is_sell'] else 'SELL_CE') + '_REV'
            else:
                reverse_type = ('BUY_PE_' if pos['is_sell'] else 'SELL_PE') + '_REV'

            logger.info(f"  MCX_REVERSAL: {pos['id']} → Opening {reverse_type} {commodity} "
                       f"@ strike {pos['strike']} due to {exit_reason}")

            # Get real LTP and option token for the reversed option
            rev_opt_type = 'CE' if 'CE' in reverse_type else 'PE'
            reversal_premium = pos['current_premium'] * 0.75  # fallback
            option_token = None
            option_exchange = 'MCX'
            entry_oi = pos.get('entry_oi', 0)
            spot = pos.get('entry_spot', 0) or self.get_spot(commodity)

            try:
                expiry = self._get_nearest_mcx_expiry(commodity)
                if expiry:
                    option_info = self.angel.find_option_tokens(commodity, expiry, pos['strike'], rev_opt_type)
                    if option_info:
                        option_token = str(option_info.get('token', ''))
                        mkt_data = self.angel.get_market_data('MCX', option_token)
                        if mkt_data:
                            real_ltp = float(mkt_data.get('ltp', 0) or 0)
                            entry_oi = float(mkt_data.get('opnInterest', mkt_data.get('oi', 0)) or 0)
                            if real_ltp > 0:
                                reversal_premium = real_ltp
                                logger.info(f"  MCX_REVERSAL_LTP: {commodity} {pos['strike']}{rev_opt_type} "
                                           f"Market={real_ltp:.2f}")
            except Exception as e:
                logger.warning(f"  MCX Reversal LTP fetch failed: {e}")

            # Compute real Greeks from implied vol
            greeks = {
                'delta': pos.get('delta', 0),
                'gamma': pos.get('gamma', 0),
                'theta': pos.get('theta', 0),
                'iv': (pos.get('entry_iv', 15) or 15) / 100,
            }
            if reversal_premium > 0 and spot > 0:
                T = max(pos.get('dte', 1), 1) / 365
                real_greeks = greeks_from_market_price_b76(
                    reversal_premium, spot, pos['strike'], T, RISK_FREE_RATE, rev_opt_type
                )
                if real_greeks:
                    greeks = real_greeks
                    logger.info(f"  MCX_REVERSAL_GREEKS: {commodity} {pos['strike']}{rev_opt_type} "
                               f"IV={real_greeks['iv']*100:.1f}%")

            # Set target/SL for reversal (conservative)
            is_sell_rev = 'SELL' in reverse_type
            if is_sell_rev:
                rev_target = round(reversal_premium * 0.1, 2)
                rev_sl = round(reversal_premium * 1.5, 2)
            else:
                rev_target = round(reversal_premium * 1.5, 2)  # v7.6: Was 2.5
                rev_sl = round(reversal_premium * 0.5, 2)  # v7.6: Was 0.4

            reverse_pos = self.portfolio.add_signal(
                strategy=strategy + ' (Reversal)',
                commodity=commodity,
                signal_type=reverse_type,
                strike=pos['strike'],
                entry_premium=reversal_premium,
                greeks=greeks,
                dte=pos.get('dte', 1),
                details={'origin': 'REVERSAL', 'original_id': pos['id'],
                         'exit_reason': exit_reason, 'spot': spot,
                         'target': rev_target, 'sl': rev_sl,
                         'option_token': option_token},
                oi=entry_oi,
                iv=greeks.get('iv', 0),
            )

            if reverse_pos:
                try:
                    from trade_notifier import send_info
                    msg = (f"<b>MCX REVERSAL TRADE</b>\n"
                           f"Closed: {pos['signal_type']} {commodity}\n"
                           f"Opened: {reverse_type} {commodity}\n"
                           f"Reason: {exit_reason}\n"
                           f"Strike: {pos['strike']:.0f}\n"
                           f"Premium: Rs {reversal_premium:.2f}")
                    send_info(msg)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"  MCX Reversal failed for {pos['id']}: {e}")

    def scan_all(self):
        logger.info("\n" + "=" * 70)
        logger.info("SCANNING COMMODITY STRATEGIES...")
        logger.info("=" * 70)

        now = datetime.now()
        dow = now.weekday()
        all_signals = []
        scan_data = {}  # v10: Collect indicator data for dashboard live display

        # Reset daily counters at market open
        if now.time() < dtime(9, 16) and self.daily_trade_count > 0:
            logger.info("  DAILY_RESET: Clearing commodity trade count and cooldowns")
            self.daily_trade_count = 0
            self.exit_history = []
            self.ghost_zone_losses = {}

        # CRITICAL: Block trading outside MCX hours
        if not self.is_mcx_open():
            logger.warning(f"  MCX MARKET CLOSED (current: {now.strftime('%H:%M:%S')})")
            logger.warning(f"  MCX hours: {MCX_OPEN} - {MCX_CLOSE}, Mon-Fri")
            logger.warning(f"  No signals will be generated.")
            return all_signals

        for commodity in PAPER_TRADE_COMMODITIES:
            spec = COMMODITIES[commodity]
            logger.info(f"\n--- {commodity} ({spec['description']}) ---")

            spot = self.get_spot(commodity)
            if not spot:
                logger.warning(f"  No spot data for {commodity}")
                continue

            # v2.5.2: Fetch real intraday OHLC (enables Gamma Blast signals)
            ohlc = self.get_intraday_ohlc(commodity)
            if not ohlc:
                ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}
            logger.info(f"  Spot: {spot:,.2f} | O: {ohlc['open']:,.2f} H: {ohlc['high']:,.2f} "
                       f"L: {ohlc['low']:,.2f} C: {ohlc['close']:,.2f}")

            indicators = self.engine.compute_indicators(commodity, ohlc)
            if not indicators:
                logger.warning(f"  No indicators for {commodity}")
                continue

            logger.info(f"  ATR: {indicators['atr']:,.2f} | IV: {indicators['iv']*100:.1f}% | "
                       f"CPR: {indicators['cpr_width']:.3f}%")
            logger.info(f"  Pivot: {indicators['pivot']:,.0f} | TC: {indicators['tc']:,.0f} | "
                       f"BC: {indicators['bc']:,.0f}")

            # v10: Collect live scan data for dashboard
            scan_data[commodity] = {
                'spot': spot,
                'atr': indicators.get('atr', 0),
                'iv': indicators.get('iv', 0),
                'hv': indicators.get('hv', 0),
                'vwap': indicators.get('vwap', 0),
                'pcr': indicators.get('pcr', 0),
                'pivot': indicators.get('pivot', 0),
                'tc': indicators.get('tc', 0),
                'bc': indicators.get('bc', 0),
                'cpr_width': indicators.get('cpr_width', 0),
                'cam_r3': indicators.get('cam_r3', 0),
                'cam_r4': indicators.get('cam_r4', 0),
                'cam_s3': indicators.get('cam_s3', 0),
                'cam_s4': indicators.get('cam_s4', 0),
                'resistance': indicators.get('resistance', 0),
                'support': indicators.get('support', 0),
                'demand_zone': indicators.get('demand_zone', 0),
                'supply_zone': indicators.get('supply_zone', 0),
            }

            # Monthly expiry - estimate DTE
            dte = max(5, 15 - (now.day % 28))

            # v7.1: Only 3 commodity strategies — PCR+VWAP needs equity OI data,
            # Survivor halted (needs more capital)
            checks = [
                ('CPR', self.engine.check_cpr_signals),
                ('Gamma Blast', self.engine.check_gamma_blast_signals),
                ('Ghost Zone', self.engine.check_ghost_zone_signals),
                # ('PCR+VWAP', ...),  # Not applicable to commodities (needs equity OI chain)
                # ('Survivor', ...),  # HALTED v7 — needs more capital
            ]

            for strat_name, check_fn in checks:
                signals = check_fn(commodity, spot, ohlc, indicators, dow, dte)
                for sig in signals:
                    sig['strategy'] = strat_name
                    sig['commodity'] = commodity
                    sig['spot'] = spot
                    sig['dte'] = dte
                    all_signals.append(sig)
                    logger.info(f"  SIGNAL [{strat_name}]: {sig['type']} "
                               f"Strike={sig['strike']:,.0f} Premium=Rs {sig['premium']:.2f} "
                               f"| {sig['reason']}")

        # v10: Write live scan data for dashboard
        self._write_live_scan_data(scan_data)

        return all_signals

    def execute_signals(self, signals):
        if not signals:
            logger.info("\n  No commodity signals this scan.")
            return

        # CRITICAL: Block execution outside MCX hours
        if not self.is_mcx_open():
            logger.warning(f"  {len(signals)} signals found but MCX CLOSED - NOT executing")
            return

        # v8.0: No trades before 09:30 — first 30 min inflated premiums, OI spikes, wide spreads
        if datetime.now().time() < MCX_FIRST_TRADE_TIME:
            logger.info(f"  MCX_EARLY_MARKET_BLOCK: {len(signals)} signals before "
                       f"{MCX_FIRST_TRADE_TIME.strftime('%H:%M')} — skipping (market stabilization)")
            return

        # v2.5.2: No new entries after 10:30 PM — trades need 60 min to develop
        if datetime.now().time() > MCX_LAST_ENTRY_TIME:
            logger.warning(f"  MCX_LATE_ENTRY_BLOCK: {len(signals)} signals at {datetime.now().strftime('%H:%M')} "
                          f"after {MCX_LAST_ENTRY_TIME.strftime('%H:%M')} cutoff — skipping to avoid EOD force close")
            return

        # Daily trade cap check
        if self.daily_trade_count >= MCX_MAX_TRADES_PER_DAY:
            logger.warning(f"  MCX_MAX_TRADES_REACHED: {self.daily_trade_count} trades today (limit {MCX_MAX_TRADES_PER_DAY}). Skipping all signals.")
            return

        logger.info(f"\n  {len(signals)} COMMODITY SIGNALS (PAPER):")

        # v9.6: PRE-FILTER — suppress weaker direction in CE vs PE conflicts (ported from equity)
        by_commodity = {}
        for sig in signals:
            comm = sig['commodity']
            direction = 'CE' if 'CE' in sig['type'] else 'PE'
            by_commodity.setdefault(comm, {}).setdefault(direction, []).append(sig)

        suppressed = set()
        for comm, directions in by_commodity.items():
            if 'CE' in directions and 'PE' in directions:
                best_ce = max(directions['CE'], key=lambda s: s.get('quality_score', 0))
                best_pe = max(directions['PE'], key=lambda s: s.get('quality_score', 0))
                ce_score = best_ce.get('quality_score', 0)
                pe_score = best_pe.get('quality_score', 0)
                if ce_score > pe_score:
                    for s in directions['PE']:
                        suppressed.add(id(s))
                    logger.info(f"  MCX_CONFLICT_FILTER: {comm} CE(score={ce_score}) > PE(score={pe_score}) "
                               f"— suppressing {len(directions['PE'])} PE signals")
                elif pe_score > ce_score:
                    for s in directions['CE']:
                        suppressed.add(id(s))
                    logger.info(f"  MCX_CONFLICT_FILTER: {comm} PE(score={pe_score}) > CE(score={ce_score}) "
                               f"— suppressing {len(directions['CE'])} CE signals")
                # Equal scores: keep both (let other filters decide)

        signals = [s for s in signals if id(s) not in suppressed]
        if suppressed:
            logger.info(f"  {len(suppressed)} conflicting commodity signals suppressed, {len(signals)} remaining")

        # v9.6: Fetch VIX for threshold adjustment (ported from equity)
        self._fetch_india_vix()
        vix_mult = self.get_vix_multiplier()
        if self.current_vix:
            logger.info(f"  MCX_VIX: {self.current_vix:.1f} (multiplier={vix_mult:.1f}x)")

        executed = 0
        skipped = 0
        for sig in signals:
            # Track ALL signals for dummy PnL at EOD
            self.daily_signal_count += 1
            self.daily_signals_all.append(sig)

            # Daily cap re-check inside loop
            if self.daily_trade_count >= MCX_MAX_TRADES_PER_DAY:
                logger.info(f"  MCX_MAX_TRADES_REACHED: {self.daily_trade_count} trades today. Skipping remaining.")
                break

            # v7.7: Strategy-aware duplicate check
            strike_tolerance = {'GOLDM': 200, 'SILVERM': 1000, 'CRUDEOILM': 100}
            tol = strike_tolerance.get(sig['commodity'], 100)
            sig_opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            same_strat_dup = [p for p in self.portfolio.positions
                              if p['commodity'] == sig['commodity']
                              and abs(p['strike'] - sig['strike']) <= tol
                              and (('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
                              and p['strategy'] == sig['strategy']]
            if same_strat_dup:
                logger.info(f"  SKIP (duplicate): {sig['strategy']} {sig['commodity']} "
                           f"{sig['strike']}{sig_opt_type} — already held @ {same_strat_dup[0]['strike']}")
                skipped += 1
                continue

            # v9.5: Count prior same-strategy+direction trades today (for score escalation)
            today_str = datetime.now().strftime('%Y-%m-%d')
            same_strat_closed = [t for t in self.portfolio.closed_trades
                if t.get('exit_time', '').startswith(today_str)
                and t.get('commodity') == sig['commodity']
                and t.get('strategy') == sig['strategy']
                and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type]
            reentry_count = len(same_strat_closed)

            # v9.5: Escalating score threshold — each re-entry needs higher conviction
            # 1st trade: MCX_MIN_SIGNAL_SCORE (50), 2nd: +15 (65), 3rd: +30 (80), 4th: +45 (95)
            escalated_min_score = MCX_MIN_SIGNAL_SCORE + (reentry_count * MCX_SCORE_ESCALATION_PER_REENTRY)
            sig['_escalated_min_score'] = escalated_min_score  # Pass to score check below
            if reentry_count > 0:
                logger.info(f"  REENTRY #{reentry_count+1}: {sig['strategy']} {sig['commodity']} {sig_opt_type} "
                           f"— need score >= {escalated_min_score} (base {MCX_MIN_SIGNAL_SCORE} + {reentry_count}x{MCX_SCORE_ESCALATION_PER_REENTRY})")

            # v9.5: Max same-direction trades per commodity per day (hard safety cap across all strategies)
            today_same_dir = sum(1 for t in self.portfolio.closed_trades
                if t.get('exit_time', '').startswith(today_str)
                and t.get('commodity') == sig['commodity']
                and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type)
            today_same_dir += sum(1 for p in self.portfolio.positions
                if p['commodity'] == sig['commodity']
                and ('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
            if today_same_dir >= MCX_MAX_SAME_DIRECTION_PER_COMMODITY:
                logger.info(f"  SKIP_DIR_CAP: {sig['commodity']} {sig_opt_type} — "
                           f"{today_same_dir} trades today (max {MCX_MAX_SAME_DIRECTION_PER_COMMODITY})")
                skipped += 1
                continue

            diff_strat_same_dir = [p for p in self.portfolio.positions
                                   if p['commodity'] == sig['commodity']
                                   and abs(p['strike'] - sig['strike']) <= tol
                                   and (('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
                                   and p['strategy'] != sig['strategy']]
            if diff_strat_same_dir:
                logger.info(f"  MCX_STRATEGY_CONFIRM: {sig['strategy']} {sig['commodity']} {sig['strike']}{sig_opt_type} "
                           f"confirms {diff_strat_same_dir[0]['strategy']} @ {diff_strat_same_dir[0]['strike']} — allowing entry")

            # Check for CONFLICTING positions: no BUY_CE + SELL_CE on same commodity/strike
            opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            is_buy_sig = 'BUY' in sig['type']
            conflicting = [p for p in self.portfolio.positions
                           if p['commodity'] == sig['commodity']
                           and abs(p['strike'] - sig['strike']) <= tol
                           and (('CE' in p['signal_type']) == (opt_type == 'CE'))
                           and p['is_sell'] == is_buy_sig]
            if conflicting:
                logger.info(f"  SKIP (conflicting): {sig['type']} conflicts with "
                           f"{conflicting[0]['signal_type']} on {sig['commodity']} {sig['strike']}")
                skipped += 1
                continue

            # v7.6.2: If opposite direction from SAME strategy, check quality score first, then flip
            opposite_dir = [p for p in self.portfolio.positions
                           if p['commodity'] == sig['commodity']
                           and p['strategy'] == sig['strategy']
                           and (('CE' in p['signal_type']) != (opt_type == 'CE'))]
            if opposite_dir:
                opp = opposite_dir[0]
                opp_pnl = opp.get('unrealized_pnl', 0)
                opp_prem = opp.get('current_premium', opp.get('entry_premium', 0))

                # v7.6.2: Compute quality score for the NEW signal BEFORE deciding to flip
                flip_score = sig.get('quality_score', 0)
                if not flip_score:
                    try:
                        indicators = self.engine.compute_indicators(sig['commodity'],
                            {'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'],
                             'close': sig['spot'], 'volume': 0})
                        if indicators:
                            flip_score = mcx_compute_signal_score(sig, sig['spot'], indicators)
                            sig['quality_score'] = flip_score
                    except Exception:
                        flip_score = 0

                # Reject flip if new signal quality is below DIRECTION_FLIP threshold
                if flip_score < MCX_DIRECTION_FLIP_MIN_SCORE:
                    logger.info(f"  SKIP_FLIP_QUALITY: {sig['strategy']} {sig['commodity']} {sig['type']} "
                               f"score={flip_score} < {MCX_DIRECTION_FLIP_MIN_SCORE} — not strong enough to flip")
                    skipped += 1
                    continue

                # v7.7: Only flip LOSING positions. Profitable ones run regardless of breakeven_locked.
                if opp_pnl <= 0:
                    logger.info(f"  DIRECTION_FLIP: Closing {opp['id']} (PnL Rs {opp_pnl:.0f} losing) "
                               f"to flip to {sig['type']} (score={flip_score})")
                    self.portfolio.close_position(opp['id'], opp_prem, 'DIRECTION_FLIP')
                    self._track_exit(opp, 'DIRECTION_FLIP')
                    self._notify_commodity_exit(opp, sig['commodity'], opp_prem, 'DIRECTION_FLIP')
                else:
                    # Position is profitable — let it run (even if breakeven-locked)
                    logger.info(f"  SKIP_DIRECTION: {sig['strategy']} {sig['commodity']} {sig['type']} "
                               f"— already holding {opp['signal_type']} (PnL Rs {opp_pnl:.0f}, let it run)")
                    skipped += 1
                    continue

            # ---- v7.2: Per-strategy capital usage LOGGING (shared pool, no hard caps) ----
            strat_name = sig.get('strategy', 'Unknown')
            strat_used = get_mcx_strategy_used_capital(self.portfolio.positions, strat_name)
            strat_target_pct = MCX_STRATEGY_ALLOCATION.get(strat_name, 0.10) * 100
            logger.info(f"  STRAT_USAGE: {strat_name} using Rs {strat_used:,.0f} "
                       f"(target {strat_target_pct:.0f}% of Rs {COMMODITY_CAPITAL:,.0f} pool)")

            # ---- v2.3: Re-entry cooldown check (v9.5: escalating) ----
            now = datetime.now()
            cooldown_hit = False

            # v9.5: Escalating cooldown — count today's same-direction losses
            today_str_cd = datetime.now().strftime('%Y-%m-%d')
            dir_losses = sum(1 for t in self.portfolio.closed_trades
                if t.get('exit_time', '').startswith(today_str_cd)
                and t.get('commodity') == sig['commodity']
                and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type
                and (t.get('net_pnl', 0) < 0 or
                     (t.get('exit_premium', 0) - t.get('entry_premium', 0)) < 0))
            escalated_cooldown = MCX_REENTRY_COOLDOWN_SECONDS * (2 ** min(dir_losses, 4))

            for eh in self.exit_history:
                if eh['commodity'] == sig['commodity'] and eh['direction'] == sig_opt_type:
                    elapsed = (now - eh['time']).total_seconds()
                    if elapsed < escalated_cooldown:
                        cd_label = "SKIP_ESCALATED_COOLDOWN" if dir_losses > 0 else "SKIP_COOLDOWN"
                        logger.info(f"  {cd_label}: {sig['commodity']} {sig_opt_type} exited {int(elapsed)}s ago "
                                   f"(need {int(escalated_cooldown)}s, {dir_losses} prior losses)")
                        skipped += 1
                        cooldown_hit = True
                        break
            if cooldown_hit:
                continue

            # ---- v2.3: Ghost Zone cooldown check ----
            if 'GTZ' in sig['type'] or 'Ghost' in sig.get('strategy', ''):
                gz_key = (sig['commodity'], sig_opt_type)
                if gz_key in self.ghost_zone_losses:
                    elapsed = (now - self.ghost_zone_losses[gz_key]).total_seconds()
                    if elapsed < MCX_GHOST_ZONE_COOLDOWN_SECONDS:
                        logger.info(f"  SKIP_GZ_COOLDOWN: {sig['commodity']} Ghost Zone {sig_opt_type} lost {int(elapsed)}s ago")
                        skipped += 1
                        continue

            # ---- v9.6: Stale CPR pivot validation (ported from equity) ----
            if 'CPR' in sig.get('strategy', '') or 'CPR' in sig.get('type', ''):
                sig_indicators = self.engine.compute_indicators(sig['commodity'],
                                 self.get_intraday_ohlc(sig['commodity']) or
                                 {'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'],
                                  'close': sig['spot'], 'volume': 0})
                if sig_indicators:
                    pivot = sig_indicators.get('pivot', 0)
                    if pivot > 0:
                        distance_pct = abs(sig['spot'] - pivot) / pivot * 100
                        if distance_pct > 3.0:
                            logger.info(f"  MCX_SKIP_STALE_CPR: {sig['commodity']} {sig['type']} "
                                       f"spot={sig['spot']:.0f} is {distance_pct:.1f}% from pivot={pivot:.0f} (max 3%)")
                            skipped += 1
                            continue

            # ---- v9.6: VIX-adjusted minimum premium check (ported from equity) ----
            is_sell = 'SELL' in sig['type']
            min_prem = (MCX_MIN_PREMIUM_SELL if is_sell else MCX_MIN_PREMIUM_BUY) * vix_mult
            if sig['premium'] < min_prem:
                logger.info(f"  MCX_SKIP_PREMIUM: {sig['commodity']} {sig['type']} Rs {sig['premium']:.2f} "
                           f"< VIX-adjusted min Rs {min_prem:.2f} (VIX mult={vix_mult}x)")
                skipped += 1
                continue

            # ---- v9.5: Signal quality score check (MANDATORY — no bypass) ----
            min_score_required = sig.get('_escalated_min_score', MCX_MIN_SIGNAL_SCORE)
            indicators = self.engine.compute_indicators(sig['commodity'],
                         {'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'], 'close': sig['spot'], 'volume': 0})
            if indicators:
                score = mcx_compute_signal_score(sig, sig['spot'], indicators, self.current_vix)
                sig['quality_score'] = score
                if score < min_score_required:
                    logger.info(f"  SKIP_QUALITY: {sig['commodity']} {sig['type']} score={score} < {min_score_required}"
                               f"{' (escalated)' if min_score_required > MCX_MIN_SIGNAL_SCORE else ''}")
                    skipped += 1
                    continue
                logger.info(f"  QUALITY_SCORE: {sig['commodity']} {sig['type']} score={score}/100"
                           f" (min required: {min_score_required})")
            else:
                # v9.5 FIX: indicators unavailable — BLOCK trade (was silently bypassing score check)
                logger.warning(f"  SKIP_NO_INDICATORS: {sig['commodity']} {sig['type']} "
                              f"— cannot compute quality score, trade blocked")
                skipped += 1
                continue

            # ---- v2.3: Profit filter check ----
            is_sell = 'SELL' in sig['type']
            spec = COMMODITIES[sig['commodity']]
            target_mult = sig.get('target', sig['premium'] * 2) / max(sig['premium'], 0.01)
            passes, exp_profit, total_cost = mcx_passes_profit_filter(
                sig['premium'], spec['lot_size'], spec['multiplier'], target_mult, is_sell
            )
            if not passes:
                logger.info(f"  SKIP_PROFIT: {sig['commodity']} {sig['type']} Rs {sig['premium']:.2f} "
                           f"expected Rs {exp_profit:.0f} < 2x cost Rs {total_cost:.0f}")
                skipped += 1
                continue

            # Fetch entry OI + real option LTP from market data
            entry_oi = 0
            option_token = None
            mkt_data = None
            real_ltp = None
            try:
                opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
                expiry = self._get_nearest_mcx_expiry(sig['commodity'])
                if expiry:
                    option_info = self.angel.find_option_tokens(
                        sig['commodity'], expiry, sig['strike'], opt_type
                    )
                    if option_info:
                        option_token = str(option_info.get('token', ''))
                        mkt_data = self.angel.get_market_data('MCX', option_token)
                        if mkt_data:
                            entry_oi = float(mkt_data.get('opnInterest', mkt_data.get('oi', 0)) or 0)
                            logger.info(f"  MCX_ENTRY_OI: {sig['commodity']} {sig['strike']}{opt_type} OI={entry_oi}")
                            # Extract real option LTP from same API response (zero extra calls)
                            fetched_ltp = float(mkt_data.get('ltp', 0) or 0)
                            if fetched_ltp > 0:
                                real_ltp = fetched_ltp
            except Exception as e:
                logger.info(f"  MCX OI/LTP fetch at entry failed for {sig['commodity']}: {e}")

            # Replace BS premium with real market LTP + compute real Greeks
            bs_premium = sig['premium']
            commodity_greeks = sig['greeks']

            # v9.4: CRITICAL — Skip trade if no real LTP from Angel API
            # Never enter trades at Black-76 derived pricing (causes phantom PnL)
            if not real_ltp or real_ltp <= 0:
                logger.warning(f"  MCX_SKIP_NO_LTP: {sig['commodity']} {sig['strike']}{opt_type} "
                              f"— no real LTP from Angel API. "
                              f"Black-76 premium Rs {bs_premium:.2f} rejected. Trade skipped.")
                skipped += 1
                continue

            if real_ltp and real_ltp > 0:
                # Preserve target/SL ratios from strategy, apply to real premium
                if bs_premium > 0:
                    target_ratio = sig.get('target', bs_premium * 1.5) / bs_premium
                    sl_ratio = sig.get('sl', bs_premium * 0.4) / bs_premium
                else:
                    target_ratio = 1.5
                    sl_ratio = 0.4
                sig['premium'] = real_ltp
                sig['target'] = round(real_ltp * target_ratio, 2)
                sig['sl'] = round(real_ltp * sl_ratio, 2)

                # Back-solve for real implied volatility and compute accurate Greeks (Black-76)
                T = max(sig['dte'], 1) / 365
                spot = sig.get('spot', 0)
                if spot > 0:
                    real_greeks = greeks_from_market_price_b76(
                        real_ltp, spot, sig['strike'], T, RISK_FREE_RATE, opt_type
                    )
                    if real_greeks:
                        commodity_greeks = real_greeks
                        sig['greeks'] = real_greeks
                        logger.info(f"  MCX_REAL_GREEKS: {sig['commodity']} {sig['strike']}{opt_type} "
                                   f"IV={real_greeks['iv']*100:.1f}% Δ={real_greeks['delta']:.3f} "
                                   f"Γ={real_greeks['gamma']:.6f} Θ={real_greeks['theta']:.2f}")
                    else:
                        logger.info(f"  MCX_REAL_GREEKS: IV solve failed, using Black-76 Greeks")
                logger.info(f"  MCX_REAL_LTP: {sig['commodity']} {sig['strike']}{opt_type} "
                           f"BS={bs_premium:.2f} → Market={real_ltp:.2f} "
                           f"Target={sig['target']:.2f} SL={sig['sl']:.2f}")

            result = self.portfolio.add_signal(
                strategy=sig['strategy'],
                commodity=sig['commodity'],
                signal_type=sig['type'],
                strike=sig['strike'],
                entry_premium=sig['premium'],
                greeks=sig['greeks'],
                dte=sig['dte'],
                details={'reason': sig['reason'], 'target': sig.get('target'),
                         'sl': sig.get('sl'), 'spot': sig.get('spot'),
                         'option_token': option_token,
                         'quality_score': sig.get('quality_score', 0),  # v2.5: FIX — was missing!
                         'expiry': expiry},  # v9.1: Store expiry for dashboard display
                oi=entry_oi,
                iv=sig['greeks'].get('iv', 0),
            )
            if result is None:
                skipped += 1
                continue
            executed += 1
            self.daily_trade_count += 1

            # Send Telegram notification
            try:
                from trade_notifier import notify_trade_entry
                logger.info(f"  TELEGRAM: Sending entry notification for {sig['commodity']} {sig['type']}")
                spec = COMMODITIES[sig['commodity']]
                # Capital used: SELL = margin blocked, BUY = premium paid
                is_sell_sig = 'SELL' in sig['type']
                if is_sell_sig:
                    capital = spec['margin']
                else:
                    capital = sig['premium'] * spec['lot_size'] * spec['multiplier']
                # Calculate total locked (BUY/SELL aware) and available capital
                locked = 0
                for p in self.portfolio.positions:
                    p_spec = COMMODITIES[p['commodity']]
                    if p['is_sell']:
                        locked += p_spec['margin']
                    else:
                        locked += p['entry_premium'] * p['lot_size'] * p['multiplier']
                total_invested = locked
                capital_available = COMMODITY_CAPITAL - locked
                score_info = f" | Score={sig.get('quality_score', 'N/A')}"
                trade_info = f" | Trade #{self.daily_trade_count}/{MCX_MAX_TRADES_PER_DAY}"
                enriched_reason = sig['reason'] + score_info + trade_info
                notify_trade_entry(
                    market="COMMODITY", strategy=sig['strategy'],
                    symbol=sig['commodity'], signal_type=sig['type'],
                    strike=sig['strike'], entry_price=sig['premium'],
                    spot=sig.get('spot', 0), lot_size=spec['lot_size'],
                    multiplier=spec['multiplier'], delta=sig['greeks'].get('delta', 0),
                    target=sig.get('target', 0), sl=sig.get('sl', 0),
                    capital_used=capital, reason=enriched_reason,
                    capital_available=capital_available,
                    total_invested=total_invested,
                )
            except Exception as e:
                logger.warning(f"  Telegram notify failed: {e}")

        if skipped:
            logger.info(f"  Executed: {executed} | Skipped: {skipped} | "
                       f"Day total: {self.daily_trade_count}/{MCX_MAX_TRADES_PER_DAY}")

    def check_exits(self):
        if not self.portfolio.positions:
            return

        logger.info("\n  Checking commodity exits for open positions...")

        # ---- CIRCUIT BREAKER: Close ALL if daily loss exceeds limit ----
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.portfolio.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"  MCX_CIRCUIT_BREAKER: Daily loss Rs {daily_loss:,.0f} > limit Rs {MAX_DAILY_LOSS:,.0f}")
            for pos in list(self.portfolio.positions):
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'CIRCUIT_BREAKER')
                self._track_exit(pos, 'CIRCUIT_BREAKER')
            try:
                from trade_notifier import send_message
                send_message(f"<b>MCX CIRCUIT BREAKER</b>\nDaily loss: Rs {daily_loss:,.0f}\nAll commodity positions closed.")
            except Exception:
                pass
            self.portfolio.save_state()
            return

        for pos in list(self.portfolio.positions):
            # Skip positions opened less than 15 minutes ago (grace period — v2.3)
            entry_time = datetime.fromisoformat(pos['timestamp'])
            elapsed_secs = (datetime.now() - entry_time).total_seconds()
            if elapsed_secs < MCX_GRACE_PERIOD_SECONDS:
                logger.info(f"  GRACE: {pos['id']} opened {int(elapsed_secs)}s ago (need {MCX_GRACE_PERIOD_SECONDS}s)")
                continue

            # ---- EOD FORCE CLOSE: 15 min before MCX close (23:15) ----
            if datetime.now().time() > dtime(23, 15):
                current = pos.get('current_premium', pos['entry_premium'])
                logger.info(f"  MCX_EOD_CLOSE: {pos['id']} force close (MCX closing at 23:30)")
                self.portfolio.close_position(pos['id'], current, 'EOD_FORCE_CLOSE')
                self._track_exit(pos, 'EOD_FORCE_CLOSE')
                self._notify_commodity_exit(pos, pos['commodity'], current, 'EOD_FORCE_CLOSE')
                continue

            commodity = pos['commodity']
            spot = self.get_spot(commodity)
            if not spot:
                continue

            spec = COMMODITIES[commodity]

            # ---- PREMIUM UPDATE: Real market LTP with fallback to delta+gamma+theta ----
            entry_spot = pos.get('entry_spot', 0)
            if entry_spot == 0:
                entry_spot = pos.get('details', {}).get('spot', spot)
            spot_change = spot - entry_spot
            delta_val = pos.get('delta', 0.5)
            gamma_val = pos.get('gamma', 0)
            hours_held = (datetime.now() - entry_time).total_seconds() / 3600

            # Try real option LTP first (15s cached)
            real_option_ltp = self._get_commodity_option_ltp(pos)
            if real_option_ltp is not None:
                current = real_option_ltp
                premium_source = 'MARKET'
            else:
                # Fallback: delta+gamma+theta approximation
                theta_val = pos.get('theta', 0)
                premium_delta = delta_val * spot_change + 0.5 * gamma_val * (spot_change ** 2)
                time_decay = theta_val * (hours_held / 24)
                current = max(pos['entry_premium'] + premium_delta + time_decay, 0.05)
                premium_source = 'APPROX'
            pos['current_premium'] = round(current, 2)

            # Update unrealized PnL
            mult = spec['multiplier']
            lot = spec['lot_size']
            if pos['is_sell']:
                pos['unrealized_pnl'] = round(
                    (pos['entry_premium'] - current) * lot * mult - pos['entry_cost'], 2)
            else:
                pos['unrealized_pnl'] = round(
                    (current - pos['entry_premium']) * lot * mult - pos['entry_cost'], 2)

            # ---- v7.7: COMPUTE HOLD SCORE for signal-based exit decisions ----
            hold_score = 50  # Default moderate
            hold_mins = (datetime.now() - entry_time).total_seconds() / 60
            if hold_mins >= MCX_HOLD_SCORE_MIN_HOLD_MINS:
                try:
                    hold_indicators = self.engine.compute_indicators(commodity,
                        {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0})
                    hold_score = mcx_compute_hold_score(pos, spot, hold_indicators)
                except Exception:
                    hold_score = 50
            pos['hold_score'] = hold_score
            hold_label = 'STRONG' if hold_score >= MCX_HOLD_SCORE_STRONG else 'MODERATE' if hold_score >= MCX_HOLD_SCORE_WEAK else 'WEAK'

            # ---- v7.7: SIGNAL WEAK EXIT — early exit for losing commodity positions with weak signals ----
            if (hold_mins >= MCX_HOLD_SCORE_MIN_HOLD_MINS
                    and hold_score < MCX_HOLD_SCORE_WEAK
                    and pos.get('unrealized_pnl', 0) < 0
                    and hours_held > 1):
                logger.info(f"  MCX_SIGNAL_WEAK_EXIT: {pos['id']} hold_score={hold_score} ({hold_label}) "
                           f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} — signals reversed, early exit")
                self.portfolio.close_position(pos['id'], current, 'SIGNAL_WEAK_EXIT')
                self._track_exit(pos, 'SIGNAL_WEAK_EXIT')
                self._notify_commodity_exit(pos, commodity, current, 'SIGNAL_WEAK_EXIT')
                continue

            # ---- TIME-BASED EXIT: Close stale commodity positions (v2.5: >4 hours with <10% profit) ----
            if hours_held > 4:  # v2.5: Reduced from 6 hours
                profit_pct = (pos['unrealized_pnl'] / max(pos['entry_premium'] * lot * mult, 1)) * 100
                if abs(profit_pct) < 10:  # v2.5: Raised from 5%
                    # v7.7: Override TIME_EXIT if hold score is STRONG
                    if hold_score >= MCX_HOLD_SCORE_STRONG:
                        logger.info(f"  MCX_SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                                   f"— overriding TIME_EXIT (signals still valid)")
                    else:
                        logger.info(f"  MCX_TIME_EXIT: {pos['id']} held {hours_held:.1f}h with only "
                                   f"{profit_pct:.1f}% profit (hold_score={hold_score} {hold_label})")
                        self.portfolio.close_position(pos['id'], current, 'TIME_EXIT_NO_PROGRESS')
                        self._track_exit(pos, 'TIME_EXIT_NO_PROGRESS')
                        self._notify_commodity_exit(pos, commodity, current, 'TIME_EXIT_NO_PROGRESS')
                        continue

            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            target = details.get('target', pos['entry_premium'] * 1.5)
            sl = details.get('sl', pos['entry_premium'] * 0.5)

            # ---- v7.6: TSL based on PREMIUM GAIN % (not target distance %) ----
            entry_prem = pos['entry_premium']
            if not pos['is_sell']:
                # BUY positions: premium going UP is profit
                premium_gain_pct = ((current - entry_prem) / entry_prem * 100) if entry_prem > 0 else 0

                if current > pos.get('peak_premium', entry_prem):
                    pos['peak_premium'] = round(current, 2)

                peak = pos.get('peak_premium', current)
                peak_gain_pct = ((peak - entry_prem) / entry_prem * 100) if entry_prem > 0 else 0
                profit_from_entry = peak - entry_prem

                # Phase 1: Lock breakeven at 15% premium gain
                if peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 1.03, 2)
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} gained {peak_gain_pct:.0f}% "
                               f"→ locked SL at Rs {pos['trailing_sl']:.2f}")

                # Phase 3: Tight trail at 40%+ gain (20% below peak profit)
                if peak_gain_pct >= TSL_TIGHT_GAIN_PCT:
                    new_tsl = round(peak - (profit_from_entry * TSL_TIGHT_DISTANCE_PCT / 100), 2)
                    new_tsl = max(new_tsl, pos.get('trailing_sl') or 0)
                    if new_tsl > (pos.get('trailing_sl') or 0):
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TIGHT: {pos['id']} gain {peak_gain_pct:.0f}% "
                                   f"SL→Rs {pos['trailing_sl']:.2f} (peak={peak:.2f})")
                # Phase 2: Trail at 25%+ gain (30% below peak profit)
                elif peak_gain_pct >= TSL_TRAIL_GAIN_PCT:
                    new_tsl = round(peak - (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    new_tsl = max(new_tsl, pos.get('trailing_sl') or 0)
                    if new_tsl > (pos.get('trailing_sl') or 0):
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TRAIL: {pos['id']} gain {peak_gain_pct:.0f}% "
                                   f"SL→Rs {pos['trailing_sl']:.2f} (peak={peak:.2f})")

                # v7.7: Signal-based TSL ratchet — if signals STRONG, raise TSL aggressively
                if (hold_score >= MCX_HOLD_SCORE_STRONG
                        and peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                        and pos.get('trailing_sl')):
                    ratchet_tsl = round(current * 0.85, 2)
                    if ratchet_tsl > pos['trailing_sl']:
                        old_tsl = pos['trailing_sl']
                        pos['trailing_sl'] = ratchet_tsl
                        logger.info(f"  MCX_SIGNAL_HOLD_RATCHET: {pos['id']} hold_score={hold_score} "
                                   f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                   f"(85% of current Rs {current:.2f})")

                if pos.get('trailing_sl') and current <= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} premium {current:.2f} <= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    self._notify_commodity_exit(pos, commodity, current, 'TRAILING_SL_HIT')
                    continue
            else:
                # SELL positions: premium going DOWN is profit
                premium_gain_pct = ((entry_prem - current) / entry_prem * 100) if entry_prem > 0 else 0

                if current < pos.get('trough_premium', entry_prem):
                    pos['trough_premium'] = round(current, 2)

                trough = pos.get('trough_premium', current)
                trough_gain_pct = ((entry_prem - trough) / entry_prem * 100) if entry_prem > 0 else 0
                profit_from_entry = entry_prem - trough

                # Phase 1: Lock breakeven at 15% premium gain
                if trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 0.97, 2)
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} SELL gained {trough_gain_pct:.0f}% "
                               f"→ locked SL at Rs {pos['trailing_sl']:.2f}")

                # Phase 3: Tight trail at 40%+ gain
                if trough_gain_pct >= TSL_TIGHT_GAIN_PCT:
                    new_tsl = round(trough + (profit_from_entry * TSL_TIGHT_DISTANCE_PCT / 100), 2)
                    if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TIGHT: {pos['id']} SELL gain {trough_gain_pct:.0f}% "
                                   f"SL→Rs {pos['trailing_sl']:.2f} (trough={trough:.2f})")
                # Phase 2: Trail at 25%+ gain
                elif trough_gain_pct >= TSL_TRAIL_GAIN_PCT:
                    new_tsl = round(trough + (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SELL gain {trough_gain_pct:.0f}% "
                                   f"SL→Rs {pos['trailing_sl']:.2f} (trough={trough:.2f})")

                # v7.7: Signal-based TSL ratchet for SELL
                if (hold_score >= MCX_HOLD_SCORE_STRONG
                        and trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                        and pos.get('trailing_sl')):
                    ratchet_tsl = round(current * 1.15, 2)  # SELL: 15% above current (tighter)
                    if ratchet_tsl < pos['trailing_sl']:
                        old_tsl = pos['trailing_sl']
                        pos['trailing_sl'] = ratchet_tsl
                        logger.info(f"  MCX_SIGNAL_HOLD_RATCHET: {pos['id']} SELL hold_score={hold_score} "
                                   f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                   f"(115% of current Rs {current:.2f})")

                if pos.get('trailing_sl') and current >= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} SELL premium {current:.2f} >= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    self._notify_commodity_exit(pos, commodity, current, 'TRAILING_SL_HIT')
                    continue

            # ---- OI+IV DYNAMIC EXIT CHECK ----
            # Compute current IV from implied vol back-solve (same method as entry)
            current_iv_pct = None
            if current > 0 and spot > 0:
                opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
                T_iv = max(pos.get('dte', 1), 1) / 365
                iv_solved = implied_vol_b76(current, spot, pos['strike'], T_iv, RISK_FREE_RATE, opt_type)
                if iv_solved:
                    current_iv_pct = round(iv_solved * 100, 1)
            oi_iv_reason, should_reverse = self.check_oi_iv_exit(pos, current, current_iv_pct)
            # v7.7: Signal-based override
            if oi_iv_reason:
                if hold_score >= MCX_HOLD_SCORE_STRONG and pos.get('unrealized_pnl', 0) > 0:
                    logger.info(f"  MCX_SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                               f"— overriding {oi_iv_reason} (signals strong + profitable)")
                    oi_iv_reason = None
            if oi_iv_reason:
                # v8.0: OI/IV TIGHTEN SL instead of forcing exit. Only GAMMA_SHIELD forces exit.
                if 'SL_TIGHTEN' in oi_iv_reason:
                    # Tighten trailing SL — combo tightens 10%, single tightens 5%
                    tighten_pct = 0.10 if 'OI_IV_SL_TIGHTEN' == oi_iv_reason else 0.05
                    if pos['is_sell']:
                        new_tsl = round(current * (1 + tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL "
                                       f"Rs {old_tsl}→{new_tsl:.2f} "
                                       f"({tighten_pct*100:.0f}% above current {current:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL already tighter "
                                       f"Rs {pos['trailing_sl']:.2f}. No change.")
                    else:
                        new_tsl = round(current * (1 - tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl > (pos.get('trailing_sl') or 0):
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL "
                                       f"Rs {old_tsl}→{new_tsl:.2f} "
                                       f"({tighten_pct*100:.0f}% below current {current:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL already tighter "
                                       f"Rs {pos.get('trailing_sl'):.2f}. No change.")
                    # v8.0: Don't close, don't reverse — let TSL handle exit naturally
                else:
                    # GAMMA_SHIELD_EXIT — forced exit (real gamma risk)
                    self.portfolio.close_position(pos['id'], current, oi_iv_reason)
                    self._track_exit(pos, oi_iv_reason)
                    self._notify_commodity_exit(pos, commodity, current, oi_iv_reason)
                    continue

            # ---- STATIC EXIT CHECK ----
            logger.info(f"  EXIT_CHECK: {pos['id']} | Entry: {pos['entry_premium']:.2f} → Current: {current:.2f} [{premium_source}] | "
                       f"Target: {target:.2f} SL: {sl:.2f} | TSL: {pos.get('trailing_sl', 'N/A')} | "
                       f"Spot: {entry_spot:.0f}→{spot:.0f} Δ={spot_change:+.0f} | "
                       f"Hold: {hold_score}/{hold_label}")

            exit_reason = None
            if pos['is_sell']:
                if current <= target: exit_reason = 'TARGET'
                elif current >= sl: exit_reason = 'SL'
            else:
                if current >= target: exit_reason = 'TARGET'
                elif current <= sl: exit_reason = 'SL'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current, exit_reason)
                self._track_exit(pos, exit_reason)
                self._notify_commodity_exit(pos, commodity, current, exit_reason)

        # Persist updated PnL to disk after exit check cycle
        self.portfolio.save_state()

    def _notify_commodity_exit(self, pos, commodity, current, exit_reason):
        """Helper to send Telegram exit notification for commodity trades."""
        try:
            from trade_notifier import notify_trade_exit
            spec = COMMODITIES[commodity]
            if pos['is_sell']:
                capital = spec['margin']
            else:
                capital = pos['entry_premium'] * spec['lot_size'] * spec['multiplier']
            locked = 0
            for p in self.portfolio.positions:
                p_spec = COMMODITIES[p['commodity']]
                if p['is_sell']:
                    locked += p_spec['margin']
                else:
                    locked += p['entry_premium'] * p['lot_size'] * p['multiplier']
            notify_trade_exit(
                market="COMMODITY", strategy=pos['strategy'],
                symbol=commodity, signal_type=pos['signal_type'],
                strike=pos['strike'], entry_price=pos['entry_premium'],
                exit_price=current, entry_time=pos['timestamp'],
                pnl=pos['unrealized_pnl'], capital_used=capital,
                exit_reason=exit_reason,
                capital_available=COMMODITY_CAPITAL - locked,
                total_invested=locked,
            )
        except Exception as e:
            logger.warning(f"  Telegram exit notify failed: {e}")

    def get_eod_summary(self):
        """Return end-of-day summary with actual PnL (our trades) + dummy PnL (all signals)."""
        actual_closed_pnl = sum(t.get('pnl', 0) for t in self.portfolio.closed_trades)
        actual_open_pnl = sum(p.get('unrealized_pnl', 0) for p in self.portfolio.positions)
        actual_total = actual_closed_pnl + actual_open_pnl

        # Dummy PnL: estimate for ALL signals as if unlimited capital
        dummy_pnl = 0
        for sig in self.daily_signals_all:
            spot_now = self.get_spot(sig['commodity']) or sig.get('spot', 0)
            entry_spot = sig.get('spot', spot_now)
            if not entry_spot or not spot_now:
                continue
            spot_change = spot_now - entry_spot
            delta = sig.get('greeks', {}).get('delta', 0.5)
            premium_change = delta * spot_change
            spec = COMMODITIES.get(sig['commodity'], {})
            lot = spec.get('lot_size', 1)
            mult = spec.get('multiplier', 1)
            dummy_pnl += premium_change * lot * mult

        capital_used = 0
        for p in self.portfolio.positions:
            spec = COMMODITIES.get(p['commodity'], {})
            if p.get('is_sell', False):
                capital_used += spec.get('margin', 15000)
            else:
                capital_used += p['entry_premium'] * p['lot_size'] * p['multiplier']

        return {
            'total_signals': self.daily_signal_count,
            'trades_taken': len(self.portfolio.closed_trades) + len(self.portfolio.positions),
            'open_positions': len(self.portfolio.positions),
            'closed_trades': len(self.portfolio.closed_trades),
            'actual_pnl': round(actual_total, 2),
            'dummy_pnl': round(dummy_pnl, 2),
            'capital_used': round(capital_used, 2),
        }

    def _write_live_scan_data(self, scan_data):
        """v10: Write live indicator data for dashboard consumption."""
        try:
            live_file = os.path.join(PAPER_DIR, 'live_scan_data.json')
            payload = {
                'last_updated': datetime.now().isoformat(),
                'vix': None,  # Commodities don't track India VIX
                'market': 'commodity',
                'symbols': scan_data,
            }
            with open(live_file, 'w') as f:
                json.dump(payload, f, indent=2, default=str)
            logger.debug(f"Live scan data written: {len(scan_data)} commodities")
        except Exception as e:
            logger.error(f"Error writing live scan data: {e}")

    def run_once(self):
        signals = self.scan_all()
        self.execute_signals(signals)
        self.check_exits()
        self.portfolio.print_status()

        # Save signals log with full data for future backtests (v7.2)
        if signals:
            today = datetime.now().strftime('%Y%m%d')
            log_file = os.path.join(PAPER_DIR, f'commodity_signals_{today}.csv')

            # Build executed signal keys for status tracking
            executed_keys = set()
            for p in self.portfolio.positions:
                key = f"{p.get('strategy', '')}_{p.get('commodity', '')}_{p.get('strike', 0)}"
                executed_keys.add(key)

            rows = []
            for s in signals:
                greeks = s.get('greeks', {})
                sig_key = f"{s.get('strategy', '')}_{s.get('commodity', '')}_{s.get('strike', 0)}"

                # Fetch volume from OHLC if available
                ohlc = self.get_intraday_ohlc(s.get('commodity', ''))
                volume = ohlc.get('volume', 0) if ohlc else 0

                # Get IV from indicators
                indicators = self.engine.compute_indicators(
                    s.get('commodity', ''),
                    ohlc or {'open': s.get('spot', 0), 'high': s.get('spot', 0),
                             'low': s.get('spot', 0), 'close': s.get('spot', 0), 'volume': 0}
                )
                iv = indicators.get('iv', 0) if indicators else 0

                rows.append({
                    'timestamp': datetime.now().isoformat(),
                    'strategy': s.get('strategy', ''),
                    'commodity': s.get('commodity', ''),
                    'type': s['type'],
                    'strike': s['strike'],
                    'premium': s['premium'],
                    'spot': s.get('spot', 0),
                    'dte': s.get('dte', 0),
                    # Greeks
                    'delta': greeks.get('delta', 0),
                    'gamma': greeks.get('gamma', 0),
                    'theta': greeks.get('theta', 0),
                    'vega': greeks.get('vega', 0),
                    'iv': greeks.get('iv', iv),
                    # Market data
                    'volume': volume,
                    'oi': s.get('oi', 0),
                    # Trade params
                    'target': s.get('target', 0),
                    'sl': s.get('sl', 0),
                    'quality_score': s.get('quality_score', 0),
                    'reason': s.get('reason', ''),
                    # Execution status
                    'executed': sig_key in executed_keys,
                })
            df = pd.DataFrame(rows)
            if os.path.exists(log_file):
                df = pd.concat([pd.read_csv(log_file), df], ignore_index=True)
            df.to_csv(log_file, index=False)
            logger.info(f"  Signals logged ({len(rows)} rows): {log_file}")

    def run_continuous(self, interval=5):
        self._running = True
        logger.info(f"\n{'='*70}")
        logger.info("COMMODITY PAPER TRADING STARTED")
        logger.info(f"MCX Hours: 9:00 AM - 11:30 PM | Scan every {interval} min")
        logger.info(f"Commodities: {', '.join(PAPER_TRADE_COMMODITIES)}")
        logger.info(f"Capital: Rs {self.portfolio.capital:,.0f}")
        logger.info(f"{'='*70}\n")

        signal.signal(signal.SIGINT, lambda s, f: setattr(self, '_running', False))

        while self._running:
            now = datetime.now().time()
            if MCX_OPEN <= now <= MCX_CLOSE:
                self.run_once()
            elif now > MCX_CLOSE:
                logger.info("MCX closed. Final status:")
                self.portfolio.print_status()
                break
            time.sleep(interval * 60)


def main():
    print("=" * 70)
    print("  COMMODITY OPTIONS - PAPER TRADING SYSTEM")
    print("  MCX | Gold Mini, Silver Mini, Crude Oil Mini")
    print("  Strategies: CPR, Gamma Blast, Ghost Zone")
    print("=" * 70)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=int, default=5)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()

    if args.reset:
        p = CommodityPortfolio()
        p.capital = INITIAL_CAPITAL; p.positions = []; p.closed_trades = []; p.daily_pnl = {}
        p.save_state()
        print("Commodity portfolio reset to Rs 3,00,000")
        return

    if args.status:
        CommodityPortfolio().print_status()
        return

    trader = CommodityPaperTrader()
    for comm in PAPER_TRADE_COMMODITIES:
        trader.engine.load_historical(comm)

    if args.offline:
        logger.info("Running OFFLINE mode...")
        trader.run_once()
        return

    if trader.connect():
        if args.once:
            trader.run_once()
        else:
            trader.run_continuous(args.interval)
    else:
        logger.warning("API failed, running offline...")
        trader.run_once()


if __name__ == '__main__':
    main()
