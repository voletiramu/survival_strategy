"""
ORB (Opening Range Breakout) Strategy
=======================================
Type: Option BUYING strategy
Logic:
- Signal generated on day i based on breakout pattern
- Trade executed on day i+1 (next day) to avoid look-ahead bias
- If day i had bullish breakout (close > opening range high) → BUY CE next day
- If day i had bearish breakout (close < opening range low) → BUY PE next day
- Entry: Next day's Open
- Stop loss: ATR-based (1x ATR from entry)
- Target: 1.5x ATR from entry
- Time exit: Close at end of next day if neither target nor SL hit

Confirmation filters:
- Body must be > 50% of day's range (strong directional day)
- Volume > 80% of 20-day average
- Gap between close and breakout level must be meaningful (> 0.1% of spot)
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


class ORBStrategy:
    NAME = "ORB Breakout"

    def __init__(self,
                 range_atr_mult: float = 0.15,
                 target_atr_mult: float = 1.5,
                 sl_atr_mult: float = 1.0,
                 min_body_pct: float = 0.50,
                 min_breakout_pct: float = 0.10,
                 volume_threshold: float = 0.8,
                 delta: float = 0.45,
                 theta_pct: float = 0.03):
        self.range_atr_mult = range_atr_mult
        self.target_atr_mult = target_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.min_body_pct = min_body_pct
        self.min_breakout_pct = min_breakout_pct
        self.volume_threshold = volume_threshold
        self.delta = delta
        self.theta_pct = theta_pct

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
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
        df['AvgVol'] = df['Volume'].rolling(20).mean()

        pending_signal = None  # Signal from day i, trade on day i+1

        for i in range(21, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            date = df.index[i]
            atr = df['ATR'].iloc[i]
            prev_atr = df['ATR'].iloc[i - 1]
            avg_vol = df['AvgVol'].iloc[i]

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                pending_signal = None
                continue

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']

            # === EXECUTE PENDING SIGNAL FROM PREVIOUS DAY ===
            if pending_signal is not None:
                entry_spot = open_price  # Enter at today's open
                target_dist = atr * self.target_atr_mult
                sl_dist = atr * self.sl_atr_mult
                iv = estimate_iv_from_atr(atr, entry_spot)

                if pending_signal == 'BUY_CE':
                    target_spot = entry_spot + target_dist
                    sl_spot = entry_spot - sl_dist

                    if low <= sl_spot:
                        exit_spot = sl_spot
                    elif high >= target_spot:
                        exit_spot = target_spot
                    else:
                        exit_spot = close

                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, exit_spot, TradeType.BUY_CE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )
                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_CE,
                        entry_price=entry_spot, exit_price=exit_spot,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)

                elif pending_signal == 'BUY_PE':
                    target_spot = entry_spot - target_dist
                    sl_spot = entry_spot + sl_dist

                    if high >= sl_spot:
                        exit_spot = sl_spot
                    elif low <= target_spot:
                        exit_spot = target_spot
                    else:
                        exit_spot = close

                    pnl, entry_prem, exit_prem, strike = engine.compute_premium_pnl(
                        entry_spot, exit_spot, TradeType.BUY_PE, symbol,
                        lots=1, iv=iv, atr=atr, trade_date=date
                    )
                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_PE,
                        entry_price=entry_spot, exit_price=exit_spot,
                        option_entry_premium=entry_prem,
                        option_exit_premium=exit_prem,
                        strike=strike,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)

                pending_signal = None

            # === GENERATE SIGNAL FOR NEXT DAY ===
            if pd.isna(avg_vol) or avg_vol == 0:
                engine.record_equity(date)
                continue

            volume = row['Volume']
            prev_close = prev['Close']

            # Volume filter
            if volume < avg_vol * self.volume_threshold:
                engine.record_equity(date)
                continue

            # Opening range from today
            gap_buffer = atr * self.range_atr_mult
            range_high = max(open_price, prev_close) + gap_buffer
            range_low = min(open_price, prev_close) - gap_buffer

            # Body confirmation
            body = abs(close - open_price)
            hl_range = high - low
            if hl_range == 0 or body / hl_range < self.min_body_pct:
                engine.record_equity(date)
                continue

            # Breakout strength: close must be meaningfully beyond range
            if close > range_high:
                breakout_pct = (close - range_high) / close * 100
                if breakout_pct >= self.min_breakout_pct:
                    pending_signal = 'BUY_CE'
            elif close < range_low:
                breakout_pct = (range_low - close) / close * 100
                if breakout_pct >= self.min_breakout_pct:
                    pending_signal = 'BUY_PE'

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
