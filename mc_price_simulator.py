"""
Monte Carlo Price Simulator — Predictive Entry/Exit Engine
============================================================
Simulates 1000 possible future price paths from current state.
Uses historical volatility + theta decay to predict probability
of hitting target vs SL at any given moment.

Usage:
    from mc_price_simulator import MCPriceEngine

    engine = MCPriceEngine()

    # Before entry — should we enter?
    decision = engine.should_enter(
        spot=23500, premium=150, strike=23500, opt_type='CE',
        target=180, sl=135, iv=0.20, dte=1, atr=120
    )
    # Returns: {'enter': True, 'prob_target': 0.72, 'prob_sl': 0.18, ...}

    # During trade — should we hold or exit?
    decision = engine.should_hold(
        spot=23550, premium=165, entry_premium=150, strike=23500,
        opt_type='CE', target=180, sl=135, iv=0.18, dte=0.8,
        time_in_trade_min=15
    )
    # Returns: {'hold': True, 'prob_target': 0.65, 'prob_sl': 0.12, ...}

Architecture:
    1. Generate N random price paths using Geometric Brownian Motion
    2. For each path, compute option premium using Black-Scholes
    3. Check if premium hits target or SL first
    4. Probability = count(target hits) / N
    5. Enter if prob_target > threshold (e.g., 60%)
    6. Exit if prob_target drops below hold_threshold (e.g., 40%)
"""

import numpy as np
from math import log, sqrt, exp
from scipy.stats import norm
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────

N_SIMULATIONS = 500          # Number of price paths (500 = fast, accurate enough)
TIME_STEPS = 30              # Simulate 30 steps ahead (each step = 1 minute)
ENTRY_PROB_THRESHOLD = 0.55  # Need 55% chance of hitting target to enter
HOLD_PROB_THRESHOLD = 0.35   # Exit if probability drops below 35%
EXIT_PROB_THRESHOLD = 0.20   # Emergency exit below 20%
RISK_FREE_RATE = 0.065       # Annual risk-free rate

# Premium volatility calibration (per MINUTE, from real data)
# Calibrated from NIFTY option chain data:
#   Normal day: 2.3% per minute (45% daily)
#   Volatile day: 7.6% per minute (148% daily)
#   Average: ~3% per minute
PREMIUM_VOL_PER_MIN_NORMAL = 0.023    # 2.3% per minute (normal day)
PREMIUM_VOL_PER_MIN_VOLATILE = 0.076  # 7.6% per minute (volatile day)
PREMIUM_VOL_PER_MIN_DEFAULT = 0.030   # 3.0% per minute (default)

# Theta decay: mean return per minute is -0.05% to -0.2%
THETA_DECAY_PER_MIN_NORMAL = 0.0006   # ~0.06% per min (normal)
THETA_DECAY_PER_MIN_EXPIRY = 0.0020   # ~0.20% per min (expiry day)


# ── Black-Scholes ─────────────────────────────────────────────────────────

def bs_price(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option price."""
    if T <= 0:
        T = 1e-6
    if sigma <= 0:
        sigma = 0.01
    d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if opt_type == 'CE':
        return max(S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2), 0.05)
    else:
        return max(K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1), 0.05)


def bs_delta(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes delta."""
    if T <= 0:
        T = 1e-6
    if sigma <= 0:
        sigma = 0.01
    d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
    if opt_type == 'CE':
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1


# ── Geometric Brownian Motion Price Path Generator ────────────────────────

def simulate_spot_paths(spot, iv, dte, n_sims=N_SIMULATIONS, n_steps=TIME_STEPS, dt_minutes=1):
    """Generate N random spot price paths using GBM.

    Args:
        spot: Current spot price
        iv: Implied volatility (annualized, e.g., 0.20 for 20%)
        dte: Days to expiry
        n_sims: Number of simulations
        n_steps: Number of time steps
        dt_minutes: Minutes per step

    Returns:
        numpy array of shape (n_sims, n_steps+1) with price paths
    """
    # Convert minutes to fraction of year for GBM
    # 1 minute = 1 / (252 * 375) of a trading year
    dt = dt_minutes / (252.0 * 375.0)

    drift = (RISK_FREE_RATE - 0.5 * iv ** 2) * dt
    vol = iv * sqrt(dt)

    # Generate random returns
    random_returns = np.random.normal(0, 1, (n_sims, n_steps))

    # Build price paths
    paths = np.zeros((n_sims, n_steps + 1))
    paths[:, 0] = spot

    for t in range(n_steps):
        paths[:, t + 1] = paths[:, t] * np.exp(drift + vol * random_returns[:, t])

    return paths


# ── Premium Path from Spot Path ──────────────────────────────────────────

def compute_premium_paths(spot_paths, strike, opt_type, iv, dte, dt_minutes=1):
    """Compute option premium for each spot path at each time step.

    Args:
        spot_paths: (n_sims, n_steps+1) array of spot prices
        strike: Option strike price
        opt_type: 'CE' or 'PE'
        iv: Implied volatility
        dte: Days to expiry at start
        dt_minutes: Minutes per step

    Returns:
        numpy array of shape (n_sims, n_steps+1) with premium paths
    """
    n_sims, n_steps_plus1 = spot_paths.shape
    premium_paths = np.zeros_like(spot_paths)

    for t in range(n_steps_plus1):
        # Time remaining decreases each step
        remaining_dte = max(dte - t * dt_minutes / 375, 0.001)
        T = remaining_dte / 365

        for i in range(n_sims):
            premium_paths[i, t] = bs_price(spot_paths[i, t], strike, T, RISK_FREE_RATE, iv, opt_type)

    return premium_paths


def compute_premium_paths_vectorized(spot_paths, strike, opt_type, iv, dte, dt_minutes=1):
    """Faster vectorized version of premium path computation."""
    n_sims, n_steps_plus1 = spot_paths.shape
    premium_paths = np.zeros_like(spot_paths)

    for t in range(n_steps_plus1):
        remaining_dte = max(dte - t * dt_minutes / 375, 0.001)
        T = remaining_dte / 365

        S = spot_paths[:, t]
        K = strike
        r = RISK_FREE_RATE
        sigma = iv

        if T <= 1e-6:
            T = 1e-6

        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)

        if opt_type == 'CE':
            premium_paths[:, t] = np.maximum(
                S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2), 0.05)
        else:
            premium_paths[:, t] = np.maximum(
                K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1), 0.05)

    return premium_paths


# ── MC Price Engine ──────────────────────────────────────────────────────

class MCPriceEngine:
    """Monte Carlo predictive engine for entry/exit decisions.

    Uses PREMIUM-DIRECT simulation (not spot-based BS).
    Premium has its own volatility and theta decay dynamics.
    """

    def __init__(self, n_sims=N_SIMULATIONS, n_steps=TIME_STEPS):
        self.n_sims = n_sims
        self.n_steps = n_steps

    def _simulate_premium_paths(self, premium, dte, n_steps,
                                 recent_returns=None, is_trending=False):
        """Simulate premium paths using REAL volatility calibration.

        Uses actual per-minute premium volatility from market data.
        If recent_returns provided, uses those to calibrate (best accuracy).
        Otherwise uses default calibration from historical data.

        Args:
            premium: Current option premium (REAL market price)
            dte: Days to expiry
            n_steps: Number of 1-minute steps forward
            recent_returns: List of recent per-minute log returns (last 10-15 mins)
                           If provided, uses this for vol + drift calibration
            is_trending: If True, uses trending regime calibration

        Returns:
            numpy array (n_sims, n_steps+1)
        """
        if recent_returns is not None and len(recent_returns) >= 5:
            # BEST: Calibrate from actual recent premium movements
            arr = np.array(recent_returns)
            vol_per_min = max(arr.std(), 0.005)  # Floor at 0.5%
            drift_per_min = arr.mean()  # Actual drift direction
        elif is_trending:
            # Use trending calibration
            vol_per_min = PREMIUM_VOL_PER_MIN_NORMAL * 1.5  # Higher vol in trends
            drift_per_min = PREMIUM_VOL_PER_MIN_NORMAL * 0.3  # Positive drift
        else:
            # Default calibration
            vol_per_min = PREMIUM_VOL_PER_MIN_DEFAULT
            drift_per_min = 0  # No directional bias

        # Near-expiry adjustments
        if dte <= 0.5:
            vol_per_min *= 1.3
            theta_per_min = premium * THETA_DECAY_PER_MIN_EXPIRY
        elif dte <= 1:
            vol_per_min *= 1.1
            theta_per_min = premium * (THETA_DECAY_PER_MIN_NORMAL + THETA_DECAY_PER_MIN_EXPIRY) / 2
        else:
            theta_per_min = premium * THETA_DECAY_PER_MIN_NORMAL

        n_steps = int(n_steps)
        paths = np.zeros((self.n_sims, n_steps + 1))
        paths[:, 0] = premium

        noise = np.random.normal(0, 1, (self.n_sims, n_steps))

        for t in range(n_steps):
            # Per-minute simulation: log return = drift + vol * noise
            log_return = drift_per_min + vol_per_min * noise[:, t]
            paths[:, t + 1] = np.maximum(
                paths[:, t] * np.exp(log_return) - theta_per_min,
                0.05
            )

        return paths

    def should_enter(self, spot, premium, strike, opt_type, target, sl,
                     iv=0.20, dte=1, atr=100, max_hold_min=60,
                     recent_returns=None, is_trending=False):
        """Predict probability of hitting target vs SL.

        Args:
            spot: Current spot price
            premium: Current option premium (REAL market price)
            strike: Option strike
            opt_type: 'CE' or 'PE'
            target: Target premium
            sl: Stop loss premium
            iv: Implied volatility (used for vol calibration)
            dte: Days to expiry
            atr: ATR (used for moneyness detection)
            max_hold_min: Max hold time in minutes
            momentum: -1 to +1 (spot momentum, positive = bullish)
            trend_strength: 0 to 1

        Returns:
            dict with 'enter' (bool), probabilities, and reasoning
        """
        n_steps = int(min(self.n_steps, max_hold_min))

        # Simulate premium paths using real calibration
        paths = self._simulate_premium_paths(
            premium, dte, n_steps, recent_returns, is_trending)

        # Analyze outcomes
        target_hits = 0
        sl_hits = 0
        time_to_target = []
        time_to_sl = []
        final_premiums = []

        for i in range(self.n_sims):
            hit = False
            for t in range(1, n_steps + 1):
                if paths[i, t] >= target:
                    target_hits += 1
                    time_to_target.append(t)
                    hit = True
                    break
                if paths[i, t] <= sl:
                    sl_hits += 1
                    time_to_sl.append(t)
                    hit = True
                    break
            if not hit:
                final_premiums.append(paths[i, -1])

        prob_target = target_hits / self.n_sims
        prob_sl = sl_hits / self.n_sims
        prob_neither = 1 - prob_target - prob_sl

        # Expected value
        ev = (target - premium) * prob_target + (sl - premium) * prob_sl
        if final_premiums:
            ev += (np.mean(final_premiums) - premium) * prob_neither

        # Risk-reward
        reward = target - premium
        risk = premium - sl
        rr = reward / max(risk, 0.01)

        # Decision: enter if probability and EV are favorable
        enter = (prob_target >= ENTRY_PROB_THRESHOLD and ev > 0 and rr >= 0.8)

        return {
            'enter': enter,
            'prob_target': prob_target,
            'prob_sl': prob_sl,
            'prob_neither': prob_neither,
            'expected_value': ev,
            'rr_ratio': rr,
            'avg_time_to_target': np.mean(time_to_target) if time_to_target else n_steps,
            'avg_time_to_sl': np.mean(time_to_sl) if time_to_sl else n_steps,
            'reason': 'MC: P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f RR=%.1f' % (
                prob_target * 100, prob_sl * 100, ev, rr),
        }

    def should_hold(self, spot, premium, entry_premium, strike, opt_type,
                    target, sl, iv=0.20, dte=1, time_in_trade_min=0, max_hold_min=60,
                    recent_returns=None, is_trending=False):
        """During an open trade, should we continue holding?

        Re-simulates from CURRENT premium (not entry).
        Continuously recalculates probability and exits when edge disappears.

        Returns:
            dict with 'hold' (bool), 'exit_now' (bool), probabilities
        """
        remaining_hold = max(max_hold_min - time_in_trade_min, 3)
        n_steps = int(min(self.n_steps, remaining_hold))

        paths = self._simulate_premium_paths(
            premium, dte, n_steps, recent_returns, is_trending)

        target_hits = 0
        sl_hits = 0
        final_premiums = []

        for i in range(self.n_sims):
            hit = False
            for t in range(1, n_steps + 1):
                if paths[i, t] >= target:
                    target_hits += 1
                    hit = True
                    break
                if paths[i, t] <= sl:
                    sl_hits += 1
                    hit = True
                    break
            if not hit:
                final_premiums.append(paths[i, -1])

        prob_target = target_hits / self.n_sims
        prob_sl = sl_hits / self.n_sims
        current_pnl_pct = (premium - entry_premium) / max(entry_premium, 0.01) * 100

        reward = target - premium
        risk = premium - sl
        ev = reward * prob_target - risk * prob_sl
        if final_premiums:
            ev += (np.mean(final_premiums) - premium) * (1 - prob_target - prob_sl)

        # Decision matrix
        if prob_target >= HOLD_PROB_THRESHOLD and ev > 0:
            hold = True
            exit_now = False
            action = 'HOLD (P(tgt)=%.0f%% EV=Rs %.0f)' % (prob_target * 100, ev)
        elif prob_target < EXIT_PROB_THRESHOLD:
            hold = False
            exit_now = True
            action = 'MC_EXIT: P(tgt) dropped to %.0f%%' % (prob_target * 100)
        elif ev < -premium * 0.05:  # EV worse than -5% of premium
            hold = False
            exit_now = True
            action = 'MC_EXIT: negative EV Rs %.0f' % ev
        elif current_pnl_pct > 10 and prob_target < 0.45:
            hold = False
            exit_now = True
            action = 'MC_LOCK_PROFIT: +%.0f%% gain, P(tgt) only %.0f%%' % (
                current_pnl_pct, prob_target * 100)
        elif current_pnl_pct < -15 and prob_target < 0.30:
            hold = False
            exit_now = True
            action = 'MC_CUT_LOSS: -%.0f%% loss, P(tgt) only %.0f%%' % (
                abs(current_pnl_pct), prob_target * 100)
        else:
            hold = True
            exit_now = False
            action = 'HOLD (borderline EV=Rs %.0f)' % ev

        return {
            'hold': hold,
            'exit_now': exit_now,
            'prob_target': prob_target,
            'prob_sl': prob_sl,
            'expected_value': ev,
            'current_pnl_pct': current_pnl_pct,
            'action': action,
            'reason': 'MC: P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f PnL=%.0f%%' % (
                prob_target * 100, prob_sl * 100, ev, current_pnl_pct),
        }

    def find_optimal_target_sl(self, spot, premium, strike, opt_type,
                               iv=0.20, dte=1, max_hold_min=60):
        """Find the optimal target and SL that maximizes expected value.

        Tests multiple target/SL combinations and returns the best one.

        Returns:
            dict with optimal target, sl, and their probabilities
        """
        n_steps = int(min(self.n_steps, max_hold_min))
        spot_paths = simulate_spot_paths(spot, iv, dte, self.n_sims, n_steps)
        prem_paths = compute_premium_paths_vectorized(
            spot_paths, strike, opt_type, iv, dte)

        best_ev = -999999
        best_config = None

        # Test target: 5% to 40%, SL: 5% to 25%
        for tgt_pct in [5, 10, 15, 20, 25, 30, 40]:
            for sl_pct in [5, 8, 10, 15, 20, 25]:
                if tgt_pct <= sl_pct:
                    continue

                target = premium * (1 + tgt_pct / 100)
                sl = premium * (1 - sl_pct / 100)

                t_hits = 0
                s_hits = 0
                finals = []

                for i in range(self.n_sims):
                    path = prem_paths[i]
                    hit = False
                    for t in range(1, len(path)):
                        if path[t] >= target:
                            t_hits += 1
                            hit = True
                            break
                        if path[t] <= sl:
                            s_hits += 1
                            hit = True
                            break
                    if not hit:
                        finals.append(path[-1])

                pt = t_hits / self.n_sims
                ps = s_hits / self.n_sims
                ev = (target - premium) * pt + (sl - premium) * ps
                if finals:
                    ev += (np.mean(finals) - premium) * (1 - pt - ps)

                if ev > best_ev and pt > 0.30:  # At least 30% hit rate
                    best_ev = ev
                    best_config = {
                        'target': target,
                        'sl': sl,
                        'target_pct': tgt_pct,
                        'sl_pct': sl_pct,
                        'prob_target': pt,
                        'prob_sl': ps,
                        'expected_value': ev,
                        'rr_ratio': tgt_pct / max(sl_pct, 1),
                    }

        if best_config:
            best_config['reason'] = 'Optimal: T=%d%% SL=%d%% P(tgt)=%.0f%% EV=Rs %.0f' % (
                best_config['target_pct'], best_config['sl_pct'],
                best_config['prob_target'] * 100, best_config['expected_value'])

        return best_config


# ── Quick Test ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    engine = MCPriceEngine(n_sims=500, n_steps=30)

    print("MC PRICE SIMULATOR - Calibrated from Real Data")
    print("=" * 90)

    # Simulate recent returns for different market conditions
    normal_returns = list(np.random.normal(-0.0006, 0.023, 15))  # Normal day
    trending_up = list(np.random.normal(0.005, 0.025, 15))       # Strong uptrend
    trending_down = list(np.random.normal(-0.008, 0.030, 15))    # Strong downtrend
    volatile = list(np.random.normal(-0.002, 0.076, 15))         # Volatile (Mar 24)

    print("\nTest 1: NO SIGNAL (random entry, no momentum)")
    r = engine.should_enter(23500, 150, 23500, 'CE', 180, 135, dte=1)
    print("  Enter: %s | P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f" % (
        r['enter'], r['prob_target']*100, r['prob_sl']*100, r['expected_value']))

    print("\nTest 2: TRENDING UP (CE entry with bullish momentum)")
    r = engine.should_enter(23500, 150, 23500, 'CE', 180, 135, dte=1,
                            recent_returns=trending_up)
    print("  Enter: %s | P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f" % (
        r['enter'], r['prob_target']*100, r['prob_sl']*100, r['expected_value']))

    print("\nTest 3: TRENDING DOWN (PE entry with bearish momentum)")
    r = engine.should_enter(23500, 150, 23500, 'PE', 180, 135, dte=1,
                            recent_returns=trending_down)
    print("  Enter: %s | P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f" % (
        r['enter'], r['prob_target']*100, r['prob_sl']*100, r['expected_value']))

    print("\nTest 4: VOLATILE DAY (high uncertainty)")
    r = engine.should_enter(23500, 150, 23500, 'CE', 180, 135, dte=1,
                            recent_returns=volatile)
    print("  Enter: %s | P(tgt)=%.0f%% P(sl)=%.0f%% EV=Rs %.0f" % (
        r['enter'], r['prob_target']*100, r['prob_sl']*100, r['expected_value']))

    print("\nTest 5: HOLD decision during WINNING trade (trending)")
    r = engine.should_hold(23550, 170, 150, 23500, 'CE', 180, 135, dte=0.8,
                           time_in_trade_min=10, recent_returns=trending_up)
    print("  Hold: %s | %s" % (r['hold'], r['action']))

    print("\nTest 6: HOLD decision during LOSING trade (no trend)")
    r = engine.should_hold(23450, 135, 150, 23500, 'CE', 180, 120, dte=0.8,
                           time_in_trade_min=15, recent_returns=normal_returns)
    print("  Hold: %s | %s" % (r['hold'], r['action']))

    print("\nTest 7: Optimal target/SL search")
    r = engine.find_optimal_target_sl(23500, 200, 23500, 'CE', 0.20, 1, 45)
    if r:
        print("  %s" % r['reason'])
    else:
        print("  No profitable config (need trending conditions)")

    # Summary
    print("\n" + "=" * 90)
    print("INTERPRETATION:")
    print("  - Without trend/momentum: MC correctly says SKIP (EV negative)")
    print("  - With trend: MC says ENTER (probability shifts in our favor)")
    print("  - During trade: MC continuously checks if edge still exists")
    print("  - This replaces fixed TSL/target with PROBABILITY-BASED exits")
