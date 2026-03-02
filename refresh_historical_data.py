"""
Historical Data Refresh Script
===============================
Downloads daily OHLC data from Angel SmartAPI for all symbols.
Runs STANDALONE with proper rate limiting (3s between chunks, 10s between symbols).
Use this when historical data is stale/missing.

Usage: python refresh_historical_data.py
"""
import sys
import os
import json
import time
import pandas as pd
from datetime import datetime, timedelta

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
os.makedirs(DATA_DIR, exist_ok=True)

# ============================================================
# Angel SmartAPI connection
# ============================================================
def connect_angel():
    """Connect to Angel SmartAPI."""
    creds_path = os.path.join(BASE_DIR, 'angel_credentials.json')
    if not os.path.exists(creds_path):
        print(f"ERROR: {creds_path} not found!")
        return None

    with open(creds_path) as f:
        creds = json.load(f)

    from SmartApi import SmartConnect
    import pyotp

    obj = SmartConnect(api_key=creds['api_key'])
    totp = pyotp.TOTP(creds['totp_secret']).now()
    data = obj.generateSession(creds['client_id'], creds['password'], totp)

    if data.get('status'):
        print(f"Connected to Angel as {creds['client_id']}")
        return obj
    else:
        print(f"Angel connection FAILED: {data}")
        return None


def download_symbol(obj, symbol, exchange, token, save_path, days_back=2000):
    """Download daily OHLC for one symbol with chunked requests."""
    print(f"\n{'='*60}")
    print(f"Downloading {symbol} (token={token}, exchange={exchange})")
    print(f"{'='*60}")

    all_data = []
    chunk_start = datetime.now() - timedelta(days=days_back)
    chunk_num = 0

    # Determine time format based on exchange
    if exchange == 'MCX':
        from_time, to_time = '09:00', '23:30'
    else:
        from_time, to_time = '09:15', '15:30'

    while chunk_start < datetime.now():
        chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
        chunk_num += 1

        try:
            params = {
                'exchange': exchange,
                'symboltoken': token,
                'interval': 'ONE_DAY',
                'fromdate': chunk_start.strftime(f'%Y-%m-%d {from_time}'),
                'todate': chunk_end.strftime(f'%Y-%m-%d {to_time}'),
            }
            data = obj.getCandleData(params)

            if data and data.get('status') and data.get('data'):
                rows = data['data']
                all_data.extend(rows)
                print(f"  Chunk {chunk_num}: {chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')} → {len(rows)} rows")
            else:
                msg = data.get('message', 'No data') if data else 'None'
                print(f"  Chunk {chunk_num}: {chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')} → EMPTY ({msg})")
        except Exception as e:
            print(f"  Chunk {chunk_num}: ERROR — {e}")

        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(3)  # 3 seconds between API calls

    if all_data:
        df = pd.DataFrame(all_data, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df.to_csv(save_path, index=False)
        print(f"  SAVED: {len(df)} rows → {save_path}")

        # Show last 3 rows
        tail = df.tail(3)
        for _, row in tail.iterrows():
            dt = str(row['DateTime'])[:10]
            print(f"    {dt}: O={row['Open']:.2f} H={row['High']:.2f} L={row['Low']:.2f} C={row['Close']:.2f}")
    else:
        print(f"  WARNING: No data downloaded for {symbol}!")
    return len(all_data)


def main():
    print("=" * 60)
    print("HISTORICAL DATA REFRESH")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    obj = connect_angel()
    if not obj:
        sys.exit(1)

    # ---- EQUITY INDICES ----
    equity_symbols = [
        ('NIFTY',     'NSE', '99926000', 'NIFTY_spot_one_day_2000d.csv'),
        ('BANKNIFTY', 'NSE', '99926009', 'BANKNIFTY_spot_one_day_2000d.csv'),
        ('SENSEX',    'BSE', '99919000', 'SENSEX_spot_one_day_2000d.csv'),
    ]

    for symbol, exchange, token, filename in equity_symbols:
        save_path = os.path.join(DATA_DIR, filename)
        count = download_symbol(obj, symbol, exchange, token, save_path)
        print(f"  → {symbol}: {count} rows downloaded")
        time.sleep(10)  # 10 seconds between symbols

    # ---- MCX COMMODITIES ----
    # First, find current futures tokens for MCX commodities
    print(f"\n{'='*60}")
    print("Looking up MCX futures tokens from instrument master...")
    print(f"{'='*60}")

    # Load instrument master
    inst_path = os.path.join(DATA_DIR, 'instrument_master.csv')
    if not os.path.exists(inst_path):
        print("Downloading instrument master...")
        try:
            instruments = obj.fetchAllInstruments()
            if instruments:
                import io
                inst_df = pd.read_csv(io.StringIO(instruments))
                inst_df.to_csv(inst_path, index=False)
                print(f"  Instrument master saved: {len(inst_df)} rows")
        except Exception as e:
            print(f"  Instrument master download failed: {e}")
            instruments = None

    if os.path.exists(inst_path):
        inst_df = pd.read_csv(inst_path, low_memory=False)
        mcx_df = inst_df[inst_df['exch_seg'] == 'MCX']

        commodity_symbols = [
            ('GOLDM',      'GOLDM_spot_one_day_2000d.csv'),
            ('SILVERM',    'SILVERM_spot_one_day_2000d.csv'),
            ('CRUDEOILM',  'CRUDEOIL_spot_one_day_2000d.csv'),
        ]

        for commodity, filename in commodity_symbols:
            # Find the nearest futures contract token
            futures = mcx_df[
                (mcx_df['name'].str.contains(commodity, case=False, na=False)) &
                (mcx_df['instrumenttype'].str.contains('FUT', case=False, na=False))
            ].sort_values('expiry')

            if len(futures) > 0:
                token = str(futures.iloc[0]['token'])
                expiry = str(futures.iloc[0]['expiry'])[:10]
                print(f"\n  {commodity}: Found futures token={token} (expiry={expiry})")

                save_path = os.path.join(DATA_DIR, filename)
                count = download_symbol(obj, commodity, 'MCX', token, save_path)
                print(f"  → {commodity}: {count} rows downloaded")
            else:
                print(f"\n  {commodity}: No futures contract found in instrument master!")

            time.sleep(10)  # 10 seconds between symbols
    else:
        print("WARNING: No instrument master available, skipping MCX commodities")

    print(f"\n{'='*60}")
    print("DATA REFRESH COMPLETE")
    print(f"{'='*60}")

    # Summary
    for f in os.listdir(DATA_DIR):
        if f.endswith('_2000d.csv'):
            fpath = os.path.join(DATA_DIR, f)
            df = pd.read_csv(fpath)
            last_date = str(df['DateTime'].iloc[-1])[:10] if len(df) > 0 else 'EMPTY'
            print(f"  {f}: {len(df)} rows, last={last_date}")


if __name__ == '__main__':
    main()
