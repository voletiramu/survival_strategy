"""
Technical indicators for algo trading strategies.
All functions are instrument-agnostic -- they take OHLCV DataFrames and scalars.
"""

import numpy as np
import pandas as pd


# ====================================================================
# INDIVIDUAL INDICATOR FUNCTIONS
# ====================================================================
def compute_cpr(prev_high, prev_low, prev_close):
    """Central Pivot Range from previous day's HLC.

    Args:
        prev_high: Previous day's high.
        prev_low: Previous day's low.
        prev_close: Previous day's close.

    Returns:
        dict with pivot, bc (bottom central), tc (top central), cpr_width (%).
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * pivot - bc
    cpr_width = abs(tc - bc) / prev_close * 100
    return {'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_width}


def compute_camarilla(prev_high, prev_low, prev_close):
    """Camarilla pivot levels from previous day's HLC.

    Args:
        prev_high: Previous day's high.
        prev_low: Previous day's low.
        prev_close: Previous day's close.

    Returns:
        dict with cam_r3, cam_r4, cam_s3, cam_s4.
    """
    h_range = prev_high - prev_low
    return {
        'cam_r3': prev_close + h_range * 1.1 / 4,
        'cam_r4': prev_close + h_range * 1.1 / 2,
        'cam_s3': prev_close - h_range * 1.1 / 4,
        'cam_s4': prev_close - h_range * 1.1 / 2,
    }


def compute_atr(df, period=14):
    """Average True Range from OHLC DataFrame.

    Uses simplified ATR: mean of (High - Low) over the last `period` bars.
    For a proper Wilder ATR, use compute_atr_wilder().

    Args:
        df: DataFrame with 'High' and 'Low' columns.
        period: Lookback period (default 14).

    Returns:
        ATR value (float).
    """
    return (df['High'].tail(period) - df['Low'].tail(period)).mean()


def compute_atr_wilder(df, period=14):
    """Wilder's True Range ATR (more standard).

    Args:
        df: DataFrame with 'High', 'Low', 'Close' columns.
        period: Lookback period (default 14).

    Returns:
        ATR value (float).
    """
    high = df['High']
    low = df['Low']
    close = df['Close'].shift(1)
    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low - close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr.iloc[-1] if not atr.empty else 0.0


def compute_historical_volatility(df, period=20):
    """Historical volatility (annualized) from daily closes.

    Args:
        df: DataFrame with 'Close' column.
        period: Lookback period for std dev (default 20).

    Returns:
        Annualized HV (decimal, e.g. 0.15 for 15%).
    """
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    hv = log_ret.tail(period).std() * np.sqrt(252)
    return hv


def compute_iv_estimate(hv):
    """Estimate IV from HV (proxy when no real IV available).

    Applies a 15% premium to HV and clamps to [8%, 60%].

    Args:
        hv: Historical volatility (decimal).

    Returns:
        Estimated IV (decimal).
    """
    return max(min(hv * 1.15, 0.60), 0.08)


def compute_vwap(df, period=5):
    """Volume-weighted average price from daily OHLCV.

    Falls back to typical price average if no volume data.

    Args:
        df: DataFrame with 'High', 'Low', 'Close', and optionally 'Volume'.
        period: Lookback period (default 5).

    Returns:
        VWAP value (float).
    """
    if 'Volume' in df.columns and df['Volume'].tail(period).sum() > 0:
        tp = (df['High'] + df['Low'] + df['Close']) / 3
        return ((tp * df['Volume']).tail(period).sum()
                / df['Volume'].tail(period).sum())
    else:
        return ((df['High'] + df['Low'] + df['Close']) / 3).tail(period).mean()


def compute_pcr_proxy(df, period=5):
    """PCR proxy from price changes when no real OI data available.

    Uses recent close-to-close changes as a sentiment proxy.

    Args:
        df: DataFrame with 'Close' column.
        period: Lookback period (default 5).

    Returns:
        PCR proxy value clamped to [0.5, 2.0].
    """
    close_chg = df['Close'].pct_change()
    pcr_proxy = 1 + (close_chg.tail(period).mean() * 10)
    return max(0.5, min(2.0, pcr_proxy))


# ====================================================================
# MASTER INDICATOR COMPUTATION
# ====================================================================
def compute_all_indicators(df, current_ohlc=None):
    """Compute all indicators from historical DataFrame + optional live OHLC.

    This is the instrument-agnostic equivalent of paper_trader.py's
    compute_indicators(). It does NOT include Ghost Zone detection
    (that lives in ghost_zone_strategy.py).

    Args:
        df: Historical daily OHLCV DataFrame with columns:
            Open, High, Low, Close, Volume (Volume optional).
            Index should be DatetimeIndex.
        current_ohlc: Optional dict with today's live bar:
            {'open': float, 'high': float, 'low': float, 'close': float,
             'volume': float (optional)}.

    Returns:
        dict with all indicator values, or None if insufficient data (< 2 rows).
    """
    if df is None or len(df) < 2:
        return None

    # Make a copy to avoid mutating caller's DataFrame
    df = df.copy()

    # Append live OHLC if provided
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

    # ATR (14-period simple)
    atr = compute_atr(df, period=14)

    # Historical Volatility (20-day)
    hv = compute_historical_volatility(df, period=20)

    # IV estimate from HV
    iv = compute_iv_estimate(hv)

    # VWAP (5-day rolling)
    vwap = compute_vwap(df, period=5)

    # PCR proxy (5-day)
    pcr_proxy = compute_pcr_proxy(df, period=5)

    # Previous day's bar (for CPR, Camarilla, support/resistance)
    prev = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]

    # CPR
    cpr = compute_cpr(prev['High'], prev['Low'], prev['Close'])

    # Camarilla
    cam = compute_camarilla(prev['High'], prev['Low'], prev['Close'])

    # Support/Resistance (previous day's range)
    resistance = prev['High']
    support = prev['Low']

    # Previous range normalized by ATR
    prev_range = (prev['High'] - prev['Low']) / max(atr, 1)

    return {
        # Core indicators
        'atr': atr,
        'iv': iv,
        'hv': hv,
        'vwap': vwap,
        'pcr': pcr_proxy,
        # CPR
        'pivot': cpr['pivot'],
        'bc': cpr['bc'],
        'tc': cpr['tc'],
        'cpr_width': cpr['cpr_width'],
        # Camarilla
        'cam_r3': cam['cam_r3'],
        'cam_r4': cam['cam_r4'],
        'cam_s3': cam['cam_s3'],
        'cam_s4': cam['cam_s4'],
        # Support / Resistance
        'resistance': resistance,
        'support': support,
        # Previous day
        'prev_high': prev['High'],
        'prev_low': prev['Low'],
        'prev_close': prev['Close'],
        'prev_range': prev_range,
    }
