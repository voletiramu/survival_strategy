"""
MACD + ADX Momentum Strategy
==============================
Type: Option BUYING strategy
Logic:
- MACD (12, 26, 9) for direction signal:
  - MACD line crosses ABOVE signal line → Bullish → BUY CE
  - MACD line crosses BELOW signal line → Bearish → BUY PE
- ADX > 25 for trend strength confirmation (avoid choppy markets)
- RSI filter: Avoid overbought (>75) for CE, oversold (<25) for PE
- Entry: At close on crossover day
- Exit: On reverse crossover OR after max 3 days (theta protection)
- Stop loss: 1.5x ATR from entry

Suitable for swing trades on weekly expiry options.
Designed for low-capital accounts (option buying only, no margin needed).
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


class MACDMomentumStrategy:
    NAME = "MACD Momentum"

    def __init__(self,
                 fast_period: int = 12,
                 slow_period: int = 26,
                 signal_period: int = 9,
                 adx_period: int = 14,
                 adx_threshold: float = 25.0,
                 rsi_period: int = 14,
                 rsi_ob: float = 75.0,
                 rsi_os: float = 25.0,
                 max_hold_days: int = 3,
                 sl_atr_mult: float = 1.5,
                 delta: float = 0.45,
                 theta_pct: float = 0.04):
        """
        Args:
            fast_period: MACD fast EMA period
            slow_period: MACD slow EMA period
            signal_period: MACD signal line period
            adx_period: ADX calculation period
            adx_threshold: Minimum ADX to confirm trend
            rsi_period: RSI period
            rsi_ob: RSI overbought threshold (skip CE buys above this)
            rsi_os: RSI oversold threshold (skip PE buys below this)
            max_hold_days: Maximum hold period
            sl_atr_mult: Stop loss as ATR multiple
            delta: Option delta
            theta_pct: Daily theta as fraction of premium
        """
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.rsi_period = rsi_period
        self.rsi_ob = rsi_ob
        self.rsi_os = rsi_os
        self.max_hold_days = max_hold_days
        self.sl_atr_mult = sl_atr_mult
        self.delta = delta
        self.theta_pct = theta_pct

    def _compute_macd(self, close: pd.Series):
        """Compute MACD line, signal line, and histogram."""
        ema_fast = close.ewm(span=self.fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow_period, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def _compute_rsi(self, close: pd.Series, period: int = 14):
        """Compute RSI."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _compute_adx(self, df: pd.DataFrame, period: int = 14):
        """Compute ADX."""
        high = df['High']
        low = df['Low']
        close = df['Close']

        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        tr = np.maximum(
            high - low,
            np.maximum(abs(high - close.shift(1)), abs(low - close.shift(1)))
        )

        atr = tr.rolling(period).mean()
        plus_di = 100 * (plus_dm.rolling(period).mean() / (atr + 1e-10))
        minus_di = 100 * (minus_dm.rolling(period).mean() / (atr + 1e-10))
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = dx.rolling(period).mean()
        return adx, atr

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        macd_line, signal_line, histogram = self._compute_macd(df['Close'])
        rsi = self._compute_rsi(df['Close'], self.rsi_period)
        adx, atr = self._compute_adx(df, self.adx_period)

        start = max(self.slow_period + self.signal_period, self.adx_period * 2)

        open_trade = None
        entry_bar = 0

        for i in range(start, len(df)):
            row = df.iloc[i]
            date = df.index[i]
            cur_macd = macd_line.iloc[i]
            cur_signal = signal_line.iloc[i]
            prev_macd = macd_line.iloc[i-1]
            prev_signal = signal_line.iloc[i-1]
            cur_rsi = rsi.iloc[i]
            cur_adx = adx.iloc[i]
            cur_atr = atr.iloc[i]

            if pd.isna(cur_atr) or pd.isna(cur_adx) or pd.isna(cur_macd) or cur_atr == 0:
                engine.record_equity(date)
                continue

            close = row['Close']
            low = row['Low']
            high = row['High']

            # Check exit for open trade
            if open_trade is not None:
                hold_days = i - entry_bar
                sl_hit = False

                if open_trade['type'] == TradeType.BUY_CE:
                    sl_hit = low <= open_trade['sl']
                elif open_trade['type'] == TradeType.BUY_PE:
                    sl_hit = high >= open_trade['sl']

                reverse = (open_trade['type'] == TradeType.BUY_CE and cur_macd < cur_signal) or \
                          (open_trade['type'] == TradeType.BUY_PE and cur_macd > cur_signal)

                if sl_hit or reverse or hold_days >= self.max_hold_days:
                    if sl_hit:
                        exit_spot = open_trade['sl']
                    else:
                        exit_spot = close

                    iv = estimate_iv_from_atr(cur_atr, exit_spot)

                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        open_trade['entry_spot'], exit_spot, open_trade['type'],
                        symbol, lots=1, iv=iv, atr=cur_atr,
                        trade_date=open_trade['entry_date'],
                        hold_days=hold_days
                    )

                    trade = Trade(
                        entry_date=open_trade['entry_date'], exit_date=date,
                        trade_type=open_trade['type'],
                        entry_price=open_trade['entry_spot'], exit_price=exit_spot,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    open_trade = None

            # New entry on MACD crossover
            if open_trade is None:
                # Bullish crossover: MACD crosses above Signal
                bullish_cross = prev_macd <= prev_signal and cur_macd > cur_signal
                # Bearish crossover: MACD crosses below Signal
                bearish_cross = prev_macd >= prev_signal and cur_macd < cur_signal

                # ADX filter
                if pd.isna(cur_adx) or cur_adx < self.adx_threshold:
                    engine.record_equity(date)
                    continue

                if bullish_cross and cur_rsi < self.rsi_ob:
                    sl = close - cur_atr * self.sl_atr_mult
                    open_trade = {
                        'type': TradeType.BUY_CE,
                        'entry_spot': close,
                        'entry_date': date,
                        'sl': sl,
                    }
                    entry_bar = i

                elif bearish_cross and cur_rsi > self.rsi_os:
                    sl = close + cur_atr * self.sl_atr_mult
                    open_trade = {
                        'type': TradeType.BUY_PE,
                        'entry_spot': close,
                        'entry_date': date,
                        'sl': sl,
                    }
                    entry_bar = i

            engine.record_equity(date)

        # Close any remaining open trade
        if open_trade is not None and len(df) > 0:
            last = df.iloc[-1]
            hold_days = len(df) - 1 - entry_bar
            last_atr = atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 100
            iv = estimate_iv_from_atr(last_atr, last['Close'])

            pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                open_trade['entry_spot'], last['Close'], open_trade['type'],
                symbol, lots=1, iv=iv, atr=last_atr,
                trade_date=open_trade['entry_date'],
                hold_days=hold_days
            )
            trade = Trade(
                entry_date=open_trade['entry_date'], exit_date=df.index[-1],
                trade_type=open_trade['type'],
                entry_price=open_trade['entry_spot'], exit_price=last['Close'],
                option_entry_premium=entry_prem,
                option_exit_premium=exit_prem,
                strike=strike,
                quantity=engine.lot_size,
                pnl=pnl, status="CLOSED"
            )
            engine.add_trade(trade)

        return engine.compute_results(self.NAME, symbol, "swing")
