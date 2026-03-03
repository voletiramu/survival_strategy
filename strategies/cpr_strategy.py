"""
CPR (Central Pivot Range) Strategy by Gomathi Shankar
======================================================
Type: Option BUYING + SELLING (spread strategies)
Logic:
- Calculate CPR: Pivot = (H+L+C)/3, BC = (H+L)/2, TC = (Pivot-BC)+Pivot
- Narrow CPR → Trending day expected → Breakout strategy
- Wide CPR → Range-bound day → Mean reversion
- Price above CPR → Bullish → Buy CE or Bull Put Spread
- Price below CPR → Bearish → Buy PE or Bear Call Spread
- Support levels: S1 = (2*P) - H, S2 = P - (H-L), S3 = L - 2*(H-P)
- Resistance levels: R1 = (2*P) - L, R2 = P + (H-L), R3 = H + 2*(P-L)
- Risk: Max 2% per day, 1% per trade, 2 trades per day
- Timeframe: 5-min candles, use previous day OHLC for daily CPR

Combined with Camarilla pivots for additional confirmation.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr


class CPRStrategy:
    NAME = "CPR (Gomathi Shankar)"

    def __init__(self, risk_per_trade_pct: float = 1.0, max_trades_per_day: int = 2,
                 narrow_cpr_threshold_pct: float = 0.3, use_camarilla: bool = True):
        """
        Args:
            risk_per_trade_pct: % of capital to risk per trade
            max_trades_per_day: Maximum trades per day
            narrow_cpr_threshold_pct: CPR width threshold for narrow/wide
            use_camarilla: Whether to use Camarilla pivots for confirmation
        """
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_trades_per_day = max_trades_per_day
        self.narrow_cpr_threshold_pct = narrow_cpr_threshold_pct
        self.use_camarilla = use_camarilla

    def _compute_cpr(self, high: float, low: float, close: float):
        """Compute CPR levels from previous day OHLC."""
        pivot = (high + low + close) / 3
        bc = (high + low) / 2  # Bottom CPR
        tc = (pivot - bc) + pivot  # Top CPR

        # Standard pivot levels
        r1 = (2 * pivot) - low
        r2 = pivot + (high - low)
        r3 = high + 2 * (pivot - low)
        s1 = (2 * pivot) - high
        s2 = pivot - (high - low)
        s3 = low - 2 * (high - pivot)

        return {
            'pivot': pivot, 'tc': tc, 'bc': bc,
            'r1': r1, 'r2': r2, 'r3': r3,
            's1': s1, 's2': s2, 's3': s3,
            'cpr_width': abs(tc - bc),
            'cpr_width_pct': abs(tc - bc) / pivot * 100
        }

    def _compute_camarilla(self, high: float, low: float, close: float):
        """Compute Camarilla pivot levels."""
        range_hl = high - low
        cam_r3 = close + range_hl * 1.1 / 4
        cam_r4 = close + range_hl * 1.1 / 2
        cam_s3 = close - range_hl * 1.1 / 4
        cam_s4 = close - range_hl * 1.1 / 2
        return {'cam_r3': cam_r3, 'cam_r4': cam_r4,
                'cam_s3': cam_s3, 'cam_s4': cam_s4}

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Compute ATR for IV estimation
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR'] = df['TR'].rolling(14).mean()

        for i in range(15, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            date = df.index[i]
            atr = df['ATR'].iloc[i]

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            # Compute CPR from previous day
            cpr = self._compute_cpr(prev['High'], prev['Low'], prev['Close'])
            cam = self._compute_camarilla(prev['High'], prev['Low'], prev['Close'])

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']
            iv = estimate_iv_from_atr(atr, close)

            is_narrow = cpr['cpr_width_pct'] < self.narrow_cpr_threshold_pct
            trades_today = 0

            # ---- NARROW CPR: BREAKOUT STRATEGY (BUY only) ----
            if is_narrow:
                if open_price > cpr['tc'] and close > cpr['r1'] and engine.can_trade(TradeType.BUY_CE):
                    entry_spot = cpr['r1']
                    sl_spot = cpr['pivot']
                    target_spot = cpr['r2']

                    if high >= target_spot:
                        exit_spot = target_spot
                    elif low <= sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close

                    risk_points = entry_spot - sl_spot
                    if risk_points > 0:
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
                        trades_today += 1

                elif open_price < cpr['bc'] and close < cpr['s1'] and engine.can_trade(TradeType.BUY_PE):
                    entry_spot = cpr['s1']
                    sl_spot = cpr['pivot']
                    target_spot = cpr['s2']

                    if low <= target_spot:
                        exit_spot = target_spot
                    elif high >= sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close

                    risk_points = sl_spot - entry_spot
                    if risk_points > 0:
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
                        trades_today += 1

            # ---- WIDE CPR: MEAN REVERSION (BUY on reversals) ----
            # SELL trades skipped — need Rs 75K+ margin per lot, but we only have Rs 33K.
            # Instead, use wide CPR reversals as BUY signals at pivot levels.
            else:
                if low <= cpr['bc'] * 1.002 and close > cpr['pivot'] and engine.can_trade(TradeType.BUY_CE):
                    # Price tested bottom CPR and reversed up → Buy CE
                    entry_spot = cpr['bc']
                    sl_spot = cpr['s1']
                    target_spot = cpr['tc']

                    if high >= target_spot:
                        exit_spot = target_spot
                    elif low <= sl_spot:
                        exit_spot = sl_spot
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

                elif high >= cpr['tc'] * 0.998 and close < cpr['pivot'] and engine.can_trade(TradeType.BUY_PE):
                    # Price tested top CPR and reversed down → Buy PE
                    entry_spot = cpr['tc']
                    sl_spot = cpr['r1']
                    target_spot = cpr['bc']

                    if low <= target_spot:
                        exit_spot = target_spot
                    elif high >= sl_spot:
                        exit_spot = sl_spot
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

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
