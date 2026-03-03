"""
Backtest engine for options trading strategies.
Simulates option selling and buying strategies on Indian indices.

v5: Premium-based PnL using Black-Scholes pricing.
- Computes actual option premiums at entry and exit
- Proper strike selection (ATM rounding)
- IV estimation from ATR (annualized historical vol)
- DTE computation for weekly expiry cycles
- Realistic premium behaviour: gamma, theta, IV crush all naturally captured
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
from scipy.stats import norm

# Strike intervals for Indian indices
STRIKE_INTERVALS = {'NIFTY50': 50, 'BANKNIFTY': 100, 'SENSEX': 100,
                    'NIFTY': 50}
RISK_FREE_RATE = 0.065  # India 10Y ~6.5%


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             opt_type: str = 'CE') -> float:
    """Black-Scholes option price.

    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry in years (e.g., 1/365 for 1 day)
        r: Risk-free rate (decimal)
        sigma: Implied volatility (decimal, e.g., 0.15 for 15%)
        opt_type: 'CE' for call, 'PE' for put

    Returns:
        Option premium (price per unit)
    """
    T = max(T, 1e-6)
    sigma = max(sigma, 0.01)
    S = max(S, 0.01)
    K = max(K, 0.01)

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if opt_type == 'CE':
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return max(price, 0.05)  # Minimum 5 paise


def get_strike(spot: float, symbol: str = 'NIFTY50') -> float:
    """Get ATM strike by rounding spot to nearest strike interval."""
    interval = STRIKE_INTERVALS.get(symbol, 50)
    return round(spot / interval) * interval


def estimate_iv_from_atr(atr: float, spot: float) -> float:
    """Estimate implied volatility from ATR.

    Historical vol ≈ ATR/spot × sqrt(252)
    IV typically trades at 1.1-1.3x historical vol (risk premium).
    Clamped to 8%-80% range for realistic Indian index options.
    """
    if spot <= 0 or atr <= 0:
        return 0.15  # Default 15%
    hist_vol = (atr / spot) * np.sqrt(252)
    iv = hist_vol * 1.2  # IV premium over historical
    return max(0.08, min(0.80, iv))


def compute_dte(date: pd.Timestamp, symbol: str = 'NIFTY50') -> int:
    """Estimate days to nearest weekly expiry.

    NIFTY: Thursday expiry
    BANKNIFTY: Wednesday expiry (changed to Friday from some date, but we use Wed for historical)
    SENSEX: Friday expiry

    Returns DTE in calendar days (min 0 = expiry day itself).
    """
    dow = date.weekday()  # 0=Mon, 6=Sun

    if symbol in ('BANKNIFTY',):
        expiry_dow = 2  # Wednesday
    elif symbol in ('SENSEX',):
        expiry_dow = 4  # Friday
    else:  # NIFTY50, NIFTY
        expiry_dow = 3  # Thursday

    days_ahead = (expiry_dow - dow) % 7
    if days_ahead == 0:
        return 0  # Today is expiry
    return days_ahead


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
                                       trade_type: TradeType, lots: int = 1,
                                       delta: float = 0.5, gamma: float = 0.0,
                                       theta_pct: float = 0.0,
                                       entry_premium: float = 0.0) -> float:
        """
        Simulate option P&L from spot price movement with realistic adjustments.

        v4: Added theta decay, slippage penalty, and premium-capped P&L.
        - Theta decay reduces buyer profits and helps sellers
        - For BUY trades: profit capped by realistic premium movement
        - For SELL trades: profit capped by premium collected
        - Slippage penalty scaled to move size (larger moves = worse fills)

        Args:
            spot_entry: Spot price at entry
            spot_exit: Spot price at exit
            trade_type: BUY_CE, BUY_PE, SELL_CE, SELL_PE
            lots: Number of lots
            delta: Starting delta (0.4-0.6 for ATM, default 0.5)
            gamma: Delta change per point of spot move (0 = no gamma effect)
            theta_pct: Daily theta decay as fraction of premium (0.05 = 5%)
            entry_premium: Estimated entry premium (if 0, auto-estimated)
        """
        spot_move = spot_exit - spot_entry
        abs_move = abs(spot_move)
        qty = lots * self.lot_size

        # Auto-estimate premium if not provided (ATM ~ 1.5% of spot)
        if entry_premium <= 0:
            entry_premium = spot_entry * 0.015

        # If gamma is provided, compute average delta over the move
        if gamma > 0 and abs_move > 0:
            end_delta = min(delta + gamma * abs_move, 0.95)
            avg_delta = (delta + end_delta) / 2
        else:
            avg_delta = delta

        # Theta decay (intraday: partial day's theta)
        theta_cost = entry_premium * theta_pct * qty

        if trade_type == TradeType.BUY_CE:
            pnl = spot_move * avg_delta * qty - theta_cost
        elif trade_type == TradeType.BUY_PE:
            pnl = -spot_move * avg_delta * qty - theta_cost
        elif trade_type == TradeType.SELL_CE:
            pnl = -spot_move * avg_delta * qty + theta_cost
        elif trade_type == TradeType.SELL_PE:
            pnl = spot_move * avg_delta * qty + theta_cost
        else:
            pnl = 0.0

        # Cap profit for BUY trades: can't make more than premium allows
        # Real premium move is ~70% of delta-estimated on average (IV crush, bid-ask)
        if trade_type in (TradeType.BUY_CE, TradeType.BUY_PE) and pnl > 0:
            pnl *= 0.75  # Realistic fill adjustment

        # Cap profit for SELL trades: max profit = full premium collected
        if trade_type in (TradeType.SELL_CE, TradeType.SELL_PE) and pnl > 0:
            max_sell_profit = entry_premium * qty
            pnl = min(pnl, max_sell_profit)

        # Slippage: higher on large moves (market orders in fast markets)
        slippage_mult = 1.0 + min(abs_move / spot_entry * 10, 1.0)  # Up to 2x slippage
        total_slippage = self.slippage * qty * slippage_mult

        pnl -= (self.brokerage * 2 + total_slippage)
        return pnl

    def compute_premium_pnl(self, spot_entry: float, spot_exit: float,
                            trade_type: TradeType, symbol: str = 'NIFTY50',
                            lots: int = 1, iv: float = 0.0,
                            atr: float = 0.0, trade_date: pd.Timestamp = None,
                            hold_days: int = 0) -> tuple:
        """
        Compute option PnL using Black-Scholes premium pricing.

        v5: Replaces delta-only approximation with full premium computation.
        - Derives ATM strike from spot
        - Computes entry premium at (spot_entry, strike, dte, iv)
        - Computes exit premium at (spot_exit, strike, dte - hold_time, iv_exit)
        - PnL = (exit_premium - entry_premium) × qty for BUY
        - PnL = (entry_premium - exit_premium) × qty for SELL
        - Naturally captures gamma, theta decay, and premium floor at 0

        Args:
            spot_entry: Spot price at entry
            spot_exit: Spot price at exit
            trade_type: BUY_CE, BUY_PE, SELL_CE, SELL_PE
            symbol: Index name for strike interval
            lots: Number of lots
            iv: Implied volatility (decimal). If 0, estimated from ATR.
            atr: ATR value for IV estimation (used only if iv=0)
            trade_date: Date of trade (for DTE computation). If None, uses dte=2.
            hold_days: Number of days trade is held (0 = intraday)

        Returns:
            tuple: (pnl, entry_premium, exit_premium, strike)
        """
        qty = lots * self.lot_size

        # 1. Derive ATM strike
        strike = get_strike(spot_entry, symbol)

        # 2. Estimate IV if not provided
        if iv <= 0:
            if atr > 0:
                iv = estimate_iv_from_atr(atr, spot_entry)
            else:
                iv = 0.15  # Default 15%

        # 3. Compute DTE
        if trade_date is not None:
            dte = compute_dte(trade_date, symbol)
        else:
            dte = 2  # Default: 2 days to expiry

        # Time to expiry in years
        T_entry = max(dte, 0.5) / 365  # Min 0.5 day (half-day for intraday)

        # For intraday trades, exit happens same day with partial theta decay
        if hold_days <= 0:
            # Intraday: ~6 hours of a trading day ≈ 0.25 calendar days
            T_exit = max(T_entry - 0.25 / 365, 0.01 / 365)
        else:
            T_exit = max((dte - hold_days) / 365, 0.01 / 365)

        # 4. Determine option type
        opt_type = 'CE' if trade_type in (TradeType.BUY_CE, TradeType.SELL_CE) else 'PE'

        # 5. IV adjustment at exit
        # - Winning BUY trades: IV often drops (IV crush) → reduce exit IV by 5-10%
        # - Losing trades / SL hits: IV spikes → increase exit IV
        spot_move = spot_exit - spot_entry
        move_favorable = (spot_move > 0 and opt_type == 'CE') or (spot_move < 0 and opt_type == 'PE')

        if move_favorable:
            iv_exit = iv * 0.95  # IV crush on favorable moves
        else:
            iv_exit = iv * 1.05  # IV spike on adverse moves

        # 6. Compute BS premiums
        entry_premium = bs_price(spot_entry, strike, T_entry, RISK_FREE_RATE, iv, opt_type)
        exit_premium = bs_price(spot_exit, strike, T_exit, RISK_FREE_RATE, iv_exit, opt_type)

        # 7. Compute PnL
        if trade_type in (TradeType.BUY_CE, TradeType.BUY_PE):
            # BUY: profit when premium increases
            pnl = (exit_premium - entry_premium) * qty
            # Bid-ask spread: BUY at ask (slightly above mid), SELL at bid (slightly below)
            # Net effect: ~3-5% of premium eaten by spread
            spread_cost = entry_premium * 0.04 * qty  # 4% of entry premium
            pnl -= spread_cost
        else:
            # SELL: profit when premium decreases (collect premium, buy back cheaper)
            pnl = (entry_premium - exit_premium) * qty
            # For SELL: cap max loss at 3x premium collected (realistic margin call level)
            max_loss = entry_premium * 3 * qty
            pnl = max(pnl, -max_loss)
            # Spread benefit for sellers (sell at ask, buy back at bid)
            spread_benefit = entry_premium * 0.02 * qty
            pnl += spread_benefit

        # 8. Costs: brokerage + slippage
        abs_move = abs(spot_exit - spot_entry)
        slippage_mult = 1.0 + min(abs_move / spot_entry * 10, 1.0)
        total_slippage = self.slippage * qty * slippage_mult
        pnl -= (self.brokerage * 2 + total_slippage)

        return pnl, entry_premium, exit_premium, strike

    def can_trade(self, trade_type: TradeType, premium: float = 0) -> bool:
        """Check if capital is sufficient for this trade.

        BUY trades: need premium × lot_size (option purchase cost)
        SELL trades: need margin (typically 3-5x premium × lot_size)
        """
        if self.capital <= 0:
            return False  # Blown up — stop trading

        min_buy_capital = max(premium * self.lot_size, 1000)  # At least Rs 1000
        min_sell_margin = max(premium * self.lot_size * 3, 5000)  # 3x premium for margin

        if trade_type in (TradeType.BUY_CE, TradeType.BUY_PE):
            return self.capital >= min_buy_capital
        else:
            return self.capital >= min_sell_margin

    def add_trade(self, trade: Trade):
        self.trades.append(trade)
        self.capital += trade.pnl
        # Capital floor: can't go below -10% of initial (margin call)
        if self.capital < -self.initial_capital * 0.1:
            self.capital = -self.initial_capital * 0.1

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
