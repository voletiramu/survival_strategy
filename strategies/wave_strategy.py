"""
Raahi Bhushan - Wave Extractor Strategy
==========================================
Type: Option SELLING strategy (range/mean-reversion)
Logic:
- Places bracket orders: sell CE when price goes up X points, sell PE when down X points
- Captures oscillations in range-bound markets
- Uses multiplier scaling: widens gap when net position builds in one direction
- Cool-off period between trades
- Thrives in sideways markets, struggles in trending

Backtested on daily OHLC by simulating intraday range oscillations.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


class WaveStrategy:
    NAME = "Wave Extractor (Raahi Bhushan)"

    def __init__(self, base_gap: int = 25, multiplier_up: float = 1.5,
                 multiplier_down: float = 1.5, max_trades_per_day: int = 6,
                 trend_filter_period: int = 20):
        """
        Args:
            base_gap: Base points movement to trigger trade
            multiplier_up: Gap multiplier when net short (trending up)
            multiplier_down: Gap multiplier when net long (trending down)
            max_trades_per_day: Maximum trades allowed per day
            trend_filter_period: Period for trend detection (avoid trending days)
        """
        self.base_gap = base_gap
        self.multiplier_up = multiplier_up
        self.multiplier_down = multiplier_down
        self.max_trades_per_day = max_trades_per_day
        self.trend_filter_period = trend_filter_period

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Calculate ATR for volatility-based gap adjustment
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR'] = df['TR'].rolling(14).mean()

        # Trend filter: if ADX-like measure is high, reduce trading
        df['range_pct'] = (df['High'] - df['Low']) / df['Close'] * 100
        df['avg_range'] = df['range_pct'].rolling(self.trend_filter_period).mean()
        df['is_ranging'] = df['range_pct'] < df['avg_range'] * 1.5

        net_position = 0  # positive = net short CE, negative = net short PE

        for i in range(max(self.trend_filter_period, 14) + 1, len(df)):
            row = df.iloc[i]
            date = df.index[i]

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']
            atr = df.iloc[i]['ATR']

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            # Adaptive gap based on ATR
            adaptive_gap = max(self.base_gap, atr * 0.3)

            # Calculate effective gap with multiplier
            if net_position > 0:  # Net short CE (market went up)
                up_gap = adaptive_gap * self.multiplier_up
                down_gap = adaptive_gap
            elif net_position < 0:  # Net short PE (market went down)
                up_gap = adaptive_gap
                down_gap = adaptive_gap * self.multiplier_down
            else:
                up_gap = adaptive_gap
                down_gap = adaptive_gap

            daily_range = high - low
            trades_today = 0

            iv = estimate_iv_from_atr(atr, open_price)

            # Simulate oscillations within the day
            if daily_range > 0 and df.iloc[i]['is_ranging']:
                up_moves = max(0, (high - open_price) / up_gap)
                down_moves = max(0, (open_price - low) / down_gap)

                ce_trades = min(int(up_moves), self.max_trades_per_day // 2)
                for _ in range(ce_trades):
                    if trades_today >= self.max_trades_per_day:
                        break
                    entry_spot = open_price + up_gap
                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, close, TradeType.SELL_CE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.SELL_CE,
                        entry_price=entry_spot, exit_price=close,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    net_position += 1
                    trades_today += 1

                pe_trades = min(int(down_moves), self.max_trades_per_day // 2)
                for _ in range(pe_trades):
                    if trades_today >= self.max_trades_per_day:
                        break
                    entry_spot = open_price - down_gap
                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, close, TradeType.SELL_PE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.SELL_PE,
                        entry_price=entry_spot, exit_price=close,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    net_position -= 1
                    trades_today += 1

            else:
                # Trending day - Wave strategy struggles
                if daily_range > atr * 1.5:
                    tt = TradeType.SELL_CE if close > open_price else TradeType.SELL_PE
                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        open_price, close, tt, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )
                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=tt,
                        entry_price=open_price, exit_price=close,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)

            # Reset net position daily (intraday strategy)
            net_position = 0
            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
