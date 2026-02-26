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

INITIAL_CAPITAL = 300000  # Rs 3L for commodities (all strategies share this pool)
RISK_FREE_RATE = 0.065

# Focus on MINI contracts (affordable with Rs 3L capital)
COMMODITIES = {
    'GOLDM': {
        'lot_size': 1, 'multiplier': 10, 'margin': 15000,
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

# ====================================================================
# CAPITAL & RISK MANAGEMENT FOR COMMODITY TRADING
# ====================================================================
COMMODITY_CAPITAL = 300000                              # Rs 3L for commodities
MAX_RISK_PCT = 25                                       # Max 25% of capital per trade
MAX_PER_TRADE = COMMODITY_CAPITAL * MAX_RISK_PCT / 100  # Rs 75,000
MAX_POSITIONS_PER_COMMODITY = 3                         # Prevent cascade (e.g., 16 SILVERM trades)
MAX_DAILY_LOSS = COMMODITY_CAPITAL * 0.10               # Rs 30,000 (10% daily loss limit)

# Trailing Stop Loss Parameters
TSL_BREAKEVEN_TRIGGER_PCT = 30   # Lock breakeven when 30% of target distance reached
TSL_TRAIL_TRIGGER_PCT = 50       # Start trailing when 50% of target distance reached
TSL_TRAIL_DISTANCE_PCT = 25      # Trail at 25% below peak unrealized profit

# Commodity OI/IV Exit Thresholds (wider than equity due to lower liquidity)
MCX_OI_SURGE_PCT = 20        # Exit if OI changes >20% from entry
MCX_OI_REVERSE_PCT = 30      # Reverse trade if OI changes >30%
MCX_IV_SPIKE_PCT = 30         # Exit if IV changes >30% from entry
MCX_IV_REVERSE_PCT = 50       # Reverse trade if IV changes >50%
MCX_OI_IV_COMBO_OI = 15      # Combined exit: OI >15% AND IV >20%
MCX_OI_IV_COMBO_IV = 20
MCX_GAMMA_SHIELD_THRESHOLD = 0.003  # Exit short if gamma > this

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
            'theta': theta, 'vega': vega, 'iv': sigma}


def implied_vol_b76(market_price, F, K, T, r, opt_type='CE', max_iter=50, tol=1e-4):
    """Back-solve for implied volatility from real market price using Black-76.
    Returns IV (decimal) or None if convergence fails.
    """
    if market_price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    # Initial guess: ATM approximation
    sigma = market_price / F * np.sqrt(2 * np.pi / T)
    sigma = max(min(sigma, 3.0), 0.01)
    for _ in range(max_iter):
        g = black76_greeks(F, K, T, r, sigma, opt_type)
        diff = g['price'] - market_price
        vega_full = g['vega'] * 100  # vega was divided by 100
        if abs(vega_full) < 1e-10:
            break
        sigma -= diff / vega_full
        sigma = max(min(sigma, 3.0), 0.005)
        if abs(diff) < tol:
            return sigma
    return sigma if 0.005 < sigma < 3.0 else None


def greeks_from_market_price_b76(market_price, F, K, T, r, opt_type='CE'):
    """Compute accurate commodity Greeks from real market price using Black-76."""
    iv = implied_vol_b76(market_price, F, K, T, r, opt_type)
    if iv is None:
        return None
    g = black76_greeks(F, K, T, r, iv, opt_type)
    g['iv'] = iv
    g['price'] = market_price
    return g


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
                   greeks, dte, details=None, oi=0, iv=0):
        spec = COMMODITIES[commodity]
        lot = spec['lot_size']
        mult = spec['multiplier']
        is_sell = 'SELL' in signal_type

        # ---- RISK CHECKS: Only trade with leftover capital ----

        # Capital required: SELL = margin blocked, BUY = premium paid
        if is_sell:
            trade_capital = spec['margin']
        else:
            trade_capital = entry_premium * lot * mult

        # Check 1: Per-trade limit (25% of commodity capital)
        if trade_capital > MAX_PER_TRADE:
            logger.warning(f"  RISK_LIMIT: {commodity} {strategy} trade capital Rs {trade_capital:,.0f} "
                          f"> max Rs {MAX_PER_TRADE:,.0f}. SKIPPED.")
            return None

        # Check 2: Only trade with LEFTOVER available capital
        total_locked = 0
        for p in self.positions:
            p_spec = COMMODITIES[p['commodity']]
            if p['is_sell']:
                total_locked += p_spec['margin']
            else:
                total_locked += p['entry_premium'] * p['lot_size'] * p['multiplier']

        available_capital = self.capital - total_locked
        if trade_capital > available_capital:
            logger.warning(f"  RISK_LIMIT: {commodity} {strategy} needs Rs {trade_capital:,.0f} "
                          f"but only Rs {available_capital:,.0f} available. SKIPPED.")
            return None

        # Check 3: Daily loss limit (10% of commodity capital)
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"  RISK_LIMIT: Daily loss Rs {daily_loss:,.0f} exceeds "
                          f"limit Rs {MAX_DAILY_LOSS:,.0f}. SKIPPED.")
            return None

        # Check 4: Max positions per commodity (prevent cascade)
        same_commodity = [p for p in self.positions if p['commodity'] == commodity]
        if len(same_commodity) >= MAX_POSITIONS_PER_COMMODITY:
            logger.warning(f"  RISK_LIMIT: Max {MAX_POSITIONS_PER_COMMODITY} positions "
                          f"for {commodity} reached. SKIPPED.")
            return None

        # Check 5: Drawdown-based position scaling
        hwm = max(self.initial_capital, self.capital)
        dd_pct = (hwm - self.capital) / hwm * 100
        if dd_pct > 5:
            scale = max(0.5, 1.0 - (dd_pct - 5) * 0.05)
            scaled_max = MAX_PER_TRADE * scale
            if trade_capital > scaled_max:
                logger.warning(f"  RISK_SCALE: DD={dd_pct:.1f}%, trade Rs {trade_capital:,.0f} > "
                              f"scaled Rs {scaled_max:,.0f}. SKIPPED.")
                return None

        # ---- END RISK CHECKS ----

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
            'entry_spot': details.get('spot', 0) if details else 0,
            # OI/IV tracking for dynamic exits
            'entry_oi': oi,
            'entry_iv': round(greeks.get('iv', iv) * 100 if greeks.get('iv', iv) and greeks.get('iv', iv) < 1 else (greeks.get('iv', iv) or 0), 1),
            # Trailing Stop Loss tracking
            'peak_premium': round(entry_premium, 2),
            'trough_premium': round(entry_premium, 2),
            'trailing_sl': None,
            'breakeven_locked': False,
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

    def __init__(self, portfolio, angel=None):
        self.portfolio = portfolio
        self.angel = angel
        self.historical_data = {}

    def load_historical(self, commodity):
        """Load historical data for commodity. Auto-download if missing/stale."""
        spec = COMMODITIES[commodity]
        fpath = os.path.join(DATA_DIR, spec['file'])

        # Check if file needs downloading (missing or stale >7 days)
        needs_download = False
        if os.path.exists(fpath):
            mod_time = datetime.fromtimestamp(os.path.getmtime(fpath))
            age_days = (datetime.now() - mod_time).days
            if age_days >= 7:
                needs_download = True
                logger.info(f"Historical data for {commodity} is stale ({age_days}d old), refreshing...")
        else:
            needs_download = True
            logger.info(f"Historical data for {commodity} not found, downloading...")

        if needs_download and self.angel and self.angel._connected:
            self._download_historical_commodity(commodity, fpath)

        if os.path.exists(fpath):
            df = pd.read_csv(fpath, parse_dates=['DateTime'], index_col='DateTime')
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            self.historical_data[commodity] = df
            logger.info(f"Loaded {len(df)} days for {commodity}")
            return df

        logger.error(f"No historical data found for {commodity} — indicators will not work")
        return None

    def _download_historical_commodity(self, commodity, save_path):
        """Download daily OHLC data from Angel SmartAPI for MCX commodity."""
        try:
            # Use the futures token from angel connection
            token = self.angel._futures_tokens.get(commodity)
            if not token:
                logger.warning(f"No futures token for {commodity}, skipping download")
                return

            logger.info(f"Downloading historical data for {commodity} (token={token})...")
            all_data = []
            chunk_start = datetime.now() - timedelta(days=2000)

            while chunk_start < datetime.now():
                chunk_end = min(chunk_start + timedelta(days=500), datetime.now())
                data = self.angel.get_historical(
                    'MCX', token, 'ONE_DAY',
                    chunk_start.strftime('%Y-%m-%d 09:00'),
                    chunk_end.strftime('%Y-%m-%d 23:30')
                )
                if data:
                    all_data.extend(data)
                chunk_start = chunk_end + timedelta(days=1)
                time.sleep(0.5)  # Rate limit between API calls

            if all_data:
                df = pd.DataFrame(all_data, columns=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                df.to_csv(save_path, index=False)
                logger.info(f"Downloaded {len(df)} days for {commodity} → {save_path}")
            else:
                logger.warning(f"No data returned from Angel API for {commodity}")
        except Exception as e:
            logger.error(f"Download failed for {commodity}: {e}")

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

        # VWAP calculation (for PCR+VWAP strategy)
        if 'Volume' in df.columns and df['Volume'].tail(5).sum() > 0:
            tp = (df['High'] + df['Low'] + df['Close']) / 3
            vwap = (tp * df['Volume']).tail(5).sum() / df['Volume'].tail(5).sum()
        else:
            vwap = (df['High'].tail(5) + df['Low'].tail(5) + df['Close'].tail(5)).mean() / 3

        # PCR proxy from recent momentum direction
        pcr_proxy = 1 + (log_ret.tail(5).mean() * 10 if len(log_ret) > 5 else 0)
        pcr_proxy = max(0.5, min(2.0, pcr_proxy))

        # Support/Resistance for Survivor strategy
        resistance = prev['High']
        support = prev['Low']

        return {
            'atr': atr, 'iv': iv, 'hv': hv,
            'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_width,
            'cam_r3': cam_r3, 'cam_r4': cam_r4, 'cam_s3': cam_s3, 'cam_s4': cam_s4,
            'demand_zone': demand_zone, 'supply_zone': supply_zone,
            'demand_strength': demand_strength, 'supply_strength': supply_strength,
            'prev_range': prev_range,
            'vwap': vwap, 'pcr': pcr_proxy,
            'resistance': resistance, 'support': support,
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

    def check_pcr_vwap_signals(self, commodity, spot, ohlc, indicators, dow, dte):
        """PCR+VWAP Strategy adapted for commodities.
        Uses momentum-based PCR proxy + VWAP proximity for entries.
        """
        signals = []
        ind = indicators
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        T = dte / 365

        # Skip in high IV environment
        if ind['iv'] > 0.50:
            return signals

        tolerance = max(ind['atr'] * 0.1, spot * 0.003)
        vwap = ind.get('vwap', spot)
        pcr = ind.get('pcr', 1.0)

        # BUY CE: PCR > 1.05 (bullish momentum), near VWAP
        if pcr > 1.05 and abs(spot - vwap) < tolerance * 2 and spot >= vwap * 0.995:
            ce_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > 1 and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_CE_PCRVWAP',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bullish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.0,
                    'sl': g['price'] * 0.4,
                })

        # BUY PE: PCR < 0.95 (bearish momentum), near VWAP
        elif pcr < 0.95 and abs(spot - vwap) < tolerance * 2 and spot <= vwap * 1.005:
            pe_strike = round(spot / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > 1 and g['price'] < spot * 0.05:
                signals.append({
                    'type': 'BUY_PE_PCRVWAP',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"PCR+VWAP: Bearish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}",
                    'target': g['price'] * 2.0,
                    'sl': g['price'] * 0.4,
                })

        return signals

    def check_survivor_signals(self, commodity, spot, ohlc, indicators, dow, dte):
        """Survivor V2 option selling adapted for commodities.
        Sells OTM options after breakout/breakdown from support/resistance.
        Uses DTE-based filter instead of day-of-week (MCX has monthly expiry).
        """
        signals = []
        ind = indicators
        spec = COMMODITIES[commodity]
        strike_int = spec['strike_interval']
        T = dte / 365
        atr = ind['atr']

        # Skip if too close to expiry (< 3 DTE)
        if dte < 3:
            return signals

        # Check margin availability
        margin_ok = self.portfolio.capital >= spec['margin']
        if not margin_ok:
            return signals

        # DTE-based distance scaling (wider for more DTE)
        if dte >= 15:
            gap = max(atr * 0.4, spot * 0.005)
            distance = max(atr * 0.8, spot * 0.01)
        elif dte >= 10:
            gap = max(atr * 0.3, spot * 0.004)
            distance = max(atr * 0.7, spot * 0.008)
        elif dte >= 5:
            gap = max(atr * 0.2, spot * 0.003)
            distance = max(atr * 0.5, spot * 0.006)
        else:
            gap = max(atr * 0.15, spot * 0.002)
            distance = max(atr * 0.4, spot * 0.004)

        resistance = ind.get('resistance', ind.get('supply_zone', spot + atr))
        support = ind.get('support', ind.get('demand_zone', spot - atr))

        # PE SELLING — price breaks above resistance (bullish → sell OTM PEs)
        if ohlc['high'] > resistance + gap:
            pe_strike = round((spot - distance) / strike_int) * strike_int
            g = black76_greeks(spot, pe_strike, T, RISK_FREE_RATE, ind['iv'], 'PE')
            if g['price'] > 2:
                signals.append({
                    'type': 'SELL_PE_SURV',
                    'strike': pe_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: PE sell at {pe_strike:.0f} dist={distance:.0f} "
                              f"gap={gap:.0f} res={resistance:.0f}",
                    'target': g['price'] * 0.2,
                    'sl': g['price'] * 1.5,
                })

        # CE SELLING — price breaks below support (bearish → sell OTM CEs)
        if ohlc['low'] < support - gap:
            ce_strike = round((spot + distance) / strike_int) * strike_int
            g = black76_greeks(spot, ce_strike, T, RISK_FREE_RATE, ind['iv'], 'CE')
            if g['price'] > 2:
                signals.append({
                    'type': 'SELL_CE_SURV',
                    'strike': ce_strike,
                    'premium': g['price'],
                    'greeks': g,
                    'reason': f"Survivor: CE sell at {ce_strike:.0f} dist={distance:.0f} "
                              f"gap={gap:.0f} sup={support:.0f}",
                    'target': g['price'] * 0.2,
                    'sl': g['price'] * 1.5,
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
        self._last_api_call = 0  # Timestamp of last REST API call
        self._api_min_interval = 0.5  # Minimum 500ms between REST calls

    def _throttle(self):
        """Enforce minimum interval between REST API calls to avoid rate limiting."""
        elapsed = time.time() - self._last_api_call
        if elapsed < self._api_min_interval:
            time.sleep(self._api_min_interval - elapsed)
        self._last_api_call = time.time()

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

        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to Angel MCX... attempt {attempt+1}/{max_retries}")
                self.obj = SmartConnect(api_key=app['ANGEL_API_KEY'])
                totp = pyotp.TOTP(app['ANGEL_TOTP_KEY']).now()
                session = self.obj.generateSession(app['ANGEL_CLIENT_CODE'], app['ANGEL_PIN'], totp)

                if session and session.get('status'):
                    self._connected = True
                    logger.info(f"Angel MCX connected")
                    self._load_mcx_tokens()
                    return True
                else:
                    logger.warning(f"Angel MCX login failed (attempt {attempt+1}): {session}")
            except Exception as e:
                logger.warning(f"Angel MCX connection error (attempt {attempt+1}): {e}")

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s, 32s
                logger.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"Angel MCX connection failed after {max_retries} attempts")
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

    def get_historical(self, exchange, token, interval, from_date, to_date):
        """Get historical candle data from Angel SmartAPI."""
        if not self._connected:
            return None
        try:
            self._throttle()
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date
            }
            data = self.obj.getCandleData(params)
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"MCX Historical error: {e}")
        return None

    def get_ltp(self, commodity):
        if not self._connected or commodity not in self._futures_tokens:
            return None
        try:
            self._throttle()
            data = self.obj.ltpData('MCX', self._futures_tokens[commodity],
                                    self._futures_tokens[commodity])
            if data and data.get('data'):
                return data['data'].get('ltp')
        except Exception as e:
            logger.error(f"LTP error {commodity}: {e}")
        return None

    def get_market_data(self, exchange, symbol_token):
        """Get full market data including OI for MCX option."""
        if not self._connected:
            return None
        try:
            self._throttle()
            data = self.obj.getMarketData("FULL", {exchange: [str(symbol_token)]})
            if data and data.get('data') and data['data'].get('fetched'):
                return data['data']['fetched'][0]
        except Exception as e:
            logger.error(f"MCX Market data error: {e}")
        return None

    def find_option_tokens(self, commodity, expiry, strike, opt_type):
        """Find MCX option contract token from instrument master."""
        cache = os.path.join(DATA_DIR, 'instrument_master.csv')
        if not os.path.exists(cache):
            return None
        try:
            df = pd.read_csv(cache, low_memory=False)
            # MCX options store strikes as strike*100 in some formats
            mask = (df['name'] == commodity) & \
                   (df['exch_seg'] == 'MCX') & \
                   (df['instrumenttype'] == 'OPTFUT')
            if opt_type:
                mask = mask & (df['symbol'].str.endswith(opt_type))
            matches = df[mask]
            if len(matches) == 0:
                return None
            # Try matching strike (MCX may store as strike*100)
            exact = matches[matches['strike'].astype(float) == float(strike * 100)]
            if len(exact) == 0:
                exact = matches[matches['strike'].astype(float) == float(strike)]
            if len(exact) > 0:
                return exact.iloc[0].to_dict()
        except Exception as e:
            logger.error(f"MCX option token lookup error: {e}")
        return None

    def get_option_greeks(self, commodity, expiry=None):
        """Get option greeks including IV from Angel API."""
        if not self._connected:
            return None
        try:
            self._throttle()
            params = {"name": commodity, "expirydate": expiry} if expiry else {"name": commodity}
            data = self.obj.optionGreek(params)
            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"MCX option greeks error: {e}")
        return None


# ====================================================================
# MAIN PAPER TRADER
# ====================================================================
class CommodityPaperTrader:

    def __init__(self, ws_feed=None):
        self.angel = AngelMCXConnection()
        self.portfolio = CommodityPortfolio()
        self.engine = CommodityStrategyEngine(self.portfolio, self.angel)
        self._running = False
        self.ws_feed = ws_feed  # Real-time WebSocket price feed (optional)
        # Caches to reduce REST API calls
        self._option_ltp_cache = {}  # {cache_key: {'ltp': float, 'time': datetime}}
        # EOD signal tracking
        self.daily_signal_count = 0
        self.daily_signals_all = []  # ALL signals (including skipped) for dummy PnL

    def connect(self):
        connected = self.angel.connect()
        # Share MCX futures tokens with WebSocket feed for subscription
        if connected and self.ws_feed and self.angel._futures_tokens:
            self.ws_feed.set_mcx_tokens(self.angel._futures_tokens)
        return connected

    def get_spot(self, commodity):
        """Get current spot price for commodity.
        Priority: WebSocket cache -> REST API -> historical close.
        """
        # 1. Try WebSocket cache (instant, no API call)
        if self.ws_feed and commodity in self.angel._futures_tokens:
            ws_ltp = self.ws_feed.get_ltp(self.angel._futures_tokens[commodity])
            if ws_ltp:
                return ws_ltp

        # 2. Fallback: REST API
        ltp = self.angel.get_ltp(commodity)
        if ltp:
            return ltp

        # 3. Fallback: historical close
        df = self.engine.historical_data.get(commodity)
        if df is not None and len(df) > 0:
            return df['Close'].iloc[-1]
        return None

    def _get_commodity_option_ltp(self, pos):
        """Get real-time option LTP for a commodity position, with 15s cache.
        Returns LTP float or None if unavailable.
        """
        details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
        option_token = details.get('option_token')

        # Backfill for positions opened before this fix (no stored token)
        if not option_token:
            commodity = pos['commodity']
            opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
            expiry = self._get_nearest_mcx_expiry(commodity)
            if expiry:
                option_info = self.angel.find_option_tokens(commodity, expiry, pos['strike'], opt_type)
                if option_info:
                    option_token = str(option_info.get('token', ''))
                    # Store in position so we don't re-lookup every cycle
                    if isinstance(details, dict):
                        details['option_token'] = option_token
                    else:
                        pos['details'] = {'option_token': option_token}

        if not option_token:
            return None

        # Check cache (15-second TTL)
        cache_key = f"MCX_{option_token}"
        cached = self._option_ltp_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 15:
            return cached['ltp']

        try:
            # Use get_market_data for MCX option LTP (commodity get_ltp is futures only)
            mkt_data = self.angel.get_market_data('MCX', option_token)
            if mkt_data:
                ltp = float(mkt_data.get('ltp', 0) or 0)
                if ltp > 0:
                    self._option_ltp_cache[cache_key] = {'ltp': ltp, 'time': datetime.now()}
                    return ltp
        except Exception as e:
            logger.debug(f"  MCX Option LTP fetch failed for {cache_key}: {e}")

        return None

    def is_mcx_open(self):
        """Check if MCX trading is allowed (after 3:30 PM until 11:30 PM, Mon-Fri)."""
        now = datetime.now()
        if now.weekday() > 4:  # Saturday/Sunday
            return False
        return COMMODITY_TRADE_START <= now.time() <= MCX_CLOSE

    def _get_nearest_mcx_expiry(self, commodity):
        """Get nearest MCX option expiry for a commodity."""
        if self.angel.instruments is None:
            cache = os.path.join(DATA_DIR, 'instrument_master.csv')
            if os.path.exists(cache):
                self.angel.instruments = pd.read_csv(cache, low_memory=False)
            else:
                return None
        mask = (self.angel.instruments['name'] == commodity) & \
               (self.angel.instruments['exch_seg'] == 'MCX') & \
               (self.angel.instruments['instrumenttype'] == 'OPTFUT')
        matches = self.angel.instruments[mask]
        if len(matches) == 0:
            return None
        today = datetime.now().date()
        expiries = []
        for exp_str in matches['expiry'].unique():
            try:
                exp_date = pd.to_datetime(exp_str).date()
                if exp_date >= today:
                    expiries.append(exp_date)
            except Exception:
                continue
        if not expiries:
            return None
        nearest = min(expiries)
        return nearest.strftime('%d%b%Y').upper()

    def fetch_current_oi_iv(self, pos):
        """Fetch current OI and IV for a commodity option position."""
        if not hasattr(self, '_greeks_cache'):
            self._greeks_cache = {}

        commodity = pos['commodity']
        strike = pos['strike']
        opt_type = 'CE' if 'CE' in pos['signal_type'] else 'PE'
        cache_key = f"{commodity}_{strike}_{opt_type}"

        # Check cache (60s TTL)
        cached = self._greeks_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 60:
            return cached['data']

        current_oi = None
        current_iv = None

        try:
            # Method 1: get_market_data for OI
            expiry = self._get_nearest_mcx_expiry(commodity)
            if expiry:
                option_info = self.angel.find_option_tokens(commodity, expiry, strike, opt_type)
                if option_info:
                    token = str(option_info.get('token', ''))
                    mkt_data = self.angel.get_market_data('MCX', token)
                    if mkt_data:
                        current_oi = mkt_data.get('opnInterest', mkt_data.get('oi'))
                        if current_oi is not None:
                            current_oi = float(current_oi)

            # Method 2: get_option_greeks for IV
            greeks_data = self.angel.get_option_greeks(commodity, expiry)
            if greeks_data:
                for item in (greeks_data if isinstance(greeks_data, list) else [greeks_data]):
                    item_strike = float(item.get('strikePrice', 0))
                    item_type = item.get('optionType', '')
                    if abs(item_strike - strike) < 1 and item_type == opt_type:
                        iv_val = item.get('impliedVolatility')
                        if iv_val:
                            current_iv = float(iv_val)
                        break
        except Exception as e:
            logger.info(f"  MCX OI/IV fetch failed for {pos['id']}: {e}")

        # Fallback IV from historical volatility
        if current_iv is None or current_iv == 0:
            df = self.engine.historical_data.get(commodity)
            if df is not None and len(df) > 20:
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                hv = log_ret.tail(20).std() * np.sqrt(252)
                current_iv = max(min(hv * 1.15 * 100, 60), 8)
            else:
                current_iv = pos.get('entry_iv', 0)

        if current_oi is None:
            current_oi = pos.get('entry_oi', 0)

        result = (current_oi, current_iv)
        self._greeks_cache[cache_key] = {'data': result, 'time': datetime.now()}

        if current_oi is not None or current_iv is not None:
            logger.info(f"  MCX OI/IV: {pos['id']} OI={current_oi} IV={current_iv}")

        return result

    def check_oi_iv_exit(self, pos, current_premium):
        """Check if commodity position should exit based on OI/IV changes.
        Returns: (exit_reason, should_reverse) or (None, False)
        """
        entry_oi = pos.get('entry_oi', 0)
        entry_iv = pos.get('entry_iv', 0)

        current_oi, current_iv = self.fetch_current_oi_iv(pos)

        if current_iv is None or current_iv == 0:
            current_iv = entry_iv
        if current_oi is None:
            current_oi = entry_oi

        oi_change = 0
        iv_change = 0

        if entry_oi and entry_oi > 0:
            oi_change = abs(current_oi - entry_oi) / entry_oi * 100
        if entry_iv and entry_iv > 0:
            iv_change = abs(current_iv - entry_iv) / entry_iv * 100

        # Rule 1: OI surge
        if oi_change > MCX_OI_SURGE_PCT:
            should_reverse = oi_change > MCX_OI_REVERSE_PCT
            logger.info(f"  MCX_OI_SURGE: {pos['id']} OI changed {oi_change:.1f}% (entry={entry_oi}, current={current_oi})")
            return 'OI_SURGE_EXIT', should_reverse

        # Rule 2: IV spike
        if iv_change > MCX_IV_SPIKE_PCT:
            should_reverse = iv_change > MCX_IV_REVERSE_PCT
            logger.info(f"  MCX_IV_SPIKE: {pos['id']} IV changed {iv_change:.1f}% (entry={entry_iv}%, current={current_iv}%)")
            return 'IV_SPIKE_EXIT', should_reverse

        # Rule 3: Combined OI+IV
        if oi_change > MCX_OI_IV_COMBO_OI and iv_change > MCX_OI_IV_COMBO_IV:
            logger.info(f"  MCX_OI_IV_COMBO: {pos['id']} OI={oi_change:.1f}%, IV={iv_change:.1f}%. REVERSE!")
            return 'OI_IV_COMBINED_EXIT', True

        # Rule 4: Gamma shield for short positions
        if pos.get('is_sell') and abs(pos.get('gamma', 0)) > MCX_GAMMA_SHIELD_THRESHOLD:
            logger.info(f"  MCX_GAMMA_SHIELD: {pos['id']} gamma={pos.get('gamma', 0):.6f} > {MCX_GAMMA_SHIELD_THRESHOLD}")
            return 'GAMMA_SHIELD_EXIT', False

        return None, False

    def execute_reversal(self, pos, exit_reason):
        """After closing a commodity position due to OI+IV exit, open reverse trade."""
        try:
            commodity = pos['commodity']
            strategy = pos['strategy']
            original_type = pos['signal_type']

            # Don't chain reversals
            if '(Reversal)' in strategy:
                logger.info(f"  REVERSAL_SKIP: {pos['id']} already a reversal trade. No chain.")
                return

            # Determine reverse direction
            if 'CE' in original_type:
                reverse_type = ('BUY_CE_' if pos['is_sell'] else 'SELL_CE') + '_REV'
            else:
                reverse_type = ('BUY_PE_' if pos['is_sell'] else 'SELL_PE') + '_REV'

            logger.info(f"  MCX_REVERSAL: {pos['id']} → Opening {reverse_type} {commodity} "
                       f"@ strike {pos['strike']} due to {exit_reason}")

            # Use 75% of current premium for reversal (reduced risk)
            reversal_premium = pos['current_premium'] * 0.75

            greeks = {
                'delta': pos.get('delta', 0),
                'gamma': pos.get('gamma', 0),
                'theta': pos.get('theta', 0),
                'iv': (pos.get('entry_iv', 15) or 15) / 100,
            }

            reverse_pos = self.portfolio.add_signal(
                strategy=strategy + ' (Reversal)',
                commodity=commodity,
                signal_type=reverse_type,
                strike=pos['strike'],
                entry_premium=reversal_premium,
                greeks=greeks,
                dte=pos.get('dte', 1),
                details={'origin': 'REVERSAL', 'original_id': pos['id'],
                         'exit_reason': exit_reason, 'spot': pos.get('entry_spot', 0)},
                oi=pos.get('entry_oi', 0),
                iv=(pos.get('entry_iv', 15) or 15) / 100,
            )

            if reverse_pos:
                try:
                    from trade_notifier import send_message
                    msg = (f"<b>MCX REVERSAL TRADE</b>\n"
                           f"Closed: {pos['signal_type']} {commodity}\n"
                           f"Opened: {reverse_type} {commodity}\n"
                           f"Reason: {exit_reason}\n"
                           f"Strike: {pos['strike']:.0f}\n"
                           f"Premium: Rs {reversal_premium:.2f}")
                    send_message(msg)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"  MCX Reversal failed for {pos['id']}: {e}")

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
                logger.warning(f"  No indicators for {commodity}")
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
                ('PCR+VWAP', self.engine.check_pcr_vwap_signals),
                ('Survivor', self.engine.check_survivor_signals),
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
            # Track ALL signals for dummy PnL at EOD
            self.daily_signal_count += 1
            self.daily_signals_all.append(sig)

            # Check for duplicate: same strategy + commodity + NEARBY strike + SAME option type
            strike_tolerance = {'GOLDM': 200, 'SILVERM': 1000, 'CRUDEOILM': 100}
            tol = strike_tolerance.get(sig['commodity'], 100)
            sig_opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            existing = [p for p in self.portfolio.positions
                        if p['strategy'] == sig['strategy']
                        and p['commodity'] == sig['commodity']
                        and abs(p['strike'] - sig['strike']) <= tol
                        and (('CE' if 'CE' in p['signal_type'] else 'PE') == sig_opt_type)]
            if existing:
                logger.info(f"  SKIP (duplicate): {sig['strategy']} {sig['commodity']} "
                           f"{sig['strike']}{sig_opt_type} near existing {existing[0]['strike']}")
                skipped += 1
                continue

            # Check for CONFLICTING positions: no BUY_CE + SELL_CE on same commodity/strike
            opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
            is_buy_sig = 'BUY' in sig['type']
            conflicting = [p for p in self.portfolio.positions
                           if p['commodity'] == sig['commodity']
                           and abs(p['strike'] - sig['strike']) <= tol
                           and (('CE' in p['signal_type']) == (opt_type == 'CE'))
                           and p['is_sell'] == is_buy_sig]
            if conflicting:
                logger.info(f"  SKIP (conflicting): {sig['type']} conflicts with "
                           f"{conflicting[0]['signal_type']} on {sig['commodity']} {sig['strike']}")
                skipped += 1
                continue

            # Fetch entry OI + real option LTP from market data
            entry_oi = 0
            option_token = None
            mkt_data = None
            real_ltp = None
            try:
                opt_type = 'CE' if 'CE' in sig['type'] else 'PE'
                expiry = self._get_nearest_mcx_expiry(sig['commodity'])
                if expiry:
                    option_info = self.angel.find_option_tokens(
                        sig['commodity'], expiry, sig['strike'], opt_type
                    )
                    if option_info:
                        option_token = str(option_info.get('token', ''))
                        mkt_data = self.angel.get_market_data('MCX', option_token)
                        if mkt_data:
                            entry_oi = float(mkt_data.get('opnInterest', mkt_data.get('oi', 0)) or 0)
                            logger.info(f"  MCX_ENTRY_OI: {sig['commodity']} {sig['strike']}{opt_type} OI={entry_oi}")
                            # Extract real option LTP from same API response (zero extra calls)
                            fetched_ltp = float(mkt_data.get('ltp', 0) or 0)
                            if fetched_ltp > 0:
                                real_ltp = fetched_ltp
            except Exception as e:
                logger.info(f"  MCX OI/LTP fetch at entry failed for {sig['commodity']}: {e}")

            # Replace BS premium with real market LTP + compute real Greeks
            bs_premium = sig['premium']
            commodity_greeks = sig['greeks']
            if real_ltp and real_ltp > 0:
                # Preserve target/SL ratios from strategy, apply to real premium
                if bs_premium > 0:
                    target_ratio = sig.get('target', bs_premium * 1.5) / bs_premium
                    sl_ratio = sig.get('sl', bs_premium * 0.4) / bs_premium
                else:
                    target_ratio = 1.5
                    sl_ratio = 0.4
                sig['premium'] = real_ltp
                sig['target'] = round(real_ltp * target_ratio, 2)
                sig['sl'] = round(real_ltp * sl_ratio, 2)

                # Back-solve for real implied volatility and compute accurate Greeks (Black-76)
                T = max(sig['dte'], 1) / 365
                spot = sig.get('spot', 0)
                if spot > 0:
                    real_greeks = greeks_from_market_price_b76(
                        real_ltp, spot, sig['strike'], T, RISK_FREE_RATE, opt_type
                    )
                    if real_greeks:
                        commodity_greeks = real_greeks
                        sig['greeks'] = real_greeks
                        logger.info(f"  MCX_REAL_GREEKS: {sig['commodity']} {sig['strike']}{opt_type} "
                                   f"IV={real_greeks['iv']*100:.1f}% Δ={real_greeks['delta']:.3f} "
                                   f"Γ={real_greeks['gamma']:.6f} Θ={real_greeks['theta']:.2f}")
                    else:
                        logger.info(f"  MCX_REAL_GREEKS: IV solve failed, using Black-76 Greeks")
                logger.info(f"  MCX_REAL_LTP: {sig['commodity']} {sig['strike']}{opt_type} "
                           f"BS={bs_premium:.2f} → Market={real_ltp:.2f} "
                           f"Target={sig['target']:.2f} SL={sig['sl']:.2f}")
            else:
                logger.warning(f"  MCX_REAL_LTP: Unavailable for {sig['commodity']} {sig['strike']}{opt_type}, "
                              f"using Black-76 premium Rs {bs_premium:.2f}")

            result = self.portfolio.add_signal(
                strategy=sig['strategy'],
                commodity=sig['commodity'],
                signal_type=sig['type'],
                strike=sig['strike'],
                entry_premium=sig['premium'],
                greeks=sig['greeks'],
                dte=sig['dte'],
                details={'reason': sig['reason'], 'target': sig.get('target'),
                         'sl': sig.get('sl'), 'spot': sig.get('spot'),
                         'option_token': option_token},
                oi=entry_oi,
                iv=sig['greeks'].get('iv', 0),
            )
            if result is None:
                skipped += 1
                continue
            executed += 1

            # Send Telegram notification
            try:
                from trade_notifier import notify_trade_entry
                logger.info(f"  TELEGRAM: Sending entry notification for {sig['commodity']} {sig['type']}")
                spec = COMMODITIES[sig['commodity']]
                # Capital used: SELL = margin blocked, BUY = premium paid
                is_sell_sig = 'SELL' in sig['type']
                if is_sell_sig:
                    capital = spec['margin']
                else:
                    capital = sig['premium'] * spec['lot_size'] * spec['multiplier']
                # Calculate total locked (BUY/SELL aware) and available capital
                locked = 0
                for p in self.portfolio.positions:
                    p_spec = COMMODITIES[p['commodity']]
                    if p['is_sell']:
                        locked += p_spec['margin']
                    else:
                        locked += p['entry_premium'] * p['lot_size'] * p['multiplier']
                total_invested = locked
                capital_available = COMMODITY_CAPITAL - locked
                notify_trade_entry(
                    market="COMMODITY", strategy=sig['strategy'],
                    symbol=sig['commodity'], signal_type=sig['type'],
                    strike=sig['strike'], entry_price=sig['premium'],
                    spot=sig.get('spot', 0), lot_size=spec['lot_size'],
                    multiplier=spec['multiplier'], delta=sig['greeks'].get('delta', 0),
                    target=sig.get('target', 0), sl=sig.get('sl', 0),
                    capital_used=capital, reason=sig['reason'],
                    capital_available=capital_available,
                    total_invested=total_invested,
                )
            except Exception as e:
                logger.warning(f"  Telegram notify failed: {e}")

        if skipped:
            logger.info(f"  Executed: {executed} | Skipped (duplicates): {skipped}")

    def check_exits(self):
        if not self.portfolio.positions:
            return

        logger.info("\n  Checking commodity exits for open positions...")

        # ---- CIRCUIT BREAKER: Close ALL if daily loss exceeds limit ----
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.portfolio.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"  MCX_CIRCUIT_BREAKER: Daily loss Rs {daily_loss:,.0f} > limit Rs {MAX_DAILY_LOSS:,.0f}")
            for pos in list(self.portfolio.positions):
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'CIRCUIT_BREAKER')
            try:
                from trade_notifier import send_message
                send_message(f"<b>MCX CIRCUIT BREAKER</b>\nDaily loss: Rs {daily_loss:,.0f}\nAll commodity positions closed.")
            except Exception:
                pass
            self.portfolio.save_state()
            return

        for pos in list(self.portfolio.positions):
            # Skip positions opened less than 5 minutes ago (grace period)
            entry_time = datetime.fromisoformat(pos['timestamp'])
            if (datetime.now() - entry_time).total_seconds() < 300:
                continue

            # ---- EOD FORCE CLOSE: 15 min before MCX close (23:15) ----
            if datetime.now().time() > dtime(23, 15):
                current = pos.get('current_premium', pos['entry_premium'])
                logger.info(f"  MCX_EOD_CLOSE: {pos['id']} force close (MCX closing at 23:30)")
                self.portfolio.close_position(pos['id'], current, 'EOD_FORCE_CLOSE')
                self._notify_commodity_exit(pos, pos['commodity'], current, 'EOD_FORCE_CLOSE')
                continue

            commodity = pos['commodity']
            spot = self.get_spot(commodity)
            if not spot:
                continue

            spec = COMMODITIES[commodity]

            # ---- PREMIUM UPDATE: Real market LTP with fallback to delta+gamma+theta ----
            entry_spot = pos.get('entry_spot', 0)
            if entry_spot == 0:
                entry_spot = pos.get('details', {}).get('spot', spot)
            spot_change = spot - entry_spot
            delta_val = pos.get('delta', 0.5)
            gamma_val = pos.get('gamma', 0)
            hours_held = (datetime.now() - entry_time).total_seconds() / 3600

            # Try real option LTP first (15s cached)
            real_option_ltp = self._get_commodity_option_ltp(pos)
            if real_option_ltp is not None:
                current = real_option_ltp
                premium_source = 'MARKET'
            else:
                # Fallback: delta+gamma+theta approximation
                theta_val = pos.get('theta', 0)
                premium_delta = delta_val * spot_change + 0.5 * gamma_val * (spot_change ** 2)
                time_decay = theta_val * (hours_held / 24)
                current = max(pos['entry_premium'] + premium_delta + time_decay, 0.05)
                premium_source = 'APPROX'
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

            # ---- TIME-BASED EXIT: Close stale commodity positions (>6 hours with <5% profit) ----
            if hours_held > 6:
                profit_pct = (pos['unrealized_pnl'] / max(pos['entry_premium'] * lot * mult, 1)) * 100
                if abs(profit_pct) < 5:
                    logger.info(f"  MCX_TIME_EXIT: {pos['id']} held {hours_held:.1f}h with only {profit_pct:.1f}% profit")
                    self.portfolio.close_position(pos['id'], current, 'TIME_EXIT_NO_PROGRESS')
                    self._notify_commodity_exit(pos, commodity, current, 'TIME_EXIT_NO_PROGRESS')
                    continue

            details = pos.get('details', {}) if isinstance(pos.get('details'), dict) else {}
            target = details.get('target', pos['entry_premium'] * 2)
            sl = details.get('sl', pos['entry_premium'] * 0.3)

            # ---- TRAILING STOP LOSS UPDATE ----
            if not pos['is_sell']:
                # BUY positions: premium going UP is profit
                target_distance = target - pos['entry_premium']
                current_profit_pct = ((current - pos['entry_premium']) / target_distance * 100
                                      if target_distance > 0 else 0)
                if current > pos.get('peak_premium', pos['entry_premium']):
                    pos['peak_premium'] = round(current, 2)

                if current_profit_pct >= TSL_BREAKEVEN_TRIGGER_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(pos['entry_premium'] * 1.01, 2)
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} locked SL at Rs {pos['trailing_sl']:.2f}")

                if current_profit_pct >= TSL_TRAIL_TRIGGER_PCT:
                    peak = pos.get('peak_premium', current)
                    profit_from_entry = peak - pos['entry_premium']
                    new_tsl = round(peak - (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    new_tsl = max(new_tsl, pos.get('trailing_sl') or 0)
                    if new_tsl > (pos.get('trailing_sl') or 0):
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SL→Rs {pos['trailing_sl']:.2f} (peak={peak:.2f})")

                if pos.get('trailing_sl') and current <= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} premium {current:.2f} <= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current, 'TRAILING_SL_HIT')
                    self._notify_commodity_exit(pos, commodity, current, 'TRAILING_SL_HIT')
                    continue
            else:
                # SELL positions: premium going DOWN is profit
                target_distance = pos['entry_premium'] - target
                current_profit_pct = ((pos['entry_premium'] - current) / target_distance * 100
                                      if target_distance > 0 else 0)
                if current < pos.get('trough_premium', pos['entry_premium']):
                    pos['trough_premium'] = round(current, 2)

                if current_profit_pct >= TSL_BREAKEVEN_TRIGGER_PCT and not pos.get('breakeven_locked'):
                    pos['breakeven_locked'] = True
                    pos['trailing_sl'] = round(pos['entry_premium'] * 0.99, 2)
                    logger.info(f"  TSL_BREAKEVEN: {pos['id']} SELL locked SL at Rs {pos['trailing_sl']:.2f}")

                if current_profit_pct >= TSL_TRAIL_TRIGGER_PCT:
                    trough = pos.get('trough_premium', current)
                    profit_from_entry = pos['entry_premium'] - trough
                    new_tsl = round(trough + (profit_from_entry * TSL_TRAIL_DISTANCE_PCT / 100), 2)
                    if pos.get('trailing_sl') is None or new_tsl < pos['trailing_sl']:
                        pos['trailing_sl'] = new_tsl
                        logger.info(f"  TSL_TRAIL: {pos['id']} SELL SL→Rs {pos['trailing_sl']:.2f} (trough={trough:.2f})")

                if pos.get('trailing_sl') and current >= pos['trailing_sl']:
                    logger.info(f"  TRAILING_SL_HIT: {pos['id']} SELL premium {current:.2f} >= TSL {pos['trailing_sl']:.2f}")
                    self.portfolio.close_position(pos['id'], current, 'TRAILING_SL_HIT')
                    self._notify_commodity_exit(pos, commodity, current, 'TRAILING_SL_HIT')
                    continue

            # ---- OI+IV DYNAMIC EXIT CHECK ----
            oi_iv_reason, should_reverse = self.check_oi_iv_exit(pos, current)
            if oi_iv_reason:
                self.portfolio.close_position(pos['id'], current, oi_iv_reason)
                self._notify_commodity_exit(pos, commodity, current, oi_iv_reason)
                if should_reverse:
                    self.execute_reversal(pos, oi_iv_reason)
                continue

            # ---- STATIC EXIT CHECK ----
            logger.info(f"  EXIT_CHECK: {pos['id']} | Entry: {pos['entry_premium']:.2f} → Current: {current:.2f} [{premium_source}] | "
                       f"Target: {target:.2f} SL: {sl:.2f} | TSL: {pos.get('trailing_sl', 'N/A')} | "
                       f"Spot: {entry_spot:.0f}→{spot:.0f} Δ={spot_change:+.0f}")

            exit_reason = None
            if pos['is_sell']:
                if current <= target: exit_reason = 'TARGET'
                elif current >= sl: exit_reason = 'SL'
            else:
                if current >= target: exit_reason = 'TARGET'
                elif current <= sl: exit_reason = 'SL'

            if exit_reason:
                self.portfolio.close_position(pos['id'], current, exit_reason)
                self._notify_commodity_exit(pos, commodity, current, exit_reason)

        # Persist updated PnL to disk after exit check cycle
        self.portfolio.save_state()

    def _notify_commodity_exit(self, pos, commodity, current, exit_reason):
        """Helper to send Telegram exit notification for commodity trades."""
        try:
            from trade_notifier import notify_trade_exit
            spec = COMMODITIES[commodity]
            if pos['is_sell']:
                capital = spec['margin']
            else:
                capital = pos['entry_premium'] * spec['lot_size'] * spec['multiplier']
            locked = 0
            for p in self.portfolio.positions:
                p_spec = COMMODITIES[p['commodity']]
                if p['is_sell']:
                    locked += p_spec['margin']
                else:
                    locked += p['entry_premium'] * p['lot_size'] * p['multiplier']
            notify_trade_exit(
                market="COMMODITY", strategy=pos['strategy'],
                symbol=commodity, signal_type=pos['signal_type'],
                strike=pos['strike'], entry_price=pos['entry_premium'],
                exit_price=current, entry_time=pos['timestamp'],
                pnl=pos['unrealized_pnl'], capital_used=capital,
                exit_reason=exit_reason,
                capital_available=COMMODITY_CAPITAL - locked,
                total_invested=locked,
            )
        except Exception as e:
            logger.warning(f"  Telegram exit notify failed: {e}")

    def get_eod_summary(self):
        """Return end-of-day summary with actual PnL (our trades) + dummy PnL (all signals)."""
        actual_closed_pnl = sum(t.get('pnl', 0) for t in self.portfolio.closed_trades)
        actual_open_pnl = sum(p.get('unrealized_pnl', 0) for p in self.portfolio.positions)
        actual_total = actual_closed_pnl + actual_open_pnl

        # Dummy PnL: estimate for ALL signals as if unlimited capital
        dummy_pnl = 0
        for sig in self.daily_signals_all:
            spot_now = self.get_spot(sig['commodity']) or sig.get('spot', 0)
            entry_spot = sig.get('spot', spot_now)
            if not entry_spot or not spot_now:
                continue
            spot_change = spot_now - entry_spot
            delta = sig.get('greeks', {}).get('delta', 0.5)
            premium_change = delta * spot_change
            spec = COMMODITIES.get(sig['commodity'], {})
            lot = spec.get('lot_size', 1)
            mult = spec.get('multiplier', 1)
            dummy_pnl += premium_change * lot * mult

        capital_used = 0
        for p in self.portfolio.positions:
            spec = COMMODITIES.get(p['commodity'], {})
            if p.get('is_sell', False):
                capital_used += spec.get('margin', 15000)
            else:
                capital_used += p['entry_premium'] * p['lot_size'] * p['multiplier']

        return {
            'total_signals': self.daily_signal_count,
            'trades_taken': len(self.portfolio.closed_trades) + len(self.portfolio.positions),
            'open_positions': len(self.portfolio.positions),
            'closed_trades': len(self.portfolio.closed_trades),
            'actual_pnl': round(actual_total, 2),
            'dummy_pnl': round(dummy_pnl, 2),
            'capital_used': round(capital_used, 2),
        }

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
