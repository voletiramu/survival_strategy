"""
Fetch REAL NSE options data - V2 with proper session handling.
Uses multiple approaches with fallbacks.
"""

import sys
import os
import time
import json
import warnings
warnings.filterwarnings('ignore')
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from scipy.stats import norm
import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "options")
os.makedirs(DATA_DIR, exist_ok=True)


# ====================================================================
# BLACK-SCHOLES GREEKS
# ====================================================================
def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    if T <= 0: T = 1/365
    if sigma <= 0: sigma = 0.01
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CE':
        delta = norm.cdf(d1)
        price = S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
    else:
        delta = norm.cdf(d1) - 1
        price = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
    gamma = norm.pdf(d1) / (S*sigma*np.sqrt(T))
    theta = (-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T)))/365
    vega = S*norm.pdf(d1)*np.sqrt(T)/100
    return {'price':max(price,0.01),'delta':delta,'gamma':gamma,'theta':theta,'vega':vega}


# ====================================================================
# METHOD 1: NSE with proper session (cookies + headers)
# ====================================================================
def get_nse_session():
    """Create NSE session with proper cookies."""
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://www.nseindia.com/option-chain',
    }
    session.headers.update(headers)
    # Get cookies first
    try:
        r = session.get('https://www.nseindia.com', timeout=10)
        time.sleep(1)
        r = session.get('https://www.nseindia.com/option-chain', timeout=10)
        time.sleep(1)
    except:
        pass
    return session


def fetch_nse_option_chain(symbol="NIFTY"):
    """Fetch option chain from NSE with proper session."""
    session = get_nse_session()
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"  NSE API error: {e}", flush=True)
    return None


# ====================================================================
# METHOD 2: OpenChart with correct segment
# ====================================================================
def fetch_openchart_options():
    """Fetch options data via OpenChart."""
    try:
        from openchart import NSEData
        nse = NSEData()

        results = {}
        for sym in ['NIFTY', 'BANKNIFTY']:
            print(f"\n  [OpenChart] Searching {sym} FO instruments...", flush=True)
            # Search with FO segment
            search = nse.search(sym, 'FO')
            if search is not None:
                if isinstance(search, pd.DataFrame) and not search.empty:
                    print(f"  Found {len(search)} FO instruments", flush=True)
                    # Filter for options (CE/PE in name)
                    options = search[search.iloc[:, 0].str.contains('CE|PE', na=False)]
                    if len(options) > 0:
                        print(f"  Options found: {len(options)}", flush=True)
                        # Get historical data for first few
                        for idx, row in options.head(5).iterrows():
                            sym_code = str(row.iloc[0])
                            try:
                                end = datetime.now()
                                start = end - timedelta(days=30)
                                hist = nse.historical(sym_code, 'FO', start, end, '1d')
                                if hist is not None and len(hist) > 0:
                                    print(f"  {sym_code}: {len(hist)} bars, cols={list(hist.columns)}", flush=True)
                                    results[sym_code] = hist
                                    break
                            except Exception as e:
                                pass
                    else:
                        # Show what we found
                        print(f"  Found instruments: {search.head(5).to_string()}", flush=True)
                        # Try futures
                        for idx, row in search.head(3).iterrows():
                            sym_code = str(row.iloc[0])
                            try:
                                end = datetime.now()
                                start = end - timedelta(days=60)
                                hist = nse.historical(sym_code, 'FO', start, end, '1d')
                                if hist is not None and len(hist) > 0:
                                    print(f"  {sym_code}: {len(hist)} bars", flush=True)
                                    print(f"  Columns: {list(hist.columns)}", flush=True)
                                    print(f"  Sample:\n{hist.head(3)}", flush=True)
                                    results[sym_code] = hist
                                    break
                            except Exception as e:
                                print(f"  {sym_code}: {e}", flush=True)
                elif isinstance(search, list) and search:
                    print(f"  Found {len(search)} items: {search[:5]}", flush=True)
                else:
                    print(f"  No results", flush=True)

        return results
    except Exception as e:
        print(f"  OpenChart error: {e}", flush=True)
        return {}


# ====================================================================
# METHOD 3: nsepython option chain
# ====================================================================
def fetch_nsepython_chain():
    """Fetch option chain via nsepython."""
    results = {}
    try:
        from nsepython import nse_optionchain_scrapper
        for symbol in ['NIFTY', 'BANKNIFTY']:
            print(f"\n  [nsepython] Fetching {symbol} option chain...", flush=True)
            try:
                data = nse_optionchain_scrapper(symbol)
                if data:
                    records = data.get('records', {})
                    chain = records.get('data', [])
                    spot = records.get('underlyingValue', 0)
                    expiries = records.get('expiryDates', [])
                    print(f"  Got {len(chain)} records, spot={spot}, "
                          f"{len(expiries)} expiries", flush=True)
                    results[symbol] = {'data': data, 'spot': spot}
            except Exception as e:
                print(f"  {symbol}: {e}", flush=True)
            time.sleep(3)
    except ImportError as e:
        print(f"  nsepython import error: {e}", flush=True)
    return results


# ====================================================================
# PARSE AND COMPUTE GREEKS
# ====================================================================
def parse_chain_to_df(chain_json, spot, r=0.065):
    """Parse option chain JSON to DataFrame with Greeks."""
    records = chain_json.get('records', {}).get('data', [])
    rows = []
    for item in records:
        expiry = item.get('expiryDate', '')
        strike = item.get('strikePrice', 0)
        try:
            exp_date = datetime.strptime(expiry, '%d-%b-%Y')
            dte = max((exp_date - datetime.now()).days, 0)
            T = max(dte/365, 1/365)
        except:
            continue

        for ot in ['CE', 'PE']:
            od = item.get(ot)
            if not od:
                continue
            oi = od.get('openInterest', 0)
            chg_oi = od.get('changeinOpenInterest', 0)
            ltp = od.get('lastPrice', 0)
            vol = od.get('totalTradedVolume', 0)
            iv_raw = od.get('impliedVolatility', 0)
            iv = iv_raw/100 if iv_raw > 0 else 0.15
            g = bs_greeks(spot, strike, T, r, iv, ot)
            rows.append({
                'expiry': expiry, 'dte': dte, 'strike': strike,
                'type': ot, 'ltp': ltp, 'volume': vol,
                'oi': oi, 'change_oi': chg_oi,
                'iv': iv*100, 'delta': g['delta'], 'gamma': g['gamma'],
                'theta': g['theta'], 'vega': g['vega'], 'bs_price': g['price'],
                'moneyness': (spot - strike)/spot*100,
            })
    return pd.DataFrame(rows)


# ====================================================================
# MAIN
# ====================================================================
def main():
    print("=" * 70, flush=True)
    print("REAL NSE OPTIONS DATA FETCH - WITH OI & GREEKS", flush=True)
    print("=" * 70, flush=True)

    all_data = {}

    # --- Method 1: nsepython ---
    print("\n[1] Trying nsepython...", flush=True)
    nse_data = fetch_nsepython_chain()
    for symbol, info in nse_data.items():
        spot = info['spot']
        df = parse_chain_to_df(info['data'], spot)
        if not df.empty:
            fp = os.path.join(DATA_DIR, f"{symbol}_options_with_greeks.csv")
            df.to_csv(fp, index=False)
            all_data[symbol] = {'df': df, 'spot': spot}
            print(f"\n  === {symbol} OPTIONS DATA ===", flush=True)
            print(f"  Spot: {spot}", flush=True)
            print(f"  Total records: {len(df)}", flush=True)
            print(f"  Expiries: {df['expiry'].nunique()}", flush=True)
            print(f"  Strikes: {df['strike'].nunique()}", flush=True)
            print(f"  Total OI (CE): {df[df['type']=='CE']['oi'].sum():,.0f}", flush=True)
            print(f"  Total OI (PE): {df[df['type']=='PE']['oi'].sum():,.0f}", flush=True)
            print(f"  PCR (OI): {df[df['type']=='PE']['oi'].sum() / max(df[df['type']=='CE']['oi'].sum(),1):.2f}", flush=True)
            # Show ATM options
            nearest = df.loc[(df['type']=='CE') & (df['dte'] <= 7)].copy()
            if not nearest.empty:
                nearest['dist'] = abs(nearest['strike'] - spot)
                atm = nearest.nsmallest(1, 'dist')
                if not atm.empty:
                    a = atm.iloc[0]
                    print(f"\n  ATM CE (nearest weekly expiry):", flush=True)
                    print(f"    Strike: {a['strike']}, LTP: {a['ltp']}, "
                          f"Delta: {a['delta']:.3f}, Gamma: {a['gamma']:.5f}", flush=True)
                    print(f"    Theta: {a['theta']:.2f}, Vega: {a['vega']:.2f}, "
                          f"IV: {a['iv']:.1f}%, OI: {a['oi']:,.0f}", flush=True)

            # Max OI analysis
            print(f"\n  Top 5 CE OI (Resistance levels):", flush=True)
            top_ce = df[(df['type']=='CE') & (df['dte']<=7)].nlargest(5, 'oi')[['strike','oi','change_oi','iv','delta']]
            print(f"  {top_ce.to_string(index=False)}", flush=True)
            print(f"\n  Top 5 PE OI (Support levels):", flush=True)
            top_pe = df[(df['type']=='PE') & (df['dte']<=7)].nlargest(5, 'oi')[['strike','oi','change_oi','iv','delta']]
            print(f"  {top_pe.to_string(index=False)}", flush=True)

    # --- Method 2: OpenChart ---
    if not all_data:
        print("\n[2] Trying OpenChart...", flush=True)
        oc_data = fetch_openchart_options()
        for key, df in oc_data.items():
            fp = os.path.join(DATA_DIR, f"openchart_{key}.csv")
            df.to_csv(fp)
            print(f"  Saved {key}: {len(df)} rows", flush=True)

    # --- Method 3: Direct NSE API ---
    if not all_data:
        print("\n[3] Trying Direct NSE API...", flush=True)
        for sym in ['NIFTY', 'BANKNIFTY']:
            chain = fetch_nse_option_chain(sym)
            if chain:
                records = chain.get('records', {})
                spot = records.get('underlyingValue', 0)
                df = parse_chain_to_df(chain, spot)
                if not df.empty:
                    fp = os.path.join(DATA_DIR, f"{sym}_nse_direct.csv")
                    df.to_csv(fp, index=False)
                    all_data[sym] = {'df': df, 'spot': spot}
                    print(f"  {sym}: {len(df)} options, spot={spot}", flush=True)

    # === SUMMARY ===
    print("\n" + "=" * 70, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 70, flush=True)

    if all_data:
        print(f"\n  Successfully fetched LIVE options data for: {list(all_data.keys())}", flush=True)
        print(f"\n  Data includes: OI, Change in OI, IV, Volume, LTP", flush=True)
        print(f"  Computed Greeks: Delta, Gamma, Theta, Vega", flush=True)
        print(f"\n  IMPORTANT NOTES on historical options data availability:", flush=True)
        print(f"  - NSE provides LIVE option chain data freely (what we fetched above)", flush=True)
        print(f"  - Historical OI snapshots are NOT freely available via public APIs", flush=True)
        print(f"  - For historical backtesting with real OI, you need:", flush=True)
        print(f"    1. Paid data: Quantsapp, SensiBull, TrueData, Global Datafeeds", flush=True)
        print(f"    2. Build your own: Store daily snapshots via cron job + nsepython", flush=True)
        print(f"    3. NSE bhav copies: Daily EOD data (free, no intraday OI)", flush=True)
        print(f"\n  For backtesting, we will use:", flush=True)
        print(f"  - Spot OHLCV (5 years, already downloaded)", flush=True)
        print(f"  - TODAY'S live OI/Greeks as calibration for option premium estimation", flush=True)
        print(f"  - Black-Scholes model with historical IV to compute realistic premiums", flush=True)
    else:
        print(f"\n  Could not fetch live data (possible network/firewall issue).", flush=True)
        print(f"  Will proceed with synthetic Greeks computation.", flush=True)

    return all_data


if __name__ == "__main__":
    main()
