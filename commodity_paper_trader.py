"""
COMMODITY PAPER TRADING SYSTEM
================================
Paper trading for MCX commodity options using Angel One SmartAPI
Adapted CPR, Gamma Blast, Ghost Zone strategies for commodities
Uses Black-76 model (options on futures)

Focus: Gold Mini, Silver Mini, Crude Oil Mini (affordable with Rs 3L)

Trading Hours: MCX 9:00 AM - 11:30 PM (extended vs equity 9:15-3:30)

Usage:
    python commodity_paper_trader.py              # Live continuous
    python commodity_paper_trader.py --once       # Single scan
    python commodity_paper_trader.py --offline    # Offline test
    python commodity_paper_trader.py --status     # Show positions
    python commodity_paper_trader.py --reset      # Reset portfolio
"""

import sys
import os
import json
import time
import signal
import logging
from datetime import datetime, timedelta, time as dtime
import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ====================================================================
# CONFIGURATION
# ====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_commodity')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

for d in [PAPER_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

INITIAL_CAPITAL = 300000
RISK_FREE_RATE = 0.065

# Focus on MINI contracts (affordable with Rs 3L capital)
COMMODITIES = {
    'GOLDM': {
        'lot_size': 100, 'multiplier': 10, 'margin': 15000,
        'strike_interval': 100, 'vol_adj': 1.0,
        'file': 'GOLDM_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Gold Mini (100g)',
    },
    'SILVERM': {
        'lot_size': 5, 'multiplier': 5, 'margin': 15000,
        'strike_interval': 500, 'vol_adj': 1.15,
        'file': 'SILVERM_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Silver Mini (5kg)',
    },
    'CRUDEOILM': {
        'lot_size': 10, 'multiplier': 10, 'margin': 8000,
        'strike_interval': 50, 'vol_adj': 1.4,
        'file': 'CRUDEOIL_spot_one_day_2000d.csv',  # Use standard data
        'exchange': 'MCX', 'description': 'Crude Oil Mini (10 bbl)',
    },
    'GOLD': {
        'lot_size': 1, 'multiplier': 100, 'margin': 100000,
        'strike_interval': 500, 'vol_adj': 1.0,
        'file': 'GOLD_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Gold Standard (1kg)',
    },
    'SILVER': {
        'lot_size': 30, 'multiplier': 30, 'margin': 80000,
        'strike_interval': 500, 'vol_adj': 1.15,
        'file': 'SILVER_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Silver (30kg)',
    },
    'NATURALGAS': {
        'lot_size': 1250, 'multiplier': 1250, 'margin': 70000,
        'strike_interval': 5, 'vol_adj': 1.8,
        'file': 'NATURALGAS_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Natural Gas (1250 mmBtu)',
    },
    'COPPER': {
        'lot_size': 2500, 'multiplier': 2500, 'margin': 60000,
        'strike_interval': 5, 'vol_adj': 1.1,
        'file': 'COPPER_spot_one_day_2000d.csv',
        'exchange': 'MCX', 'description': 'Copper (2500kg)',
    },
}

# For paper trading with Rs 3L, focus on affordable mini contracts
PAPER_TRADE_COMMODITIES = ['GOLDM', 'SILVERM', 'CRUDEOILM']

# MCX Trading costs
MCX_BROKERAGE = 20
MCX_CTT = 0.0001
MCX_EXCHANGE = 0.00026
MCX_GST = 0.18

# MCX Market hours (extended)
MCX_OPEN = dtime(9, 0)
MCX_CLOSE = dtime(23, 30)  # 11:30 PM
COMMODITY_TRADE_START = dtime(9, 15)  # Commodity trades from 9:15 AM (no time barrier)

ANGEL_CRED_FILE = os.environ.get('ANGEL_CRED_FILE', r"C:\Users\Ram\Data\Angel\ANGEL_API_KEY=your_api_key.txt")

# Logging
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'commodity_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('CommodityPaperTrader')


# ====================================================================
# BLACK-76 MODEL
# ====================================================================
def black76_greeks(F, K, T, r, sigma, opt_type='CE'):
    """Black-76 for commodity options on futures."""
    T = max(T, 1e-6); sigma = max(sigma, 0.01)
    F = max(F, 0.01); K = max(K, 0.01)
    d1 = (np.log(F/K) + 0.5*sigma**2*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    disc = np.exp(-r * T)
    if opt_type == 'CE':
        price = disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
        delta = disc * norm.cdf(d1)
    else:
        price = disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        delta = -disc * norm.cdf(-d1)
    gamma = disc * norm.pdf(d1) / (F * sigma * np.sqrt(T))
    theta = -(F * sigma * norm.pdf(d1) * disc) / (2 * np.sqrt(T)) / 365
    vega = F * np.sqrt(T) * norm.pdf(d1) * disc / 100
    return {'price': max(price, 0.01), 'delta': delta, 'gamma': gamma,
            'theta': theta, 'vega': vega}


def calc_mcx_costs(premium, qty, multiplier, is_sell=False):
    """MCX trading costs."""
    turnover = premium * qty * multiplier
    brokerage = MCX_BROKERAGE * 2
    ctt = turnover * MCX_CTT if is_sell else 0
    exchange = turnover * MCX_EXCHANGE
    gst = brokerage * MCX_GST
    return brokerage + ctt + exchange + gst


# ====================================================================
# PORTFOLIO TRACKER
# ====================================================================
class CommodityPortfolio:
    """Track commodity paper positions."""

    def __init__(self, capital=INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.positions = []
        self.closed_trades = []
        self.daily_pnl = {}
        self._load_state()

    def _state_file(self):
        return os.path.join(PAPER_DIR, 'commodity_portfolio_state.json')

    def _load_state(self):
        sf = self._state_file()
        if os.path.exists(sf):
            try:
                with open(sf, 'r') as f:
                    state = json.load(f)
                self.capital = state.get('capital', self.initial_capital)
                self.positions = state.get('positions', [])
                self.closed_trades = state.get('closed_trades', [])
                self.daily_pnl = state.get('daily_pnl', {})
                logger.info(f"Loaded state: Capital Rs {self.capital:,.0f}, "
                           f"{len(self.positions)} open, {len(self.closed_trades)} closed")
            except Exception as e:
                logger.error(f"Error loading state: {e}")

    def save_state(self):
        state = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat()
        }
        with open(self._state_file(), 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def add_signal(self, strategy, commodity, signal_type, strike, entry_premium,
                   greeks, dte, details=None):
        spec = COMMODITIES[commodity]
        lot = spec['lot_size']
        mult = spec['multiplier']
        is_sell = 'SELL' in signal_type
        cost = calc_mcx_costs(entry_premium, lot, mult, is_sell)

        pos = {
            'id': f"{strategy}_{commodity}_{datetime.now().strftime('%H%M%S')}",
            'timestamp': datetime.now().isoformat(),
            'strategy': strategy,
            'commodity': commodity,
            'signal_type': signal_type,
            'strike': strike,
            'entry_premium': round(entry_premium, 2),
            'current_premium': round(entry_premium, 2),
            'lot_size': lot,
            'multiplier': mult,
            'is_sell': is_sell,
            'entry_cost': round(cost, 2),
            'delta': round(greeks['delta'], 4),
            'gamma': round(greeks['gamma'], 6),
            'theta': round(greeks['theta'], 2),
            'dte': dte,
            'unrealized_pnl': 0,
            'status': 'OPEN',
            'details': details or {},
        }
        self.positions.append(pos)

        logger.info(f"  PAPER TRADE: {signal_type} {commodity} {strike} "
                    f"@ Rs {entry_premium:.2f} | {strategy} | Delta: {greeks['delta']:.3f}")
        self.save_state()
        return pos

    def close_position(self, pos_id, exit_premium, reason=''):
        for i, pos in enumerate(self.positions):
            if pos['id'] == pos_id:
                exit_cost = calc_mcx_costs(exit_premium, pos['lot_size'],
                                           pos['multiplier'], not pos['is_sell'])
                if pos['is_sell']:
                    pnl = (pos['entry_premium'] - exit_premium) * pos['lot_size'] * pos['multiplier']
                else:
                    pnl = (exit_premium - pos['entry_premium']) * pos['lot_size'] * pos['multiplier']
                pnl -= (pos['entry_cost'] + exit_cost)

                trade = {**pos, 'exit_premium': round(exit_premium, 2),
                         'pnl': round(pnl, 2), 'exit_reason': reason,
                         'exit_time': datetime.now().isoformat(), 'status': 'CLOSED'}
                self.closed_trades.append(trade)
                self.positions.pop(i)
                today = datetime.now().strftime('%Y-%m-%d')
                self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl
                self.capital += pnl
                logger.info(f"  CLOSED: {pos['signal_type']} {pos['commodity']} "
                           f"| PnL: Rs {pnl:,.2f} | {reason}")
                self.save_state()
                return trade
        return None

    def print_status(self):
        total_unrealized = sum(p['unrealized_pnl'] for p in self.positions)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        total_realized = sum(t['pnl'] for t in self.closed_trades)

        print("\n" + "=" * 70)
        print("COMMODITY PAPER TRADING STATUS")
        print("=" * 70)
        print(f"  Capital:         Rs {self.capital:,.0f}")
        print(f"  Return:          {(self.capital-self.initial_capital)/self.initial_capital*100:.2f}%")
        print(f"  Open Positions:  {len(self.positions)}")
        print(f"  Closed Trades:   {len(self.closed_trades)}")
        print(f"  Win Rate:        {len(wins)/len(self.closed_trades)*100:.1f}%" if self.closed_trades else "  Win Rate:        N/A")
        print(f"  Realized PnL:    Rs {total_realized:,.0f}")
        print(f"  Unrealized PnL:  Rs {total_unrealized:,.0f}")

        if self.positions:
            print("\n  OPEN POSITIONS:")
            for p in self.positions:
                print(f"    {p['strategy']:12s} | {p['signal_type']:12s} | "
                      f"{p['commodity']:12s} | Strike: {p['strike']:>10.0f} | "
                      f"Entry: {p['entry_premium']:>8.2f} | PnL: Rs {p['unrealized_pnl']:>8.2f}")

        if self.closed_trades:
            recent = self.closed_trades[-5:]
            print(f"\n  RECENT TRADES:")
            for t in recent:
                print(f"    {t['strategy']:12s} | {t['commodity']:12s} | "
                      f"PnL: Rs {t['pnl']:>10.2f} | {t['exit_reason']}")
        print("=" * 70)


# ====================================================================
# STRATEGY ENGINE FOR COMMODITIES
# ====================================================================
class CommodityStrategyEngine:
    """Generate signals from strategies adapted for commodities."""

    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.historical_data = {}

    def load_historical(self, commodity):
        spec = COMMODITIES[commodity]
        fpath = os.path.join(DATA_DIR, spec['file'])
        if os.path.exists(fpath):
            df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            self.historical_data[commodity] = df
            logger.info(f"Loaded {len(df)} days for {commodity}")
            return df
        return None

    def compute_indicators(self, commodity, current_ohlc=None):
        df = self.historical_data.get(commodity)
        if df is None:
            df = self.load_historical(commodity)
        if df is None:
            return None

        spec = COMMODITIES[commodity]

        if current_ohlc:
            today = pd.Timestamp.now().normalize()
            new_row = pd.DataFrame({
                'Open': [current_ohlc['open']], 'High': [current_ohlc['high']],
                'Low': [current_ohlc['low']], 'Close': [current_ohlc['close']],
                'Volume': [current_ohlc.get('volume', 0)],
            }, index=[today])
            if today in df.index:
                df.loc[today] = new_row.iloc[0]
            else:
                df = pd.concat([df, new_row])

        atr = (df['High'].tail(14) - df['Low'].tail(14)).mean()
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        hv = log_ret.tail(20).std() * np.sqrt(252)
        iv = max(min(hv * 1.15 * spec['vol_adj'], 0.80), 0.08)

        prev = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        pivot = (prev['High'] + prev['Low'] + prev['Close']) / 3
        bc = (prev['High'] + prev['Low']) / 2
        tc = 2 * pivot - bc
        cpr_width = abs(tc - bc) / prev['Close'] * 100

        h_range = prev['High'] - prev['Low']
        cam_r3 = prev['Close'] + h_range * 1.1 / 4
        cam_r4 = prev['Close'] + h_range * 1.1 / 2
        cam_s3 = prev['Close'] - h_range * 1.1 / 4
        cam_s4 = prev['Close'] - h_range * 1.1 / 2

        recent_lows = df['Low'].tail(10)
        recent_highs = df['High'].tail(10)
        demand_zone = recent_lows.min()
        supply_zone = recent_highs.max()
        demand_strength = ((recent_lows - demand_zone).abs() < atr * 0.5).sum()
        supply_strength = ((supply_zone - recent_highs).abs() < atr * 0.5).sum()

        prev_range = (prev['High'] - prev['Low']) / max(atr, 1)

        return {
            'atr': atr, 'iv': iv, 'hv': hv,
            'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_width,
            'cam_r3': cam_r3, 'cam_r4': cam_r4, 'cam_s3': cam_s3, 'cam_s4': cam_s4,
            'demand_zone': demand_zone, 'supply_zone': supply_zone,
            'demand_strength': demand_strength, 'supply_strength': supply_strength,
            'prev_range': prev_range,
        }

    def check_cpr_signals(self, commodity, spot, ohlc, ind, dow, dte):
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        T = dte / 365

        if ind['cpr_width'] < 0.4:
            if spot > ind['tc']:
                ce_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                if g['price'] > 1:
                    signals.append({
                        'type': 'BUY_CE_CPR', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Narrow CPR ({ind['cpr_width']:.3f}%) bullish breakout",
                        'target': g['price'] * 1.8, 'sl': g['price'] * 0.4,
                    })
            elif spot < ind['bc']:
                pe_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                if g['price'] > 1:
                    signals.append({
                        'type': 'BUY_PE_CPR', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Narrow CPR ({ind['cpr_width']:.3f}%) bearish breakout",
                        'target': g['price'] * 1.8, 'sl': g['price'] * 0.4,
                    })

        elif ind['cpr_width'] > 0.6:
            margin_ok = self.portfolio.capital >= spec['margin']
            if ohlc['high'] >= ind['cam_r3'] * 0.998 and spot < ind['cam_r4'] and margin_ok:
                ce_strike = round(ind['cam_r4'] / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
                if g['price'] > 2:
                    signals.append({
                        'type': 'SELL_CE_CPR', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at R3",
                        'target': g['price'] * 0.3, 'sl': g['price'] * 2.0,
                    })
            if ohlc['low'] <= ind['cam_s3'] * 1.002 and spot > ind['cam_s4'] and margin_ok:
                pe_strike = round(ind['cam_s4'] / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
                if g['price'] > 2:
                    signals.append({
                        'type': 'SELL_PE_CPR', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Wide CPR ({ind['cpr_width']:.3f}%) mean reversion at S3",
                        'target': g['price'] * 0.3, 'sl': g['price'] * 2.0,
                    })
        return signals

    def check_gamma_blast_signals(self, commodity, spot, ohlc, ind, dow, dte):
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        atr = ind['atr']
        T = max(3, dte) / 365

        if ind['prev_range'] > 1.2:
            return signals

        day_range = (ohlc['high'] - ohlc['low']) / max(atr, 1)
        if day_range < 0.5:
            return signals

        body = spot - ohlc['open']
        if abs(body) > atr * 0.2:
            if body > 0:
                ce_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv * 1.2, 'CE')
                if g['price'] > 1:
                    signals.append({
                        'type': 'BUY_CE_GAMMA', 'strike': ce_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Gamma Blast: Up breakout body={body:.1f}",
                        'target': g['price'] * 2.5, 'sl': g['price'] * 0.3,
                    })
            else:
                pe_strike = round(spot / strike_int) * strike_int
                g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv * 1.2, 'PE')
                if g['price'] > 1:
                    signals.append({
                        'type': 'BUY_PE_GAMMA', 'strike': pe_strike,
                        'premium': g['price'], 'greeks': g,
                        'reason': f"Gamma Blast: Down breakout body={body:.1f}",
                        'target': g['price'] * 2.5, 'sl': g['price'] * 0.3,
                    })
        return signals

    def check_ghost_zone_signals(self, commodity, spot, ohlc, ind, dow, dte):
        signals = []
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        iv = ind['iv']
        atr = ind['atr']
        T = dte / 365

        if ohlc['low'] <= ind['demand_zone'] * 1.01 and spot > ind['demand_zone'] and ind['demand_strength'] >= 2:
            ce_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, iv, 'CE')
            if g['price'] > 1:
                bounce = (spot - ohlc['low']) / max(atr, 1)
                signals.append({
                    'type': 'BUY_CE_GTZ', 'strike': ce_strike,
                    'premium': g['price'], 'greeks': g,
                    'reason': f"Ghost Zone: Demand retest bounce={bounce:.2f}x",
                    'target': g['price'] * 1.8 if bounce > 0.8 else g['price'] * 1.3,
                    'sl': g['price'] * 0.4,
                })

        elif ohlc['high'] >= ind['supply_zone'] * 0.99 and spot < ind['supply_zone'] and ind['supply_strength'] >= 2:
            pe_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, iv, 'PE')
            if g['price'] > 1:
                signals.append({
                    'type': 'BUY_PE_GTZ', 'strike': pe_strike,
                    'premium': g['price'], 'greeks': g,
                    'reason': f"Ghost Zone: Supply retest",
                    'target': g['price'] * 1.8, 'sl': g['price'] * 0.4,
                })
        return signals


# ====================================================================
# ANGEL API CONNECTION FOR MCX
# ====================================================================
class AngelMCXConnection:
    """Angel One SmartAPI for MCX data."""

    def __init__(self):
        self.obj = None
        self._connected = False
        self.instruments = None
        # MCX futures tokens for spot proxy
        self._futures_tokens = {}

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
                if not line or line.startswith('#'): continue
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

        app = creds.get('Historical', creds.get('Market', {}))
        self.obj = SmartConnect(api_key=app['ANGEL_API_KEY'])
        totp = pyotp.TOTP(app['ANGEL_TOTP_KEY']).now()
        session = self.obj.generateSession(app['ANGEL_CLIENT_CODE'], app['ANGEL_PIN'], totp)

        if session and session.get('status'):
            self._connected = True
            logger.info(f"Angel MCX connected")
            self._load_mcx_tokens()
            return True
        return False

    def _load_mcx_tokens(self):
        """Find nearest futures tokens for each commodity."""
        cache = os.path.join(DATA_DIR, 'instrument_master.csv')
        if os.path.exists(cache):
            df = pd.read_csv(cache, low_memory=False)
            mcx = df[df['exch_seg'] == 'MCX']
            for comm in COMMODITIES:
                futs = mcx[(mcx['name'] == comm) & (mcx['instrumenttype'] == 'FUTCOM')]
                if len(futs) > 0:
                    futs_sorted = futs.sort_values('expiry')
                    self._futures_tokens[comm] = str(futs_sorted.iloc[0]['token'])

    def get_ltp(self, commodity):
        if not self._connected or commodity not in self._futures_tokens:
            return None
        try:
            data = self.obj.ltpData('MCX', self._futures_tokens[commodity],
                                    self._futures_tokens[commodity])
            if data and data.get('data'):
                return data['data'].get('ltp')
        except Exception as e:
            logger.error(f"LTP error {commodity}: {e}")
        return None


# ====================================================================
# MAIN PAPER TRADER
# ====================================================================
class CommodityPaperTrader:

    def __init__(self):
        self.angel = AngelMCXConnection()
        self.portfolio = CommodityPortfolio()
        self.engine = CommodityStrategyEngine(self.portfolio)
        self._running = False

    def connect(self):
        return self.angel.connect()

    def get_spot(self, commodity):
        ltp = self.angel.get_ltp(commodity)
        if ltp:
            return ltp
        df = self.engine.historical_data.get(commodity)
        if df is not None and len(df) > 0:
            return df['Close'].iloc[-1]
        return None

    def is_mcx_open(self):
        """Check if MCX trading is allowed (after 3:30 PM until 11:30 PM, Mon-Fri)."""
        now = datetime.now()
        if now.weekday() > 4:  # Saturday/Sunday
            return False
        return COMMODITY_TRADE_START <= now.time() <= MCX_CLOSE

    def scan_all(self):
        logger.info("\n" + "=" * 70)
        logger.info("SCANNING COMMODITY STRATEGIES...")
        logger.info("=" * 70)

        now = datetime.now()
        dow = now.weekday()
        all_signals = []

        # CRITICAL: Block trading outside MCX hours
        if not self.is_mcx_open():
            logger.warning(f"  MCX MARKET CLOSED (current: {now.strftime('%H:%M:%S')})")
            logger.warning(f"  MCX hours: {MCX_OPEN} - {MCX_CLOSE}, Mon-Fri")
            logger.warning(f"  No signals will be generated.")
            return all_signals

        for commodity in PAPER_TRADE_COMMODITIES:
            spec = COMMODITIES[commodity]
            logger.info(f"\n--- {commodity} ({spec['description']}) ---")

            spot = self.get_spot(commodity)
            if not spot:
                logger.warning(f"  No spot data for {commodity}")
                continue

            ohlc = {'open': spot, 'high': spot, 'low': spot, 'close': spot, 'volume': 0}
            logger.info(f"  Spot: {spot:,.2f}")

            indicators = self.engine.compute_indicators(commodity, ohlc)
            if not indicators:
                continue

            logger.info(f"  ATR: {indicators['atr']:,.2f} | IV: {indicators['iv']*100:.1f}% | "
                       f"CPR: {indicators['cpr_width']:.3f}%")
            logger.info(f"  Pivot: {indicators['pivot']:,.0f} | TC: {indicators['tc']:,.0f} | "
                       f"BC: {indicators['bc']:,.0f}")

            # Monthly expiry - estimate DTE
            dte = max(5, 15 - (now.day % 28))

            checks = [
                ('CPR', self.engine.check_cpr_signals),
                ('Gamma Blast', self.engine.check_gamma_blast_signals),
                ('Ghost Zone', self.engine.check_ghost_zone_signals),
            ]

            for strat_name, check_fn in checks:
                signals = check_fn(commodity, spot, ohlc, indicators, dow, dte)
                for sig in signals:
                    sig['strategy'] = strat_name
                    sig['commodity'] = commodity
                    sig['spot'] = spot
                    sig['dte'] = dte
                    all_signals.append(sig)
                    logger.info(f"  SIGNAL [{strat_name}]: {sig['type']} "
                               f"Strike={sig['strike']:,.0f} Premium=Rs {sig['premium']:.2f} "
                               f"| {sig['reason']}")

        return all_signals

    def execute_signals(self, signals):
        if not signals:
            logger.info("\n  No commodity signals this scan.")
            return

        # CRITICAL: Block execution outside MCX hours
        if not self.is_mcx_open():
            logger.warning(f"  {len(signals)} signals found but MCX CLOSED - NOT executing")
            return

        logger.info(f"\n  {len(signals)} COMMODITY SIGNALS (PAPER):")
        executed = 0
        skipped = 0
        for sig in signals:
            # Check for duplicate: same strategy + commodity + strike already open
            existing = [p for p in self.portfolio.positions
                        if p['strategy'] == sig['strategy']
                        and p['commodity'] == sig['commodity']
                        and p['strike'] == sig['strike']]
            if existing:
                logger.info(f"  SKIP (duplicate): {sig['strategy']} {sig['commodity']} "
                           f"{sig['strike']} already open")
                skipped += 1
                continue

            self.portfolio.add_signal(
                strategy=sig['strategy'],
                commodity=sig['commodity'],
                signal_type=sig['type'],
                strike=sig['strike'],
                entry_premium=sig['premium'],
                greeks=sig['greeks'],
                dte=sig['dte'],
                details={'reason': sig['reason'], 'target': sig.get('target'),
                         'sl': sig.get('sl'), 'spot': sig.get('spot')},
            )
            executed += 1

            # Send Telegram notification
            try:
                from trade_notifier import notify_trade_entry
                spec = COMMODITIES[sig['commodity']]
                capital = sig['premium'] * spec['lot_size'] * spec['multiplier']
                # Calculate available capital for Telegram message
                locked = sum(
                    p['entry_premium'] * COMMODITIES[p['commodity']]['lot_size'] * COMMODITIES[p['commodity']]['multiplier']
                    for p in self.portfolio.positions
                )
                capital_available = self.portfolio.capital - locked
                notify_trade_entry(
                    market="COMMODITY", strategy=sig['strategy'],
                    symbol=sig['commodity'], signal_type=sig['type'],
                    strike=sig['strike'], entry_price=sig['premium'],
                    spot=sig.get('spot', 0), lot_size=spec['lot_size'],
                    multiplier=spec['multiplier'], delta=sig['greeks'].get('delta', 0),
                    target=sig.get('target', 0), sl=sig.get('sl', 0),
                    capital_used=capital, reason=sig['reason'],
                    capital_available=capital_available,
                )
            except Exception as e:
                logger.warning(f"  Telegram notify failed: {e}")

        if skipped:
            logger.info(f"  Executed: {executed} | Skipped (duplicates): {skipped}")

    def check_exits(self):
        if not self.portfolio.positions:
            return
        for pos in list(self.portfolio.positions):
            # Skip positions opened less than 5 minutes ago (grace period)
            entry_time = datetime.fromisoformat(pos['timestamp'])
            if (datetime.now() - entry_time).total_seconds() < 300:
                continue

            commodity = pos['commodity']
            spot = self.get_spot(commodity)
            if not spot:
                continue

            # Use the same IV that was used at entry for consistency
            T = max(pos['dte'] - 0.5, 0.01) / 365
            spec = COMMODITIES[commodity]
            # Compute IV from historical data (same as entry)
            df = self.engine.historical_data.get(commodity)
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                iv = max(min(hv * 1.15 * spec['vol_adj'], 0.80), 0.08)
            else:
                iv = 0.20 * spec['vol_adj']

            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
            g = black76_greeks(spot, pos['strike'], T, RISK_FREE_RATE, iv, opt_type)
            current = g['price']
            pos['current_premium'] = round(current, 2)

            # Update unrealized PnL
            mult = spec['multiplier']
            lot = spec['lot_size']
            if pos['is_sell']:
                pos['unrealized_pnl'] = round(
                    (pos['entry_premium'] - current) * lot * mult - pos['entry_cost'], 2)
            else:
                pos['unrealized_pnl'] = round(
                    (current - pos['entry_premium']) * lot * mult - pos['entry_cost'], 2)

            details = pos.get('details', {})
            target = details.get('target', pos['entry_premium'] * 2)
            sl = details.get('sl', pos['entry_premium'] * 0.3)

            exit_reason = None
            if pos['is_sell']:
                if current <= target: exit_reason = 'TARGET'
                elif current >= sl: exit_reason = 'SL'
            else:
                if current >= target: exit_reason = 'TARGET'
                elif current <= sl: exit_reason = 'SL'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current, exit_reason)
                # Send Telegram exit notification
                try:
                    from trade_notifier import notify_trade_exit
                    spec = COMMODITIES[commodity]
                    capital = pos['entry_premium'] * spec['lot_size'] * spec['multiplier']
                    # Capital available AFTER closing this position
                    locked = sum(
                        p['entry_premium'] * COMMODITIES[p['commodity']]['lot_size'] * COMMODITIES[p['commodity']]['multiplier']
                        for p in self.portfolio.positions
                    )
                    capital_available = self.portfolio.capital - locked
                    notify_trade_exit(
                        market="COMMODITY", strategy=pos['strategy'],
                        symbol=commodity, signal_type=pos['signal_type'],
                        strike=pos['strike'], entry_price=pos['entry_premium'],
                        exit_price=current, entry_time=pos['timestamp'],
                        pnl=pos['unrealized_pnl'], capital_used=capital,
                        exit_reason=exit_reason,
                        capital_available=capital_available,
                    )
                except Exception as e:
                    logger.warning(f"  Telegram exit notify failed: {e}")

    def run_once(self):
        signals = self.scan_all()
        self.execute_signals(signals)
        self.check_exits()
        self.portfolio.print_status()

        # Save signals log
        if signals:
            today = datetime.now().strftime('%Y%m%d')
            log_file = os.path.join(PAPER_DIR, f'commodity_signals_{today}.csv')
            rows = [{'timestamp': datetime.now().isoformat(), 'strategy': s['strategy'],
                     'commodity': s['commodity'], 'type': s['type'], 'strike': s['strike'],
                     'premium': s['premium'], 'spot': s.get('spot'), 'reason': s['reason']}
                    for s in signals]
            df = pd.DataFrame(rows)
            if os.path.exists(log_file):
                df = pd.concat([pd.read_csv(log_file), df], ignore_index=True)
            df.to_csv(log_file, index=False)

    def run_continuous(self, interval=5):
        self._running = True
        logger.info(f"\n{'='*70}")
        logger.info("COMMODITY PAPER TRADING STARTED")
        logger.info(f"MCX Hours: 9:00 AM - 11:30 PM | Scan every {interval} min")
        logger.info(f"Commodities: {', '.join(PAPER_TRADE_COMMODITIES)}")
        logger.info(f"Capital: Rs {self.portfolio.capital:,.0f}")
        logger.info(f"{'='*70}\n")

        signal.signal(signal.SIGINT, lambda s, f: setattr(self, '_running', False))

        while self._running:
            now = datetime.now().time()
            if MCX_OPEN <= now <= MCX_CLOSE:
                self.run_once()
            elif now > MCX_CLOSE:
                logger.info("MCX closed. Final status:")
                self.portfolio.print_status()
                break
            time.sleep(interval * 60)


def main():
    print("=" * 70)
    print("  COMMODITY OPTIONS - PAPER TRADING SYSTEM")
    print("  MCX | Gold Mini, Silver Mini, Crude Oil Mini")
    print("  Strategies: CPR, Gamma Blast, Ghost Zone")
    print("=" * 70)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=int, default=5)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()

    if args.reset:
        p = CommodityPortfolio()
        p.capital = INITIAL_CAPITAL; p.positions = []; p.closed_trades = []; p.daily_pnl = {}
        p.save_state()
        print("Commodity portfolio reset to Rs 3,00,000")
        return

    if args.status:
        CommodityPortfolio().print_status()
        return

    trader = CommodityPaperTrader()
    for comm in PAPER_TRADE_COMMODITIES:
        trader.engine.load_historical(comm)

    if args.offline:
        logger.info("Running OFFLINE mode...")
        trader.run_once()
        return

    if trader.connect():
        if args.once:
            trader.run_once()
        else:
            trader.run_continuous(args.interval)
    else:
        logger.warning("API failed, running offline...")
        trader.run_once()


if __name__ == '__main__':
    main()
