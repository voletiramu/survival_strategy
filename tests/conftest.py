"""
Shared fixtures for algo_trading test suite.
Provides sample DataFrames, engine instances, and mock market data.
"""

import sys
import os
import pytest
import pandas as pd
import numpy as np

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── Sample Market Data Fixtures ─────────────────────────────────────────────


def _make_ohlcv(
    n: int = 100,
    start_price: float = 20000.0,
    volatility: float = 0.01,
    start_date: str = "2024-01-01",
    trend: float = 0.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing.

    Args:
        n: Number of bars.
        start_price: Starting close price.
        volatility: Daily return std-dev.
        start_date: Start date string.
        trend: Daily drift (e.g., 0.0005 for slight uptrend).
        seed: Random seed for reproducibility.
    """
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range(start=start_date, periods=n, freq="B")  # business days

    closes = [start_price]
    for _ in range(1, n):
        ret = trend + volatility * rng.randn()
        closes.append(closes[-1] * (1 + ret))
    closes = np.array(closes)

    # Generate realistic OHLCV from closes
    highs = closes * (1 + np.abs(rng.randn(n) * volatility * 0.5))
    lows = closes * (1 - np.abs(rng.randn(n) * volatility * 0.5))
    opens = closes * (1 + rng.randn(n) * volatility * 0.3)
    volumes = (rng.lognormal(mean=15, sigma=0.5, size=n)).astype(int)

    # Ensure High >= max(Open, Close) and Low <= min(Open, Close)
    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )
    return df


@pytest.fixture
def sample_df():
    """100-bar neutral OHLCV DataFrame for NIFTY-like prices."""
    return _make_ohlcv(n=100, start_price=20000.0)


@pytest.fixture
def large_df():
    """500-bar OHLCV DataFrame for longer backtests."""
    return _make_ohlcv(n=500, start_price=20000.0)


@pytest.fixture
def uptrend_df():
    """100-bar DataFrame with upward trend."""
    return _make_ohlcv(n=100, start_price=20000.0, trend=0.002, seed=123)


@pytest.fixture
def downtrend_df():
    """100-bar DataFrame with downward trend."""
    return _make_ohlcv(n=100, start_price=20000.0, trend=-0.002, seed=456)


@pytest.fixture
def flat_df():
    """100-bar DataFrame with very low volatility (range-bound)."""
    return _make_ohlcv(n=100, start_price=20000.0, volatility=0.002, seed=789)


@pytest.fixture
def volatile_df():
    """100-bar DataFrame with high volatility."""
    return _make_ohlcv(n=100, start_price=20000.0, volatility=0.03, seed=321)


@pytest.fixture
def tiny_df():
    """5-bar minimal DataFrame for edge-case tests."""
    return _make_ohlcv(n=5, start_price=20000.0)


@pytest.fixture
def empty_df():
    """Empty DataFrame with correct columns."""
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ── Engine Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    """Default BacktestEngine instance."""
    from backtest_engine import BacktestEngine

    return BacktestEngine(
        initial_capital=300000.0,
        lot_size=25,
        max_risk_per_trade_pct=2.0,
        brokerage_per_order=20.0,
        slippage_points=2.0,
    )


@pytest.fixture
def engine_nifty():
    """BacktestEngine configured for NIFTY50 lot size."""
    from backtest_engine import BacktestEngine

    return BacktestEngine(initial_capital=300000.0, lot_size=65)


@pytest.fixture
def engine_banknifty():
    """BacktestEngine configured for BANKNIFTY lot size."""
    from backtest_engine import BacktestEngine

    return BacktestEngine(initial_capital=300000.0, lot_size=30)


# ── Helper for creating ghost zone test data ─────────────────────────────────


@pytest.fixture
def ghost_zone_df():
    """DataFrame with volume spikes suitable for Ghost Zone detection."""
    rng = np.random.RandomState(42)
    n = 200
    dates = pd.bdate_range(start="2024-01-01", periods=n, freq="B")

    closes = [20000.0]
    for _ in range(1, n):
        closes.append(closes[-1] * (1 + 0.01 * rng.randn()))
    closes = np.array(closes)

    highs = closes * (1 + np.abs(rng.randn(n) * 0.005))
    lows = closes * (1 - np.abs(rng.randn(n) * 0.005))
    opens = closes * (1 + rng.randn(n) * 0.003)
    volumes = (rng.lognormal(mean=15, sigma=0.5, size=n)).astype(int)

    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))

    # Inject volume spikes every ~40 bars to create detectable zones
    for i in range(40, n, 40):
        volumes[i] = volumes[i] * 8  # 8x volume spike
        # Make it an injection candle (long wick)
        body = abs(closes[i] - opens[i])
        if rng.rand() > 0.5:
            lows[i] = lows[i] - (highs[i] - lows[i]) * 1.5  # long lower wick
        else:
            highs[i] = highs[i] + (highs[i] - lows[i]) * 1.5  # long upper wick

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
            "Date": dates,
        },
        index=dates,
    )
    return df
