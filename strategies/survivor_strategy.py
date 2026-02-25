"""
Raahi Bhushan - Survivor Strategy
===================================
Type: Option SELLING strategy (trend-following after breakout)
Logic:
- Identify support/resistance levels using previous day high/low
- When price breaks above resistance → SELL PE (puts) at distance from spot
- When price breaks below support → SELL CE (calls) at distance from spot
- For every N points further in trend, sell another lot
- Reset reference when price retraces by reset_gap points
- Run Mon-Wed only (weekly expiry consideration)

Backtested on daily OHLC by simulating intraday breakouts from prev day levels.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class SurvivorStrategy:
    NAME = "Survivor (Raahi Bhushan)"

    def __init__(self, pe_gap: int = 30, ce_gap: int = 30,
                 pe_symbol_gap: int = 300, ce_symbol_gap: int = 300,
                 reset_gap: int = 90, max_positions: int = 5,
                 min_premium: float = 20.0):
        """
        Args:
            pe_gap: Points move up to trigger PE sell
            ce_gap: Points move down to trigger CE sell
            pe_symbol_gap: Strike distance for PE from spot
            ce_symbol_gap: Strike distance for CE from spot
            reset_gap: Points retracement to reset reference
            max_positions: Max concurrent positions per side
            min_premium: Minimum premium to sell
        """
        self.pe_gap = pe_gap
        self.ce_gap = ce_gap
        self.pe_symbol_gap = pe_symbol_gap
        self.ce_symbol_gap = ce_symbol_gap
        self.reset_gap = reset_gap
        self.max_positions = max_positions
        self.min_premium = min_premium

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()
        df['prev_high'] = df['High'].shift(1)
        df['prev_low'] = df['Low'].shift(1)
        df['prev_close'] = df['Close'].shift(1)

        pe_ref = None  # PE reference value
        ce_ref = None  # CE reference value
        open_pe_positions = 0
        open_ce_positions = 0
        pe_trades_today = []
        ce_trades_today = []

        for i in range(2, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            date = df.index[i]

            # Only trade Mon-Wed (0=Mon, 2=Wed)
            if date.weekday() > 2:
                engine.record_equity(date)
                continue

            resistance = prev['High']
            support = prev['Low']
            current_high = row['High']
            current_low = row['Low']
            current_close = row['Close']

            # Initialize references
            if pe_ref is None:
                pe_ref = resistance
            if ce_ref is None:
                ce_ref = support

            # --- PE SELLING (market going UP, sell puts) ---
            if current_high > pe_ref + self.pe_gap:
                moves = int((current_high - pe_ref) / self.pe_gap)
                trades_to_take = min(moves, self.max_positions - open_pe_positions)

                for _ in range(trades_to_take):
                    pe_ref += self.pe_gap
                    # Simulate PE sell: premium decays as market goes up
                    # Entry when market breaks up, exit at close
                    spot_at_entry = pe_ref
                    spot_at_exit = current_close

                    pnl = engine.simulate_option_pnl_from_spot(
                        spot_at_entry, spot_at_exit,
                        TradeType.SELL_PE, lots=1
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.SELL_PE,
                        entry_price=spot_at_entry,
                        exit_price=spot_at_exit,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    open_pe_positions += 1

                # Reset check: if price pulls back
                if pe_ref - current_low > self.reset_gap:
                    pe_ref = current_low + self.reset_gap
                    open_pe_positions = 0

            # --- CE SELLING (market going DOWN, sell calls) ---
            if current_low < ce_ref - self.ce_gap:
                moves = int((ce_ref - current_low) / self.ce_gap)
                trades_to_take = min(moves, self.max_positions - open_ce_positions)

                for _ in range(trades_to_take):
                    ce_ref -= self.ce_gap
                    spot_at_entry = ce_ref
                    spot_at_exit = current_close

                    pnl = engine.simulate_option_pnl_from_spot(
                        spot_at_entry, spot_at_exit,
                        TradeType.SELL_CE, lots=1
                    )

                    trade = Trade(
                        entry_date=date, exit_date=date,
                        trade_type=TradeType.SELL_CE,
                        entry_price=spot_at_entry,
                        exit_price=spot_at_exit,
                        quantity=engine.lot_size,
                        pnl=pnl, status="CLOSED"
                    )
                    engine.add_trade(trade)
                    open_ce_positions += 1

                # Reset check
                if current_high - ce_ref > self.reset_gap:
                    ce_ref = current_high - self.reset_gap
                    open_ce_positions = 0

            # Daily reset of position count (intraday strategy)
            open_pe_positions = 0
            open_ce_positions = 0

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
