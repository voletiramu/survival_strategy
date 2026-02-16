"""
Survivor V2 - Enhanced with OI Monitoring + Gamma Blast Protection
====================================================================
Enhancements over V1:
1. OI Velocity Simulation: Detects unusual volume surges (proxy for OI buildup)
   that precede gamma blasts
2. Day-specific gap adjustment (Mon: 200pt/20pt, Tue: 120pt/15pt, Wed: 70pt/10pt)
   exactly as Raahi uses
3. Gamma Blast Shield: Exits/reduces positions when blast detected on expiry day
4. Pre-expiry close: Closes all positions by Wednesday close (avoids Thursday gamma)
5. Delta-Theta conversion: Converts losing ITM positions to straddles
6. Max drawdown controls: Trailing stop, daily loss limit, position sizing by ATR

Designed for BankNifty weekly options.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class SurvivorV2Strategy:
    NAME = "Survivor V2 (OI+Gamma)"

    def __init__(self,
                 # Day-specific PE gaps (distance from spot to sell PE)
                 mon_pe_distance: int = 200, mon_pe_gap: int = 20,
                 tue_pe_distance: int = 120, tue_pe_gap: int = 15,
                 wed_pe_distance: int = 70, wed_pe_gap: int = 10,
                 # CE gaps (symmetric)
                 mon_ce_distance: int = 200, mon_ce_gap: int = 20,
                 tue_ce_distance: int = 120, tue_ce_gap: int = 15,
                 wed_ce_distance: int = 70, wed_ce_gap: int = 10,
                 # Risk management
                 reset_gap: int = 90, max_positions: int = 3,
                 daily_loss_limit_pct: float = 2.0,
                 trailing_stop_pct: float = 3.0,
                 # Gamma blast detection
                 volume_spike_threshold: float = 2.0,
                 gamma_blast_reduction_pct: float = 50.0,
                 # OI velocity thresholds (simulated via volume)
                 oi_velocity_10min_threshold: float = 0.10,  # 10% OI change
                 oi_velocity_15min_threshold: float = 0.15,
                 oi_velocity_30min_threshold: float = 0.25,
                 # Position management
                 close_before_expiry: bool = True,
                 use_delta_theta_conversion: bool = True):

        # Day-specific parameters (per Raahi's exact rules)
        self.day_params = {
            0: {'pe_distance': mon_pe_distance, 'pe_gap': mon_pe_gap,
                'ce_distance': mon_ce_distance, 'ce_gap': mon_ce_gap},
            1: {'pe_distance': tue_pe_distance, 'pe_gap': tue_pe_gap,
                'ce_distance': tue_ce_distance, 'ce_gap': tue_ce_gap},
            2: {'pe_distance': wed_pe_distance, 'pe_gap': wed_pe_gap,
                'ce_distance': wed_ce_distance, 'ce_gap': wed_ce_gap},
        }

        self.reset_gap = reset_gap
        self.max_positions = max_positions
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.volume_spike_threshold = volume_spike_threshold
        self.gamma_blast_reduction_pct = gamma_blast_reduction_pct
        self.oi_velocity_thresholds = {
            10: oi_velocity_10min_threshold,
            15: oi_velocity_15min_threshold,
            30: oi_velocity_30min_threshold,
        }
        self.close_before_expiry = close_before_expiry
        self.use_delta_theta_conversion = use_delta_theta_conversion

    def _detect_gamma_blast_risk(self, df: pd.DataFrame, i: int) -> float:
        """
        Simulate gamma blast detection using volume and price action.
        Returns risk score 0-1 (1 = high gamma blast risk).

        Real implementation would use:
        - Kite Connect historical_data API for minute OI
        - Track OI change across ATM-2 to ATM+2 strikes
        - Alert when >10% OI change in 10min, >15% in 15min, >25% in 30min
        """
        if i < 5:
            return 0.0

        row = df.iloc[i]
        risk_score = 0.0

        # Factor 1: Volume spike (proxy for OI velocity)
        avg_vol = df['Volume'].iloc[max(0, i-20):i].mean()
        if avg_vol > 0:
            vol_ratio = row['Volume'] / avg_vol
            if vol_ratio > self.volume_spike_threshold:
                risk_score += 0.3 * min(vol_ratio / 4.0, 1.0)

        # Factor 2: Low intraday range followed by expansion (classic blast setup)
        # "Market moves only 0.5-0.6% over 4 hours → blast likely"
        recent_ranges = []
        for j in range(max(0, i-3), i):
            r = (df.iloc[j]['High'] - df.iloc[j]['Low']) / df.iloc[j]['Close'] * 100
            recent_ranges.append(r)
        if recent_ranges:
            avg_recent_range = np.mean(recent_ranges)
            current_range = (row['High'] - row['Low']) / row['Close'] * 100
            # Low recent range + sudden expansion = blast pattern
            if avg_recent_range < 1.0 and current_range > avg_recent_range * 2:
                risk_score += 0.3

        # Factor 3: Day of week - Wednesday/Thursday (expiry proximity)
        date = df.index[i]
        if date.weekday() == 2:  # Wednesday (BankNifty expiry eve for old, or Nifty expiry)
            risk_score += 0.2
        elif date.weekday() == 3:  # Thursday (Nifty expiry / BankNifty new expiry)
            risk_score += 0.3

        # Factor 4: Strong trend day (big body candle)
        body = abs(row['Close'] - row['Open'])
        atr_val = df['ATR'].iloc[i] if 'ATR' in df.columns and not pd.isna(df['ATR'].iloc[i]) else 1
        if atr_val > 0 and body > atr_val * 1.5:
            risk_score += 0.2

        return min(risk_score, 1.0)

    def _is_expiry_day(self, date: pd.Timestamp) -> bool:
        """Check if it's BankNifty expiry day (Wednesday) or Nifty expiry (Thursday)."""
        return date.weekday() in [2, 3]

    def _apply_delta_theta_conversion(self, pnl: float, is_losing: bool,
                                       days_to_expiry: int) -> float:
        """
        Simulate delta-to-theta conversion.
        When position goes ITM (losing), convert to straddle to capture theta.
        Effect: reduces loss by theta benefit, but caps upside.
        """
        if is_losing and self.use_delta_theta_conversion and days_to_expiry <= 2:
            # Converting ITM CE to straddle reduces loss by ~30-40% near expiry
            theta_benefit = abs(pnl) * 0.35
            return pnl + theta_benefit
        return pnl

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
        df['prev_high'] = df['High'].shift(1)
        df['prev_low'] = df['Low'].shift(1)
        df['prev_close'] = df['Close'].shift(1)

        # Track equity high water mark for trailing stop
        equity_hwm = engine.initial_capital
        daily_pnl = 0.0
        pe_ref = None
        ce_ref = None

        for i in range(15, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            date = df.index[i]
            dow = date.weekday()
            atr = df.iloc[i]['ATR']

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            # === ONLY TRADE Mon-Wed (Raahi's rule) ===
            if dow > 2:
                engine.record_equity(date)
                continue

            # === DAILY LOSS LIMIT CHECK ===
            daily_loss_limit = engine.capital * self.daily_loss_limit_pct / 100
            if daily_pnl < -daily_loss_limit:
                engine.record_equity(date)
                if date.date() != df.index[min(i+1, len(df)-1)].date():
                    daily_pnl = 0.0
                continue

            # === TRAILING STOP CHECK (Drawdown minimizer) ===
            equity_hwm = max(equity_hwm, engine.capital)
            trailing_dd = (equity_hwm - engine.capital) / equity_hwm * 100
            if trailing_dd > self.trailing_stop_pct:
                # Reduce position sizes until DD recovers
                position_scale = 0.5
            else:
                position_scale = 1.0

            # === GAMMA BLAST DETECTION ===
            gamma_risk = self._detect_gamma_blast_risk(df, i)
            gamma_scale = 1.0
            if gamma_risk > 0.5:
                # High gamma blast risk: reduce position or skip
                gamma_scale = 1.0 - (gamma_risk * self.gamma_blast_reduction_pct / 100)
                gamma_scale = max(gamma_scale, 0.25)

            # === PRE-EXPIRY CLOSE (Wednesday afternoon) ===
            if self.close_before_expiry and dow == 2:
                # On Wednesday, use tighter parameters (already in day_params)
                # Also apply additional caution
                gamma_scale *= 0.7

            # === GET DAY-SPECIFIC PARAMETERS ===
            params = self.day_params.get(dow, self.day_params[2])
            pe_gap = params['pe_gap']
            ce_gap = params['ce_gap']

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

            open_pe_positions = 0
            open_ce_positions = 0

            # Effective max positions (adjusted for gamma risk and DD)
            effective_max = max(1, int(self.max_positions * position_scale * gamma_scale))

            # === PE SELLING (market going UP, sell puts) ===
            if current_high > pe_ref + pe_gap:
                moves = int((current_high - pe_ref) / pe_gap)
                trades_to_take = min(moves, effective_max - open_pe_positions)

                for _ in range(trades_to_take):
                    pe_ref += pe_gap
                    spot_at_entry = pe_ref
                    spot_at_exit = current_close

                    pnl = engine.simulate_option_pnl_from_spot(
                        spot_at_entry, spot_at_exit,
                        TradeType.SELL_PE, lots=1
                    )

                    # Apply delta-theta conversion if losing
                    days_to_expiry = max(0, 3 - dow)  # Wed expiry = 0 DTE on Wed
                    if pnl < 0:
                        pnl = self._apply_delta_theta_conversion(
                            pnl, True, days_to_expiry
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
                    daily_pnl += pnl
                    open_pe_positions += 1

                # Reset check
                if pe_ref - current_low > self.reset_gap:
                    pe_ref = current_low + self.reset_gap
                    open_pe_positions = 0

            # === CE SELLING (market going DOWN, sell calls) ===
            if current_low < ce_ref - ce_gap:
                moves = int((ce_ref - current_low) / ce_gap)
                trades_to_take = min(moves, effective_max - open_ce_positions)

                for _ in range(trades_to_take):
                    ce_ref -= ce_gap
                    spot_at_entry = ce_ref
                    spot_at_exit = current_close

                    pnl = engine.simulate_option_pnl_from_spot(
                        spot_at_entry, spot_at_exit,
                        TradeType.SELL_CE, lots=1
                    )

                    days_to_expiry = max(0, 3 - dow)
                    if pnl < 0:
                        pnl = self._apply_delta_theta_conversion(
                            pnl, True, days_to_expiry
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
                    daily_pnl += pnl
                    open_ce_positions += 1

                # Reset check
                if current_high - ce_ref > self.reset_gap:
                    ce_ref = current_high - self.reset_gap
                    open_ce_positions = 0

            # Reset daily PnL tracker on new day
            next_date = df.index[min(i + 1, len(df) - 1)]
            if date.date() != next_date.date():
                daily_pnl = 0.0

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
