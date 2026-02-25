"""
Backtest engine for options trading strategies.
Simulates option selling and buying strategies on Indian indices.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class TradeType(Enum):
    BUY_CE = "BUY_CE"
    BUY_PE = "BUY_PE"
    SELL_CE = "SELL_CE"
    SELL_PE = "SELL_PE"


@dataclass
class Trade:
    entry_date: pd.Timestamp
    exit_date: Optional[pd.Timestamp]
    trade_type: TradeType
    entry_price: float  # spot price at entry
    exit_price: float = 0.0  # spot price at exit
    option_entry_premium: float = 0.0
    option_exit_premium: float = 0.0
    strike: float = 0.0
    quantity: int = 1
    pnl: float = 0.0
    status: str = "OPEN"


@dataclass
class BacktestResult:
    strategy_name: str
    symbol: str
    timeframe: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_trade_duration: float = 0.0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    calmar_ratio: float = 0.0
    equity_curve: pd.Series = field(default_factory=pd.Series)
    daily_returns: pd.Series = field(default_factory=pd.Series)
    trades: List[Trade] = field(default_factory=list)
    monthly_returns: pd.Series = field(default_factory=pd.Series)


class BacktestEngine:
    """Core backtesting engine."""

    def __init__(self, initial_capital: float = 300000.0, lot_size: int = 25,
                 max_risk_per_trade_pct: float = 2.0, brokerage_per_order: float = 20.0,
                 slippage_points: float = 2.0):
        self.initial_capital = initial_capital
        self.lot_size = lot_size
        self.max_risk_per_trade = initial_capital * max_risk_per_trade_pct / 100
        self.brokerage = brokerage_per_order
        self.slippage = slippage_points
        self.capital = initial_capital
        self.trades: List[Trade] = []
        self.equity_history: List[float] = []
        self.date_history: List[pd.Timestamp] = []

    def reset(self):
        self.capital = self.initial_capital
        self.trades = []
        self.equity_history = []
        self.date_history = []

    def estimate_option_premium(self, spot: float, strike: float, is_call: bool,
                                 dte: int = 0, iv_pct: float = 15.0) -> float:
        """
        Rough option premium estimation using simplified Black-Scholes-like approximation.
        For backtesting purposes - not for real trading.
        """
        moneyness = (spot - strike) if is_call else (strike - spot)
        intrinsic = max(0, moneyness)

        # Time value approximation
        if dte <= 0:
            dte = 1
        time_factor = np.sqrt(dte / 365)
        time_value = spot * (iv_pct / 100) * time_factor * 0.4  # rough approximation

        # ATM options have max time value
        atm_distance = abs(spot - strike) / spot
        time_value *= max(0, 1 - atm_distance * 10)

        premium = intrinsic + time_value
        return max(premium, 1.0)  # minimum 1 rupee

    def simulate_option_pnl_from_spot(self, spot_entry: float, spot_exit: float,
                                       trade_type: TradeType, lots: int = 1) -> float:
        """
        Simulate option P&L from spot price movement.
        Uses delta-based approximation for ATM options.
        """
        spot_move = spot_exit - spot_entry
        qty = lots * self.lot_size

        if trade_type == TradeType.BUY_CE:
            # Long call benefits from up move
            delta = 0.5
            pnl = spot_move * delta * qty
        elif trade_type == TradeType.BUY_PE:
            # Long put benefits from down move
            delta = 0.5
            pnl = -spot_move * delta * qty
        elif trade_type == TradeType.SELL_CE:
            # Short call benefits from down move (theta + direction)
            delta = 0.5
            pnl = -spot_move * delta * qty
        elif trade_type == TradeType.SELL_PE:
            # Short put benefits from up move (theta + direction)
            delta = 0.5
            pnl = spot_move * delta * qty
        else:
            pnl = 0.0

        # Subtract brokerage and slippage
        pnl -= (self.brokerage * 2 + self.slippage * qty)
        return pnl

    def add_trade(self, trade: Trade):
        self.trades.append(trade)
        self.capital += trade.pnl

    def record_equity(self, date: pd.Timestamp):
        self.equity_history.append(self.capital)
        self.date_history.append(date)

    def compute_results(self, strategy_name: str, symbol: str,
                        timeframe: str = "daily") -> BacktestResult:
        """Compute comprehensive backtest statistics."""
        result = BacktestResult(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            trades=self.trades.copy()
        )

        if not self.trades:
            return result

        pnls = [t.pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        result.total_trades = len(self.trades)
        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = len(wins) / len(pnls) * 100 if pnls else 0
        result.total_pnl = sum(pnls)
        result.avg_win = np.mean(wins) if wins else 0
        result.avg_loss = np.mean(losses) if losses else 0
        result.largest_win = max(wins) if wins else 0
        result.largest_loss = min(losses) if losses else 0
        result.profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')

        # Equity curve
        if self.equity_history:
            equity = pd.Series(self.equity_history, index=self.date_history)
            result.equity_curve = equity

            # Daily returns
            daily_ret = equity.pct_change().dropna()
            result.daily_returns = daily_ret

            # Max drawdown
            peak = equity.expanding().max()
            drawdown = equity - peak
            result.max_drawdown = drawdown.min()
            result.max_drawdown_pct = (drawdown / peak).min() * 100

            # Returns
            result.total_return_pct = (self.capital - self.initial_capital) / self.initial_capital * 100
            n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
            if self.capital > 0 and self.initial_capital > 0:
                result.annualized_return_pct = ((self.capital / self.initial_capital) ** (1 / n_years) - 1) * 100

            # Sharpe ratio (annualized, risk-free rate ~6% for India)
            if len(daily_ret) > 1 and daily_ret.std() > 0:
                rf_daily = 0.06 / 252
                excess = daily_ret - rf_daily
                result.sharpe_ratio = np.sqrt(252) * excess.mean() / excess.std()

            # Sortino ratio
            if len(daily_ret) > 1:
                downside = daily_ret[daily_ret < 0]
                if len(downside) > 0 and downside.std() > 0:
                    result.sortino_ratio = np.sqrt(252) * (daily_ret.mean() - 0.06/252) / downside.std()

            # Calmar ratio
            if result.max_drawdown_pct != 0:
                result.calmar_ratio = result.annualized_return_pct / abs(result.max_drawdown_pct)

            # Monthly returns
            if len(equity) > 20:
                monthly = equity.resample('ME').last().pct_change().dropna()
                result.monthly_returns = monthly

        # Average trade duration
        durations = []
        for t in self.trades:
            if t.exit_date and t.entry_date:
                dur = (t.exit_date - t.entry_date).days
                durations.append(dur)
        result.avg_trade_duration = np.mean(durations) if durations else 0

        return result
