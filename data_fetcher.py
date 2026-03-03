"""
Data fetcher for Indian market indices.
Uses Angel One SmartAPI for real broker data (primary).
Falls back to pre-downloaded CSV data from the data/ directory.
"""

import pandas as pd
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ANGEL_CACHE_DIR = os.path.join(DATA_DIR, "angel_cache")

SYMBOLS = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}

# Map backtest symbol names to Angel One index names
ANGEL_SYMBOL_MAP = {
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "SENSEX": "SENSEX",
}

# Lot sizes (NSE revised Jan 2026)
LOT_SIZES = {
    "NIFTY50": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}

# Data source preference: 'angel' (primary) or 'csv' (legacy Yahoo)
DATA_SOURCE = 'angel'


def _normalize_angel_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize Angel One DataFrame for backtest compatibility."""
    # Strip timezone info for consistency with backtest engine
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    # Normalize to midnight (daily data)
    df.index = df.index.normalize()
    # Drop duplicates (can happen with date normalization)
    df = df[~df.index.duplicated(keep='last')]
    df.sort_index(inplace=True)
    return df


def _load_angel_cached(symbol_key: str) -> pd.DataFrame:
    """Load Angel One data from local cache if fresh enough for backtesting."""
    import time
    os.makedirs(ANGEL_CACHE_DIR, exist_ok=True)
    angel_name = ANGEL_SYMBOL_MAP.get(symbol_key, symbol_key)

    # Check locations in priority order
    cache_paths = [
        os.path.join(ANGEL_CACHE_DIR, f"{angel_name}_spot_daily.csv"),
        os.path.join(DATA_DIR, "options", f"{angel_name}_spot_one_day_2000d.csv"),
    ]

    for cache_file in cache_paths:
        if os.path.exists(cache_file):
            age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
            # For backtesting, daily data up to 7 days old is fine
            if age_hours < 168:  # 7 days
                df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                df = _normalize_angel_df(df)
                print(f"Loaded {symbol_key} from Angel cache: {len(df)} rows "
                      f"({df.index[0].date()} to {df.index[-1].date()}) "
                      f"[cache {age_hours:.1f}h old]", flush=True)
                return df
    return None


def _fetch_angel_live(symbol_key: str, days_back: int = 2000) -> pd.DataFrame:
    """Fetch fresh data from Angel One SmartAPI and cache it."""
    try:
        from angel_data_fetcher import AngelDataFetcher
    except ImportError:
        print(f"angel_data_fetcher not available, cannot fetch {symbol_key}", flush=True)
        return None

    angel_name = ANGEL_SYMBOL_MAP.get(symbol_key, symbol_key)
    os.makedirs(ANGEL_CACHE_DIR, exist_ok=True)

    fetcher = AngelDataFetcher(app_type='Historical')
    if not fetcher.login():
        print(f"Angel One login failed for {symbol_key}", flush=True)
        return None

    try:
        df = fetcher.fetch_index_spot(name=angel_name, interval='ONE_DAY', days_back=days_back)
        if df is not None and len(df) > 0:
            # Normalize timezone and index
            df = _normalize_angel_df(df)

            # Ensure column names match expected format
            expected_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
            for col in expected_cols:
                if col not in df.columns:
                    print(f"Warning: Missing column '{col}' in Angel data for {symbol_key}", flush=True)

            # Save to cache
            cache_file = os.path.join(ANGEL_CACHE_DIR, f"{angel_name}_spot_daily.csv")
            df.to_csv(cache_file)
            print(f"Fetched {symbol_key} from Angel One: {len(df)} rows "
                  f"({df.index[0].date()} to {df.index[-1].date()})", flush=True)
            return df
        else:
            print(f"No data returned from Angel One for {symbol_key}", flush=True)
            return None
    finally:
        fetcher.logout()


def load_or_fetch(symbol_key: str, period: str = "5y", interval: str = "1d",
                  force: bool = False, source: str = None) -> pd.DataFrame:
    """Load market data - Angel One (primary) with CSV fallback.

    Args:
        symbol_key: Symbol name (NIFTY50, BANKNIFTY, SENSEX)
        period: Data period ('5y', '3y', '1y')
        interval: Data interval (currently only '1d' supported)
        force: Force re-fetch even if cache exists
        source: Override data source ('angel' or 'csv')
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    use_source = source or DATA_SOURCE

    days_map = {'5y': 1825, '3y': 1095, '1y': 365, '2y': 730}
    days_back = days_map.get(period, 1825)

    # Try Angel One data first (if configured as primary)
    if use_source == 'angel':
        # Check cache first
        if not force:
            df = _load_angel_cached(symbol_key)
            if df is not None and len(df) >= 100:
                return df

        # Fetch fresh from Angel One
        df = _fetch_angel_live(symbol_key, days_back=days_back)
        if df is not None and len(df) >= 100:
            return df

        print(f"Angel One fetch failed for {symbol_key}, falling back to CSV...", flush=True)

    # Fallback: load from pre-downloaded CSV (legacy Yahoo data)
    filename = f"{symbol_key}_{interval}_{period}.csv"
    filepath = os.path.join(DATA_DIR, filename)

    if os.path.exists(filepath):
        df = pd.read_csv(filepath, index_col=0, parse_dates=True)
        print(f"Loaded {symbol_key} from CSV: {len(df)} rows "
              f"({df.index[0].date()} to {df.index[-1].date()}) [legacy Yahoo]", flush=True)
        return df

    raise FileNotFoundError(
        f"No data available for {symbol_key}.\n"
        f"Angel One fetch failed and CSV not found at: {filepath}\n"
        f"Run 'python download_data.py' or check Angel One credentials."
    )


def get_all_daily_data(period: str = "5y", force: bool = False, source: str = None) -> dict:
    """Load daily data for all symbols."""
    data = {}
    for key in SYMBOLS:
        try:
            data[key] = load_or_fetch(key, period=period, force=force, source=source)
        except Exception as e:
            print(f"Error loading {key}: {e}", flush=True)
    return data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Fetch market data')
    parser.add_argument('--source', choices=['angel', 'csv'], default='angel',
                        help='Data source: angel (Angel One API) or csv (legacy Yahoo)')
    parser.add_argument('--force', action='store_true', help='Force re-fetch')
    args = parser.parse_args()

    data = get_all_daily_data(source=args.source, force=args.force)
    for k, v in data.items():
        print(f"{k}: {v.shape}, Date range: {v.index[0]} to {v.index[-1]}")
