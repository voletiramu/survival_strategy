"""
Supertrend Strategy - ATR-Based Trend Following
=================================================
Type: Option BUYING strategy
Logic:
- Supertrend = ATR-based trailing stop that flips direction
- When Supertrend flips from DOWN to UP (bearish to bullish) → BUY CE
- When Supertrend flips from UP to DOWN (bullish to bearish) → BUY PE
- Hold until Supertrend flips direction (trend exit)
- Maximum hold period: 5 days (prevents theta bleed on long holds)

Supertrend calculation:
- Basic upper band = (High + Low)/2 + ATR * multiplier
- Basic lower band = (High + Low)/2 - ATR * multiplier
- Final bands adjust to trail the trend (only tighten, never widen)
- Trend = UP when close > final lower band, DOWN when close < final upper band

Confirmation filters:
- ADX > 20 (confirms trend exists, avoids choppy markets)
- Volume > 70% of 20-day average (avoids low-liquidity days)

Popular in Indian markets. ATR multiplier of 3 with period 10 is standard.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


class SupertrendStrategy:
    NAME = "Supertrend"

    def __init__(self,
                 atr_period: int = 10,
                 atr_multiplier: float = 3.0,
                 adx_period: int = 14,
                 adx_threshold: float = 20.0,
                 max_hold_days: int = 5,
                 volume_threshold: float = 0.7,
                 delta: float = 0.45,
                 theta_pct: float = 0.03):
        """
        Args:
            atr_period: ATR period for Supertrend
            atr_multiplier: ATR multiplier for band width
            adx_period: Period for ADX calculation
            adx_threshold: Minimum ADX for trend confirmation
            max_hold_days: Maximum holding period
            volume_threshold: Volume filter (fraction of 20-day avg)
            delta: Option delta for ATM
            theta_pct: Daily theta decay as fraction of premium
        """
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.max_hold_days = max_hold_days
        self.volume_threshold = volume_threshold
        self.delta = delta
        self.theta_pct = theta_pct

    def _compute_supertrend(self, df: pd.DataFrame):
        """Compute Supertrend indicator."""
        hl2 = (df['High'] + df['Low']) / 2

        # ATR
        tr = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        atr = tr.rolling(self.atr_period).mean()

        # Basic bands
        basic_upper = hl2 + self.atr_multiplier * atr
        basic_lower = hl2 - self.atr_multiplier * atr

        # Final bands (trail only)
        final_upper = pd.Series(np.nan, index=df.index)
        final_lower = pd.Series(np.nan, index=df.index)
        supertrend = pd.Series(np.nan, index=df.index)
        direction = pd.Series(0, index=df.index)  # 1 = UP, -1 = DOWN

        for i in range(1, len(df)):
            # Final upper band
            if pd.isna(basic_upper.iloc[i]):
                final_upper.iloc[i] = np.nan
            elif pd.isna(final_upper.iloc[i-1]):
                final_upper.iloc[i] = basic_upper.iloc[i]
            else:
                if basic_upper.iloc[i] < final_upper.iloc[i-1] or df['Close'].iloc[i-1] > final_upper.iloc[i-1]:
                    final_upper.iloc[i] = basic_upper.iloc[i]
                else:
                    final_upper.iloc[i] = final_upper.iloc[i-1]

            # Final lower band
            if pd.isna(basic_lower.iloc[i]):
                final_lower.iloc[i] = np.nan
            elif pd.isna(final_lower.iloc[i-1]):
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                if basic_lower.iloc[i] > final_lower.iloc[i-1] or df['Close'].iloc[i-1] < final_lower.iloc[i-1]:
                    final_lower.iloc[i] = basic_lower.iloc[i]
                else:
                    final_lower.iloc[i] = final_lower.iloc[i-1]

            # Direction
            if pd.isna(final_upper.iloc[i]) or pd.isna(final_lower.iloc[i]):
                direction.iloc[i] = direction.iloc[i-1]
                continue

            if direction.iloc[i-1] == 1:  # Was UP
                if df['Close'].iloc[i] < final_lower.iloc[i]:
                    direction.iloc[i] = -1  # Flip to DOWN
                    supertrend.iloc[i] = final_upper.iloc[i]
                else:
                    direction.iloc[i] = 1
                    supertrend.iloc[i] = final_lower.iloc[i]
            else:  # Was DOWN or 0
                if df['Close'].iloc[i] > final_upper.iloc[i]:
                    direction.iloc[i] = 1  # Flip to UP
                    supertrend.iloc[i] = final_lower.iloc[i]
                else:
                    direction.iloc[i] = -1
                    supertrend.iloc[i] = final_upper.iloc[i]

        return supertrend, direction, atr

    def _compute_adx(self, df: pd.DataFrame, period: int = 14):
        """Compute ADX (Average Directional Index)."""
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
        plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = dx.rolling(period).mean()

        return adx

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        supertrend, direction, atr = self._compute_supertrend(df)
        adx = self._compute_adx(df, self.adx_period)
        df['AvgVol'] = df['Volume'].rolling(20).mean()

        open_trade = None  # Track open position
        entry_bar = 0

        for i in range(max(30, self.atr_period + self.adx_period), len(df)):
            row = df.iloc[i]
            date = df.index[i]
            cur_atr = atr.iloc[i]
            cur_adx = adx.iloc[i]
            cur_dir = direction.iloc[i]
            prev_dir = direction.iloc[i-1]
            avg_vol = df['AvgVol'].iloc[i]

            if pd.isna(cur_atr) or pd.isna(cur_adx) or cur_atr == 0:
                engine.record_equity(date)
                continue

            close = row['Close']
            open_price = row['Open']

            # Check if we need to exit an open trade
            if open_trade is not None:
                hold_days = i - entry_bar
                flip = (open_trade['type'] == TradeType.BUY_CE and cur_dir == -1) or \
                       (open_trade['type'] == TradeType.BUY_PE and cur_dir == 1)

                if flip or hold_days >= self.max_hold_days:
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

            # New entry on direction flip (only if no open trade)
            if open_trade is None and prev_dir != cur_dir and cur_dir != 0:
                # Volume filter
                if pd.isna(avg_vol) or row['Volume'] < avg_vol * self.volume_threshold:
                    engine.record_equity(date)
                    continue

                # ADX filter
                if cur_adx < self.adx_threshold:
                    engine.record_equity(date)
                    continue

                if cur_dir == 1:  # Flipped to UP → Buy CE
                    open_trade = {
                        'type': TradeType.BUY_CE,
                        'entry_spot': close,
                        'entry_date': date,
                    }
                    entry_bar = i
                elif cur_dir == -1:  # Flipped to DOWN → Buy PE
                    open_trade = {
                        'type': TradeType.BUY_PE,
                        'entry_spot': close,
                        'entry_date': date,
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

        return engine.compute_results(self.NAME, symbol, "daily_trend")
