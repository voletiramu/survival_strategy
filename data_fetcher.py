"""
Data fetcher for Indian market indices.
Loads pre-downloaded CSV data from the data/ directory.
"""

import pandas as pd
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

SYMBOLS = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}

# Lot sizes (as of 2024-2025)
LOT_SIZES = {
    "NIFTY50": 25,
    "BANKNIFTY": 15,
    "SENSEX": 10,
}


def load_or_fetch(symbol_key: str, period: str = "5y", interval: str = "1d", force: bool = False) -> pd.DataFrame:
    """Load cached CSV data."""
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"{symbol_key}_{interval}_{period}.csv"
    filepath = os.path.join(DATA_DIR, filename)

    if os.path.exists(filepath):
        df = pd.read_csv(filepath, index_col=0, parse_dates=True)
        print(f"Loaded {symbol_key}: {len(df)} rows ({df.index[0].date()} to {df.index[-1].date()})", flush=True)
        return df

    raise FileNotFoundError(
        f"Data file not found: {filepath}\n"
        f"Run 'python download_data.py' first to download data."
    )


def get_all_daily_data(period: str = "5y", force: bool = False) -> dict:
    """Load daily data for all symbols."""
    data = {}
    for key in SYMBOLS:
        try:
            data[key] = load_or_fetch(key, period=period, force=force)
        except Exception as e:
            print(f"Error loading {key}: {e}", flush=True)
    return data


if __name__ == "__main__":
    data = get_all_daily_data()
    for k, v in data.items():
        print(f"{k}: {v.shape}, Date range: {v.index[0]} to {v.index[-1]}")
