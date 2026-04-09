"""
Black-Scholes and Black-76 option pricing with Greeks.
All functions are instrument-agnostic -- they take spot/forward, strike, time, rate, vol.
"""

import numpy as np
from scipy.stats import norm


# ====================================================================
# BLACK-SCHOLES (Equity/Index Options)
# ====================================================================
def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option pricing with Greeks.

    Args:
        S: Spot (underlying) price.
        K: Strike price.
        T: Time to expiry in years (e.g. 7/365 for 7 days).
        r: Risk-free rate (annualized decimal, e.g. 0.065).
        sigma: Volatility (annualized decimal, e.g. 0.15 for 15%).
        opt_type: 'CE' for call, 'PE' for put.

    Returns:
        dict with keys: price, delta, gamma, theta (per day), vega (per 1% vol), iv.
    """
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    S = max(S, 0.01)
    K = max(K, 0.01)

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if opt_type == 'CE':
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = -norm.cdf(-d1)

    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    theta = (-(S * sigma * norm.pdf(d1)) / (2 * np.sqrt(T))
             - r * K * np.exp(-r * T) * norm.cdf(d2 if opt_type == 'CE' else -d2))
    theta /= 365  # Per-day theta
    vega = S * np.sqrt(T) * norm.pdf(d1) / 100  # Per 1% vol change

    return {
        'price': max(price, 0.05),
        'delta': delta,
        'gamma': gamma,
        'theta': theta,
        'vega': vega,
        'iv': sigma,
    }


# ====================================================================
# BLACK-76 (Commodity Futures Options)
# ====================================================================
def black76_greeks(F, K, T, r, sigma, opt_type='CE'):
    """Black-76 model for futures/commodity options.

    Same as Black-Scholes but uses forward price F instead of spot S.
    No cost-of-carry: d1 = (ln(F/K) + 0.5*sigma^2*T) / (sigma*sqrt(T)).

    Args:
        F: Forward/futures price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate (for discounting).
        sigma: Volatility (annualized decimal).
        opt_type: 'CE' for call, 'PE' for put.

    Returns:
        dict with keys: price, delta, gamma, theta (per day), vega (per 1% vol), iv.
    """
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    F = max(F, 0.01)
    K = max(K, 0.01)

    d1 = (np.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    discount = np.exp(-r * T)

    if opt_type == 'CE':
        price = discount * (F * norm.cdf(d1) - K * norm.cdf(d2))
        delta = discount * norm.cdf(d1)
    else:
        price = discount * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        delta = -discount * norm.cdf(-d1)

    gamma = discount * norm.pdf(d1) / (F * sigma * np.sqrt(T))
    theta = (-(F * sigma * norm.pdf(d1) * discount) / (2 * np.sqrt(T))
             - r * discount * (F * norm.cdf(d1) - K * norm.cdf(d2))
             if opt_type == 'CE' else
             -(F * sigma * norm.pdf(d1) * discount) / (2 * np.sqrt(T))
             + r * discount * (K * norm.cdf(-d2) - F * norm.cdf(-d1)))
    theta /= 365  # Per-day theta
    vega = F * np.sqrt(T) * norm.pdf(d1) * discount / 100  # Per 1% vol change

    return {
        'price': max(price, 0.05),
        'delta': delta,
        'gamma': gamma,
        'theta': theta,
        'vega': vega,
        'iv': sigma,
    }


# ====================================================================
# IMPLIED VOLATILITY SOLVER
# ====================================================================
def implied_vol(market_price, S, K, T, r, opt_type='CE', max_iter=50, tol=1e-4,
                model='bs'):
    """Newton-Raphson IV solver.

    Back-solves for implied volatility from a real market option price.

    Args:
        market_price: Observed market LTP of the option.
        S: Spot price (or forward price F if model='black76').
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        opt_type: 'CE' or 'PE'.
        max_iter: Maximum Newton-Raphson iterations.
        tol: Convergence tolerance (price difference).
        model: 'bs' for Black-Scholes, 'black76' for Black-76.

    Returns:
        Implied volatility (decimal) or None if convergence fails.
    """
    if market_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None

    pricer = black76_greeks if model == 'black76' else bs_greeks

    # Intrinsic value check
    if model == 'black76':
        discount = np.exp(-r * T)
        if opt_type == 'CE':
            intrinsic = max(discount * (S - K), 0)
        else:
            intrinsic = max(discount * (K - S), 0)
    else:
        if opt_type == 'CE':
            intrinsic = max(S - K * np.exp(-r * T), 0)
        else:
            intrinsic = max(K * np.exp(-r * T) - S, 0)

    if market_price < intrinsic * 0.9:
        return None  # Price below intrinsic -- likely bad data

    # Initial guess: Brenner-Subrahmanyam ATM approximation
    sigma = market_price / S * np.sqrt(2 * np.pi / T)
    sigma = max(min(sigma, 3.0), 0.01)  # Clamp between 1% and 300%

    for _ in range(max_iter):
        g = pricer(S, K, T, r, sigma, opt_type)
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


def greeks_from_market_price(market_price, S, K, T, r, opt_type='CE',
                             model='bs'):
    """Compute accurate Greeks by first solving for IV from real market LTP.

    Args:
        market_price: Observed market LTP.
        S: Spot price (or forward price if model='black76').
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        opt_type: 'CE' or 'PE'.
        model: 'bs' or 'black76'.

    Returns:
        Full Greeks dict with real IV, or None if IV solve fails.
    """
    iv = implied_vol(market_price, S, K, T, r, opt_type, model=model)
    if iv is None:
        return None

    pricer = black76_greeks if model == 'black76' else bs_greeks
    g = pricer(S, K, T, r, iv, opt_type)
    g['iv'] = iv  # Store the real implied volatility
    g['price'] = market_price  # Use actual market price, not model-computed
    return g
