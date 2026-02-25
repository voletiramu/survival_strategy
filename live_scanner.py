"""
LIVE MARKET SCANNER - Real-Time OI, Greeks, IV Logger
=====================================================
Scans NIFTY, BANKNIFTY, SENSEX + MCX commodity option chains.
Captures real OI, Delta, Gamma, Theta, Vega, IV from Angel One API.

TWO LOG FILES ONLY (single file each, appended per scan):
  1. logs/live_data_log.csv   - Raw option chain: OI, Greeks, IV, LTP per strike per minute
  2. logs/trading_log.csv     - Strategy signals + decisions

Commodity trades start ONLY after 14:30 IST.

Usage:
    python live_scanner.py                # Live scan every 1 min (equity 9:15-15:30, MCX after 14:30)
    python live_scanner.py --once         # Single scan now
    python live_scanner.py --status       # Show latest captured data
"""

import sys
import os
import time
import signal as sig_module
import logging
import argparse
from datetime import datetime, time as dtime
import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# === TWO LOG FILES ONLY ===
LIVE_DATA_LOG = os.path.join(LOG_DIR, 'live_data_log.csv')
TRADING_LOG = os.path.join(LOG_DIR, 'trading_log.csv')

ANGEL_CRED_FILE = os.environ.get('ANGEL_CRED_FILE', r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt")

# Market hours
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 30)
MCX_OPEN = dtime(9, 0)
MCX_CLOSE = dtime(23, 30)
COMMODITY_TRADE_START = dtime(15, 30)   # Commodity trades only after 3:30 PM (equity close)

RISK_FREE_RATE = 0.065

INDEX_TOKENS = {
    'NIFTY':     {'exchange': 'NSE', 'token': '99926000', 'strike_int': 50,  'lot': 65},   # NSE revised Jan 2026
    'BANKNIFTY': {'exchange': 'NSE', 'token': '99926009', 'strike_int': 100, 'lot': 30},  # NSE revised Jan 2026
    'SENSEX':    {'exchange': 'BSE', 'token': '99919000', 'strike_int': 100, 'lot': 20},  # NSE revised Jan 2026
}

MCX_COMMODITIES = {
    'GOLDM':      {'exchange': 'MCX', 'strike_int': 100,  'lot': 1, 'mult': 10},
    'SILVERM':    {'exchange': 'MCX', 'strike_int': 500,  'lot': 1, 'mult': 5},
    'CRUDEOILM':  {'exchange': 'MCX', 'strike_int': 50,   'lot': 1, 'mult': 10},
}

STRIKES_AROUND_ATM = 5  # 5 above + 5 below ATM

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('LiveScan')


def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes Greeks for comparison."""
    T = max(T, 1e-6); sigma = max(sigma, 0.01)
    d1 = (np.log(max(S,0.01)/max(K,0.01)) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CE':
        price = S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
        delta = -norm.cdf(-d1)
    gamma = norm.pdf(d1) / (S*sigma*np.sqrt(T))
    theta = -(S*sigma*norm.pdf(d1))/(2*np.sqrt(T))/365
    vega = S*np.sqrt(T)*norm.pdf(d1) / 100
    return {'price': max(price, 0.01), 'delta': round(delta, 4),
            'gamma': round(gamma, 6), 'theta': round(theta, 2), 'vega': round(vega, 4)}


class LiveScanner:

    def __init__(self):
        self.obj = None
        self._connected = False
        self.instruments = None
        self.historical = {}
        self._running = False
        self._scan_count = 0
        # Cache: token lookup tables built once
        self._nfo_tokens = {}   # (symbol, expiry, strike, CE/PE) -> token
        self._bfo_tokens = {}
        self._mcx_tokens = {}
        self._mcx_fut_tokens = {}  # commodity -> nearest futures token

    # ----------------------------------------------------------------
    # CONNECTION
    # ----------------------------------------------------------------
    def connect(self):
        try:
            from SmartApi import SmartConnect
            import pyotp
        except ImportError:
            os.system("pip install smartapi-python pyotp logzero websocket-client")
            from SmartApi import SmartConnect
            import pyotp

        creds = {}; current_app = {}
        with open(ANGEL_CRED_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip(); raw_val = val.strip()
                    if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                        parts = raw_val.split('#')
                        val = parts[-1].strip() if 'your_' in parts[0].strip().lower() else parts[0].strip()
                    else:
                        val = raw_val.split('#')[0].strip()
                    current_app[key] = val
                    if key == 'BROKER_NAME':
                        creds[current_app.get('ANGEL_APP_TYPE', 'Unknown')] = current_app.copy()
                        current_app = {}

        app = creds.get('Market', creds.get('Historical', {}))
        self.obj = SmartConnect(api_key=app['ANGEL_API_KEY'])
        totp = pyotp.TOTP(app['ANGEL_TOTP_KEY']).now()
        session = self.obj.generateSession(app['ANGEL_CLIENT_CODE'], app['ANGEL_PIN'], totp)

        if session and session.get('status'):
            self._connected = True
            logger.info(f"Angel API connected: {app.get('ANGEL_APP_NAME','')}")
            self._load_instruments()
            return True
        logger.error(f"Login failed: {session}")
        return False

    def _load_instruments(self):
        cache = os.path.join(DATA_DIR, 'instrument_master.csv')
        if not os.path.exists(cache):
            return
        self.instruments = pd.read_csv(cache, low_memory=False)
        logger.info(f"Instruments: {len(self.instruments)}")

        # Build fast token lookup for NFO/BFO
        for _, row in self.instruments.iterrows():
            seg = row.get('exch_seg', '')
            itype = row.get('instrumenttype', '')
            if itype == 'OPTIDX' and seg in ('NFO', 'BFO'):
                sym = row['symbol']
                name = row['name']
                strike = float(row['strike'])
                expiry = row['expiry']
                token = str(int(row['token']))
                # Determine CE/PE from symbol suffix
                opt_type = 'CE' if sym.endswith('CE') else 'PE' if sym.endswith('PE') else ''
                if opt_type:
                    key = (name, expiry, strike, opt_type)
                    if seg == 'NFO':
                        self._nfo_tokens[key] = token
                    else:
                        self._bfo_tokens[key] = token

            # MCX futures for spot proxy
            if itype == 'FUTCOM' and seg == 'MCX':
                name = row['name']
                if name in MCX_COMMODITIES:
                    expiry = row.get('expiry', '')
                    token = str(int(row['token']))
                    if name not in self._mcx_fut_tokens:
                        self._mcx_fut_tokens[name] = []
                    self._mcx_fut_tokens[name].append({'token': token, 'expiry': expiry})

            # MCX option tokens
            if itype == 'OPTFUT' and seg == 'MCX':
                name = row['name']
                if name in MCX_COMMODITIES:
                    strike = float(row['strike'])
                    expiry = row['expiry']
                    token = str(int(row['token']))
                    sym = row['symbol']
                    opt_type = 'CE' if sym.endswith('CE') else 'PE' if sym.endswith('PE') else ''
                    if opt_type:
                        key = (name, expiry, strike, opt_type)
                        self._mcx_tokens[key] = token

        # Sort MCX futures by expiry, pick nearest
        today = datetime.now().strftime('%Y-%m-%d')
        for comm, futs in self._mcx_fut_tokens.items():
            futs.sort(key=lambda x: x['expiry'])
            nearest = [f for f in futs if f['expiry'] >= today]
            if nearest:
                self._mcx_fut_tokens[comm] = nearest[0]
            elif futs:
                self._mcx_fut_tokens[comm] = futs[-1]

        logger.info(f"Token maps: NFO={len(self._nfo_tokens)} BFO={len(self._bfo_tokens)} MCX_OPT={len(self._mcx_tokens)}")

    def load_historical(self):
        for symbol in list(INDEX_TOKENS) + list(MCX_COMMODITIES):
            fname = f"{symbol}_spot_one_day_2000d.csv"
            fpath = os.path.join(DATA_DIR, fname)
            if os.path.exists(fpath):
                df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                df.index = df.index.normalize()
                self.historical[symbol] = df

    # ----------------------------------------------------------------
    # DATA FETCHING
    # ----------------------------------------------------------------
    def get_spot(self, symbol):
        """Get live spot/futures price."""
        if symbol in INDEX_TOKENS:
            info = INDEX_TOKENS[symbol]
            try:
                data = self.obj.ltpData(info['exchange'], info['token'], info['token'])
                if data and data.get('data'):
                    return data['data'].get('ltp')
            except:
                pass
        elif symbol in MCX_COMMODITIES:
            fut = self._mcx_fut_tokens.get(symbol)
            if fut:
                try:
                    data = self.obj.ltpData('MCX', fut['token'], fut['token'])
                    if data and data.get('data'):
                        return data['data'].get('ltp')
                except:
                    pass
        return None

    def get_option_greeks_api(self, symbol, expiry_date):
        """Fetch Greeks from optionGreek API (all strikes in one call)."""
        try:
            # Parse expiry in various formats
            try:
                exp_dt = datetime.strptime(expiry_date, '%Y-%m-%d')
            except ValueError:
                exp_dt = datetime.strptime(expiry_date.upper(), '%d%b%Y')
            exp_str = exp_dt.strftime('%d%b%Y').upper()
            data = self.obj.optionGreek({"name": symbol, "expirydate": exp_str})
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.warning(f"  optionGreek({symbol}): {e}")
        return None

    def get_market_data(self, exchange, token):
        """Get full market data (OI, LTP, volume) for one token."""
        try:
            data = self.obj.getMarketData("FULL", {exchange: [str(token)]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
        except:
            pass
        return None

    def get_nearest_expiry(self, symbol):
        """Find nearest expiry from instrument master."""
        if self.instruments is None:
            return None
        if symbol in INDEX_TOKENS:
            seg = 'BFO' if symbol == 'SENSEX' else 'NFO'
            itype = 'OPTIDX'
        else:
            seg = 'MCX'
            itype = 'OPTFUT'

        opts = self.instruments[
            (self.instruments['name'] == symbol) &
            (self.instruments['exch_seg'] == seg) &
            (self.instruments['instrumenttype'] == itype)
        ]
        if len(opts) == 0:
            return None
        today = datetime.now()
        raw_expiries = opts['expiry'].dropna().unique()
        # Parse expiries in various formats
        parsed = []
        for e in raw_expiries:
            try:
                dt = datetime.strptime(str(e), '%Y-%m-%d')
                parsed.append((dt, str(e)))
            except ValueError:
                try:
                    dt = datetime.strptime(str(e).upper(), '%d%b%Y')
                    parsed.append((dt, str(e)))
                except ValueError:
                    continue
        # Filter future expiries and sort
        future = [(dt, raw) for dt, raw in parsed if dt >= today.replace(hour=0, minute=0, second=0)]
        if not future:
            return None
        future.sort(key=lambda x: x[0])
        return future[0][1]  # Return original string format

    def find_token(self, symbol, expiry, strike, opt_type):
        """Fast token lookup from cached maps."""
        key = (symbol, expiry, float(strike), opt_type)
        if symbol == 'SENSEX':
            return self._bfo_tokens.get(key)
        elif symbol in INDEX_TOKENS:
            return self._nfo_tokens.get(key)
        else:
            return self._mcx_tokens.get(key)

    # ----------------------------------------------------------------
    # INDICATORS
    # ----------------------------------------------------------------
    def compute_indicators(self, symbol):
        df = self.historical.get(symbol)
        if df is None or len(df) < 20:
            return None
        atr = (df['High'].tail(14) - df['Low'].tail(14)).mean()
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        hv = log_ret.tail(20).std() * np.sqrt(252)

        prev = df.iloc[-1]
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_w = abs(tc - bc) / prev['Close'] * 100
        h_range = prev['High'] - prev['Low']

        return {
            'atr': atr, 'hv': hv,
            'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_w,
            'cam_r3': prev['Close'] + h_range*1.1/4,
            'cam_s3': prev['Close'] - h_range*1.1/4,
            'demand': df['Low'].tail(10).min(),
            'supply': df['High'].tail(10).max(),
            'prev_h': prev['High'], 'prev_l': prev['Low'], 'prev_c': prev['Close'],
        }

    # ----------------------------------------------------------------
    # SCANNING
    # ----------------------------------------------------------------
    def scan_once(self):
        self._scan_count += 1
        now = datetime.now()
        ts = now.strftime('%Y-%m-%d %H:%M:%S')
        current_time = now.time()

        logger.info(f"\n{'='*80}")
        logger.info(f"SCAN #{self._scan_count} | {ts}")
        logger.info(f"{'='*80}")

        live_rows = []
        signal_rows = []

        # --- EQUITY INDICES ---
        equity_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and now.weekday() <= 4
        if equity_open:
            for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
                rows, sigs = self._scan_symbol(symbol, ts, is_commodity=False)
                live_rows.extend(rows)
                signal_rows.extend(sigs)
        else:
            logger.info(f"  Equity CLOSED ({current_time.strftime('%H:%M')}). Hours: 09:15-15:30")

        # --- MCX COMMODITIES (trade only after 14:30) ---
        mcx_open = MCX_OPEN <= current_time <= MCX_CLOSE and now.weekday() <= 4
        commodity_trade_ok = current_time >= COMMODITY_TRADE_START
        if mcx_open:
            for comm in ['GOLDM', 'SILVERM', 'CRUDEOILM']:
                rows, sigs = self._scan_symbol(comm, ts, is_commodity=True,
                                                can_trade=commodity_trade_ok)
                live_rows.extend(rows)
                signal_rows.extend(sigs)
        else:
            logger.info(f"  MCX CLOSED ({current_time.strftime('%H:%M')}). Hours: 09:00-23:30")

        # --- WRITE TO LOG FILES ---
        if live_rows:
            df = pd.DataFrame(live_rows)
            hdr = not os.path.exists(LIVE_DATA_LOG) or os.path.getsize(LIVE_DATA_LOG) == 0
            df.to_csv(LIVE_DATA_LOG, mode='a', header=hdr, index=False)
            logger.info(f"\n  >> {len(df)} rows -> live_data_log.csv")

        if signal_rows:
            df = pd.DataFrame(signal_rows)
            hdr = not os.path.exists(TRADING_LOG) or os.path.getsize(TRADING_LOG) == 0
            df.to_csv(TRADING_LOG, mode='a', header=hdr, index=False)
            logger.info(f"  >> {len(df)} signals -> trading_log.csv")

        return len(live_rows), len(signal_rows)

    def _scan_symbol(self, symbol, scan_ts, is_commodity=False, can_trade=True):
        """Scan one symbol: fetch spot, Greeks, OI, log everything."""
        rows = []
        signals = []
        is_mcx = symbol in MCX_COMMODITIES

        logger.info(f"\n--- {symbol} {'(MCX)' if is_mcx else ''} ---")

        # 1. Spot
        spot = self.get_spot(symbol)
        if not spot:
            logger.warning(f"  No spot data")
            return rows, signals
        logger.info(f"  Spot: {spot:,.2f}")

        # 2. Expiry
        expiry = self.get_nearest_expiry(symbol)
        if not expiry:
            logger.warning(f"  No expiry found")
            return rows, signals
        # Parse expiry — handle multiple formats (YYYY-MM-DD, DDMMMYYYY, etc.)
        try:
            exp_dt = datetime.strptime(expiry, '%Y-%m-%d')
        except ValueError:
            try:
                exp_dt = datetime.strptime(expiry, '%d%b%Y')
            except ValueError:
                try:
                    exp_dt = datetime.strptime(expiry.upper(), '%d%b%Y')
                except ValueError:
                    logger.warning(f"  Cannot parse expiry: {expiry}")
                    return rows, signals
        dte = max(0, (exp_dt - datetime.now()).days)
        logger.info(f"  Expiry: {expiry} (DTE={dte})")

        # 3. Indicators
        ind = self.compute_indicators(symbol)
        if ind:
            logger.info(f"  CPR: {ind['cpr_width']:.3f}% | Pivot: {ind['pivot']:,.0f} | "
                        f"TC: {ind['tc']:,.0f} | BC: {ind['bc']:,.0f} | ATR: {ind['atr']:,.0f}")

        # 4. API Greeks (all strikes, one call)
        greeks_map = {}  # (strike, opt_type) -> greeks dict
        api_greeks = self.get_option_greeks_api(symbol, expiry)
        if api_greeks:
            for g in api_greeks:
                k = (float(g.get('strikePrice', 0)), g.get('optionType', ''))
                greeks_map[k] = {
                    'delta': float(g.get('delta', 0)),
                    'gamma': float(g.get('gamma', 0)),
                    'theta': float(g.get('theta', 0)),
                    'vega': float(g.get('vega', 0)),
                    'iv': float(g.get('impliedVolatility', 0)),
                }
            logger.info(f"  API Greeks: {len(greeks_map)} entries")
        else:
            logger.info(f"  API Greeks: unavailable, using BS model")

        # 5. Strike range
        if is_mcx:
            strike_int = MCX_COMMODITIES[symbol]['strike_int']
            exchange = 'MCX'
        else:
            strike_int = INDEX_TOKENS[symbol]['strike_int']
            exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'

        atm = round(spot / strike_int) * strike_int
        strikes = [atm + i * strike_int for i in range(-STRIKES_AROUND_ATM, STRIKES_AROUND_ATM + 1)]

        # 6. Fetch OI + LTP for each strike (market data API)
        for strike in strikes:
            for ot in ['CE', 'PE']:
                row = {
                    'scan_time': scan_ts, 'scan_no': self._scan_count,
                    'market': 'MCX' if is_mcx else 'EQUITY',
                    'symbol': symbol, 'expiry': expiry,
                    'strike': strike, 'opt_type': ot,
                    'spot': spot, 'dte': dte,
                    'atm_dist': strike - atm,
                }

                # API Greeks
                gk = greeks_map.get((float(strike), ot))
                if gk:
                    row['delta'] = gk['delta']
                    row['gamma'] = gk['gamma']
                    row['theta'] = gk['theta']
                    row['vega'] = gk['vega']
                    row['iv'] = gk['iv']

                # OI + LTP from market data (limit to ATM +/- 3 to save rate)
                if abs(strike - atm) <= 3 * strike_int:
                    token = self.find_token(symbol, expiry, strike, ot)
                    if token:
                        mkt = self.get_market_data(exchange, token)
                        if mkt:
                            row['ltp'] = mkt.get('ltp', 0)
                            row['oi'] = mkt.get('opnInterest', 0)
                            row['oi_chg'] = mkt.get('opnInterestChange', 0)
                            row['volume'] = mkt.get('tradeVolume', 0)
                            row['open'] = mkt.get('open', 0)
                            row['high'] = mkt.get('high', 0)
                            row['low'] = mkt.get('low', 0)
                        time.sleep(0.12)  # Rate limit: ~8 req/sec

                # BS model for comparison / fallback
                T = max(dte, 0.5) / 365
                iv_val = row.get('iv', ind['hv'] * 100 * 1.15 if ind else 18)
                bs = bs_greeks(spot, strike, T, RISK_FREE_RATE, max(iv_val, 5) / 100, ot)
                row['premium_bs'] = bs['price']
                if 'delta' not in row:
                    row['delta'] = bs['delta']
                    row['gamma'] = bs['gamma']
                    row['theta'] = bs['theta']
                    row['vega'] = bs['vega']
                    row['iv'] = round(iv_val, 2)

                # Add CPR context
                if ind:
                    row['cpr_width'] = round(ind['cpr_width'], 3)
                    row['pivot'] = round(ind['pivot'], 2)

                rows.append(row)

        # 7. Strategy signals
        if ind and can_trade:
            sigs = self._check_signals(symbol, spot, ind, greeks_map, dte, is_mcx, scan_ts)
            signals.extend(sigs)
        elif ind and not can_trade and is_commodity:
            logger.info(f"  Commodity signals BLOCKED until {COMMODITY_TRADE_START} "
                        f"(current: {datetime.now().strftime('%H:%M')})")

        # 8. Print ATM snapshot
        atm_ce = next((r for r in rows if r['strike'] == atm and r['opt_type'] == 'CE'), None)
        atm_pe = next((r for r in rows if r['strike'] == atm and r['opt_type'] == 'PE'), None)
        if atm_ce:
            logger.info(f"  ATM {atm} CE: LTP={atm_ce.get('ltp','?'):>8} OI={atm_ce.get('oi','?'):>8} "
                        f"IV={atm_ce.get('iv',0):>5.1f}% D={atm_ce.get('delta',0):>6.3f} "
                        f"G={atm_ce.get('gamma',0):.5f} T={atm_ce.get('theta',0):>7.2f}")
        if atm_pe:
            logger.info(f"  ATM {atm} PE: LTP={atm_pe.get('ltp','?'):>8} OI={atm_pe.get('oi','?'):>8} "
                        f"IV={atm_pe.get('iv',0):>5.1f}% D={atm_pe.get('delta',0):>6.3f} "
                        f"G={atm_pe.get('gamma',0):.5f} T={atm_pe.get('theta',0):>7.2f}")

        return rows, signals

    def _check_signals(self, symbol, spot, ind, greeks_map, dte, is_mcx, scan_ts):
        """Check CPR, Ghost Zone, Gamma Blast signals."""
        signals = []
        dow = datetime.now().weekday()
        si = MCX_COMMODITIES[symbol]['strike_int'] if is_mcx else INDEX_TOKENS[symbol]['strike_int']

        # --- CPR ---
        narrow = 0.4 if is_mcx else 0.3
        if ind['cpr_width'] < narrow:
            if spot > ind['tc']:
                strike = round(spot / si) * si
                g = greeks_map.get((float(strike), 'CE'), {})
                ltp = g.get('ltp', 0) if 'ltp' in g else 0
                signals.append(self._signal_row(scan_ts, 'CPR', symbol, 'BUY_CE', strike,
                    ltp, g, spot, is_mcx,
                    f"Narrow CPR ({ind['cpr_width']:.3f}%) bullish > TC={ind['tc']:.0f}"))
            elif spot < ind['bc']:
                strike = round(spot / si) * si
                g = greeks_map.get((float(strike), 'PE'), {})
                ltp = g.get('ltp', 0) if 'ltp' in g else 0
                signals.append(self._signal_row(scan_ts, 'CPR', symbol, 'BUY_PE', strike,
                    ltp, g, spot, is_mcx,
                    f"Narrow CPR ({ind['cpr_width']:.3f}%) bearish < BC={ind['bc']:.0f}"))

        # --- GHOST ZONE ---
        bounce = abs(spot - ind['demand']) / max(ind['atr'], 1)
        if bounce < 0.2:
            strike = round(spot / si) * si
            g = greeks_map.get((float(strike), 'CE'), {})
            signals.append(self._signal_row(scan_ts, 'Ghost Zone', symbol, 'BUY_CE', strike,
                0, g, spot, is_mcx,
                f"Demand zone retest {ind['demand']:.0f} bounce={bounce:.2f}x"))

        # --- GAMMA BLAST (expiry day) ---
        if not is_mcx:
            is_exp = (symbol == 'BANKNIFTY' and dow == 2) or (symbol != 'BANKNIFTY' and dow == 3)
            if is_exp and dte <= 1:
                strike = round(spot / si) * si
                g = greeks_map.get((float(strike), 'CE'), {})
                if g.get('gamma', 0) > 0.001:
                    signals.append(self._signal_row(scan_ts, 'Gamma Blast', symbol, 'BUY_CE', strike,
                        0, g, spot, is_mcx,
                        f"Expiry day gamma={g.get('gamma',0):.4f}"))

        for s in signals:
            logger.info(f"  >> SIGNAL [{s['strategy']}] {s['signal_type']} {s['symbol']} "
                        f"@{s['strike']} | {s['reason']}")
        return signals

    def _signal_row(self, ts, strategy, symbol, sig_type, strike, premium, greeks, spot, is_mcx, reason):
        return {
            'scan_time': ts, 'scan_no': self._scan_count,
            'market': 'MCX' if is_mcx else 'EQUITY',
            'strategy': strategy, 'symbol': symbol,
            'signal_type': sig_type, 'strike': strike,
            'premium': premium, 'spot': spot,
            'delta': greeks.get('delta', 0),
            'gamma': greeks.get('gamma', 0),
            'theta': greeks.get('theta', 0),
            'iv': greeks.get('iv', 0),
            'reason': reason,
        }

    # ----------------------------------------------------------------
    # CONTINUOUS MODE
    # ----------------------------------------------------------------
    def run_continuous(self, interval_min=1):
        self._running = True
        sig_module.signal(sig_module.SIGINT, lambda s, f: setattr(self, '_running', False))

        logger.info(f"\nLIVE SCANNER | Every {interval_min} min")
        logger.info(f"Equity: {EQUITY_OPEN}-{EQUITY_CLOSE} | MCX: {MCX_OPEN}-{MCX_CLOSE}")
        logger.info(f"Commodity trades: after {COMMODITY_TRADE_START} only")
        logger.info(f"Data -> {LIVE_DATA_LOG}")
        logger.info(f"Signals -> {TRADING_LOG}")
        logger.info(f"Ctrl+C to stop\n")

        while self._running:
            now = datetime.now()
            if now.weekday() > 4:
                logger.info("Weekend. Sleeping 5 min...")
                time.sleep(300)
                continue

            ct = now.time()
            any_open = (EQUITY_OPEN <= ct <= EQUITY_CLOSE) or (MCX_OPEN <= ct <= MCX_CLOSE)

            if not any_open:
                if ct < MCX_OPEN:
                    wait = (datetime.combine(now.date(), MCX_OPEN) - now).total_seconds()
                    logger.info(f"All markets closed. MCX opens in {wait/60:.0f} min")
                    time.sleep(min(wait, 60))
                    continue
                else:
                    logger.info("All markets closed for today.")
                    break

            self.scan_once()
            logger.info(f"  Next scan in {interval_min} min...")
            time.sleep(interval_min * 60)

    # ----------------------------------------------------------------
    # STATUS
    # ----------------------------------------------------------------
    def show_latest(self):
        print("\n" + "=" * 100)
        print("  LATEST LIVE DATA SNAPSHOT")
        print("=" * 100)

        if not os.path.exists(LIVE_DATA_LOG):
            print("  No data yet. Run: python live_scanner.py --once")
            return

        df = pd.read_csv(LIVE_DATA_LOG)
        latest_no = df['scan_no'].max()
        latest = df[df['scan_no'] == latest_no]
        print(f"\n  Scan #{latest_no} at {latest['scan_time'].iloc[0]} | {len(latest)} rows")

        for sym in latest['symbol'].unique():
            sd = latest[latest['symbol'] == sym]
            spot = sd['spot'].iloc[0]
            mkt = sd['market'].iloc[0]
            print(f"\n  {sym} ({mkt}) | Spot: {spot:,.2f}")
            print(f"  {'Strike':>8} {'Type':>3} {'LTP':>9} {'OI':>10} {'OI Chg':>8} "
                  f"{'IV%':>6} {'Delta':>7} {'Gamma':>9} {'Theta':>8} {'Vol':>8}")
            print(f"  {'-'*90}")
            for _, r in sd.iterrows():
                def fmt(v, f='>8'):
                    return format(v, f) if pd.notna(v) and v != '' else '     -'
                ltp = f"{r.get('ltp', 0):>9.2f}" if pd.notna(r.get('ltp')) else '        -'
                oi = f"{r.get('oi', 0):>10.0f}" if pd.notna(r.get('oi')) else '         -'
                oic = f"{r.get('oi_chg', 0):>8.0f}" if pd.notna(r.get('oi_chg')) else '       -'
                iv = f"{r.get('iv', 0):>6.1f}" if pd.notna(r.get('iv')) else '     -'
                d = f"{r.get('delta', 0):>7.3f}" if pd.notna(r.get('delta')) else '      -'
                g = f"{r.get('gamma', 0):>9.6f}" if pd.notna(r.get('gamma')) else '        -'
                t = f"{r.get('theta', 0):>8.2f}" if pd.notna(r.get('theta')) else '       -'
                v = f"{r.get('volume', 0):>8.0f}" if pd.notna(r.get('volume')) else '       -'
                print(f"  {r['strike']:>8.0f} {r['opt_type']:>3} {ltp} {oi} {oic} {iv} {d} {g} {t} {v}")

        if os.path.exists(TRADING_LOG):
            tdf = pd.read_csv(TRADING_LOG)
            print(f"\n  TRADING LOG: {len(tdf)} signals total")
            if len(tdf) > 0:
                for _, r in tdf.tail(10).iterrows():
                    print(f"    {r['scan_time']} [{r['strategy']:12s}] {r['symbol']:10s} "
                          f"{r['signal_type']:8s} @{r['strike']:>8,.0f} | {r['reason']}")

        print("=" * 100)


def main():
    parser = argparse.ArgumentParser(description='Live Market Scanner')
    parser.add_argument('--once', action='store_true', help='Single scan')
    parser.add_argument('--interval', type=int, default=1, help='Interval (minutes)')
    parser.add_argument('--status', action='store_true', help='Show latest data')
    args = parser.parse_args()

    scanner = LiveScanner()

    if args.status:
        scanner.show_latest()
        return

    scanner.load_historical()
    if not scanner.connect():
        return

    if args.once:
        scanner.scan_once()
    else:
        scanner.run_continuous(args.interval)


if __name__ == '__main__':
    main()
