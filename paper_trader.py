"""
PAPER TRADING SYSTEM - 4 ACTIVE STRATEGIES (v7.6)
===================================================
Live paper trading with Angel One SmartAPI
Active: Gamma Blast (40%), CPR (30%), Ghost Zone v7 (20%), PCR+VWAP (10%)
Halted: Survivor (needs Rs 1L+ capital per symbol)
Runs on NIFTY, BANKNIFTY, SENSEX with live Angel data

Per-Strategy Capital Allocation (Rs 3L total):
  Gamma Blast:  40% = Rs 1,20,000 — best risk-adjusted (Sharpe 4.42)
  CPR:          30% = Rs 90,000 — solid 70% WR, consistent
  Ghost Zone:   20% = Rs 60,000 — v7 institutional methodology
  PCR+VWAP:     10% = Rs 30,000 — needs live OI data to prove

Usage:
    python paper_trader.py              # Run paper trading
    python paper_trader.py --backfill   # Backfill historical + run
    python paper_trader.py --status     # Show current positions
"""

import sys
import os
import json
import time
import signal
import threading
import logging
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
from oi_velocity_tracker import OIVelocityTracker, make_gamma_blast_signal
from option_premium_cpr import OptionPremiumCPR
from ghost_zone_v8 import GhostZoneV8

from self_healing import SelfHealingEngine
from self_healing import SelfHealingEngine
import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OPTIONS_DIR = os.path.join(DATA_DIR, 'options')
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

for d in [PAPER_DIR, LOG_DIR, OPTIONS_DIR]:
    os.makedirs(d, exist_ok=True)

INITIAL_CAPITAL = 300000  # Rs 3L for equity (all strategies share this pool)
RISK_FREE_RATE = 0.065
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}  # NSE revised Jan 2026
MARGIN_PER_LOT = {'NIFTY': 100000, 'BANKNIFTY': 95000, 'SENSEX': 70000}  # Realistic SPAN margins for selling
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}  # NSE strike gap
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18
# Wave Extractor v3 constants (v17)
WAVE_GAP_PTS = {"NIFTY": 75, "BANKNIFTY": 150, "SENSEX": 300}
WAVE_VWAP_FILTER = {"NIFTY": True, "BANKNIFTY": False, "SENSEX": True}
WAVE_SL_MULT = {"NIFTY": 0.70, "BANKNIFTY": 0.65, "SENSEX": 0.55}
WAVE_TARGET_MULT = 1.40

# ====================================================================
# CAPITAL ALLOCATION & RISK MANAGEMENT
# ====================================================================
TOTAL_CAPITAL = 600000
EQUITY_CAPITAL = 1000000  # v19: Paper trade - no capital lock        # Rs 3L for NIFTY, BANKNIFTY, SENSEX
COMMODITY_CAPITAL = 1000000  # v19: Paper trade - no capital lock     # Rs 3L for GOLDM, SILVERM, CRUDEOILM
MAX_RISK_PCT = 25              # Max 25% of segment capital per trade
MAX_EQUITY_PER_TRADE = 500000  # v19: No per-trade cap for paper
MAX_COMMODITY_PER_TRADE = 500000  # v19: No per-trade cap for paper
MAX_DAILY_LOSS_EQUITY = 500000  # v19: Paper trade - high limit
MAX_DAILY_LOSS_COMMODITY = 500000  # v19: Paper trade - high limit
MAX_POSITIONS_PER_SYMBOL = 10  # v19: Allow more concurrent positions  # Prevent cascade (e.g., 8 BANKNIFTY trades in 4 minutes)

# ====================================================================
# PER-STRATEGY TRACKING (v7.2) — MONITORING ONLY, NO HARD CAPS
# ====================================================================
# Rs 3L is a SHARED POOL across all equity strategies.
# Strategies compete for the same capital — no per-strategy hard limits.
# Allocation percentages are for MONITORING/LOGGING only.
STRATEGY_ALLOCATION = {
    'CPR':          0.25,    # 25% target — solid 65% WR, consistent
    'Gamma Blast':  0.30,    # 30% target — best risk-adjusted (Sharpe 4.42)
    'Ghost Zone':   0.15,    # 15% target — v7 institutional methodology
    'PCR+VWAP':     0.10,
    'Wave':         0.20,    # v17: Wave Extractor v3    # 10% target — needs live OI data to prove
    'Trend Rider':  0.20,    # 20% target
    'Liquidity Sweep': 0.15,  # 15% target — SL hunt reversal (PF 2.95, WR 78%) — NEW: trend day riding (PF 5.56 in backtest)
    # 'Survivor':   0.00,    # HALTED — needs Rs 1L+ per symbol
}

def get_strategy_used_capital(positions, strategy_name):
    """Calculate capital currently used by a specific strategy (for logging only)."""
    used = 0
    for p in positions:
        if p.get('strategy', '') != strategy_name:
            continue
        if p.get('is_sell', False):
            used += MARGIN_PER_LOT.get(p['symbol'], 120000) * p.get('num_lots', 1)
        else:
            used += p['entry_premium'] * p.get('lot_size', LOT_SIZES.get(p['symbol'], 50))
    return used

# v2.5.3: Tiered lot sizing — scale lots by signal quality + available capital
MAX_LOTS = {'NIFTY': 3, 'BANKNIFTY': 3, 'SENSEX': 4}  # Hard cap on lots per trade
LOT_TIER_ELITE = 80     # Score >= 80: allocate 30% of available capital
LOT_TIER_STRONG = 60    # Score >= 60: allocate 20% of available capital
LOT_TIER_STANDARD = 50  # Score >= 50: allocate 10% of available capital (MIN_SIGNAL_SCORE)

# OI + IV Exit Thresholds (INCREASED — 15% was too aggressive, caused 82% premature exits)
OI_SURGE_PCT = 35                # Exit if OI changes >35% from entry (was 15%)
OI_SURGE_FIRST_HOUR_PCT = 50     # 9:15-10:15 AM: first hour OI shifts are massive
OI_SURGE_MID_DAY_PCT = 35        # 10:15 AM - 2:00 PM: normal session
OI_SURGE_LAST_HOUR_PCT = 40      # 2:00-3:30 PM: close compression
OI_REVERSE_PCT = 50              # Reverse trade if OI changes >50% (was 25%)
IV_SPIKE_PCT = 30                # Exit if IV changes >30% from entry (was 25%)
IV_SPIKE_PCT_BANKNIFTY = 55      # v2.5: Raised from 45 (BANKNIFTY inherently volatile)
IV_REVERSE_PCT = 50              # Reverse trade if IV changes >50% (was 40%)
OI_IV_COMBO_OI = 20              # Combined exit: OI >20% AND IV >25% (was 10%/15%)
OI_IV_COMBO_IV = 25
GAMMA_SHIELD_THRESHOLD = 0.002   # Exit short positions if gamma > this

# v2.4: Strategy-specific OI/IV exit multipliers
# Ghost Zone exits faster (worst performer), Survivor tolerates more (benefits from decay)
STRATEGY_EXIT_MULT = {
    'CPR':          {'oi': 1.0, 'iv': 1.0},
    'Gamma Blast':  {'oi': 1.0, 'iv': 1.0},
    'Ghost Zone':   {'oi': 0.7, 'iv': 0.8},    # 30% LOWER threshold → exits faster
    'PCR+VWAP':     {'oi': 1.0, 'iv': 1.0},
    'Trend Rider':  {'oi': 2.0, 'iv': 2.0},
    'Liquidity Sweep': {'oi': 1.5, 'iv': 1.5},  # Medium tolerance — fast trades    # 2x HIGHER threshold — let trends run
    'Survivor':     {'oi': 2.0, 'iv': 2.0},    # 2x HIGHER threshold → tolerates swings
}

# Trade Quality Filters (eliminate brokerage-losing weak trades)
MIN_PREMIUM_BUY = 15             # Min Rs 15 premium for BUY trades (was Rs 3)
MIN_PREMIUM_BUY_BN = 40          # v11.1: BANKNIFTY min premium Rs 40 (premiums 3-5x NIFTY)
MIN_PREMIUM_SELL = 20            # Min Rs 20 premium for SELL trades (was Rs 5)
MIN_SIGNAL_SCORE = 50            # v2.5: Quality score 0-100, reject below 50 (was 40)

# v11.1: Drawdown-reduction filters (backtest-validated: +Rs 10,671 PnL, -52% BANKNIFTY DD)
CPR_MIN_WIDTH_PCT = 0.03         # Skip CPR signals when TC-BC width < 0.03% of pivot
CHOPPY_DAY_EFF_THRESHOLD = 15    # Block late entries if 11:00 efficiency < 15%
CHOPPY_DAY_BLOCK_AFTER = dtime(11, 0)  # Block new entries after 11:00 on choppy days
DIRECTION_FLIP_MIN_SCORE = 70    # v7.6.2: Higher bar for DIRECTION_FLIP (closing existing to flip)

# v7.7: Signal-Based Hold Score — controls exits
HOLD_SCORE_STRONG = 60           # >= 60: Raise TSL aggressively, override TIME_EXIT/OI_SURGE
HOLD_SCORE_WEAK = 40             # < 40: Allow early exit on losing positions
HOLD_SCORE_MIN_HOLD_MINS = 30    # Minimum hold time before computing hold score
MIN_PROFIT_TO_COST_RATIO = 2.0   # Expected profit must be >= 2x total brokerage cost

# VIX-Adaptive Trading
VIX_LOW_THRESHOLD = 14           # Below 14 = low vol regime → increase filters 1.5x
VIX_HIGH_THRESHOLD = 20          # Above 20 = high vol regime → relax filters 0.8x
VIX_LOW_MULTIPLIER = 1.5         # Multiply thresholds by 1.5 in low VIX
VIX_HIGH_MULTIPLIER = 0.8        # Multiply thresholds by 0.8 in high VIX
# v10.6: VIX Hard Gate — block ALL entries in extreme VIX regimes
VIX_BLOCK_HIGH = 35              # VIX > 35: too volatile, block entries
VIX_BLOCK_LOW = 11               # VIX < 11: no movement expected, block entries
# v10.6: Greeks Refresh + Theta Decay Exit
GREEKS_REFRESH_INTERVAL_SECONDS = 10    # v15: Refresh Greeks every 10s (was 300s — pure BS math, zero API cost)
THETA_BURDEN_TIGHTEN_PCT = 5.0   # >5%/day theta burden → tighten SL to 90%
THETA_BURDEN_EXIT_PCT = 999.0    # DISABLED (was 10%) — backtest proved it kills winning trades in 3 min
THETA_TIGHTEN_SL_FACTOR = 0.90   # SL set to 90% of current premium
# v10.6: PCR Shift Monitor (equity only)
PCR_SHIFT_MIN_HOLD_MINUTES = 15
PCR_SHIFT_SL_TIGHTEN_PCT = 0.20  # Tighten SL by 20% when PCR opposes trade

# Cooldowns & Limits
GRACE_PERIOD_SECONDS = 180       # v7.5: 3 min (was 10 min — missed fast spikes)
GHOST_ZONE_COOLDOWN_SECONDS = 1800  # 30 min after Ghost Zone loss, no re-entry same direction
REENTRY_COOLDOWN_SECONDS = 600   # 10 min after any exit before re-entering same symbol
DIRECTION_FLIP_COOLDOWN_SECONDS = 900  # v9.2: 15 min after DIRECTION_FLIP, no re-entry ANY direction
MAX_TRADES_PER_DAY = 999  # v19: No daily trade limit for paper          # Hard cap on daily equity trades
MAX_SAME_DIRECTION_PER_SYMBOL = 5  # v9.5: Max BUY_CE or BUY_PE per symbol per day (across all strategies)
SCORE_ESCALATION_PER_REENTRY = 15  # v9.5: Each re-entry same strat+dir needs +15 score
MIN_OI_EXIT_PNL = 80             # Min Rs 80 PnL to allow OI/IV exit (covers 2x brokerage)

# v7.5: Trailing Stop Loss — based on PREMIUM GAIN % (not target distance %)
# Old system: TSL thresholds tied to target distance. With 2.5x targets,
# needed 45% premium gain just to lock breakeven → never triggered.
# New system: TSL triggers on actual premium gain from entry.
# v13.0: TSL phases — TIGHTENED to capture more peak profit
# Data shows avg peak capture was only 11% — trades that peaked at +21% exited at +1.5%
# Problem: trail distances too wide, breakeven lock too late
TSL_MICRO_GAIN_PCT = 5               # Activate at 5% peak gain
TSL_MICRO_TRAIL_DISTANCE_PCT = 45    # v13: Keep 55% of peak gain (was 40% — too loose)
TSL_MICRO_MIN_HOLD_SECONDS = 180     # v13: 3 min hold (was 5 min — missed fast peaks)

TSL_BREAKEVEN_GAIN_PCT = 10      # v13: Lock breakeven at 10% gain (was 15% — too late)
TSL_TRAIL_GAIN_PCT = 20          # v13: Start trailing at 20% gain (was 25%)
TSL_TRAIL_DISTANCE_PCT = 20      # v13: Trail 20% below peak (was 30% — too loose)
TSL_TIGHT_GAIN_PCT = 35          # v13: Tight trail at 35% gain (was 40%)
TSL_TIGHT_DISTANCE_PCT = 12      # v13: Trail 12% below peak (was 20% — captures more)

# v10.1: Trailing Target (replaces hard TARGET_HIT exit)
TARGET_TRAIL_ENABLED = True
TARGET_TRAIL_EXTEND_PCT = 20          # Extend target by 20% of current premium when hit
TARGET_TRAIL_TSL_DISTANCE_PCT = 15    # TSL at 15% below peak after target hit
TARGET_TRAIL_MAX_EXTENSIONS = 5       # Max extensions (safety cap)

# v13.0: Breakout Failure Detection — RELAXED to prevent premature exits
# Data shows BREAKOUT_FAIL_REVERSE lost Rs -8,632 across 8 trades (0% WR on reverses)
# Problem: 3-5 min exits + wrong-direction reversals destroy PnL
BREAKOUT_FAIL_CHECK_MINUTES = 9999  # v16.1: DISABLED — cost Rs -34K across 25 exits (24% WR)       # v13: Extended from 15 min — give trades time to develop
BREAKOUT_FAIL_MIN_GAIN_PCT = 1        # v13: Reduced from 2% — don't exit nearly-flat positions
BREAKOUT_FAIL_REVERSE_DROP_PCT = 25   # v13: Increased from 15% — only reverse on severe failures
BREAKOUT_FAIL_REVERSE_ENABLED = False

# v13.3: Volatility-adjusted SL — scale SL by inverse ATR ratio
# BANKNIFTY 2.7x more volatile than NIFTY, needs tighter SL
# Simulation: saves Rs +8,638/week across BANKNIFTY + SENSEX
VOLATILITY_SL_ENABLED = True
ATR_REFERENCE = {
    'NIFTY': 313, 'BANKNIFTY': 851, 'SENSEX': 1094,
    'GOLDM': 3322, 'SILVERM': 9537, 'CRUDEOILM': 409,
}
ATR_BASE_SYMBOL = 'NIFTY'  # Reference for scaling

# v13.3: Momentum reversal exit
# If spot moves 0.4% AGAINST trade direction after 20min, exit
# Simulation: saves Rs +6,974, hurts ZERO winning trades
MOMENTUM_EXIT_ENABLED = False  # v16.2: DISABLED — 0% WR, Rs -9,559 lost across 4 trades
MOMENTUM_EXIT_SPOT_PCT = 0.4
MOMENTUM_EXIT_MIN_MINUTES = 20

# v11: TREND RIDER — 5th strategy for riding big trend days
TR_ENTRY_TIME = dtime(10, 15)          # Enter after 1-hour confirmation
TR_LAST_ENTRY_TIME = dtime(10, 30)     # Must enter by 10:30
TR_MAX_GAP_PCT = 1.5                   # Skip gaps > 1.5% (reversal risk)
TR_MIN_SCORE = 5                       # Need 5/10+ signal score
TR_WIDE_TSL_TRIGGER_PCT = 35           # Start trailing after 35% gain
TR_WIDE_TSL_DISTANCE_PCT = 28          # Trail 28% from peak (wide)
TR_TARGET_MULT = 1.5                   # Target = 1.5x premium
TR_SL_MULT = 0.65                      # SL at 65% of entry (35% loss allowed)

# v10.3: Import regime-aware parameter function
from market_regime import get_regime_params  # noqa: E402

# v3.1: Strategy weights from Angel One backtest (PnL + risk-adjusted)
# Survivor highest PnL (Rs 36.5M avg, 257% annual), Gamma Blast best risk-adjusted (Sharpe 4.42)
STRATEGY_WEIGHTS = {
    'Survivor': 0.40,     # 40% — highest absolute PnL, 91% win rate, 257% annual
    'Gamma Blast': 0.30,  # 30% — best Sharpe (4.42), lowest DD (1.4%), 91% win rate
    'CPR': 0.20,          # 20% — solid 70% win rate, consistent across symbols
    'PCR+VWAP': 0.05,     # 5% — needs real OI data (0 trades in daily backtest)
    'Ghost Zone': 0.05,   # 5% — needs intraday data (0 trades in daily backtest)
}

# Market hours (IST)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
PRE_MARKET = dtime(9, 0)
LAST_ENTRY_TIME = dtime(14, 30)  # v2.5.2: No new entries after 2:30 PM (need 50 min to develop)
EQUITY_FIRST_TRADE_TIME = dtime(9, 16)  # v16.1: Changed from 09:30 to 09:16 — 9AM hour has 59% WR (best) — first 15 min inflated premiums/OI spikes

# Angel API config
ANGEL_CRED_FILE = os.environ.get('ANGEL_CRED_FILE', r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt")

# ====================================================================
# LOGGING SETUP
# ====================================================================
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'paper_trade_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('PaperTrader')


# ====================================================================
# BLACK-SCHOLES ENGINE
# ====================================================================
from scipy.stats import norm

def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option pricing with Greeks."""
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    S = max(S, 0.01)
    K = max(K, 0.01)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CE':
        price = S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
        delta = -norm.cdf(-d1)
    gamma = norm.pdf(d1) / (S*sigma*np.sqrt(T))
    theta = -(S*sigma*norm.pdf(d1))/(2*np.sqrt(T)) - r*K*np.exp(-r*T)*norm.cdf(d2 if opt_type=='CE' else -d2)
    theta /= 365
    vega = S*np.sqrt(T)*norm.pdf(d1) / 100
    return {'price': max(price, 0.05), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega, 'iv': sigma}


def implied_vol(market_price, S, K, T, r, opt_type='CE', max_iter=50, tol=1e-4):
    """Back-solve for implied volatility from real market option price.
    Uses Newton-Raphson method with vega as derivative.
    Returns IV (decimal) or None if convergence fails.
    """
    if market_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    # Intrinsic value check
    if opt_type == 'CE':
        intrinsic = max(S - K * np.exp(-r * T), 0)
    else:
        intrinsic = max(K * np.exp(-r * T) - S, 0)
    if market_price < intrinsic * 0.9:
        return None  # Price below intrinsic, likely bad data

    # Initial guess: use ATM approximation (Brenner-Subrahmanyam)
    sigma = market_price / S * np.sqrt(2 * np.pi / T)
    sigma = max(min(sigma, 3.0), 0.01)  # Clamp between 1% and 300%

    for _ in range(max_iter):
        g = bs_greeks(S, K, T, r, sigma, opt_type)
        diff = g['price'] - market_price
        vega_full = g['vega'] * 100  # vega was divided by 100
        if abs(vega_full) < 1e-10:
            break
        sigma -= diff / vega_full
        sigma = max(min(sigma, 3.0), 0.005)
        if abs(diff) < tol:
            return sigma
    # Return best guess even if not fully converged
    return sigma if 0.005 < sigma < 3.0 else None


def greeks_from_market_price(market_price, S, K, T, r, opt_type='CE'):
    """Compute accurate Greeks by first solving for IV from real market LTP.
    Returns full Greeks dict with real IV, or None if IV solve fails.
    """
    iv = implied_vol(market_price, S, K, T, r, opt_type)
    if iv is None:
        return None
    g = bs_greeks(S, K, T, r, iv, opt_type)
    g['iv'] = iv  # Store the real implied volatility
    g['price'] = market_price  # Use actual market price, not BS-computed
    return g


def calc_costs(premium, qty, is_sell=False):
    """Real trading costs."""
    turnover = premium * qty
    brokerage = BROKERAGE * 2
    stt = turnover * STT_SELL if is_sell else 0
    exchange = turnover * EXCHANGE_CHARGES
    gst = brokerage * GST_RATE
    return brokerage + stt + exchange + gst


def passes_profit_filter(premium, lot_size, target_multiplier, is_sell=False):
    """Check if expected profit exceeds 2x brokerage cost.
    Returns: (passes, expected_profit, total_cost)
    """
    total_cost = calc_costs(premium, lot_size, is_sell)
    if is_sell:
        expected_profit = premium * lot_size * (1 - target_multiplier)
    else:
        expected_profit = premium * lot_size * (target_multiplier - 1)
    passes = expected_profit >= (total_cost * MIN_PROFIT_TO_COST_RATIO)
    return passes, round(expected_profit, 2), round(total_cost, 2)


def validate_signal_direction(signal_type, spot, ohlc, indicators):
    """v10.1: Multi-factor direction validation — THE PRIMARY GATE for all signals.

    CE (bullish) requires majority of: spot>VWAP, spot>prev_close, spot>open,
    EMA(9)>EMA(20), positive 3-bar trend.
    PE (bearish) requires the opposite.

    Returns (is_valid, reason_str, score_adjustment).
    Must pass at least 3 of 5 checks for entry.
    """
    is_ce = 'CE' in signal_type
    is_sell = 'SELL' in signal_type
    if is_sell:
        return True, "SELL_EXEMPT", 0  # Sell strategies have their own direction logic

    vwap = indicators.get('vwap', 0)
    prev_close = indicators.get('prev_close', spot)
    ema_9 = indicators.get('ema_9', 0)
    ema_20 = indicators.get('ema_20', 0)
    three_bar = indicators.get('three_bar_trend', 0)
    open_price = ohlc.get('open', spot) if ohlc else spot

    checks = []
    passed = 0

    # Check 1: VWAP alignment (spot vs 5-day VWAP)
    if vwap and vwap > 0:
        if (is_ce and spot > vwap) or (not is_ce and spot < vwap):
            passed += 1
            checks.append('VWAP+')
        else:
            checks.append('VWAP-')

    # Check 2: Day trend (spot vs prev close)
    body = spot - prev_close
    if (is_ce and body > 0) or (not is_ce and body < 0):
        passed += 1
        checks.append('TREND+')
    else:
        checks.append('TREND-')

    # Check 3: Intraday direction (spot vs today's open)
    intraday = spot - open_price
    if (is_ce and intraday > 0) or (not is_ce and intraday < 0):
        passed += 1
        checks.append('INTRA+')
    else:
        checks.append('INTRA-')

    # Check 4: EMA trend (9 vs 20)
    if ema_9 > 0 and ema_20 > 0:
        if (is_ce and ema_9 > ema_20) or (not is_ce and ema_9 < ema_20):
            passed += 1
            checks.append('EMA+')
        else:
            checks.append('EMA-')

    # Check 5: Multi-bar momentum (3-bar trend)
    if (is_ce and three_bar >= 2) or (not is_ce and three_bar <= -2):
        passed += 1
        checks.append('MOM+')
    elif three_bar == 0:
        checks.append('MOM~')  # Neutral, don't penalize
    else:
        checks.append('MOM-')

    # MUST pass at least 3 of 5 checks
    is_valid = passed >= 3
    score_adj = (passed - 3) * 5  # +5 per extra check, 0 at threshold

    reason = f"DIR({passed}/5: {' '.join(checks)})"
    return is_valid, reason, score_adj


def compute_signal_score(signal, spot, indicators, vix=None):
    """Compute quality score (0-100) for a trading signal.
    Factors: delta, ATR-premium ratio, VIX level, momentum, PCR.
    """
    score = 0
    greeks = signal.get('greeks', {})
    premium = signal.get('premium', 0)
    atr = indicators.get('atr', 1)
    is_buy = 'BUY' in signal.get('type', '')
    is_ce = 'CE' in signal.get('type', '')

    # 1. DELTA (0-25): sweet spot scoring
    delta = abs(greeks.get('delta', 0))
    if is_buy:
        if 0.35 <= delta <= 0.65:
            score += 25
        elif 0.25 <= delta < 0.35 or 0.65 < delta <= 0.75:
            score += 18
        elif 0.15 <= delta < 0.25:
            score += 10
        else:
            score += 5
    else:
        if 0.10 <= delta <= 0.25:
            score += 25
        elif 0.25 < delta <= 0.35:
            score += 18
        else:
            score += 10

    # 2. ATR-PREMIUM RATIO (0-20)
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

    # 3. VIX LEVEL (0-20)
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

    # 4. MOMENTUM (0-20): body direction matches signal
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

    # 5. PCR CONFIRMATION (0-15)
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

    # v3.1: DATA QUALITY PENALTY — penalize when critical market data is missing
    data_quality = indicators.get('_data_quality', {})
    if not data_quality.get('has_iv', True):
        score -= 15  # No IV data = blind entry, heavy penalty
    if not data_quality.get('has_pcr', True):
        score -= 10  # No PCR = no sentiment confirmation
    # Missing VWAP is less critical, small penalty
    if not data_quality.get('has_vwap', True):
        score -= 5

    return max(min(score, 100), 0)


def compute_direction_confidence(sig, spot, indicators, vix, greeks):
    """v10.4: Direction Confidence Index (DCI) — continuous 0-100 score.

    Uses ALL 8 available signal inputs mathematically weighted by their
    backtest-proven correlation with profitable trades.
    Higher = stronger conviction in the signal direction.

    Components:
      VWAP position     (±10): Distance from VWAP = institutional bias
      EMA trend         (±12): EMA9 vs EMA20 crossover + gap size
      3-bar momentum    (±8):  Recent candle direction alignment
      PCR sentiment     (±8):  Put-Call Ratio institutional positioning
      IV signal strength(±10): Higher IV = bigger premium moves
      Delta quality     (±5):  ATM delta = best gamma exposure
      Intraday body     (±5):  Spot vs open confirms direction
      Theta burden      (-8):  Penalty for BUY on high theta near expiry
    """
    is_ce = 'CE' in sig.get('type', '')
    score = 50  # Neutral baseline

    # 1. VWAP POSITION (±10): Distance from VWAP indicates institutional bias
    vwap = indicators.get('vwap', spot) if indicators else spot
    vwap_dist_pct = (spot - vwap) / vwap * 100 if vwap else 0
    if is_ce:
        score += min(max(vwap_dist_pct * 5, -10), 10)
    else:
        score += min(max(-vwap_dist_pct * 5, -10), 10)

    # 2. EMA TREND (±12): EMA9 vs EMA20 crossover + gap size
    ema9 = indicators.get('ema_9', spot) if indicators else spot
    ema20 = indicators.get('ema_20', spot) if indicators else spot
    ema_gap_pct = (ema9 - ema20) / ema20 * 100 if ema20 else 0
    if is_ce:
        score += min(max(ema_gap_pct * 10, -12), 12)
    else:
        score += min(max(-ema_gap_pct * 10, -12), 12)

    # 3. 3-BAR MOMENTUM (±8): Recent candle direction alignment
    trend = indicators.get('three_bar_trend', 0) if indicators else 0
    if is_ce:
        score += min(trend * 3, 8)
    else:
        score += min(-trend * 3, 8)

    # 4. PCR SENTIMENT (±8): Put-Call Ratio institutional positioning
    pcr = indicators.get('pcr', 1.0) if indicators else 1.0
    if is_ce and pcr > 1.0:
        score += min((pcr - 1.0) * 20, 8)
    elif not is_ce and pcr < 1.0:
        score += min((1.0 - pcr) * 20, 8)

    # 5. IV SIGNAL STRENGTH (±10): Higher IV = bigger premium moves
    iv = greeks.get('iv', 0) if greeks else 0
    if iv > 0:
        if iv >= 0.40:
            score += 10
        elif iv >= 0.25:
            score += 5
        elif iv < 0.15:
            score -= 10
        elif iv < 0.20:
            score -= 5

    # 6. DELTA QUALITY (±5): ATM delta = best gamma exposure
    delta = abs(greeks.get('delta', 0)) if greeks else 0
    if 0.40 <= delta <= 0.60:
        score += 5
    elif delta < 0.25 or delta > 0.75:
        score -= 5

    # 7. INTRADAY BODY (±5): Spot vs open confirms direction
    open_price = indicators.get('open', spot) if indicators else spot
    body_pct = (spot - open_price) / open_price * 100 if open_price else 0
    if is_ce:
        score += min(max(body_pct * 3, -5), 5)
    else:
        score += min(max(-body_pct * 3, -5), 5)

    # 8. THETA BURDEN (penalty for BUY on high theta near expiry)
    theta = greeks.get('theta', 0) if greeks else 0
    dte = sig.get('dte', 5)
    if dte <= 1 and abs(theta) > 0:
        premium = sig.get('premium', 100)
        theta_pct_per_hour = abs(theta) / premium * 100 / 6.5 if premium > 0 else 0
        if 'BUY' in sig.get('type', '') and theta_pct_per_hour > 3:
            score -= 8

    return max(min(score, 100), 0)


# v10.4 constants for DCI and IV gates
DCI_MIN_THRESHOLD = 40          # Minimum DCI to enter a trade
IV_MIN_FOR_DTE1 = 0.20          # Minimum IV for DTE=1 trades
IV_MIN_FOR_DTE0 = 0.25          # Minimum IV for expiry-day trades


def compute_hold_score(pos, spot, indicators, current_oi=None, current_iv=None):
    """v7.7: Score 0-100 — how strongly signals support STAYING in this trade.

    Factors:
      Signal alignment (0-30): Is spot still on our side of CPR pivot?
      Spot momentum    (0-25): Is spot moving in our trade's direction?
      Premium health   (0-20): Is premium growing or stable?
      OI trend         (0-15): Is OI accumulating (increasing)?
      IV stability     (0-10): Is IV stable (not spiking against us)?

    Thresholds:
      >= HOLD_SCORE_STRONG (60): Raise TSL aggressively, override TIME_EXIT
      40-59: Moderate hold, normal TSL behaviour
      < HOLD_SCORE_WEAK (40): Allow early exit on losers
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
            # else: spot below CPR = signal reversed, 0 pts
        else:
            if spot < pivot:
                score += 30
            elif spot < tc:
                score += 15

    # 2. Spot momentum (0-25): Is spot moving in our direction?
    entry_spot = pos.get('entry_spot', spot)
    atr = indicators.get('atr', 100) if indicators else 100
    spot_move = spot - entry_spot
    # Normalize move by ATR: 1 ATR move = 25 points
    if atr > 0:
        normalized = abs(spot_move) / atr
        if (is_bullish and spot_move > 0) or (not is_bullish and spot_move < 0):
            score += min(25, int(normalized * 25))
        # Spot moving AGAINST us = 0 pts (no penalty, just no bonus)

    # 3. Premium health (0-20): Is premium gaining?
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

    # 4. OI trend (0-15): Increasing OI = accumulation = bullish hold
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

    # 5. IV stability (0-10): Stable IV = predictable environment
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
# ANGEL API CONNECTION
# ====================================================================

# v16: 14 ML features for future ML Oracle training

# v16: Signal Strength Score (0-100) — LOGGING ONLY
# Combines quality_score + Greeks + regime + VIX + OI + data source
def compute_signal_strength(sig, spot, indicators, vix=None, ltp_source='UNKNOWN'):
    """Compute signal strength 0-100 and label. For logging/analysis only."""
    try:
        greeks = sig.get('greeks', {})
        q_score = sig.get('quality_score', 0)

        # 1. Quality Score (40% weight, max 40 points)
        q_pts = min(40, q_score * 0.4)

        # 2. Greeks Quality (15% weight, max 15 points)
        delta = abs(greeks.get('delta', sig.get('delta', 0)))
        gamma = abs(greeks.get('gamma', sig.get('gamma', 0)))
        g_pts = 0
        if 0.35 <= delta <= 0.65:
            g_pts += 8  # delta in sweet spot
        elif 0.25 <= delta <= 0.75:
            g_pts += 4
        if gamma > 0.001:
            g_pts += 7  # meaningful gamma
        elif gamma > 0.0005:
            g_pts += 3

        # 3. Regime Score (15% weight, max 15 points)
        eff = 0
        if indicators:
            eff = indicators.get('efficiency', 0)
            if isinstance(eff, (int, float)) and eff > 1:
                eff = eff / 100
        r_pts = min(15, eff * 30) if eff > 0 else 5  # trending = higher score

        # 4. VIX Alignment (10% weight, max 10 points)
        v_pts = 5  # default neutral
        if vix:
            if 14 <= vix <= 28:
                v_pts = 10  # ideal range
            elif 11 <= vix <= 35:
                v_pts = 6   # acceptable
            else:
                v_pts = 2   # extreme VIX

        # 5. OI Confirmation (10% weight, max 10 points)
        oi = sig.get('oi', 0)
        oi_pts = 5  # default
        if oi and oi > 1000:
            oi_pts = 10  # good OI = liquid
        elif oi and oi > 100:
            oi_pts = 7

        # 6. Data Source Quality (10% weight, max 10 points)
        source_scores = {
            'ZERODHA': 10,
            'TRUEDATA': 8,
            'NSE_PIPELINE': 6,
            'ANGEL': 4,
            'UNKNOWN': 3,
        }
        s_pts = source_scores.get(ltp_source, 3)

        # Total
        strength = int(q_pts + g_pts + r_pts + v_pts + oi_pts + s_pts)
        strength = max(0, min(100, strength))

        # Label
        if strength >= 80:
            label = 'ELITE'
        elif strength >= 60:
            label = 'STRONG'
        elif strength >= 50:
            label = 'MODERATE'
        else:
            label = 'WEAK'

        return strength, label
    except Exception:
        return 0, 'UNKNOWN'


def compute_ml_features(sig, ohlc, spot, indicators, vix=None):
    """Compute 14 ML features for signal logging."""
    f = {}
    try:
        greeks = sig.get('greeks', {})
        atr = indicators.get('atr', 1) if indicators else 1
        f['ml_cpr_width_pct'] = indicators.get('cpr_width', 0) if indicators else 0
        day_open = ohlc.get('open', spot) if ohlc else spot
        f['ml_body_atr_ratio'] = abs(spot - day_open) / atr if atr > 0 else 0
        f['ml_pcr_shift'] = indicators.get('pcr_shift', 0) if indicators else 0
        f['ml_efficiency_ratio'] = indicators.get('efficiency', 0.5) if indicators else 0.5
        f['ml_gamma'] = abs(greeks.get('gamma', sig.get('gamma', 0)))
        f['ml_delta'] = abs(greeks.get('delta', sig.get('delta', 0)))
        theta = abs(greeks.get('theta', sig.get('theta', 0)))
        dte = sig.get('dte', 1) or 1
        f['ml_theta_burden'] = theta * dte
        f['ml_vix_level'] = vix if vix else 0
        eff = f['ml_efficiency_ratio']
        body_ratio = min(1.0, f['ml_body_atr_ratio'])
        f['ml_regime_score'] = 0.6 * body_ratio + 0.4 * eff
        f['ml_oi_change_15m'] = indicators.get('oi_change', 0) if indicators else 0
        iv = greeks.get('iv', sig.get('iv', 0))
        if isinstance(iv, (int, float)) and iv > 0:
            if iv > 1: iv = iv / 100
            f['ml_iv_rank'] = min(100, max(0, iv * 200))
        else:
            f['ml_iv_rank'] = 50
        atr_ref = {'NIFTY': 313, 'BANKNIFTY': 851, 'SENSEX': 1094,
                    'GOLDM': 3322, 'SILVERM': 9537, 'CRUDEOILM': 409}
        sym_atr = atr_ref.get(sig.get('symbol', ''), atr)
        f['ml_hold_score'] = (sym_atr / (0.01 * spot) * 60) if spot > 0 else 60
        strat_map = {'CPR': 0, 'Gamma Blast': 1, 'Ghost Zone': 2,
                      'PCR+VWAP': 3, 'Trend Rider': 4, 'Liquidity Sweep': 5}
        f['ml_strategy_id'] = strat_map.get(sig.get('strategy', ''), 6)
        f['ml_gamma_blast_strength'] = f['ml_gamma'] * f['ml_body_atr_ratio']
    except Exception:
        pass
    return f

class AngelConnection:
    """Manages Angel One SmartAPI connection."""

    def __init__(self):
        self.obj = None
        self.session = None
        self.instruments = None
        self._connected = False
        self._last_api_call = 0  # Timestamp of last REST API call
        self._api_min_interval = 1.0  # v7.7: Minimum 1s between REST calls (was 0.5s, caused AB1004)
        self._backoff_until = 0  # v7.7: Exponential backoff timestamp
        self._auth_time = 0  # v9.2: Track when we last authenticated
        self._app_type = 'Historical'  # v9.2: Remember app type for reconnect
        self._reconnecting = False  # v9.2: Prevent recursive reconnect loops

    def load_credentials(self):
        """Load Angel credentials."""
        creds = {}
        current_app = {}
        with open(ANGEL_CRED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    raw_val = val.strip()
                    if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                        parts = raw_val.split('#')
                        comment_val = parts[-1].strip()
                        main_val = parts[0].strip()
                        if 'your_' in main_val.lower() or 'secret' in main_val.lower():
                            val = comment_val
                        else:
                            val = main_val
                    else:
                        val = raw_val.split('#')[0].strip()
                    current_app[key] = val
                    if key == 'BROKER_NAME':
                        app_type = current_app.get('ANGEL_APP_TYPE', 'Unknown')
                        creds[app_type] = current_app.copy()
                        current_app = {}
        return creds

    def _throttle(self):
        """Enforce minimum interval between REST API calls to avoid rate limiting."""
        # v7.7: Respect exponential backoff if active
        now = time.time()
        if now < self._backoff_until:
            wait = self._backoff_until - now
            logger.debug(f"API backoff: waiting {wait:.1f}s")
            time.sleep(wait)
        elapsed = time.time() - self._last_api_call
        if elapsed < self._api_min_interval:
            time.sleep(self._api_min_interval - elapsed)
        self._last_api_call = time.time()

    def _handle_rate_limit(self, error_msg=''):
        """v7.7: Exponential backoff on rate limit (AB1004/TooManyRequests)."""
        backoff = min(30, max(2, (self._backoff_until - time.time()) * 2 + 2))
        self._backoff_until = time.time() + backoff
        logger.warning(f"API RATE_LIMIT: backing off {backoff:.0f}s — {error_msg}")
        try:
            from trade_notifier import notify_api_rate_limit
            notify_api_rate_limit('EQUITY', 'REST API', error_msg)
        except Exception:
            pass

    def _is_token_expired(self, data):
        """v9.2: Check if API response indicates expired/invalid token."""
        if not data or not isinstance(data, dict):
            return False
        err_code = str(data.get('errorcode', ''))
        err_msg = str(data.get('message', '')).lower()
        # AG8001 = Invalid Token, AG8002 = Token Expired, AB8050 = Unauthorized
        return err_code in ('AG8001', 'AG8002', 'AB8050') or 'invalid token' in err_msg or 'token expired' in err_msg

    def reconnect(self):
        """v9.2: Re-authenticate to Angel API when token expires."""
        if self._reconnecting:
            return False  # Prevent recursive reconnect loops
        self._reconnecting = True
        logger.warning("TOKEN_EXPIRED: Attempting re-authentication...")
        try:
            from trade_notifier import notify_system_event
            notify_system_event('EQUITY', 'Angel API token expired — reconnecting...')
        except Exception:
            pass
        try:
            self._connected = False
            result = self.connect(self._app_type)
            if result:
                logger.info("TOKEN_REFRESH: Re-authentication successful")
                try:
                    from trade_notifier import notify_system_event
                    notify_system_event('EQUITY', 'Angel API reconnected successfully')
                except Exception:
                    pass
            else:
                logger.error("TOKEN_REFRESH: Re-authentication FAILED")
            return result
        finally:
            self._reconnecting = False

    def connect(self, app_type='Historical'):
        """Connect to Angel SmartAPI with retry on rate limit."""
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError:
            os.system("pip install smartapi-python pyotp logzero websocket-client")
            from SmartApi import SmartConnect
            import pyotp

        creds = self.load_credentials()
        self._app_type = app_type  # v9.2: Remember for reconnect
        app = creds.get(app_type, creds.get('Historical', {}))

        api_key = app.get('ANGEL_API_KEY', '')
        client = app.get('ANGEL_CLIENT_CODE', '')
        pin = app.get('ANGEL_PIN', '')
        totp_secret = app.get('ANGEL_TOTP_KEY', '')

        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to Angel One ({app_type})... attempt {attempt+1}/{max_retries}")
                self.obj = SmartConnect(api_key=api_key)
                totp = pyotp.TOTP(totp_secret).now()
                self.session = self.obj.generateSession(client, pin, totp)

                if self.session and self.session.get('status'):
                    self._connected = True
                    self._auth_time = time.time()  # v9.2: Track auth time
                    logger.info(f"Angel One connected: {client}")
                    return True
                else:
                    logger.warning(f"Angel login failed (attempt {attempt+1}): {self.session}")
            except Exception as e:
                logger.warning(f"Angel connection error (attempt {attempt+1}): {e}")

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s, 32s
                logger.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"Angel One connection failed after {max_retries} attempts")
        return False

    def get_ltp(self, exchange, symbol_token):
        """Get Last Traded Price."""
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.ltpData(exchange, symbol_token, symbol_token)
            if data and data.get('data'):
                return data['data'].get('ltp')
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"LTP: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.ltpData(exchange, symbol_token, symbol_token)
                    if data and data.get('data'):
                        return data['data'].get('ltp')
        except Exception as e:
            logger.error(f"LTP error: {e}")
        return None

    def get_market_data(self, exchange, symbol_token):
        """Get full market data including OI."""
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.getMarketData("FULL", {exchange: [symbol_token]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"MarketData: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getMarketData("FULL", {exchange: [symbol_token]})
                    if data and data.get('data') and data['data'].get('fetched'):
                        return data['data']['fetched'][0]
        except Exception as e:
            logger.error(f"Market data error: {e}")
        return None

    def get_option_greeks(self, name, expiry_date):
        """Get option Greeks from Angel API."""
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_date
            })
            if data and data.get('data'):
                return data['data']
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"Greeks: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.optionGreek({"name": name, "expirydate": expiry_date})
                    if data and data.get('data'):
                        return data['data']
            # v7.7: Handle specific error codes with Telegram alerts
            err_code = str(data.get('errorcode', '')) if data else ''
            err_msg = str(data.get('message', '')) if data else ''
            if 'AB9019' in err_code:
                logger.debug(f"Greeks: No data for {name} expiry {expiry_date} (AB9019)")
                # v16.2: Suppress AB9019 alert for SENSEX (Angel BFO not supported, Zerodha handles it)
                if name != 'SENSEX':
                    try:
                        from trade_notifier import notify_api_error
                        notify_api_error('EQUITY', 'optionGreek', 'AB9019',
                                        f"{name} expiry {expiry_date}: {err_msg}")
                    except Exception:
                        pass
            elif 'AB1004' in err_code:
                self._handle_rate_limit(f"optionGreek {name}")
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'rate' in err_str.lower() or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"Greeks error: {e}")
        return None

    def get_historical(self, exchange, token, interval, from_date, to_date):
        """Get historical candle data."""
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
                logger.warning(f"Historical: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.getCandleData(params)
                    if data and data.get('data'):
                        return data['data']
            # v7.7: Detect rate limit from error response
            if data and 'AB1004' in str(data.get('errorcode', '')):
                self._handle_rate_limit(str(data.get('message', '')))
        except Exception as e:
            err_str = str(e)
            if 'TooMany' in err_str or 'rate' in err_str.lower() or 'AB1004' in err_str:
                self._handle_rate_limit(err_str)
            else:
                logger.error(f"Historical error: {e}")
        return None

    def load_instruments(self):
        """Load or download instrument master."""
        cache_file = os.path.join(OPTIONS_DIR, 'instrument_master.csv')
        if os.path.exists(cache_file):
            mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
            if (datetime.now() - mod_time).days < 1:
                self.instruments = pd.read_csv(cache_file, low_memory=False)
                logger.info(f"Loaded {len(self.instruments)} instruments from cache")
                return self.instruments

        logger.info("Downloading instrument master...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        import requests
        resp = requests.get(url, timeout=60)
        self.instruments = pd.DataFrame(resp.json())
        self.instruments.to_csv(cache_file, index=False)
        logger.info(f"Downloaded {len(self.instruments)} instruments")
        return self.instruments

    def find_token(self, symbol_name, exchange='NSE'):
        """Find symbol token from instrument master."""
        if self.instruments is None:
            self.load_instruments()
        mask = (self.instruments['name'] == symbol_name) & \
               (self.instruments['exch_seg'] == exchange)
        matches = self.instruments[mask]
        if len(matches) > 0:
            return str(matches.iloc[0]['token'])
        return None

    def find_option_tokens(self, name, expiry, strike, opt_type):
        """Find option contract token. Filters by nearest expiry to avoid stale contracts."""
        if self.instruments is None:
            self.load_instruments()

        exchange = 'BFO' if name == 'SENSEX' else 'NFO'
        mask = (self.instruments['name'] == name) & \
               (self.instruments['exch_seg'] == exchange) & \
               (self.instruments['instrumenttype'].isin(['OPTIDX', 'OPTSTK'])) & \
               (self.instruments['strike'].astype(float) == float(strike * 100))

        if opt_type:
            mask = mask & (self.instruments['symbol'].str.endswith(opt_type))

        matches = self.instruments[mask]
        if len(matches) == 0:
            return None

        # Filter by nearest future expiry to avoid stale contracts
        try:
            today = datetime.now().date()
            matches = matches.copy()
            matches['expiry_date'] = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True)
            future = matches[matches['expiry_date'].dt.date >= today]
            if len(future) > 0:
                nearest_expiry = future['expiry_date'].min()
                matches = future[future['expiry_date'] == nearest_expiry]
        except Exception:
            pass  # Fall back to unfiltered matches

        if len(matches) > 0:
            return matches.iloc[0].to_dict()
        return None

    def get_pcr(self):
        """Get market PCR."""
        if not self._connected:
            return None
        try:
            data = self.obj.putCallRatio()
            if data and data.get('data'):
                return data['data']
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"PCR: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    data = self.obj.putCallRatio()
                    if data and data.get('data'):
                        return data['data']
        except Exception as e:
            logger.error(f"PCR error: {e}")
        return None

    def get_real_pcr(self, name, expiry_date):
        """v2.5.2: Compute real PCR from option chain OI data.
        PCR = Total Put OI / Total Call OI for all strikes.
        Returns float PCR or None if unavailable.
        """
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_date
            })
            # v9.2: Detect token expiry and auto-reconnect
            if self._is_token_expired(data):
                logger.warning(f"RealPCR: Token expired ({data.get('errorcode')}), reconnecting...")
                if self.reconnect():
                    self._throttle()
                    data = self.obj.optionGreek({"name": name, "expirydate": expiry_date})
            if data and data.get('data'):
                total_put_oi = 0
                total_call_oi = 0
                for strike_data in data['data']:
                    opt_type = strike_data.get('optionType', '')
                    oi = float(strike_data.get('opnInterest', 0) or 0)
                    if opt_type == 'PE':
                        total_put_oi += oi
                    elif opt_type == 'CE':
                        total_call_oi += oi
                if total_call_oi > 0:
                    pcr = total_put_oi / total_call_oi
                    logger.info(f"  REAL_PCR: {name} Put OI={total_put_oi:,.0f} Call OI={total_call_oi:,.0f} PCR={pcr:.3f}")
                    return pcr
        except Exception as e:
            logger.error(f"Real PCR error for {name}: {e}")
        return None

    def select_optimal_strike(self, name, spot, opt_type, expiry_date=None, num_strikes=3):
        """v5: Select best strike from live option chain based on OI and liquidity.

        Instead of round(spot/interval)*interval, fetches real option chain
        and picks the strike with best OI among ATM ± num_strikes.

        Args:
            name: Index name (NIFTY, BANKNIFTY, SENSEX)
            spot: Current spot price
            opt_type: 'CE' or 'PE'
            expiry_date: Expiry in DDMMMYYYY format, or None for nearest
            num_strikes: Number of strikes above/below ATM to consider

        Returns:
            dict: {strike, token, symbol, ltp, oi, volume} or None
        """
        if not self._connected:
            return None

        strike_interval = STRIKE_INTERVALS.get(name, 50)
        atm_strike = round(spot / strike_interval) * strike_interval

        # Generate candidate strikes: ATM ± num_strikes
        candidates = [atm_strike + i * strike_interval
                      for i in range(-num_strikes, num_strikes + 1)]

        best = None
        best_score = -1

        try:
            # Use optionGreek API to get OI + IV for all strikes (single API call)
            if expiry_date:
                self._throttle()
                data = self.obj.optionGreek({
                    "name": name,
                    "expirydate": expiry_date
                })
                if data and data.get('data'):
                    for strike_data in data['data']:
                        sd_type = strike_data.get('optionType', '')
                        if sd_type != opt_type:
                            continue
                        sd_strike = float(strike_data.get('strikePrice', 0) or 0)
                        if sd_strike not in candidates:
                            continue

                        oi = float(strike_data.get('opnInterest', 0) or 0)
                        ltp = float(strike_data.get('ltp', 0) or 0)
                        iv = float(strike_data.get('impliedVolatility', 0) or 0)
                        volume = float(strike_data.get('tradeVolume', 0) or 0)
                        # v10.3: Extract delta + gamma from API (was ignored before!)
                        delta = abs(float(strike_data.get('delta', 0) or 0))
                        gamma = float(strike_data.get('gamma', 0) or 0)

                        # v10.3d: Gamma-first scoring — highest gamma wins
                        delta_score = max(0, 1 - abs(delta - 0.50) * 6.67)
                        gamma_score = gamma * 10000
                        oi_norm = min(oi / 100000, 1.0)
                        vol_norm = min(volume / 10000, 1.0)
                        score = (gamma_score * 40) + (delta_score * 30) + (oi_norm * 20) + (vol_norm * 10)

                        # FILTER: Skip strikes outside delta range 0.20-0.70
                        if delta > 0 and (delta < 0.35 or delta > 0.65):
                            continue

                        # v10.3d-4: Score by delta+gamma+volume (optionGreek API returns LTP=0, OI=0)
                        # Real LTP/OI fetched after selecting best strike
                        if score > best_score and volume > 0:
                            best_score = score
                            best = {
                                'strike': sd_strike,
                                'ltp': ltp,
                                'oi': oi,
                                'iv': iv / 100 if iv > 1 else iv,
                                'volume': volume,
                                'delta': delta,
                                'gamma': gamma,
                            }

                    if best:
                        # Find token and fetch REAL market data (optionGreek gives LTP=0)
                        token_info = self.find_option_tokens(name, None, best['strike'], opt_type)
                        if token_info:
                            best['token'] = str(token_info.get('token', ''))
                            best['symbol'] = token_info.get('symbol', '')
                            # v10.3d-4: Fetch real LTP + OI from market data API
                            exchange = 'BFO' if name == 'SENSEX' else 'NFO'
                            mkt = self.get_market_data(exchange, best['token'])
                            if mkt:
                                real_ltp = float(mkt.get('ltp', 0) or 0)
                                real_oi = float(mkt.get('opnInterest', mkt.get('oi', 0)) or 0)
                                if real_ltp > 0:
                                    best['ltp'] = real_ltp
                                    best['oi'] = real_oi
                                else:
                                    logger.warning(f"  OPTGREEK_SKIP: {name} {best['strike']}{opt_type} "
                                                  f"real LTP=0 — dead option")
                                    best = None
                            else:
                                logger.warning(f"  OPTGREEK_SKIP: {name} {best['strike']}{opt_type} "
                                              f"— market data fetch failed")
                                best = None
                    if best:
                        logger.info(f"  OPTIMAL_STRIKE: {name} {opt_type} ATM={atm_strike} "
                                   f"Selected={best['strike']} Delta={best.get('delta',0):.3f} "
                                   f"Gamma={best.get('gamma',0):.6f} OI={best['oi']:,.0f} "
                                   f"LTP={best['ltp']:.2f} IV={best.get('iv', 0)*100:.1f}%")
                        return best

        except Exception as e:
            logger.error(f"Option chain strike selection error for {name}: {e}")

        # v10.3d: Try optionGreek API for SENSEX/BFO (was skipped entirely before)
        # Fall back to per-strike market data + BS greeks only if API returns empty
        if name == 'SENSEX' or best is None:
            # Step 1: Try optionGreek API (same as NFO path)
            try:
                bfo_expiry = self._get_nearest_expiry(name) if not expiry_date else expiry_date
                if bfo_expiry:
                    self._throttle()
                    bfo_data = self.obj.optionGreek({
                        "name": name,
                        "expirydate": bfo_expiry
                    })
                    if bfo_data and bfo_data.get('data') and len(bfo_data['data']) > 0:
                        for strike_data in bfo_data['data']:
                            sd_type = strike_data.get('optionType', '')
                            if sd_type != opt_type:
                                continue
                            sd_strike = float(strike_data.get('strikePrice', 0) or 0)
                            if sd_strike not in candidates:
                                continue
                            iv = float(strike_data.get('impliedVolatility', 0) or 0)
                            volume = float(strike_data.get('tradeVolume', 0) or 0)
                            delta = abs(float(strike_data.get('delta', 0) or 0))
                            gamma = float(strike_data.get('gamma', 0) or 0)
                            if delta > 0 and (delta < 0.35 or delta > 0.65):
                                continue
                            delta_score = max(0, 1 - abs(delta - 0.50) * 6.67)
                            gamma_score = gamma * 10000
                            vol_norm = min(volume / 10000, 1.0)
                            score = (delta_score * 30) + (gamma_score * 40) + (vol_norm * 10)
                            if score > best_score and volume > 0:
                                best_score = score
                                best = {
                                    'strike': sd_strike, 'ltp': 0, 'oi': 0,
                                    'iv': iv / 100 if iv > 1 else iv,
                                    'volume': volume, 'delta': delta, 'gamma': gamma,
                                }
                        if best:
                            token_info = self.find_option_tokens(name, None, best['strike'], opt_type)
                            if token_info:
                                best['token'] = str(token_info.get('token', ''))
                                best['symbol'] = token_info.get('symbol', '')
                                # v10.3d-4: Fetch real LTP + OI
                                exchange = 'BFO' if name == 'SENSEX' else 'NFO'
                                mkt = self.get_market_data(exchange, best['token'])
                                if mkt:
                                    real_ltp = float(mkt.get('ltp', 0) or 0)
                                    real_oi = float(mkt.get('opnInterest', mkt.get('oi', 0)) or 0)
                                    if real_ltp > 0:
                                        best['ltp'] = real_ltp
                                        best['oi'] = real_oi
                                    else:
                                        best = None
                                else:
                                    best = None
                        if best:
                            logger.info(f"  OPTIMAL_STRIKE: {name} {opt_type} ATM={atm_strike} "
                                       f"Selected={best['strike']} Delta={best.get('delta',0):.3f} "
                                       f"Gamma={best.get('gamma',0):.6f} OI={best['oi']:,.0f} "
                                       f"LTP={best['ltp']:.2f} IV={best.get('iv', 0)*100:.1f}% [BFO API]")
                            return best
                    else:
                        logger.info(f"  OPTGREEK_BFO_EMPTY: {name} expiry={bfo_expiry} — "
                                   f"API returned empty, trying per-strike fallback")
            except Exception as e:
                logger.warning(f"  OPTGREEK_BFO_ERR: {name} — {e}, trying per-strike fallback")

            # Step 2: Per-strike fallback with real LTP + BS greeks (only if API failed)
            try:
                expiry = self._get_nearest_expiry(name) if not expiry_date else expiry_date
                if expiry:
                    best_fallback_score = -1
                    best_strike_info = None
                    try:
                        exp_dt = datetime.strptime(expiry, '%d%b%Y')
                        dte = max((exp_dt.date() - datetime.now().date()).days, 1)
                    except Exception:
                        dte = 7
                    T_fallback = dte / 365
                    iv_default = 0.15
                    for cand_strike in candidates:
                        token_info = self.find_option_tokens(name, None, cand_strike, opt_type)
                        if token_info:
                            exchange = 'BFO' if name == 'SENSEX' else 'NFO'
                            self._throttle()
                            mkt = self.get_market_data(exchange, str(token_info.get('token', '')))
                            if mkt:
                                ltp = float(mkt.get('ltp', 0) or 0)
                                oi = float(mkt.get('opnInterest', mkt.get('oi', 0)) or 0)
                                if ltp > 0:
                                    iv_estimate = implied_vol(ltp, spot, cand_strike, T_fallback, RISK_FREE_RATE, opt_type) or iv_default
                                    g = bs_greeks(spot, cand_strike, T_fallback, RISK_FREE_RATE, iv_estimate, opt_type)
                                    delta = abs(g['delta'])
                                    gamma = g['gamma']
                                    delta_score = max(0, 1 - abs(delta - 0.50) * 6.67)
                                    gamma_score = gamma * 10000
                                    oi_norm = min(oi / 100000, 1.0)
                                    score = (delta_score * 30) + (gamma_score * 40) + (oi_norm * 20) + (0.5 * 10)
                                    if delta < 0.35 or delta > 0.65:
                                        continue
                                    if score > best_fallback_score:
                                        best_fallback_score = score
                                        best_strike_info = {
                                            'strike': cand_strike, 'ltp': ltp, 'oi': oi,
                                            'iv': iv_estimate, 'volume': 0,
                                            'delta': delta, 'gamma': gamma,
                                            'token': str(token_info.get('token', '')),
                                            'symbol': token_info.get('symbol', ''),
                                        }
                    if best_strike_info and best_strike_info['ltp'] > 0:
                        logger.info(f"  BFO_FALLBACK_STRIKE: {name} {opt_type} ATM={atm_strike} "
                                   f"Selected={best_strike_info['strike']} Delta={best_strike_info.get('delta',0):.3f} "
                                   f"Gamma={best_strike_info.get('gamma',0):.6f} OI={best_strike_info['oi']:,.0f} "
                                   f"LTP={best_strike_info['ltp']:.2f} [BS fallback]")
                        return best_strike_info
            except Exception as e:
                logger.error(f"BFO per-strike fallback error for {name}: {e}")

        # Fallback: mathematical derivation
        logger.info(f"  FALLBACK_STRIKE: {name} {opt_type} using ATM={atm_strike} (option chain unavailable)")
        return {'strike': atm_strike, 'ltp': 0, 'oi': 0, 'iv': 0, 'volume': 0}

    def check_token_health(self):
        """v9.2: Proactive token health check — call from heartbeat.
        Returns True if token is healthy, False if expired/reconnected.
        """
        if not self._connected:
            return False
        # Proactive refresh if token is older than 5 hours
        token_age_hours = (time.time() - self._auth_time) / 3600 if self._auth_time else 0
        if token_age_hours > 5:
            logger.info(f"TOKEN_HEALTH: Token age {token_age_hours:.1f}h > 5h, proactive refresh...")
            return self.reconnect()
        # Lightweight API ping to verify token is still valid
        try:
            self._throttle()
            data = self.obj.ltpData('NSE', '99926000', '99926000')  # NIFTY 50 index
            if self._is_token_expired(data):
                logger.warning(f"TOKEN_HEALTH: Token invalid (ping returned {data.get('errorcode')}), reconnecting...")
                return self.reconnect()
            return True
        except Exception as e:
            logger.warning(f"TOKEN_HEALTH: Ping failed ({e}), reconnecting...")
            return self.reconnect()


# ====================================================================
# PAPER POSITION TRACKER
# ====================================================================
class PaperPortfolio:
    """Track paper trading positions and P&L."""

    def __init__(self, capital=INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.positions = []      # Open positions
        self.closed_trades = []  # Completed trades
        self.signals = []        # All signals generated
        self.daily_pnl = {}      # Date -> PnL
        self._load_state()

    def _state_file(self):
        return os.path.join(PAPER_DIR, 'portfolio_state.json')

    def _load_state(self):
        """Load saved state."""
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
        """Save state to disk."""
        state = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat()
        }
        with open(self._state_file(), 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def add_signal(self, strategy, symbol, signal_type, strike, entry_premium,
                   delta, gamma, theta, iv, dte, details=None, oi=0, spot_price=0):
        """Record a trading signal with risk management, dynamic lot sizing, and OI/IV tracking."""
        # ---- RISK CHECK ----
        base_lot = LOT_SIZES.get(symbol, 65)
        is_sell = 'SELL' in signal_type
        is_commodity = symbol in ('GOLDM', 'SILVERM', 'CRUDEOILM', 'GOLD', 'SILVER', 'CRUDEOIL')
        max_per_trade = MAX_COMMODITY_PER_TRADE if is_commodity else MAX_EQUITY_PER_TRADE
        segment_capital = COMMODITY_CAPITAL if is_commodity else EQUITY_CAPITAL

        # Compute available capital first (needed for dynamic lot sizing)
        total_exposure = 0
        for p in self.positions:
            p_is_commodity = p['symbol'] in ('GOLDM', 'SILVERM', 'CRUDEOILM', 'GOLD', 'SILVER', 'CRUDEOIL')
            if p_is_commodity == is_commodity:
                if p.get('is_sell', False):
                    total_exposure += MARGIN_PER_LOT.get(p['symbol'], 120000) * p.get('num_lots', 1)
                else:
                    total_exposure += p['entry_premium'] * p['lot_size']
        available_capital = segment_capital - total_exposure

        # v2.5.3: Tiered lot sizing — scale lots by signal quality + available capital
        quality_score = (details or {}).get('quality_score', 0) if details else 0
        if is_sell:
            cost_per_lot = MARGIN_PER_LOT.get(symbol, 120000)
        else:
            cost_per_lot = entry_premium * base_lot

        # Drawdown check — force single lot if DD > 5%
        equity_hwm = max(self.initial_capital, self.capital)
        current_dd_pct = (equity_hwm - self.capital) / equity_hwm * 100

        # Determine number of lots using tiered allocation
        max_lots_cap = MAX_LOTS.get(symbol, 2)
        if current_dd_pct > 5 or cost_per_lot <= 0:
            num_lots = 1
            tier_name = 'DD_CAP' if current_dd_pct > 5 else 'MIN'
        elif quality_score >= LOT_TIER_ELITE:
            capital_alloc = available_capital * 0.30  # Elite: 30%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'ELITE'
        elif quality_score >= LOT_TIER_STRONG:
            capital_alloc = available_capital * 0.20  # Strong: 20%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'STRONG'
        else:
            capital_alloc = available_capital * 0.10  # Standard: 10%
            num_lots = max(1, min(int(capital_alloc // cost_per_lot), max_lots_cap))
            tier_name = 'STANDARD'

        logger.info(f"  LOT_TIER: {symbol} {strategy} score={quality_score} tier={tier_name} "
                    f"lots={num_lots}/{max_lots_cap} cost/lot=Rs {cost_per_lot:,.0f} "
                    f"avail=Rs {available_capital:,.0f} DD={current_dd_pct:.1f}%")

        lot_size = base_lot * num_lots

        # v19.4: Sweep-confirmed Ghost Zone v8 gets extra lots
        extra = details.get('_extra_lots', 0) if details else 0
        if extra > 0:
            num_lots += extra
            lot_size = base_lot * num_lots
            logger.info('  SWEEP_LOT: %s adding %d extra lots -> %d total' % (symbol, extra, num_lots))


        # Capital required for this trade
        if is_sell:
            trade_cost = MARGIN_PER_LOT.get(symbol, 120000) * num_lots
        else:
            trade_cost = entry_premium * lot_size

        # Check 1: Per-trade risk limit (skip for SELL — margin is collateral, not risk)
        if not is_sell and trade_cost > max_per_trade * num_lots:
            logger.warning(f"  RISK_LIMIT: {symbol} {strategy} trade cost Rs {trade_cost:,.0f} "
                          f"> max Rs {max_per_trade * num_lots:,.0f}. SKIPPED.")
            return None

        # Check 2: Available capital
        if trade_cost > available_capital:
            # Try with fewer lots
            if num_lots > 1 and cost_per_lot > 0:
                num_lots = max(1, int(available_capital // cost_per_lot))
                lot_size = base_lot * num_lots
                trade_cost = cost_per_lot * num_lots
            if trade_cost > available_capital:
                logger.warning(f"  RISK_LIMIT: {symbol} {strategy} needs Rs {trade_cost:,.0f} "
                              f"but only Rs {available_capital:,.0f} available. SKIPPED.")
                return None

        # Check 3: Daily loss limit
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.daily_pnl.get(today, 0)
        max_daily = MAX_DAILY_LOSS_COMMODITY if is_commodity else MAX_DAILY_LOSS_EQUITY
        if daily_loss < -max_daily:
            logger.warning(f"  RISK_LIMIT: Daily loss Rs {daily_loss:,.0f} exceeds limit Rs {max_daily:,.0f}. SKIPPED.")
            return None

        # Check 4: Max positions per symbol (prevent cascade)
        same_symbol = [p for p in self.positions if p['symbol'] == symbol]
        if len(same_symbol) >= MAX_POSITIONS_PER_SYMBOL:
            logger.warning(f"  RISK_LIMIT: Max {MAX_POSITIONS_PER_SYMBOL} positions for {symbol}. SKIPPED.")
            return None

        # Check 5: Drawdown-based position scaling (for single-lot trades too)
        if False:  # v19: DD scaling disabled for paper
            scale_factor = max(0.5, 1.0 - (current_dd_pct - 5) * 0.05)
            scaled_max = max_per_trade * scale_factor
            if trade_cost > scaled_max:
                logger.warning(f"  RISK_SCALE: DD={current_dd_pct:.1f}%, trade Rs {trade_cost:,.0f} > "
                              f"scaled max Rs {scaled_max:,.0f} ({scale_factor*100:.0f}%). SKIPPED.")
                return None

        sig = {
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'symbol': symbol,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': entry_premium,
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'iv': iv,
            'dte': dte,
            'details': details or {},
            'status': 'SIGNAL'
        }
        self.signals.append(sig)

        is_sell = 'SELL' in signal_type
        cost = calc_costs(entry_premium, lot_size, is_sell)

        pos = {
            'id': f"{strategy}_{symbol}_{datetime.now().strftime('%H%M%S')}",
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'symbol': symbol,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': round(entry_premium, 2),
                'ltp_source': (details or {}).get('ltp_source', getattr(self, '_last_ltp_source', 'UNKNOWN')),
                'signal_strength': (details or {}).get('signal_strength', 0),
                'strength_label': (details or {}).get('strength_label', 'UNKNOWN'),
            'current_premium': round(entry_premium, 2),
            'lot_size': lot_size,
            'num_lots': num_lots,  # v2.4: How many exchange lots
            'is_sell': is_sell,
            'entry_cost': round(cost, 2),
            'delta': round(delta, 4),
            'gamma': round(gamma, 6),
            'theta': round(theta, 2),
            'iv': round(iv * 100, 1),
            'dte': dte,
            'unrealized_pnl': 0,
            'status': 'OPEN',
            'quality_score': (details or {}).get('quality_score', 0),
            'reason': (details or {}).get('reason', ''),
            # OI+IV tracking for dynamic exits
            'entry_oi': oi,
            'entry_iv': round(iv * 100, 1),
            'entry_spot': round(spot_price, 2),
            'max_risk': round(max_per_trade, 2),
            # Trailing Stop Loss tracking
            'peak_premium': round(entry_premium, 2),
            'trough_premium': round(entry_premium, 2),
            'trailing_sl': None,
            'breakeven_locked': False,
            'details': details or {},  # Store target/SL from strategy signals
            'capital_available': round(available_capital - trade_cost, 2),  # v2.4
        }
        self.positions.append(pos)

        if num_lots > 1:
            logger.info(f"  MULTI_LOT: {num_lots} lots ({lot_size} qty) for {symbol} "
                       f"| Score={quality_score} | Available Rs {available_capital:,.0f}")
        logger.info(f"  PAPER TRADE: {signal_type} {symbol} {strike} "
                    f"@ Rs {entry_premium:.2f} x{num_lots}lot | Strategy: {strategy} "
                    f"| Delta: {delta:.3f} | IV: {iv*100:.1f}% | OI: {oi}")

        self.save_state()
        return pos

    def update_position(self, pos_id, current_premium):
        """Update position with current market price."""
        for pos in self.positions:
            if pos['id'] == pos_id:
                pos['current_premium'] = round(current_premium, 2)
                if pos['is_sell']:
                    pos['unrealized_pnl'] = round(
                        (pos['entry_premium'] - current_premium) * pos['lot_size'] - pos['entry_cost'], 2)
                else:
                    pos['unrealized_pnl'] = round(
                        (current_premium - pos['entry_premium']) * pos['lot_size'] - pos['entry_cost'], 2)
                return pos
        return None

    def close_position(self, pos_id, exit_premium, reason=''):
        """Close a paper position."""
        for i, pos in enumerate(self.positions):
            if pos['id'] == pos_id:
                exit_cost = calc_costs(exit_premium, pos['lot_size'], not pos['is_sell'])
                if pos['is_sell']:
                    pnl = (pos['entry_premium'] - exit_premium) * pos['lot_size']
                else:
                    pnl = (exit_premium - pos['entry_premium']) * pos['lot_size']
                pnl -= (pos['entry_cost'] + exit_cost)

                trade = {
                    **pos,
                    'exit_premium': round(exit_premium, 2),
                    'exit_cost': round(exit_cost, 2),
                    'pnl': round(pnl, 2),
                    'exit_reason': reason,
                    'exit_time': datetime.now().isoformat(),
                    'status': 'CLOSED'
                }
                self.closed_trades.append(trade)
                self.positions.pop(i)

                today = datetime.now().strftime('%Y-%m-%d')
                self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl
                self.capital += pnl
                trade['capital_after'] = round(self.capital, 2)  # v2.4

                logger.info(f"  CLOSED: {pos['signal_type']} {pos['symbol']} {pos['strike']} "
                           f"| Entry: Rs {pos['entry_premium']:.2f} Exit: Rs {exit_premium:.2f} "
                           f"| PnL: Rs {pnl:,.2f} | Reason: {reason} | Capital: Rs {self.capital:,.0f}")

                self.save_state()
                return trade
        return None

    def get_summary(self):
        """Get portfolio summary."""
        total_unrealized = sum(p['unrealized_pnl'] for p in self.positions)
        total_realized = sum(t['pnl'] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        losses = [t for t in self.closed_trades if t['pnl'] <= 0]

        return {
            'capital': self.capital,
            'initial_capital': self.initial_capital,
            'total_return_pct': (self.capital - self.initial_capital) / self.initial_capital * 100,
            'open_positions': len(self.positions),
            'total_trades': len(self.closed_trades),
            'win_rate': len(wins) / len(self.closed_trades) * 100 if self.closed_trades else 0,
            'total_realized': total_realized,
            'total_unrealized': total_unrealized,
            'avg_win': np.mean([t['pnl'] for t in wins]) if wins else 0,
            'avg_loss': np.mean([t['pnl'] for t in losses]) if losses else 0,
        }

    def print_status(self):
        """Print current portfolio status."""
        summary = self.get_summary()
        print("\n" + "=" * 70)
        print("PAPER TRADING PORTFOLIO STATUS")
        print("=" * 70)
        print(f"  Capital:           Rs {summary['capital']:,.0f}")
        print(f"  Initial:           Rs {summary['initial_capital']:,.0f}")
        print(f"  Return:            {summary['total_return_pct']:.2f}%")
        print(f"  Open Positions:    {summary['open_positions']}")
        print(f"  Closed Trades:     {summary['total_trades']}")
        print(f"  Win Rate:          {summary['win_rate']:.1f}%")
        print(f"  Realized PnL:      Rs {summary['total_realized']:,.0f}")
        print(f"  Unrealized PnL:    Rs {summary['total_unrealized']:,.0f}")

        if self.positions:
            print("\n  OPEN POSITIONS:")
            for p in self.positions:
                print(f"    {p['strategy']:12s} | {p['signal_type']:10s} | "
                      f"{p['symbol']:10s} | Strike: {p['strike']:>8.0f} | "
                      f"Entry: Rs {p['entry_premium']:>8.2f} | "
                      f"PnL: Rs {p['unrealized_pnl']:>8.2f}")

        if self.closed_trades:
            recent = self.closed_trades[-5:]
            print(f"\n  RECENT TRADES (last {len(recent)}):")
            for t in recent:
                print(f"    {t['strategy']:12s} | {t['signal_type']:10s} | "
                      f"{t['symbol']:10s} | PnL: Rs {t['pnl']:>8.2f} | "
                      f"{t['exit_reason']}")
        print("=" * 70)


# ====================================================================
# STRATEGY SIGNAL GENERATORS (LIVE)
# ====================================================================
class StrategyEngine:
    """Generate signals from all 5 strategies using live data."""

    def __init__(self, angel: AngelConnection, portfolio: PaperPortfolio):
        self.angel = angel
        self.portfolio = portfolio
        self.historical_data = {}  # symbol -> DataFrame
        self.market_pipeline = None  # v9.6d: Set by PaperTrader if available
        self.zerodha = None   # v12.0: Set by PaperTrader if available
        self.truedata = None  # v11.2: Set by PaperTrader if available
        self.oi_tracker = OIVelocityTracker()  # v17: OI velocity Gamma Blast

    def load_historical(self, symbol):
        """Load historical data for indicators. Auto-download if missing/stale."""
        angel_map = {
            'NIFTY': 'NIFTY_spot_one_day_2000d.csv',
            'BANKNIFTY': 'BANKNIFTY_spot_one_day_2000d.csv',
            'SENSEX': 'SENSEX_spot_one_day_2000d.csv',
        }
        fpath = os.path.join(OPTIONS_DIR, angel_map.get(symbol, ''))

        # v9.6d: Check if file needs downloading (missing or stale >1 day)
        # Changed from 7 days to 1 day — CPR needs yesterday's OHLC to be accurate
        needs_download = False
        if os.path.exists(fpath):
            mod_time = datetime.fromtimestamp(os.path.getmtime(fpath))
            age_days = (datetime.now() - mod_time).days
            if age_days >= 1:
                needs_download = True
                logger.info(f"Historical data for {symbol} is stale ({age_days}d old), refreshing...")
        else:
            needs_download = True
            logger.info(f"Historical data for {symbol} not found, downloading...")

        if needs_download and self.angel and self.angel._connected:
            self._download_historical_equity(symbol, fpath)

        if os.path.exists(fpath):
            df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            self.historical_data[symbol] = df
            logger.info(f"Loaded {len(df)} historical days for {symbol}")
            return df

        logger.error(f"No historical data found for {symbol} (Angel SmartAPI only) — indicators will not work")
        return None

    def _download_historical_equity(self, symbol, save_path):
        """Download daily OHLC data from Angel SmartAPI for equity indices."""
        try:
            # Well-known Angel One tokens for indices
            token_map = {'NIFTY': '99926000', 'BANKNIFTY': '99926009', 'SENSEX': '99919000'}
            # v2.5.1: SENSEX is on BSE, not NSE
            exchange_map = {'NIFTY': 'NSE', 'BANKNIFTY': 'NSE', 'SENSEX': 'BSE'}
            token = token_map.get(symbol)
            exchange = exchange_map.get(symbol, 'NSE')

            if not token:
                # Fallback: look up from instrument master
                if self.angel.instruments is None:
                    self.angel.load_instruments()
                if self.angel.instruments is not None:
                    nse_df = self.angel.instruments[
                        (self.angel.instruments['name'] == symbol) &
                        (self.angel.instruments['exch_seg'] == exchange)
                    ]
                    if len(nse_df) > 0:
                        token = str(nse_df.iloc[0]['token'])

            if not token:
                logger.warning(f"Could not find Angel token for {symbol}, skipping download")
                return

            logger.info(f"Downloading historical data for {symbol} (token={token}, exchange={exchange})...")
            all_data = []
            chunk_start = datetime.now() - timedelta(days=2000)

            while chunk_start < datetime.now():
                chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
                data = self.angel.get_historical(
                    exchange, token, 'ONE_DAY',  # v2.5.1: Use correct exchange per symbol
                    chunk_start.strftime('%Y-%m-%d 09:15'),
                    chunk_end.strftime('%Y-%m-%d 15:30')
                )
                if data:
                    all_data.extend(data)
                chunk_start = chunk_end + timedelta(days=1)
                time.sleep(2)  # v2.5.1: Increased from 0.5s to 2s to avoid rate limiting

            if all_data:
                df = pd.DataFrame(all_data, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                df.to_csv(save_path, index=False)
                logger.info(f"Downloaded {len(df)} days for {symbol} → {save_path}")
            else:
                logger.warning(f"No data returned from Angel API for {symbol}")
        except Exception as e:
            logger.error(f"Download failed for {symbol}: {e}")

    def _fetch_4h_ghost_zones(self, symbol):
        """Fetch 5m candles from Angel SmartAPI → aggregate to 4H → detect Ghost Zones.

        Multi-Timeframe Architecture (Manish Maheshwari):
        - 4H charts: Identify Ghost Zones (institutional demand/supply zones)
        - 5m/3m charts: Entry timing at 50% of zone

        Returns: (demand_zones, supply_zones) lists, or ([], []) if fetch fails.
        Cache: 4H zones cached for 30 min (zones don't change that often).
        """
        cache_key = f"4h_zones_{symbol}"
        now = time.time()

        # Check cache (30 min TTL)
        if hasattr(self, '_zone_cache') and cache_key in self._zone_cache:
            cached_time, cached_demand, cached_supply = self._zone_cache[cache_key]
            if now - cached_time < 1800:  # 30 min
                return cached_demand, cached_supply

        if not hasattr(self, '_zone_cache'):
            self._zone_cache = {}

        if not self.angel or not self.angel._connected:
            return [], []

        try:
            # Get token for index
            token_map = {'NIFTY': '99926000', 'BANKNIFTY': '99926009', 'SENSEX': '99919000'}
            exchange_map = {'NIFTY': 'NSE', 'BANKNIFTY': 'NSE', 'SENSEX': 'BSE'}
            token = token_map.get(symbol)
            exchange = exchange_map.get(symbol, 'NSE')
            if not token:
                return [], []

            # Fetch last 5 days of 5-minute candles
            to_date = datetime.now().strftime('%Y-%m-%d 15:30')
            from_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d 09:15')

            candles = self.angel.get_historical(exchange, token, 'FIVE_MINUTE', from_date, to_date)
            if not candles or len(candles) < 48:  # Need at least one 4H block
                return [], []

            # Build 5m DataFrame
            df_5m = pd.DataFrame(candles, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df_5m['DateTime'] = pd.to_datetime(df_5m['DateTime'])
            df_5m.set_index('DateTime', inplace=True)
            df_5m = df_5m.astype(float)

            # Aggregate to 4H OHLCV blocks
            # Each 4H block: Open=first, High=max, Low=min, Close=last, Volume=sum
            df_4h = df_5m.resample('4h').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum'
            }).dropna()

            if len(df_4h) < 5:
                return [], []

            # ATR on 4H bars
            tr_4h = pd.concat([
                df_4h['High'] - df_4h['Low'],
                abs(df_4h['High'] - df_4h['Close'].shift(1)),
                abs(df_4h['Low'] - df_4h['Close'].shift(1))
            ], axis=1).max(axis=1)
            atr_4h = tr_4h.rolling(min(14, len(df_4h) - 1)).mean().iloc[-1]
            avg_vol_4h = df_4h['Volume'].rolling(min(20, len(df_4h) - 1)).mean().iloc[-1]

            # Detect institutional candles on 4H blocks
            demand_zones = []
            supply_zones = []

            for j in range(1, len(df_4h) - 1):
                bar = df_4h.iloc[j]
                next_bar = df_4h.iloc[j + 1]

                o, h, l, c = bar['Open'], bar['High'], bar['Low'], bar['Close']
                body = abs(c - o)
                full_range = h - l
                if full_range <= 0:
                    continue
                body_ratio = body / full_range
                lower_wick = min(o, c) - l
                upper_wick = h - max(o, c)
                vol = bar['Volume']

                # Volume spike on 4H: >= 3x average (true Ghost Trader threshold)
                vol_mult = vol / max(avg_vol_4h, 1) if avg_vol_4h > 0 else 1
                if vol_mult < 3.0:
                    continue  # Strict 3x threshold on 4H candles

                candle_type = None
                bias = 'bullish' if c > o else 'bearish'

                # Injection (Pin Bar)
                if body_ratio < 0.35:
                    if lower_wick > full_range * 0.55:
                        candle_type = 'injection'
                        bias = 'bullish'
                    elif upper_wick > full_range * 0.55:
                        candle_type = 'injection'
                        bias = 'bearish'

                # Bat (Expansion)
                if candle_type is None and body_ratio > 0.70 and body > atr_4h * 0.8:
                    candle_type = 'bat'

                # Belan (Doji)
                if candle_type is None and body_ratio < 0.15 and full_range > atr_4h * 0.5:
                    candle_type = 'belan'

                if candle_type is None:
                    continue

                # Impulse check
                impulse_up = next_bar['Close'] > h
                impulse_down = next_bar['Close'] < l

                zone = {
                    'low': l, 'high': h, 'mid': (l + h) / 2,
                    'candle_type': candle_type, 'vol_mult': vol_mult,
                    'timeframe': '4H',
                    'created_at': df_4h.index[j],
                }

                if (bias == 'bullish' or candle_type == 'belan') and impulse_up:
                    demand_zones.append(zone)
                if (bias == 'bearish' or candle_type == 'belan') and impulse_down:
                    supply_zones.append(zone)

            # Keep most recent zones (max 5)
            demand_zones = demand_zones[-5:]
            supply_zones = supply_zones[-5:]

            logger.info(f"GTZ 4H zones for {symbol}: {len(demand_zones)} demand, "
                        f"{len(supply_zones)} supply (from {len(df_4h)} 4H blocks)")

            # Cache
            self._zone_cache[cache_key] = (now, demand_zones, supply_zones)
            return demand_zones, supply_zones

        except Exception as e:
            logger.error(f"4H zone fetch error for {symbol}: {e}")
            return [], []

    def _get_prev_trading_day(self, df, symbol=''):
        """v10.1: Get previous trading day OHLC for CPR. Validates data freshness."""
        if len(df) < 2:
            return df.iloc[-1], "only_row"
        today = datetime.now().date()
        prev_idx = df.index[-2]
        prev_date = pd.Timestamp(prev_idx).date()  # .date() strips timezone
        gap_days = (today - prev_date).days
        if gap_days <= 4:  # Normal: yesterday or weekend gap (Fri→Mon = 3)
            return df.iloc[-2], f"prev({prev_date.strftime('%Y-%m-%d')},gap={gap_days}d)"
        else:
            last_date = pd.Timestamp(df.index[-1]).date()  # .date() strips timezone
            logger.warning(f"  CPR_STALE: {symbol} prev bar {gap_days}d old ({prev_date}), using last bar")
            return df.iloc[-1], f"fallback({last_date.strftime('%Y-%m-%d')},stale={gap_days}d)"

    def compute_indicators(self, symbol, current_ohlc=None):
        """Compute all indicators needed for strategies."""
        df = self.historical_data.get(symbol)
        if df is None:
            df = self.load_historical(symbol)
        if df is None:
            return None

        # v9.6d: Force refresh if data is stale and angel is connected
        # (load_historical at startup runs before connect(), so download is skipped)
        if df is not None and len(df) > 0 and self.angel and self.angel._connected:
            last_date = df.index[-1]
            if hasattr(last_date, 'date'):
                last_date = last_date.date()
            days_old = (datetime.now().date() - last_date).days
            if days_old > 1:
                logger.info(f"Historical data for {symbol} last date={last_date}, {days_old}d old — refreshing...")
                angel_map = {
                    'NIFTY': 'NIFTY_spot_one_day_2000d.csv',
                    'BANKNIFTY': 'BANKNIFTY_spot_one_day_2000d.csv',
                    'SENSEX': 'SENSEX_spot_one_day_2000d.csv',
                }
                fpath = os.path.join(OPTIONS_DIR, angel_map.get(symbol, ''))
                if fpath:
                    self._download_historical_equity(symbol, fpath)
                    refreshed = self.load_historical(symbol)
                    if refreshed is not None:
                        df = refreshed

        # If we have live OHLC, append it
        if current_ohlc:
            today = pd.Timestamp.now().normalize()
            new_row = pd.DataFrame({
                'Open': [current_ohlc['open']],
                'High': [current_ohlc['high']],
                'Low': [current_ohlc['low']],
                'Close': [current_ohlc['close']],
                'Volume': [current_ohlc.get('volume', 0)],
            }, index=[today])
            if today in df.index:
                df.loc[today] = new_row.iloc[0]
            else:
                df = pd.concat([df, new_row])

        # ATR
        atr = (df['High'].tail(14) - df['Low'].tail(14)).mean()

        # Historical Volatility
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        hv = log_ret.tail(20).std() * np.sqrt(252)
        iv = max(min(hv * 1.15, 0.60), 0.08)

        # v13.0: Intraday VWAP (integral calculus) — replaces stale 5-day VWAP
        vwap = None
        if hasattr(self, 'calculus'):
            vwap = self.calculus.get_intraday_vwap_for_indicators(symbol)
        if vwap is None:
            # Fallback: 5-day VWAP only if calculus engine has no data yet
            if 'Volume' in df.columns and df['Volume'].tail(5).sum() > 0:
                tp = (df['High'] + df['Low'] + df['Close']) / 3
                vwap = (tp * df['Volume']).tail(5).sum() / df['Volume'].tail(5).sum()
            else:
                vwap = ((df['High'] + df['Low'] + df['Close']) / 3).tail(5).mean()

        # PCR proxy
        close_chg = df['Close'].pct_change()
        pcr_proxy = 1 + (close_chg.tail(5).mean() * 10)
        pcr_proxy = max(0.5, min(2.0, pcr_proxy))

        # v10.1: EMA trend indicators for direction validation
        ema_9 = df['Close'].ewm(span=9).mean().iloc[-1] if len(df) >= 9 else df['Close'].iloc[-1]
        ema_20 = df['Close'].ewm(span=20).mean().iloc[-1] if len(df) >= 20 else df['Close'].iloc[-1]
        n_bars = min(3, len(df) - 1)
        three_bar_trend = sum(1 if df['Close'].iloc[-(n_bars - i)] > df['Close'].iloc[-(n_bars - i + 1)] else -1
                              for i in range(n_bars)) if n_bars > 0 else 0

        # v10.1: CPR from previous trading day — validated for freshness
        prev, cpr_source = self._get_prev_trading_day(df, symbol)
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / prev['Close'] * 100

        logger.debug(f"  CPR_SOURCE: {symbol} {cpr_source} pivot={pivot:.0f} BC={bc:.0f} TC={tc:.0f}")

        # Camarilla
        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        # Ghost Zones v7: Multi-timeframe zone detection (Manish Maheshwari methodology)
        # PRIMARY: 4H blocks from 5m candles (true Ghost Trader timeframe)
        # FALLBACK: Daily bars (when Angel API unavailable)
        demand_zones, supply_zones = self._fetch_4h_ghost_zones(symbol)

        # Fallback to daily bar zone detection if 4H fetch failed
        if not demand_zones and not supply_zones:
            lookback_df = df.tail(40)
            avg_vol_20 = df['Volume'].tail(20).mean()
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

                # Injection (Pin Bar)
                if body_ratio < 0.35:
                    if lower_wick > full_range * 0.55:
                        candle_type = 'injection'
                        bias = 'bullish'
                    elif upper_wick > full_range * 0.55:
                        candle_type = 'injection'
                        bias = 'bearish'

                # Bat (Expansion)
                if candle_type is None and body_ratio > 0.70 and body > atr * 0.8:
                    candle_type = 'bat'

                # Belan (Doji)
                if candle_type is None and body_ratio < 0.15 and full_range > atr * 0.5:
                    candle_type = 'belan'

                if candle_type is None:
                    continue

                # Impulse check
                impulse_up = next_bar['Close'] > h
                impulse_down = next_bar['Close'] < l

                zone = {'low': l, 'high': h, 'mid': (l + h) / 2,
                        'candle_type': candle_type, 'vol_mult': vol_mult,
                        'timeframe': 'daily'}

                if (bias == 'bullish' or candle_type == 'belan') and impulse_up:
                    demand_zones.append(zone)
                if (bias == 'bearish' or candle_type == 'belan') and impulse_down:
                    supply_zones.append(zone)

            demand_zones = demand_zones[-5:]
            supply_zones = supply_zones[-5:]

        demand_zone = demand_zones[-1]['low'] if demand_zones else df['Low'].tail(10).min()
        demand_zone_high = demand_zones[-1]['high'] if demand_zones else demand_zone + atr
        supply_zone = supply_zones[-1]['high'] if supply_zones else df['High'].tail(10).max()
        supply_zone_low = supply_zones[-1]['low'] if supply_zones else supply_zone - atr
        demand_strength = len(demand_zones)
        supply_strength = len(supply_zones)

        # Support/Resistance for Survivor
        resistance = prev['High']
        support = prev['Low']

        return {
            'atr': atr,
            'iv': iv,
            'hv': hv,
            'vwap': vwap,
            'pcr': pcr_proxy,
            'pivot': pivot,
            'bc': bc,
            'tc': tc,
            'cpr_width': cpr_width,
            'cam_r3': cam_r3,
            'cam_r4': cam_r4,
            'cam_s3': cam_s3,
            'cam_s4': cam_s4,
            'demand_zone': demand_zone,
            'demand_zone_high': demand_zone_high,
            'supply_zone': supply_zone,
            'supply_zone_low': supply_zone_low,
            'demand_strength': demand_strength,
            'supply_strength': supply_strength,
            'demand_zones': demand_zones,
            'supply_zones': supply_zones,
            'resistance': resistance,
            'support': support,
            'prev_high': prev['High'],
            'prev_low': prev['Low'],
            'prev_close': prev['Close'],
            'prev_range': (prev['High'] - prev['Low']) / max(atr, 1),
            # v10.1: Direction indicators
            'ema_9': ema_9,
            'ema_20': ema_20,
            'three_bar_trend': three_bar_trend,
        }

    def _get_nearest_expiry(self, symbol):
        """Get nearest expiry for index options from instruments master.

        v10.3d-3: Added to StrategyEngine so _get_strike_from_chain()
        can resolve expiry for optionGreek API calls.
        """
        if not self.angel or self.angel.instruments is None:
            return None
        exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
        mask = (self.angel.instruments['name'] == symbol) & \
               (self.angel.instruments['exch_seg'] == exchange) & \
               (self.angel.instruments['instrumenttype'].isin(['OPTIDX']))
        matches = self.angel.instruments[mask]
        if len(matches) == 0:
            return None
        today = datetime.now().date()
        try:
            expiries = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True).dt.date
            future = expiries[expiries >= today]
            if len(future) == 0:
                return None
            return future.min().strftime('%d%b%Y').upper()
        except Exception:
            return None

    def _get_strike_from_chain(self, symbol, spot, opt_type, dte, target_strike=None):
        """v5: Get optimal strike from live option chain with caching.

        Uses Angel option chain API to select strike with best OI/liquidity.
        Falls back to mathematical derivation if API unavailable.
        Caches results for 5 minutes to avoid excessive API calls.
        v11.2: target_strike param — if set, fetch LTP for that specific strike (for SELL signals).

        Returns: (strike, real_ltp, real_iv) tuple
        """
        cache_key = f"{symbol}_{opt_type}" + (f"_{target_strike}" if target_strike else "")
        now = time.time()

        # Check cache (5 min TTL for chain hits, 30s for fallbacks — v9.3)
        if hasattr(self, '_strike_cache') and cache_key in self._strike_cache:
            cached = self._strike_cache[cache_key]
            if now - cached['time'] < 300:  # 5 min (fallback entries expire in ~30s)
                return cached['strike'], cached['ltp'], cached['iv']

        if not hasattr(self, '_strike_cache'):
            self._strike_cache = {}

        strike_interval = STRIKE_INTERVALS.get(symbol, 50)
        fallback_strike = round(spot / strike_interval) * strike_interval

        # v12.0: Data source priority: 1) Zerodha, 2) TrueData, 3) NSE/BSE pipeline, 4) Angel API

        # Source 1: Zerodha Kite Connect option chain (primary)
        if hasattr(self, 'zerodha') and self.zerodha and self.zerodha.is_connected:
            try:
                zd_chain = self.zerodha.get_option_chain(symbol)
                if zd_chain:
                    contracts = zd_chain.get(opt_type, [])
                    zd_best = None
                    zd_best_dist = float('inf')
                    if target_strike:
                        for c in contracts:
                            if c.get('ltp', 0) > 0 and c['strike'] == target_strike:
                                zd_best = c
                                break
                    else:
                        for c in contracts:
                            if c.get('ltp', 0) > 0:
                                dist = abs(c['strike'] - spot)
                                if dist < zd_best_dist:
                                    zd_best_dist = dist
                                    zd_best = c
                    if zd_best:
                        self._strike_cache[cache_key] = {
                            'strike': zd_best['strike'],
                            'ltp': zd_best['ltp'],
                            'iv': zd_best.get('iv', 0),
                            'time': now,
                            'source': 'ZERODHA',
                        }
                        self._last_ltp_source = 'ZERODHA'
                        logger.info(f"  ZERODHA_STRIKE: {symbol} {opt_type} "
                                   f"strike={zd_best['strike']} LTP={zd_best['ltp']:.2f} "
                                   f"IV={zd_best.get('iv', 0)*100:.1f}% [Zerodha]")
                        return zd_best['strike'], zd_best['ltp'], zd_best.get('iv', 0)
            except Exception as e:
                logger.debug(f"[Zerodha] Strike lookup failed for {symbol}: {e}")

        # Source 2: TrueData option chain (secondary — real-time WebSocket data)
        if self.truedata and self.truedata.is_connected:
            try:
                td_chain = self.truedata.get_option_chain(symbol)
                if td_chain:
                    contracts = td_chain.get(opt_type, [])
                    td_best = None
                    td_best_dist = float('inf')
                    if target_strike:
                        for c in contracts:
                            if c.get('ltp', 0) > 0 and c['strike'] == target_strike:
                                td_best = c
                                break
                    else:
                        for c in contracts:
                            if c.get('ltp', 0) > 0:
                                dist = abs(c['strike'] - spot)
                                if dist < td_best_dist:
                                    td_best_dist = dist
                                    td_best = c
                    if td_best:
                        self._strike_cache[cache_key] = {
                            'strike': td_best['strike'],
                            'ltp': td_best['ltp'],
                            'iv': td_best.get('iv', 0),
                            'time': now,
                            'source': 'TRUEDATA',
                        }
                        self._last_ltp_source = 'TRUEDATA'
                        logger.info(f"  TRUEDATA_STRIKE: {symbol} {opt_type} "
                                   f"strike={td_best['strike']} LTP={td_best['ltp']:.2f} "
                                   f"IV={td_best.get('iv', 0)*100:.1f}% [TrueData]")
                        return td_best['strike'], td_best['ltp'], td_best.get('iv', 0)
            except Exception as e:
                logger.debug(f"[TrueData] Strike lookup failed for {symbol}: {e}")

        # Source 2: Pipeline chain from NSE/BSE (fallback when TrueData unavailable)
        if self.market_pipeline:
            try:
                chain = self.market_pipeline.get_option_chain(symbol)
                if chain:
                    contracts = chain.get(opt_type, [])
                    # v11.2: If target_strike specified, find that exact strike
                    best = None
                    best_dist = float('inf')
                    if target_strike:
                        for c in contracts:
                            if c.get('ltp', 0) > 0 and c['strike'] == target_strike:
                                best = c
                                break
                    else:
                        # Find ATM contract closest to spot with real LTP
                        for c in contracts:
                            if c.get('ltp', 0) > 0:
                                dist = abs(c['strike'] - spot)
                                if dist < best_dist:
                                    best_dist = dist
                                    best = c
                    if best:
                        self._strike_cache[cache_key] = {
                            'strike': best['strike'],
                            'ltp': best['ltp'],
                            'iv': best.get('iv', 0),
                            'time': now,
                            'source': 'NSE_PIPELINE',
                        }
                        self._last_ltp_source = 'NSE_PIPELINE'
                        logger.info(f"  PIPELINE_STRIKE: {symbol} {opt_type} "
                                   f"strike={best['strike']} LTP={best['ltp']:.2f} (from NSE/BSE chain)")
                        return best['strike'], best['ltp'], best.get('iv', 0)
            except Exception as e:
                logger.debug(f"Pipeline strike lookup failed for {symbol}: {e}")

        # Source 3: Angel API option chain (last resort)
        if self.angel and self.angel._connected:
            try:
                expiry = self._get_nearest_expiry(symbol)
                if expiry:
                    if target_strike:
                        # v11.2: Fetch LTP for specific strike (SELL signals)
                        token_info = self.angel.find_option_tokens(symbol, None, target_strike, opt_type)
                        if token_info:
                            exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
                            self.angel._throttle()
                            mkt = self.angel.get_market_data(exchange, str(token_info.get('token', '')))
                            if mkt:
                                ltp = float(mkt.get('ltp', 0) or 0)
                                if ltp > 0:
                                    self._strike_cache[cache_key] = {
                                        'strike': target_strike, 'ltp': ltp, 'iv': 0, 'time': now,
                                    }
                                    return target_strike, ltp, 0
                    else:
                        result = self.angel.select_optimal_strike(
                            symbol, spot, opt_type, expiry_date=expiry
                        )
                        if result and result.get('strike', 0) > 0:
                            self._strike_cache[cache_key] = {
                                'strike': result['strike'],
                                'ltp': result.get('ltp', 0),
                                'iv': result.get('iv', 0),
                                'time': now,
                            }
                            return result['strike'], result.get('ltp', 0), result.get('iv', 0)
                        else:
                            logger.info(f"  ANGEL_STRIKE_EMPTY: {symbol} {opt_type} expiry={expiry} — "
                                       f"no valid strike from optionGreek API")
                else:
                    logger.info(f"  ANGEL_STRIKE_NO_EXPIRY: {symbol} — no future expiry found")
            except Exception as e:
                logger.info(f"  ANGEL_STRIKE_ERR: {symbol} {opt_type} — {e}")

        # Fallback — v9.3: Short cache (30s) for ltp=0 so we retry quickly at market open
        self._strike_cache[cache_key] = {
            'strike': fallback_strike, 'ltp': 0, 'iv': 0, 'time': now - 270
        }
        return fallback_strike, 0, 0

    def check_cpr_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """CPR Strategy - Gomathi Shankar.

        v7.4: Removed CPR dead zone (0.3-0.5%) that blocked SENSEX signals.
        BUY breakout signals generated for ALL CPR widths — target/SL scaled by CPR width.
        SELL mean reversion only for very wide CPR (> 0.6%) with margin.
        """
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = STRIKE_INTERVALS.get(symbol, 100)
        cpr_w = ind['cpr_width']

        # v11.1: Skip CPR signals on ultra-narrow CPR days (causes signal flipping)
        if cpr_w < CPR_MIN_WIDTH_PCT:
            logger.info(f"  CPR_NARROW_SKIP: {symbol} CPR width {cpr_w:.4f}% < {CPR_MIN_WIDTH_PCT}% "
                       f"— TC≈BC≈Pivot, signals unreliable")
            return signals

        # ---- v7.5: Realistic intraday option targets ----
        # Old targets (2.5x) required 150% option gain — almost never hit intraday.
        # New targets based on realistic option delta/gamma math:
        #   ATM option with Δ=-0.5: 200pt NIFTY drop → ~Rs 120 gain on Rs 300 premium = 40%
        #   With gamma boost on strong day: 50-80% gain is achievable
        # Narrow CPR (< 0.3%): strong breakout → moderate targets
        # Moderate CPR (0.3-0.6%): directional trade → standard targets
        # Wide CPR (> 0.6%): breakout less likely → conservative targets
        if cpr_w < 0.3:
            cpr_label = "Narrow"
            target_hit_mult = 1.5   # Target = 1.5x premium (50% gain)
            target_base_mult = 1.3  # Fallback target = 1.3x (30% gain)
            sl_mult = 0.5
            # v13.3: Volatility-adjusted SL
            if VOLATILITY_SL_ENABLED and symbol in ATR_REFERENCE:
                vol_sl_mult = sl_mult * (ATR_REFERENCE.get(ATR_BASE_SYMBOL, 313) / ATR_REFERENCE.get(symbol, 313))
                sl_mult = max(0.12, min(0.50, vol_sl_mult))  # Clamp 12%-50%
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
        # Spot above TC → Bullish breakout → Buy CE
        if spot > ind['tc']:
            # v10.1: Direction confirmation — skip CE if intraday bearish AND below VWAP
            intraday_body = spot - ohlc['open']
            vwap = ind.get('vwap', spot)
            if intraday_body < 0 and vwap > 0 and spot < vwap:
                logger.info(f"  CPR_DIR_SKIP: {symbol} CE above TC but bearish intraday "
                           f"(body={intraday_body:.0f}, spot={spot:.0f}<VWAP={vwap:.0f})")
            else:
                ce_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
                if chain_ltp <= 0:
                    logger.info(f"  CPR_NO_LTP: {symbol} CE strike={ce_strike} — no live premium, skipping")
                else:
                    use_iv = chain_iv if chain_iv > 0 else ind['iv']
                    g = greeks_from_market_price(chain_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE') if chain_iv > 0 else bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, use_iv, 'CE')
                    premium = chain_ltp
                    if premium > MIN_PREMIUM_BUY:
                        signals.append({
                            'type': 'BUY_CE_CPR',
                            'strike': ce_strike,
                            'premium': premium,
                            'greeks': g,
                            'reason': f"{cpr_label} CPR ({cpr_w:.3f}%) bullish breakout above TC={ind['tc']:.0f} [LIVE]",
                        'target': premium * target_hit_mult if ohlc['high'] > ind['cam_r3'] else premium * target_base_mult,
                        'sl': premium * sl_mult,
                    })

        # Spot below BC → Bearish breakdown → Buy PE
        elif spot < ind['bc']:
            # v10.1: Direction confirmation — skip PE if intraday bullish AND above VWAP
            intraday_body = spot - ohlc['open']
            vwap = ind.get('vwap', spot)
            if intraday_body > 0 and vwap > 0 and spot > vwap:
                logger.info(f"  CPR_DIR_SKIP: {symbol} PE below BC but bullish intraday "
                           f"(body={intraday_body:+.0f}, spot={spot:.0f}>VWAP={vwap:.0f})")
            else:
                pe_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte)
                if chain_ltp <= 0:
                    logger.info(f"  CPR_NO_LTP: {symbol} PE strike={pe_strike} — no live premium, skipping")
                else:
                    use_iv = chain_iv if chain_iv > 0 else ind['iv']
                    g = greeks_from_market_price(chain_ltp, spot, pe_strike, T, RISK_FREE_RATE, 'PE') if chain_iv > 0 else bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, use_iv, 'PE')
                    premium = chain_ltp
                    if premium > MIN_PREMIUM_BUY:
                        signals.append({
                            'type': 'BUY_PE_CPR',
                            'strike': pe_strike,
                            'premium': premium,
                            'greeks': g,
                            'reason': f"{cpr_label} CPR ({cpr_w:.3f}%) bearish breakout below BC={ind['bc']:.0f} [LIVE]",
                        'target': premium * target_hit_mult if ohlc['low'] < ind['cam_s3'] else premium * target_base_mult,
                        'sl': premium * sl_mult,
                    })

        # ---- SELL MEAN REVERSION: Only for very wide CPR (> 0.6%) with margin ----
        if cpr_w > 0.6:
            margin_ok = self.portfolio.capital >= MARGIN_PER_LOT.get(symbol, 120000)
            if ohlc['high'] >= ind['cam_r3'] * 0.998 and spot < ind['cam_r4'] and margin_ok:
                ce_strike = round(ind['cam_r4'] / strike_interval) * strike_interval
                _, sell_ltp, sell_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte, target_strike=ce_strike)
                if sell_ltp > 0:
                    g = greeks_from_market_price(sell_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE')
                    if sell_ltp > MIN_PREMIUM_SELL:
                        signals.append({
                            'type': 'SELL_CE_CPR',
                            'strike': ce_strike,
                            'premium': sell_ltp,
                            'greeks': g,
                            'reason': f"Wide CPR ({cpr_w:.3f}%) mean reversion at R3={ind['cam_r3']:.0f} [LIVE]",
                            'target': sell_ltp * 0.3,
                            'sl': sell_ltp * 1.2,
                        })

            if ohlc['low'] <= ind['cam_s3'] * 1.002 and spot > ind['cam_s4'] and margin_ok:
                pe_strike = round(ind['cam_s4'] / strike_interval) * strike_interval
                _, sell_ltp, sell_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte, target_strike=pe_strike)
                if sell_ltp > 0:
                    g = greeks_from_market_price(sell_ltp, spot, pe_strike, T, RISK_FREE_RATE, 'PE')
                    if sell_ltp > MIN_PREMIUM_SELL:
                        signals.append({
                            'type': 'SELL_PE_CPR',
                            'strike': pe_strike,
                            'premium': sell_ltp,
                            'greeks': g,
                            'reason': f"Wide CPR ({cpr_w:.3f}%) mean reversion at S3={ind['cam_s3']:.0f} [LIVE]",
                            'target': sell_ltp * 0.3,
                            'sl': sell_ltp * 1.2,
                        })

        return signals

    def check_gamma_blast_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Gamma Blast v17 - OI Velocity based (replaces price-action breakout).

        Detects sudden OI surges at ATM+-2 strikes as leading indicator.
        Based on Raahi Bhushan article + backtest optimization.
        Backtest: NIFTY 80% WR, PF 4.58, +16.5% return.
        """
        signals = []

        # Fetch option chain OI if enough time has passed
        if self.oi_tracker.should_fetch(symbol):
            chain = None
            # Source 1: Zerodha
            if self.zerodha:
                try:
                    chain = self.zerodha.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: Zerodha failed for {symbol}: {e}")
            # Source 2: TrueData
            if chain is None and self.truedata:
                try:
                    chain = self.truedata.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: TrueData failed for {symbol}: {e}")
            # Source 3: Market Pipeline
            if chain is None and self.market_pipeline:
                try:
                    chain = self.market_pipeline.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: Pipeline failed for {symbol}: {e}")

            if chain:
                self.oi_tracker.update_oi(symbol, spot, chain)
                logger.debug(f"  {self.oi_tracker.get_status(symbol)}")

        # Check cooldown
        if self.oi_tracker.check_cooldown(symbol):
            return signals

        # Check blast signal
        blast = self.oi_tracker.check_blast(symbol, spot)
        if blast["signal"] is None or blast["signal"] == "STRADDLE":
            return signals

        # Determine expiry info
        if symbol == "BANKNIFTY":
            days_to_expiry = (2 - dow) % 7
        elif symbol == "SENSEX":
            days_to_expiry = (0 - dow) % 7
        else:
            days_to_expiry = (3 - dow) % 7
        is_expiry = (days_to_expiry == 0 or days_to_expiry == 7)
        T = max(dte, days_to_expiry) / 365 if not is_expiry else 1 / 365

        # Get ATM strike and live premium
        opt_type = "CE" if blast["signal"] == "BUY_CE" else "PE"
        strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, opt_type, dte)

        if chain_ltp <= 0:
            logger.info(f"  GAMMA_OI_NO_LTP: {symbol} {opt_type} strike={strike} - no live premium")
            return signals

        if chain_ltp < MIN_PREMIUM_BUY:
            return signals

        # Compute greeks
        use_iv = chain_iv if chain_iv > 0 else indicators["iv"] * 1.3
        if chain_iv > 0:
            g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, opt_type)
        else:
            g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, opt_type)

        # Build signal
        sig = make_gamma_blast_signal(blast, symbol, spot, strike, chain_ltp, g, dte, is_expiry)
        if sig:
            signals.append(sig)
            self.oi_tracker.set_cooldown(symbol)
            surge_pct = blast["ce_pct"] if opt_type == "CE" else blast["pe_pct"]
            logger.info(f"  GAMMA_OI_SIGNAL: {symbol} {blast['signal']} "
                       f"surge={surge_pct:.0%} strike={strike} prem={chain_ltp:.2f}")

        # Periodic cleanup
        self.oi_tracker.cleanup(symbol)

        return signals


    def check_wave_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Wave Extractor v3 - Gap-based entry with VWAP filter.

        Backtest: +Rs 1,02,437 (+34.15%) in 42 days.
        SENSEX star performer: PF 2.62, Rs 3K drawdown.
        """
        signals = []

        gap = WAVE_GAP_PTS.get(symbol)
        if gap is None:
            return signals

        use_vwap = WAVE_VWAP_FILTER.get(symbol, False)
        sl_mult = WAVE_SL_MULT.get(symbol, 0.65)
        target_mult = WAVE_TARGET_MULT

        # Anchor: day open price (9:15-9:30 open)
        anchor = ohlc.get('open', 0)
        if anchor <= 0:
            return signals

        move = spot - anchor
        vwap = indicators.get('vwap', 0)

        # Determine expiry
        if symbol == "BANKNIFTY":
            days_to_expiry = (2 - dow) % 7
        elif symbol == "SENSEX":
            days_to_expiry = (0 - dow) % 7
        else:
            days_to_expiry = (3 - dow) % 7
        is_expiry = (days_to_expiry == 0 or days_to_expiry == 7)
        T = max(dte, days_to_expiry) / 365 if not is_expiry else 1 / 365
        day_label = "EXPIRY" if is_expiry else f"{days_to_expiry}DTE"

        if move >= gap:
            # UP gap -> BUY CE
            if use_vwap and vwap > 0 and spot < vwap:
                logger.info(f"  WAVE_VWAP_SKIP: {symbol} CE spot={spot:.0f} < VWAP={vwap:.0f}")
                return signals

            strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
            if chain_ltp > MIN_PREMIUM_BUY:
                use_iv = chain_iv if chain_iv > 0 else indicators['iv'] * 1.3
                if chain_iv > 0:
                    g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, 'CE')
                else:
                    g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, 'CE')
                signals.append({
                    'type': 'BUY_CE_WAVE',
                    'strike': strike,
                    'premium': chain_ltp,
                    'greeks': g,
                    'reason': f"Wave v3 [{day_label}]: Up gap={move:.0f}pts (threshold={gap}) [LIVE]",
                    'target': chain_ltp * target_mult,
                    'sl': chain_ltp * sl_mult,
                })

        elif move <= -gap:
            # DOWN gap -> BUY PE
            if use_vwap and vwap > 0 and spot > vwap:
                logger.info(f"  WAVE_VWAP_SKIP: {symbol} PE spot={spot:.0f} > VWAP={vwap:.0f}")
                return signals

            strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte)
            if chain_ltp > MIN_PREMIUM_BUY:
                use_iv = chain_iv if chain_iv > 0 else indicators['iv'] * 1.3
                if chain_iv > 0:
                    g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, 'PE')
                else:
                    g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, 'PE')
                signals.append({
                    'type': 'BUY_PE_WAVE',
                    'strike': strike,
                    'premium': chain_ltp,
                    'greeks': g,
                    'reason': f"Wave v3 [{day_label}]: Down gap={move:.0f}pts (threshold={gap}) [LIVE]",
                    'target': chain_ltp * target_mult,
                    'sl': chain_ltp * sl_mult,
                })

        return signals

    def check_ghost_zone_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Ghost Trade Zone (GTZ) v7 - Manish Maheshwari Methodology.

        Based on the Ghost Trader's Free F&O Masterclass:
        - Grandfather (Candlesticks): Institutional footprint candles
        - Father (Volume): 3x+ volume spike = institutional money injection
        - Mother (OI): Angel API OI validates the zone

        Phase 1: Identify Ghost Zone from institutional candles + volume
        Phase 2: Check exhaustion rule (300pt NIFTY / 1000pt BANKNIFTY)
        Phase 3: Entry at 50% of zone on retest, SL below zone
        """
        signals = []
        ind = indicators
        T = dte / 365
        atr = ind['atr']

        if atr <= 0:
            return signals

        # ---- EXHAUSTION RULE ----
        # Ghost Zone setups are MOST profitable after trend exhaustion
        exhaustion_pts = {'NIFTY': 300, 'BANKNIFTY': 1000, 'SENSEX': 1000}.get(symbol, 300)
        trend_distance = abs(spot - ind.get('prev_close', spot))  # Simplified for real-time

        # ---- OI-Based Zone Validation (The Mother) ----
        oi_data = {}  # strike → {put_oi, call_oi}
        oi_available = False  # v7.4: Track if OI data was fetched successfully
        if self.angel and self.angel._connected:
            try:
                expiry = self._get_nearest_expiry(symbol)
                if expiry:
                    self.angel._throttle()
                    data = self.angel.obj.optionGreek({
                        "name": symbol,
                        "expirydate": expiry
                    })
                    if data and data.get('data'):
                        oi_available = True
                        for sd in data['data']:
                            sd_strike = float(sd.get('strikePrice', 0) or 0)
                            sd_type = sd.get('optionType', '')
                            sd_oi = float(sd.get('opnInterest', 0) or 0)
                            if sd_strike not in oi_data:
                                oi_data[sd_strike] = {'put_oi': 0, 'call_oi': 0}
                            if sd_type == 'PE':
                                oi_data[sd_strike]['put_oi'] = sd_oi
                            elif sd_type == 'CE':
                                oi_data[sd_strike]['call_oi'] = sd_oi
                    else:
                        logger.info(f"  GTZ: No OI data from optionGreek for {symbol} "
                                   f"(BFO/SENSEX not supported) — using price-action zones only")
            except Exception as e:
                logger.debug(f"GTZ OI fetch error: {e}")

        # ---- Find OI-confirmed demand/supply levels ----
        strike_interval = STRIKE_INTERVALS.get(symbol, 50)

        # High PUT OI = institutional support (demand zone)
        # High CALL OI = institutional resistance (supply zone)
        oi_demand_strike = 0
        oi_supply_strike = 0
        max_put_oi = 0
        max_call_oi = 0

        for stk, oi_info in oi_data.items():
            # Only consider strikes near spot (within 5 intervals)
            if abs(stk - spot) > strike_interval * 5:
                continue
            if stk < spot and oi_info['put_oi'] > max_put_oi:
                max_put_oi = oi_info['put_oi']
                oi_demand_strike = stk
            if stk > spot and oi_info['call_oi'] > max_call_oi:
                max_call_oi = oi_info['call_oi']
                oi_supply_strike = stk

        # ---- Combine Price-Action Zones with OI Zones ----
        demand_zones = ind.get('demand_zones', [])
        supply_zones = ind.get('supply_zones', [])

        # ---- DEMAND ZONE RETEST: BUY CE ----
        for dz in demand_zones:
            zone_low = dz['low']
            zone_high = dz['high']
            zone_mid = (zone_low + zone_high) / 2

            # Price dips INTO the zone and bounces (close above zone)
            if ohlc['low'] <= zone_high and ohlc['low'] >= zone_low * 0.998 and spot > zone_high:

                # OI validation (The Mother): check if PUT OI exists near this zone
                oi_label = ""
                oi_score = 0
                if oi_demand_strike > 0 and abs(oi_demand_strike - zone_low) <= strike_interval:
                    oi_score = max_put_oi
                    oi_label = f" [PUT OI={max_put_oi:,.0f}@{oi_demand_strike}]"

                # Exhaustion boost: signal is stronger if trend is exhausted
                exhaustion_label = ""
                if ind.get('demand_strength', 0) >= 2 or oi_score > 10000:
                    pass  # Strong zone — take it
                elif oi_score == 0 and oi_available:
                    continue  # OI data available but no OI backing — skip
                elif oi_score == 0 and not oi_available and ind.get('demand_strength', 0) < 1:
                    continue  # No OI data AND no price-action strength — skip

                # Entry at 50% of zone (institutional limit order level)
                entry_at_mid = zone_mid
                ce_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
                if chain_ltp <= 0:
                    logger.info(f"  GTZ_NO_LTP: {symbol} CE strike={ce_strike} — no live premium, skipping")
                    continue
                use_iv = chain_iv if chain_iv > 0 else ind['iv']
                g = greeks_from_market_price(chain_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE') if chain_iv > 0 else bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, use_iv, 'CE')
                premium = chain_ltp

                if premium > MIN_PREMIUM_BUY:
                    signals.append({
                        'type': 'BUY_CE_GTZ',
                        'strike': ce_strike,
                        'premium': premium,
                        'greeks': g,
                        'reason': f"GTZ v7 Demand: zone={zone_low:.0f}-{zone_high:.0f} "
                                  f"entry@50%={zone_mid:.0f} spot={spot:.0f}"
                                  f"{oi_label}{exhaustion_label} [LIVE]",
                        'target': premium * 1.5,  # v7.5: realistic intraday target (was 2.5x)
                        'sl': premium * 0.35,      # SL below zone
                    })
                    break

        # ---- SUPPLY ZONE RETEST: BUY PE ----
        for sz in supply_zones:
            zone_low = sz['low']
            zone_high = sz['high']
            zone_mid = (zone_low + zone_high) / 2

            if ohlc['high'] >= zone_low and ohlc['high'] <= zone_high * 1.002 and spot < zone_low:

                oi_label = ""
                oi_score = 0
                if oi_supply_strike > 0 and abs(oi_supply_strike - zone_high) <= strike_interval:
                    oi_score = max_call_oi
                    oi_label = f" [CALL OI={max_call_oi:,.0f}@{oi_supply_strike}]"

                if ind.get('supply_strength', 0) >= 2 or oi_score > 10000:
                    pass
                elif oi_score == 0 and oi_available:
                    continue  # OI data available but no OI backing — skip
                elif oi_score == 0 and not oi_available and ind.get('supply_strength', 0) < 1:
                    continue  # No OI data AND no price-action strength — skip

                pe_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte)
                if chain_ltp <= 0:
                    logger.info(f"  GTZ_NO_LTP: {symbol} PE strike={pe_strike} — no live premium, skipping")
                    continue
                use_iv = chain_iv if chain_iv > 0 else ind['iv']
                g = greeks_from_market_price(chain_ltp, spot, pe_strike, T, RISK_FREE_RATE, 'PE') if chain_iv > 0 else bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, use_iv, 'PE')
                premium = chain_ltp

                if premium > MIN_PREMIUM_BUY:
                    signals.append({
                        'type': 'BUY_PE_GTZ',
                        'strike': pe_strike,
                        'premium': premium,
                        'greeks': g,
                        'reason': f"GTZ v7 Supply: zone={zone_low:.0f}-{zone_high:.0f} "
                                  f"entry@50%={zone_mid:.0f} spot={spot:.0f}"
                                  f"{oi_label} [LIVE]",
                        'target': premium * 1.5,  # v7.5: realistic intraday target
                        'sl': premium * 0.35,
                    })
                    break

        return signals

    def check_pcr_vwap_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """PCR+VWAP v2: Institutional Flow Strategy.

        Redesigned from live PCR data analysis (6 days):
        - PCR SHIFT (not absolute level) is the real signal
        - Bullish PCR shift +0.03 predicts UP with 76% accuracy (NIFTY)
        - Bearish PCR shift is UNRELIABLE (32%) so PE entries disabled
        - VWAP used as trend filter, not proximity check
        - SENSEX needs higher threshold (+0.05) due to noise
        """
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = STRIKE_INTERVALS.get(symbol, 100)

        # IV cap
        if ind['iv'] > 0.50:
            logger.info(f"  PCR_SKIP: {symbol} IV={ind['iv']*100:.1f}%% > 50%% cap")
            return signals

        vwap = ind['vwap']
        pcr = ind['pcr']

        # v2 CORE: Use PCR SHIFT from pipeline
        pcr_shift = 0
        if hasattr(self, 'market_pipeline') and self.market_pipeline:
            pcr_shift_data = self.market_pipeline.get_pcr_shift(symbol, window_minutes=15)
            if pcr_shift_data:
                pcr_shift = pcr_shift_data.get('shift', 0)

        # CONDITION 1: PCR shift threshold (symbol-specific)
        pcr_threshold = 0.05 if symbol == 'SENSEX' else 0.03
        if pcr_shift <= pcr_threshold:
            return signals

        # CONDITION 2: Spot above VWAP (trend confirmation)
        if spot < vwap:
            logger.info(f"  PCR_V2_SKIP: {symbol} spot={spot:.0f} < VWAP={vwap:.0f}")
            return signals

        # CONDITION 3: PCR not extreme (>1.5 means potential reversal)
        if pcr > 1.5:
            logger.info(f"  PCR_V2_EXTREME: {symbol} PCR={pcr:.2f} > 1.5")
            return signals

        # CONDITION 4: Intraday body positive (spot > open)
        if spot < ohlc['open']:
            return signals

        # ALL CONDITIONS MET: Generate BUY CE
        ce_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
        if chain_ltp <= 0:
            logger.info(f"  PCR_V2_NO_LTP: {symbol} CE strike={ce_strike}")
            return signals

        use_iv = chain_iv if chain_iv > 0 else ind['iv']
        g = greeks_from_market_price(chain_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE') if chain_iv > 0 else bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, use_iv, 'CE')
        premium = chain_ltp

        if premium > MIN_PREMIUM_BUY and premium < spot * 0.05:
            # Quality score from shift strength (0.03=15, 0.10=50, 0.20=100)
            shift_quality = min(100, int(pcr_shift * 500))

            signals.append({
                'type': 'BUY_CE',
                'strike': ce_strike,
                'premium': premium,
                'greeks': g,
                'reason': (f"PCR+VWAP v2: Bullish shift={pcr_shift:+.3f} "
                          f"PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f} [LIVE]"),
                'target': premium * 1.5,
                'sl': premium * 0.5,
                'quality_score': shift_quality,
            })

            logger.info(f"  PCR_V2_SIGNAL: {symbol} BUY_CE shift={pcr_shift:+.3f} "
                       f"PCR={pcr:.2f} spot>{vwap:.0f}(VWAP) Q={shift_quality}")

        # PE entries DISABLED: bearish PCR shift has only 28-32% accuracy
        # Spot moves UP even when PCR drops (institutional put buying is protective)

        return signals


    def check_trend_rider_signal(self, symbol, spot, ohlc, indicators, dow, dte):
        """Trend Rider v3 — ride big trend days with wide trailing stops.
        v11: NEW strategy. Evaluates once per day at 10:15 using multi-signal confirmation.
        Backtest: PF 5.56, 66.7% WR across NIFTY/BANKNIFTY/SENSEX.

        Returns list with 0 or 1 signal (max 1 trade per day per symbol).
        """
        signals = []
        ind = indicators
        T = dte / 365
        strike_interval = STRIKE_INTERVALS.get(symbol, 100)

        now = datetime.now()
        if now.time() < TR_ENTRY_TIME or now.time() > TR_LAST_ENTRY_TIME:
            return signals

        # Need previous day data for gap, prev trend
        df = self.historical_data.get(symbol)
        if df is None or len(df) < 3:
            return signals

        prev1 = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        prev2 = df.iloc[-3] if len(df) >= 3 else prev1
        prev_close = prev1['Close']

        # Gap check
        day_open = ohlc['open']
        gap_pct = (day_open - prev_close) / prev_close * 100
        if abs(gap_pct) > TR_MAX_GAP_PCT:
            logger.info(f"  TR_SKIP: {symbol} gap={gap_pct:+.2f}% > {TR_MAX_GAP_PCT}% (reversal risk)")
            return signals

        # 1-hour move
        atr = ind['atr']
        if atr <= 0:
            return signals
        move_1h = (spot - day_open) / atr

        # VWAP
        vwap = ind.get('vwap', spot)

        # Check sustained direction (not spike-and-reverse)
        # Use intraday body as proxy for 9:45 direction
        intraday_body = spot - day_open
        early_body = ohlc.get('close', spot) - day_open  # Current body direction

        # Scoring
        bear_score = 0; bull_score = 0
        bear_r = []; bull_r = []

        # 1. Gap (2 pts)
        if gap_pct < -0.3:
            bear_score += 2; bear_r.append(f'gap{gap_pct:.1f}%')
        elif gap_pct > 0.3:
            bull_score += 2; bull_r.append(f'gap+{gap_pct:.1f}%')

        # 2. 1-hour momentum (3 pts — strongest signal)
        if move_1h < -0.20:
            bear_score += 3; bear_r.append(f'1h{move_1h:.2f}')
        elif move_1h > 0.20:
            bull_score += 3; bull_r.append(f'1h+{move_1h:.2f}')

        # 3. VWAP position (1 pt)
        if vwap > 0 and spot < vwap:
            bear_score += 1; bear_r.append('<VW')
        elif vwap > 0 and spot > vwap:
            bull_score += 1; bull_r.append('>VW')

        # 4. PCR (2 pts) — only with real PCR data
        pcr = ind.get('pcr', 1.0)
        pcr_source = ind.get('pcr_source', 'PROXY')
        if pcr_source in ('PIPELINE', 'ANGEL_OI', 'REAL'):
            if pcr < 0.85:
                bear_score += 2; bear_r.append(f'PCR{pcr:.2f}')
            elif pcr > 1.10:
                bull_score += 2; bull_r.append(f'PCR{pcr:.2f}')

        # 5. Previous day trend (1-2 pts)
        prev1_red = prev1['Close'] < prev1['Open']
        prev2_red = prev2['Close'] < prev2['Open']
        prev1_grn = prev1['Close'] > prev1['Open']
        prev2_grn = prev2['Close'] > prev2['Open']
        if prev1_red and prev2_red:
            bear_score += 2; bear_r.append('2xRed')
        elif prev1_red:
            bear_score += 1; bear_r.append('pRed')
        if prev1_grn and prev2_grn:
            bull_score += 2; bull_r.append('2xGrn')
        elif prev1_grn:
            bull_score += 1; bull_r.append('pGrn')

        # 6. Making new low/high in first hour (1 pt)
        if spot < day_open and spot <= ohlc['low'] * 1.001:
            bear_score += 1; bear_r.append('newLo')
        elif spot > day_open and spot >= ohlc['high'] * 0.999:
            bull_score += 1; bull_r.append('newHi')

        # Generate signal
        if bear_score >= TR_MIN_SCORE:
            pe_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte)
            if chain_ltp <= 0:
                logger.info(f"  TR_NO_LTP: {symbol} PE strike={pe_strike} — no live premium, skipping")
            else:
                use_iv = chain_iv if chain_iv > 0 else ind['iv']
                g = greeks_from_market_price(chain_ltp, spot, pe_strike, T, RISK_FREE_RATE, 'PE') if chain_iv > 0 else bs_greeks(spot, pe_strike, T, RISK_FREE_RATE, use_iv, 'PE')
                premium = chain_ltp
                if premium > MIN_PREMIUM_BUY:
                    reason_str = '+'.join(bear_r)
                    signals.append({
                        'type': 'BUY_PE',
                        'strike': pe_strike,
                        'premium': premium,
                        'greeks': g,
                        'reason': f"TrendRider DN {reason_str} sc={bear_score} [LIVE]",
                        'target': premium * TR_TARGET_MULT,
                        'sl': premium * TR_SL_MULT,
                        'trend_rider_score': bear_score,
                    })

        elif bull_score >= TR_MIN_SCORE:
            ce_strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
            if chain_ltp <= 0:
                logger.info(f"  TR_NO_LTP: {symbol} CE strike={ce_strike} — no live premium, skipping")
            else:
                use_iv = chain_iv if chain_iv > 0 else ind['iv']
                g = greeks_from_market_price(chain_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE') if chain_iv > 0 else bs_greeks(spot, ce_strike, T, RISK_FREE_RATE, use_iv, 'CE')
                premium = chain_ltp
                if premium > MIN_PREMIUM_BUY:
                    reason_str = '+'.join(bull_r)
                    signals.append({
                        'type': 'BUY_CE',
                        'strike': ce_strike,
                        'premium': premium,
                        'greeks': g,
                        'reason': f"TrendRider UP {reason_str} sc={bull_score} [LIVE]",
                        'target': premium * TR_TARGET_MULT,
                        'sl': premium * TR_SL_MULT,
                        'trend_rider_score': bull_score,
                    })

        return signals

    def check_survivor_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Survivor V2 - Raahi Bhushan option selling.
        v2.5.2: RE-ENABLED with single-lot constraint. Needs Rs 95-100K margin/lot.
        Only trades when portfolio has enough free capital for margin.
        """
        signals = []
        ind = indicators
        T = dte / 365
        atr = ind['atr']

        if dow > 3:
            return signals

        # v2.5.2: Check if enough capital for at least 1 lot margin
        margin_needed = MARGIN_PER_LOT.get(symbol, 120000)
        if self.portfolio.capital < margin_needed:
            return signals

        # Day-specific distances (RESTORED wider values)
        if dow == 0:  # Monday
            gap = max(atr * 0.4, 200)
            distance = max(atr * 0.8, 200)
        elif dow == 1:  # Tuesday
            gap = max(atr * 0.3, 120)
            distance = max(atr * 0.7, 150)
        elif dow == 2:  # Wednesday
            gap = max(atr * 0.2, 70)
            distance = max(atr * 0.5, 100)
        else:  # Thursday
            gap = max(atr * 0.15, 50)
            distance = max(atr * 0.4, 80)

        # PE SELLING - price breaks above resistance
        strike_interval = STRIKE_INTERVALS.get(symbol, 100)
        if ohlc['high'] > ind['resistance'] + gap:
            pe_strike = round((spot - distance) / strike_interval) * strike_interval
            _, sell_ltp, sell_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte, target_strike=pe_strike)
            if sell_ltp > 0 and sell_ltp > MIN_PREMIUM_SELL:
                g = greeks_from_market_price(sell_ltp, spot, pe_strike, T, RISK_FREE_RATE, 'PE')
                signals.append({
                    'type': 'SELL_PE',
                    'strike': pe_strike,
                    'premium': sell_ltp,
                    'greeks': g,
                    'reason': f"Survivor: PE sell at {pe_strike:.0f} "
                              f"dist={distance:.0f} gap={gap:.0f} res={ind['resistance']:.0f} [LIVE]",
                    'target': sell_ltp * 0.3,
                    'sl': sell_ltp * 0.8,
                })

        # CE SELLING - price breaks below support
        if ohlc['low'] < ind['support'] - gap:
            ce_strike = round((spot + distance) / strike_interval) * strike_interval
            _, sell_ltp, sell_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte, target_strike=ce_strike)
            if sell_ltp > 0 and sell_ltp > MIN_PREMIUM_SELL:
                g = greeks_from_market_price(sell_ltp, spot, ce_strike, T, RISK_FREE_RATE, 'CE')
                signals.append({
                    'type': 'SELL_CE',
                    'strike': ce_strike,
                    'premium': sell_ltp,
                    'greeks': g,
                    'reason': f"Survivor: CE sell at {ce_strike:.0f} "
                              f"dist={distance:.0f} gap={gap:.0f} sup={ind['support']:.0f} [LIVE]",
                    'target': sell_ltp * 0.3,
                    'sl': sell_ltp * 0.8,
                })

        return signals


# ====================================================================
# MAIN PAPER TRADING LOOP
# ====================================================================

def auto_refresh_zerodha_token():
    """Auto-regenerate Zerodha token if expired. Called when Zerodha auth fails."""
    try:
        import subprocess
        logger.warning("[Zerodha] Token expired - auto-regenerating...")
        result = subprocess.run(
            ["python3", "/root/algo_trading/zerodha_token_gen.py"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and "Done" in result.stdout:
            logger.info("[Zerodha] Token auto-regenerated successfully")
            return True
        else:
            logger.error(f"[Zerodha] Token regen failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        logger.error(f"[Zerodha] Token auto-regen error: {e}")
        return False

class PaperTrader:
    """Main paper trading orchestrator."""

    def __init__(self, ws_feed=None, market_pipeline=None):
        self.angel = AngelConnection()
        self.portfolio = PaperPortfolio()
        self.engine = StrategyEngine(self.angel, self.portfolio)
        self._running = False
        self._wave_anchors = {}  # v17: {symbol: anchor_price} for Wave Extractor
        self._wave_trade_count = defaultdict(int)  # v17: daily trade count per symbol
        self.ws_feed = ws_feed  # Real-time WebSocket price feed (optional)
        self.market_pipeline = market_pipeline  # v9: Real-time NSE/BSE option chain pipeline
        self.engine.market_pipeline = market_pipeline  # v9.6d: Share pipeline with engine
        # v12.0: Zerodha Kite Connect as Source 1 (primary)
        self.zerodha = None
        try:
            from zerodha_feed import ZerodhaFeed
            self.zerodha = ZerodhaFeed()
            if self.zerodha.connect():
                self.engine.zerodha = self.zerodha
                logger.info("[Zerodha] ✅ Initialized as Source 1 for option chain + LTP")
                self._last_ltp_source = 'ZERODHA'
                self.engine._last_ltp_source = 'ZERODHA'
                # v18: Option Premium CPR for WaveBC strategies
                self.option_cpr = OptionPremiumCPR(self.zerodha)
                logger.info('[OptionCPR] Initialized with Zerodha feed')
            else:
                self.zerodha = None
                # v16: Auto-refresh token on auth failure
                if auto_refresh_zerodha_token():
                    try:
                        self.zerodha = ZerodhaFeed()
                        if self.zerodha.connect():
                            self.engine.zerodha = self.zerodha
                            logger.info("[Zerodha] Reconnected after token refresh")
                        else:
                            self.zerodha = None
                    except Exception:
                        self.zerodha = None
                logger.warning("[Zerodha] Connection failed — trying TrueData as Source 2")
        except Exception as e:
            logger.warning(f"[Zerodha] Not available: {e} — trying TrueData as Source 2")

        # v11.2: TrueData DISABLED (v17 - trial expired 2026-03-23)
        # v17: TrueData disabled (trial expired 2026-03-23) — skip entirely
        self.truedata = None
        self.engine.truedata = None
        if not hasattr(self, 'option_cpr'): self.option_cpr = OptionPremiumCPR(self.zerodha)
        if not hasattr(self, "ghost_v8"): self.ghost_v8 = GhostZoneV8()
        self._index_tokens = {
            'NIFTY': {'exchange': 'NSE', 'token': '99926000'},
            'BANKNIFTY': {'exchange': 'NSE', 'token': '99926009'},
            'SENSEX': {'exchange': 'BSE', 'token': '99919000'},
        }
        # v13.0: Calculus engine — intraday VWAP + momentum direction
        from market_calculus import MarketCalculus
        self.calculus = MarketCalculus()
        self.engine.calculus = self.calculus  # Share with StrategyEngine

        # v14.0: Trade Intelligence engine
        from trade_intelligence import TradeIntelligence
        self.trade_intel = TradeIntelligence()

        # Caches to reduce REST API calls (prevents rate limiting AB1004)
        self._ohlc_cache = {}      # {symbol: {'data': ohlc_dict, 'time': datetime}}
        self._greeks_cache = {}    # {cache_key: {'data': greeks, 'time': datetime}}
        self._option_ltp_cache = {}  # {exchange_token: {'ltp': float, 'time': datetime}}
        self._pcr_cache = {}  # v2.5.2: {symbol: {'pcr': float, 'time': datetime}}
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
            logger.info(f"  RESTART_SAFE: Recovered daily_trade_count={self.daily_trade_count} from portfolio state")
        self.exit_history = []          # [{symbol, direction, time}] for re-entry cooldown
        self.ghost_zone_losses = {}     # {(symbol, 'CE'/'PE'): datetime}
        self.pending_reverses = {}      # v10.1: {symbol: {opt_type, spot, ...}} for breakout failure reversal
        self._vix_cache = {'value': None, 'time': None}
        self.current_vix = None
        # v16.1: GIFT Nifty gap bias
        self.gap_analysis = None
        try:
            from gift_nifty import load_gap_analysis
            self.gap_analysis = load_gap_analysis()
            if self.gap_analysis:
                logger.info(f"[GiftNifty] Gap: {self.gap_analysis['direction']} "
                           f"{self.gap_analysis['gap_pct']:+.2f}% | Bias: {self.gap_analysis['bias']} "
                           f"| Lot mult: {self.gap_analysis['lot_multiplier']}x")
            else:
                logger.info("[GiftNifty] No pre-market data available")
        except Exception as e:
            logger.debug(f"[GiftNifty] Load failed: {e}")
        # v15: Two-tier logging for 1-second scan loop
        self._last_full_log_time = 0       # epoch timestamp
        self._full_log_interval = 60       # full indicator dump every 60 seconds
        self.data_logger = None  # v10.2e: Live data logger for backtesting
        # v10.3: Market regime detection
        from market_regime import RegimeDetector
        self.regime_detector = RegimeDetector()
        self._last_logged_regime = {}
        # v11: Trend Rider — track daily entries (max 1 per symbol per day)
        self._trend_rider_today = {}  # {symbol: date_str} — tracks if TR entered today

    def connect(self):
        """Connect to Angel API."""
        # Try Historical first, then Market
        if self.angel.connect('Historical'):
            self.angel.load_instruments()
            return True
        if self.angel.connect('Market'):
            self.angel.load_instruments()
            return True
        logger.error("Failed to connect to Angel One")
        return False

    def get_index_spot(self, symbol):
        """Get current spot price for index.
        Priority: WebSocket cache → REST API → historical close.
        """
        info = self._index_tokens.get(symbol)
        if not info:
            return None

        # 1. Try WebSocket cache (instant, no API call)
        if self.ws_feed:
            ws_ltp = self.ws_feed.get_ltp(info['token'])
            if ws_ltp:
                return ws_ltp

        # 2. Fallback: REST API
        ltp = self.angel.get_ltp(info['exchange'], info['token'])
        if ltp:
            return ltp

        # 3. Fallback: use last close from historical data
        df = self.engine.historical_data.get(symbol)
        if df is not None and len(df) > 0:
            return df['Close'].iloc[-1]
        return None

    def get_india_vix(self):
        """Fetch India VIX value. Cached for 120 seconds.
        Priority: MarketDataPipeline (NSE direct) → Angel API → HV proxy.
        """
        cached = self._vix_cache
        if cached['value'] and cached['time'] and (datetime.now() - cached['time']).total_seconds() < 120:
            return cached['value']

        # Method 0 (v9): Try MarketDataPipeline (NSE direct, most reliable)
        if self.market_pipeline:
            vix = self.market_pipeline.get_vix()
            if vix and 5 < vix < 80:
                self._vix_cache = {'value': vix, 'time': datetime.now()}
                self.current_vix = vix
                return vix

        # Method 1: Try Angel API with multiple known VIX tokens
        for token in ['26017', '99926004']:
            try:
                ltp = self.angel.get_ltp('NSE', token)
                if ltp and ltp > 0:
                    vix = ltp
                    # Normalize: Angel may return VIX × 100, × 1000, or raw
                    if vix > 1000:
                        vix = vix / 1000  # e.g., 23270 → 23.27
                    elif vix > 100:
                        vix = vix / 100   # e.g., 1327 → 13.27
                    # Valid VIX range: 5-80
                    if 5 < vix < 80:
                        self._vix_cache = {'value': vix, 'time': datetime.now()}
                        self.current_vix = vix
                        return vix
            except Exception:
                pass

        # Method 2: Fallback to historical volatility as VIX proxy
        df = self.engine.historical_data.get('NIFTY')
        if df is not None and len(df) > 20:
            log_ret = np.log(df['Close'] / df['Close'].shift(1))
            hv = log_ret.tail(20).std() * np.sqrt(252) * 100
            self.current_vix = max(min(hv, 40), 8)
            self._vix_cache = {'value': self.current_vix, 'time': datetime.now()}
            return self.current_vix
        return None

    def get_vix_multiplier(self):
        """Return threshold multiplier based on VIX level.
        Low VIX (<14): 1.5x (harder to enter, wider exits)
        Normal (14-20): 1.0x
        High VIX (>20): 0.8x (easier to enter)
        """
        vix = self.current_vix
        if vix is None:
            return 1.0
        if vix < VIX_LOW_THRESHOLD:
            return VIX_LOW_MULTIPLIER
        elif vix > VIX_HIGH_THRESHOLD:
            return VIX_HIGH_MULTIPLIER
        return 1.0

    def _get_option_ltp(self, pos):
        """Get real-time option LTP for a position. 4-source fallback.

        v12.0: Zerodha (1st) > TrueData (2nd) > NSE/BSE pipeline (3rd) > Angel API (4th).
        Returns None if all unavailable.
        """
        symbol = pos['symbol']
        opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'

        # Source 1: Zerodha Kite Connect (primary — REST API)
        if hasattr(self, 'zerodha') and self.zerodha and self.zerodha.is_connected:
            try:
                zd_ltp = self.zerodha.get_option_ltp(symbol, pos['strike'], opt_type)
                if zd_ltp and zd_ltp > 0:
                    return zd_ltp
            except Exception as e:
                logger.debug(f"[Zerodha] Option LTP failed for {symbol} {pos['strike']}{opt_type}: {e}")

        # Source 2: TrueData option LTP (secondary — real-time WebSocket)
        if self.truedata and self.truedata.is_connected:
            try:
                td_ltp = self.truedata.get_option_ltp(symbol, pos['strike'], opt_type)
                if td_ltp and td_ltp > 0:
                    return td_ltp
            except Exception as e:
                logger.debug(f"[TrueData] Option LTP failed for {symbol} {pos['strike']}{opt_type}: {e}")

        # Source 2: Pipeline chain LTP from NSE/BSE (fallback when TrueData unavailable)
        # v10.5: Skip stale BSE data for SENSEX (> 5 min old → fall through to Angel BFO)
        try:
            if self.market_pipeline:
                chain = self.market_pipeline.get_option_chain(symbol)
                if chain:
                    # Staleness check for BSE (SENSEX) — BSE session can go stale
                    fetch_time = chain.get('_fetch_time')
                    if symbol == 'SENSEX' and fetch_time:
                        age_sec = (datetime.now() - fetch_time).total_seconds()
                        if age_sec > 300:  # > 5 min
                            logger.debug(f"  BSE_STALE: {symbol} chain data is {age_sec:.0f}s old, using Angel BFO")
                        else:
                            contracts = chain.get(opt_type, [])
                            for c in contracts:
                                if c.get('strike') == pos['strike'] and c.get('ltp', 0) > 0:
                                    return c['ltp']
                    else:
                        contracts = chain.get(opt_type, [])
                        for c in contracts:
                            if c.get('strike') == pos['strike'] and c.get('ltp', 0) > 0:
                                return c['ltp']
        except Exception as e:
            logger.debug(f"  Pipeline exit LTP failed for {symbol} {pos['strike']}{opt_type}: {e}")

        # Source 3: Angel API with 15s cache (last resort)
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        option_token = details.get('option_token')
        exchange = details.get('option_exchange')

        # Backfill for positions opened before this fix (no stored token)
        if not option_token:
            expiry = self._get_nearest_expiry(symbol)
            if expiry:
                option_info = self.angel.find_option_tokens(symbol, expiry, pos['strike'], opt_type)
                if option_info:
                    option_token = str(option_info.get('token', ''))
                    exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
                    # Store in position so we don't re-lookup every cycle
                    if isinstance(details, dict):
                        details['option_token'] = option_token
                        details['option_exchange'] = exchange
                    else:
                        pos['details'] = {'option_token': option_token, 'option_exchange': exchange}

        if not option_token or not exchange:
            return None

        # Check cache (15-second TTL)
        cache_key = f"{exchange}_{option_token}"
        cached = self._option_ltp_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 5:  # v15: 5s (was 15s)
            return cached['ltp']

        try:
            ltp = self.angel.get_ltp(exchange, option_token)
            if ltp and ltp > 0:
                self._option_ltp_cache[cache_key] = {'ltp': ltp, 'time': datetime.now()}
                return ltp
        except Exception as e:
            logger.debug(f"  Option LTP fetch failed for {cache_key}: {e}")

        return None

    def get_intraday_ohlc(self, symbol):
        """Get today's OHLC so far. Cached for 60s to avoid rate limiting."""
        info = self._index_tokens.get(symbol)
        if not info:
            return None

        # Check cache (60-second TTL)
        cached = self._ohlc_cache.get(symbol)
        if cached and (datetime.now() - cached['time']).total_seconds() < 60:
            return cached['data']

        try:
            # Try 5-min candles for today
            today = datetime.now()
            from_date = today.strftime('%Y-%m-%d 09:15')
            to_date = today.strftime('%Y-%m-%d %H:%M')
            data = self.angel.get_historical(
                info['exchange'], info['token'], 'FIVE_MINUTE',
                from_date, to_date
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
                # v13.0: Feed candle data to calculus engine
                if hasattr(self, 'calculus') and self.calculus.bar_count(symbol) < len(data):
                    self.calculus.load_intraday_candles(symbol, data)
                self._ohlc_cache[symbol] = {'data': ohlc, 'time': datetime.now()}
                return ohlc
        except Exception as e:
            logger.error(f"Intraday OHLC error for {symbol}: {e}")

        # Fallback: use LTP as all OHLC
        spot = self.get_index_spot(symbol)
        if spot:
            ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}
            self._ohlc_cache[symbol] = {'data': ohlc, 'time': datetime.now()}
            return ohlc
        return None

    def get_real_pcr_cached(self, symbol):
        """v2.5.2: Get real PCR from option chain OI, cached for 60 seconds (was 300s)."""
        cached = self._pcr_cache.get(symbol)
        if cached and (datetime.now() - cached['time']).total_seconds() < 60:
            return cached['pcr']
        expiry = self._get_nearest_expiry(symbol)
        if expiry:
            pcr = self.angel.get_real_pcr(symbol, expiry)
            if pcr is not None:
                self._pcr_cache[symbol] = {'pcr': pcr, 'time': datetime.now()}
                return pcr
        return None

    def is_market_open(self):
        """Check if equity market is currently open (9:15 AM - 3:30 PM IST, Mon-Fri, non-holidays)."""
        now = datetime.now()
        if now.weekday() > 4:  # Saturday/Sunday
            return False

        # Check NSE/BSE holiday calendar
        from market_holidays import is_nse_holiday
        is_holiday, holiday_name = is_nse_holiday(now.date())
        if is_holiday:
            if not hasattr(self, '_holiday_logged') or self._holiday_logged != now.date():
                logger.warning(f"  NSE HOLIDAY: {holiday_name} — no equity trading today")
                self._holiday_logged = now.date()
            return False

        current_time = now.time()
        return MARKET_OPEN <= current_time <= MARKET_CLOSE

    def scan_all_strategies(self):
        """Scan all strategies across all 3 indices. Runs every 1 second (v15)."""
        # v15: Two-tier logging — full dump every 60s, compact tick line otherwise
        do_full_log = (time.time() - self._last_full_log_time) >= self._full_log_interval
        if do_full_log:
            logger.info("\n" + "=" * 70)
            logger.info("SCANNING ALL STRATEGIES...")
            logger.info("=" * 70)
            self._last_full_log_time = time.time()

        now = datetime.now()
        dow = now.weekday()
        all_signals = []
        scan_data = {}  # v10: Collect indicator data for dashboard live display

        # Reset daily counters at market open
        if now.time() < dtime(9, 16) and self.daily_trade_count > 0:
            logger.info("  DAILY_RESET: Clearing trade count and cooldowns for new day")
            self.daily_trade_count = 0
            self.exit_history = []
            self.ghost_zone_losses = {}
            self._trend_rider_today = {}  # v11: Reset TR daily entries

        # Fetch India VIX for adaptive filtering
        vix = self.get_india_vix()
        vix_mult = self.get_vix_multiplier()
        if vix and do_full_log:
            regime = "LOW" if vix < VIX_LOW_THRESHOLD else ("HIGH" if vix > VIX_HIGH_THRESHOLD else "NORMAL")
            # v18: Compute Option Premium CPR levels at market open
            if hasattr(self, 'option_cpr') and self.option_cpr and not hasattr(self, '_opcpr_computed'):
                try:
                    self.option_cpr.compute_all_levels()
                    self._opcpr_computed = True
                except Exception as e:
                    logger.error('OPTION_CPR: Level computation failed: %s' % e)

            logger.info(f"  INDIA VIX: {vix:.2f} | Regime: {regime} | Filter multiplier: {vix_mult}x")

        # CRITICAL: Block trading outside market hours
        if not self.is_market_open():
            logger.warning(f"  EQUITY MARKET CLOSED (current: {now.strftime('%H:%M:%S')})")
            logger.warning(f"  Market hours: {MARKET_OPEN} - {MARKET_CLOSE}, Mon-Fri")
            logger.warning(f"  Signals will be logged but NOT executed.")
            return all_signals  # Return empty - no trades outside hours

        # v15: Collect spots for compact tick line
        _tick_spots = {}
        for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            if do_full_log:
                logger.info(f"\n--- {symbol} ---")

            spot = self.get_index_spot(symbol)
            if not spot:
                if do_full_log:
                    logger.warning(f"  No spot data for {symbol}")
                continue
            _tick_spots[symbol] = spot

            # v10.2e: Log spot tick for backtesting
            if self.data_logger and spot:
                self.data_logger.log_spot_tick(symbol, spot)

            # v13.0: Feed spot tick to calculus engine
            if hasattr(self, 'calculus'):
                ohlc_for_calc = self.get_intraday_ohlc(symbol)
                vol = ohlc_for_calc.get('volume', 0) if ohlc_for_calc else 0
                self.calculus.add_spot_tick(symbol, spot, vol)

            # v10.3: Update regime detector with latest spot + VIX
            self.regime_detector.update(symbol, spot, vix)
            regime = self.regime_detector.get_regime(symbol)
            regime_params = get_regime_params(regime)
            if self._last_logged_regime.get(symbol) != regime:
                logger.info(f"  REGIME: {symbol} → {regime.value} "
                           f"(TSL trail={regime_params['tsl_trail_distance_pct']}%, "
                           f"BKOUT_FAIL={'OFF' if not regime_params['breakout_fail_enabled'] else 'ON'})")
                self._last_logged_regime[symbol] = regime

            ohlc = self.get_intraday_ohlc(symbol)
            if not ohlc:
                ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}

            if do_full_log:
                logger.info(f"  Spot: {spot:.2f} | O: {ohlc['open']:.2f} H: {ohlc['high']:.2f} "
                           f"L: {ohlc['low']:.2f} C: {ohlc['close']:.2f}")

            indicators = self.engine.compute_indicators(symbol, ohlc)
            if not indicators:
                logger.warning(f"  No indicators for {symbol}")
                continue

            # v9: Replace proxy PCR with real option chain PCR
            # Priority: MarketDataPipeline (NSE/BSE direct) → Angel API → proxy
            real_pcr = None
            pcr_source = 'PROXY'

            if self.market_pipeline:
                real_pcr = self.market_pipeline.get_pcr(symbol)
                if real_pcr is not None:
                    pcr_source = 'PIPELINE'

            if real_pcr is None:
                real_pcr = self.get_real_pcr_cached(symbol)
                if real_pcr is not None:
                    pcr_source = 'ANGEL_OI'

            if real_pcr is not None:
                indicators['pcr'] = real_pcr
            indicators['pcr_source'] = pcr_source

            # v9: Add pipeline-enriched market data to indicators
            if self.market_pipeline:
                # PCR shift detection (like Murarka: 0.80→1.29 = bullish)
                pcr_shift = self.market_pipeline.get_pcr_shift(symbol)
                if pcr_shift:
                    indicators['pcr_shift'] = pcr_shift['shift']
                    indicators['pcr_direction'] = pcr_shift['direction']
                    indicators['pcr_strength'] = pcr_shift['strength']

                # OI sentiment (accumulation vs unwinding)
                oi_sent = self.market_pipeline.get_oi_sentiment(symbol)
                if oi_sent:
                    indicators['oi_sentiment'] = oi_sent['sentiment']
                    indicators['oi_sentiment_reason'] = oi_sent['reason']

                # Premium sentiment (CE/PE premium %change)
                prem_sent = self.market_pipeline.get_premium_sentiment(symbol)
                if prem_sent:
                    indicators['premium_signal'] = prem_sent['signal']

                # VIX regime
                vix_regime = self.market_pipeline.get_vix_regime()
                if vix_regime:
                    indicators['vix_regime'] = vix_regime['regime']
                    indicators['vix_contrarian'] = vix_regime['contrarian_signal']

            # v3.1: Track data quality for this symbol
            data_quality = {'has_iv': indicators.get('iv', 0) > 0,
                           'has_pcr': indicators.get('pcr', 0) > 0,
                           'has_vwap': indicators.get('vwap', 0) > 0,
                           'has_pipeline': pcr_source == 'PIPELINE'}
            indicators['_data_quality'] = data_quality

            if do_full_log:
                logger.info(f"  ATR: {indicators['atr']:.2f} | IV: {indicators['iv']*100:.1f}% | "
                           f"CPR: {indicators['cpr_width']:.3f}% | PCR: {indicators['pcr']:.2f} ({pcr_source})")
                if indicators.get('pcr_shift') is not None:
                    logger.info(f"  PCR Shift: {indicators['pcr_shift']:+.3f} ({indicators.get('pcr_direction', 'N/A')}) | "
                               f"OI: {indicators.get('oi_sentiment', 'N/A')} | "
                               f"Premium: {indicators.get('premium_signal', 'N/A')}")
                logger.info(f"  Pivot: {indicators['pivot']:.0f} | TC: {indicators['tc']:.0f} | "
                           f"BC: {indicators['bc']:.0f}")
                logger.info(f"  Cam R3: {indicators['cam_r3']:.0f} R4: {indicators['cam_r4']:.0f} | "
                           f"S3: {indicators['cam_s3']:.0f} S4: {indicators['cam_s4']:.0f}")

            # v10: Collect live scan data for dashboard
            scan_data[symbol] = {
                'spot': spot,
                'atr': indicators.get('atr', 0),
                'iv': indicators.get('iv', 0),
                'hv': indicators.get('hv', 0),
                'vwap': indicators.get('vwap', 0),
                'pcr': indicators.get('pcr', 0),
                'pcr_shift': indicators.get('pcr_shift'),
                'pcr_direction': indicators.get('pcr_direction', ''),
                'oi_sentiment': indicators.get('oi_sentiment', ''),
                'premium_signal': indicators.get('premium_signal', ''),
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

            dte = max(1, (3 - dow) + 1) if dow <= 3 else 1

            # v11.1: Choppy day detection — block late entries if morning efficiency < 15%
            choppy_blocked = False  # v19: choppy shield DISABLED — both strategies work in all regimes
            if False:  # v19: DISABLED — choppy shield removed
                day_range = ohlc['high'] - ohlc['low']
                net_move = abs(spot - ohlc['open'])
                day_eff = net_move / day_range * 100 if day_range > 0 else 100
                if day_eff < CHOPPY_DAY_EFF_THRESHOLD:
                    choppy_blocked = True
                    logger.info(f"  CHOPPY_DAY_BLOCK: {symbol} efficiency={day_eff:.0f}% "
                               f"(range={day_range:.0f}, net={net_move:.0f}) — blocking new entries")

            # Run active strategies (Survivor HALTED — needs Rs 1L+ capital per symbol)
            strategy_checks = [
                ('CPR', self.engine.check_cpr_signals),
                # DISABLED v19.3 (MC REJECTED): ('Gamma Blast', self.engine.check_gamma_blast_signals),
                # DISABLED v19.3 (MC REJECTED): ('Ghost Zone', self.engine.check_ghost_zone_signals),
                # DISABLED v19.3 (MC REJECTED): ('PCR+VWAP', self.engine.check_pcr_vwap_signals),
                ('Wave', self.engine.check_wave_signals),
                # ('Survivor', self.engine.check_survivor_signals),  # HALTED v7 — needs more capital
            ]

            for strat_name, check_fn in strategy_checks:
                if False:  # v19: choppy shield disabled
                    logger.debug(f"  CHOPPY_SKIP: {strat_name} blocked on choppy day")
                    continue
                signals = check_fn(symbol, spot, ohlc, indicators, dow, dte)
                for sig in signals:
                    sig['strategy'] = strat_name
                    sig['symbol'] = symbol
                    sig['spot'] = spot
                    sig['dte'] = dte
                    all_signals.append(sig)

                    logger.info(f"  SIGNAL [{strat_name}]: {sig['type']} "
                               f"Strike={sig['strike']:.0f} Premium=Rs {sig['premium']:.2f} "
                               f"| {sig['reason']}")


# v19: Ghost Zone v8 — Institutional zone detection (no choppy shield)            if hasattr(self, "ghost_v8") and self.ghost_v8:                self.ghost_v8.update_tick(symbol, spot, now)                gz8_signals = self.ghost_v8.get_signals(symbol, now)                for gz_sig in gz8_signals:                    # Get option premium from Zerodha                    gz_opt_type = "CE" if "CE" in gz_sig["type"] else "PE"                    gz_strike, gz_ltp, gz_iv = self._get_strike_from_chain(symbol, spot, gz_opt_type, dte)                    if gz_ltp > 0:                        from math import log, sqrt, exp                        T = dte / 365                        g = bs_greeks(spot, gz_strike, T, RISK_FREE_RATE, gz_iv if gz_iv > 0 else 0.20, gz_opt_type)                        gz_entry = {                            "type": gz_sig["type"],                            "strategy": "Ghost Zone v8",                            "symbol": symbol,                            "strike": gz_strike,                            "premium": gz_ltp,                            "greeks": g,                            "spot": spot,                            "dte": dte,                            "target": gz_ltp * (1 + gz_sig.get("target_pct", 20) / 100),                            "sl": gz_ltp * (1 - gz_sig.get("sl_pct", 10) / 100),                            "reason": gz_sig["reason"],                            "quality_score": 85,                        }                        all_signals.append(gz_entry)                        logger.info(f"  SIGNAL [Ghost Zone v8]: {gz_sig[chr(39)+type+chr(39)]} "                                   f"Strike={gz_strike:.0f} Premium=Rs {gz_ltp:.2f} "                                   f"| {gz_sig[chr(39)+reason+chr(39)]}")
            # v19.5: Sweep lot boost for ALL strategies
            # If Liquidity Sweep detected near any signal -> add 2 extra lots
            if hasattr(self, 'calculus') and self.calculus:
                try:
                    if self.calculus.bar_count(symbol) >= 25:
                        _sweeps = self.calculus.detect_liquidity_sweep(symbol)
                        if _sweeps and all_signals:
                            for _sig in all_signals:
                                if _sig.get('symbol') == symbol and not _sig.get('_sweep_confirmed'):
                                    _sig['_sweep_confirmed'] = True
                                    _sig['_extra_lots'] = 2
                                    _sig['quality_score'] = min(100, _sig.get('quality_score', 50) + 15)
                                    _sig['reason'] = _sig.get('reason', '') + ' [SWEEP+2LOT]'
                                    logger.info('  SWEEP_LOT_BOOST: %s %s sweep-confirmed, +2 lots' % (symbol, _sig.get('strategy', '')))
                except Exception:
                    pass
            # v18: WaveBC strategies -- Option Premium CPR levels as entry/exit
            if hasattr(self, 'option_cpr') and self.option_cpr : # v19: choppy removed
                for sig in list(all_signals):
                    if sig.get('symbol') != symbol:
                        continue
                    opt_type = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                    strike = sig.get('strike', 0)
                    premium = sig.get('premium', 0)
                    quality = sig.get('quality_score', 50)
                    self.option_cpr.update_premium_tick(symbol, strike, opt_type, premium)

                    # WaveBC: entry just above BC, target=Pivot, SL=below BC
                    allowed_bc, reason_bc, lv_bc = self.option_cpr.should_allow_wavebc(
                        symbol, strike, opt_type, premium)
                    if allowed_bc and lv_bc:
                        wbc = dict(sig)
                        wbc['strategy'] = 'WaveBC'
                        wbc['type'] = 'BUY_' + opt_type + '_WAVEBC'
                        wbc['target'] = self.option_cpr.get_target_wavebc(
                            symbol, strike, opt_type, premium)
                        wbc['sl'] = self.option_cpr.get_sl(
                            symbol, strike, opt_type, premium)
                        bc_val = lv_bc.get('bc', 0)
                        pv_val = lv_bc.get('pivot', 0)
                        wbc['reason'] = 'WaveBC: prem %.0f > BC %.0f, tgt Pivot %.0f' % (
                            premium, bc_val, pv_val)
                        all_signals.append(wbc)
                        logger.info('  SIGNAL [WaveBC]: %s Strike=%.0f Prem=%.2f | %s',
                                    wbc['type'], strike, premium, reason_bc)

                    # WaveBCElite: BC + Q>=80 + momentum rising
                    allowed_e, reason_e, lv_e = self.option_cpr.should_allow_wavebc_elite(
                        symbol, strike, opt_type, premium, quality)
                    if allowed_e and lv_e:
                        rising = self.option_cpr.is_premium_rising(
                            symbol, strike, opt_type)
                        if rising:
                            we = dict(sig)
                            we['strategy'] = 'WaveBCElite'
                            we['type'] = 'BUY_' + opt_type + '_WBCELITE'
                            we['target'] = self.option_cpr.get_target_wavebc_elite(
                                symbol, strike, opt_type, premium)
                            we['sl'] = self.option_cpr.get_sl(
                                symbol, strike, opt_type, premium)
                            bc_val = lv_e.get('bc', 0)
                            r1_val = lv_e.get('r1', 0)
                            we['reason'] = 'WaveBCElite: Q=%.0f prem %.0f > BC %.0f RISING tgt R1 %.0f' % (
                                quality, premium, bc_val, r1_val)
                            all_signals.append(we)
                            logger.info('  SIGNAL [WaveBCElite]: %s Strike=%.0f Q=%.0f | RISING',
                                        we['type'], strike, quality)

            # v11: TREND RIDER — independent scan at 10:15-10:30 (max 1 per symbol per day)
            today_str = now.strftime('%Y-%m-%d')
            tr_entered_today = self._trend_rider_today.get(symbol) == today_str
            if not tr_entered_today and TR_ENTRY_TIME <= now.time() <= TR_LAST_ENTRY_TIME:
                tr_signals = self.engine.check_trend_rider_signal(symbol, spot, ohlc, indicators, dow, dte)
                for sig in tr_signals:
                    sig['strategy'] = 'Trend Rider'
                    sig['symbol'] = symbol
                    sig['spot'] = spot
                    sig['dte'] = dte
                    sig['is_trend_rider'] = True  # Flag for special handling in execute
                    all_signals.append(sig)
                    logger.info(f"  SIGNAL [Trend Rider]: {sig['type']} "
                               f"Strike={sig['strike']:.0f} Premium=Rs {sig['premium']:.2f} "
                               f"| {sig['reason']}")

        
            # ---- v13.1: LIQUIDITY SWEEP (SL Hunt Reversal) ----
            # Detects institutional stop-loss hunts and trades the reversal
            # Hybrid sizing: Q>=45 half-lot, Q>=60 full-lot
            # Backtested 6 days: PF=1.98-2.95, WR=70-78%, DD<1%
            # Timing: 09:20 to 15:15 (needs 5+ bars of data)
            if hasattr(self, 'calculus') : # v19: choppy removed
                ls_start = now.replace(hour=9, minute=16, second=0)
                ls_end = now.replace(hour=15, minute=15, second=0)
                if ls_start <= now <= ls_end:
                    for sw_sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
                        _bc = self.calculus.bar_count(sw_sym)
                        if _bc > 0 and _bc % 20 == 1: logger.info(f"  SWEEP_BARS: {sw_sym} has {_bc} bars")
                        if self.calculus.bar_count(sw_sym) >= 25:
                            sweeps = self.calculus.detect_liquidity_sweep(sw_sym)
                            if not sweeps: continue
                            for sw in sweeps:
                                if sw['quality'] < 45:
                                    continue
                                # Get option chain for the sweep direction
                                sw_spot = self.get_index_spot(sw_sym) or 0
                                if sw_spot <= 0:
                                    continue
                                sw_dte = dte  # Use same DTE as other strategies
                                T = max(sw_dte / 365.0, 1/365.0)
                                sw_type = f"BUY_{sw['direction']}_SWEEP"
                                sw_strike, sw_ltp, sw_iv = self.engine._get_strike_from_chain(
                                    symbol, sw_spot, sw['direction'], sw_dte)
                                if sw_ltp <= 0:
                                    continue
                                g = greeks_from_market_price(
                                    sw_ltp, sw_spot, sw_strike, T, RISK_FREE_RATE,
                                    sw['direction'])
                                if sw_ltp > MIN_PREMIUM_BUY:
                                    # SL at spike peak + 30%
                                    sl_buffer = sw['spike_size'] * 0.3
                                    # Target: 80% of spike retrace
                                    target_move = sw['spike_size'] * 0.8 * 0.5  # delta-adjusted
                                    sig = {
                                        'type': sw_type,
                                        'strike': sw_strike,
                                        'premium': sw_ltp,
                                        'greeks': g,
                                        'reason': (f"Liquidity Sweep: {sw['spike_dir']} spike "
                                                  f"{sw['spike_pct']:.3f}% Z={sw['z_score']:.1f} "
                                                  f"rev={sw['reversal_ratio']:.0%} "
                                                  f"{'AccFlip ' if sw['accel_flip'] else ''}"
                                                  f"Q={sw['quality']} [LIVE]"),
                                        'target': sw_ltp + target_move,
                                        'sl': max(sw_ltp * 0.5, sw_ltp - sl_buffer * 0.5),
                                        'strategy': 'Liquidity Sweep',
                                        'symbol': sw_sym,
                                        'spot': sw_spot,
                                        'dte': sw_dte,
                                        'quality_score': sw['quality'],
                                        '_sweep_quality': sw['quality'],
                                        '_sweep_half_lot': sw['quality'] < 60,
                                    }
                                    all_signals.append(sig)
                                    logger.info(f"  SIGNAL [Liquidity Sweep]: {sw_type} "
                                              f"Strike={sw_strike:.0f} Premium=Rs {sw_ltp:.2f} "
                                              f"| {sig['reason']}")


        # v10: Write live scan data for dashboard (only on full log cycles to reduce I/O)
        if do_full_log:
            self._write_live_scan_data(scan_data, vix)

        # v15: Compact tick line on non-full-log cycles
        if not do_full_log:
            unrealized = sum(p.get('unrealized_pnl', 0) for p in self.portfolio.positions)
            spots_str = ' '.join(f"{s[:3]}={_tick_spots.get(s, 0):.0f}" for s in ['NIFTY', 'BANKNIFTY', 'SENSEX'])
            logger.info(f"  TICK {datetime.now().strftime('%H:%M:%S')} | {spots_str} | "
                       f"Pos:{len(self.portfolio.positions)} UnrlPnL:{unrealized:+,.0f} | "
                       f"Signals:{len(all_signals)}")

        # v17-fix: Compute quality_score for ALL collected signals
        for sig in all_signals:
            if not sig.get("quality_score"):
                try:
                    sig_indicators = self.engine.compute_indicators(
                        sig["symbol"],
                        self.get_intraday_ohlc(sig["symbol"]) or
                        {"open": sig["spot"], "high": sig["spot"], "low": sig["spot"],
                         "close": sig["spot"], "volume": 0})
                    if sig_indicators:
                        sig["quality_score"] = compute_signal_score(sig, sig["spot"], sig_indicators, self.current_vix)
                except Exception:
                    sig["quality_score"] = 50  # default if computation fails

            # v17-fix: Also set ltp_source from last known source
            if not sig.get("ltp_source") or sig.get("ltp_source") == "UNKNOWN":
                sig["ltp_source"] = getattr(self.engine, "_last_ltp_source", getattr(self, "_last_ltp_source", "UNKNOWN"))

        return all_signals

    def execute_paper_signals(self, signals):
        """Execute signals in paper mode with quality filters."""
        if not signals:
            logger.info("\n  No signals generated this scan.")
            return

        # CRITICAL: Block execution outside market hours
        if not self.is_market_open():
            logger.warning(f"  {len(signals)} signals found but MARKET CLOSED - NOT executing")
            return

        # v8.0: No trades before 09:30 — first 15 min inflated premiums, OI spikes, wide spreads
        if datetime.now().time() < EQUITY_FIRST_TRADE_TIME:
            logger.info(f"  EARLY_MARKET_BLOCK: {len(signals)} signals before "
                       f"{EQUITY_FIRST_TRADE_TIME.strftime('%H:%M')} — skipping (market stabilization)")
            return

        # v2.5.2: No new entries after 2:30 PM — trades need 50+ min to develop
        if datetime.now().time() > LAST_ENTRY_TIME:
            logger.warning(f"  LATE_ENTRY_BLOCK: {len(signals)} signals at {datetime.now().strftime('%H:%M')} "
                          f"after {LAST_ENTRY_TIME.strftime('%H:%M')} cutoff — skipping to avoid EOD force close")
            return

        # Daily trade cap check
        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            logger.warning(f"  MAX_TRADES_REACHED: {self.daily_trade_count} trades today (limit {MAX_TRADES_PER_DAY}). Skipping all signals.")
            return

        logger.info(f"\n  {len(signals)} SIGNALS TO EXECUTE (PAPER MODE):")

        # v9.2: PRE-FILTER — Remove conflicting signals within the same scan batch.
        # If two signals for the SAME symbol point in OPPOSITE directions (CE vs PE),
        # keep only the higher-scored one. This prevents a weak GAMMA CE (score=56)
        # from entering when CPR PE (score=85) will immediately flip it.
        by_symbol = {}
        for sig in signals:
            sym = sig['symbol']
            direction = 'CE' if 'CE' in sig['type'] else 'PE'
            score = sig.get('quality_score', 0)
            if sym not in by_symbol:
                by_symbol[sym] = {}
            if direction not in by_symbol[sym]:
                by_symbol[sym][direction] = []
            by_symbol[sym][direction].append(sig)

        suppressed = set()  # Signal IDs to suppress
        for sym, directions in by_symbol.items():
            if 'CE' in directions and 'PE' in directions:
                # Conflict: same symbol, opposing directions
                best_ce = max(directions['CE'], key=lambda s: s.get('quality_score', 0))
                best_pe = max(directions['PE'], key=lambda s: s.get('quality_score', 0))
                ce_score = best_ce.get('quality_score', 0)
                pe_score = best_pe.get('quality_score', 0)
                # Suppress the weaker direction's signals entirely
                if ce_score > pe_score:
                    for s in directions['PE']:
                        suppressed.add(id(s))
                    logger.info(f"  CONFLICT_FILTER: {sym} CE(score={ce_score}) > PE(score={pe_score}) "
                               f"— suppressing {len(directions['PE'])} PE signals")
                elif pe_score > ce_score:
                    for s in directions['CE']:
                        suppressed.add(id(s))
                    logger.info(f"  CONFLICT_FILTER: {sym} PE(score={pe_score}) > CE(score={ce_score}) "
                               f"— suppressing {len(directions['CE'])} CE signals")
                # Equal scores: keep both (let other filters decide)

        signals = [s for s in signals if id(s) not in suppressed]
        if suppressed:
            logger.info(f"  {len(suppressed)} conflicting signals suppressed, {len(signals)} remaining")

        vix_mult = self.get_vix_multiplier()

        # v10.6: VIX Hard Gate — block ALL entries in extreme VIX
        if self.current_vix is not None:
            if self.current_vix > VIX_BLOCK_HIGH:
                logger.warning(f"  SKIP_VIX_EXTREME_HIGH: VIX={self.current_vix:.1f} > {VIX_BLOCK_HIGH} — blocking {len(signals)} entries")
                return
            if self.current_vix < VIX_BLOCK_LOW:
                logger.warning(f"  SKIP_VIX_EXTREME_LOW: VIX={self.current_vix:.1f} < {VIX_BLOCK_LOW} — blocking {len(signals)} entries")
                return

        executed = 0
        skipped = 0
        for sig in signals:
            # Track ALL signals for dummy PnL at EOD
            self.daily_signal_count += 1
            self.daily_signals_all.append(sig)

            # Daily cap re-check inside loop
            if self.daily_trade_count >= MAX_TRADES_PER_DAY:
                logger.info(f"  MAX_TRADES_REACHED: {self.daily_trade_count} trades today. Skipping remaining signals.")
                break

            # v7.7: Strategy-aware duplicate check (v13.2: strike-agnostic — prevents duplicate positions)
            sig_opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            same_strat_dup = [p for p in self.portfolio.positions
                              if p['symbol'] == sig['symbol']
                              and (('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
                              and p['strategy'] == sig['strategy']]
            if same_strat_dup:
                logger.info(f"  SKIP (duplicate): {sig['strategy']} {sig['symbol']} "
                           f"{sig['strike']}{sig_opt_type} — already held @ {same_strat_dup[0]['strike']} (strike-agnostic v13.2)")
                skipped += 1
                continue

            # v9.5: Count prior same-strategy+direction trades today (for score escalation)
            today_str = datetime.now().strftime('%Y-%m-%d')
            same_strat_closed = [t for t in self.portfolio.closed_trades
                if t.get('exit_time', '').startswith(today_str)
                and t.get('symbol') == sig['symbol']
                and t.get('strategy') == sig['strategy']
                and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type]
            reentry_count = len(same_strat_closed)

            # v9.5: Escalating score threshold — each re-entry needs higher conviction
            # 1st: MIN_SIGNAL_SCORE (50), 2nd: +15 (65), 3rd: +30 (80), 4th: +45 (95)
            escalated_min_score = MIN_SIGNAL_SCORE + (reentry_count * SCORE_ESCALATION_PER_REENTRY)
            sig['_escalated_min_score'] = escalated_min_score
            if reentry_count > 0:
                logger.info(f"  REENTRY #{reentry_count+1}: {sig['strategy']} {sig['symbol']} {sig_opt_type} "
                           f"— need score >= {escalated_min_score} (base {MIN_SIGNAL_SCORE} + {reentry_count}x{SCORE_ESCALATION_PER_REENTRY})")

            # v9.5: Max same-direction trades per symbol per day (hard safety cap across all strategies)
            today_same_dir = sum(1 for t in self.portfolio.closed_trades
                if t.get('exit_time', '').startswith(today_str)
                and t.get('symbol') == sig['symbol']
                and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type)
            today_same_dir += sum(1 for p in self.portfolio.positions
                if p['symbol'] == sig['symbol']
                and ('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
            if today_same_dir >= MAX_SAME_DIRECTION_PER_SYMBOL:
                logger.info(f"  SKIP_DIR_CAP: {sig['symbol']} {sig_opt_type} — "
                           f"{today_same_dir} trades today (max {MAX_SAME_DIRECTION_PER_SYMBOL})")
                skipped += 1
                continue

            # v10.4: Cross-strategy dedup — block if ANY strategy already holds same strike+direction
            # Previously this allowed "confirmation" entries, but backtest proved it creates duplicates
            # (e.g., Silver 280000 CE at 13:30 executed by both CPR and Gamma Blast = same position twice)
            cross_strat_dup = [p for p in self.portfolio.positions
                               if p['symbol'] == sig['symbol']
                               and (('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)
                               and p['strategy'] != sig['strategy']]
            if cross_strat_dup:
                logger.info(f"  SKIP_CROSS_DEDUP: {sig['strategy']} {sig['symbol']} {sig['strike']}{sig_opt_type} "
                           f"— already held by {cross_strat_dup[0]['strategy']} @ {cross_strat_dup[0]['strike']}")
                skipped += 1
                continue

            # Check for CONFLICTING positions: no BUY_CE + SELL_CE on same symbol/strike
            opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            is_buy_sig = 'BUY' in sig['type']
            conflicting = [p for p in self.portfolio.positions
                           if p['symbol'] == sig['symbol']
                           and abs(p["strike"] - sig["strike"]) <= 100
                           and (('CE' in p['signal_type']) == (opt_type == 'CE'))
                           and p['is_sell'] == is_buy_sig]  # opposite direction
            if conflicting:
                logger.info(f"  SKIP (conflicting): {sig['type']} conflicts with "
                           f"{conflicting[0]['signal_type']} on {sig['symbol']} {sig['strike']}")
                skipped += 1
                continue

            # v7.6.2: DIRECTION_FLIP — Close losing/breakeven positions, flip to new direction
            # Quality score must meet higher bar before closing an existing position
            all_opposite = [p for p in self.portfolio.positions
                           if p['symbol'] == sig['symbol']
                           and (('CE' in p['signal_type']) != (opt_type == 'CE'))]
            if all_opposite:
                # v7.6.2: Compute quality score for new signal BEFORE deciding to flip
                flip_score = sig.get('quality_score', 0)
                if not flip_score:
                    try:
                        flip_indicators = self.engine.compute_indicators(sig['symbol'],
                            self.get_intraday_ohlc(sig['symbol']) or
                            {'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'],
                             'close': sig['spot'], 'volume': 0})
                        if flip_indicators:
                            flip_score = compute_signal_score(sig, sig['spot'], flip_indicators, self.current_vix)
                            sig['quality_score'] = flip_score
                    except Exception:
                        flip_score = 0

                # Reject flip if new signal quality is below DIRECTION_FLIP threshold
                if flip_score < DIRECTION_FLIP_MIN_SCORE:
                    logger.info(f"  SKIP_FLIP_QUALITY: {sig['strategy']} {sig['symbol']} {sig['type']} "
                               f"score={flip_score} < {DIRECTION_FLIP_MIN_SCORE} — not strong enough to flip")
                    skipped += 1
                    continue

                flip_blocked = False
                for opp in all_opposite:
                    opp_pnl = opp.get('unrealized_pnl', 0)
                    opp_current = opp.get('current_premium', opp['entry_premium'])

                    # v13.0: Calculus-based flip protection — replaces static DCI
                    # Use live momentum/VWAP instead of frozen entry-time DCI
                    if hasattr(self, 'calculus') and self.calculus.bar_count(sig['symbol']) >= 15:
                        calc_allowed, calc_reason, calc_data = self.calculus.should_allow_direction(
                            sig['symbol'], sig['type'], sig['spot'])
                        if not calc_allowed:
                            logger.info(f"  SKIP_FLIP_CALCULUS: {sig['symbol']} {sig['type']} — {calc_reason}")
                            flip_blocked = True
                            break
                        else:
                            logger.info(f"  CALC_FLIP_OK: {sig['symbol']} {sig['type']} — {calc_reason}")
                    else:
                        # Fallback: use original DCI if calculus has no data yet
                        opp_details = opp.get('details', {}) if isinstance(opp.get('details'), dict) else {}
                        existing_dci = opp_details.get('dci', 0)
                        if existing_dci > 60:
                            logger.info(f"  SKIP_FLIP_DCI: Existing {opp['id']} ({opp['strategy']} "
                                       f"{opp['signal_type']}) DCI={existing_dci:.0f} > 60 — direction strong, "
                                       f"blocking flip to {sig['type']}")
                            flip_blocked = True
                            break

                    # v7.7: Only flip LOSING positions. Profitable ones run regardless of breakeven_locked.
                    if opp_pnl <= 0:
                        logger.info(f"  DIRECTION_FLIP: Closing {opp['id']} ({opp['strategy']} "
                                   f"{opp['signal_type']}) PnL Rs {opp_pnl:.0f} (losing) "
                                   f"to flip to {sig['type']} (score={flip_score})")
                        self.portfolio.close_position(opp['id'], opp_current, 'DIRECTION_FLIP')
                        self._track_exit(opp, 'DIRECTION_FLIP')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(opp['symbol'], 50)
                            cap = MARGIN_PER_LOT.get(opp['symbol'], 120000) if opp['is_sell'] else opp['entry_premium'] * lot_size
                            notify_trade_exit(market="EQUITY", strategy=opp['strategy'],
                                symbol=opp['symbol'], signal_type=opp['signal_type'],
                                strike=opp['strike'], entry_price=opp['entry_premium'],
                                exit_price=opp_current, entry_time=opp['timestamp'],
                                pnl=opp_pnl, capital_used=cap,
                                exit_reason='DIRECTION_FLIP')
                        except Exception:
                            pass
                    else:
                        # Position is profitable — let it run (even if breakeven-locked)
                        logger.info(f"  SKIP_DIRECTION: {sig['strategy']} {sig['symbol']} {sig['type']} "
                                   f"— {opp['strategy']} {opp['signal_type']} is profitable "
                                   f"Rs {opp_pnl:.0f} (let it run)")
                        flip_blocked = True
                if flip_blocked:
                    skipped += 1
                    continue

            # ---- v7.2: Per-strategy capital usage LOGGING (shared pool, no hard caps) ----
            strat_name = sig.get('strategy', 'Unknown')
            strat_used = get_strategy_used_capital(self.portfolio.positions, strat_name)
            strat_target_pct = STRATEGY_ALLOCATION.get(strat_name, 0.10) * 100
            logger.info(f"  STRAT_USAGE: {strat_name} using Rs {strat_used:,.0f} "
                       f"(target {strat_target_pct:.0f}% of Rs {EQUITY_CAPITAL:,.0f} pool)")

            # ---- v10.1: Pending reverse check (breakout failure → opposite direction fast entry) ----
            is_reverse_signal = False
            pending_rev = self.pending_reverses.get(sig['symbol'])
            if pending_rev and pending_rev.get('opt_type') == sig_opt_type:
                # Check staleness — expire reverses older than 10 min
                try:
                    rev_ts = datetime.fromisoformat(pending_rev['timestamp'])
                    rev_age = (datetime.now() - rev_ts).total_seconds()
                    if rev_age <= 600:  # 10 min
                        is_reverse_signal = True
                        logger.info(f"  REVERSE_MATCH: {sig['symbol']} BUY_{sig_opt_type} matches pending reverse "
                                   f"from {pending_rev.get('source_id', '?')} ({int(rev_age)}s ago) — "
                                   f"skipping cooldown, score +15")
                        del self.pending_reverses[sig['symbol']]
                    else:
                        logger.info(f"  REVERSE_EXPIRED: {sig['symbol']} reverse was {int(rev_age)}s ago (>600s), ignoring")
                        del self.pending_reverses[sig['symbol']]
                except Exception as e:
                    logger.warning(f"  REVERSE_CHECK_ERR: {sig['symbol']} — {e}")

            # ---- v2.3: Re-entry cooldown check (v9.5: escalating + elif fix) ----
            # v10.1: Reverse signals bypass cooldown entirely
            if is_reverse_signal:
                logger.info(f"  REVERSE_COOLDOWN_BYPASS: {sig['symbol']} {sig_opt_type} — reverse signal, skipping cooldown")
            now = datetime.now()
            cooldown_hit = False

            # v9.5: Escalating cooldown — count today's same-direction losses
            if not is_reverse_signal:
                today_str_cd = datetime.now().strftime('%Y-%m-%d')
                dir_losses = sum(1 for t in self.portfolio.closed_trades
                    if t.get('exit_time', '').startswith(today_str_cd)
                    and t.get('symbol') == sig['symbol']
                    and ('CE' if 'CE' in t.get('signal_type', '') else 'PE') == sig_opt_type
                    and (t.get('net_pnl', 0) < 0 or
                         (t.get('exit_premium', 0) - t.get('entry_premium', 0)) < 0))
                escalated_cooldown = REENTRY_COOLDOWN_SECONDS * (2 ** min(dir_losses, 4))


                for eh in self.exit_history:
                    if eh['symbol'] == sig['symbol']:
                        elapsed = (now - eh['time']).total_seconds()
                        # v9.5 fix: Two independent checks (was elif — bug where expired FLIP blocked nothing)
                        # DIRECTION_FLIP exits block ANY direction re-entry for 15 min
                        if eh.get('reason') == 'DIRECTION_FLIP' and elapsed < DIRECTION_FLIP_COOLDOWN_SECONDS:
                            logger.info(f"  SKIP_FLIP_COOLDOWN: {sig['symbol']} {sig_opt_type} — "
                                       f"DIRECTION_FLIP exit {int(elapsed)}s ago "
                                       f"(need {DIRECTION_FLIP_COOLDOWN_SECONDS}s any direction)")
                            skipped += 1
                            cooldown_hit = True
                            break
                        # Same-direction cooldown with escalation based on losses
                        if eh['direction'] == sig_opt_type and elapsed < escalated_cooldown:
                            cd_label = "SKIP_ESCALATED_COOLDOWN" if dir_losses > 0 else "SKIP_COOLDOWN"
                            logger.info(f"  {cd_label}: {sig['symbol']} {sig_opt_type} exited {int(elapsed)}s ago "
                                       f"(need {int(escalated_cooldown)}s, {dir_losses} prior losses)")
                            skipped += 1
                            cooldown_hit = True
                            break
            if cooldown_hit:
                continue

            # ---- v2.3: Ghost Zone cooldown check ----
            if 'GTZ' in sig['type'] or 'Ghost' in sig.get('strategy', ''):
                gz_key = (sig['symbol'], sig_opt_type)
                if gz_key in self.ghost_zone_losses:
                    elapsed = (now - self.ghost_zone_losses[gz_key]).total_seconds()
                    if elapsed < GHOST_ZONE_COOLDOWN_SECONDS:
                        logger.info(f"  SKIP_GZ_COOLDOWN: {sig['symbol']} Ghost Zone {sig_opt_type} lost {int(elapsed)}s ago "
                                   f"(need {GHOST_ZONE_COOLDOWN_SECONDS}s)")
                        skipped += 1
                        continue

            # ---- v2.3: VIX-adjusted minimum premium check ----
            # v11.1: BANKNIFTY higher min premium (premiums 3-5x NIFTY, each SL costs more)
            is_sell = 'SELL' in sig['type']
            if is_sell:
                min_prem = MIN_PREMIUM_SELL * vix_mult
            elif sig.get('symbol') == 'BANKNIFTY':
                min_prem = MIN_PREMIUM_BUY_BN * vix_mult
            else:
                min_prem = MIN_PREMIUM_BUY * vix_mult
            if sig['premium'] < min_prem:
                logger.info(f"  SKIP_PREMIUM: {sig['symbol']} {sig['type']} Rs {sig['premium']:.2f} "
                           f"< VIX-adjusted min Rs {min_prem:.2f} (VIX mult={vix_mult}x)")
                skipped += 1
                continue

            # ---- v3.1: Stale CPR pivot validation ----
            # Reject CPR signals where spot is too far from pivot levels (stale pivots)
            if 'CPR' in sig.get('strategy', '') or 'CPR' in sig.get('type', ''):
                sig_indicators = self.engine.compute_indicators(sig['symbol'],
                                 self.get_intraday_ohlc(sig['symbol']) or
                                 {'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'], 'close': sig['spot'], 'volume': 0})
                if sig_indicators:
                    pivot = sig_indicators.get('pivot', 0)
                    if pivot > 0:
                        distance_pct = abs(sig['spot'] - pivot) / pivot * 100
                        if distance_pct > 3.0:  # Spot more than 3% from pivot = stale/wrong data
                            logger.info(f"  SKIP_STALE_CPR: {sig['symbol']} {sig['type']} "
                                       f"spot={sig['spot']:.0f} is {distance_pct:.1f}% from pivot={pivot:.0f} "
                                       f"(max 3%). Pivots may be stale.")
                            skipped += 1
                            continue

            # ---- v13.0: CALCULUS-BASED DIRECTION VALIDATION (replaces v10.1 static VWAP checks) ----
            dir_ohlc = self.get_intraday_ohlc(sig['symbol']) or {
                'open': sig['spot'], 'high': sig['spot'], 'low': sig['spot'], 'close': sig['spot'], 'volume': 0}
            dir_indicators = self.engine.compute_indicators(sig['symbol'], dir_ohlc) or {}

            # v13.0: Use calculus engine as PRIMARY direction gate
            calc_dir_valid = True
            dir_adj = 0
            if hasattr(self, 'calculus') and self.calculus.bar_count(sig['symbol']) >= 15:
                calc_allowed, calc_reason, calc_data = self.calculus.should_allow_direction(
                    sig['symbol'], sig['type'], sig['spot'])
                logger.info(f"  CALC_DIR: {sig['symbol']} {sig['type']} — {calc_reason}")

                if not calc_allowed:
                    # Calculus says wrong direction — try flipping
                    orig_type = sig['type']
                    flipped_type = orig_type.replace('CE', 'PE') if 'CE' in orig_type else orig_type.replace('PE', 'CE')
                    calc_dir = calc_data.get('direction')
                    flipped_opt = 'CE' if 'CE' in flipped_type else 'PE'

                    if calc_dir and flipped_opt == calc_dir:
                        # Calculus direction matches flipped direction — flip signal
                        existing_flipped = [p for p in self.portfolio.positions
                            if p['symbol'] == sig['symbol']
                            and ('CE' if 'CE' in p['signal_type'] else 'PE') == flipped_opt]
                        if existing_flipped:
                            logger.info(f"  DIR_REJECT: {sig['symbol']} {orig_type} — calculus says {calc_dir} "
                                       f"but already holding {flipped_opt}")
                            skipped += 1; continue
                        logger.info(f"  CALC_DIR_FLIP: {sig['symbol']} {orig_type} -> {flipped_type} "
                                   f"(calculus score={calc_data['score']:+.0f}, mom={calc_data['momentum']:+.2f})")
                        sig['type'] = flipped_type
                        dir_adj = 5  # Small boost for calculus-aligned flip
                    else:
                        logger.info(f"  DIR_REJECT: {sig['symbol']} {orig_type} — calculus blocks "
                                   f"(score={calc_data['score']:+.0f})")
                        skipped += 1; continue
                else:
                    # Calculus agrees — give score boost based on confidence
                    dir_adj = min(calc_data.get('confidence', 0) // 10, 10)
            elif dir_indicators:
                # Fallback: old multi-factor validation if calculus has insufficient data
                dir_valid, dir_reason, dir_adj = validate_signal_direction(
                    sig['type'], sig['spot'], dir_ohlc, dir_indicators)
                if not dir_valid:
                    orig_type = sig['type']
                    ema_9 = dir_indicators.get('ema_9', 0)
                    ema_20 = dir_indicators.get('ema_20', 0)
                    flipped_type = orig_type.replace('CE', 'PE') if 'CE' in orig_type else orig_type.replace('PE', 'CE')
                    flip_to_pe = 'PE' in flipped_type
                    if ema_9 > 0 and ema_20 > 0:
                        daily_bullish = ema_9 > ema_20
                        if (daily_bullish and flip_to_pe) or (not daily_bullish and not flip_to_pe):
                            logger.info(f"  DIR_REJECT: {sig['symbol']} {orig_type} -- {dir_reason} "
                                       f"(no flip to {'PE' if flip_to_pe else 'CE'}: "
                                       f"daily EMA {'bullish' if daily_bullish else 'bearish'})")
                            skipped += 1; continue
                    flip_valid, flip_reason, flip_adj = validate_signal_direction(
                        flipped_type, sig['spot'], dir_ohlc, dir_indicators)
                    if flip_valid:
                        flipped_opt = 'CE' if 'CE' in flipped_type else 'PE'
                        existing_flipped = [p for p in self.portfolio.positions
                            if p['symbol'] == sig['symbol']
                            and ('CE' if 'CE' in p['signal_type'] else 'PE') == flipped_opt]
                        if existing_flipped:
                            logger.info(f"  DIR_REJECT: {sig['symbol']} {orig_type} -- {dir_reason} "
                                       f"(flip to {flipped_type} blocked: already holding {flipped_opt})")
                            skipped += 1; continue
                        logger.info(f"  DIR_FLIP: {sig['symbol']} {orig_type} -> {flipped_type} "
                                   f"-- {dir_reason} | flipped: {flip_reason}")
                        sig['type'] = flipped_type
                        dir_adj = flip_adj
                    else:
                        logger.info(f"  DIR_REJECT: {sig['symbol']} {orig_type} -- {dir_reason} "
                                   f"(flip {flipped_type}: {flip_reason})")
                        skipped += 1; continue
                else:
                    logger.info(f"  DIR_PASS: {sig['symbol']} {sig['type']} -- {dir_reason}")
            else:
                dir_adj = 0

            # ---- v9.5: Signal quality score check (MANDATORY — no bypass) ----
            min_score_required = sig.get('_escalated_min_score', MIN_SIGNAL_SCORE)
            indicators = dir_indicators or self.engine.compute_indicators(sig['symbol'], dir_ohlc) or {}
            if indicators:
                score = compute_signal_score(sig, sig['spot'], indicators, self.current_vix)
                score = max(0, min(100, score + dir_adj))  # v10.1: direction adjustment
                # v10.1: Reverse signal score boost (+15)
                if is_reverse_signal:
                    score = min(100, score + 15)
                    logger.info(f"  REVERSE_BOOST: {sig['symbol']} score {score-15} → {score}")
                sig['quality_score'] = score
                if score < min_score_required:
                    logger.info(f"  SKIP_QUALITY: {sig['symbol']} {sig['type']} score={score} < {min_score_required}"
                               f"{' (escalated)' if min_score_required > MIN_SIGNAL_SCORE else ''}")
                    skipped += 1
                    continue
                logger.info(f"  QUALITY_SCORE: {sig['symbol']} {sig['type']} score={score}/100"
                           f" (min required: {min_score_required})")
            else:
                # v9.5 FIX: indicators unavailable — BLOCK trade (was silently bypassing score check)
                logger.warning(f"  SKIP_NO_INDICATORS: {sig['symbol']} {sig['type']} "
                              f"— cannot compute quality score, trade blocked")
                skipped += 1
                continue

            # ---- v13.0: Direction Confidence Index — enhanced with calculus momentum ----
            g = sig['greeks']
            dci = compute_direction_confidence(sig, sig['spot'], indicators, self.current_vix, g)
            # v13.0: Boost/penalize DCI based on calculus momentum alignment
            if hasattr(self, 'calculus') and self.calculus.bar_count(sig['symbol']) >= 15:
                calc_sig = self.calculus.direction_signal(sig['symbol'], sig['spot'])
                sig_dir = 'CE' if 'CE' in sig['type'] else 'PE'
                if calc_sig['direction'] == sig_dir:
                    dci = min(100, dci + 10)  # Calculus agrees — boost
                elif calc_sig['direction'] and calc_sig['confidence'] > 30:
                    dci = max(0, dci - 10)    # Calculus opposes — penalize
                sig['_calculus'] = calc_sig  # Store for logging
            if dci < DCI_MIN_THRESHOLD:
                logger.info(f"  SKIP_DCI: {sig['symbol']} {sig['type']} DCI={dci:.0f} < {DCI_MIN_THRESHOLD} — weak direction")
                skipped += 1
                continue
            # DCI-based lot tier: higher confidence = more capital
            if dci >= 70:
                sig['_lot_tier'] = 'ELITE'      # 30% capital allocation
            elif dci >= 55:
                sig['_lot_tier'] = 'STRONG'     # 20% capital allocation
            else:
                sig['_lot_tier'] = 'BASE'       # 10% capital allocation (minimum)
            logger.info(f"  DCI: {sig['symbol']} {sig['type']} DCI={dci:.0f}/100 tier={sig['_lot_tier']}")
            sig["_dci"] = dci  # Store DCI for trade_intelligence gate


            # ---- v12.0: PHYSICS GATE — mathematical signal quality filter ----
            # Blocks entries where: momentum dying, wave peak, VWAP stretched, RSI extreme
            # Backtested: PF 2.65 -> 3.18, blocks wrong-direction entries mathematically
            if hasattr(self, 'calculus') and self.calculus.bar_count(sig['symbol']) >= 6:
                sig_dir = 'CE' if 'CE' in sig['type'] else 'PE'
                phy_score, phy_diag = self.calculus.physics_gate(sig['symbol'], sig_dir, sig['spot'])
                sig['_physics_score'] = phy_score
                sig['_physics_diag'] = phy_diag
                if phy_score < 0:
                    warnings_list = []
                    if phy_diag.get('mom_dying'): warnings_list.append('MOM_DYING')
                    if phy_diag.get('wave_peak'): warnings_list.append('WAVE_PEAK')
                    if phy_diag.get('stretched'): warnings_list.append('VWAP_STRETCHED')
                    if phy_diag.get('rsi_extreme'): warnings_list.append('RSI_EXTREME')
                    logger.info(f"  PHYSICS_BLOCK: {sig['symbol']} {sig['type']} "
                               f"score={phy_score} [{','.join(warnings_list)}] "
                               f"accel={phy_diag.get('accel','?')} wave={phy_diag.get('wave_risk','?')} "
                               f"vwap={phy_diag.get('vwap_stretch','?')} rsi={phy_diag.get('rsi','?')}")
                    skipped += 1
                    continue
                else:
                    logger.info(f"  PHYSICS_PASS: {sig['symbol']} {sig['type']} "
                               f"score={phy_score} accel={phy_diag.get('accel','?')} "
                               f"wave={phy_diag.get('wave_risk','?')}")
            # ---- v10.4: IV + DTE hard gate (0% WR when IV crushed near expiry) ----
            if sig.get('dte', 5) <= 1 and g and g.get('iv', 0) > 0:
                if g['iv'] < IV_MIN_FOR_DTE0 and sig.get('dte', 5) == 0:
                    logger.info(f"  SKIP_IV_DTE: {sig['symbol']} {sig['type']} IV={g['iv']*100:.1f}% < {IV_MIN_FOR_DTE0*100:.0f}% on expiry day — theta decay dominates")
                    skipped += 1
                    continue
                if g['iv'] < IV_MIN_FOR_DTE1 and sig.get('dte', 5) == 1:
                    logger.info(f"  SKIP_IV_DTE: {sig['symbol']} {sig['type']} IV={g['iv']*100:.1f}% < {IV_MIN_FOR_DTE1*100:.0f}% with DTE=1 — low IV near expiry")
                    skipped += 1
                    continue

            # ---- v2.3: Profit filter check ----
            lot_size = LOT_SIZES.get(sig['symbol'], 50)
            target_mult = sig.get('target', sig['premium'] * 2) / max(sig['premium'], 0.01)
            passes, exp_profit, total_cost = passes_profit_filter(
                sig['premium'], lot_size, target_mult, is_sell
            )
            if not passes:
                logger.info(f"  SKIP_PROFIT: {sig['symbol']} {sig['type']} Rs {sig['premium']:.2f} "
                           f"expected Rs {exp_profit:.0f} < 2x cost Rs {total_cost:.0f}")
                skipped += 1
                continue

            g = sig['greeks']

            # v9.4: Fetch entry OI + real option LTP — pipeline chain LTP is PRIMARY source
            entry_oi = 0
            option_token = None
            option_exchange = None
            mkt_data = None
            real_ltp = None
            opt_type = 'CE' if 'CE' in sig['type'] else 'PE'

            # Source 1: Zerodha Kite LTP (primary)
            if hasattr(self, 'zerodha') and self.zerodha and self.zerodha.is_connected:
                try:
                    zd_ltp = self.zerodha.get_option_ltp(sig['symbol'], sig['strike'], opt_type)
                    if zd_ltp and zd_ltp > 0:
                        real_ltp = zd_ltp
                        logger.info(f"  ZERODHA_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                                   f"LTP={real_ltp:.2f} [Zerodha]")
                except Exception as e:
                    logger.debug(f"[Zerodha] Entry LTP failed for {sig['symbol']}: {e}")

            # Source 2: TrueData LTP (secondary)
            if not real_ltp and self.truedata and self.truedata.is_connected:
                try:
                    td_ltp = self.truedata.get_option_ltp(sig['symbol'], sig['strike'], opt_type)
                    if td_ltp and td_ltp > 0:
                        real_ltp = td_ltp
                        logger.info(f"  TRUEDATA_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                                   f"LTP={real_ltp:.2f} [TrueData]")
                except Exception as e:
                    logger.debug(f"[TrueData] Entry LTP failed for {sig['symbol']}: {e}")

            # Source 3: Pipeline chain LTP from NSE/BSE website
            if not real_ltp:
                try:
                    if self.market_pipeline:
                        chain = self.market_pipeline.get_option_chain(sig['symbol'])
                        if chain:
                            contracts = chain.get(opt_type, [])
                            for c in contracts:
                                if c.get('strike') == sig['strike'] and c.get('ltp', 0) > 0:
                                    real_ltp = c['ltp']
                                    entry_oi = float(c.get('oi', 0) or 0)
                                    logger.info(f"  PIPELINE_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                                               f"LTP={real_ltp:.2f} OI={entry_oi} (from NSE/BSE chain)")
                                    break
                except Exception as e:
                    logger.debug(f"  Pipeline chain lookup failed for {sig['symbol']}: {e}")

            # Source 3: Angel API (fallback if TrueData + pipeline didn't have LTP)
            try:
                expiry = self._get_nearest_expiry(sig['symbol'])
                if expiry:
                    option_info = self.angel.find_option_tokens(
                        sig['symbol'], expiry, sig['strike'], opt_type
                    )
                    if option_info:
                        option_token = str(option_info.get('token', ''))
                        option_exchange = 'BFO' if sig['symbol'] == 'SENSEX' else 'NFO'
                        mkt_data = self.angel.get_market_data(option_exchange, option_token)
                        if mkt_data:
                            if not entry_oi:
                                entry_oi = float(mkt_data.get('opnInterest', mkt_data.get('oi', 0)) or 0)
                            logger.info(f"  ENTRY_OI: {sig['symbol']} {sig['strike']}{opt_type} OI={entry_oi}")
                            # Use Angel LTP only if pipeline didn't provide one
                            if not real_ltp:
                                fetched_ltp = float(mkt_data.get('ltp', 0) or 0)
                                if fetched_ltp > 0:
                                    real_ltp = fetched_ltp
                                    logger.info(f"  ANGEL_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                                               f"LTP={real_ltp:.2f} (Angel API fallback)")
            except Exception as e:
                logger.info(f"  OI/LTP fetch at entry failed for {sig['symbol']}: {e}")

            # v9.4: CRITICAL — Skip trade if no real LTP from ANY source
            # Never enter trades at BS-derived pricing (causes phantom PnL)
            bs_premium = sig['premium']
            if not real_ltp or real_ltp <= 0:
                logger.warning(f"  SKIP_NO_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                              f"— no real LTP from pipeline or Angel API. "
                              f"BS premium Rs {bs_premium:.2f} rejected. Trade skipped.")
                skipped += 1
                continue

            # Replace BS premium with real market LTP + compute real Greeks
            MAX_BS_MARKET_GAP_PCT = 30  # v10.4: Block if BS vs market gap too large
            if real_ltp and real_ltp > 0:
                # v10.4: BS vs Market gap gate — block invalid pricing
                if bs_premium > 0:
                    premium_gap_pct = abs(real_ltp - bs_premium) / bs_premium * 100
                    if premium_gap_pct > MAX_BS_MARKET_GAP_PCT:
                        logger.info(f"  SKIP_PREMIUM_GAP: {sig['symbol']} {sig['type']} "
                                   f"BS={bs_premium:.2f} vs Market={real_ltp:.2f} ({premium_gap_pct:.0f}% gap > {MAX_BS_MARKET_GAP_PCT}%) "
                                   f"— pricing unreliable, trade blocked")
                        skipped += 1
                        continue

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

                # Back-solve for real implied volatility and compute accurate Greeks
                T = max(sig['dte'], 1) / 365
                spot = sig.get('spot', 0)
                if spot > 0:
                    real_greeks = greeks_from_market_price(
                        real_ltp, spot, sig['strike'], T, RISK_FREE_RATE, opt_type
                    )
                    if real_greeks:
                        g = real_greeks  # Replace BS greeks with market-derived greeks
                        logger.info(f"  REAL_GREEKS: {sig['symbol']} {sig['strike']}{opt_type} "
                                   f"IV={g['iv']*100:.1f}% Δ={g['delta']:.3f} Γ={g['gamma']:.6f} "
                                   f"Θ={g['theta']:.2f} | LTP={real_ltp:.2f}")
                    else:
                        logger.info(f"  REAL_GREEKS: IV solve failed, using BS Greeks "
                                   f"IV={g['iv']*100:.1f}% Δ={g['delta']:.3f}")
                logger.info(f"  REAL_LTP: {sig['symbol']} {sig['strike']}{opt_type} "
                           f"BS={bs_premium:.2f} → Market={real_ltp:.2f} "
                           f"Target={sig['target']:.2f} SL={sig['sl']:.2f}")

            # v14.0: Trade Intelligence entry filter
            if hasattr(self, 'trade_intel'):
                ti_allowed, ti_reason, ti_quality = self.trade_intel.should_enter(
                    sig, existing_positions=self.portfolio.positions
                )
                if not ti_allowed:
                    logger.info(f"  TI_BLOCK: {sig.get('symbol','')} {sig.get('type','')} \u2014 {ti_reason}")
                    skipped += 1
                    continue
                logger.info(f"  TI_PASS: {sig.get('symbol','')} {sig.get('type','')} \u2014 {ti_reason}")

# v13.1: Liquidity Sweep sizing — Q<60: 1 lot (min), Q>=60: full tier
            if sig.get('_sweep_half_lot'):
                sig['_lot_tier'] = 'BASE'  # 1 lot only for lower quality sweeps
                logger.info(f"  SWEEP_MIN_LOT: {sig.get('symbol','')} Q={sig.get('_sweep_quality',0)} < 60 -- 1 lot")

            result = self.portfolio.add_signal(
                strategy=sig['strategy'],
                symbol=sig['symbol'],
                signal_type=sig['type'],
                strike=sig['strike'],
                entry_premium=sig['premium'],
                delta=g['delta'],
                gamma=g['gamma'],
                theta=g['theta'],
                iv=g.get('iv', 0.15),
                dte=sig['dte'],
                details={
                    'reason': sig['reason'],
                    'target': sig.get('target'),
                    'sl': sig.get('sl'),
                    'spot': sig.get('spot'),
                    'option_token': option_token,
                    'option_exchange': option_exchange,
                    'quality_score': sig.get('quality_score', 0),  # v2.5: FIX — was missing! Enables dynamic lots
                    'expiry': expiry,  # v9.1: Store expiry for dashboard display
                    'dci': dci,  # v10.4: Direction Confidence Index at entry
                    'calculus': sig.get('_calculus', {}),  # v13.0: Calculus momentum/VWAP at entry
                    'entry_iv': g.get('iv', 0),  # v10.4: IV at entry for BREAKOUT_FAIL scaling
                    'entry_regime': self.regime_detector.get_regime(sig['symbol']) if hasattr(self, 'regime_detector') else 'UNKNOWN',  # v10.4
                    'lot_tier': sig.get('_lot_tier', 'BASE'),  # v10.4: DCI-based lot tier
                },
                oi=entry_oi,
                spot_price=sig.get('spot', 0),
            )
            if result is None:
                skipped += 1
                continue
            executed += 1
            self.daily_trade_count += 1

            # v14.0: Register position with Trade Intelligence
            if hasattr(self, 'trade_intel'):
                try:
                    self.trade_intel.register_position(
                        pos_id=result['id'] if isinstance(result, dict) else str(result),
                        entry_premium=sig['premium'],
                        target=sig.get('target', sig['premium'] * 1.5),
                        sl=sig.get('sl', sig['premium'] * 0.5),
                        lot_size=sig.get('lot_size', 65),
                        num_lots=sig.get('num_lots', 1),
                        is_buy='BUY' in sig.get('type', sig.get('signal_type', 'BUY')),
                        symbol=sig.get('symbol', ''),
                        strategy=sig.get('strategy', ''),
                    )
                except Exception as e:
                    logger.debug(f"  TI register failed: {e}")

            # v11: Track Trend Rider entry (max 1 per symbol per day)
            if sig.get('is_trend_rider') or sig.get('strategy') == 'Trend Rider':
                self._trend_rider_today[sig['symbol']] = datetime.now().strftime('%Y-%m-%d')
                logger.info(f"  TREND_RIDER_ENTERED: {sig['symbol']} marked for today — no more TR entries")

            # Send Telegram notification
            try:
                from trade_notifier import notify_trade_entry
                logger.info(f"  TELEGRAM: Sending entry notification for {sig['symbol']} {sig['type']}")
                lot_size = LOT_SIZES.get(sig['symbol'], 50)
                # Capital used: SELL = margin blocked, BUY = premium paid
                is_sell_sig = 'SELL' in sig['type']
                if is_sell_sig:
                    capital = MARGIN_PER_LOT.get(sig['symbol'], 120000)
                else:
                    capital = sig['premium'] * lot_size
                # Calculate total locked (BUY/SELL aware) and available capital
                locked = 0
                for p in self.portfolio.positions:
                    if p.get('is_sell', False):
                        locked += MARGIN_PER_LOT.get(p['symbol'], 120000)
                    else:
                        locked += p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                total_invested = locked
                capital_available = EQUITY_CAPITAL - locked
                # Add VIX + quality score to reason for Telegram
                vix_info = f" | VIX={self.current_vix:.1f}" if self.current_vix else ""
                score_info = f" | Score={sig.get('quality_score', 'N/A')}"
                trade_info = f" | Trade #{self.daily_trade_count}/{MAX_TRADES_PER_DAY}"
                enriched_reason = sig['reason'] + vix_info + score_info + trade_info
                notify_trade_entry(
                    market="EQUITY", strategy=sig['strategy'],
                    symbol=sig['symbol'], signal_type=sig['type'],
                    strike=sig['strike'], entry_price=sig['premium'],
                    spot=sig.get('spot', 0), lot_size=lot_size,
                    multiplier=1, delta=g['delta'],
                    target=sig.get('target', 0), sl=sig.get('sl', 0),
                    capital_used=capital, reason=enriched_reason,
                    capital_available=capital_available,
                    total_invested=total_invested,
                )
            except Exception as e:
                logger.warning(f"  Telegram notify failed: {e}")

        if skipped:
            logger.info(f"  Executed: {executed} | Skipped: {skipped} | "
                       f"Day total: {self.daily_trade_count}/{MAX_TRADES_PER_DAY}")

    def _get_nearest_expiry(self, symbol):
        """Get nearest weekly/monthly expiry for an index option.
        Returns expiry as 'ddMONyyyy' format (e.g. '27FEB2026') for Angel API.
        """
        if self.angel.instruments is None:
            return None
        exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
        mask = (self.angel.instruments['name'] == symbol) & \
               (self.angel.instruments['exch_seg'] == exchange) & \
               (self.angel.instruments['instrumenttype'].isin(['OPTIDX']))
        matches = self.angel.instruments[mask]
        if len(matches) == 0:
            return None
        # Parse expiry dates and find nearest future expiry
        today = datetime.now().date()
        expiries = pd.to_datetime(matches['expiry'], format='mixed', dayfirst=True).dt.date
        future = expiries[expiries >= today]
        if len(future) == 0:
            return None
        nearest = future.min()
        # Angel optionGreek API expects 'ddMONyyyy' format
        return nearest.strftime('%d%b%Y').upper()

    def fetch_current_oi_iv(self, pos):
        """Fetch current OI and IV using Angel SmartAPI directly.
        Uses get_market_data for OI and get_option_greeks for IV.
        Results are cached for 60s to avoid rate limiting.
        """
        try:
            symbol = pos['symbol']
            strike = pos['strike']
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'

            # Check cache (60s TTL)
            cache_key = f"{symbol}_{strike}_{opt_type}"
            cached = self._greeks_cache.get(cache_key)
            if cached and (datetime.now() - cached['time']).total_seconds() < 60:
                return cached['data']

            current_oi = None
            current_iv = None

            # Method 1: Try to find the option token and get market data (for OI)
            expiry = self._get_nearest_expiry(symbol)
            if expiry:
                option_info = self.angel.find_option_tokens(symbol, expiry, strike, opt_type)
                if option_info:
                    token = str(option_info.get('token', ''))
                    exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
                    mkt_data = self.angel.get_market_data(exchange, token)
                    if mkt_data:
                        current_oi = mkt_data.get('opnInterest', mkt_data.get('oi', 0))
                        # Some feeds include IV in market data
                        if mkt_data.get('impliedVolatility'):
                            current_iv = mkt_data['impliedVolatility']

            # Method 2: Try option Greeks API for IV (if not already obtained)
            if current_iv is None and expiry:
                greeks_data = self.angel.get_option_greeks(symbol, expiry)
                if greeks_data and isinstance(greeks_data, list):
                    for row in greeks_data:
                        row_strike = float(row.get('strikePrice', row.get('strike', 0)))
                        row_type = row.get('optionType', row.get('type', ''))
                        if abs(row_strike - strike) < 10 and opt_type in str(row_type).upper():
                            current_iv = row.get('impliedVolatility', row.get('iv', None))
                            if current_oi is None:
                                current_oi = row.get('openInterest', row.get('oi', None))
                            break

            # Ensure numeric types (API may return strings)
            if current_oi is not None:
                current_oi = float(current_oi)
            if current_iv is not None:
                current_iv = float(current_iv)

            result = (current_oi, current_iv)
            self._greeks_cache[cache_key] = {'data': result, 'time': datetime.now()}

            if current_oi is not None or current_iv is not None:
                logger.info(f"  OI/IV fetched: {pos['id']} OI={current_oi} IV={current_iv}")

            return result
        except Exception as e:
            logger.info(f"  OI/IV fetch failed for {pos['id']}: {e}")
            return None, None

    def check_oi_iv_exit(self, pos, current_premium, current_iv_pct=None):
        """Check if position should exit based on OI velocity + IV changes + Gamma risk.
        current_iv_pct: if provided (from implied vol back-solve), use instead of HV estimate.
        Returns: (exit_reason, should_reverse) or (None, False)

        v2.3: Time-adaptive OI thresholds + BANKNIFTY-specific IV + VIX multiplier.
        """
        entry_oi = pos.get('entry_oi', 0)
        entry_iv = pos.get('entry_iv', 0)

        # Try to fetch current OI and IV
        current_oi, fetched_iv = self.fetch_current_oi_iv(pos)

        # Prefer implied vol from market LTP (passed in), then fetched, then HV fallback
        current_iv = current_iv_pct if current_iv_pct and current_iv_pct > 0 else fetched_iv
        if current_iv is None or current_iv == 0:
            # Use the recalculated IV from BS model as proxy
            df = self.engine.historical_data.get(pos['symbol'])
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                current_iv = max(min(hv * 1.15 * 100, 60), 8)  # as percentage
            else:
                current_iv = entry_iv

        if current_oi is None:
            current_oi = entry_oi

        # Calculate changes
        oi_change = 0
        iv_change = 0

        if entry_oi and entry_oi > 0:
            oi_change = abs(current_oi - entry_oi) / entry_oi * 100

        if entry_iv and entry_iv > 0:
            iv_change = abs(current_iv - entry_iv) / entry_iv * 100

        # v2.3: Time-adaptive OI threshold
        now_time = datetime.now().time()
        if now_time < dtime(10, 15):
            oi_threshold = OI_SURGE_FIRST_HOUR_PCT   # 50% — first hour OI massive
        elif now_time < dtime(14, 0):
            oi_threshold = OI_SURGE_MID_DAY_PCT      # 35% — normal
        else:
            oi_threshold = OI_SURGE_LAST_HOUR_PCT    # 40% — close compression

        # v2.3: Apply VIX multiplier to OI threshold (low VIX = higher threshold)
        vix_mult = self.get_vix_multiplier()
        oi_threshold *= vix_mult

        # v2.4: Strategy-specific threshold multiplier
        strat_name = pos.get('strategy', '').replace(' (Reversal)', '')
        strat_mult = STRATEGY_EXIT_MULT.get(strat_name, {'oi': 1.0, 'iv': 1.0})
        oi_threshold *= strat_mult['oi']

        # v2.3: BANKNIFTY-specific IV threshold
        iv_threshold = IV_SPIKE_PCT_BANKNIFTY if pos['symbol'] == 'BANKNIFTY' else IV_SPIKE_PCT
        iv_threshold *= vix_mult
        iv_threshold *= strat_mult['iv']

        # v2.4: If position is profitable AND moving toward target, hold despite OI/IV
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        target = details.get('target', 0)
        if target > 0 and pos.get('entry_premium', 0) > 0:
            is_sell = pos.get('is_sell', False)
            if is_sell:
                moving_toward_target = current_premium < pos['entry_premium']
            else:
                moving_toward_target = current_premium > pos['entry_premium']
            pnl = pos.get('unrealized_pnl', 0)
            if moving_toward_target and pnl > 0:
                logger.info(f"  OI_TREND_HOLD: {pos['id']} ({strat_name}) profitable Rs {pnl:.0f} "
                           f"& moving toward target. Holding despite OI={oi_change:.1f}%/IV={iv_change:.1f}%.")
                return None, False

        # v2.5.2: Get unrealized PnL for loss-only exits
        unrealized_pnl = pos.get('unrealized_pnl', 0)

        # v3.1: Dynamic loss threshold based on entry cost (not fixed -Rs 200)
        # Use 15% of entry cost as loss threshold — proportional to position size
        entry_premium = pos.get('entry_premium', 0)
        lot_size = pos.get('lot_size', 50)
        entry_cost = entry_premium * lot_size
        oi_loss_threshold = max(-entry_cost * 0.15, -1500)  # 15% of entry cost, min -Rs 1500

        # v8.0: OI/IV signals TIGHTEN SL instead of forcing exit
        # Rule 1: OI surge (time-adaptive) — v8.0: tighten SL, never force exit
        if oi_change > oi_threshold:
            if unrealized_pnl >= oi_loss_threshold:
                # Trade loss is within acceptable range — don't even tighten on OI noise
                logger.info(f"  OI_HOLD: {pos['id']} OI={oi_change:.1f}% > {oi_threshold:.0f}% "
                           f"but PnL Rs {unrealized_pnl:.0f} >= threshold Rs {oi_loss_threshold:.0f}. Holding.")
            else:
                logger.info(f"  OI_SL_TIGHTEN: {pos['id']} OI changed {oi_change:.1f}% > {oi_threshold:.0f}% "
                           f"PnL Rs {unrealized_pnl:.0f} < threshold Rs {oi_loss_threshold:.0f}. "
                           f"(entry={entry_oi}, current={current_oi}, time={now_time.strftime('%H:%M')}). Tightening SL.")
                return 'OI_SL_TIGHTEN', False

        # Rule 2: IV change — v8.0: direction-aware, tighten SL instead of forced exit
        # IV direction: IV DROP hurts BUY positions, IV RISE hurts SELL positions
        is_sell = pos.get('is_sell', False)
        iv_raw_change = ((current_iv - entry_iv) / entry_iv * 100) if entry_iv > 0 else 0
        iv_hurts_position = (iv_raw_change < 0 and not is_sell) or (iv_raw_change > 0 and is_sell)

        if iv_change > iv_threshold:
            entry_premium = pos.get('entry_premium', 0)
            if entry_premium < 30:
                # Low premium options have noisy IV — skip
                logger.info(f"  IV_HOLD_LOW_PREM: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                           f"but entry premium Rs {entry_premium:.2f} < 30. Holding (noisy IV on cheap options).")
            elif not iv_hurts_position:
                # v8.0: IV moving in our FAVOR — no need to tighten
                logger.info(f"  IV_FAVORABLE: {pos['id']} IV changed {iv_raw_change:+.1f}% "
                           f"({'DROP' if iv_raw_change < 0 else 'RISE'}) — favorable for "
                           f"{'SELL' if is_sell else 'BUY'} position. Holding.")
            elif unrealized_pnl >= oi_loss_threshold:
                logger.info(f"  IV_HOLD: {pos['id']} IV={iv_change:.1f}% > {iv_threshold:.0f}% "
                           f"but PnL Rs {unrealized_pnl:.0f} >= threshold Rs {oi_loss_threshold:.0f}. Holding.")
            else:
                logger.info(f"  IV_SL_TIGHTEN: {pos['id']} IV changed {iv_raw_change:+.1f}% > {iv_threshold:.0f}% "
                           f"PnL Rs {unrealized_pnl:.0f} — hurting {'SELL' if is_sell else 'BUY'} position. "
                           f"(entry={entry_iv}%, current={current_iv}%). Tightening SL.")
                return 'IV_SL_TIGHTEN', False

        # Rule 3: Combined OI+IV — v8.0: tighten SL aggressively (10% tighter)
        combo_oi = OI_IV_COMBO_OI * vix_mult
        combo_iv = OI_IV_COMBO_IV * vix_mult
        if oi_change > combo_oi and iv_change > combo_iv:
            logger.info(f"  OI_IV_COMBO_TIGHTEN: {pos['id']} OI={oi_change:.1f}%, IV={iv_change:.1f}%. Tightening SL aggressively.")
            return 'OI_IV_SL_TIGHTEN', False

        # Rule 4: Gamma shield for short positions
        if pos.get('is_sell') and abs(pos.get('gamma', 0)) > GAMMA_SHIELD_THRESHOLD:
            logger.info(f"  GAMMA_SHIELD: {pos['id']} gamma={pos.get('gamma', 0):.6f} > {GAMMA_SHIELD_THRESHOLD}")
            return 'GAMMA_SHIELD_EXIT', False

        return None, False

    def _check_sl_reversal_confirmation(self, pos, spot):
        """Check if SL hit should trigger a reversal based on momentum confirmation."""
        # Don't reverse if already a reversal trade (prevent ping-pong)
        if pos.get('details', {}).get('origin') == 'REVERSAL' or '(Reversal)' in pos.get('strategy', ''):
            return False

        # Don't reverse after 2 PM (not enough time to recover)
        if datetime.now().time() > dtime(14, 0):
            return False

        # Check spot movement direction vs original trade direction
        entry_spot = pos.get('entry_spot', 0)
        if entry_spot == 0:
            return False

        spot_move_pct = (spot - entry_spot) / entry_spot * 100
        is_ce = 'CE' in pos['signal_type']

        # BUY_CE hit SL: spot went down → reverse to BUY_PE if spot moved > 0.3%
        if is_ce and not pos['is_sell'] and spot_move_pct < -0.3:
            return True
        # BUY_PE hit SL: spot went up → reverse to BUY_CE if spot moved > 0.3%
        if not is_ce and not pos['is_sell'] and spot_move_pct > 0.3:
            return True
        # SELL options hit SL: strong momentum against us → always reverse
        if pos['is_sell']:
            return True

        return False

    def _track_exit(self, pos, exit_reason):
        """Track exit for cooldown logic. Called after every close_position."""
        direction = 'CE' if 'CE' in pos['signal_type'] else 'PE'
        self.exit_history.append({
            'symbol': pos['symbol'],
            'direction': direction,
            'time': datetime.now(),
            'reason': exit_reason,
        })
        # Track Ghost Zone losses for extended cooldown
        if ('GTZ' in pos['signal_type'] or 'Ghost' in pos.get('strategy', '')) and \
           pos.get('unrealized_pnl', 0) < 0:
            gz_key = (pos['symbol'], direction)
            self.ghost_zone_losses[gz_key] = datetime.now()
            logger.info(f"  GZ_LOSS_TRACKED: {pos['symbol']} {direction} — 30min cooldown active")
        # Cleanup old exit history (keep last 2 hours only)
        cutoff = datetime.now() - timedelta(hours=2)
        self.exit_history = [eh for eh in self.exit_history if eh['time'] > cutoff]

    def execute_reversal(self, pos, exit_reason, spot=None):
        """After closing a position, open a reverse trade.
        v2.5: DISABLED — reversals net -Rs 7,468 combined on Feb 27 backtest.
        CPR Reversal 0% WR, Gamma Blast Reversal net negative.
        BUG: SELL reversal targets still at broken 0.1x/1.5x (never fixed in v2.4).
        """
        return  # v2.5: Disabled
        try:
            symbol = pos['symbol']
            strategy = pos['strategy']
            original_type = pos['signal_type']

            # Don't chain reversals
            if '(Reversal)' in strategy:
                logger.info(f"  REVERSAL_SKIP: {pos['id']} already a reversal trade. No chain.")
                return

            # Determine reverse direction
            if 'CE' in original_type:
                if pos['is_sell']:
                    reverse_type = 'BUY_CE_' + strategy.upper().replace(' ', '_')
                else:
                    reverse_type = 'SELL_CE'
            else:
                if pos['is_sell']:
                    reverse_type = 'BUY_PE_' + strategy.upper().replace(' ', '_')
                else:
                    reverse_type = 'SELL_PE'

            logger.info(f"  REVERSAL: {pos['id']} → Opening {reverse_type} {symbol} "
                       f"@ strike {pos['strike']} due to {exit_reason}")

            # Get real LTP and option token for the reversed option
            rev_opt_type = 'CE' if 'CE' in reverse_type else 'PE'
            reversal_premium = pos['current_premium'] * 0.75  # fallback
            option_token = None
            entry_oi = pos.get('entry_oi', 0)
            rev_delta = pos.get('delta', 0)
            rev_gamma = pos.get('gamma', 0)
            rev_theta = pos.get('theta', 0)
            rev_iv = pos.get('iv', 15) / 100
            spot_price = spot or pos.get('entry_spot', 0)

            try:
                exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
                expiry = self._get_nearest_expiry(symbol)
                if expiry:
                    option_info = self.angel.find_option_tokens(symbol, expiry, pos['strike'], rev_opt_type)
                    if option_info:
                        option_token = str(option_info.get('token', ''))
                        mkt_data = self.angel.get_market_data(exchange, option_token)
                        if mkt_data:
                            real_ltp = float(mkt_data.get('ltp', 0) or 0)
                            entry_oi = float(mkt_data.get('opnInterest', mkt_data.get('oi', 0)) or 0)
                            if real_ltp > 0:
                                reversal_premium = real_ltp
                                logger.info(f"  REVERSAL_LTP: {symbol} {pos['strike']}{rev_opt_type} "
                                           f"Market={real_ltp:.2f}")
                                # Compute real Greeks
                                T_rev = max(pos.get('dte', 1), 1) / 365
                                real_greeks = greeks_from_market_price(
                                    real_ltp, spot_price, pos['strike'], T_rev, RISK_FREE_RATE, rev_opt_type
                                )
                                if real_greeks:
                                    rev_delta = real_greeks['delta']
                                    rev_gamma = real_greeks['gamma']
                                    rev_theta = real_greeks['theta']
                                    rev_iv = real_greeks['iv']
            except Exception as e:
                logger.warning(f"  Reversal LTP fetch failed: {e}")

            # Set target/SL for reversal
            is_sell_rev = 'SELL' in reverse_type
            if is_sell_rev:
                rev_target = round(reversal_premium * 0.1, 2)
                rev_sl = round(reversal_premium * 1.5, 2)
            else:
                rev_target = round(reversal_premium * 2.5, 2)
                rev_sl = round(reversal_premium * 0.4, 2)

            reverse_pos = self.portfolio.add_signal(
                strategy=strategy + ' (Reversal)',
                symbol=symbol,
                signal_type=reverse_type,
                strike=pos['strike'],
                entry_premium=reversal_premium,
                delta=rev_delta,
                gamma=rev_gamma,
                theta=rev_theta,
                iv=rev_iv,
                dte=pos.get('dte', 1),
                details={'origin': 'REVERSAL', 'original_id': pos['id'], 'exit_reason': exit_reason,
                         'target': rev_target, 'sl': rev_sl,
                         'option_token': option_token, 'spot': spot_price},
                oi=entry_oi,
                spot_price=spot_price,
            )

            if reverse_pos:
                try:
                    from trade_notifier import send_info
                    msg = (f"<b>REVERSAL TRADE</b>\n"
                           f"Closed: {pos['signal_type']} {symbol}\n"
                           f"Opened: {reverse_type} {symbol}\n"
                           f"Reason: {exit_reason}\n"
                           f"Strike: {pos['strike']:.0f}\n"
                           f"Premium: Rs {reversal_premium:.2f}")
                    send_info(msg)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"  Reversal failed for {pos['id']}: {e}")

    def check_exits(self):
        """Check if any open positions should be exited.
        Includes: circuit breaker, trailing SL, OI+IV dynamic exits, time exits,
        EOD force close, and static target/SL.
        """
        if not self.portfolio.positions:
            return

        logger.info("\n  Checking exits for open positions...")

        # ---- CIRCUIT BREAKER: Close ALL positions if daily loss exceeds limit ----
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.portfolio.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS_EQUITY:
            logger.warning(f"  CIRCUIT_BREAKER: Daily loss Rs {daily_loss:,.0f} > limit Rs {MAX_DAILY_LOSS_EQUITY:,.0f}")
            logger.warning(f"  Closing ALL {len(self.portfolio.positions)} open positions!")
            for pos in list(self.portfolio.positions):
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'CIRCUIT_BREAKER')
                self._track_exit(pos, 'CIRCUIT_BREAKER')
            try:
                from trade_notifier import send_message
                send_message(f"<b>CIRCUIT BREAKER TRIGGERED</b>\n"
                           f"Daily loss: Rs {daily_loss:,.0f}\n"
                           f"All equity positions closed. Trading halted.")
            except Exception:
                pass
            self.portfolio.save_state()
            return

        for pos in list(self.portfolio.positions):
            # Skip positions opened less than 15 minutes ago (grace period — v2.3: was 5 min)
            entry_time = datetime.fromisoformat(pos['timestamp'])
            elapsed_secs = (datetime.now() - entry_time).total_seconds()
            # v16: Update premium DURING grace period so PnL is always live
            _grace_ltp = self._get_option_ltp(pos) if hasattr(self, '_get_option_ltp') else None
            if _grace_ltp is not None and _grace_ltp > 0:
                self.portfolio.update_position(pos['id'], _grace_ltp)

            if elapsed_secs < GRACE_PERIOD_SECONDS:
                logger.info(f"  GRACE: {pos['id']} opened {int(elapsed_secs)}s ago (need {GRACE_PERIOD_SECONDS}s) | LTP={_grace_ltp or 'N/A'}")
                continue

            # ---- EOD FORCE CLOSE: Close positions 10 min before market close ----
            if datetime.now().time() > dtime(15, 20):
                logger.info(f"  EOD_CLOSE: {pos['id']} force close (market closing at 15:30)")
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'EOD_FORCE_CLOSE')
                self._track_exit(pos, 'EOD_FORCE_CLOSE')
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = LOT_SIZES.get(pos['symbol'], 50)
                    cap = MARGIN_PER_LOT.get(pos['symbol'], 120000) if pos['is_sell'] else pos['entry_premium'] * lot_size
                    notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                        symbol=pos['symbol'], signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current, entry_time=pos['timestamp'],
                        pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                        exit_reason='EOD_FORCE_CLOSE')
                except Exception:
                    pass
                continue

            symbol = pos['symbol']
            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            spot = self.get_index_spot(symbol)
            if not spot:
                continue

            # ---- v10.6: GREEKS REFRESH — Recalculate every 5 min during hold ----
            _last_refresh = pos.get('greeks_refreshed_at')
            _should_refresh = (_last_refresh is None or
                              (datetime.now() - datetime.fromisoformat(_last_refresh)).total_seconds() >= GREEKS_REFRESH_INTERVAL_SECONDS)
            if _should_refresh and pos.get('strike'):
                try:
                    _opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
                    _elapsed_days = (datetime.now() - entry_time).total_seconds() / 86400
                    _T = max((pos.get('dte', 1) - _elapsed_days) / 365, 1e-6)
                    # v11.2: Use live LTP for greeks instead of stale BS model
                    _curr_prem_for_greeks = pos.get('current_premium', pos['entry_premium'])
                    if _curr_prem_for_greeks > 0:
                        _g = greeks_from_market_price(_curr_prem_for_greeks, spot, pos['strike'], _T, RISK_FREE_RATE, _opt_type)
                    else:
                        _iv = pos.get('iv', 0.15)
                        _iv = _iv / 100 if _iv > 1 else _iv
                        _g = bs_greeks(spot, pos['strike'], _T, RISK_FREE_RATE, _iv, _opt_type)
                    pos['delta'] = round(_g['delta'], 4)
                    pos['gamma'] = round(_g['gamma'], 6)
                    pos['theta'] = round(_g['theta'], 4)
                    pos['greeks_refreshed_at'] = datetime.now().isoformat()
                    logger.debug(f"  GREEKS_REFRESH: {pos['id']} D={_g['delta']:.3f} G={_g['gamma']:.5f} "
                                f"Th={_g['theta']:.3f} (T={_T:.4f}yr)")
                except Exception as e:
                    logger.debug(f"  GREEKS_REFRESH_ERR: {pos.get('id', '?')} — {e}")

            # ---- v10.6: THETA DECAY EXIT — expiry day theta burden check (BUY only) ----
            _elapsed_days_td = (datetime.now() - entry_time).total_seconds() / 86400
            _remaining_dte = max(pos.get('dte', 5) - _elapsed_days_td, 0)
            if _remaining_dte < 1 and not pos.get('is_sell', False):
                _theta_val = abs(pos.get('theta', 0))
                _curr_prem = pos.get('current_premium', pos['entry_premium'])
                if _curr_prem > 0 and _theta_val > 0:
                    _theta_burden_pct = _theta_val / _curr_prem * 100
                    if _theta_burden_pct > THETA_BURDEN_EXIT_PCT and pos.get('unrealized_pnl', 0) < 0:
                        logger.info(f"  THETA_DECAY_EXIT: {pos['id']} theta_burden={_theta_burden_pct:.1f}%/day "
                                   f"> {THETA_BURDEN_EXIT_PCT}% + losing Rs {pos.get('unrealized_pnl', 0):.0f}")
                        self.portfolio.close_position(pos['id'], _curr_prem, 'THETA_DECAY_EXIT')
                        self._track_exit(pos, 'THETA_DECAY_EXIT')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(pos['symbol'], 50)
                            cap = pos['entry_premium'] * lot_size
                            notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                                symbol=pos['symbol'], signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=pos['entry_premium'],
                                exit_price=_curr_prem, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='THETA_DECAY_EXIT')
                        except Exception:
                            pass
                        continue
                    elif _theta_burden_pct > THETA_BURDEN_TIGHTEN_PCT:
                        _new_tsl = round(_curr_prem * THETA_TIGHTEN_SL_FACTOR, 2)
                        if pos.get('trailing_sl') is None or _new_tsl > (pos.get('trailing_sl') or 0):
                            logger.info(f"  THETA_SL_TIGHTEN: {pos['id']} theta_burden={_theta_burden_pct:.1f}%/day "
                                       f"> {THETA_BURDEN_TIGHTEN_PCT}% — SL→Rs {_new_tsl:.2f}")
                            pos['trailing_sl'] = _new_tsl

            # ---- PREMIUM UPDATE: Real market LTP with fallback to delta+gamma+theta ----
            # v3.1: Guard against None values in spot/delta/gamma to prevent NoneType crashes
            entry_spot = pos.get('entry_spot', 0)
            if entry_spot == 0 or entry_spot is None:
                entry_spot = pos.get('details', {}).get('spot', spot) if isinstance(pos.get('details'), dict) else spot
            entry_spot = float(entry_spot) if entry_spot else spot
            spot_change = (spot or 0) - (entry_spot or 0)
            delta_val = float(pos.get('delta', 0) or 0) if pos.get('delta') is not None else 0.5
            gamma_val = float(pos.get('gamma', 0) or 0)
            hours_held = (datetime.now() - entry_time).total_seconds() / 3600

            # Try real option LTP first (15s cached)
            real_option_ltp = self._get_option_ltp(pos)
            if real_option_ltp is not None:
                current_premium = real_option_ltp
                premium_source = 'MARKET'
            else:
                # v11.2: Fallback approximation — only for premium tracking, NOT for entry/exit decisions
                premium_delta = delta_val * spot_change + 0.5 * gamma_val * (spot_change ** 2)
                theta_val = pos.get('theta', 0)
                time_decay = theta_val * (hours_held / 24)
                current_premium = max(pos['entry_premium'] + premium_delta + time_decay, 0.05)
                premium_source = 'APPROX'
                if hours_held > 0.5:  # Only warn after 30 min without live data
                    logger.warning(f"  NO_LIVE_LTP: {pos['id']} using approx premium Rs {current_premium:.2f} "
                                  f"(no market data for {hours_held:.1f}h)")

            self.portfolio.update_position(pos['id'], current_premium)

            # v14.0: Trade Intelligence monitoring
            if hasattr(self, 'trade_intel'):
                try:
                    ti_signal = self.trade_intel.update(pos['id'], current_premium)
                    if ti_signal.action == 'EXIT':
                        logger.info(f"  TI_EXIT: {pos['symbol']} {pos['id']} \u2014 {ti_signal.reason} "
                                    f"P(win)={ti_signal.p_win:.0%} EV={ti_signal.ev:+.1%}")
                        self._track_exit(pos, current_premium, f'TI_{ti_signal.reason}')
                        continue
                    elif ti_signal.action == 'TIGHTEN_SL' and ti_signal.new_sl:
                        if ti_signal.new_sl > pos.get('trailing_sl', 0):
                            old_tsl = pos.get('trailing_sl', 0)
                            pos['trailing_sl'] = ti_signal.new_sl
                            logger.info(f"  TI_TIGHTEN: {pos['symbol']} TSL {old_tsl:.1f} \u2192 {ti_signal.new_sl:.1f} "
                                        f"({ti_signal.reason})")
                    # Log status periodically (every ~30 readings = 5 min)
                    elif hasattr(self.trade_intel, '_trackers') and pos['id'] in self.trade_intel._trackers:
                        tracker = self.trade_intel._trackers[pos['id']]
                        if hasattr(tracker, 'readings') and len(tracker.readings) % 30 == 0:
                            logger.info(f"  TI_STATUS: {pos['symbol']} {ti_signal.reason}")
                except Exception as e:
                    logger.debug(f"  TI update error: {e}")

            # ---- v7.7: COMPUTE HOLD SCORE for signal-based exit decisions ----
            hold_score = 50  # Default moderate
            hold_mins = (datetime.now() - entry_time).total_seconds() / 60
            if hold_mins >= HOLD_SCORE_MIN_HOLD_MINS:
                try:
                    hold_indicators = self.engine.compute_indicators(symbol,
                        {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0})
                    hold_score = compute_hold_score(pos, spot, hold_indicators)
                except Exception:
                    hold_score = 50
            pos['hold_score'] = hold_score
            hold_label = 'STRONG' if hold_score >= HOLD_SCORE_STRONG else 'MODERATE' if hold_score >= HOLD_SCORE_WEAK else 'WEAK'

            # ---- v10.6: PCR SHIFT MONITOR — tighten SL if PCR opposes trade ----
            if hold_mins >= PCR_SHIFT_MIN_HOLD_MINUTES and self.market_pipeline:
                try:
                    _pcr_shift = self.market_pipeline.get_pcr_shift(symbol)
                    if _pcr_shift and _pcr_shift.get('direction', 'NEUTRAL') != 'NEUTRAL':
                        _is_ce = 'CE' in pos['signal_type']
                        _pcr_opposes = ((_is_ce and _pcr_shift['direction'] == 'BEARISH') or
                                       (not _is_ce and _pcr_shift['direction'] == 'BULLISH'))
                        if _pcr_opposes and not pos.get('is_sell', False):
                            _new_pcr_tsl = round(current_premium * (1 - PCR_SHIFT_SL_TIGHTEN_PCT), 2)
                            if pos.get('trailing_sl') is None or _new_pcr_tsl > (pos.get('trailing_sl') or 0):
                                logger.info(f"  PCR_SHIFT_SL_TIGHTEN: {pos['id']} PCR shift={_pcr_shift['shift']:+.3f} "
                                           f"({_pcr_shift['direction']}) opposes BUY_{'CE' if _is_ce else 'PE'} — "
                                           f"SL→Rs {_new_pcr_tsl:.2f}")
                                pos['trailing_sl'] = _new_pcr_tsl
                        elif _pcr_opposes and pos.get('is_sell', False):
                            _new_pcr_tsl = round(current_premium * (1 + PCR_SHIFT_SL_TIGHTEN_PCT), 2)
                            if pos.get('trailing_sl') is None or _new_pcr_tsl < pos['trailing_sl']:
                                logger.info(f"  PCR_SHIFT_SL_TIGHTEN: {pos['id']} PCR shift={_pcr_shift['shift']:+.3f} "
                                           f"({_pcr_shift['direction']}) opposes SELL_{'CE' if _is_ce else 'PE'} — "
                                           f"SL→Rs {_new_pcr_tsl:.2f}")
                                pos['trailing_sl'] = _new_pcr_tsl
                except Exception as e:
                    logger.debug(f"  PCR_SHIFT_CHECK_ERR: {pos.get('id', '?')} — {e}")

            # ---- v7.7: SIGNAL WEAK EXIT — early exit for losing positions with weak signals ----
            if (hold_mins >= HOLD_SCORE_MIN_HOLD_MINS
                    and hold_score < HOLD_SCORE_WEAK
                    and pos.get('unrealized_pnl', 0) < 0
                    and hours_held > 1):
                logger.info(f"  SIGNAL_WEAK_EXIT: {pos['id']} hold_score={hold_score} ({hold_label}) "
                           f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} — signals reversed, early exit")
                self.portfolio.close_position(pos['id'], current_premium, 'SIGNAL_WEAK_EXIT')
                self._track_exit(pos, 'SIGNAL_WEAK_EXIT')
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = LOT_SIZES.get(symbol, 50)
                    cap = MARGIN_PER_LOT.get(symbol, 120000) if pos['is_sell'] else pos['entry_premium'] * lot_size
                    notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                        exit_reason='SIGNAL_WEAK_EXIT')
                except Exception:
                    pass
                continue

            # ---- TIME-BASED EXIT: Close stale positions (v3.1: >4 hours with <15% PnL of max risk) ----
            if hours_held > 4:  # v3.1: Increased from 3 to 4 hours — give trades more time
                # v3.1: Use max_risk as denominator (not premium*lot) for fair comparison across strategies
                max_risk = pos.get('max_risk', pos['entry_premium'] * pos['lot_size'])
                profit_pct = (pos['unrealized_pnl'] / max(max_risk, 1)) * 100
                if abs(profit_pct) < 15:  # v3.1: Raised from 10% — exit only if truly stagnant
                    # v7.7: Override TIME_EXIT if hold score is STRONG (signals still valid)
                    if hold_score >= HOLD_SCORE_STRONG:
                        logger.info(f"  SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                                   f"— overriding TIME_EXIT (signals still valid, letting run)")
                    else:
                        logger.info(f"  TIME_EXIT: {pos['id']} held {hours_held:.1f}h with only "
                                   f"{profit_pct:.1f}% profit (hold_score={hold_score} {hold_label})")
                        self.portfolio.close_position(pos['id'], current_premium, 'TIME_EXIT_NO_PROGRESS')
                        self._track_exit(pos, 'TIME_EXIT_NO_PROGRESS')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(symbol, 50)
                            cap = MARGIN_PER_LOT.get(symbol, 120000) if pos['is_sell'] else pos['entry_premium'] * lot_size
                            notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=pos['entry_premium'],
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='TIME_EXIT_NO_PROGRESS')
                        except Exception:
                            pass
                        continue

            # ---- v10.1: BREAKOUT FAILURE DETECTION ----
            # v11: Skip for Trend Rider — it has wide SL, no breakout check needed
            # v10.3: Regime-aware — disabled on TRENDING days to let winners run
            # v10.4: Regime-aware timer + IV extension + confidence gate
            _skip_breakout_fail = (pos.get('strategy') == 'Trend Rider')
            if not _skip_breakout_fail:
                regime = self.regime_detector.get_regime(symbol)
                regime_params = get_regime_params(regime)
                # v10.4: Dynamic timer from regime params (was hardcoded 5 min)
                bf_check_minutes = regime_params.get('breakout_fail_check_minutes', BREAKOUT_FAIL_CHECK_MINUTES)
                bf_timer_secs = bf_check_minutes * 60
                # v10.4: If entry IV > 40% (high vol), extend timer 50% — big moves take longer
                entry_iv = details.get('entry_iv', 0) if isinstance(pos.get('details'), dict) else 0
                if entry_iv > 0.40:
                    bf_timer_secs = int(bf_timer_secs * 1.5)
                # v10.4: Confidence gate — skip BREAKOUT_FAIL if regime not confident (< 60 data points)
                regime_confident = self.regime_detector.is_regime_confident(symbol) if hasattr(self.regime_detector, 'is_regime_confident') else True
            if not _skip_breakout_fail and regime_params['breakout_fail_enabled'] and regime_confident and GRACE_PERIOD_SECONDS < elapsed_secs <= bf_timer_secs:
                entry_prem_bf = pos['entry_premium']
                if entry_prem_bf > 0:
                    if not pos['is_sell']:
                        peak_bf = pos.get('peak_premium', entry_prem_bf)
                        gain_bf = (peak_bf - entry_prem_bf) / entry_prem_bf * 100
                        drop_bf = (entry_prem_bf - current_premium) / entry_prem_bf * 100
                    else:
                        peak_bf = pos.get('trough_premium', entry_prem_bf)
                        gain_bf = (entry_prem_bf - peak_bf) / entry_prem_bf * 100
                        drop_bf = (current_premium - entry_prem_bf) / entry_prem_bf * 100

                    # Rapid drop: exit + queue reverse
                    if BREAKOUT_FAIL_REVERSE_ENABLED and drop_bf > BREAKOUT_FAIL_REVERSE_DROP_PCT:
                        logger.info(f"  BREAKOUT_FAIL_REVERSE: {pos['id']} dropped {drop_bf:.1f}% in {int(elapsed_secs)}s")
                        self.portfolio.close_position(pos['id'], current_premium, 'BREAKOUT_FAIL_REVERSE')
                        self._track_exit(pos, 'BREAKOUT_FAIL_REVERSE')
                        rev_type = 'PE' if 'CE' in pos['signal_type'] else 'CE'
                        self.pending_reverses[pos['symbol']] = {
                            'opt_type': rev_type, 'spot': spot,
                            'source_id': pos['id'], 'timestamp': datetime.now().isoformat()
                        }
                        logger.info(f"  REVERSE_QUEUED: {pos['symbol']} BUY_{rev_type}")
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(symbol, 50)
                            cap = MARGIN_PER_LOT.get(symbol, 120000) if pos['is_sell'] else entry_prem_bf * lot_size
                            notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=entry_prem_bf,
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='BREAKOUT_FAIL_REVERSE')
                        except Exception:
                            pass
                        continue

                    # v10.4: Near timer end — no movement — exit (no reverse)
                    # v10.3/v10.4: Use regime-aware min gain threshold + dynamic timer
                    bf_min_gain = regime_params['breakout_fail_min_gain_pct']
                    if elapsed_secs >= (bf_timer_secs - 15) and gain_bf < bf_min_gain:
                        logger.info(f"  BREAKOUT_FAIL: {pos['id']} peak gain={gain_bf:.1f}% < {bf_min_gain}% in {int(elapsed_secs)}s (regime={regime.value})")
                        self.portfolio.close_position(pos['id'], current_premium, 'BREAKOUT_FAIL')
                        self._track_exit(pos, 'BREAKOUT_FAIL')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(symbol, 50)
                            cap = MARGIN_PER_LOT.get(symbol, 120000) if pos['is_sell'] else entry_prem_bf * lot_size
                            notify_trade_exit(market="EQUITY", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=entry_prem_bf,
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='BREAKOUT_FAIL')
                        except Exception:
                            pass
                        continue

            # ---- v7.5: TRAILING STOP LOSS — Premium Gain % Based ----
            # Old: TSL thresholds tied to target distance → with 2.5x targets, never triggered
            # New: TSL triggers on actual premium gain % from entry
            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            # v10.1: Use dynamically updated target (from trailing target) if available
            target = pos.get('target', details.get('target', pos['entry_premium'] * 1.5))
            sl = details.get('sl', pos['entry_premium'] * 0.5)

            if not pos['is_sell']:
                # BUY positions: premium going UP is profit
                entry_prem = pos['entry_premium']
                premium_gain_pct = ((current_premium - entry_prem) / entry_prem * 100
                                    if entry_prem > 0 else 0)

                # Update peak premium
                if current_premium > pos.get('peak_premium', entry_prem):
                    pos['peak_premium'] = round(current_premium, 2)

                peak = pos.get('peak_premium', current_premium)
                peak_gain_pct = ((peak - entry_prem) / entry_prem * 100
                                 if entry_prem > 0 else 0)
                profit_from_entry = peak - entry_prem

                # v11: TREND RIDER — Wide TSL (separate from standard phases)
                is_trend_rider = pos.get('strategy') == 'Trend Rider'
                if is_trend_rider:
                    # Only start trailing after 35% gain, trail 28% from peak
                    if peak_gain_pct >= TR_WIDE_TSL_TRIGGER_PCT:
                        wide_tsl = round(peak * (1 - TR_WIDE_TSL_DISTANCE_PCT / 100), 2)
                        if wide_tsl > (pos.get('trailing_sl') or 0):
                            pos['trailing_sl'] = wide_tsl
                            logger.info(f"  TSL_WIDE_TR: {pos['id']} peak={peak_gain_pct:.0f}% → "
                                       f"SL Rs {wide_tsl:.2f} (28% from peak Rs {peak:.2f})")

                    # Check trailing SL hit
                    if pos.get('trailing_sl') and current_premium <= pos['trailing_sl']:
                        logger.info(f"  WIDE_TSL_HIT: {pos['id']} premium {current_premium:.2f} <= "
                                   f"TSL {pos['trailing_sl']:.2f} (Trend Rider)")
                        self.portfolio.close_position(pos['id'], current_premium, 'WIDE_TSL_HIT')
                        self._track_exit(pos, 'WIDE_TSL_HIT')
                        try:
                            from trade_notifier import notify_trade_exit
                            lot_size = LOT_SIZES.get(symbol, 50)
                            cap = entry_prem * lot_size
                            notify_trade_exit(
                                market="EQUITY", strategy=pos['strategy'],
                                symbol=symbol, signal_type=pos['signal_type'],
                                strike=pos['strike'], entry_price=entry_prem,
                                exit_price=current_premium, entry_time=pos['timestamp'],
                                pnl=pos.get('unrealized_pnl', 0), capital_used=cap,
                                exit_reason='WIDE_TSL_HIT',
                            )
                        except Exception:
                            pass
                        continue

                    # Trend Rider: skip standard TSL phases, go directly to target/SL/EOD checks
                    # (handled by static target & SL below)

                else:
                    # ---- Standard strategies: TSL phases ----

                    # v10.4: Phase 0 — Micro-gains protection (5%+ peaks that never reach Phase 1)
                    if (peak_gain_pct >= TSL_MICRO_GAIN_PCT
                            and elapsed_secs >= TSL_MICRO_MIN_HOLD_SECONDS
                            and not pos.get('breakeven_locked')):
                        # Set trailing SL = entry + 40% of peak gain (floor at entry - 2%)
                        micro_sl = round(entry_prem + (profit_from_entry * (1 - TSL_MICRO_TRAIL_DISTANCE_PCT / 100)), 2)
                        micro_sl = max(micro_sl, round(entry_prem * 0.98, 2))  # Floor: entry - 2%
                        if micro_sl > (pos.get('trailing_sl') or 0):
                            pos['trailing_sl'] = micro_sl
                            logger.info(f"  TSL_MICRO: {pos['id']} peak={peak_gain_pct:.1f}% → SL Rs {micro_sl:.2f} "
                                       f"(keeping {100-TSL_MICRO_TRAIL_DISTANCE_PCT}% of peak gain)")

                # Phase 1: Lock breakeven when premium gains 15%+
                if not is_trend_rider and peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 1.03, 2)  # entry + 3% buffer
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} gained {peak_gain_pct:.0f}% → locked SL at Rs {pos['trailing_sl']:.2f}")

                # v10.3: Regime-aware TSL distances (skip for Trend Rider — uses wide TSL above)
                if not is_trend_rider:
                    regime = self.regime_detector.get_regime(symbol)
                    regime_params = get_regime_params(regime)
                    r_trail_dist = regime_params['tsl_trail_distance_pct']
                    r_tight_dist = regime_params['tsl_tight_distance_pct']

                    # Phase 3: Tight trail when premium gained 40%+ (regime-aware distance)
                    if peak_gain_pct >= TSL_TIGHT_GAIN_PCT:
                        new_trailing_sl = round(peak - (profit_from_entry * r_tight_dist / 100), 2)
                        new_trailing_sl = max(new_trailing_sl, pos.get('trailing_sl') or 0)
                        if new_trailing_sl > (pos.get('trailing_sl') or 0):
                            pos['trailing_sl'] = new_trailing_sl
                            logger.info(f"  TSL_TIGHT: {pos['id']} SL→Rs {pos['trailing_sl']:.2f} "
                                       f"(peak={peak:.2f} +{peak_gain_pct:.0f}%, phase3, trail={r_tight_dist}%)")

                    # Phase 2: Trail when premium gained 25%+ (regime-aware distance)
                    elif peak_gain_pct >= TSL_TRAIL_GAIN_PCT:
                        new_trailing_sl = round(peak - (profit_from_entry * r_trail_dist / 100), 2)
                        new_trailing_sl = max(new_trailing_sl, pos.get('trailing_sl') or 0)
                        if new_trailing_sl > (pos.get('trailing_sl') or 0):
                            pos['trailing_sl'] = new_trailing_sl
                            logger.info(f"  TSL_TRAIL: {pos['id']} SL→Rs {pos['trailing_sl']:.2f} "
                                       f"(peak={peak:.2f} +{peak_gain_pct:.0f}%)")

                    # v7.7: Signal-based TSL ratchet — if signals STRONG, raise TSL aggressively
                    if (hold_score >= HOLD_SCORE_STRONG
                            and peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                            and pos.get('trailing_sl')):
                        ratchet_tsl = round(current_premium * 0.85, 2)
                        if ratchet_tsl > pos['trailing_sl']:
                            old_tsl = pos['trailing_sl']
                            pos['trailing_sl'] = ratchet_tsl
                            logger.info(f"  SIGNAL_HOLD_RATCHET: {pos['id']} hold_score={hold_score} "
                                       f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                       f"(85% of current Rs {current_premium:.2f})")

                # Check trailing SL hit (BUY: premium drops below trailing SL)
                if pos.get('trailing_sl') and current_premium <= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} premium {current_premium:.2f} <= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current_premium, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    try:
                        from trade_notifier import notify_trade_exit
                        lot_size = LOT_SIZES.get(symbol, 50)
                        cap = entry_prem * lot_size
                        pnl = pos.get('unrealized_pnl', 0)
                        notify_trade_exit(
                            market="EQUITY", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=entry_prem,
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pnl, capital_used=cap, exit_reason='TRAILING_SL_HIT',
                        )
                    except Exception:
                        pass
                    continue

            else:
                # SELL positions: premium going DOWN is profit
                entry_prem = pos['entry_premium']
                premium_gain_pct = ((entry_prem - current_premium) / entry_prem * 100
                                    if entry_prem > 0 else 0)

                # Update trough premium (lowest seen)
                if current_premium < pos.get('trough_premium', entry_prem):
                    pos['trough_premium'] = round(current_premium, 2)

                trough = pos.get('trough_premium', current_premium)
                trough_gain_pct = ((entry_prem - trough) / entry_prem * 100
                                   if entry_prem > 0 else 0)
                profit_from_entry = entry_prem - trough

                # v10.4: Phase 0 — Micro-gains for SELL (5%+ premium drop, held 5+ min)
                if (trough_gain_pct >= TSL_MICRO_GAIN_PCT
                        and elapsed_secs >= TSL_MICRO_MIN_HOLD_SECONDS
                        and not pos.get('breakeven_locked')):
                    # SELL: trailing SL = entry - 40% of peak drop (cap at entry + 2%)
                    micro_sl = round(entry_prem - (profit_from_entry * (1 - TSL_MICRO_TRAIL_DISTANCE_PCT / 100)), 2)
                    micro_sl = min(micro_sl, round(entry_prem * 1.02, 2))  # Cap: entry + 2%
                    if pos.get('trailing_sl') is None or micro_sl < pos['trailing_sl']:
                        pos['trailing_sl'] = micro_sl
                        logger.info(f"  TSL_MICRO: {pos['id']} SELL trough={trough_gain_pct:.1f}% → SL Rs {micro_sl:.2f} "
                                   f"(keeping {100-TSL_MICRO_TRAIL_DISTANCE_PCT}% of peak drop)")

                # Phase 1: Lock breakeven when premium drops 15%+ from entry (SELL profit)
                if trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(entry_prem * 0.97, 2)  # entry - 3% buffer
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} SELL gained {trough_gain_pct:.0f}% → locked SL at Rs {pos['trailing_sl']:.2f}")

                # v10.3: Regime-aware TSL for SELL
                regime_sell = self.regime_detector.get_regime(symbol)
                rp_sell = get_regime_params(regime_sell)

                # Phase 3: Tight trail when premium dropped 40%+ (regime-aware distance)
                if trough_gain_pct >= TSL_TIGHT_GAIN_PCT:
                    new_trailing_sl = round(trough + (profit_from_entry * rp_sell['tsl_tight_distance_pct'] / 100), 2)
                    if pos.get('trailing_sl') is None or new_trailing_sl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TIGHT: {pos['id']} SELL SL→Rs {pos['trailing_sl']:.2f} "
                                   f"(trough={trough:.2f} -{trough_gain_pct:.0f}%, phase3)")
                # Phase 2: Trail when premium dropped 25%+ (regime-aware distance)
                elif trough_gain_pct >= TSL_TRAIL_GAIN_PCT:
                    new_trailing_sl = round(trough + (profit_from_entry * rp_sell['tsl_trail_distance_pct'] / 100), 2)
                    if pos.get('trailing_sl') is None or new_trailing_sl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_trailing_sl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SELL SL→Rs {pos['trailing_sl']:.2f} "
                                   f"(trough={trough:.2f} -{trough_gain_pct:.0f}%)")

                # v7.7: Signal-based TSL ratchet for SELL — if signals STRONG, tighten TSL
                if (hold_score >= HOLD_SCORE_STRONG
                        and trough_gain_pct >= TSL_BREAKEVEN_GAIN_PCT
                        and pos.get('trailing_sl')):
                    ratchet_tsl = round(current_premium * 1.15, 2)  # SELL: 15% above current (tighter)
                    if ratchet_tsl < pos['trailing_sl']:
                        old_tsl = pos['trailing_sl']
                        pos['trailing_sl'] = ratchet_tsl
                        logger.info(f"  SIGNAL_HOLD_RATCHET: {pos['id']} SELL hold_score={hold_score} "
                                   f"→ TSL Rs {old_tsl:.2f}→{ratchet_tsl:.2f} "
                                   f"(115% of current Rs {current_premium:.2f})")

                # Check trailing SL hit (SELL: premium rises above trailing SL)
                if pos.get('trailing_sl') and current_premium >= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} SELL premium {current_premium:.2f} >= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current_premium, 'TRAILING_SL_HIT')
                    self._track_exit(pos, 'TRAILING_SL_HIT')
                    try:
                        from trade_notifier import notify_trade_exit
                        lot_size = LOT_SIZES.get(symbol, 50)
                        cap = MARGIN_PER_LOT.get(symbol, 120000)
                        pnl = pos.get('unrealized_pnl', 0)
                        notify_trade_exit(
                            market="EQUITY", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=pos['entry_premium'],
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pnl, capital_used=cap, exit_reason='TRAILING_SL_HIT',
                        )
                    except Exception:
                        pass
                    continue

            # ---- OI+IV DYNAMIC EXIT CHECK (runs BEFORE static exit) ----
            # Compute current IV from implied vol back-solve (same method as entry)
            current_iv_pct = None
            if current_premium > 0 and spot > 0:
                opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
                T_iv = max(pos.get('dte', 1), 1) / 365
                iv_solved = implied_vol(current_premium, spot, pos['strike'], T_iv, RISK_FREE_RATE, opt_type)
                if iv_solved:
                    current_iv_pct = round(iv_solved * 100, 1)
            oi_iv_reason, should_reverse = self.check_oi_iv_exit(pos, current_premium, current_iv_pct)
            if oi_iv_reason:
                # v7.7: Override if hold score STRONG + profitable
                if hold_score >= HOLD_SCORE_STRONG and pos.get('unrealized_pnl', 0) > 0:
                    logger.info(f"  SIGNAL_HOLD_OVERRIDE: {pos['id']} hold_score={hold_score} ({hold_label}) "
                               f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} "
                               f"— overriding {oi_iv_reason} (signals strong + profitable)")
                    oi_iv_reason = None  # Cancel OI/IV signal

            if oi_iv_reason:
                # v8.0: OI/IV TIGHTEN SL instead of forcing exit. Only GAMMA_SHIELD forces exit.
                if 'SL_TIGHTEN' in oi_iv_reason:
                    # Tighten trailing SL — combo tightens 10%, single tightens 5%
                    tighten_pct = 0.10 if 'OI_IV_SL_TIGHTEN' == oi_iv_reason else 0.05
                    if pos['is_sell']:
                        # SELL: TSL is ABOVE current (premium rising = loss). Tighten by pulling it closer.
                        new_tsl = round(current_premium * (1 + tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL "
                                       f"Rs {old_tsl}→{new_tsl:.2f} "
                                       f"({tighten_pct*100:.0f}% above current {current_premium:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} SELL TSL already tighter "
                                       f"Rs {pos['trailing_sl']:.2f} (proposed {new_tsl:.2f}). No change.")
                    else:
                        # BUY: TSL is BELOW current (premium falling = loss). Tighten by raising it.
                        new_tsl = round(current_premium * (1 - tighten_pct), 2)
                        if pos.get('trailing_sl') is None or new_tsl > (pos.get('trailing_sl') or 0):
                            old_tsl = pos.get('trailing_sl', 'None')
                            pos['trailing_sl'] = new_tsl
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL "
                                       f"Rs {old_tsl}→{new_tsl:.2f} "
                                       f"({tighten_pct*100:.0f}% below current {current_premium:.2f})")
                        else:
                            logger.info(f"  {oi_iv_reason}: {pos['id']} BUY TSL already tighter "
                                       f"Rs {pos.get('trailing_sl'):.2f} (proposed {new_tsl:.2f}). No change.")
                    # v8.0: Don't close, don't reverse — let TSL handle exit naturally
                else:
                    # GAMMA_SHIELD_EXIT — forced exit (real gamma risk, keep existing behavior)
                    lot_size = pos.get('lot_size', LOT_SIZES.get(symbol, 50))
                    self.portfolio.close_position(pos['id'], current_premium, oi_iv_reason)
                    self._track_exit(pos, oi_iv_reason)
                    try:
                        from trade_notifier import notify_trade_exit
                        if pos.get('is_sell', False):
                            capital = MARGIN_PER_LOT.get(symbol, 120000)
                        else:
                            capital = pos['entry_premium'] * lot_size
                        pnl = pos.get('unrealized_pnl', 0)
                        locked = sum(
                            MARGIN_PER_LOT.get(p['symbol'], 120000) if p.get('is_sell', False)
                            else p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                            for p in self.portfolio.positions
                        )
                        notify_trade_exit(
                            market="EQUITY", strategy=pos['strategy'],
                            symbol=symbol, signal_type=pos['signal_type'],
                            strike=pos['strike'], entry_price=pos['entry_premium'],
                            exit_price=current_premium, entry_time=pos['timestamp'],
                            pnl=pnl, capital_used=capital,
                            exit_reason=oi_iv_reason,
                            capital_available=EQUITY_CAPITAL - locked,
                            total_invested=locked,
                        )
                    except Exception as e:
                        logger.warning(f"  Telegram exit notify failed: {e}")
                    continue  # Skip static exit check for this position

            # ---- STATIC EXIT CHECK (original logic) ----
            # details/target/sl already computed above in TSL section
            logger.info(f"  EXIT_CHECK: {pos['id']} | Entry: {pos['entry_premium']:.2f} → Current: {current_premium:.2f} [{premium_source}] | "
                       f"Target: {target:.2f} SL: {sl:.2f} | TSL: {pos.get('trailing_sl', 'N/A')} | "
                       f"Spot: {entry_spot:.0f}→{spot:.0f} Δ={spot_change:+.0f} | "
                       f"Hold: {hold_score}/{hold_label}")

            exit_reason = None

            # ---- v13.8: MOMENTUM EXIT — spot reverses against direction ----
            if MOMENTUM_EXIT_ENABLED and exit_reason is None:
                _me_entry_spot = pos.get('details', {}).get('spot', 0) if isinstance(pos.get('details'), dict) else 0
                _me_spot_now = spot  # Already computed above in exit loop
                _me_entry_ts = pos.get('timestamp', '')
                if _me_entry_spot > 0 and _me_spot_now > 0 and _me_entry_ts:
                    try:
                        _me_entry_dt = datetime.fromisoformat(_me_entry_ts)
                        _me_hold_min = (datetime.now() - _me_entry_dt).total_seconds() / 60
                        if _me_hold_min >= MOMENTUM_EXIT_MIN_MINUTES:
                            _me_change = (_me_spot_now - _me_entry_spot) / _me_entry_spot * 100
                            _me_is_ce = 'CE' in pos.get('signal_type', '')
                            _me_against = (_me_is_ce and _me_change < -MOMENTUM_EXIT_SPOT_PCT) or                                           (not _me_is_ce and _me_change > MOMENTUM_EXIT_SPOT_PCT)
                            if _me_against and pos.get('unrealized_pnl', 0) <= 0:
                                exit_reason = 'MOMENTUM_EXIT'
                                logger.info(f"  MOMENTUM_EXIT: {pos['id']} spot {_me_entry_spot:.0f}->{_me_spot_now:.0f} "
                                           f"({_me_change:+.2f}%) after {_me_hold_min:.0f}min")
                    except Exception:
                        pass


            if not pos['is_sell']:
                # BUY positions
                if current_premium <= sl:
                    exit_reason = 'SL_HIT'
                elif pos['dte'] <= 0:
                    exit_reason = 'EXPIRY'
                elif current_premium >= target:
                    # v10.1: Trailing target — extend instead of hard exit
                    if TARGET_TRAIL_ENABLED and pos.get('target_extensions', 0) < TARGET_TRAIL_MAX_EXTENSIONS:
                        gain_pct = (current_premium - pos['entry_premium']) / pos['entry_premium'] * 100 if pos['entry_premium'] > 0 else 0
                        new_target = round(current_premium * (1 + TARGET_TRAIL_EXTEND_PCT / 100), 2)
                        new_tsl = round(pos.get('peak_premium', current_premium) * (1 - TARGET_TRAIL_TSL_DISTANCE_PCT / 100), 2)
                        new_tsl = max(new_tsl, pos.get('trailing_sl') or 0)
                        pos['target_extensions'] = pos.get('target_extensions', 0) + 1
                        pos['target'] = new_target
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TARGET_TRAIL: {pos['id']} BUY ext#{pos['target_extensions']} "
                                   f"gain={gain_pct:.0f}% -> target Rs {new_target:.2f}, TSL Rs {new_tsl:.2f}")
                    else:
                        exit_reason = 'TARGET_HIT'
            else:
                # SELL positions
                if current_premium >= sl:
                    exit_reason = 'SL_HIT'
                elif pos['dte'] <= 0:
                    exit_reason = 'EXPIRY'
                elif current_premium <= target:
                    # v10.1: Trailing target for SELL
                    if TARGET_TRAIL_ENABLED and pos.get('target_extensions', 0) < TARGET_TRAIL_MAX_EXTENSIONS:
                        gain_pct = (pos['entry_premium'] - current_premium) / pos['entry_premium'] * 100 if pos['entry_premium'] > 0 else 0
                        new_target = round(current_premium * (1 - TARGET_TRAIL_EXTEND_PCT / 100), 2)
                        new_tsl = round(pos.get('trough_premium', current_premium) * (1 + TARGET_TRAIL_TSL_DISTANCE_PCT / 100), 2)
                        new_tsl = min(new_tsl, pos.get('trailing_sl') or float('inf'))
                        pos['target_extensions'] = pos.get('target_extensions', 0) + 1
                        pos['target'] = new_target
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TARGET_TRAIL: {pos['id']} SELL ext#{pos['target_extensions']} "
                                   f"gain={gain_pct:.0f}% -> target Rs {new_target:.2f}, TSL Rs {new_tsl:.2f}")
                    else:
                        exit_reason = 'TARGET_HIT'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current_premium, exit_reason)
                self._track_exit(pos, exit_reason)
                try:
                    from trade_notifier import notify_trade_exit
                    lot_size = LOT_SIZES.get(symbol, 50)
                    if pos.get('is_sell', False):
                        capital = MARGIN_PER_LOT.get(symbol, 120000)
                    else:
                        capital = pos['entry_premium'] * lot_size
                    pnl = pos.get('unrealized_pnl', 0)
                    locked = 0
                    for p in self.portfolio.positions:
                        if p.get('is_sell', False):
                            locked += MARGIN_PER_LOT.get(p['symbol'], 120000)
                        else:
                            locked += p['entry_premium'] * LOT_SIZES.get(p['symbol'], 50)
                    notify_trade_exit(
                        market="EQUITY", strategy=pos['strategy'],
                        symbol=symbol, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current_premium, entry_time=pos['timestamp'],
                        pnl=pnl, capital_used=capital,
                        exit_reason=exit_reason,
                        capital_available=EQUITY_CAPITAL - locked,
                        total_invested=locked,
                    )
                except Exception as e:
                    logger.warning(f"  Telegram exit notify failed: {e}")

                # Smart reversal on SL hit (with momentum confirmation)
                if exit_reason == 'SL_HIT' and self._check_sl_reversal_confirmation(pos, spot):
                    logger.info(f"  SL_REVERSAL: {pos['id']} SL hit with momentum confirmation → reversing")
                    self.execute_reversal(pos, 'SL_HIT_REVERSE', spot=spot)

        # Persist updated PnL/Greeks to disk after exit check cycle
        self.portfolio.save_state()

    def get_eod_summary(self):
        """Return end-of-day summary with actual PnL (our trades) + dummy PnL (all signals)."""
        # Actual PnL: from portfolio closed_trades + open positions unrealized PnL
        actual_closed_pnl = sum(t.get('pnl', 0) for t in self.portfolio.closed_trades)
        actual_open_pnl = sum(p.get('unrealized_pnl', 0) for p in self.portfolio.positions)
        actual_total = actual_closed_pnl + actual_open_pnl

        # Dummy PnL: estimate PnL for ALL signals as if unlimited capital
        dummy_pnl = 0
        for sig in self.daily_signals_all:
            spot_now = self.get_index_spot(sig['symbol']) or sig.get('spot', 0)
            entry_spot = sig.get('spot', spot_now)
            if not entry_spot or not spot_now:
                continue
            spot_change = spot_now - entry_spot
            delta = sig.get('greeks', {}).get('delta', 0.5)
            premium_change = delta * spot_change
            lot_size = LOT_SIZES.get(sig['symbol'], 50)
            dummy_pnl += premium_change * lot_size

        # Capital used in open positions
        capital_used = 0
        for p in self.portfolio.positions:
            if p.get('is_sell', False):
                capital_used += MARGIN_PER_LOT.get(p['symbol'], 120000)
            else:
                capital_used += p['entry_premium'] * p['lot_size']

        return {
            'total_signals': self.daily_signal_count,
            'trades_taken': len(self.portfolio.closed_trades) + len(self.portfolio.positions),
            'open_positions': len(self.portfolio.positions),
            'closed_trades': len(self.portfolio.closed_trades),
            'actual_pnl': round(actual_total, 2),
            'dummy_pnl': round(dummy_pnl, 2),
            'capital_used': round(capital_used, 2),
        }

    def run_once(self):
        """Run one scan cycle (every 1 second in v15).
        v16: check_exits() runs FIRST and is protected — if scanning fails,
        open trades still get their trailing SL/target/stop loss checked.
        """
        # v16: ALWAYS check exits first — protects open trades even if scan fails
        try:
            self.check_exits()
        except Exception as exit_err:
            logger.error(f"  EXIT_CHECK_FAILED: {exit_err} — open trades unprotected this cycle!")

        # Scan for new signals (if this fails, exits above already ran)
        signals = self.scan_all_strategies()
        self.execute_paper_signals(signals)

        # v15: Full status only every 60s, signals log only when signals exist
        if (time.time() - self._last_full_log_time) < self._full_log_interval:
            pass  # skip verbose status on tick cycles
        else:
            self.portfolio.print_status()
        if signals:
            self.save_signals_log(signals)

    def save_signals_log(self, signals):
        """Save ALL signals to CSV with full data for future backtests.
        v7.2: Added volume, IV, OI, vega, quality_score, pcr, vwap, executed status.
        """
        if not signals:
            return
        today = datetime.now().strftime('%Y%m%d')
        log_file = os.path.join(PAPER_DIR, f'signals_{today}.csv')

        # Build list of executed signal IDs for status tracking
        executed_ids = set()
        for p in self.portfolio.positions:
            # Match by strategy+symbol+strike to mark executed
            key = f"{p.get('strategy', '')}_{p.get('symbol', '')}_{p.get('strike', 0)}"
            executed_ids.add(key)

        rows = []
        for sig in signals:
            greeks = sig.get('greeks', {})
            sig_key = f"{sig.get('strategy', '')}_{sig.get('symbol', '')}_{sig.get('strike', 0)}"

            # Fetch volume from intraday OHLC if available
            ohlc = self.get_intraday_ohlc(sig.get('symbol', ''))
            volume = ohlc.get('volume', 0) if ohlc else 0

            # Get IV from computed indicators
            indicators = self.engine.compute_indicators(
                sig.get('symbol', ''),
                ohlc or {'open': sig.get('spot', 0), 'high': sig.get('spot', 0),
                         'low': sig.get('spot', 0), 'close': sig.get('spot', 0), 'volume': 0}
            )
            iv = indicators.get('iv', 0) if indicators else 0
            pcr = indicators.get('pcr', 0) if indicators else 0
            vwap = indicators.get('vwap', 0) if indicators else 0

            rows.append({
                'timestamp': datetime.now().isoformat(),
                'strategy': sig.get('strategy', ''),
                'symbol': sig.get('symbol', ''),
                'type': sig['type'],
                'strike': sig['strike'],
                'premium': sig['premium'],
                'spot': sig.get('spot', 0),
                'dte': sig.get('dte', 0),
                # Greeks
                'delta': greeks.get('delta', 0),
                'gamma': greeks.get('gamma', 0),
                'theta': greeks.get('theta', 0),
                'vega': greeks.get('vega', 0),
                'iv': greeks.get('iv', iv),
                # Market data
                'volume': volume,
                'oi': sig.get('oi', 0),
                'pcr': pcr,
                'vwap': vwap,
                # Trade params
                'target': sig.get('target', 0),
                'sl': sig.get('sl', 0),
                'quality_score': sig.get('quality_score', 0),
                'reason': sig.get('reason', ''),
                # Execution status
                'executed': sig_key in executed_ids,
                    'gap_direction': self.gap_analysis.get('direction', 'UNKNOWN') if self.gap_analysis else 'NO_DATA',
                    'gap_pct': self.gap_analysis.get('gap_pct', 0) if self.gap_analysis else 0,
                    'gap_bias': self.gap_analysis.get('bias', 'NEUTRAL') if self.gap_analysis else 'NEUTRAL',
                    'ltp_source': sig.get('ltp_source', getattr(self.engine, '_last_ltp_source', getattr(self, '_last_ltp_source', 'UNKNOWN'))),
                    'signal_strength': compute_signal_strength(sig, sig.get('spot', 0),
                        self.engine.compute_indicators(sig.get('symbol', ''),
                            self.get_intraday_ohlc(sig.get('symbol', '')) or
                            {'open': sig.get('spot', 0), 'high': sig.get('spot', 0),
                             'low': sig.get('spot', 0), 'close': sig.get('spot', 0), 'volume': 0}
                        ) if hasattr(self, 'engine') else {},
                        self.current_vix if hasattr(self, 'current_vix') else None,
                        sig.get('ltp_source', getattr(self.engine, '_last_ltp_source', getattr(self, '_last_ltp_source', 'UNKNOWN')))
                    )[0],
                    'strength_label': compute_signal_strength(sig, sig.get('spot', 0),
                        self.engine.compute_indicators(sig.get('symbol', ''),
                            self.get_intraday_ohlc(sig.get('symbol', '')) or
                            {'open': sig.get('spot', 0), 'high': sig.get('spot', 0),
                             'low': sig.get('spot', 0), 'close': sig.get('spot', 0), 'volume': 0}
                        ) if hasattr(self, 'engine') else {},
                        self.current_vix if hasattr(self, 'current_vix') else None,
                        sig.get('ltp_source', getattr(self.engine, '_last_ltp_source', getattr(self, '_last_ltp_source', 'UNKNOWN')))
                    )[1],
                    # v16: 14 ML features
                    **compute_ml_features(sig,
                        ohlc or {},
                        sig.get('spot', 0),
                        indicators or {},
                        self.current_vix if hasattr(self, 'current_vix') else None),
            })
        df = pd.DataFrame(rows)
        if os.path.exists(log_file):
            existing = pd.read_csv(log_file)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_csv(log_file, index=False)
        logger.info(f"  Signals logged ({len(rows)} rows): {log_file}")

    def run_continuous(self, interval_seconds=1):
        """Run continuous scanning during market hours.
        v15: Reduced from 5 min (300s) to 1 second for tick-by-tick responsiveness.
        All heavy API calls are cache-protected (OHLC 60s, VIX 120s, PCR 60s).
        """
        self._running = True
        logger.info(f"\n{'='*70}")
        logger.info("PAPER TRADING SYSTEM STARTED (v15 — TICK-BY-TICK)")
        logger.info(f"Scanning every {interval_seconds} second(s) during market hours")
        logger.info(f"Capital: Rs {self.portfolio.capital:,.0f}")
        logger.info(f"Greeks refresh: every 10s | Option LTP cache: 5s | PCR cache: 60s")
        logger.info(f"Strategies: CPR, Gamma Blast, Ghost Zone, PCR+VWAP")
        logger.info(f"Indices: NIFTY, BANKNIFTY, SENSEX")
        logger.info(f"{'='*70}\n")

        def signal_handler(sig, frame):
            logger.info("\nShutting down paper trader...")
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
# v19.2: Start self-healing background scheduler        try:            self._healer = SelfHealingEngine()            self._healer.start()            logger.info("[SelfHealing] Auto-validation engine started (4h backtest, daily digest)")        except Exception as e:            logger.warning("[SelfHealing] Failed to start: %s" % e)

        while self._running:
            now = datetime.now().time()

            if PRE_MARKET <= now <= MARKET_CLOSE:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"  SCAN_ERROR: {e}", exc_info=True)
            elif now > MARKET_CLOSE:
                logger.info("Market closed. Final status:")
                self.portfolio.print_status()
                self._save_daily_report()
                break
            else:
                next_open = datetime.combine(datetime.now().date(),
                                            PRE_MARKET)
                wait = (next_open - datetime.now()).total_seconds()
                if wait > 0:
                    logger.info(f"Market not open yet. Waiting {wait/60:.0f} minutes...")
                    time.sleep(min(wait, 300))
                    continue

            time.sleep(interval_seconds)

    def _save_daily_report(self):
        """Save end-of-day report."""
        today = datetime.now().strftime('%Y%m%d')
        report = {
            'date': today,
            'summary': self.portfolio.get_summary(),
            'positions': self.portfolio.positions,
            'trades_today': [t for t in self.portfolio.closed_trades
                           if t.get('exit_time', '').startswith(datetime.now().strftime('%Y-%m-%d'))],
        }
        report_file = os.path.join(PAPER_DIR, f'daily_report_{today}.json')
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Daily report saved: {report_file}")

    def _write_live_scan_data(self, scan_data, vix=None):
        """v10: Write live indicator data for dashboard consumption.

        Called every scan cycle. Dashboard reads this file to show
        live OI, PCR, IV, CPR levels next to each position.
        """
        try:
            live_file = os.path.join(PAPER_DIR, 'live_scan_data.json')
            payload = {
                'last_updated': datetime.now().isoformat(),
                'vix': vix,
                'market': 'equity',
                'symbols': scan_data,
            }
            with open(live_file, 'w') as f:
                json.dump(payload, f, indent=2, default=str)
            logger.debug(f"Live scan data written: {len(scan_data)} symbols")
        except Exception as e:
            logger.error(f"Error writing live scan data: {e}")


# ====================================================================
# CLI INTERFACE
# ====================================================================
def main():
    print("=" * 70)
    print("  ALGO TRADING - PAPER TRADING SYSTEM")
    print("  5 Strategies x 3 Indices | Angel One SmartAPI")
    print("=" * 70)

    import argparse
    parser = argparse.ArgumentParser(description='Paper Trading System')
    parser.add_argument('--status', action='store_true', help='Show current status')
    parser.add_argument('--once', action='store_true', help='Run single scan (no loop)')
    parser.add_argument('--interval', type=int, default=1, help='Scan interval in seconds (v15: default 1s, was 5 min)')
    parser.add_argument('--offline', action='store_true', help='Run offline (no API, use historical)')
    parser.add_argument('--reset', action='store_true', help='Reset paper portfolio')
    args = parser.parse_args()

    if args.reset:
        portfolio = PaperPortfolio()
        portfolio.capital = INITIAL_CAPITAL
        portfolio.positions = []
        portfolio.closed_trades = []
        portfolio.daily_pnl = {}
        portfolio.save_state()
        print("Portfolio reset to Rs 3,00,000")
        return

    if args.status:
        portfolio = PaperPortfolio()
        portfolio.print_status()
        return

    trader = PaperTrader()

    # Load historical data for all indices
    for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        trader.engine.load_historical(symbol)

    if args.offline:
        # Offline mode: use last available data, no API
        logger.info("Running in OFFLINE mode (no Angel API)...")
        trader.run_once()
        return

    # Connect to Angel API
    if trader.connect():
        if args.once:
            trader.run_once()
        else:
            trader.run_continuous(interval_seconds=args.interval)
    else:
        logger.warning("Angel API connection failed. Running in offline mode...")
        trader.run_once()


if __name__ == '__main__':
    main()
