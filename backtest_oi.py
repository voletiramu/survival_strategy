#!/usr/bin/env python3
"""
Backtest engine for OI-based options BUYING strategies.

Runs OI Wall Bouncer, Max Pain Magnet, and VWAP Bounce strategies
on 5 years of NIFTY/BANKNIFTY/SENSEX daily data.

Since no historical per-strike OI data exists, uses approximation
methods for OI-dependent logic (round-number walls, ATM max pain).
Live trading uses real option chain data.

Usage:
    python backtest_oi.py                              # All strategies, all symbols
    python backtest_oi.py --symbol NIFTY               # Single symbol
    python backtest_oi.py --strategy "OI Wall Bouncer"  # Single strategy
"""

import os
import sys
import argparse
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional

# Reuse from existing backtest engine
from backtest_engine import (
    BacktestResult, Trade, TradeType, bs_price, get_strike,
    estimate_iv_from_atr, compute_dte, RISK_FREE_RATE, STRIKE_INTERVALS,
)

# OI strategy imports
from strategies.oi_strategies import (
    OI_WALL_BOUNCER, MAX_PAIN_MAGNET, VWAP_BOUNCE, ALL_OI_STRATEGIES,
    check_all_oi_signals, check_oi_wall_signal, check_max_pain_signal,
    check_vwap_bounce_signal,
    OIW_TARGET_PCT, OIW_SL_PCT,
    MP_TARGET_PCT, MP_SL_PCT, MP_EXPIRY_BOOST,
    VB_TARGET_PCT, VB_SL_PCT,
)

# Indicator computation
from strategies.indicators import compute_all_indicators

# Data directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
RESULTS_DIR = os.path.join(BASE_DIR, 'results')

# Lot sizes
LOT_SIZES = {'NIFTY': 75, 'NIFTY50': 75, 'BANKNIFTY': 30, 'SENSEX': 20}

# Symbol mapping (file names use NIFTY50, strategy uses NIFTY)
SYMBOL_MAP = {'NIFTY': 'NIFTY50', 'BANKNIFTY': 'BANKNIFTY', 'SENSEX': 'SENSEX'}
SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']


def load_ohlc_data(symbol):
    """Load daily OHLCV data from Angel cache CSV."""
    file_symbol = SYMBOL_MAP.get(symbol, symbol)
    path = os.path.join(DATA_DIR, f'{file_symbol}_1d_5y.csv')
    if not os.path.exists(path):
        print(f"  [WARN] Data file not found: {path}")
        return None

    df = pd.read_csv(path, parse_dates=['Date'], index_col='Date')
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    df = df.sort_index()

    # Ensure required columns
    for col in ['Open', 'High', 'Low', 'Close']:
        if col not in df.columns:
            print(f"  [WARN] Missing column {col} in {path}")
            return None

    if 'Volume' not in df.columns:
        df['Volume'] = 0

    print(f"  Loaded {len(df)} bars for {symbol} ({df.index[0].date()} to {df.index[-1].date()})")
    return df


class OIBacktestEngine:
    """Backtest engine for OI-based options BUYING strategies."""

    def __init__(self, symbol, initial_capital=150000, brokerage=20, slippage=2):
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.brokerage = brokerage
        self.slippage = slippage
        self.lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(SYMBOL_MAP.get(symbol, ''), 75))
        self.strike_interval = STRIKE_INTERVALS.get(SYMBOL_MAP.get(symbol, symbol), 50)
        self.trades = []
        self.open_positions = {}  # strategy_type -> position dict
        self.equity_history = []
        self.date_history = []
        self.daily_pnl = {}

    def reset(self):
        self.capital = self.initial_capital
        self.trades = []
        self.open_positions = {}
        self.equity_history = []
        self.date_history = []
        self.daily_pnl = {}

    def run_backtest(self, ohlc_df, strategies=None):
        """Run backtest on daily OHLC data.

        Args:
            ohlc_df: DataFrame with OHLCV columns and DatetimeIndex.
            strategies: List of strategy types to test. None = all.

        Returns:
            dict of {strategy_type: BacktestResult}
        """
        if strategies is None:
            strategies = ALL_OI_STRATEGIES

        self.reset()

        # Need at least 30 bars for indicator computation
        if len(ohlc_df) < 30:
            print(f"  [WARN] Insufficient data ({len(ohlc_df)} bars), need >= 30")
            return {}

        trade_counts = {s: 0 for s in strategies}

        for i in range(30, len(ohlc_df)):
            date = ohlc_df.index[i]
            row = ohlc_df.iloc[i]
            spot = row['Close']
            hist_df = ohlc_df.iloc[:i+1]

            # Skip weekends
            if date.weekday() >= 5:
                continue

            # Compute indicators
            indicators = compute_all_indicators(hist_df)
            if not indicators:
                continue

            iv = indicators.get('iv', 0.15)
            atr = indicators.get('atr', spot * 0.01)
            dte = compute_dte(date, SYMBOL_MAP.get(self.symbol, self.symbol))

            # Estimate VIX from ATR (same as existing backtest)
            vix_est = max(8, min(30, (atr / spot) * np.sqrt(252) * 100 * 1.1))

            # 1. Check exits for open positions
            self._check_exits(date, spot, iv, dte, indicators)

            # 2. Generate new signals
            signals = check_all_oi_signals(
                symbol=self.symbol, spot=spot, chain_data=None,
                indicators=indicators, iv=iv, dte=dte,
                lot_size=self.lot_size, vix=vix_est
            )

            # 3. Execute best signal per strategy (if no open position for that strategy)
            for signal in signals:
                strat = signal['strategy_type']
                if strat not in strategies:
                    continue
                if strat in self.open_positions:
                    continue  # Already have a position for this strategy

                # Capital check
                cost = signal['entry_premium'] * self.lot_size
                if cost > self.capital * 0.25:
                    continue  # Max 25% of capital per trade

                # Enter position
                self._enter_position(date, signal)
                trade_counts[strat] += 1

            # Record equity
            self.equity_history.append(self.capital)
            self.date_history.append(date)

        # Force close any remaining positions at last bar
        last_date = ohlc_df.index[-1]
        last_spot = ohlc_df.iloc[-1]['Close']
        last_iv = indicators.get('iv', 0.15) if indicators else 0.15
        for strat in list(self.open_positions.keys()):
            self._exit_position(strat, last_date, last_spot, last_iv, 0, 'BACKTEST_END')

        # Compute results per strategy
        results = {}
        for strat in strategies:
            strat_trades = [t for t in self.trades if t.status == strat]
            if not strat_trades:
                results[strat] = BacktestResult(
                    strategy_name=strat, symbol=self.symbol, timeframe='daily'
                )
                continue

            # Build per-strategy equity curve
            engine = _MockEngine(self.initial_capital, strat_trades,
                                 self.date_history, self.lot_size)
            results[strat] = engine.compute_results(strat, self.symbol)

        return results

    def _enter_position(self, date, signal):
        """Enter a new position."""
        self.open_positions[signal['strategy_type']] = {
            'entry_date': date,
            'entry_spot': signal['spot'],
            'entry_premium': signal['entry_premium'],
            'strike': signal['strike'],
            'opt_type': signal['opt_type'],
            'signal_type': signal['signal_type'],
            'strategy_type': signal['strategy_type'],
            'target': signal['target'],
            'sl': signal['sl'],
            'iv': signal['iv'],
            'dte': signal['dte'],
            'quality_score': signal.get('quality_score', 0),
        }

    def _check_exits(self, date, spot, iv, dte, indicators):
        """Check exit conditions for all open positions."""
        for strat in list(self.open_positions.keys()):
            pos = self.open_positions[strat]
            entry_premium = pos['entry_premium']
            entry_date = pos['entry_date']

            # Compute current premium via BS
            hold_days = (date - entry_date).days
            T_exit = max((pos['dte'] - hold_days), 0.5) / 365

            # IV adjustment: favorable move -> IV crush, adverse -> IV spike
            spot_move = spot - pos['entry_spot']
            if pos['opt_type'] == 'CE':
                move_favorable = spot_move > 0
            else:
                move_favorable = spot_move < 0

            iv_exit = iv * (0.95 if move_favorable else 1.05)

            current_premium = bs_price(
                spot, pos['strike'], T_exit, RISK_FREE_RATE, iv_exit, pos['opt_type']
            )

            # Apply bid-ask spread (4% cost for BUY closing)
            current_premium *= 0.96

            # Check target
            if current_premium >= pos['target']:
                self._exit_position(strat, date, spot, iv, current_premium, 'TARGET_HIT')
                continue

            # Check stop loss
            if current_premium <= pos['sl']:
                self._exit_position(strat, date, spot, iv, current_premium, 'SL_HIT')
                continue

            # Time exit: position held > 5 days
            if hold_days >= 5:
                self._exit_position(strat, date, spot, iv, current_premium, 'TIME_EXIT')
                continue

            # DTE = 0: force close
            remaining_dte = pos['dte'] - hold_days
            if remaining_dte <= 0:
                self._exit_position(strat, date, spot, iv, current_premium, 'EXPIRY_CLOSE')
                continue

    def _exit_position(self, strategy_type, date, spot, iv, exit_premium, reason):
        """Close a position and record the trade."""
        pos = self.open_positions.pop(strategy_type, None)
        if not pos:
            return

        if exit_premium <= 0:
            # Compute exit premium if not provided
            hold_days = (date - pos['entry_date']).days
            T_exit = max((pos['dte'] - hold_days), 0.5) / 365
            exit_premium = bs_price(spot, pos['strike'], T_exit, RISK_FREE_RATE, iv, pos['opt_type'])
            exit_premium *= 0.96  # Bid-ask spread

        # PnL calculation (BUY only)
        pnl = (exit_premium - pos['entry_premium']) * self.lot_size

        # Costs
        total_cost = self.brokerage * 2 + self.slippage * self.lot_size
        pnl -= total_cost

        # Update capital
        self.capital += pnl

        # Record trade (use status field to store strategy type for filtering)
        trade = Trade(
            entry_date=pos['entry_date'],
            exit_date=date,
            trade_type=TradeType.BUY_CE if pos['opt_type'] == 'CE' else TradeType.BUY_PE,
            entry_price=pos['entry_spot'],
            exit_price=spot,
            option_entry_premium=pos['entry_premium'],
            option_exit_premium=round(exit_premium, 2),
            strike=pos['strike'],
            quantity=self.lot_size,
            pnl=round(pnl, 2),
            status=strategy_type,  # Store strategy type for filtering
        )
        self.trades.append(trade)


class _MockEngine:
    """Helper to compute BacktestResult from a subset of trades."""

    def __init__(self, initial_capital, trades, all_dates, lot_size):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trades = trades
        self.lot_size = lot_size

        # Build equity curve from trades
        self.equity_history = []
        self.date_history = []

        trade_pnl_by_date = {}
        for t in trades:
            d = t.exit_date
            if d:
                trade_pnl_by_date[d] = trade_pnl_by_date.get(d, 0) + t.pnl

        running = initial_capital
        for d in all_dates:
            running += trade_pnl_by_date.get(d, 0)
            self.equity_history.append(running)
            self.date_history.append(d)

        self.capital = running

    def compute_results(self, strategy_name, symbol):
        """Compute BacktestResult."""
        result = BacktestResult(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe='daily',
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

            daily_ret = equity.pct_change().dropna()
            result.daily_returns = daily_ret

            peak = equity.expanding().max()
            drawdown = equity - peak
            result.max_drawdown = drawdown.min()
            result.max_drawdown_pct = (drawdown / peak).min() * 100

            result.total_return_pct = (self.capital - self.initial_capital) / self.initial_capital * 100
            n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
            if self.capital > 0 and self.initial_capital > 0:
                result.annualized_return_pct = ((self.capital / self.initial_capital) ** (1 / n_years) - 1) * 100

            if len(daily_ret) > 1 and daily_ret.std() > 0:
                rf_daily = 0.06 / 252
                excess = daily_ret - rf_daily
                result.sharpe_ratio = np.sqrt(252) * excess.mean() / excess.std()

            if len(daily_ret) > 1:
                downside = daily_ret[daily_ret < 0]
                if len(downside) > 0 and downside.std() > 0:
                    result.sortino_ratio = np.sqrt(252) * (daily_ret.mean() - 0.06/252) / downside.std()

            if result.max_drawdown_pct != 0:
                result.calmar_ratio = result.annualized_return_pct / abs(result.max_drawdown_pct)

        return result


def print_results(results, symbol):
    """Pretty-print backtest results."""
    print(f"\n{'='*70}")
    print(f"  OI STRATEGY BACKTEST RESULTS - {symbol}")
    print(f"{'='*70}")

    for strat, res in results.items():
        if res.total_trades == 0:
            print(f"\n  {strat}: No trades generated")
            continue

        pnl_sign = '+' if res.total_pnl >= 0 else ''
        print(f"\n  --- {strat} ---")
        print(f"  Trades: {res.total_trades} (W:{res.winning_trades} L:{res.losing_trades})")
        print(f"  Win Rate: {res.win_rate:.1f}%")
        print(f"  Total PnL: Rs {pnl_sign}{res.total_pnl:,.2f}")
        print(f"  Profit Factor: {res.profit_factor:.2f}")
        print(f"  Sharpe Ratio: {res.sharpe_ratio:.2f}")
        print(f"  Sortino Ratio: {res.sortino_ratio:.2f}")
        print(f"  Max Drawdown: {res.max_drawdown_pct:.2f}%")
        print(f"  Return: {res.total_return_pct:.1f}%  (Ann: {res.annualized_return_pct:.1f}%)")
        print(f"  Avg Win: Rs {res.avg_win:,.2f}  |  Avg Loss: Rs {res.avg_loss:,.2f}")
        print(f"  Largest Win: Rs {res.largest_win:,.2f}  |  Largest Loss: Rs {res.largest_loss:,.2f}")

    print(f"\n{'='*70}\n")


def save_trades_csv(trades, symbol, strategy_name):
    """Save trade results to CSV."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe_name = strategy_name.replace(' ', '_')
    path = os.path.join(RESULTS_DIR, f'trades_oi_{safe_name}_{symbol}.csv')

    rows = []
    for t in trades:
        rows.append({
            'entry_date': t.entry_date,
            'exit_date': t.exit_date,
            'trade_type': t.trade_type.value,
            'entry_spot': t.entry_price,
            'exit_spot': t.exit_price,
            'strike': t.strike,
            'entry_premium': t.option_entry_premium,
            'exit_premium': t.option_exit_premium,
            'quantity': t.quantity,
            'pnl': t.pnl,
            'strategy': t.status,
        })

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"  Saved {len(rows)} trades to {path}")


def main():
    parser = argparse.ArgumentParser(description='Backtest OI-based strategies')
    parser.add_argument('--symbol', type=str, default=None,
                        help='Single symbol to test (NIFTY, BANKNIFTY, SENSEX)')
    parser.add_argument('--strategy', type=str, default=None,
                        help='Single strategy to test (e.g., "OI Wall Bouncer")')
    parser.add_argument('--capital', type=float, default=150000,
                        help='Initial capital (default: 150000)')
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else SYMBOLS
    strategies = [args.strategy] if args.strategy else None

    print("\n" + "="*70)
    print("  OI-BASED OPTIONS BUYING STRATEGIES BACKTEST")
    print("  Strategies: " + ", ".join(strategies or ALL_OI_STRATEGIES))
    print("  Symbols: " + ", ".join(symbols))
    print("  Capital: Rs {:,.0f}".format(args.capital))
    print("="*70)

    all_results = {}

    for symbol in symbols:
        print(f"\n--- Loading {symbol} ---")
        ohlc = load_ohlc_data(symbol)
        if ohlc is None:
            continue

        engine = OIBacktestEngine(symbol, initial_capital=args.capital)
        results = engine.run_backtest(ohlc, strategies=strategies)

        all_results[symbol] = results
        print_results(results, symbol)

        # Save trade CSVs
        for strat, res in results.items():
            if res.trades:
                save_trades_csv(res.trades, symbol, strat)

    # Summary across all symbols
    if len(all_results) > 1:
        print("\n" + "="*70)
        print("  COMBINED SUMMARY (ALL SYMBOLS)")
        print("="*70)

        for strat in (strategies or ALL_OI_STRATEGIES):
            total_trades = 0
            total_wins = 0
            total_pnl = 0
            all_pnls = []

            for symbol, results in all_results.items():
                if strat in results:
                    res = results[strat]
                    total_trades += res.total_trades
                    total_wins += res.winning_trades
                    total_pnl += res.total_pnl
                    all_pnls.extend([t.pnl for t in res.trades])

            if total_trades == 0:
                print(f"\n  {strat}: No trades across any symbol")
                continue

            wr = total_wins / total_trades * 100 if total_trades > 0 else 0
            wins = [p for p in all_pnls if p > 0]
            losses = [p for p in all_pnls if p <= 0]
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float('inf')
            pnl_sign = '+' if total_pnl >= 0 else ''

            print(f"\n  --- {strat} (Combined) ---")
            print(f"  Total Trades: {total_trades}  |  Win Rate: {wr:.1f}%")
            print(f"  Total PnL: Rs {pnl_sign}{total_pnl:,.2f}")
            print(f"  Profit Factor: {pf:.2f}")
            if wins:
                print(f"  Avg Win: Rs {np.mean(wins):,.2f}  |  Avg Loss: Rs {np.mean(losses):,.2f}")

        print(f"\n{'='*70}\n")


if __name__ == '__main__':
    main()
