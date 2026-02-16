"""
CA Nitin Muraka - PCR + VWAP Strategy
========================================
Type: Option BUYING strategy
Logic:
- Use PCR (Put-Call Ratio) for direction:
  PCR > 1 → Bullish → Buy CE
  PCR < 1 → Bearish → Buy PE
- Entry: Wait for price to touch/bounce from VWAP
- Stop Loss: Below VWAP for calls, above VWAP for puts
- Target: Double the premium (100% gain) or partial at 50%
- Timeframe: 15-min, trade after 10:30 AM
- Accuracy: ~70% claimed

Since we don't have real PCR data, we simulate it using:
- OI proxy: Volume ratio and price action patterns
- VWAP: Computed from daily OHLCV
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class PCRVWAPStrategy:
    NAME = "PCR+VWAP (CA Nitin Muraka)"

    def __init__(self, pcr_lookback: int = 5, vwap_tolerance_pct: float = 0.3,
                 target_multiplier: float = 2.0, sl_multiplier: float = 1.0,
                 max_trades_per_day: int = 2):
        """
        Args:
            pcr_lookback: Days to compute simulated PCR
            vwap_tolerance_pct: % distance from VWAP to trigger entry
            target_multiplier: Target as multiple of risk (2x = double premium)
            sl_multiplier: Stop loss as multiple of entry
            max_trades_per_day: Max trades per day
        """
        self.pcr_lookback = pcr_lookback
        self.vwap_tolerance_pct = vwap_tolerance_pct
        self.target_multiplier = target_multiplier
        self.sl_multiplier = sl_multiplier
        self.max_trades_per_day = max_trades_per_day

    def _compute_simulated_pcr(self, df: pd.DataFrame, i: int) -> float:
        """
        Simulate PCR using volume and price action.
        Higher volume on up days → more put writing → PCR > 1 → Bullish
        Higher volume on down days → more call writing → PCR < 1 → Bearish
        """
        if i < self.pcr_lookback:
            return 1.0

        lookback = df.iloc[i - self.pcr_lookback:i]
        up_days = lookback[lookback['Close'] > lookback['Open']]
        down_days = lookback[lookback['Close'] <= lookback['Open']]

        up_vol = up_days['Volume'].sum() if len(up_days) > 0 else 1
        down_vol = down_days['Volume'].sum() if len(down_days) > 0 else 1

        # Simulate PCR: more up volume = more put writing = bullish PCR
        pcr = up_vol / max(down_vol, 1)
        return pcr

    def _compute_daily_vwap(self, row: pd.Series) -> float:
        """Compute VWAP approximation from daily OHLCV."""
        typical_price = (row['High'] + row['Low'] + row['Close']) / 3
        return typical_price

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Compute VWAP (rolling typical price weighted by volume)
        df['TypicalPrice'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['CumVolPrice'] = (df['TypicalPrice'] * df['Volume']).rolling(5).sum()
        df['CumVol'] = df['Volume'].rolling(5).sum()
        df['VWAP'] = df['CumVolPrice'] / df['CumVol']

        # Moving averages for trend confirmation
        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['EMA50'] = df['Close'].ewm(span=50).mean()

        for i in range(max(50, self.pcr_lookback + 1), len(df)):
            row = df.iloc[i]
            date = df.index[i]
            vwap = df.iloc[i]['VWAP']

            if pd.isna(vwap):
                engine.record_equity(date)
                continue

            # Simulate PCR
            pcr = self._compute_simulated_pcr(df, i)

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']

            vwap_distance_pct = abs(close - vwap) / vwap * 100
            near_vwap = vwap_distance_pct < self.vwap_tolerance_pct

            # Entry conditions
            trade_taken = False

            if pcr > 1.0 and not trade_taken:
                # Bullish: Buy CE
                # Entry near VWAP on dip
                if low <= vwap * 1.003 and close > vwap:
                    # Price dipped to VWAP and bounced - buy CE
                    entry_spot = vwap
                    sl_spot = vwap * (1 - self.vwap_tolerance_pct / 100)
                    target_spot = entry_spot + (entry_spot - sl_spot) * self.target_multiplier

                    # Check if target was hit (high reached target?)
                    if high >= target_spot:
                        exit_spot = target_spot
                    elif close < sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close  # Exit at close

                    pnl = engine.simulate_option_pnl_from_spot(
                        entry_spot, exit_spot, TradeType.BUY_CE, lots=1
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_CE,
                        entry_price=entry_spot,
                        exit_price=exit_spot,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    trade_taken = True

            elif pcr < 1.0 and not trade_taken:
                # Bearish: Buy PE
                if high >= vwap * 0.997 and close < vwap:
                    entry_spot = vwap
                    sl_spot = vwap * (1 + self.vwap_tolerance_pct / 100)
                    target_spot = entry_spot - (sl_spot - entry_spot) * self.target_multiplier

                    if low <= target_spot:
                        exit_spot = target_spot
                    elif close > sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close

                    pnl = engine.simulate_option_pnl_from_spot(
                        entry_spot, exit_spot, TradeType.BUY_PE, lots=1
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.BUY_PE,
                        entry_price=entry_spot,
                        exit_price=exit_spot,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    trade_taken = True

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
