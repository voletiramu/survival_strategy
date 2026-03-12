#!/usr/bin/env python3
"""
v10.3 Backtest — Replay signals through OLD vs NEW rules.

Compares:
  OLD (v10.2): OI-only strike scoring, fixed TSL (30%), BREAKOUT_FAIL always ON
  NEW (v10.3): Delta+Gamma scoring, regime-aware TSL, regime-aware BREAKOUT_FAIL,
               delta filter 0.20-0.70, NO Black-Scholes

Reads signal CSVs from VPS (all signals logged every cycle) and portfolio JSON
(actual trades taken). Simulates trade entries/exits with both rule sets.

Usage:
  python backtest_v10_3.py
"""

import pandas as pd
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

# ===================== CONFIGURATION =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'backtest_data', 'mar11')

EQUITY_SIGNALS = os.path.join(DATA_DIR, 'equity_signals_20260311.csv')
COMMODITY_SIGNALS = os.path.join(DATA_DIR, 'commodity_signals_20260311.csv')
EQUITY_PORTFOLIO = os.path.join(DATA_DIR, 'equity_portfolio_mar11.json')
COMMODITY_PORTFOLIO = os.path.join(DATA_DIR, 'commodity_portfolio_mar11.json')

# Lot sizes
LOT_SIZES = {
    'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
    'GOLDM': 10, 'SILVERM': 100, 'CRUDEOILM': 100,
}

# ===================== RULE SETS =====================

# OLD rules (v10.2 and before)
OLD_RULES = {
    'name': 'v10.2 (OLD)',
    # Strike selection: OI-only scoring
    'use_delta_gamma_scoring': False,
    'delta_filter_min': 0,       # No delta filter
    'delta_filter_max': 1.0,     # No delta filter
    # TSL: fixed distances
    'tsl_trail_pct': 30,         # Phase 2/3 trail distance
    'tsl_tight_pct': 20,         # Phase 3 tight distance
    # BREAKOUT_FAIL: always enabled
    'breakout_fail_enabled': True,
    'breakout_fail_min_gain_pct': 2.0,
    'breakout_fail_reverse_pct': 15.0,
    # Timing
    'breakout_fail_cooldown_min': 10,
}

# NEW rules (v10.3) — we'll make these regime-aware
NEW_RULES_TRENDING = {
    'name': 'v10.3 TRENDING',
    'use_delta_gamma_scoring': True,
    'delta_filter_min': 0.20,
    'delta_filter_max': 0.70,
    'tsl_trail_pct': 20,         # Wider trail in trending
    'tsl_tight_pct': 15,
    'breakout_fail_enabled': False,  # DISABLED in trending
    'breakout_fail_min_gain_pct': 5.0,
    'breakout_fail_reverse_pct': 15.0,
    'breakout_fail_cooldown_min': 5,
}

NEW_RULES_SIDEWAYS = {
    'name': 'v10.3 SIDEWAYS',
    'use_delta_gamma_scoring': True,
    'delta_filter_min': 0.20,
    'delta_filter_max': 0.70,
    'tsl_trail_pct': 30,         # Default
    'tsl_tight_pct': 20,
    'breakout_fail_enabled': True,
    'breakout_fail_min_gain_pct': 2.0,
    'breakout_fail_reverse_pct': 15.0,
    'breakout_fail_cooldown_min': 10,
}

NEW_RULES_FLAT = {
    'name': 'v10.3 FLAT',
    'use_delta_gamma_scoring': True,
    'delta_filter_min': 0.20,
    'delta_filter_max': 0.70,
    'tsl_trail_pct': 40,         # Tighter trail in flat
    'tsl_tight_pct': 25,
    'breakout_fail_enabled': True,
    'breakout_fail_min_gain_pct': 3.0,
    'breakout_fail_reverse_pct': 15.0,
    'breakout_fail_cooldown_min': 15,
}


# ===================== REGIME DETECTION =====================

class BacktestRegimeDetector:
    """Regime detector with hysteresis for backtest — matches v10.3c market_regime.py."""

    MIN_HOLD = 20  # Cycles before regime switch

    def __init__(self):
        self.session_opens = {}   # {sym: first spot}
        self.history = defaultdict(list)
        self.regime = {}          # {sym: str}
        self.pending = {}         # {sym: str}
        self.pending_count = {}   # {sym: int}

    def update(self, symbol, spot):
        if symbol not in self.session_opens:
            self.session_opens[symbol] = spot
        self.history[symbol].append(spot)

        candidate = self._classify(symbol)
        current = self.regime.get(symbol, 'SIDEWAYS')

        if candidate != current:
            if self.pending.get(symbol) == candidate:
                self.pending_count[symbol] = self.pending_count.get(symbol, 0) + 1
            else:
                self.pending[symbol] = candidate
                self.pending_count[symbol] = 1

            if self.pending_count.get(symbol, 0) >= self.MIN_HOLD:
                self.regime[symbol] = candidate
                self.pending[symbol] = None
                self.pending_count[symbol] = 0
        else:
            self.pending[symbol] = None
            self.pending_count[symbol] = 0

        if symbol not in self.regime:
            self.regime[symbol] = candidate

    def get_regime(self, symbol):
        return self.regime.get(symbol, 'SIDEWAYS')

    def _classify(self, symbol):
        prices = self.history[symbol]
        if len(prices) < 10:
            return 'SIDEWAYS'

        session_open = self.session_opens.get(symbol, prices[0])
        current = prices[-1]
        if session_open == 0:
            return 'SIDEWAYS'

        total_move_pct = abs(current - session_open) / session_open * 100

        # Use last 60 readings for efficiency
        recent = prices[-min(60, len(prices)):]
        abs_moves = sum(abs(recent[i] - recent[i-1]) for i in range(1, len(recent)))
        net_move = abs(recent[-1] - recent[0])
        efficiency = net_move / abs_moves if abs_moves > 0 else 0

        if total_move_pct >= 0.8 and efficiency >= 0.5:
            return 'TRENDING'
        elif total_move_pct < 0.3 and efficiency < 0.3:
            return 'FLAT'
        return 'SIDEWAYS'


def get_new_rules_for_regime(regime):
    """Return v10.3 rules based on detected regime."""
    if regime == 'TRENDING':
        return NEW_RULES_TRENDING
    elif regime == 'FLAT':
        return NEW_RULES_FLAT
    else:
        return NEW_RULES_SIDEWAYS


# ===================== STRIKE SCORING =====================

def score_signal_old(sig):
    """Old scoring: OI*0.6 + Volume*0.2 + Proximity*0.2 (no delta/gamma)."""
    oi = sig.get('oi', 0)
    volume = sig.get('volume', 0)
    oi_norm = min(oi / 100000, 1.0)
    vol_norm = min(volume / 10000, 1.0)
    return oi_norm * 60 + vol_norm * 20 + 20  # proximity always 1 for ATM


def score_signal_new(sig):
    """
    New scoring: Delta(35%) + Gamma(25%) + OI(25%) + Volume(15%).
    Returns -1 if delta is outside 0.20-0.70 range (filtered out).
    """
    delta = abs(sig.get('delta', 0))
    gamma = sig.get('gamma', 0)
    oi = sig.get('oi', 0)
    volume = sig.get('volume', 0)

    # Delta filter
    if delta > 0 and (delta < 0.20 or delta > 0.70):
        return -1  # Filtered out

    delta_score = max(0, 1 - abs(delta - 0.50) * 3.33)
    gamma_score = min(gamma * 10000, 5.0)
    oi_norm = min(oi / 100000, 1.0)
    vol_norm = min(volume / 10000, 1.0)

    return (delta_score * 35) + (gamma_score * 25) + (oi_norm * 25) + (vol_norm * 15)


# ===================== TRADE SIMULATOR =====================

class TradeSimulator:
    """Simulates trade entries and exits given a set of rules."""

    def __init__(self, rules, capital, market='equity'):
        self.rules = rules
        self.initial_capital = capital
        self.capital = capital
        self.market = market
        self.positions = []       # Open positions
        self.closed_trades = []   # Closed trades
        self.max_positions = 6
        self.min_premium = 50     # Min premium to enter
        self.max_premium = 2000   # Max premium for BUY
        self.cooldown = {}        # {symbol: last_entry_time}
        self.daily_trade_count = 0
        self.max_daily_trades = 20

        # TSL tracking
        # Phase 1: 0-15% gain → SL at entry (no trail)
        # Phase 2: 15-25% gain → trail at tsl_trail_pct of gains
        # Phase 3: >25% gain → tighter trail at tsl_tight_pct
        self.TSL_PHASE1_THRESHOLD = 15  # % gain to start trailing
        self.TSL_PHASE2_THRESHOLD = 25  # % gain for tighter trail
        self.TSL_INITIAL_SL_PCT = 40    # Initial SL % below entry

    def can_enter(self, sig, timestamp):
        """Check if we can enter a new position."""
        if len(self.positions) >= self.max_positions:
            return False, 'MAX_POSITIONS'
        if self.daily_trade_count >= self.max_daily_trades:
            return False, 'MAX_DAILY_TRADES'

        symbol = sig.get('symbol', sig.get('commodity', ''))
        premium = sig.get('premium', 0)

        if premium < self.min_premium:
            return False, 'LOW_PREMIUM'
        if premium > self.max_premium:
            return False, 'HIGH_PREMIUM'

        # Cooldown check (5 min between same-symbol entries)
        if symbol in self.cooldown:
            diff = (timestamp - self.cooldown[symbol]).total_seconds()
            if diff < 300:
                return False, 'COOLDOWN'

        # Capital check
        lot_size = LOT_SIZES.get(symbol, 50)
        capital_needed = premium * lot_size
        if capital_needed > self.capital:
            return False, 'NO_CAPITAL'

        return True, 'OK'

    def enter_trade(self, sig, timestamp):
        """Enter a new trade."""
        symbol = sig.get('symbol', sig.get('commodity', ''))
        lot_size = LOT_SIZES.get(symbol, 50)
        premium = sig['premium']

        pos = {
            'id': len(self.closed_trades) + len(self.positions) + 1,
            'symbol': symbol,
            'strategy': sig['strategy'],
            'signal_type': sig['type'],
            'strike': sig['strike'],
            'entry_premium': premium,
            'entry_time': timestamp,
            'entry_spot': sig['spot'],
            'lot_size': lot_size,
            'delta': sig.get('delta', 0),
            'gamma': sig.get('gamma', 0),
            'high_premium': premium,  # Track highest premium for TSL
            'unrealized_pnl': 0,
        }
        self.positions.append(pos)
        self.capital -= premium * lot_size
        self.cooldown[symbol] = timestamp
        self.daily_trade_count += 1

    def check_exits(self, current_prices, timestamp):
        """
        Check exit conditions for all open positions.
        current_prices: {symbol: {spot: float, premiums: {strike: premium}}}
        """
        rules = self.rules
        to_close = []

        for pos in self.positions:
            symbol = pos['symbol']
            if symbol not in current_prices:
                continue

            spot = current_prices[symbol].get('spot', 0)
            # Estimate current premium using delta approximation
            spot_change = spot - pos['entry_spot']
            delta = pos.get('delta', 0.5)
            premium_change = abs(delta) * spot_change
            if 'PE' in pos['signal_type']:
                premium_change = -premium_change

            current_premium = max(0.05, pos['entry_premium'] + premium_change)

            # Update high watermark
            if current_premium > pos['high_premium']:
                pos['high_premium'] = current_premium

            # Calculate gains
            gain_pct = (current_premium - pos['entry_premium']) / pos['entry_premium'] * 100
            from_high_pct = (pos['high_premium'] - current_premium) / pos['high_premium'] * 100 if pos['high_premium'] > 0 else 0

            exit_reason = None

            # 1. Fixed SL: -40% from entry
            if gain_pct <= -self.TSL_INITIAL_SL_PCT:
                exit_reason = 'SL_HIT'

            # 2. TSL Phase 2: 15-25% gain, trail at tsl_trail_pct of gains
            elif gain_pct >= self.TSL_PHASE1_THRESHOLD and gain_pct < self.TSL_PHASE2_THRESHOLD:
                trail_distance = rules['tsl_trail_pct']
                if from_high_pct >= trail_distance:
                    exit_reason = 'TRAILING_SL_HIT'

            # 3. TSL Phase 3: >25% gain, tighter trail
            elif gain_pct >= self.TSL_PHASE2_THRESHOLD:
                trail_distance = rules['tsl_tight_pct']
                if from_high_pct >= trail_distance:
                    exit_reason = 'TRAILING_SL_HIT'

            # 4. BREAKOUT_FAIL: small gain then reversal within 5 min
            if not exit_reason and rules['breakout_fail_enabled']:
                time_held = (timestamp - pos['entry_time']).total_seconds() / 60
                if time_held <= 5:
                    if gain_pct >= rules['breakout_fail_min_gain_pct']:
                        # Had a small gain, check if reversing
                        if from_high_pct >= rules['breakout_fail_reverse_pct']:
                            exit_reason = 'BREAKOUT_FAIL'
                    elif gain_pct > 0 and from_high_pct >= rules['breakout_fail_reverse_pct']:
                        exit_reason = 'BREAKOUT_FAIL'

            # 5. Time exit: >2 hours with <2% gain
            if not exit_reason:
                time_held = (timestamp - pos['entry_time']).total_seconds() / 60
                if time_held >= 120 and gain_pct < 2:
                    exit_reason = 'TIME_EXIT_NO_PROGRESS'

            if exit_reason:
                pnl = (current_premium - pos['entry_premium']) * pos['lot_size']
                pnl_pct = gain_pct
                to_close.append((pos, current_premium, exit_reason, pnl, pnl_pct))

        for pos, exit_prem, reason, pnl, pnl_pct in to_close:
            self.positions.remove(pos)
            self.capital += exit_prem * pos['lot_size']
            self.closed_trades.append({
                'symbol': pos['symbol'],
                'strategy': pos['strategy'],
                'signal_type': pos['signal_type'],
                'strike': pos['strike'],
                'entry_premium': pos['entry_premium'],
                'exit_premium': exit_prem,
                'entry_time': pos['entry_time'].isoformat(),
                'exit_time': timestamp.isoformat(),
                'pnl': round(pnl, 2),
                'pnl_pct': round(pnl_pct, 2),
                'exit_reason': reason,
                'delta': pos.get('delta', 0),
                'gamma': pos.get('gamma', 0),
                'lot_size': pos['lot_size'],
            })

    def close_all(self, current_prices, timestamp):
        """Force close all open positions (EOD)."""
        for pos in list(self.positions):
            symbol = pos['symbol']
            spot = current_prices.get(symbol, {}).get('spot', pos['entry_spot'])
            spot_change = spot - pos['entry_spot']
            delta = pos.get('delta', 0.5)
            premium_change = abs(delta) * spot_change
            if 'PE' in pos['signal_type']:
                premium_change = -premium_change
            current_premium = max(0.05, pos['entry_premium'] + premium_change)

            pnl = (current_premium - pos['entry_premium']) * pos['lot_size']
            pnl_pct = (current_premium - pos['entry_premium']) / pos['entry_premium'] * 100

            self.positions.remove(pos)
            self.capital += current_premium * pos['lot_size']
            self.closed_trades.append({
                'symbol': pos['symbol'],
                'strategy': pos['strategy'],
                'signal_type': pos['signal_type'],
                'strike': pos['strike'],
                'entry_premium': pos['entry_premium'],
                'exit_premium': round(current_premium, 2),
                'entry_time': pos['entry_time'].isoformat(),
                'exit_time': timestamp.isoformat(),
                'pnl': round(pnl, 2),
                'pnl_pct': round(pnl_pct, 2),
                'exit_reason': 'EOD_CLOSE',
                'delta': pos.get('delta', 0),
                'gamma': pos.get('gamma', 0),
                'lot_size': pos['lot_size'],
            })

    def summary(self):
        """Return summary statistics."""
        total_pnl = sum(t['pnl'] for t in self.closed_trades)
        winners = [t for t in self.closed_trades if t['pnl'] > 0]
        losers = [t for t in self.closed_trades if t['pnl'] < 0]
        breakout_fails = [t for t in self.closed_trades if t['exit_reason'] == 'BREAKOUT_FAIL']
        tsl_exits = [t for t in self.closed_trades if 'TRAILING_SL' in t['exit_reason']]
        sl_hits = [t for t in self.closed_trades if t['exit_reason'] == 'SL_HIT']

        return {
            'total_trades': len(self.closed_trades),
            'total_pnl': round(total_pnl, 0),
            'winners': len(winners),
            'losers': len(losers),
            'win_rate': round(len(winners) / len(self.closed_trades) * 100, 1) if self.closed_trades else 0,
            'avg_win': round(sum(t['pnl'] for t in winners) / len(winners), 0) if winners else 0,
            'avg_loss': round(sum(t['pnl'] for t in losers) / len(losers), 0) if losers else 0,
            'largest_win': round(max((t['pnl'] for t in winners), default=0), 0),
            'largest_loss': round(min((t['pnl'] for t in losers), default=0), 0),
            'breakout_fail_count': len(breakout_fails),
            'breakout_fail_pnl': round(sum(t['pnl'] for t in breakout_fails), 0),
            'tsl_count': len(tsl_exits),
            'tsl_pnl': round(sum(t['pnl'] for t in tsl_exits), 0),
            'sl_hit_count': len(sl_hits),
            'sl_hit_pnl': round(sum(t['pnl'] for t in sl_hits), 0),
            'final_capital': round(self.capital, 0),
        }


# ===================== BACKTEST ENGINE =====================

def run_backtest(signals_df, rules, capital, market='equity', sym_col='symbol'):
    """
    Run backtest on signal data with given rules.

    Args:
        signals_df: DataFrame with columns [timestamp, symbol/commodity, strategy,
                    type, strike, premium, spot, delta, gamma, oi, ...]
        rules: Rule dict or 'REGIME_AWARE' for dynamic regime detection
        capital: Starting capital
        market: 'equity' or 'commodity'
        sym_col: Column name for symbol ('symbol' or 'commodity')

    Returns:
        TradeSimulator with results
    """
    regime_aware = (rules == 'REGIME_AWARE')

    # Initialize with sideways rules if regime-aware
    if regime_aware:
        sim = TradeSimulator(NEW_RULES_SIDEWAYS, capital, market)
    else:
        sim = TradeSimulator(rules, capital, market)

    # Sort by timestamp
    signals_df = signals_df.sort_values('timestamp').reset_index(drop=True)

    # Regime detector with hysteresis
    regime_detector = BacktestRegimeDetector()

    # Group signals by scan cycle (same timestamp[:16])
    signals_df['scan_time'] = signals_df['timestamp'].str[:16]
    scan_groups = signals_df.groupby('scan_time')

    for scan_time_str, group in scan_groups:
        timestamp = datetime.fromisoformat(group['timestamp'].iloc[0])

        # Build current prices from this scan cycle
        current_prices = {}
        for sym in group[sym_col].unique():
            sym_data = group[group[sym_col] == sym]
            spot = sym_data['spot'].iloc[0]
            current_prices[sym] = {'spot': spot}
            regime_detector.update(sym, spot)

        # Detect regime if regime-aware
        if regime_aware:
            primary_sym = group[sym_col].iloc[0]
            regime = regime_detector.get_regime(primary_sym)
            sim.rules = get_new_rules_for_regime(regime)

        # Check exits first
        sim.check_exits(current_prices, timestamp)

        # Then try entries from this cycle's signals
        # Deduplicate: take best signal per (symbol, type) in this cycle
        for (sym, sig_type), sub_group in group.groupby([sym_col, 'type']):
            # Pick best signal based on scoring rules
            best_sig = None
            best_score = -1

            for _, sig in sub_group.iterrows():
                if regime_aware or sim.rules.get('use_delta_gamma_scoring', False):
                    score = score_signal_new(dict(sig))
                else:
                    score = score_signal_old(dict(sig))

                if score > best_score:
                    best_score = score
                    best_sig = dict(sig)

            if best_sig is None or best_score < 0:
                continue  # All signals filtered out (delta range)

            # Normalize symbol column
            if sym_col == 'commodity':
                best_sig['symbol'] = best_sig['commodity']

            can, reason = sim.can_enter(best_sig, timestamp)
            if can:
                sim.enter_trade(best_sig, timestamp)

    # Close remaining at last known prices
    last_row = signals_df.iloc[-1]
    last_time = datetime.fromisoformat(last_row['timestamp'])
    last_prices = {}
    for sym in signals_df[sym_col].unique():
        sym_data = signals_df[signals_df[sym_col] == sym]
        last_prices[sym] = {'spot': sym_data['spot'].iloc[-1]}
    sim.close_all(last_prices, last_time)

    return sim


# ===================== ACTUAL TRADES LOADER =====================

def load_actual_trades(portfolio_file, market='equity'):
    """Load actual trades from portfolio JSON."""
    with open(portfolio_file) as f:
        data = json.load(f)

    trades = data.get('closed_trades', [])
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    winners = [t for t in trades if t.get('pnl', 0) > 0]
    losers = [t for t in trades if t.get('pnl', 0) < 0]
    breakout_fails = [t for t in trades if t.get('exit_reason', '') == 'BREAKOUT_FAIL']
    tsl_exits = [t for t in trades if 'TRAILING_SL' in t.get('exit_reason', '')]

    return {
        'total_trades': len(trades),
        'total_pnl': round(total_pnl, 0),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': round(len(winners) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(sum(t['pnl'] for t in winners) / len(winners), 0) if winners else 0,
        'avg_loss': round(sum(t['pnl'] for t in losers) / len(losers), 0) if losers else 0,
        'largest_win': round(max((t['pnl'] for t in winners), default=0), 0),
        'largest_loss': round(min((t['pnl'] for t in losers), default=0), 0),
        'breakout_fail_count': len(breakout_fails),
        'breakout_fail_pnl': round(sum(t.get('pnl', 0) for t in breakout_fails), 0),
        'tsl_count': len(tsl_exits),
        'tsl_pnl': round(sum(t.get('pnl', 0) for t in tsl_exits), 0),
        'trades': trades,
    }


# ===================== MAIN =====================

def print_comparison(label, actual, old_sim, new_sim):
    """Print side-by-side comparison."""
    print(f"\n{'='*80}")
    print(f"  {label} BACKTEST COMPARISON — March 11, 2026")
    print(f"{'='*80}")
    print(f"{'Metric':<30} {'ACTUAL (bot)':>15} {'OLD Rules':>15} {'NEW v10.3':>15}")
    print(f"{'-'*75}")

    metrics = [
        ('Total Trades', 'total_trades'),
        ('Total PnL', 'total_pnl'),
        ('Winners', 'winners'),
        ('Losers', 'losers'),
        ('Win Rate %', 'win_rate'),
        ('Avg Win', 'avg_win'),
        ('Avg Loss', 'avg_loss'),
        ('Largest Win', 'largest_win'),
        ('Largest Loss', 'largest_loss'),
        ('BREAKOUT_FAIL Count', 'breakout_fail_count'),
        ('BREAKOUT_FAIL PnL', 'breakout_fail_pnl'),
        ('TSL Exit Count', 'tsl_count'),
        ('TSL Exit PnL', 'tsl_pnl'),
    ]

    for label_str, key in metrics:
        actual_val = actual.get(key, '-')
        old_val = old_sim.get(key, '-')
        new_val = new_sim.get(key, '-')

        if key in ('total_pnl', 'avg_win', 'avg_loss', 'largest_win', 'largest_loss',
                   'breakout_fail_pnl', 'tsl_pnl'):
            actual_str = f"Rs {actual_val:+,.0f}" if isinstance(actual_val, (int, float)) else str(actual_val)
            old_str = f"Rs {old_val:+,.0f}" if isinstance(old_val, (int, float)) else str(old_val)
            new_str = f"Rs {new_val:+,.0f}" if isinstance(new_val, (int, float)) else str(new_val)
        elif key == 'win_rate':
            actual_str = f"{actual_val:.1f}%" if isinstance(actual_val, (int, float)) else str(actual_val)
            old_str = f"{old_val:.1f}%" if isinstance(old_val, (int, float)) else str(old_val)
            new_str = f"{new_val:.1f}%" if isinstance(new_val, (int, float)) else str(new_val)
        else:
            actual_str = str(actual_val)
            old_str = str(old_val)
            new_str = str(new_val)

        print(f"  {label_str:<28} {actual_str:>15} {old_str:>15} {new_str:>15}")

    # Improvement
    if isinstance(new_sim.get('total_pnl'), (int, float)) and isinstance(old_sim.get('total_pnl'), (int, float)):
        diff = new_sim['total_pnl'] - old_sim['total_pnl']
        print(f"\n  v10.3 Improvement: Rs {diff:+,.0f}")


def print_trade_details(sim, label):
    """Print individual trade details."""
    print(f"\n  --- {label} Trade Details ---")
    for t in sim.closed_trades:
        pnl_str = f"Rs {t['pnl']:+,.0f}"
        print(f"    {t['symbol']:12s} {t['strategy']:12s} {t['signal_type']:14s} "
              f"Strike={t['strike']:>8.0f} Entry={t['entry_premium']:>8.2f} Exit={t['exit_premium']:>8.2f} "
              f"{pnl_str:>12s} {t['exit_reason']}")


def analyze_delta_filtering(signals_df, sym_col='symbol'):
    """Analyze how many signals would be filtered by delta range 0.20-0.70."""
    total = len(signals_df.drop_duplicates(subset=[sym_col, 'strike', 'strategy', 'type']))
    delta_col = signals_df['delta'].abs()

    in_range = signals_df[(delta_col >= 0.20) & (delta_col <= 0.70)]
    in_range_unique = len(in_range.drop_duplicates(subset=[sym_col, 'strike', 'strategy', 'type']))

    out_range = signals_df[(delta_col < 0.20) | (delta_col > 0.70)]
    out_range_unique = len(out_range.drop_duplicates(subset=[sym_col, 'strike', 'strategy', 'type']))

    print(f"\n  Delta Filter Analysis:")
    print(f"    Total unique signals: {total}")
    print(f"    In range (0.20-0.70): {in_range_unique} ({in_range_unique/total*100:.0f}%)")
    print(f"    Filtered out:         {out_range_unique} ({out_range_unique/total*100:.0f}%)")

    # Show filtered signals
    if out_range_unique > 0:
        out_uniq = out_range.drop_duplicates(subset=[sym_col, 'strike', 'strategy', 'type'], keep='first')
        print(f"\n    Filtered signals (delta outside 0.20-0.70):")
        for _, s in out_uniq.head(15).iterrows():
            sym = s.get(sym_col, '?')
            print(f"      {sym:12s} {s['strategy']:12s} {s['type']:14s} "
                  f"strike={s['strike']:>8.0f} delta={s['delta']:.4f} prem={s['premium']:.2f}")


def analyze_regime_timeline(signals_df, sym_col='symbol'):
    """Show regime transitions throughout the day using hysteresis-based detector."""
    signals_df = signals_df.sort_values('timestamp')
    primary = signals_df[sym_col].mode().iloc[0]  # Most common symbol
    sym_data = signals_df[signals_df[sym_col] == primary].drop_duplicates(subset=['timestamp'], keep='first')

    detector = BacktestRegimeDetector()
    regimes = []
    times = []
    spots = []

    for _, row in sym_data.iterrows():
        detector.update(primary, row['spot'])
        regime = detector.get_regime(primary)
        regimes.append(regime)
        times.append(row['timestamp'][:19])
        spots.append(row['spot'])

    # Show transitions
    print(f"\n  Regime Timeline ({primary}) — with hysteresis (min {BacktestRegimeDetector.MIN_HOLD} cycles):")
    last_regime = None
    for i in range(len(regimes)):
        if regimes[i] != last_regime:
            print(f"    {times[i]} -> {regimes[i]} (spot={spots[i]:.0f})")
            last_regime = regimes[i]

    # Count time in each regime
    from collections import Counter
    counts = Counter(regimes)
    total = len(regimes)
    for r, c in counts.most_common():
        print(f"    {r}: {c}/{total} cycles ({c/total*100:.0f}%)")


def main():
    print("=" * 80)
    print("  v10.3 BACKTEST — March 11, 2026")
    print("  Comparing OLD (v10.2) vs NEW (v10.3) Rules")
    print("  Using LIVE signal data from VPS")
    print("=" * 80)

    # ======================== EQUITY ========================
    if os.path.exists(EQUITY_SIGNALS) and os.path.exists(EQUITY_PORTFOLIO):
        eq_signals = pd.read_csv(EQUITY_SIGNALS)
        eq_actual = load_actual_trades(EQUITY_PORTFOLIO, 'equity')

        print(f"\n  Equity signals: {len(eq_signals)} rows, "
              f"{eq_signals['symbol'].nunique()} symbols")

        # Regime analysis
        analyze_regime_timeline(eq_signals, 'symbol')

        # Delta filter analysis
        analyze_delta_filtering(eq_signals, 'symbol')

        # Run OLD rules backtest
        old_sim = run_backtest(eq_signals, OLD_RULES, 300000, 'equity', 'symbol')
        old_summary = old_sim.summary()

        # Run NEW rules backtest (regime-aware)
        new_sim = run_backtest(eq_signals, 'REGIME_AWARE', 300000, 'equity', 'symbol')
        new_summary = new_sim.summary()

        print_comparison("EQUITY", eq_actual, old_summary, new_summary)
        print_trade_details(old_sim, "OLD Rules")
        print_trade_details(new_sim, "NEW v10.3")

    # ======================== COMMODITY ========================
    if os.path.exists(COMMODITY_SIGNALS) and os.path.exists(COMMODITY_PORTFOLIO):
        cm_signals = pd.read_csv(COMMODITY_SIGNALS)
        cm_actual = load_actual_trades(COMMODITY_PORTFOLIO, 'commodity')

        print(f"\n\n  Commodity signals: {len(cm_signals)} rows, "
              f"{cm_signals['commodity'].nunique()} commodities")

        # Regime analysis
        analyze_regime_timeline(cm_signals, 'commodity')

        # Delta filter analysis
        analyze_delta_filtering(cm_signals, 'commodity')

        # Run OLD rules
        old_sim = run_backtest(cm_signals, OLD_RULES, 300000, 'commodity', 'commodity')
        old_summary = old_sim.summary()

        # Run NEW rules (regime-aware)
        new_sim = run_backtest(cm_signals, 'REGIME_AWARE', 300000, 'commodity', 'commodity')
        new_summary = new_sim.summary()

        print_comparison("COMMODITY", cm_actual, old_summary, new_summary)
        print_trade_details(old_sim, "OLD Rules")
        print_trade_details(new_sim, "NEW v10.3")

    print(f"\n{'='*80}")
    print("  BACKTEST COMPLETE")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
