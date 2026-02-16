"""
Direct data downloader using Yahoo Finance API via requests.
Bypasses yfinance's curl_cffi which can hang on some Windows systems.
"""
import os
import sys
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta

sys.stdout.reconfigure(line_buffering=True)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

SYMBOLS = {
    "NIFTY50": "%5ENSEI",
    "BANKNIFTY": "%5ENSEBANK",
    "SENSEX": "%5EBSESN",
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
}


def download_yahoo_data(symbol_key, encoded_symbol, period1, period2):
    """Download data directly from Yahoo Finance API."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
        f"?period1={period1}&period2={period2}&interval=1d"
        f"&includePrePost=false&events=div%7Csplit"
    )

    print(f"  Downloading {symbol_key} from Yahoo Finance...", flush=True)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Error downloading {symbol_key}: {e}", flush=True)
        return None

    try:
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]

        df = pd.DataFrame({
            'Open': quote['open'],
            'High': quote['high'],
            'Low': quote['low'],
            'Close': quote['close'],
            'Volume': quote['volume'],
        })
        df.index = pd.to_datetime(timestamps, unit='s')
        df.index.name = 'Date'

        # Remove any rows with all NaN
        df = df.dropna(how='all')

        # Forward fill small gaps
        df = df.ffill()

        return df
    except (KeyError, IndexError) as e:
        print(f"  Error parsing {symbol_key} data: {e}", flush=True)
        return None


def main():
    print("=" * 60, flush=True)
    print("Downloading 5-year data for Indian indices", flush=True)
    print("=" * 60, flush=True)

    # 5 years ago
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5*365)
    period1 = int(start_date.timestamp())
    period2 = int(end_date.timestamp())

    for key, encoded in SYMBOLS.items():
        df = download_yahoo_data(key, encoded, period1, period2)

        if df is not None and len(df) > 0:
            filepath = os.path.join(DATA_DIR, f"{key}_1d_5y.csv")
            df.to_csv(filepath)
            print(f"  {key}: {len(df)} rows saved to {filepath}", flush=True)
            print(f"  Date range: {df.index[0].date()} to {df.index[-1].date()}", flush=True)
            print(f"  Last close: {df['Close'].iloc[-1]:.2f}", flush=True)
        else:
            print(f"  {key}: FAILED to download", flush=True)

    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
