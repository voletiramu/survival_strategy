"""
Monte Carlo Live Trade Monitor — Self-Healing Strategy Gate
=============================================================
Runs quick MC validation after every trade close.
Auto-disables strategies when edge degrades, re-enables next morning.

Integration:
    from mc_monitor import MCMonitor
    monitor = MCMonitor()

    # After each trade closes:
    monitor.record_trade(strategy, symbol, net_pnl, date)
    allowed, reason = monitor.is_strategy_allowed(strategy)

    # At 9:15 AM daily:
    monitor.reset_daily()

Tests (fast — 100 iterations):
    1. Bootstrap: Resample daily PnLs, check % negative
    2. Expectancy: avg_win * WR - avg_loss * LR > threshold
    3. Consecutive Losses: Flag after N consecutive stops

Thresholds:
    - Bootstrap negative > 30% → FLAG
    - Expectancy < Rs 0 → FLAG
    - 4+ consecutive losses → FLAG
    - 2 flags in same day → DISABLE strategy for today
    - Next morning → auto re-enable
"""

import logging
import time
import json
import os
import numpy as np
from datetime import datetime, date, time as dtime
from collections import defaultdict, deque
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────

# Rolling window for MC analysis
ROLLING_WINDOW = 15           # Last N trades per strategy
MIN_TRADES_FOR_MC = 5         # Need at least 5 trades to run MC

# MC parameters (fast — runs after every trade)
MC_BOOTSTRAP_ITERATIONS = 100
MC_BOOTSTRAP_NEGATIVE_THRESHOLD = 30  # Flag if >30% resamples negative

# Expectancy threshold
EXPECTANCY_THRESHOLD = 0      # Must be > Rs 0 per trade

# Consecutive loss threshold
CONSECUTIVE_LOSS_THRESHOLD = 4  # Flag after 4 consecutive losses

# Flags needed to disable
FLAGS_TO_DISABLE = 2           # 2 flags in same day → disable

# State file
STATE_DIR = Path(os.environ.get('ALGO_DATA_DIR', '/root/algo_trading/data'))
STATE_FILE = STATE_DIR / 'mc_monitor_state.json'


class MCMonitor:
    """Live Monte Carlo trade monitor for all strategies."""

    def __init__(self):
        self._trades = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
        self._daily_flags = defaultdict(int)       # strategy → flag count today
        self._disabled = set()                      # strategies disabled today
        self._consecutive_losses = defaultdict(int) # strategy → current loss streak
        self._last_mc_result = {}                   # strategy → last MC result dict
        self._today = date.today().isoformat()
        self._load_state()

    def _load_state(self):
        """Load persistent state (trade history survives restarts)."""
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                for strat, trades in data.get('trades', {}).items():
                    for t in trades[-ROLLING_WINDOW:]:
                        self._trades[strat].append(t)
                self._disabled = set(data.get('disabled', []))
                self._daily_flags = defaultdict(int, data.get('daily_flags', {}))
                saved_date = data.get('date', '')
                if saved_date != self._today:
                    # New day — reset flags and re-enable all
                    self._daily_flags = defaultdict(int)
                    self._disabled = set()
                logger.info("[MCMonitor] Loaded: %d strategies tracked, %d disabled" %
                           (len(self._trades), len(self._disabled)))
        except Exception as e:
            logger.warning("[MCMonitor] State load failed: %s" % e)

    def _save_state(self):
        """Persist state to disk."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                'date': self._today,
                'trades': {s: list(t) for s, t in self._trades.items()},
                'disabled': list(self._disabled),
                'daily_flags': dict(self._daily_flags),
                'last_mc': self._last_mc_result,
            }
            STATE_FILE.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.warning("[MCMonitor] State save failed: %s" % e)

    def record_trade(self, strategy, symbol, net_pnl, trade_date=None):
        """Record a closed trade and update MC state.

        Call this AFTER each trade closes. Updates the rolling window
        so the next should_enter() call reflects this trade's result.

        Args:
            strategy: Strategy name (e.g., 'CPR', 'Wave', 'Ghost Zone v8')
            symbol: Trading symbol (e.g., 'NIFTY', 'BANKNIFTY')
            net_pnl: Net P&L after costs
            trade_date: Date string (default: today)
        """
        if trade_date is None:
            trade_date = date.today().isoformat()

        if trade_date != self._today:
            self.reset_daily()
            self._today = trade_date

        self._trades[strategy].append({
            'pnl': net_pnl,
            'symbol': symbol,
            'date': trade_date,
            'time': datetime.now().isoformat(),
        })

        if net_pnl <= 0:
            self._consecutive_losses[strategy] += 1
        else:
            self._consecutive_losses[strategy] = 0

        # Update MC result after recording
        result = self._run_mc_check(strategy)
        self._last_mc_result[strategy] = result

        if result.get('flagged', False):
            self._daily_flags[strategy] += 1
            logger.warning("[MCMonitor] FLAG #%d for %s: %s" %
                          (self._daily_flags[strategy], strategy, result.get('reason', '')))

        self._save_state()
        return result

    def should_enter(self, strategy, premium=0, symbol=''):
        """PRE-TRADE gate — call BEFORE entering any trade.

        This is the core function. Checks if the strategy has enough
        edge to justify a new entry based on its recent trade history.

        Args:
            strategy: Strategy name
            premium: Entry premium (for position sizing)
            symbol: Symbol being traded

        Returns:
            (bool, str): (allowed, reason)
        """
        # New day check
        today = date.today().isoformat()
        if today != self._today:
            self.reset_daily()
            self._today = today

        # Check if explicitly disabled (hit flag limit)
        if strategy in self._disabled:
            return False, 'MC_DISABLED: edge degraded (%d flags today)' % self._daily_flags.get(strategy, 0)

        # Not enough history — allow (give benefit of doubt)
        trades = list(self._trades.get(strategy, []))
        if len(trades) < MIN_TRADES_FOR_MC:
            return True, 'MC_OK: building history (%d/%d trades)' % (len(trades), MIN_TRADES_FOR_MC)

        # Run MC check on current window
        result = self._run_mc_check(strategy)

        # Hard block: consecutive losses >= threshold
        streak = self._consecutive_losses.get(strategy, 0)
        if streak >= CONSECUTIVE_LOSS_THRESHOLD:
            self._daily_flags[strategy] = self._daily_flags.get(strategy, 0) + 1
            if self._daily_flags[strategy] >= FLAGS_TO_DISABLE:
                self._disabled.add(strategy)
                self._save_state()
            return False, 'MC_BLOCK: %d consecutive losses (threshold=%d)' % (streak, CONSECUTIVE_LOSS_THRESHOLD)

        # Hard block: negative expectancy
        if result.get('expectancy', 0) < EXPECTANCY_THRESHOLD and len(trades) >= 8:
            return False, 'MC_BLOCK: negative expectancy Rs %.0f' % result.get('expectancy', 0)

        # Hard block: bootstrap shows >40% chance of being negative
        if result.get('bootstrap_neg_pct', 0) > 40 and len(trades) >= 8:
            return False, 'MC_BLOCK: %.0f%% bootstrap negative' % result.get('bootstrap_neg_pct', 0)

        # Soft warning: flag but allow (first flag doesn't block)
        if result.get('flagged', False):
            return True, 'MC_WARN: %s (allowed but flagged)' % result.get('reason', '')

        return True, 'MC_OK: edge confirmed (WR=%.0f%% Exp=Rs %.0f)' % (
            result.get('wr', 0), result.get('expectancy', 0))

    def _run_mc_check(self, strategy):
        """Run quick MC validation on rolling window of trades."""
        trades = list(self._trades[strategy])

        if len(trades) < MIN_TRADES_FOR_MC:
            return {
                'strategy': strategy,
                'trades': len(trades),
                'status': 'INSUFFICIENT',
                'flagged': False,
                'reason': 'Need %d+ trades (have %d)' % (MIN_TRADES_FOR_MC, len(trades)),
            }

        pnls = [t['pnl'] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)
        wr = len(wins) / len(pnls) * 100
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses)) if losses else 0
        expectancy = avg_win * (wr / 100) - avg_loss * (1 - wr / 100)

        # Group by date for bootstrap
        daily = defaultdict(float)
        for t in trades:
            daily[t['date']] += t['pnl']
        daily_values = list(daily.values())

        flags = []

        # Test 1: Bootstrap
        if len(daily_values) >= 3:
            neg_count = 0
            for _ in range(MC_BOOTSTRAP_ITERATIONS):
                sample = np.random.choice(daily_values, size=len(daily_values), replace=True)
                if sample.sum() <= 0:
                    neg_count += 1
            bootstrap_neg_pct = neg_count / MC_BOOTSTRAP_ITERATIONS * 100

            if bootstrap_neg_pct > MC_BOOTSTRAP_NEGATIVE_THRESHOLD:
                flags.append('BOOTSTRAP_FAIL(%.0f%% negative)' % bootstrap_neg_pct)
        else:
            bootstrap_neg_pct = 0

        # Test 2: Expectancy
        if expectancy <= EXPECTANCY_THRESHOLD:
            flags.append('EXPECTANCY_FAIL(Rs %.0f)' % expectancy)

        # Test 3: Consecutive losses
        streak = self._consecutive_losses.get(strategy, 0)
        if streak >= CONSECUTIVE_LOSS_THRESHOLD:
            flags.append('CONSEC_LOSS(%d in a row)' % streak)

        # Test 4: Win rate collapse (below 35%)
        if wr < 35 and len(trades) >= 8:
            flags.append('WR_COLLAPSE(%.0f%%)' % wr)

        flagged = len(flags) > 0

        return {
            'strategy': strategy,
            'trades': len(trades),
            'total_pnl': total_pnl,
            'wr': wr,
            'expectancy': expectancy,
            'bootstrap_neg_pct': bootstrap_neg_pct,
            'consecutive_losses': streak,
            'flagged': flagged,
            'flags': flags,
            'reason': ', '.join(flags) if flags else 'HEALTHY',
            'status': 'FLAGGED' if flagged else 'HEALTHY',
        }

    def is_strategy_allowed(self, strategy):
        """Check if a strategy is allowed to trade.

        Returns:
            (bool, str): (allowed, reason)
        """
        if strategy in self._disabled:
            flags = self._daily_flags.get(strategy, 0)
            return False, 'MC_DISABLED: %d flags today (edge degraded)' % flags

        return True, 'MC_OK'

    def reset_daily(self):
        """Reset daily flags and re-enable all strategies. Call at 9:15 AM."""
        if self._disabled:
            logger.info("[MCMonitor] Daily reset: Re-enabling %d strategies: %s" %
                       (len(self._disabled), ', '.join(self._disabled)))
        self._daily_flags = defaultdict(int)
        self._disabled = set()
        self._consecutive_losses = defaultdict(int)
        self._today = date.today().isoformat()
        self._save_state()

    def get_status(self):
        """Get current status of all monitored strategies."""
        status = {}
        for strat in self._trades:
            result = self._run_mc_check(strat)
            result['disabled'] = strat in self._disabled
            result['daily_flags'] = self._daily_flags.get(strat, 0)
            status[strat] = result
        return status

    def get_summary(self):
        """Get a one-line summary for logging/dashboard."""
        active = []
        disabled = []
        for strat in sorted(self._trades.keys()):
            trades = len(self._trades[strat])
            if strat in self._disabled:
                disabled.append('%s(%dt)' % (strat, trades))
            else:
                result = self._last_mc_result.get(strat, {})
                health = result.get('status', '?')
                active.append('%s(%dt,%s)' % (strat, trades, health))

        parts = []
        if active:
            parts.append('Active: %s' % ', '.join(active))
        if disabled:
            parts.append('DISABLED: %s' % ', '.join(disabled))
        return ' | '.join(parts) if parts else 'No trades yet'

    def format_telegram_alert(self, strategy, action, result):
        """Format a Telegram notification for strategy state change."""
        if action == 'DISABLED':
            emoji = '🔴'
            title = 'STRATEGY DISABLED'
        elif action == 'FLAGGED':
            emoji = '🟡'
            title = 'STRATEGY FLAGGED'
        else:
            emoji = '🟢'
            title = 'STRATEGY RE-ENABLED'

        msg = (
            "%s <b>%s: %s</b>\n"
            "Trades: %d | WR: %.0f%% | PnL: Rs %+.0f\n"
            "Expectancy: Rs %+.0f/trade\n"
            "Bootstrap: %.0f%% negative\n"
            "Consecutive losses: %d\n"
            "Reason: %s"
        ) % (
            emoji, title, strategy,
            result.get('trades', 0), result.get('wr', 0), result.get('total_pnl', 0),
            result.get('expectancy', 0),
            result.get('bootstrap_neg_pct', 0),
            result.get('consecutive_losses', 0),
            result.get('reason', ''),
        )
        return msg


# ── Standalone testing ────────────────────────────────────────────────────

def simulate_mc_monitor(trades_by_strategy):
    """Simulate the MC monitor on historical trade data.

    Args:
        trades_by_strategy: dict of {strategy_name: [list of trade dicts]}
    """
    monitor = MCMonitor()
    # Clear any loaded state for fresh simulation
    monitor._trades = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    monitor._daily_flags = defaultdict(int)
    monitor._disabled = set()
    monitor._consecutive_losses = defaultdict(int)

    events = []

    # Sort all trades by time
    all_trades = []
    for strat, trades in trades_by_strategy.items():
        for t in trades:
            t['strategy'] = strat
            all_trades.append(t)

    all_trades.sort(key=lambda t: str(t.get('exit_time', t.get('entry_time', ''))))

    disabled_log = defaultdict(list)  # date → [strategies disabled]
    daily_results = defaultdict(lambda: defaultdict(float))

    for t in all_trades:
        strat = t['strategy']
        trade_date = t.get('date', str(t.get('exit_time', ''))[:10])
        pnl = t.get('net_pnl', 0)

        # PRE-TRADE gate: should_enter() decides BEFORE entry
        allowed, reason = monitor.should_enter(strat, t.get('entry_premium', 0), t.get('symbol', ''))

        if not allowed:
            # MC blocked this trade BEFORE it happened
            events.append({
                'date': trade_date, 'strategy': strat,
                'pnl': pnl, 'action': 'BLOCKED',
                'reason': reason,
            })
            daily_results[trade_date][strat + '_blocked'] += pnl
            if strat in monitor._disabled and strat not in disabled_log.get(trade_date, []):
                disabled_log[trade_date].append(strat)
            continue

        # Trade allowed — record it after close (updates rolling window)
        monitor.record_trade(strat, t.get('symbol', ''), pnl, trade_date)

        events.append({
            'date': trade_date, 'strategy': strat,
            'pnl': pnl, 'action': 'ALLOWED',
            'reason': reason,
        })

        daily_results[trade_date][strat + '_taken'] += pnl

    return events, disabled_log, monitor


if __name__ == '__main__':
    # Quick self-test
    monitor = MCMonitor()

    # Simulate a series of trades
    print("MC Monitor Self-Test")
    print("=" * 60)

    # Good strategy: consistent winners
    for i in range(10):
        pnl = 500 + np.random.normal(0, 200)
        r = monitor.record_trade('TestGood', 'NIFTY', pnl, '2026-03-25')
    print("TestGood: %s" % r['reason'])

    # Bad strategy: losing streak
    for i in range(6):
        pnl = -300 + np.random.normal(0, 100)
        r = monitor.record_trade('TestBad', 'NIFTY', pnl, '2026-03-25')
    print("TestBad: %s" % r['reason'])

    # Check status
    allowed_good, reason_good = monitor.is_strategy_allowed('TestGood')
    allowed_bad, reason_bad = monitor.is_strategy_allowed('TestBad')
    print("TestGood allowed: %s (%s)" % (allowed_good, reason_good))
    print("TestBad allowed: %s (%s)" % (allowed_bad, reason_bad))

    print("\nSummary: %s" % monitor.get_summary())
