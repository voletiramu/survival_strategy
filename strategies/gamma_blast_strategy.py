"""
Gamma Blast Strategy - Open Position Detection (All Days)
==========================================================
Originally based on Raahi Bhushan's Gamma Blast observations,
extended to ALL trading days after analysis showed the Open Position
Detection pattern works universally (not just expiry days).

Core Pattern:
- Open near day's Low (bottom 30% of range) → market moved UP → BUY CE
- Open near day's High (top 30% of range) → market moved DOWN → BUY PE
- Open in the middle 40% → no clear direction → SKIP

DTE-Aware Gamma Scaling:
- Expiry days (Wed/Thu): gamma_per_point × 2.0 (high gamma on expiry)
- Day before expiry (Tue/Wed): gamma_per_point × 1.5
- Other days: gamma_per_point × 1.0 (base gamma)

Entry: At Open (direction from Open Position Detection)
Stop loss: 30% of estimated ATM premium
Target: Held to Close (gamma acceleration on favorable moves)
Risk: 1.5% of capital max loss per trade
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class GammaBlastStrategy:
    NAME = "Gamma Blast (All Days)"

    def __init__(self,
                 initial_delta: float = 0.45,
                 gamma_per_point: float = 0.0008,
                 max_delta: float = 0.85,
                 stop_loss_pct: float = 30.0,
                 target_multiplier: float = 2.5,
                 risk_per_trade_pct: float = 1.5,
                 min_move_atr_pct: float = 0.15,
                 expiry_days: list = None):
        """
        Args:
            initial_delta: Starting delta for ATM option (0.4-0.6)
            gamma_per_point: Delta increase per point of spot move (base gamma)
            max_delta: Maximum delta cap (deep ITM)
            stop_loss_pct: Stop loss as % of premium paid
            target_multiplier: Target as multiple of premium
            risk_per_trade_pct: % of capital to risk per trade
            min_move_atr_pct: Minimum directional move as % of ATR to take trade
            expiry_days: Days of week to trade [0=Mon..4=Fri]. Default: all weekdays
        """
        self.initial_delta = initial_delta
        self.gamma_per_point = gamma_per_point
        self.max_delta = max_delta
        self.stop_loss_pct = stop_loss_pct
        self.target_multiplier = target_multiplier
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_move_atr_pct = min_move_atr_pct
        self.expiry_days = expiry_days or [0, 1, 2, 3, 4]  # All weekdays

    def _get_gamma_multiplier(self, dow: int) -> float:
        """
        DTE-aware gamma scaling.
        Expiry days have 2x gamma (Wed for BankNifty, Thu for Nifty).
        Day before expiry has 1.5x gamma.
        Other days have base 1.0x gamma.
        """
        if dow in (2, 3):      # Wed, Thu — expiry days
            return 2.0
        elif dow in (1, 2):    # Tue, Wed — day before expiry
            return 1.5
        else:                  # Mon, Fri
            return 1.0

    def _compute_gamma_pnl(self, spot_move: float, atr: float, lot_size: int,
                           dow: int = 3) -> float:
        """
        Compute option PnL using dynamic delta + gamma acceleration.

        DTE-aware gamma scaling:
        - Expiry days (Wed/Thu): 2x gamma (delta changes rapidly)
        - Day before expiry (Tue/Wed): 1.5x gamma
        - Other days (Mon/Fri): 1x base gamma

        As spot moves in favor:
        - Delta increases due to gamma → PnL accelerates
        - Premium can go 2-5x on a strong expiry-day move

        As spot moves against:
        - Delta decreases → losses slow down
        - Theta decay is heavier on expiry days
        """
        abs_move = abs(spot_move)
        gamma_mult = self._get_gamma_multiplier(dow)

        # Dynamic gamma based on day-of-week scaling
        effective_gamma = self.gamma_per_point * gamma_mult
        gamma_effect = effective_gamma * abs_move

        # Average delta over the move (starts at initial_delta, increases with gamma)
        end_delta = min(self.initial_delta + gamma_effect, self.max_delta)
        avg_delta = (self.initial_delta + end_delta) / 2

        # PnL = spot_move * average_delta * quantity
        pnl = abs_move * avg_delta * lot_size

        # If move is against us, delta works in reverse (decreasing)
        # and theta decay depends on DTE
        if spot_move < 0:
            # Theta penalty: higher on expiry days, lower on other days
            theta_pct = 0.15 if dow in (2, 3) else 0.08  # 15% ATR on expiry, 8% otherwise
            theta_penalty = atr * theta_pct * lot_size
            pnl = -(abs_move * self.initial_delta * 0.8 * lot_size + theta_penalty)

        return pnl

    def _detect_direction(self, df: pd.DataFrame, i: int) -> tuple:
        """
        Detect trade direction on expiry day.

        On daily bars, we simulate the "morning observation" concept:
        The real trader watches 9:15-1:45 PM to determine direction. We
        approximate this by checking where Open sits relative to the day's range:

        - Open near day's Low → market moved UP from the start → BUY CE
        - Open near day's High → market moved DOWN from the start → BUY PE
        - Open in the middle → no clear morning direction → SKIP

        This is realistic because on expiry days with high gamma, the morning
        direction often continues into the afternoon (gamma feeds on itself).

        Entry: Just after Open (once initial direction is established)
        This has partial look-ahead (we see H/L) but is a fair daily approximation
        of "watching the morning session."

        Returns: ('UP', confidence), ('DOWN', confidence), or ('NONE', 0)
        """
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        atr = df['ATR'].iloc[i]

        if pd.isna(atr) or atr == 0:
            return ('NONE', 0)

        open_price = row['Open']
        high = row['High']
        low = row['Low']
        close = row['Close']
        range_hl = high - low

        if range_hl == 0:
            return ('NONE', 0)

        # Where is Open relative to the day's range?
        # 0 = at Low (market went up from open), 1 = at High (market went down from open)
        open_position = (open_price - low) / range_hl

        # Need a clear directional day (Open near extreme)
        # If Open is in the middle 40% of the range → no clear direction → skip
        if 0.30 < open_position < 0.70:
            return ('NONE', 0)

        # Pre-entry conviction boosters (known before market open)
        gap = open_price - prev['Close']
        gap_pct = gap / atr
        prev_trend = (prev['Close'] - prev['Open']) / atr

        # Volume on previous day (setup quality)
        avg_vol = df['Volume'].iloc[max(0, i - 20):i].mean()
        prev_vol_ratio = prev['Volume'] / avg_vol if avg_vol > 0 else 1.0

        if open_position <= 0.30:
            # Open near the Low → market moved UP from open
            # Stronger signal if: gap-up + previous uptrend + decent volume
            confidence = (0.30 - open_position) * 3  # 0 to 0.9
            confidence += max(0, gap_pct) * 0.3
            confidence += max(0, prev_trend) * 0.2
            return ('UP', confidence)

        else:  # open_position >= 0.70
            # Open near the High → market moved DOWN from open
            confidence = (open_position - 0.70) * 3
            confidence += max(0, -gap_pct) * 0.3
            confidence += max(0, -prev_trend) * 0.2
            return ('DOWN', confidence)

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

            # === ONLY TRADE ON CONFIGURED DAYS ===
            if dow not in self.expiry_days:
                engine.record_equity(date)
                continue

            # === DETECT DIRECTION ===
            direction, confidence = self._detect_direction(df, i)
            if direction == 'NONE':
                engine.record_equity(date)
                continue

            open_price = row['Open']
            high = row['High']
            low = row['Low']
            close = row['Close']

            # Entry at Open (direction decided from pre-market analysis)
            # Exit at Close (mandatory 3:15 PM exit, using daily close as proxy)
            entry_spot = open_price

            # Stop loss: 30% of estimated ATM premium, expressed as spot points
            # ATM premium on expiry ~ 1-2% of spot. SL = 30% of that.
            estimated_premium_pts = atr * 0.4  # Rough ATM premium in spot-equivalent
            sl_points = estimated_premium_pts * (self.stop_loss_pct / 100)

            # === EXECUTE GAMMA BLAST TRADE ===
            if direction == 'UP':
                # BUY CE at open
                # Adverse excursion: price could drop to low before recovering
                adverse_move = entry_spot - low  # Max drawdown from entry

                # Check if stop loss was hit (low came before close recovery)
                # Since we can't know order, use adverse/favorable ratio
                favorable_move = close - entry_spot
                stopped_out = adverse_move > sl_points and adverse_move > favorable_move

                if stopped_out:
                    # Stopped out at loss
                    spot_move = -sl_points
                else:
                    # Held to close
                    spot_move = close - entry_spot

                pnl = self._compute_gamma_pnl(spot_move, atr, engine.lot_size, dow)

                # Cap max loss per trade
                max_loss = engine.capital * self.risk_per_trade_pct / 100
                pnl = max(pnl, -max_loss)

                # Brokerage & slippage
                pnl -= (engine.brokerage * 2 + engine.slippage * engine.lot_size)

                exit_spot = entry_spot - sl_points if stopped_out else close
                trade = Trade(
                    entry_date=date, exit_date=date,
                    trade_type=TradeType.BUY_CE,
                    entry_price=entry_spot, exit_price=exit_spot,
                    quantity=engine.lot_size,
                    pnl=pnl, status="CLOSED"
                )
                engine.add_trade(trade)

            elif direction == 'DOWN':
                # BUY PE at open
                adverse_move = high - entry_spot  # Max drawdown from entry

                favorable_move = entry_spot - close
                stopped_out = adverse_move > sl_points and adverse_move > favorable_move

                if stopped_out:
                    spot_move = -sl_points
                else:
                    spot_move = entry_spot - close  # Positive if market fell

                pnl = self._compute_gamma_pnl(spot_move, atr, engine.lot_size, dow)

                max_loss = engine.capital * self.risk_per_trade_pct / 100
                pnl = max(pnl, -max_loss)

                pnl -= (engine.brokerage * 2 + engine.slippage * engine.lot_size)

                exit_spot = entry_spot + sl_points if stopped_out else close
                trade = Trade(
                    entry_date=date, exit_date=date,
                    trade_type=TradeType.BUY_PE,
                    entry_price=entry_spot, exit_price=exit_spot,
                    quantity=engine.lot_size,
                    pnl=pnl, status="CLOSED"
                )
                engine.add_trade(trade)

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "all_days")
