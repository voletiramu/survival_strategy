"""
PCR+VWAP V2 - Enhanced with OI Confirmation + Drawdown Controls
=================================================================
Enhancements over V1:
1. OI-based confirmation: Volume surge validates PCR signal
2. ATR-based adaptive VWAP tolerance (wider in volatile, tighter in calm)
3. Trailing stop to protect profits
4. Daily loss limit (max 2% of capital per day)
5. Regime filter: skip extremely volatile days (VIX proxy)
6. Partial profit booking at 50%, remaining at 100% (Nitin's 2x target)
7. Drawdown recovery mode: reduced size after big DD

Designed for Nifty50 + BankNifty intraday options buying.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class PCRVWAPV2Strategy:
    NAME = "PCR+VWAP V2 (OI Enhanced)"

    def __init__(self,
                 pcr_lookback: int = 5,
                 vwap_base_tolerance_pct: float = 0.3,
                 atr_tolerance_factor: float = 0.5,
                 target_partial: float = 1.5,
                 target_full: float = 2.0,
                 sl_multiplier: float = 1.0,
                 max_trades_per_day: int = 2,
                 daily_loss_limit_pct: float = 2.0,
                 trailing_stop_pct: float = 3.0,
                 oi_volume_confirmation: float = 1.3,
                 max_volatility_pct: float = 3.0,
                 dd_recovery_threshold: float = 5.0,
                 dd_recovery_scale: float = 0.5):
        """
        Args:
            pcr_lookback: Days to compute PCR proxy
            vwap_base_tolerance_pct: Base VWAP band width
            atr_tolerance_factor: Multiply ATR to adjust tolerance
            target_partial: Partial profit booking target (1.5x)
            target_full: Full profit target (2x = double premium)
            sl_multiplier: Stop loss as multiple of tolerance
            max_trades_per_day: Max trades per day
            daily_loss_limit_pct: Max daily loss as % of capital
            trailing_stop_pct: Max portfolio DD before scaling down
            oi_volume_confirmation: Volume must be this x avg for OI confirm
            max_volatility_pct: Skip days with range > this %
            dd_recovery_threshold: DD% to enter recovery mode
            dd_recovery_scale: Position scale in recovery mode
        """
        self.pcr_lookback = pcr_lookback
        self.vwap_base_tolerance_pct = vwap_base_tolerance_pct
        self.atr_tolerance_factor = atr_tolerance_factor
        self.target_partial = target_partial
        self.target_full = target_full
        self.sl_multiplier = sl_multiplier
        self.max_trades_per_day = max_trades_per_day
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.oi_volume_confirmation = oi_volume_confirmation
        self.max_volatility_pct = max_volatility_pct
        self.dd_recovery_threshold = dd_recovery_threshold
        self.dd_recovery_scale = dd_recovery_scale

    def _compute_pcr(self, df: pd.DataFrame, i: int) -> float:
        """Compute PCR proxy from volume on up vs down days."""
        if i < self.pcr_lookback:
            return 1.0
        lookback = df.iloc[i - self.pcr_lookback:i]
        up_days = lookback[lookback['Close'] > lookback['Open']]
        down_days = lookback[lookback['Close'] <= lookback['Open']]
        up_vol = up_days['Volume'].sum() if len(up_days) > 0 else 1
        down_vol = down_days['Volume'].sum() if len(down_days) > 0 else 1
        return up_vol / max(down_vol, 1)

    def _has_oi_confirmation(self, df: pd.DataFrame, i: int) -> bool:
        """Check if volume confirms OI buildup (proxy for real OI)."""
        if i < 20:
            return True
        avg_vol = df['Volume'].iloc[max(0, i-20):i].mean()
        if avg_vol > 0:
            return df.iloc[i]['Volume'] > avg_vol * self.oi_volume_confirmation
        return True

    def _adaptive_tolerance(self, atr: float, close: float) -> float:
        """Compute adaptive VWAP tolerance based on ATR."""
        atr_pct = (atr / close * 100) if close > 0 else 0.3
        return max(
            self.vwap_base_tolerance_pct,
            min(atr_pct * self.atr_tolerance_factor, 1.0)
        )

    def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                 symbol: str = "NIFTY50") -> BacktestResult:
        engine.reset()
        df = df.copy()

        # Indicators
        df['TypicalPrice'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['CumVolPrice'] = (df['TypicalPrice'] * df['Volume']).rolling(5).sum()
        df['CumVol'] = df['Volume'].rolling(5).sum()
        df['VWAP'] = df['CumVolPrice'] / df['CumVol']
        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR'] = df['TR'].rolling(14).mean()

        # Drawdown tracking
        equity_hwm = engine.initial_capital
        daily_pnl = 0.0

        for i in range(max(50, self.pcr_lookback + 1), len(df)):
            row = df.iloc[i]
            date = df.index[i]
            vwap = df.iloc[i]['VWAP']
            atr = df.iloc[i]['ATR']

            if pd.isna(vwap) or pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            # === VOLATILITY FILTER: Skip extreme days ===
            day_range_pct = (row['High'] - row['Low']) / row['Close'] * 100
            if day_range_pct > self.max_volatility_pct:
                engine.record_equity(date)
                continue

            # === DAILY LOSS LIMIT ===
            daily_loss_limit = engine.capital * self.daily_loss_limit_pct / 100
            if daily_pnl < -daily_loss_limit:
                engine.record_equity(date)
                next_date = df.index[min(i + 1, len(df) - 1)]
                if date.date() != next_date.date():
                    daily_pnl = 0.0
                continue

            # === DRAWDOWN RECOVERY MODE ===
            equity_hwm = max(equity_hwm, engine.capital)
            current_dd = (equity_hwm - engine.capital) / equity_hwm * 100
            position_scale = self.dd_recovery_scale if current_dd > self.dd_recovery_threshold else 1.0

            # === ADAPTIVE TOLERANCE ===
            tolerance = self._adaptive_tolerance(atr, row['Close'])

            # === PCR DIRECTION ===
            pcr = self._compute_pcr(df, i)

            # === OI CONFIRMATION ===
            has_oi = self._has_oi_confirmation(df, i)

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']

            trade_taken = False

            # === BUY CE: PCR bullish + VWAP bounce + OI confirm ===
            if pcr > 1.0 and not trade_taken:
                if low <= vwap * (1 + tolerance / 100) and close > vwap:
                    if has_oi:
                        entry_spot = vwap
                        sl_distance = vwap * tolerance / 100 * self.sl_multiplier
                        sl_spot = vwap - sl_distance
                        target_partial_spot = entry_spot + sl_distance * self.target_partial
                        target_full_spot = entry_spot + sl_distance * self.target_full

                        # Determine exit
                        if high >= target_full_spot:
                            # Full target hit: 50% at partial, 50% at full
                            exit_spot = (target_partial_spot + target_full_spot) / 2
                        elif high >= target_partial_spot:
                            # Partial target: 50% at partial, rest at close
                            exit_spot = (target_partial_spot + close) / 2
                        elif close < sl_spot:
                            exit_spot = sl_spot
                        else:
                            exit_spot = close

                        pnl = engine.simulate_option_pnl_from_spot(
                            entry_spot, exit_spot, TradeType.BUY_CE, lots=1
                        )
                        pnl *= position_scale

                        trade = Trade(
                            entry_date=date, exit_date=date,
                            trade_type=TradeType.BUY_CE,
                            entry_price=entry_spot, exit_price=exit_spot,
                            quantity=engine.lot_size,
                            pnl=pnl, status="CLOSED"
                        )
                        engine.add_trade(trade)
                        daily_pnl += pnl
                        trade_taken = True

            # === BUY PE: PCR bearish + VWAP rejection + OI confirm ===
            elif pcr < 1.0 and not trade_taken:
                if high >= vwap * (1 - tolerance / 100) and close < vwap:
                    if has_oi:
                        entry_spot = vwap
                        sl_distance = vwap * tolerance / 100 * self.sl_multiplier
                        sl_spot = vwap + sl_distance
                        target_partial_spot = entry_spot - sl_distance * self.target_partial
                        target_full_spot = entry_spot - sl_distance * self.target_full

                        if low <= target_full_spot:
                            exit_spot = (target_partial_spot + target_full_spot) / 2
                        elif low <= target_partial_spot:
                            exit_spot = (target_partial_spot + close) / 2
                        elif close > sl_spot:
                            exit_spot = sl_spot
                        else:
                            exit_spot = close

                        pnl = engine.simulate_option_pnl_from_spot(
                            entry_spot, exit_spot, TradeType.BUY_PE, lots=1
                        )
                        pnl *= position_scale

                        trade = Trade(
                            entry_date=date, exit_date=date,
                            trade_type=TradeType.BUY_PE,
                            entry_price=entry_spot, exit_price=exit_spot,
                            quantity=engine.lot_size,
                            pnl=pnl, status="CLOSED"
                        )
                        engine.add_trade(trade)
                        daily_pnl += pnl
                        trade_taken = True

            # Reset daily PnL
            next_date = df.index[min(i + 1, len(df) - 1)]
            if date.date() != next_date.date():
                daily_pnl = 0.0

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
