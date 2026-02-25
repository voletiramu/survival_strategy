"""
Angel One SmartAPI - Options Data Fetcher with OI & Greeks
==========================================================
Fetches real-time and recent historical options data for
NIFTY, BANKNIFTY, SENSEX from Angel One SmartAPI.

Features:
- Live option chain with OI for all strikes
- Greeks (Delta, Gamma, Theta, Vega, IV) via optionGreek API
- Historical candle data for active option contracts
- PCR and OI Buildup data
- Instrument master download and filtering

Limitations:
- Historical data for EXPIRED contracts is NOT available
- Only active/live contracts return candle data
- Rate limit: 3 req/sec for historical, 10 req/sec for quotes
"""

import sys
import os
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

try:
    from SmartApi import SmartConnect
    import pyotp
except ImportError:
    print("Installing required packages...")
    os.system("pip install smartapi-python pyotp")
    from SmartApi import SmartConnect
    import pyotp

# ====================================================================
# CONFIGURATION - Angel One Credentials
# ====================================================================
def load_credentials():
    """Load Angel One credentials from config file."""
    cred_file = r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt"
    creds = {}
    current_app = {}

    with open(cred_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                key = key.strip()
                raw_val = val.strip()

                # Special handling for TOTP key - the real secret might be in the comment
                # Format: "your_totp_secret  # ACTUAL_SECRET_HERE"
                if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                    parts = raw_val.split('#')
                    comment_val = parts[-1].strip()
                    main_val = parts[0].strip()
                    # Use comment value if main value looks like placeholder
                    if 'your_' in main_val.lower() or 'secret' in main_val.lower():
                        val = comment_val
                    else:
                        val = main_val
                else:
                    val = raw_val.split('#')[0].strip()  # Remove inline comments

                current_app[key] = val

                if key == 'BROKER_NAME':
                    app_type = current_app.get('ANGEL_APP_TYPE', 'Unknown')
                    creds[app_type] = current_app.copy()
                    current_app = {}

    return creds

# ====================================================================
# ANGEL API CONNECTION
# ====================================================================
class AngelDataFetcher:
    def __init__(self, app_type='Historical'):
        """Initialize with Historical or Market app credentials."""
        self.creds = load_credentials()

        if app_type not in self.creds:
            available = list(self.creds.keys())
            print(f"App type '{app_type}' not found. Available: {available}")
            app_type = available[0] if available else None

        if not app_type:
            raise ValueError("No credentials found!")

        self.config = self.creds[app_type]
        self.api_key = self.config['ANGEL_API_KEY']
        self.client_code = self.config['ANGEL_CLIENT_CODE']
        self.pin = self.config['ANGEL_PIN']
        self.totp_secret = self.config['ANGEL_TOTP_KEY']
        self.obj = None
        self.instruments_df = None
        self.data_dir = os.path.join(os.path.dirname(__file__), 'data', 'options')
        os.makedirs(self.data_dir, exist_ok=True)

        print(f"Initialized Angel fetcher: App={self.config.get('ANGEL_APP_NAME', 'N/A')}, "
              f"Type={app_type}", flush=True)

    def login(self):
        """Authenticate with Angel One SmartAPI."""
        print("\nLogging in to Angel One SmartAPI...", flush=True)
        try:
            self.obj = SmartConnect(api_key=self.api_key)
            totp = pyotp.TOTP(self.totp_secret).now()
            data = self.obj.generateSession(self.client_code, self.pin, totp)

            if data.get('status'):
                self.auth_token = data['data']['jwtToken']
                self.refresh_token = data['data']['refreshToken']
                self.feed_token = self.obj.getfeedToken()
                print(f"  Login SUCCESS! Client: {self.client_code}", flush=True)
                return True
            else:
                print(f"  Login FAILED: {data.get('message', 'Unknown error')}", flush=True)
                return False
        except Exception as e:
            print(f"  Login ERROR: {str(e)}", flush=True)
            return False

    def logout(self):
        """Terminate session."""
        if self.obj:
            try:
                self.obj.terminateSession(self.client_code)
                print("Logged out successfully.", flush=True)
            except:
                pass

    # ====================================================================
    # INSTRUMENT MASTER
    # ====================================================================
    def download_instruments(self, force=False):
        """Download and cache the instrument master file."""
        cache_file = os.path.join(self.data_dir, 'instrument_master.csv')

        # Use cached if less than 12 hours old
        if not force and os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 12 * 3600:
                print(f"  Using cached instrument master ({age/3600:.1f}h old)", flush=True)
                self.instruments_df = pd.read_csv(cache_file)
                self.instruments_df['expiry'] = pd.to_datetime(self.instruments_df['expiry'])
                return self.instruments_df

        print("\nDownloading instrument master from Angel One...", flush=True)
        urls = [
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
        ]

        for url in urls:
            try:
                resp = requests.get(url, timeout=60)
                if resp.status_code == 200:
                    instruments = resp.json()
                    df = pd.DataFrame(instruments)
                    df['expiry'] = pd.to_datetime(df['expiry'], format='mixed', dayfirst=True, errors='coerce')
                    df['strike'] = pd.to_numeric(df['strike'], errors='coerce') / 100
                    df['lotsize'] = pd.to_numeric(df['lotsize'], errors='coerce')

                    # Save cache
                    df.to_csv(cache_file, index=False)
                    self.instruments_df = df
                    print(f"  Downloaded {len(df)} instruments", flush=True)

                    # Show summary for our indices
                    for idx in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
                        opts = df[(df['name'] == idx) & (df['instrumenttype'] == 'OPTIDX')]
                        if len(opts) > 0:
                            expiries = opts['expiry'].dropna().unique()
                            print(f"  {idx}: {len(opts)} options, {len(expiries)} expiries", flush=True)

                    return df
            except Exception as e:
                print(f"  Failed from {url}: {e}", flush=True)
                continue

        print("  ERROR: Could not download instrument master", flush=True)
        return None

    def get_option_tokens(self, name='NIFTY', expiry=None, strike=None, opt_type=None):
        """Get option tokens for a specific index, expiry, strike."""
        if self.instruments_df is None:
            self.download_instruments()

        df = self.instruments_df
        mask = (df['name'] == name) & (df['instrumenttype'] == 'OPTIDX')

        if name == 'SENSEX':
            mask = (df['name'] == name) & (df['instrumenttype'] == 'OPTIDX') & (df['exch_seg'] == 'BFO')
        else:
            mask = mask & (df['exch_seg'] == 'NFO')

        opts = df[mask].copy()

        if expiry is not None:
            opts = opts[opts['expiry'] == pd.Timestamp(expiry)]

        if strike is not None:
            opts = opts[abs(opts['strike'] - strike) < 1]

        if opt_type is not None:
            opts = opts[opts['symbol'].str.endswith(opt_type)]

        return opts.sort_values('strike')

    def get_nearest_expiry(self, name='NIFTY'):
        """Get nearest expiry date for an index."""
        opts = self.get_option_tokens(name)
        if len(opts) == 0:
            return None
        future_expiries = opts[opts['expiry'] >= pd.Timestamp.now()]['expiry'].unique()
        if len(future_expiries) == 0:
            return None
        return min(future_expiries)

    def get_atm_strike(self, name='NIFTY', spot_price=None):
        """Get ATM strike for given spot price."""
        if spot_price is None:
            # Try to get LTP from index
            try:
                if name == 'NIFTY':
                    ltp_data = self.obj.ltpData('NSE', 'Nifty 50', '99926000')
                elif name == 'BANKNIFTY':
                    ltp_data = self.obj.ltpData('NSE', 'Nifty Bank', '99926009')
                elif name == 'SENSEX':
                    ltp_data = self.obj.ltpData('BSE', 'SENSEX', '99919000')

                if ltp_data.get('status') and ltp_data.get('data'):
                    spot_price = ltp_data['data']['ltp']
                    print(f"  {name} Spot: {spot_price}", flush=True)
            except Exception as e:
                print(f"  Could not get {name} LTP: {e}", flush=True)

        if spot_price is None:
            return None

        # Round to nearest strike interval
        if name == 'NIFTY':
            interval = 50
        elif name == 'BANKNIFTY':
            interval = 100
        elif name == 'SENSEX':
            interval = 100
        else:
            interval = 50

        return round(spot_price / interval) * interval

    # ====================================================================
    # OPTION CHAIN WITH OI
    # ====================================================================
    def fetch_option_chain(self, name='NIFTY', expiry=None, num_strikes=10):
        """Fetch full option chain with OI for an index.

        Args:
            name: Index name (NIFTY, BANKNIFTY, SENSEX)
            expiry: Specific expiry date or None for nearest
            num_strikes: Number of strikes above/below ATM

        Returns:
            DataFrame with strike, type, ltp, oi, volume, greeks
        """
        print(f"\n{'='*60}", flush=True)
        print(f"Fetching Option Chain: {name}", flush=True)
        print(f"{'='*60}", flush=True)

        if expiry is None:
            expiry = self.get_nearest_expiry(name)
            if expiry is None:
                print("  No active expiries found!", flush=True)
                return None

        print(f"  Expiry: {expiry}", flush=True)

        # Get ATM strike
        atm = self.get_atm_strike(name)
        if atm is None:
            print("  Could not determine ATM strike", flush=True)
            return None

        print(f"  ATM Strike: {atm}", flush=True)

        # Get tokens for strikes around ATM
        opts = self.get_option_tokens(name, expiry)
        if len(opts) == 0:
            print("  No option contracts found!", flush=True)
            return None

        # Filter for strikes near ATM
        strike_interval = 50 if name == 'NIFTY' else 100
        strikes_range = [atm + i * strike_interval for i in range(-num_strikes, num_strikes + 1)]
        near_opts = opts[opts['strike'].isin(strikes_range)]

        print(f"  Found {len(near_opts)} contracts to fetch ({len(strikes_range)} strikes x CE/PE)", flush=True)

        # Fetch market data with OI
        exchange = 'BFO' if name == 'SENSEX' else 'NFO'
        chain_data = []
        total = len(near_opts)

        for idx, (_, row) in enumerate(near_opts.iterrows()):
            try:
                mkt = self.obj.getMarketData("FULL", {exchange: [row['token']]})
                if mkt.get('status') and mkt.get('data') and mkt['data'].get('fetched'):
                    fetched = mkt['data']['fetched'][0]
                    opt_type = 'CE' if row['symbol'].endswith('CE') else 'PE'
                    chain_data.append({
                        'symbol': row['symbol'],
                        'token': row['token'],
                        'strike': row['strike'],
                        'type': opt_type,
                        'expiry': str(expiry.date()) if hasattr(expiry, 'date') else str(expiry),
                        'ltp': fetched.get('ltp', 0),
                        'open': fetched.get('open', 0),
                        'high': fetched.get('high', 0),
                        'low': fetched.get('low', 0),
                        'close': fetched.get('close', 0),
                        'volume': fetched.get('tradeVolume', 0),
                        'oi': fetched.get('opnInterest', 0),
                        'oi_change': fetched.get('opnInterestChange', 0) if 'opnInterestChange' in fetched else 0,
                        'change_pct': fetched.get('percentChange', 0),
                    })
                time.sleep(0.12)  # Rate limit: 10 req/sec

                if (idx + 1) % 10 == 0:
                    print(f"    Fetched {idx+1}/{total}...", flush=True)

            except Exception as e:
                print(f"    Error for {row['symbol']}: {e}", flush=True)
                time.sleep(0.5)

        if chain_data:
            chain_df = pd.DataFrame(chain_data)
            print(f"\n  Option Chain fetched: {len(chain_df)} contracts", flush=True)
            print(f"  Total CE OI: {chain_df[chain_df['type']=='CE']['oi'].sum():,.0f}", flush=True)
            print(f"  Total PE OI: {chain_df[chain_df['type']=='PE']['oi'].sum():,.0f}", flush=True)

            # Calculate PCR
            ce_oi = chain_df[chain_df['type']=='CE']['oi'].sum()
            pe_oi = chain_df[chain_df['type']=='PE']['oi'].sum()
            pcr = pe_oi / ce_oi if ce_oi > 0 else 0
            print(f"  PCR (OI): {pcr:.2f}", flush=True)

            # Save to CSV
            filename = f"option_chain_{name}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            filepath = os.path.join(self.data_dir, filename)
            chain_df.to_csv(filepath, index=False)
            print(f"  Saved: {filepath}", flush=True)

            return chain_df

        return None

    # ====================================================================
    # OPTION GREEKS FROM ANGEL API
    # ====================================================================
    def fetch_option_greeks(self, name='NIFTY', expiry=None):
        """Fetch Greeks (Delta, Gamma, Theta, Vega, IV) for all strikes.

        Uses Angel's optionGreek API endpoint.
        """
        print(f"\n  Fetching Greeks for {name}...", flush=True)

        if expiry is None:
            expiry = self.get_nearest_expiry(name)

        if expiry is None:
            print("  No expiry found!", flush=True)
            return None

        # Format expiry as DDMMMYYYY (e.g., 27FEB2025)
        expiry_str = pd.Timestamp(expiry).strftime('%d%b%Y').upper()

        try:
            greeks = self.obj.optionGreek({
                "name": name,
                "expirydate": expiry_str
            })

            if greeks and greeks.get('status') and greeks.get('data'):
                greeks_df = pd.DataFrame(greeks['data'])
                print(f"  Greeks fetched: {len(greeks_df)} entries", flush=True)

                # Save
                filename = f"greeks_{name}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                filepath = os.path.join(self.data_dir, filename)
                greeks_df.to_csv(filepath, index=False)
                print(f"  Saved: {filepath}", flush=True)

                return greeks_df
            else:
                print(f"  Greeks API returned: {greeks}", flush=True)
                return None

        except Exception as e:
            print(f"  Greeks API error: {e}", flush=True)
            return None

    # ====================================================================
    # PCR AND OI BUILDUP
    # ====================================================================
    def fetch_pcr(self):
        """Fetch Put-Call Ratio from Angel API."""
        try:
            pcr = self.obj.putCallRatio()
            if pcr and pcr.get('status') and pcr.get('data'):
                pcr_df = pd.DataFrame(pcr['data'])
                print(f"\n  PCR Data ({len(pcr_df)} entries):", flush=True)
                if len(pcr_df) > 0:
                    print(pcr_df.to_string(index=False), flush=True)
                return pcr_df
        except Exception as e:
            print(f"  PCR fetch error: {e}", flush=True)
        return None

    def fetch_oi_buildup(self, expiry_type='NEAR'):
        """Fetch OI Buildup analysis."""
        results = {}
        for dtype in ['Long Built Up', 'Short Built Up', 'Long Unwinding', 'Short Covering']:
            try:
                oi = self.obj.oIBuildup({
                    "expirytype": expiry_type,
                    "datatype": dtype
                })
                if oi and oi.get('status') and oi.get('data'):
                    results[dtype] = pd.DataFrame(oi['data'])
                    print(f"  OI {dtype}: {len(results[dtype])} entries", flush=True)
                time.sleep(0.12)
            except Exception as e:
                print(f"  OI Buildup error ({dtype}): {e}", flush=True)
        return results

    # ====================================================================
    # HISTORICAL CANDLE DATA FOR OPTIONS
    # ====================================================================
    def fetch_option_candles(self, token, exchange='NFO', interval='ONE_DAY',
                             from_date=None, to_date=None):
        """Fetch historical candle data for a specific option token.

        NOTE: Only works for ACTIVE (non-expired) contracts!
        Max lookback: ONE_DAY = 2000 days, ONE_MINUTE = 30 days
        """
        if to_date is None:
            to_date = datetime.now().strftime('%Y-%m-%d 15:30')
        if from_date is None:
            # Default based on interval
            days_back = {'ONE_MINUTE': 30, 'FIVE_MINUTE': 100, 'FIFTEEN_MINUTE': 200,
                        'ONE_HOUR': 400, 'ONE_DAY': 2000}
            db = days_back.get(interval, 30)
            from_date = (datetime.now() - timedelta(days=db)).strftime('%Y-%m-%d 09:15')

        try:
            params = {
                "exchange": exchange,
                "symboltoken": str(token),
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date
            }

            resp = self.obj.getCandleData(params)
            if resp.get('status') and resp.get('data'):
                df = pd.DataFrame(resp['data'],
                                  columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['DateTime'] = pd.to_datetime(df['DateTime'])
                df.set_index('DateTime', inplace=True)
                return df
            else:
                return None

        except Exception as e:
            print(f"  Candle fetch error: {e}", flush=True)
            return None

    # ====================================================================
    # FETCH HISTORICAL INDEX SPOT DATA
    # ====================================================================
    def fetch_index_spot(self, name='NIFTY', interval='ONE_DAY', days_back=2000):
        """Fetch historical spot data for an index."""
        print(f"\n  Fetching {name} spot data ({interval}, {days_back}d)...", flush=True)

        # Index tokens
        index_tokens = {
            'NIFTY': ('NSE', '99926000'),      # Nifty 50
            'BANKNIFTY': ('NSE', '99926009'),   # Nifty Bank
            'SENSEX': ('BSE', '99919000'),      # SENSEX
        }

        if name not in index_tokens:
            print(f"  Unknown index: {name}", flush=True)
            return None

        exchange, token = index_tokens[name]
        from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d 09:15')
        to_date = datetime.now().strftime('%Y-%m-%d 15:30')

        # For ONE_DAY, we can go back 2000 days (~5.5 years)
        # But rate limit is 3 req/sec, and max 8000 candles
        all_data = []

        if interval == 'ONE_DAY':
            # Single request should suffice for daily
            try:
                params = {
                    "exchange": exchange,
                    "symboltoken": token,
                    "interval": interval,
                    "fromdate": from_date,
                    "todate": to_date
                }
                resp = self.obj.getCandleData(params)
                if resp.get('status') and resp.get('data'):
                    df = pd.DataFrame(resp['data'],
                                      columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                    df['DateTime'] = pd.to_datetime(df['DateTime'])
                    df.set_index('DateTime', inplace=True)
                    print(f"  Fetched {len(df)} candles: {df.index[0]} to {df.index[-1]}", flush=True)

                    # Save
                    filename = f"{name}_spot_{interval.lower()}_{days_back}d.csv"
                    filepath = os.path.join(self.data_dir, filename)
                    df.to_csv(filepath)
                    print(f"  Saved: {filepath}", flush=True)

                    return df
                else:
                    print(f"  API returned: {resp.get('message', 'No data')}", flush=True)
            except Exception as e:
                print(f"  Error: {e}", flush=True)
        else:
            # For intraday, need to chunk by date range
            chunk_days = {'ONE_MINUTE': 25, 'FIVE_MINUTE': 90, 'FIFTEEN_MINUTE': 180,
                         'ONE_HOUR': 350}
            chunk = chunk_days.get(interval, 25)

            current_start = datetime.now() - timedelta(days=days_back)
            while current_start < datetime.now():
                chunk_end = min(current_start + timedelta(days=chunk), datetime.now())
                try:
                    params = {
                        "exchange": exchange,
                        "symboltoken": token,
                        "interval": interval,
                        "fromdate": current_start.strftime('%Y-%m-%d 09:15'),
                        "todate": chunk_end.strftime('%Y-%m-%d 15:30')
                    }
                    resp = self.obj.getCandleData(params)
                    if resp.get('status') and resp.get('data'):
                        all_data.extend(resp['data'])
                    time.sleep(0.35)  # Rate limit: 3 req/sec
                except Exception as e:
                    print(f"  Chunk error: {e}", flush=True)
                    time.sleep(1)
                current_start = chunk_end + timedelta(days=1)

            if all_data:
                df = pd.DataFrame(all_data,
                                  columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['DateTime'] = pd.to_datetime(df['DateTime'])
                df = df.drop_duplicates(subset='DateTime')
                df.set_index('DateTime', inplace=True)
                df.sort_index(inplace=True)
                print(f"  Fetched {len(df)} candles: {df.index[0]} to {df.index[-1]}", flush=True)

                filename = f"{name}_spot_{interval.lower()}_{days_back}d.csv"
                filepath = os.path.join(self.data_dir, filename)
                df.to_csv(filepath)
                print(f"  Saved: {filepath}", flush=True)

                return df

        return None

    # ====================================================================
    # COMPREHENSIVE DATA DUMP
    # ====================================================================
    def fetch_all_data(self, indices=None):
        """Fetch comprehensive options data for all indices.

        Returns dict with all data organized by index.
        """
        if indices is None:
            indices = ['NIFTY', 'BANKNIFTY', 'SENSEX']

        print("\n" + "=" * 70, flush=True)
        print("COMPREHENSIVE OPTIONS DATA FETCH - Angel One SmartAPI", flush=True)
        print(f"Indices: {', '.join(indices)}", flush=True)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print("=" * 70, flush=True)

        all_data = {}

        for idx_name in indices:
            print(f"\n{'='*60}", flush=True)
            print(f"  {idx_name}", flush=True)
            print(f"{'='*60}", flush=True)

            data = {}

            # 1. Spot data (daily, max available)
            print(f"\n  [1/5] Fetching spot data...", flush=True)
            data['spot'] = self.fetch_index_spot(idx_name, 'ONE_DAY', 2000)
            time.sleep(0.5)

            # 2. Option chain with OI
            print(f"\n  [2/5] Fetching option chain with OI...", flush=True)
            data['chain'] = self.fetch_option_chain(idx_name, num_strikes=15)
            time.sleep(0.5)

            # 3. Greeks
            print(f"\n  [3/5] Fetching option Greeks...", flush=True)
            data['greeks'] = self.fetch_option_greeks(idx_name)
            time.sleep(0.5)

            # 4. Historical candles for ATM options (nearest expiry)
            print(f"\n  [4/5] Fetching ATM option candles...", flush=True)
            expiry = self.get_nearest_expiry(idx_name)
            atm = self.get_atm_strike(idx_name)
            if expiry and atm:
                exchange = 'BFO' if idx_name == 'SENSEX' else 'NFO'
                for opt_type in ['CE', 'PE']:
                    tokens = self.get_option_tokens(idx_name, expiry, atm, opt_type)
                    if len(tokens) > 0:
                        token = tokens.iloc[0]['token']
                        symbol = tokens.iloc[0]['symbol']
                        candles = self.fetch_option_candles(token, exchange, 'FIVE_MINUTE')
                        if candles is not None:
                            data[f'atm_{opt_type.lower()}_candles'] = candles
                            filename = f"{idx_name}_ATM_{opt_type}_{datetime.now().strftime('%Y%m%d')}.csv"
                            candles.to_csv(os.path.join(self.data_dir, filename))
                            print(f"    ATM {opt_type}: {len(candles)} candles ({symbol})", flush=True)
                        time.sleep(0.35)

            # 5. OI Buildup for nearest expiry
            print(f"\n  [5/5] Fetching OI Buildup...", flush=True)
            data['oi_buildup'] = self.fetch_oi_buildup()

            all_data[idx_name] = data

        # Fetch overall PCR
        print(f"\n  Fetching overall PCR...", flush=True)
        all_data['PCR'] = self.fetch_pcr()

        return all_data

    # ====================================================================
    # SUMMARY REPORT
    # ====================================================================
    def print_data_summary(self, all_data):
        """Print a summary of all fetched data."""
        print("\n" + "=" * 70, flush=True)
        print("DATA FETCH SUMMARY", flush=True)
        print("=" * 70, flush=True)

        for idx_name in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            if idx_name not in all_data:
                continue
            data = all_data[idx_name]
            print(f"\n  {idx_name}:", flush=True)

            if data.get('spot') is not None:
                spot = data['spot']
                print(f"    Spot: {len(spot)} days ({spot.index[0].date()} to {spot.index[-1].date()})", flush=True)
                print(f"    Last Close: {spot['Close'].iloc[-1]:,.2f}", flush=True)

            if data.get('chain') is not None:
                chain = data['chain']
                ce_oi = chain[chain['type']=='CE']['oi'].sum()
                pe_oi = chain[chain['type']=='PE']['oi'].sum()
                pcr = pe_oi / ce_oi if ce_oi > 0 else 0
                print(f"    Chain: {len(chain)} contracts", flush=True)
                print(f"    CE OI: {ce_oi:,.0f} | PE OI: {pe_oi:,.0f} | PCR: {pcr:.2f}", flush=True)

            if data.get('greeks') is not None:
                print(f"    Greeks: {len(data['greeks'])} entries", flush=True)

            for ot in ['ce', 'pe']:
                key = f'atm_{ot}_candles'
                if data.get(key) is not None:
                    print(f"    ATM {ot.upper()} Candles: {len(data[key])} ({data[key].index[0]} to {data[key].index[-1]})", flush=True)

        # PCR
        if all_data.get('PCR') is not None:
            print(f"\n  Market PCR: Available ({len(all_data['PCR'])} entries)", flush=True)

        # List all saved files
        files = os.listdir(self.data_dir)
        print(f"\n  Files saved in {self.data_dir}:", flush=True)
        for f in sorted(files):
            size = os.path.getsize(os.path.join(self.data_dir, f))
            print(f"    {f} ({size/1024:.1f} KB)", flush=True)


# ====================================================================
# MAIN
# ====================================================================
def main():
    print("=" * 70, flush=True)
    print("ANGEL ONE SMARTAPI - OPTIONS DATA FETCHER", flush=True)
    print("Fetching real options data with OI & Greeks", flush=True)
    print("=" * 70, flush=True)

    # Initialize with Historical app
    fetcher = AngelDataFetcher(app_type='Historical')

    # Login
    if not fetcher.login():
        print("\nTrying Market app...", flush=True)
        fetcher = AngelDataFetcher(app_type='Market')
        if not fetcher.login():
            print("\nFATAL: Could not login to Angel One!", flush=True)
            print("Please verify your credentials and TOTP secret.", flush=True)
            return

    try:
        # Download instrument master
        fetcher.download_instruments()

        # Fetch all data
        all_data = fetcher.fetch_all_data(['NIFTY', 'BANKNIFTY', 'SENSEX'])

        # Print summary
        fetcher.print_data_summary(all_data)

    except Exception as e:
        print(f"\nERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        fetcher.logout()

    print("\n" + "=" * 70, flush=True)
    print("Data fetch complete! Files saved in: data/options/", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
