#!/usr/bin/env python3
"""
v10.3d Backtest — EXIT STRATEGY REPLAY

Takes the ACTUAL bot's 15 equity entries from March 11 and replays them
through OLD (v10.2) vs NEW (v10.3d) exit rules using spot price data from
the signal CSV for premium estimation.

This isolates the EXIT STRATEGY impact: same entries, different exit logic.

Key differences being tested:
  OLD: Fixed TSL (30/20%), BREAKOUT_FAIL always ON, hard TARGET_HIT
  NEW: Regime-aware TSL (TRENDING=40/25%, FLAT=20/15%), regime-aware BF,
       trailing target (extend 20%, TSL at 15% below peak)

Premium estimation: delta*dS + 0.5*gamma*dS^2 - theta_decay

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
EQUITY_PORTFOLIO = os.path.join(DATA_DIR, 'equity_portfolio_mar11.json')

# Correct lot sizes
BASE_LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}


# ===================== REGIME DETECTION =====================

class BacktestRegimeDetector:
    """Regime detector with hysteresis — matches v10.3c."""

    MIN_HOLD = 10  # v10.3d: reduced from 20 for faster regime detection

    def __init__(self):
        self.session_opens = {}
        self.history = defaultdict(list)
        self.regime = {}
        self.pending = {}
        self.pending_count = {}

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
        recent = prices[-min(60, len(prices)):]
        abs_moves = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
        net_move = abs(recent[-1] - recent[0])
        efficiency = net_move / abs_moves if abs_moves > 0 else 0
        # v10.3d: Lowered thresholds — 0.8% was too high, never triggered on Mar 11
        if total_move_pct >= 0.4 and efficiency >= 0.4:
            return 'TRENDING'
        elif total_move_pct < 0.15 and efficiency < 0.25:
            return 'FLAT'
        return 'SIDEWAYS'


def get_regime_params(regime):
    """TSL/BF parameters — CORRECTED: TRENDING=wide, FLAT=tight."""
    if regime == 'TRENDING':
        return {
            'tsl_trail_pct': 40,   # Wide trail — survive retracements
            'tsl_tight_pct': 25,   # Wide tight
            'bf_enabled': False,   # DISABLED
        }
    elif regime == 'FLAT':
        return {
            'tsl_trail_pct': 20,   # Tight trail — lock gains
            'tsl_tight_pct': 15,
            'bf_enabled': True,
        }
    else:  # SIDEWAYS
        return {
            'tsl_trail_pct': 30,
            'tsl_tight_pct': 20,
            'bf_enabled': True,
        }


# ===================== PREMIUM ESTIMATION =====================

def estimate_premium(entry_premium, entry_spot, current_spot, delta, gamma, theta, hours_held):
    """
    Estimate current premium: delta*dS + 0.5*gamma*dS^2 - theta_decay
    Uses SIGNED delta (negative for PE, positive for CE).
    """
    d_spot = current_spot - entry_spot
    premium_change = delta * d_spot + 0.5 * abs(gamma) * d_spot ** 2
    theta_decay = abs(theta) * hours_held / 24
    return max(0.05, round(entry_premium + premium_change - theta_decay, 2))


# ===================== EXIT STRATEGY SIMULATOR =====================

class ExitSimulator:
    """
    Simulates exit logic for a single trade.
    Matches paper_trader.py exit flow: BF -> TSL update -> TSL hit -> SL/Target
    """

    # TSL phases
    TSL_BREAKEVEN_PCT = 15
    TSL_TRAIL_PCT = 25
    TSL_TIGHT_PCT = 40

    # Trailing target
    TARGET_TRAIL_ENABLED = True
    TARGET_TRAIL_EXTEND_PCT = 20
    TARGET_TRAIL_TSL_PCT = 15
    TARGET_TRAIL_MAX_EXT = 5

    # Breakout fail
    GRACE_PERIOD = 180
    BF_CHECK_END = 300
    BF_MIN_GAIN_PCT = 2.0
    BF_REVERSE_DROP_PCT = 15.0

    def __init__(self, trade, mode='old'):
        """
        trade: dict from actual portfolio (closed_trades entry)
        mode: 'old' (v10.2) or 'new' (v10.3d)
        """
        self.trade = trade
        self.mode = mode

        # State tracking
        self.entry_premium = trade['entry_premium']
        self.entry_spot = trade['entry_spot']
        self.entry_time = datetime.fromisoformat(trade['timestamp'])
        self.delta = trade.get('delta', -0.5)
        self.gamma = trade.get('gamma', 0.0004)
        self.theta = abs(trade.get('theta', 50))
        self.lot_size = trade['lot_size']
        self.target = trade['details']['target']
        self.sl = trade['details']['sl']

        self.peak_premium = self.entry_premium
        self.trailing_sl = None
        self.breakeven_locked = False
        self.target_extensions = 0

        self.exit_premium = None
        self.exit_time = None
        self.exit_reason = None
        self.closed = False

    def check_exit(self, spot, timestamp, regime='SIDEWAYS'):
        """Check all exit conditions. Returns True if position should close."""
        if self.closed:
            return True

        hours_held = (timestamp - self.entry_time).total_seconds() / 3600
        elapsed_secs = (timestamp - self.entry_time).total_seconds()

        # Skip if in grace period
        if elapsed_secs < 15:  # At least 15 seconds between entry and first check
            return False

        current_premium = estimate_premium(
            self.entry_premium, self.entry_spot, spot,
            self.delta, self.gamma, self.theta, hours_held
        )

        # Update peak
        if current_premium > self.peak_premium:
            self.peak_premium = round(current_premium, 2)

        entry = self.entry_premium
        peak = self.peak_premium
        profit_from_entry = peak - entry
        peak_gain_pct = (peak - entry) / entry * 100 if entry > 0 else 0
        current_gain_pct = (current_premium - entry) / entry * 100 if entry > 0 else 0

        # Get regime-specific params
        if self.mode == 'new':
            rp = get_regime_params(regime)
        else:
            rp = {'tsl_trail_pct': 30, 'tsl_tight_pct': 20, 'bf_enabled': True}

        exit_reason = None

        # ---- 1. BREAKOUT_FAIL (3-5 min window) ----
        bf_enabled = rp['bf_enabled']
        if bf_enabled and self.GRACE_PERIOD < elapsed_secs <= self.BF_CHECK_END:
            drop_from_entry = (entry - current_premium) / entry * 100 if entry > 0 else 0
            peak_gain_bf = (peak - entry) / entry * 100 if entry > 0 else 0

            if drop_from_entry > self.BF_REVERSE_DROP_PCT:
                exit_reason = 'BREAKOUT_FAIL_REVERSE'
            elif elapsed_secs >= (self.BF_CHECK_END - 15) and peak_gain_bf < self.BF_MIN_GAIN_PCT:
                exit_reason = 'BREAKOUT_FAIL'

        # ---- 2. TSL PHASE TRACKING ----
        if not exit_reason and profit_from_entry > 0:
            # Phase 1: Lock breakeven at 15%+
            if peak_gain_pct >= self.TSL_BREAKEVEN_PCT and not self.breakeven_locked:
                self.breakeven_locked = True
                self.trailing_sl = round(entry * 1.03, 2)

            # Phase 3: Tight trail at 40%+
            if peak_gain_pct >= self.TSL_TIGHT_PCT:
                new_tsl = round(peak - profit_from_entry * rp['tsl_tight_pct'] / 100, 2)
                new_tsl = max(new_tsl, self.trailing_sl or 0)
                if new_tsl > (self.trailing_sl or 0):
                    self.trailing_sl = new_tsl

            # Phase 2: Trail at 25%+
            elif peak_gain_pct >= self.TSL_TRAIL_PCT:
                new_tsl = round(peak - profit_from_entry * rp['tsl_trail_pct'] / 100, 2)
                new_tsl = max(new_tsl, self.trailing_sl or 0)
                if new_tsl > (self.trailing_sl or 0):
                    self.trailing_sl = new_tsl

            # TSL hit check
            if self.trailing_sl and current_premium <= self.trailing_sl:
                exit_reason = 'TRAILING_SL_HIT'

        # ---- 3. STATIC EXITS ----
        if not exit_reason:
            if current_premium <= self.sl:
                exit_reason = 'SL_HIT'
            elif current_premium >= self.target:
                if self.mode == 'new' and self.TARGET_TRAIL_ENABLED:
                    if self.target_extensions < self.TARGET_TRAIL_MAX_EXT:
                        # Extend target
                        self.target_extensions += 1
                        self.target = round(current_premium * (1 + self.TARGET_TRAIL_EXTEND_PCT / 100), 2)
                        new_tsl = round(peak * (1 - self.TARGET_TRAIL_TSL_PCT / 100), 2)
                        self.trailing_sl = max(new_tsl, self.trailing_sl or 0)
                        # Don't exit — continue
                    else:
                        exit_reason = 'TARGET_HIT'
                else:
                    exit_reason = 'TARGET_HIT'

        # ---- 4. TIME EXIT (>4 hours, stagnant) ----
        if not exit_reason and hours_held > 4:
            max_risk = entry * self.lot_size
            unrealized = (current_premium - entry) * self.lot_size
            profit_pct = unrealized / max(max_risk, 1) * 100
            if abs(profit_pct) < 15:
                exit_reason = 'TIME_EXIT'

        if exit_reason:
            self.exit_premium = round(current_premium, 2)
            self.exit_time = timestamp
            self.exit_reason = exit_reason
            self.closed = True
            return True

        return False

    def get_pnl(self):
        """Calculate PnL."""
        if not self.closed:
            return 0
        return round((self.exit_premium - self.entry_premium) * self.lot_size, 2)

    def result(self):
        """Return result dict."""
        return {
            'id': self.trade['id'],
            'symbol': self.trade['symbol'],
            'strategy': self.trade['strategy'],
            'signal_type': self.trade['signal_type'],
            'lot_size': self.lot_size,
            'entry_premium': self.entry_premium,
            'exit_premium': self.exit_premium or self.entry_premium,
            'peak_premium': self.peak_premium,
            'entry_time': self.entry_time.isoformat(),
            'exit_time': self.exit_time.isoformat() if self.exit_time else '',
            'exit_reason': self.exit_reason or 'OPEN',
            'pnl': self.get_pnl(),
            'pnl_pct': round((self.exit_premium - self.entry_premium) / self.entry_premium * 100, 1) if self.exit_premium else 0,
            'target_extensions': self.target_extensions,
            'trailing_sl': self.trailing_sl,
        }


# ===================== BACKTEST ENGINE =====================

def run_exit_replay(portfolio_file, signals_df, mode='old'):
    """
    Replay actual entries through OLD or NEW exit rules using spot data
    from signal CSV for premium estimation.
    """
    with open(portfolio_file) as f:
        data = json.load(f)

    trades = data['closed_trades']

    # Build spot price timeline from signals: {symbol: [(timestamp, spot), ...]}
    spot_timeline = {}
    signals_df = signals_df.sort_values('timestamp')
    for sym in signals_df['symbol'].unique():
        sym_data = signals_df[signals_df['symbol'] == sym].drop_duplicates(
            subset=['timestamp'], keep='first')
        spot_timeline[sym] = [
            (datetime.fromisoformat(row['timestamp']), row['spot'])
            for _, row in sym_data.iterrows()
        ]

    # Initialize regime detector
    regime_detector = BacktestRegimeDetector()

    # Create exit simulators for each trade
    sims = [ExitSimulator(t, mode) for t in trades]

    # Walk through time using spot data
    all_timestamps = sorted(set(
        ts for sym_data in spot_timeline.values() for ts, _ in sym_data
    ))

    for timestamp in all_timestamps:
        # Update regime with current spots
        regime_by_sym = {}
        for sym, timeline in spot_timeline.items():
            # Find latest spot <= timestamp
            spot = None
            for ts, s in timeline:
                if ts <= timestamp:
                    spot = s
                else:
                    break
            if spot is not None:
                regime_detector.update(sym, spot)
                regime_by_sym[sym] = regime_detector.get_regime(sym)

        # Check exits for all open positions
        for sim in sims:
            if sim.closed:
                continue
            if timestamp < sim.entry_time:
                continue

            symbol = sim.trade['symbol']
            spot = None
            for ts, s in spot_timeline.get(symbol, []):
                if ts <= timestamp:
                    spot = s
                else:
                    break

            if spot is None:
                continue

            regime = regime_by_sym.get(symbol, 'SIDEWAYS')
            sim.check_exit(spot, timestamp, regime)

    # Force close any still-open positions at last known prices
    for sim in sims:
        if not sim.closed:
            symbol = sim.trade['symbol']
            if spot_timeline.get(symbol):
                last_ts, last_spot = spot_timeline[symbol][-1]
                sim.check_exit(last_spot, last_ts, 'SIDEWAYS')
            if not sim.closed:
                sim.exit_premium = sim.entry_premium
                sim.exit_time = all_timestamps[-1] if all_timestamps else sim.entry_time
                sim.exit_reason = 'EOD_CLOSE'
                sim.closed = True

    return [sim.result() for sim in sims]


def summarize(results):
    """Summarize a list of trade results."""
    total_pnl = sum(t['pnl'] for t in results)
    winners = [t for t in results if t['pnl'] > 0]
    losers = [t for t in results if t['pnl'] < 0]

    by_reason = defaultdict(list)
    for t in results:
        by_reason[t['exit_reason']].append(t)

    s = {
        'total_trades': len(results),
        'total_pnl': round(total_pnl, 0),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': round(len(winners) / len(results) * 100, 1) if results else 0,
        'avg_win': round(sum(t['pnl'] for t in winners) / len(winners), 0) if winners else 0,
        'avg_loss': round(sum(t['pnl'] for t in losers) / len(losers), 0) if losers else 0,
        'largest_win': round(max((t['pnl'] for t in winners), default=0), 0),
        'largest_loss': round(min((t['pnl'] for t in losers), default=0), 0),
    }

    for reason, group in by_reason.items():
        key = reason.lower().replace(' ', '_')
        s[f'{key}_count'] = len(group)
        s[f'{key}_pnl'] = round(sum(t['pnl'] for t in group), 0)

    return s


# ===================== OUTPUT =====================

def print_comparison(actual_sum, old_sum, new_sum):
    """Print side-by-side comparison."""
    print(f"\n{'=' * 95}")
    print(f"  EXIT STRATEGY REPLAY - March 11, 2026 (same entries, different exit rules)")
    print(f"{'=' * 95}")
    print(f"  {'Metric':<30} {'ACTUAL (bot)':>18} {'OLD (v10.2)':>18} {'NEW (v10.3d)':>18}")
    print(f"  {'-' * 88}")

    rows = [
        ('Total Trades', 'total_trades', 'd'),
        ('Total PnL', 'total_pnl', 'rs'),
        ('Winners', 'winners', 'd'),
        ('Losers', 'losers', 'd'),
        ('Win Rate', 'win_rate', 'pct'),
        ('Avg Win', 'avg_win', 'rs'),
        ('Avg Loss', 'avg_loss', 'rs'),
        ('Largest Win', 'largest_win', 'rs'),
        ('Largest Loss', 'largest_loss', 'rs'),
        ('BREAKOUT_FAIL', 'breakout_fail_count', 'd'),
        ('BF PnL', 'breakout_fail_pnl', 'rs'),
        ('TRAILING_SL_HIT', 'trailing_sl_hit_count', 'd'),
        ('TSL PnL', 'trailing_sl_hit_pnl', 'rs'),
        ('TARGET_HIT', 'target_hit_count', 'd'),
        ('Target PnL', 'target_hit_pnl', 'rs'),
        ('SL_HIT', 'sl_hit_count', 'd'),
        ('SL PnL', 'sl_hit_pnl', 'rs'),
        ('EOD_CLOSE', 'eod_close_count', 'd'),
        ('EOD PnL', 'eod_close_pnl', 'rs'),
        ('TIME_EXIT', 'time_exit_count', 'd'),
        ('Time PnL', 'time_exit_pnl', 'rs'),
    ]

    for label_str, key, fmt in rows:
        vals = []
        for d in [actual_sum, old_sum, new_sum]:
            v = d.get(key, None)
            if v is None:
                vals.append('-')
            elif fmt == 'rs':
                vals.append(f"Rs {v:+,.0f}")
            elif fmt == 'pct':
                vals.append(f"{v:.1f}%")
            else:
                vals.append(str(v))
        # Skip rows where all 3 are '-'
        if all(v == '-' for v in vals):
            continue
        print(f"  {label_str:<30} {vals[0]:>18} {vals[1]:>18} {vals[2]:>18}")

    old_pnl = old_sum.get('total_pnl', 0)
    new_pnl = new_sum.get('total_pnl', 0)
    act_pnl = actual_sum.get('total_pnl', 0)
    print(f"\n  v10.3d vs OLD:    Rs {new_pnl - old_pnl:+,.0f}")
    print(f"  v10.3d vs ACTUAL: Rs {new_pnl - act_pnl:+,.0f}")


def print_trades(results, label):
    """Print individual trade details."""
    print(f"\n  --- {label} ---")
    for t in sorted(results, key=lambda x: x.get('exit_time', '')):
        ext = f" ext#{t['target_extensions']}" if t.get('target_extensions', 0) > 0 else ""
        tsl = f" TSL={t['trailing_sl']:.0f}" if t.get('trailing_sl') else ""
        et = t['entry_time'][11:16]
        xt = t['exit_time'][11:16] if t.get('exit_time') else '?'
        print(f"    {et}->{xt} {t['symbol']:>10} {t['strategy']:>15} lot={t['lot_size']:>4} "
              f"entry={t['entry_premium']:>8.2f} peak={t['peak_premium']:>8.2f} "
              f"exit={t['exit_premium']:>8.2f} "
              f"PnL={t['pnl']:>+10,.0f}  {t['exit_reason']}{ext}{tsl}")


# ===================== MAIN =====================

def main():
    print("=" * 95)
    print("  v10.3d EXIT STRATEGY REPLAY - March 11, 2026")
    print("  Same entries as actual bot, comparing OLD vs NEW exit rules")
    print("  TSL FIX: TRENDING=40/25% (wide), FLAT=20/15% (tight)")
    print("  NEW adds: trailing target (extend 20%, TSL 15% below peak)")
    print("  Premium: delta*dS + 0.5*gamma*dS^2 - theta_decay")
    print("=" * 95)

    if not os.path.exists(EQUITY_SIGNALS) or not os.path.exists(EQUITY_PORTFOLIO):
        print("  ERROR: Missing signal or portfolio data files")
        return

    eq_signals = pd.read_csv(EQUITY_SIGNALS)
    print(f"\n  Equity signals: {len(eq_signals)} rows, {eq_signals['symbol'].nunique()} symbols")

    # Show regime timeline
    signals_sorted = eq_signals.sort_values('timestamp')
    detector = BacktestRegimeDetector()
    last_regimes = {}
    print(f"\n  Regime Timeline:")
    for sym in ['NIFTY', 'SENSEX', 'BANKNIFTY']:
        sym_data = signals_sorted[signals_sorted['symbol'] == sym].drop_duplicates(
            subset=['timestamp'], keep='first')
        det = BacktestRegimeDetector()
        print(f"    {sym}:")
        last_r = None
        for _, row in sym_data.iterrows():
            det.update(sym, row['spot'])
            r = det.get_regime(sym)
            if r != last_r:
                print(f"      {row['timestamp'][:19]} -> {r} (spot={row['spot']:.0f})")
                last_r = r

    # Load actual results
    with open(EQUITY_PORTFOLIO) as f:
        portfolio = json.load(f)
    actual_trades = portfolio['closed_trades']
    actual_sum = summarize([{
        'pnl': t['pnl'],
        'exit_reason': t['exit_reason'],
        'target_extensions': t.get('target_extensions', 0),
        'trailing_sl': t.get('trailing_sl'),
    } for t in actual_trades])

    # Run OLD exit rules
    print(f"\n  Running OLD (v10.2) exit replay...")
    old_results = run_exit_replay(EQUITY_PORTFOLIO, eq_signals, 'old')
    old_sum = summarize(old_results)

    # Run NEW exit rules
    print(f"  Running NEW (v10.3d) exit replay...")
    new_results = run_exit_replay(EQUITY_PORTFOLIO, eq_signals, 'new')
    new_sum = summarize(new_results)

    # Print comparison
    print_comparison(actual_sum, old_sum, new_sum)
    print_trades(old_results, "OLD v10.2 Exit Rules")
    print_trades(new_results, "NEW v10.3d Exit Rules (corrected TSL + trailing target)")

    # Show what changed
    print(f"\n  --- KEY DIFFERENCES ---")
    for old_t, new_t in zip(
        sorted(old_results, key=lambda x: x['id']),
        sorted(new_results, key=lambda x: x['id'])
    ):
        if old_t['exit_reason'] != new_t['exit_reason'] or abs(old_t['pnl'] - new_t['pnl']) > 100:
            diff = new_t['pnl'] - old_t['pnl']
            print(f"    {old_t['id'][:35]:<35} "
                  f"OLD: {old_t['exit_reason']:>20} PnL={old_t['pnl']:>+10,.0f}  |  "
                  f"NEW: {new_t['exit_reason']:>20} PnL={new_t['pnl']:>+10,.0f}  "
                  f"diff={diff:>+10,.0f}")

    print(f"\n{'=' * 95}")
    print(f"  BACKTEST COMPLETE")
    print(f"{'=' * 95}")


if __name__ == '__main__':
    main()
