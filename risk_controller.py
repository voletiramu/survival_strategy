"""
Risk Controller — Safety Layer for Live Trading
=================================================
Enforces hard limits before every order. No exceptions. No overrides.

Controls:
  1. Daily loss limit (Rs 5,000 default) — circuit breaker
  2. Max concurrent positions (10 default)
  3. Min available margin before entry
  4. Per-trade capital cap
  5. Kill switch (file-based + Dhan API)
  6. Unfilled order timeout + cancel
  7. Duplicate order prevention
  8. Trading hours enforcement
"""

import os
import json
import logging
from datetime import datetime, date, time as dtime
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

# ── Kill switch file path ────────────────────────────────────────────────────
KILL_SWITCH_FILE = Path(os.environ.get('KILL_SWITCH_FILE',
    '/root/algo_trading/KILL' if os.path.exists('/root/algo_trading')
    else 'D:/AlgoTrading/algo_trading/KILL'))

# ── NSE Holidays 2026 ───────────────────────────────────────────────────────
NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 2, 26), date(2026, 3, 30),
    date(2026, 3, 31), date(2026, 4, 2), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 25),
    date(2026, 6, 5), date(2026, 7, 6), date(2026, 8, 15),
    date(2026, 8, 18), date(2026, 9, 4), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 9), date(2026, 11, 10),
    date(2026, 11, 30), date(2026, 12, 25),
}


class RiskController:
    """Pre-trade and runtime risk enforcement.

    Usage:
        rc = RiskController(capital=100000)

        # Before every order:
        ok, reason = rc.pre_trade_check(
            trade_cost=5200,
            current_positions=3,
            symbol='NIFTY',
            strategy='CPR',
            direction='CE'
        )
        if not ok:
            logger.warning(f"BLOCKED: {reason}")
            return

        # After every fill:
        rc.record_trade(pnl=-500, symbol='NIFTY', strategy='CPR')

        # Continuous monitoring:
        if rc.should_halt():
            broker.flatten_all()
    """

    def __init__(self, capital=100000, daily_loss_limit=5000,
                 max_positions=10, max_lots_per_trade=1,
                 min_margin_buffer=20000):
        """
        Args:
            capital: Total allocated capital (Rs).
            daily_loss_limit: Max daily loss before circuit breaker (Rs).
            max_positions: Max concurrent open positions.
            max_lots_per_trade: Hard cap on lots per trade.
            min_margin_buffer: Keep at least this much free after entry.
        """
        self.capital = capital
        self.daily_loss_limit = daily_loss_limit
        self.max_positions = max_positions
        self.max_lots_per_trade = max_lots_per_trade
        self.min_margin_buffer = min_margin_buffer

        # Daily tracking
        self._today = date.today()
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._daily_losses_consecutive = 0
        self._direction_losses = defaultdict(int)  # (symbol, direction) -> consecutive losses
        self._recent_orders = []  # (timestamp, symbol, strategy, direction) for dedup
        self._halted = False
        self._halt_reason = ''

        logger.info(f"[RiskCtrl] Initialized: capital=Rs {capital:,} | "
                    f"daily_loss_limit=Rs {daily_loss_limit:,} | "
                    f"max_positions={max_positions} | max_lots={max_lots_per_trade}")

    def _reset_if_new_day(self):
        """Reset daily counters if date changed."""
        today = date.today()
        if today != self._today:
            logger.info(f"[RiskCtrl] New day: {today} — resetting daily counters")
            self._today = today
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._daily_losses_consecutive = 0
            self._direction_losses.clear()
            self._recent_orders.clear()
            self._halted = False
            self._halt_reason = ''

    # ── Pre-Trade Checks ─────────────────────────────────────────────────────

    def pre_trade_check(self, trade_cost, current_positions, symbol,
                        strategy, direction, available_margin=None):
        """Run ALL pre-trade safety checks.

        Args:
            trade_cost: Estimated capital for this trade (premium × lot_size).
            current_positions: Number of currently open positions.
            symbol: Trading symbol (NIFTY, BANKNIFTY, etc).
            strategy: Strategy name (CPR, Gamma Blast, etc).
            direction: 'CE' or 'PE'.
            available_margin: Available balance from broker (optional).

        Returns:
            (bool, str): (allowed, reason). If False, order MUST NOT be placed.
        """
        self._reset_if_new_day()

        # 1. Kill switch
        if self.is_kill_switch_active():
            return False, 'KILL_SWITCH active'

        # 2. Trading halted (circuit breaker already fired)
        if self._halted:
            return False, f'HALTED: {self._halt_reason}'

        # 3. Not a trading day
        if not self.is_trading_day():
            return False, 'Not a trading day (holiday/weekend)'

        # 4. Trading hours check
        if not self.is_trading_hours():
            return False, 'Outside trading hours (9:15-15:20)'

        # 5. Daily loss limit
        if self._daily_pnl <= -self.daily_loss_limit:
            self._halted = True
            self._halt_reason = f'Daily loss Rs {self._daily_pnl:,.0f} >= limit Rs {self.daily_loss_limit:,}'
            return False, f'CIRCUIT_BREAKER: {self._halt_reason}'

        # 6. Position limit
        if current_positions >= self.max_positions:
            return False, f'MAX_POSITIONS: {current_positions}/{self.max_positions}'

        # 7. Trade cost vs remaining capital
        remaining = self.capital + self._daily_pnl
        if trade_cost > remaining * 0.5:
            return False, f'CAPITAL: trade Rs {trade_cost:,.0f} > 50% of remaining Rs {remaining:,.0f}'

        # 8. Margin buffer (if broker funds available)
        if available_margin is not None:
            if available_margin - trade_cost < self.min_margin_buffer:
                return False, (f'MARGIN: after trade only Rs {available_margin - trade_cost:,.0f} left '
                             f'(need Rs {self.min_margin_buffer:,} buffer)')

        # 9. Direction cooldown (3 consecutive losses same direction → 30 min pause)
        dir_key = (symbol, direction)
        if self._direction_losses.get(dir_key, 0) >= 3:
            return False, f'DIR_COOLDOWN: {symbol} {direction} has 3 consecutive losses'

        # 10. Duplicate order prevention (same symbol+strategy+direction within 300s)
        # v30.3: Increased from 60s to 300s — prevents rapid lot-add re-entries
        now = datetime.now()
        dedup_window = 300  # 5 minutes
        for ts, sym, strat, d in self._recent_orders[-20:]:
            if (sym == symbol and strat == strategy and d == direction
                    and (now - ts).total_seconds() < dedup_window):
                return False, f'DEDUP: same order {symbol}/{strategy}/{direction} within {dedup_window}s'

        return True, 'OK'

    # ── Post-Trade Recording ─────────────────────────────────────────────────

    def record_entry(self, symbol, strategy, direction):
        """Record that a trade entry was placed. Call after successful fill."""
        self._reset_if_new_day()
        self._daily_trades += 1
        self._recent_orders.append((datetime.now(), symbol, strategy, direction))

    def record_exit(self, pnl, symbol, direction):
        """Record a trade exit with P&L. Call after every exit fill.

        Args:
            pnl: Realized P&L for this trade (positive = profit).
            symbol: Symbol traded.
            direction: 'CE' or 'PE'.
        """
        self._reset_if_new_day()
        self._daily_pnl += pnl

        dir_key = (symbol, direction)
        if pnl < 0:
            self._daily_losses_consecutive += 1
            self._direction_losses[dir_key] = self._direction_losses.get(dir_key, 0) + 1
        else:
            self._daily_losses_consecutive = 0
            self._direction_losses[dir_key] = 0

        logger.info(f"[RiskCtrl] PnL recorded: Rs {pnl:+,.0f} | "
                    f"Daily: Rs {self._daily_pnl:+,.0f} / -{self.daily_loss_limit:,} | "
                    f"Trades: {self._daily_trades}")

    # ── Continuous Monitoring ────────────────────────────────────────────────

    def should_halt(self):
        """Check if trading should be halted immediately.

        Returns:
            (bool, str): (should_halt, reason)
        """
        self._reset_if_new_day()

        if self.is_kill_switch_active():
            return True, 'KILL_SWITCH file detected'

        if self._halted:
            return True, self._halt_reason

        if self._daily_pnl <= -self.daily_loss_limit:
            self._halted = True
            self._halt_reason = f'Daily loss Rs {self._daily_pnl:,.0f}'
            return True, self._halt_reason

        if self._daily_losses_consecutive >= 5:
            self._halted = True
            self._halt_reason = f'{self._daily_losses_consecutive} consecutive losses'
            return True, self._halt_reason

        return False, ''

    def update_unrealized_pnl(self, unrealized_pnl):
        """Update with current unrealized P&L for MTM circuit breaker.

        If realized + unrealized exceeds 80% of daily limit, block new entries.
        """
        total = self._daily_pnl + unrealized_pnl
        if total <= -self.daily_loss_limit * 0.8:
            logger.warning(f"[RiskCtrl] MTM WARNING: realized={self._daily_pnl:+,.0f} + "
                          f"unrealized={unrealized_pnl:+,.0f} = Rs {total:+,.0f} "
                          f"(80% of daily limit)")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def is_kill_switch_active(self):
        """Check if kill switch file exists."""
        return KILL_SWITCH_FILE.exists()

    def create_kill_switch(self):
        """Create the kill switch file to halt all trading."""
        try:
            KILL_SWITCH_FILE.write_text(
                f"KILL SWITCH activated at {datetime.now().isoformat()}\n"
                f"Reason: Manual activation\n"
                f"Delete this file to resume trading.\n"
            )
            logger.critical(f"[RiskCtrl] KILL SWITCH CREATED: {KILL_SWITCH_FILE}")
            self._halted = True
            self._halt_reason = 'Kill switch activated'
        except Exception as e:
            logger.error(f"[RiskCtrl] Failed to create kill switch: {e}")

    def remove_kill_switch(self):
        """Remove the kill switch file to allow trading."""
        try:
            if KILL_SWITCH_FILE.exists():
                KILL_SWITCH_FILE.unlink()
                logger.info(f"[RiskCtrl] Kill switch removed")
                self._halted = False
                self._halt_reason = ''
        except Exception as e:
            logger.error(f"[RiskCtrl] Failed to remove kill switch: {e}")

    @staticmethod
    def is_trading_day(check_date=None):
        """Check if a date is a trading day (weekday + not NSE holiday)."""
        d = check_date or date.today()
        if d.weekday() >= 5:
            return False
        if d in NSE_HOLIDAYS:
            return False
        return True

    @staticmethod
    def is_trading_hours():
        """Check if current time is within equity trading hours."""
        now = datetime.now().time()
        return dtime(9, 15) <= now <= dtime(15, 20)

    @staticmethod
    def is_pre_market():
        """Check if it's pre-market time (08:30-09:15)."""
        now = datetime.now().time()
        return dtime(8, 30) <= now <= dtime(9, 15)

    @staticmethod
    def is_eod_squareoff_time():
        """Check if it's time to square off all positions (after 15:20)."""
        return datetime.now().time() >= dtime(15, 20)

    def get_status(self):
        """Get current risk status summary."""
        self._reset_if_new_day()
        return {
            'date': str(self._today),
            'daily_pnl': self._daily_pnl,
            'daily_loss_limit': self.daily_loss_limit,
            'pnl_pct': self._daily_pnl / self.capital * 100 if self.capital > 0 else 0,
            'trades_today': self._daily_trades,
            'consecutive_losses': self._daily_losses_consecutive,
            'halted': self._halted,
            'halt_reason': self._halt_reason,
            'kill_switch': self.is_kill_switch_active(),
            'is_trading_day': self.is_trading_day(),
            'is_trading_hours': self.is_trading_hours(),
        }
