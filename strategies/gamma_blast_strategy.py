"""
Gamma Blast Strategy - Expiry Day Option BUYING
=================================================
Based on Raahi Bhushan's Gamma Blast observations:

Rules:
1. ONLY trade on expiry days (Wed for BankNifty, Thu for Nifty)
2. Market should move < 1% from 9:15 AM to 1:45 PM (low range = coiled spring)
3. After 1:45 PM, if breakout occurs with volume spike:
   - Buy ATM/slightly OTM options (high gamma, delta 0.3-0.5)
   - CE if breakout is upward, PE if downward
4. OI velocity detection: >10% OI change in 10min triggers alert
5. Stop loss: 30% of premium or 50 points
6. Target: 2-3x premium (gamma explosion)
7. Exit by 3:15 PM mandatory

Simulated on daily OHLC by detecting coiled-spring patterns + late-day breakouts.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class GammaBlastStrategy:
    NAME = "Gamma Blast (Expiry)"

    def __init__(self,
                 max_morning_range_pct: float = 1.0,
                 coiled_spring_threshold: float = 0.6,
                 volume_spike_threshold: float = 1.5,
                 stop_loss_pct: float = 30.0,
                 target_multiplier: float = 2.5,
                 risk_per_trade_pct: float = 1.5,
                 expiry_days: list = None):
        """
        Args:
            max_morning_range_pct: Max % range from open to mid-day for setup
            coiled_spring_threshold: Below this % range = coiled spring
            volume_spike_threshold: Volume must be this x average for breakout
            stop_loss_pct: Stop loss as % of premium paid
            target_multiplier: Target as multiple of premium
            risk_per_trade_pct: % of capital to risk per trade
            expiry_days: Day of week for expiry [2=Wed, 3=Thu]
        """
        self.max_morning_range_pct = max_morning_range_pct
        self.coiled_spring_threshold = coiled_spring_threshold
        self.volume_spike_threshold = volume_spike_threshold
        self.stop_loss_pct = stop_loss_pct
        self.target_multiplier = target_multiplier
        self.risk_per_trade_pct = risk_per_trade_pct
        self.expiry_days = expiry_days or [2, 3]  # Wed + Thu

    def _is_coiled_spring(self, df: pd.DataFrame, i: int) -> bool:
        """
        Check if the day shows a coiled spring pattern.
        On daily bars: narrow range relative to ATR = low intraday volatility.
        Real implementation: check 9:15-1:45 range < 1%.
        """
        row = df.iloc[i]
        atr = df['ATR'].iloc[i]
        if pd.isna(atr) or atr == 0:
            return False

        # Narrow range day = coiled spring
        day_range = row['High'] - row['Low']
        range_pct = day_range / row['Close'] * 100

        # Check previous day was also relatively calm
        if i > 0:
            prev_range = (df.iloc[i-1]['High'] - df.iloc[i-1]['Low']) / df.iloc[i-1]['Close'] * 100
            if prev_range < self.max_morning_range_pct:
                return True

        # Current day low range up to now
        return range_pct < self.max_morning_range_pct

    def _detect_breakout_direction(self, df: pd.DataFrame, i: int) -> str:
        """
        Detect breakout direction on expiry day.
        Returns: 'UP', 'DOWN', or 'NONE'
        """
        row = df.iloc[i]
        atr = df['ATR'].iloc[i]
        if pd.isna(atr) or atr == 0:
            return 'NONE'

        open_price = row['Open']
        close = row['Close']
        high = row['High']
        low = row['Low']

        # Volume confirmation
        avg_vol = df['Volume'].iloc[max(0, i-20):i].mean()
        vol_ratio = row['Volume'] / avg_vol if avg_vol > 0 else 1

        has_volume = vol_ratio > self.volume_spike_threshold

        # Strong close near high = UP breakout
        body = close - open_price
        range_hl = high - low
        if range_hl == 0:
            return 'NONE'

        close_position = (close - low) / range_hl  # 0 = at low, 1 = at high

        if body > atr * 0.5 and close_position > 0.7 and has_volume:
            return 'UP'
        elif body < -atr * 0.5 and close_position < 0.3 and has_volume:
            return 'DOWN'

        # Strong move from mid-range (simulating 1:45 PM breakout)
        mid = (high + low) / 2
        if close > mid + atr * 0.5 and has_volume:
            return 'UP'
        elif close < mid - atr * 0.5 and has_volume:
            return 'DOWN'

        return 'NONE'

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "BANKNIFTY") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Calculate ATR
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR'] = df['TR'].rolling(14).mean()

        for i in range(20, len(df)):
            row = df.iloc[i]
            date = df.index[i]
            dow = date.weekday()
            atr = df['ATR'].iloc[i]

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            # === ONLY TRADE ON EXPIRY DAYS ===
            if dow not in self.expiry_days:
                engine.record_equity(date)
                continue

            # === CHECK COILED SPRING SETUP ===
            is_coiled = self._is_coiled_spring(df, i)
            if not is_coiled:
                engine.record_equity(date)
                continue

            # === DETECT BREAKOUT ===
            direction = self._detect_breakout_direction(df, i)
            if direction == 'NONE':
                engine.record_equity(date)
                continue

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']

            # === EXECUTE GAMMA BLAST TRADE ===
            if direction == 'UP':
                # Buy ATM CE - gamma explosion on up breakout
                # Entry at mid-point (simulating 1:45 PM entry)
                entry_spot = (open_price + close) / 2
                # On gamma blast, premium can go 2-3x
                # Simulate: option moves with high gamma (delta ~0.5 but accelerating)
                spot_move = close - entry_spot
                gamma_boost = 1.0
                if spot_move > 0:
                    # Gamma accelerates delta as option goes deeper ITM
                    gamma_boost = 1.0 + min(spot_move / atr, 1.5)  # Up to 2.5x delta

                pnl = spot_move * 0.5 * gamma_boost * engine.lot_size
                # Apply stop loss check
                worst_move = entry_spot - low
                worst_pnl = -worst_move * 0.5 * engine.lot_size
                max_loss = engine.capital * self.risk_per_trade_pct / 100
                if worst_pnl < -max_loss:
                    pnl = max(pnl, -max_loss)

                # Brokerage
                pnl -= (engine.brokerage * 2 + engine.slippage * engine.lot_size)

                trade = Trade(
                    entry_date=date, exit_date=date,
                    trade_type=TradeType.BUY_CE,
                    entry_price=entry_spot, exit_price=close,
                    quantity=engine.lot_size,
                    pnl=pnl, status="CLOSED"
                )
                engine.add_trade(trade)

            elif direction == 'DOWN':
                entry_spot = (open_price + close) / 2
                spot_move = entry_spot - close  # Positive if market fell
                gamma_boost = 1.0
                if spot_move > 0:
                    gamma_boost = 1.0 + min(spot_move / atr, 1.5)

                pnl = spot_move * 0.5 * gamma_boost * engine.lot_size
                worst_move = high - entry_spot
                worst_pnl = -worst_move * 0.5 * engine.lot_size
                max_loss = engine.capital * self.risk_per_trade_pct / 100
                if worst_pnl < -max_loss:
                    pnl = max(pnl, -max_loss)

                pnl -= (engine.brokerage * 2 + engine.slippage * engine.lot_size)

                trade = Trade(
                    entry_date=date, exit_date=date,
                    trade_type=TradeType.BUY_PE,
                    entry_price=entry_spot, exit_price=close,
                    quantity=engine.lot_size,
                    pnl=pnl, status="CLOSED"
                )
                engine.add_trade(trade)

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "expiry_day")
