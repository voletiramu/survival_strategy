"""
OI-BASED PAPER TRADING SYSTEM v1.0
====================================
3 strategies: OI Wall Bouncer, Max Pain Magnet, VWAP Bounce
3 indices: NIFTY, BANKNIFTY, SENSEX
BUY-only (no selling). Defined risk (premium = max loss).
Capital: Rs 3,00,000

Runs independently from equity/commodity and stock bots.
"""

import sys
import os
import json
import time
import logging
from datetime import datetime, timedelta, time as dtime
from collections import Counter

import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OI_PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_oi')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
OPTIONS_DIR = os.path.join(BASE_DIR, 'data', 'options')

for d in [OI_PAPER_DIR, LOG_DIR, OPTIONS_DIR]:
    os.makedirs(d, exist_ok=True)

logger = logging.getLogger('OITrader')

# ====================================================================
# CONFIGURATION
# ====================================================================
INITIAL_CAPITAL = 300000            # Rs 3L
LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20}
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}
SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']

# Risk limits
MAX_POSITIONS = 4                   # Max concurrent OI positions
MAX_POSITIONS_PER_SYMBOL = 2        # Max per symbol
MAX_DAILY_LOSS = 30000              # Rs 30K daily loss limit (10% of capital)
MAX_TRADES_PER_DAY = 10             # Hard cap
MIN_SIGNAL_SCORE = 50               # Quality threshold

# Transaction costs
BROKERAGE = 20                      # Rs 20 per order
STT_SELL = 0.000625                 # STT on sell side
EXCHANGE_CHARGES = 0.0005           # Exchange + SEBI
GST_RATE = 0.18                     # GST on brokerage + exchange

# Market hours (IST)
FIRST_TRADE_TIME = dtime(9, 30)     # Skip first 15 min (inflated premiums)
LAST_ENTRY_TIME = dtime(14, 30)     # No new entries after 2:30 PM
EOD_CLOSE_TIME = dtime(15, 20)      # Force close at 3:20 PM
GRACE_PERIOD_SECONDS = 180          # 3 min grace before exit checks

# Angel API credentials
ANGEL_CRED_FILE = os.environ.get(
    'ANGEL_CRED_FILE',
    os.path.join(os.path.dirname(BASE_DIR), 'Angel',
                 'ANGEL_API_KEY=your_api_key.txt'))

# Index tokens for Angel API
INDEX_TOKENS = {
    'NIFTY': {'exchange': 'NSE', 'token': '99926000'},
    'BANKNIFTY': {'exchange': 'NSE', 'token': '99926009'},
    'SENSEX': {'exchange': 'BSE', 'token': '99919000'},
}


# ====================================================================
# TRANSACTION COST CALCULATOR
# ====================================================================
def compute_costs(entry_premium, exit_premium, lot_size):
    """Compute total transaction costs (brokerage + taxes)."""
    turnover = (entry_premium + exit_premium) * lot_size
    brokerage = min(BROKERAGE * 2, turnover * 0.0025)  # Rs 20 per side, capped
    stt = exit_premium * lot_size * STT_SELL
    exchange_ch = turnover * EXCHANGE_CHARGES
    gst = (brokerage + exchange_ch) * GST_RATE
    return round(brokerage + stt + exchange_ch + gst, 2)


# ====================================================================
# OI PORTFOLIO — Track positions and P&L
# ====================================================================
class OIPortfolio:
    """Portfolio manager for OI paper trading."""

    def __init__(self, capital=INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.positions = []
        self.closed_trades = []
        self.daily_pnl = {}
        self._load_state()

    def _state_file(self):
        return os.path.join(OI_PAPER_DIR, 'portfolio_state.json')

    def _load_state(self):
        """Load portfolio state from JSON."""
        path = self._state_file()
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                self.capital = data.get('capital', self.initial_capital)
                self.positions = data.get('positions', [])
                self.closed_trades = data.get('closed_trades', [])
                self.daily_pnl = data.get('daily_pnl', {})
                logger.info(f"[OIPortfolio] Loaded: capital={self.capital:.0f}, "
                            f"open={len(self.positions)}, closed={len(self.closed_trades)}")
            except Exception as e:
                logger.error(f"[OIPortfolio] Load failed: {e}")

    def save_state(self):
        """Save portfolio state to JSON."""
        data = {
            'capital': self.capital,
            'positions': self.positions,
            'closed_trades': self.closed_trades,
            'daily_pnl': self.daily_pnl,
            'last_updated': datetime.now().isoformat(),
        }
        path = self._state_file()
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[OIPortfolio] Save failed: {e}")

    def open_position(self, signal, daily_trade_count=0):
        """Open a position from an OI signal dict.

        Returns:
            Position dict if opened, None if rejected.
        """
        symbol = signal['symbol']
        lot_size = signal.get('lot_size', LOT_SIZES.get(symbol, 75))
        premium = signal['entry_premium']

        # --- Risk checks ---
        # 1. Max positions
        if len(self.positions) >= MAX_POSITIONS:
            logger.info(f"[OIPortfolio] Skip: max {MAX_POSITIONS} positions reached")
            return None

        # 2. Max per symbol
        sym_count = sum(1 for p in self.positions if p['symbol'] == symbol)
        if sym_count >= MAX_POSITIONS_PER_SYMBOL:
            logger.info(f"[OIPortfolio] Skip: max {MAX_POSITIONS_PER_SYMBOL} positions for {symbol}")
            return None

        # 3. Daily trade limit
        if daily_trade_count >= MAX_TRADES_PER_DAY:
            logger.info(f"[OIPortfolio] Skip: max {MAX_TRADES_PER_DAY} trades/day reached")
            return None

        # 4. Daily loss limit
        today = datetime.now().strftime('%Y-%m-%d')
        if self.daily_pnl.get(today, 0) < -MAX_DAILY_LOSS:
            logger.info(f"[OIPortfolio] Skip: daily loss limit {MAX_DAILY_LOSS} breached")
            return None

        # 5. Capital check
        cost = premium * lot_size
        if cost > self.capital * 0.25:  # Max 25% of capital per trade
            # Try smaller lot
            lot_size = LOT_SIZES.get(symbol, 75)  # 1 lot
            cost = premium * lot_size
        if cost > self.capital:
            logger.info(f"[OIPortfolio] Skip: insufficient capital ({cost:.0f} > {self.capital:.0f})")
            return None

        # 6. Duplicate check (same strategy + symbol + strike)
        for p in self.positions:
            if (p['strategy'] == signal['strategy_type'] and
                    p['symbol'] == symbol and
                    p['strike'] == signal['strike']):
                return None

        # --- Create position ---
        entry_cost = compute_costs(premium, premium, lot_size)  # Estimate round-trip
        num_lots = max(1, lot_size // LOT_SIZES.get(symbol, 75))

        pos = {
            'id': f"OI_{signal['strategy_type'].replace(' ', '_')}_{symbol}_"
                  f"{datetime.now().strftime('%H%M%S')}",
            'timestamp': datetime.now().isoformat(),
            'strategy': signal['strategy_type'],
            'symbol': symbol,
            'signal_type': signal['signal_type'],
            'strike': signal['strike'],
            'opt_type': signal.get('opt_type', 'CE' if 'CE' in signal['signal_type'] else 'PE'),
            'entry_premium': premium,
            'current_premium': premium,
            'target': signal['target'],
            'sl': signal['sl'],
            'lot_size': lot_size,
            'num_lots': num_lots,
            'is_sell': False,  # OI strategies are BUY-only
            'entry_cost': entry_cost,
            'greeks': signal.get('greeks', {}),
            'delta': signal.get('greeks', {}).get('delta', 0),
            'gamma': signal.get('greeks', {}).get('gamma', 0),
            'theta': signal.get('greeks', {}).get('theta', 0),
            'iv': signal.get('iv', 0),
            'entry_iv': signal.get('iv', 0),
            'dte': signal.get('dte', 5),
            'vix': signal.get('vix', 15),
            'quality_score': signal.get('quality_score', 0),
            'entry_spot': signal.get('spot', 0),
            'entry_oi': signal.get('wall_oi', 0) or signal.get('max_pain', 0),
            'unrealized_pnl': 0,
            'max_risk': premium * lot_size,
            'peak_premium': premium,
            'trough_premium': premium,
            'trailing_sl': None,
            'breakeven_locked': False,
            'hold_score': 50,
            'status': 'OPEN',
            'reason': signal.get('reason', ''),
            # Strategy-specific fields
            'wall_strike': signal.get('wall_strike', 0),
            'wall_oi': signal.get('wall_oi', 0),
            'max_pain': signal.get('max_pain', 0),
            'vwap': signal.get('vwap', 0),
            'distance_pct': signal.get('distance_pct', 0),
            'oi_confirmed': signal.get('oi_confirmed', False),
            'details': {
                'target': signal['target'],
                'sl': signal['sl'],
                'spot': signal.get('spot', 0),
            },
            'capital_available': self.capital,
        }

        self.positions.append(pos)
        self.capital -= cost  # Reserve capital (premium paid)

        logger.info(f"[OIPortfolio] OPENED: {pos['id']} | {signal['strategy_type']} "
                    f"{symbol} {signal['signal_type']} @ {premium:.2f} | "
                    f"Strike={signal['strike']} | Score={signal.get('quality_score', 0)} | "
                    f"Target={signal['target']:.2f} SL={signal['sl']:.2f}")
        self.save_state()
        return pos

    def close_position(self, pos_id, exit_premium, reason=''):
        """Close a position and record P&L."""
        pos = None
        for p in self.positions:
            if p['id'] == pos_id:
                pos = p
                break
        if not pos:
            return None

        lot_size = pos['lot_size']
        entry_premium = pos['entry_premium']

        # P&L = (exit - entry) * lot_size - costs
        costs = compute_costs(entry_premium, exit_premium, lot_size)
        raw_pnl = (exit_premium - entry_premium) * lot_size
        pnl = round(raw_pnl - costs, 2)

        # Move to closed trades
        pos['exit_premium'] = exit_premium
        pos['exit_cost'] = costs
        pos['pnl'] = pnl
        pos['exit_reason'] = reason
        pos['exit_time'] = datetime.now().isoformat()
        pos['status'] = 'CLOSED'
        pos['current_premium'] = exit_premium

        self.positions.remove(pos)
        self.closed_trades.append(pos)

        # Update capital
        self.capital += (entry_premium * lot_size) + pnl  # Return reserved + profit/loss

        # Track daily P&L
        today = datetime.now().strftime('%Y-%m-%d')
        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + pnl
        pos['capital_after'] = self.capital

        emoji = '+' if pnl >= 0 else ''
        logger.info(f"[OIPortfolio] CLOSED: {pos_id} | {reason} | "
                    f"PnL: {emoji}{pnl:.2f} | Entry={entry_premium:.2f} "
                    f"Exit={exit_premium:.2f} | Capital={self.capital:.0f}")
        self.save_state()
        return pos

    def update_position(self, pos_id, current_premium):
        """Update unrealized PnL for a position."""
        for pos in self.positions:
            if pos['id'] == pos_id:
                pos['current_premium'] = current_premium
                lot_size = pos['lot_size']
                entry = pos['entry_premium']
                costs = pos.get('entry_cost', 0)
                pos['unrealized_pnl'] = round(
                    (current_premium - entry) * lot_size - costs, 2)
                # Update peak
                if current_premium > pos.get('peak_premium', entry):
                    pos['peak_premium'] = current_premium
                return pos
        return None

    def print_status(self):
        """Print portfolio summary."""
        total_unrealized = sum(p.get('unrealized_pnl', 0) for p in self.positions)
        today = datetime.now().strftime('%Y-%m-%d')
        daily = self.daily_pnl.get(today, 0)

        logger.info(f"[OIPortfolio] Capital: {self.capital:.0f} | "
                    f"Open: {len(self.positions)} | "
                    f"Unrealized: {total_unrealized:+.0f} | "
                    f"Today PnL: {daily:+.0f}")

        for p in self.positions:
            pnl = p.get('unrealized_pnl', 0)
            held = ''
            try:
                entry_time = datetime.fromisoformat(p['timestamp'])
                mins = (datetime.now() - entry_time).total_seconds() / 60
                held = f" ({mins:.0f}m)"
            except Exception:
                pass
            logger.info(f"  {p['strategy']} {p['symbol']} {p['signal_type']} "
                        f"@ {p['entry_premium']:.1f} → {p['current_premium']:.1f} "
                        f"PnL={pnl:+.0f}{held}")


# ====================================================================
# OI PAPER TRADER — Main orchestrator
# ====================================================================
class OIPaperTrader:
    """OI-based paper trading orchestrator."""

    def __init__(self, market_pipeline=None):
        self.portfolio = OIPortfolio()
        self.angel = None
        self.pipeline = market_pipeline
        self.historical_data = {}       # symbol -> DataFrame
        self._option_ltp_cache = {}     # {key: {ltp, time}}
        self._current_dte = {}          # {symbol: int}
        self._current_iv = {}           # {symbol: float}
        self.daily_signal_count = 0
        self.daily_trade_count = 0
        self._last_heartbeat = None

    def initialize(self):
        """Connect to Angel API and load historical data."""
        # Import AngelConnection from paper_trader
        try:
            from paper_trader import AngelConnection
            self.angel = AngelConnection()
            if self.angel.connect('Historical'):
                self.angel.load_instruments()
                logger.info("[OITrader] Angel API connected")
            else:
                logger.warning("[OITrader] Angel API connection failed — "
                               "will use pipeline data only")
                self.angel = None
        except Exception as e:
            logger.warning(f"[OITrader] Angel init failed: {e}")
            self.angel = None

        # Load historical daily data for indicators
        for symbol in SYMBOLS:
            self._load_historical(symbol)

        return True

    def _load_historical(self, symbol):
        """Load historical OHLCV for indicator computation."""
        csv_path = os.path.join(OPTIONS_DIR, f'{symbol}_spot_one_day_2000d.csv')
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path, parse_dates=['DateTime'])
                self.historical_data[symbol] = df
                logger.info(f"[OITrader] Loaded {len(df)} bars for {symbol}")
            except Exception as e:
                logger.warning(f"[OITrader] Failed to load {symbol} history: {e}")
        else:
            # Try to fetch from Angel API
            if self.angel:
                try:
                    info = INDEX_TOKENS.get(symbol)
                    if info:
                        to_date = datetime.now().strftime('%Y-%m-%d 15:30')
                        from_date = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d 09:15')
                        data = self.angel.get_historical(
                            info['exchange'], info['token'],
                            'ONE_DAY', from_date, to_date)
                        if data:
                            df = pd.DataFrame(data, columns=[
                                'DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'])
                            df['DateTime'] = pd.to_datetime(df['DateTime'])
                            df.to_csv(csv_path, index=False)
                            self.historical_data[symbol] = df
                            logger.info(f"[OITrader] Fetched {len(df)} bars for {symbol}")
                except Exception as e:
                    logger.warning(f"[OITrader] Historical fetch failed for {symbol}: {e}")

    def _flatten_chain_data(self, chain):
        """Convert pipeline chain format to oi_strategies flat format.

        Pipeline: {'CE': [{strike, oi, ltp, iv, ...}], 'PE': [...]}
        OI strategies: [{strikePrice, optionType, openInterest, ltp, ...}, ...]
        """
        flat = []
        for item in chain.get('CE', []):
            flat.append({
                'strikePrice': item.get('strike', 0),
                'optionType': 'CE',
                'openInterest': item.get('oi', 0),
                'ltp': item.get('ltp', 0),
                'iv': item.get('iv', 0),
                'volume': item.get('volume', 0),
                'oi_change': item.get('oi_change', 0),
            })
        for item in chain.get('PE', []):
            flat.append({
                'strikePrice': item.get('strike', 0),
                'optionType': 'PE',
                'openInterest': item.get('oi', 0),
                'ltp': item.get('ltp', 0),
                'iv': item.get('iv', 0),
                'volume': item.get('volume', 0),
                'oi_change': item.get('oi_change', 0),
            })
        return flat

    def _compute_dte(self, chain):
        """Compute days to expiry from chain expiry string."""
        expiry_str = chain.get('expiry', '')
        if not expiry_str:
            return 5
        for fmt in ['%d-%b-%Y', '%d %b %Y', '%d%b%Y', '%d-%B-%Y']:
            try:
                exp_date = datetime.strptime(expiry_str, fmt).date()
                dte = (exp_date - datetime.now().date()).days
                return max(0, dte)
            except ValueError:
                continue
        return 5

    def _get_iv_from_chain(self, chain_flat, spot, strike_interval):
        """Extract average ATM IV from chain data."""
        atm = round(spot / strike_interval) * strike_interval
        ivs = []
        for item in chain_flat:
            s = item.get('strikePrice', 0)
            iv = item.get('iv', 0)
            if abs(s - atm) <= strike_interval and iv > 0:
                # Normalize: if iv > 1, it's in percentage form
                iv_dec = iv / 100 if iv > 1 else iv
                ivs.append(iv_dec)
        return sum(ivs) / len(ivs) if ivs else 0.15

    def get_spot(self, symbol):
        """Get spot price. Priority: pipeline > Angel API > historical."""
        if self.pipeline:
            spot = self.pipeline.get_spot(symbol)
            if spot and spot > 0:
                return spot

        if self.angel:
            info = INDEX_TOKENS.get(symbol)
            if info:
                ltp = self.angel.get_ltp(info['exchange'], info['token'])
                if ltp and ltp > 0:
                    return ltp

        df = self.historical_data.get(symbol)
        if df is not None and len(df) > 0:
            return float(df['Close'].iloc[-1])
        return None

    def get_option_ltp(self, symbol, strike, opt_type):
        """Get real-time option LTP. 3-source fallback."""
        # Source 1: Chain LTP from pipeline
        if self.pipeline:
            chain = self.pipeline.get_option_chain(symbol)
            if chain:
                contracts = chain.get('CE' if opt_type == 'CE' else 'PE', [])
                for c in contracts:
                    if c.get('strike') == strike and c.get('ltp', 0) > 0:
                        return c['ltp']

        # Source 2: Angel API with 15s cache
        cache_key = f"{symbol}_{strike}_{opt_type}"
        cached = self._option_ltp_cache.get(cache_key)
        if cached and (datetime.now() - cached['time']).total_seconds() < 15:
            return cached['ltp']

        if self.angel:
            option_info = self.angel.find_option_tokens(symbol, None, strike, opt_type)
            if option_info:
                exchange = 'BFO' if symbol == 'SENSEX' else 'NFO'
                token = str(option_info.get('token', ''))
                ltp = self.angel.get_ltp(exchange, token)
                if ltp and ltp > 0:
                    self._option_ltp_cache[cache_key] = {
                        'ltp': ltp, 'time': datetime.now()}
                    return ltp

        # Source 3: BS re-pricing from spot + IV
        spot = self.get_spot(symbol)
        if spot:
            try:
                from strategies.greeks import bs_greeks
                T = max(self._current_dte.get(symbol, 5), 0.5) / 365
                iv = self._current_iv.get(symbol, 0.15)
                g = bs_greeks(spot, strike, T, 0.065, iv, opt_type)
                return g.get('price', 0)
            except Exception:
                pass

        return None

    def scan_signals(self):
        """Scan all symbols for OI-based signals.

        Returns:
            List of signal dicts sorted by quality_score descending.
        """
        from strategies.oi_strategies import check_all_oi_signals
        from strategies.indicators import compute_all_indicators

        all_signals = []

        for symbol in SYMBOLS:
            spot = self.get_spot(symbol)
            if not spot or spot <= 0:
                logger.debug(f"[OITrader] {symbol}: No spot price, skipping")
                continue

            # Get chain data from pipeline (required for OI strategies)
            if not self.pipeline:
                logger.debug(f"[OITrader] {symbol}: No pipeline, skipping")
                continue

            chain = self.pipeline.get_option_chain(symbol)
            if not chain or not chain.get('CE') or not chain.get('PE'):
                logger.debug(f"[OITrader] {symbol}: No chain data, skipping")
                continue

            chain_flat = self._flatten_chain_data(chain)
            if not chain_flat:
                continue

            dte = self._compute_dte(chain)
            strike_iv = STRIKE_INTERVALS.get(symbol, 50)
            iv = self._get_iv_from_chain(chain_flat, spot, strike_iv)

            self._current_dte[symbol] = dte
            self._current_iv[symbol] = iv

            # Compute indicators from historical data
            df = self.historical_data.get(symbol)
            indicators = {}
            if df is not None and len(df) >= 5:
                try:
                    indicators = compute_all_indicators(df) or {}
                except Exception as e:
                    logger.warning(f"[OITrader] {symbol} indicators error: {e}")

            # Override PCR with real pipeline data
            snapshot = self.pipeline.get_snapshot(symbol) if self.pipeline else {}
            if snapshot and snapshot.get('pcr'):
                indicators['pcr'] = snapshot['pcr']

            vix = self.pipeline.get_vix() if self.pipeline else 15
            lot_size = LOT_SIZES.get(symbol, 75)

            # Generate signals
            try:
                signals = check_all_oi_signals(
                    symbol, spot, chain_flat, indicators, iv, dte,
                    lot_size, vix=vix or 15)
            except Exception as e:
                logger.error(f"[OITrader] {symbol} signal error: {e}", exc_info=True)
                signals = []

            for sig in signals:
                all_signals.append(sig)
                self.daily_signal_count += 1

            if signals:
                best = signals[0]
                logger.info(f"[OITrader] {symbol}: {len(signals)} OI signal(s) — "
                            f"best: {best['strategy_type']} {best['signal_type']} "
                            f"score={best['quality_score']}")
            else:
                logger.info(f"[OITrader] {symbol}: No OI signals "
                            f"(spot={spot:.0f} DTE={dte} IV={iv:.1%})")

        all_signals.sort(key=lambda s: s.get('quality_score', 0), reverse=True)
        return all_signals

    def execute_signals(self, signals):
        """Execute the best signals within risk limits."""
        now = datetime.now().time()

        if now < FIRST_TRADE_TIME:
            return
        if now > LAST_ENTRY_TIME:
            return

        for sig in signals:
            if self.daily_trade_count >= MAX_TRADES_PER_DAY:
                break

            result = self.portfolio.open_position(sig, self.daily_trade_count)
            if result:
                self.daily_trade_count += 1

                # Telegram notification
                try:
                    from trade_notifier import notify_trade_entry
                    notify_trade_entry(
                        market="OI",
                        strategy=sig['strategy_type'],
                        symbol=sig['symbol'],
                        signal_type=sig['signal_type'],
                        strike=sig['strike'],
                        entry_price=sig['entry_premium'],
                        target=sig['target'],
                        sl=sig['sl'],
                        quality_score=sig.get('quality_score', 0),
                        reason=sig.get('reason', ''),
                    )
                except Exception:
                    pass

    def check_exits(self):
        """Check exit conditions for all open positions."""
        from strategies.exit_engine import (
            compute_tsl, check_tsl_hit, check_target_hit,
            check_sl_hit, check_max_loss, check_time_exit,
            check_signal_weak_exit
        )
        from strategies.hold_scoring import compute_hold_score

        if not self.portfolio.positions:
            return

        # Circuit breaker
        today = datetime.now().strftime('%Y-%m-%d')
        daily_loss = self.portfolio.daily_pnl.get(today, 0)
        if daily_loss < -MAX_DAILY_LOSS:
            logger.warning(f"[OITrader] CIRCUIT BREAKER: Daily loss {daily_loss:.0f} "
                           f"> limit {MAX_DAILY_LOSS}")
            for pos in list(self.portfolio.positions):
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'CIRCUIT_BREAKER')
            return

        for pos in list(self.portfolio.positions):
            entry_time = datetime.fromisoformat(pos['timestamp'])
            elapsed = (datetime.now() - entry_time).total_seconds()

            # Grace period
            if elapsed < GRACE_PERIOD_SECONDS:
                continue

            # EOD force close
            if datetime.now().time() > EOD_CLOSE_TIME:
                current = pos.get('current_premium', pos['entry_premium'])
                self.portfolio.close_position(pos['id'], current, 'EOD_FORCE_CLOSE')
                continue

            # Get current option premium (3-source fallback)
            current_premium = self.get_option_ltp(
                pos['symbol'], pos['strike'], pos.get('opt_type', 'CE'))

            if current_premium is None or current_premium <= 0:
                # Delta approximation fallback
                spot = self.get_spot(pos['symbol'])
                if spot and pos.get('entry_spot'):
                    delta = pos.get('delta', 0.5)
                    spot_change = spot - pos['entry_spot']
                    current_premium = max(
                        pos['entry_premium'] + abs(delta) * spot_change, 0.05)
                else:
                    continue

            self.portfolio.update_position(pos['id'], current_premium)

            # 1. TSL computation and check
            tsl_result = compute_tsl(pos, current_premium)
            pos['trailing_sl'] = tsl_result['trailing_sl']
            pos['breakeven_locked'] = tsl_result['breakeven_locked']
            pos['peak_premium'] = tsl_result['peak_premium']

            tsl_hit, _ = check_tsl_hit(pos, current_premium)
            if tsl_hit:
                self.portfolio.close_position(
                    pos['id'], current_premium,
                    f'TRAILING_SL_HIT ({tsl_result["phase"]})')
                self._notify_exit(pos, current_premium, 'TRAILING_SL_HIT')
                continue

            # 2. Target hit
            target_hit, _ = check_target_hit(pos, current_premium)
            if target_hit:
                self.portfolio.close_position(pos['id'], current_premium, 'TARGET_HIT')
                self._notify_exit(pos, current_premium, 'TARGET_HIT')
                continue

            # 3. Static SL
            sl_hit, _ = check_sl_hit(pos, current_premium)
            if sl_hit:
                self.portfolio.close_position(pos['id'], current_premium, 'SL_HIT')
                self._notify_exit(pos, current_premium, 'SL_HIT')
                continue

            # 4. Hold score + weak exit
            spot = self.get_spot(pos['symbol'])
            df = self.historical_data.get(pos['symbol'])
            indicators = {}
            if df is not None and len(df) >= 5:
                try:
                    from strategies.indicators import compute_all_indicators
                    indicators = compute_all_indicators(df) or {}
                except Exception:
                    pass

            # Get live OI and IV for hold scoring
            current_oi = None
            current_iv = None
            if self.pipeline:
                chain = self.pipeline.get_option_chain(pos['symbol'])
                if chain:
                    opt_key = pos.get('opt_type', 'CE')
                    for c in chain.get(opt_key, []):
                        if c.get('strike') == pos['strike']:
                            current_oi = c.get('oi')
                            current_iv = c.get('iv')
                            if current_iv and current_iv > 1:
                                current_iv = current_iv / 100
                            break

            hold_score = compute_hold_score(
                pos, spot, indicators,
                current_oi=current_oi,
                current_iv=current_iv) if spot else 50
            pos['hold_score'] = hold_score

            # Weak exit
            should_weak, reason = check_signal_weak_exit(pos, hold_score)
            if should_weak:
                self.portfolio.close_position(pos['id'], current_premium, reason)
                self._notify_exit(pos, current_premium, 'SIGNAL_WEAK_EXIT')
                continue

            # 5. Time exit (>4h, <15% profit, hold_score < 60)
            should_time, reason = check_time_exit(pos)
            if should_time and hold_score < 60:
                self.portfolio.close_position(pos['id'], current_premium, reason)
                self._notify_exit(pos, current_premium, 'TIME_EXIT')
                continue

            # 6. Max loss (60%)
            max_loss_hit, _ = check_max_loss(pos, current_premium, 60)
            if max_loss_hit:
                self.portfolio.close_position(
                    pos['id'], current_premium, 'MAX_LOSS_60PCT')
                self._notify_exit(pos, current_premium, 'MAX_LOSS_60PCT')
                continue

    def _notify_exit(self, pos, exit_premium, reason):
        """Send Telegram notification for trade exit."""
        try:
            from trade_notifier import notify_trade_exit
            pnl = (exit_premium - pos['entry_premium']) * pos['lot_size']
            notify_trade_exit(
                market="OI",
                strategy=pos['strategy'],
                symbol=pos['symbol'],
                signal_type=pos['signal_type'],
                entry_price=pos['entry_premium'],
                exit_price=exit_premium,
                pnl=pnl,
                reason=reason,
            )
        except Exception:
            pass

    def save_signals_csv(self, signals):
        """Log all signals to CSV for analysis."""
        if not signals:
            return
        today = datetime.now().strftime('%Y%m%d')
        log_file = os.path.join(OI_PAPER_DIR, f'oi_signals_{today}.csv')

        rows = []
        for sig in signals:
            rows.append({
                'timestamp': datetime.now().isoformat(),
                'strategy': sig.get('strategy_type', ''),
                'symbol': sig.get('symbol', ''),
                'signal_type': sig.get('signal_type', ''),
                'spot': sig.get('spot', 0),
                'strike': sig.get('strike', 0),
                'opt_type': sig.get('opt_type', ''),
                'entry_premium': sig.get('entry_premium', 0),
                'target': sig.get('target', 0),
                'sl': sig.get('sl', 0),
                'quality_score': sig.get('quality_score', 0),
                'dte': sig.get('dte', 0),
                'iv': sig.get('iv', 0),
                'vix': sig.get('vix', 0),
                'reason': sig.get('reason', ''),
                'wall_strike': sig.get('wall_strike', ''),
                'wall_oi': sig.get('wall_oi', ''),
                'max_pain': sig.get('max_pain', ''),
                'vwap': sig.get('vwap', ''),
                'oi_confirmed': sig.get('oi_confirmed', ''),
                'distance_pct': sig.get('distance_pct', ''),
            })

        try:
            df = pd.DataFrame(rows)
            if os.path.exists(log_file):
                existing = pd.read_csv(log_file)
                df = pd.concat([existing, df], ignore_index=True)
            df.to_csv(log_file, index=False)
        except Exception as e:
            logger.error(f"[OITrader] Signal CSV error: {e}")

    def run_once(self):
        """Run one complete scan cycle."""
        try:
            signals = self.scan_signals()
            self.check_exits()
            self.execute_signals(signals)
            self.save_signals_csv(signals)
            self.portfolio.save_state()
            self.portfolio.print_status()
        except Exception as e:
            logger.error(f"[OITrader] Scan cycle error: {e}", exc_info=True)

    def eod_summary(self):
        """Print end-of-day summary."""
        today = datetime.now().strftime('%Y-%m-%d')
        daily = self.portfolio.daily_pnl.get(today, 0)
        closed_today = [t for t in self.portfolio.closed_trades
                        if t.get('exit_time', '').startswith(today)]

        print("\n" + "=" * 70)
        print("  OI TRADING BOT — END OF DAY SUMMARY")
        print(f"  Date: {today}")
        print("=" * 70)
        print(f"  Signals generated:  {self.daily_signal_count}")
        print(f"  Trades taken:       {self.daily_trade_count}")
        print(f"  Trades closed:      {len(closed_today)}")
        print(f"  Open positions:     {len(self.portfolio.positions)}")
        print(f"  Daily P&L:          Rs {daily:+,.2f}")
        print(f"  Capital:            Rs {self.portfolio.capital:,.2f}")

        if closed_today:
            winners = [t for t in closed_today if t.get('pnl', 0) > 0]
            losers = [t for t in closed_today if t.get('pnl', 0) <= 0]
            print(f"  Winners:            {len(winners)}")
            print(f"  Losers:             {len(losers)}")
            exits = Counter(t.get('exit_reason', '?') for t in closed_today)
            print(f"  Exit reasons:       {dict(exits)}")

        print("=" * 70 + "\n")

        # Save EOD report
        report = {
            'date': today,
            'timestamp': datetime.now().isoformat(),
            'oi_trading': {
                'total_signals': self.daily_signal_count,
                'trades_taken': self.daily_trade_count,
                'open_positions': len(self.portfolio.positions),
                'closed_trades': len(closed_today),
                'actual_pnl': round(daily, 2),
                'capital': round(self.portfolio.capital, 2),
            }
        }
        try:
            report_path = os.path.join(LOG_DIR, f'oi_eod_report_{today}.json')
            with open(report_path, 'w') as f:
                json.dump(report, f, indent=2)
        except Exception:
            pass

    def get_status(self):
        """Print current portfolio status (for --status flag)."""
        print("\n" + "=" * 70)
        print("  OI TRADING BOT — PORTFOLIO STATUS")
        print("=" * 70)
        print(f"  Capital:        Rs {self.portfolio.capital:,.2f}")
        print(f"  Open positions: {len(self.portfolio.positions)}")
        print(f"  Closed trades:  {len(self.portfolio.closed_trades)}")

        total_pnl = sum(t.get('pnl', 0) for t in self.portfolio.closed_trades)
        print(f"  Total P&L:      Rs {total_pnl:+,.2f}")

        if self.portfolio.positions:
            print("\n  Open Positions:")
            for p in self.portfolio.positions:
                pnl = p.get('unrealized_pnl', 0)
                print(f"    {p['strategy']} {p['symbol']} {p['signal_type']} "
                      f"@ {p['entry_premium']:.1f} → {p['current_premium']:.1f} "
                      f"PnL={pnl:+.0f}")

        if self.portfolio.closed_trades:
            strats = Counter(t['strategy'] for t in self.portfolio.closed_trades)
            print(f"\n  By strategy: {dict(strats)}")
            exits = Counter(t.get('exit_reason', '?') for t in self.portfolio.closed_trades)
            print(f"  Exit reasons: {dict(exits)}")

        print("=" * 70 + "\n")
