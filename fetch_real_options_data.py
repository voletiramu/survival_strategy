"""
Fetch REAL options data with OI from NSE using multiple libraries.
Tests: jugaad-data, openchart, nsepython
Downloads max available historical options data for Nifty50 and BankNifty.
Computes Greeks (Delta, Gamma, Theta, Vega) using Black-Scholes.
"""

import sys
import os
import time
import warnings
warnings.filterwarnings('ignore')
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "options")
os.makedirs(DATA_DIR, exist_ok=True)

# ====================================================================
# BLACK-SCHOLES GREEKS CALCULATOR
# ====================================================================
from scipy.stats import norm

def black_scholes_greeks(S, K, T, r, sigma, option_type='CE'):
    """
    Compute option Greeks using Black-Scholes model.
    S = spot price, K = strike price, T = time to expiry (years),
    r = risk-free rate, sigma = implied volatility
    """
    if T <= 0:
        T = 1/365  # minimum 1 day
    if sigma <= 0:
        sigma = 0.01

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == 'CE':
        delta = norm.cdf(d1)
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        delta = norm.cdf(d1) - 1
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    theta = -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * (
        norm.cdf(d2) if option_type == 'CE' else norm.cdf(-d2))
    theta = theta / 365  # per day
    vega = S * norm.pdf(d1) * np.sqrt(T) / 100  # per 1% vol change

    return {
        'price': max(price, 0.01),
        'delta': delta,
        'gamma': gamma,
        'theta': theta,
        'vega': vega,
        'iv': sigma
    }


def compute_iv_from_price(S, K, T, r, market_price, option_type='CE', max_iter=100):
    """Compute implied volatility from market price using Newton's method."""
    sigma = 0.3  # initial guess
    for _ in range(max_iter):
        greeks = black_scholes_greeks(S, K, T, r, sigma, option_type)
        diff = greeks['price'] - market_price
        if abs(diff) < 0.01:
            return sigma
        vega_val = greeks['vega'] * 100  # convert back
        if abs(vega_val) < 0.001:
            break
        sigma -= diff / vega_val
        sigma = max(0.01, min(sigma, 5.0))
    return sigma


# ====================================================================
# METHOD 1: jugaad-data (NSE bhavcopy - has OI data)
# ====================================================================
def fetch_via_jugaad(symbol, start_date, end_date, option_type, strike, expiry):
    """Fetch options data via jugaad-data library."""
    try:
        from jugaad_data.nse import stock_df, index_df
        print(f"  [jugaad] Fetching {symbol} {strike}{option_type} exp:{expiry}...", flush=True)
        # jugaad-data provides derivatives bhavcopy with OI
        # Note: This fetches from NSE's bhavcopy archives
        df = stock_df(symbol=symbol, from_date=start_date, to_date=end_date,
                      series="EQ")
        return df
    except Exception as e:
        print(f"  [jugaad] Error: {e}", flush=True)
        return None


def fetch_derivatives_jugaad(symbol, from_date, to_date):
    """Fetch F&O bhavcopy data using jugaad-data."""
    try:
        from jugaad_data.nse import NSELive
        nse = NSELive()
        # Get option chain
        print(f"  [jugaad] Fetching live option chain for {symbol}...", flush=True)
        chain = nse.index_option_chain(symbol)
        return chain
    except Exception as e:
        print(f"  [jugaad] Error fetching derivatives: {e}", flush=True)
        return None


# ====================================================================
# METHOD 2: openchart (NFO historical data)
# ====================================================================
def fetch_via_openchart(symbol_code, start_date, end_date, interval='1d'):
    """Fetch options data via openchart library."""
    try:
        from openchart import NSEData
        nse = NSEData()
        print(f"  [openchart] Fetching {symbol_code}...", flush=True)
        df = nse.historical(symbol_code, 'FO', start_date, end_date, interval)
        if df is not None and len(df) > 0:
            print(f"  [openchart] Got {len(df)} rows", flush=True)
        return df
    except Exception as e:
        print(f"  [openchart] Error: {e}", flush=True)
        return None


def search_openchart_symbols(search_term):
    """Search for option symbols in openchart."""
    try:
        from openchart import NSEData
        nse = NSEData()
        results = nse.search(search_term, 'NFO')
        return results
    except Exception as e:
        print(f"  [openchart] Search error: {e}", flush=True)
        return None


# ====================================================================
# METHOD 3: nsepython (live option chain + history)
# ====================================================================
def fetch_via_nsepython(symbol):
    """Fetch option chain data via nsepython."""
    try:
        from nsepython import option_chain
        print(f"  [nsepython] Fetching option chain for {symbol}...", flush=True)
        data = option_chain(symbol)
        if data:
            records = data.get('records', {})
            chain_data = records.get('data', [])
            print(f"  [nsepython] Got {len(chain_data)} option records", flush=True)
            return data
        return None
    except Exception as e:
        print(f"  [nsepython] Error: {e}", flush=True)
        return None


def fetch_nsepython_history(symbol, start, end):
    """Fetch historical equity data via nsepython."""
    try:
        from nsepython import equity_history
        print(f"  [nsepython] Fetching history for {symbol}...", flush=True)
        df = equity_history(symbol, "EQ", start, end)
        return df
    except Exception as e:
        print(f"  [nsepython] History error: {e}", flush=True)
        return None


# ====================================================================
# METHOD 4: Direct NSE API (most reliable for option chain)
# ====================================================================
def fetch_nse_option_chain_direct(symbol="NIFTY"):
    """Fetch option chain directly from NSE API."""
    import requests
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    print(f"  [NSE Direct] Fetching option chain for {symbol}...", flush=True)
    try:
        session = requests.Session()
        # First get cookies
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        time.sleep(1)
        # Then fetch option chain
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            records = data.get('records', {})
            chain_data = records.get('data', [])
            spot = records.get('underlyingValue', 0)
            expiry_dates = records.get('expiryDates', [])
            print(f"  [NSE Direct] Got {len(chain_data)} records, spot={spot}, "
                  f"expiries={len(expiry_dates)}", flush=True)
            return data
        else:
            print(f"  [NSE Direct] HTTP {resp.status_code}", flush=True)
            return None
    except Exception as e:
        print(f"  [NSE Direct] Error: {e}", flush=True)
        return None


# ====================================================================
# PARSE OPTION CHAIN INTO USABLE DATAFRAME
# ====================================================================
def parse_option_chain(chain_data, spot_price, risk_free_rate=0.065):
    """Parse NSE option chain JSON into DataFrame with computed Greeks."""
    records = chain_data.get('records', {})
    data_list = records.get('data', [])
    expiry_dates = records.get('expiryDates', [])

    rows = []
    for item in data_list:
        expiry = item.get('expiryDate', '')
        strike = item.get('strikePrice', 0)

        # Parse expiry date
        try:
            exp_date = datetime.strptime(expiry, '%d-%b-%Y')
            dte = max((exp_date - datetime.now()).days, 0)
            T = max(dte / 365, 1/365)
        except:
            continue

        for opt_type in ['CE', 'PE']:
            opt_data = item.get(opt_type, {})
            if not opt_data:
                continue

            oi = opt_data.get('openInterest', 0)
            change_oi = opt_data.get('changeinOpenInterest', 0)
            pchange_oi = opt_data.get('pchangeinOpenInterest', 0)
            ltp = opt_data.get('lastPrice', 0)
            volume = opt_data.get('totalTradedVolume', 0)
            iv_raw = opt_data.get('impliedVolatility', 0)
            bid = opt_data.get('bidprice', 0)
            ask = opt_data.get('askPrice', 0)

            # Compute IV if not provided
            if iv_raw > 0:
                iv = iv_raw / 100
            elif ltp > 0:
                iv = compute_iv_from_price(spot_price, strike, T, risk_free_rate,
                                            ltp, opt_type)
            else:
                iv = 0.15

            # Compute Greeks
            greeks = black_scholes_greeks(spot_price, strike, T, risk_free_rate,
                                           iv, opt_type)

            rows.append({
                'expiry': expiry,
                'expiry_date': exp_date,
                'dte': dte,
                'strike': strike,
                'option_type': opt_type,
                'ltp': ltp,
                'bid': bid,
                'ask': ask,
                'volume': volume,
                'oi': oi,
                'change_oi': change_oi,
                'pchange_oi': pchange_oi,
                'iv': iv * 100,
                'bs_price': greeks['price'],
                'delta': greeks['delta'],
                'gamma': greeks['gamma'],
                'theta': greeks['theta'],
                'vega': greeks['vega'],
                'moneyness': (spot_price - strike) / spot_price * 100,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(['expiry_date', 'strike', 'option_type'])
    return df


# ====================================================================
# MAIN: TEST ALL METHODS AND DOWNLOAD DATA
# ====================================================================
def main():
    print("=" * 70, flush=True)
    print("FETCHING REAL NSE OPTIONS DATA WITH OI & GREEKS", flush=True)
    print("=" * 70, flush=True)

    results = {}

    # ------------------------------------------------------------------
    # TEST 1: NSE Direct API (Current Option Chain)
    # ------------------------------------------------------------------
    print("\n--- TEST 1: NSE Direct API ---", flush=True)
    for symbol in ["NIFTY", "BANKNIFTY"]:
        chain = fetch_nse_option_chain_direct(symbol)
        if chain:
            records = chain.get('records', {})
            spot = records.get('underlyingValue', 0)
            df = parse_option_chain(chain, spot)
            if not df.empty:
                filepath = os.path.join(DATA_DIR, f"{symbol}_option_chain_live.csv")
                df.to_csv(filepath, index=False)
                print(f"  Saved {symbol}: {len(df)} options to {filepath}", flush=True)
                print(f"  Spot: {spot}", flush=True)
                print(f"  Expiries: {df['expiry'].nunique()}", flush=True)
                print(f"  Strikes: {df['strike'].nunique()}", flush=True)
                print(f"  Total OI: {df['oi'].sum():,.0f}", flush=True)
                print(f"  Sample Greeks (ATM CE):", flush=True)
                atm = df[(df['option_type']=='CE') &
                         (abs(df['moneyness']) < 1)].head(1)
                if not atm.empty:
                    r = atm.iloc[0]
                    print(f"    Strike={r['strike']}, Delta={r['delta']:.3f}, "
                          f"Gamma={r['gamma']:.5f}, Theta={r['theta']:.2f}, "
                          f"IV={r['iv']:.1f}%, OI={r['oi']:,.0f}", flush=True)
                results[f"{symbol}_live"] = df
            time.sleep(2)

    # ------------------------------------------------------------------
    # TEST 2: OpenChart (Historical NFO data)
    # ------------------------------------------------------------------
    print("\n--- TEST 2: OpenChart (Historical Options) ---", flush=True)
    try:
        from openchart import NSEData
        nse = NSEData()

        # Search for recent options symbols
        for sym in ["NIFTY", "BANKNIFTY"]:
            print(f"\n  Searching {sym} options...", flush=True)
            try:
                search_results = nse.search(sym, 'NFO')
                if search_results is not None:
                    if isinstance(search_results, pd.DataFrame) and not search_results.empty:
                        print(f"  Found {len(search_results)} symbols", flush=True)
                        print(f"  Sample: {search_results.head(5).to_string()}", flush=True)

                        # Try to get historical data for a recent option
                        option_symbols = search_results.head(3)
                        for _, row in option_symbols.iterrows():
                            sym_code = row.iloc[0] if len(row) > 0 else str(row)
                            try:
                                end = datetime.now()
                                start = end - timedelta(days=30)
                                hist = nse.historical(str(sym_code), 'FO', start, end, '1d')
                                if hist is not None and len(hist) > 0:
                                    print(f"  {sym_code}: {len(hist)} bars", flush=True)
                                    print(f"  Columns: {list(hist.columns)}", flush=True)
                                    filepath = os.path.join(DATA_DIR, f"openchart_{sym}_{sym_code[:20]}.csv")
                                    hist.to_csv(filepath)
                                    results[f"openchart_{sym}"] = hist
                                    break
                            except Exception as e2:
                                print(f"  {sym_code} error: {e2}", flush=True)
                    elif isinstance(search_results, list):
                        print(f"  Found {len(search_results)} symbols", flush=True)
                        if search_results:
                            print(f"  Sample: {search_results[:5]}", flush=True)
                else:
                    print(f"  No results for {sym}", flush=True)
            except Exception as e:
                print(f"  Search error: {e}", flush=True)

    except Exception as e:
        print(f"  OpenChart not available: {e}", flush=True)

    # ------------------------------------------------------------------
    # TEST 3: jugaad-data (F&O bhavcopy with OI)
    # ------------------------------------------------------------------
    print("\n--- TEST 3: jugaad-data (Derivatives) ---", flush=True)
    try:
        from jugaad_data.nse import NSELive
        nse_live = NSELive()

        for symbol in ["NIFTY", "BANKNIFTY"]:
            print(f"\n  Fetching {symbol} derivatives via jugaad...", flush=True)
            try:
                oc = nse_live.index_option_chain(symbol)
                if oc:
                    records = oc.get('records', {})
                    data = records.get('data', [])
                    spot = records.get('underlyingValue', 0)
                    print(f"  {symbol}: {len(data)} records, spot={spot}", flush=True)
                    df = parse_option_chain(oc, spot)
                    if not df.empty:
                        filepath = os.path.join(DATA_DIR, f"jugaad_{symbol}_oc.csv")
                        df.to_csv(filepath, index=False)
                        results[f"jugaad_{symbol}"] = df
                        print(f"  Saved to {filepath}", flush=True)
            except Exception as e:
                print(f"  {symbol} error: {e}", flush=True)
            time.sleep(2)
    except Exception as e:
        print(f"  jugaad-data error: {e}", flush=True)

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print("\n" + "=" * 70, flush=True)
    print("DATA AVAILABILITY SUMMARY", flush=True)
    print("=" * 70, flush=True)

    for key, df in results.items():
        print(f"\n  {key}:", flush=True)
        print(f"    Rows: {len(df)}", flush=True)
        print(f"    Columns: {list(df.columns)[:10]}...", flush=True)
        if 'oi' in df.columns:
            print(f"    Total OI: {df['oi'].sum():,.0f}", flush=True)
        if 'gamma' in df.columns:
            print(f"    Gamma range: {df['gamma'].min():.6f} to {df['gamma'].max():.6f}", flush=True)
        if 'delta' in df.columns:
            print(f"    Delta range: {df['delta'].min():.3f} to {df['delta'].max():.3f}", flush=True)

    return results


if __name__ == "__main__":
    main()
