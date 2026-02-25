"""
Ghost Traders - Ghost Trade Zone (GTZ) Strategy
=================================================
Type: Option BUYING/SELLING based on institutional demand/supply zones
Logic:
- Identify institutional demand zones (where big buyers stepped in)
- Identify institutional supply zones (where big sellers stepped in)
- Demand zone: Strong bullish move away from consolidation → Buy CE on retest
- Supply zone: Strong bearish move away from consolidation → Buy PE on retest
- Stop loss below demand zone / above supply zone
- Target: Next supply zone (for longs) or next demand zone (for shorts)

Implementation uses volume-price analysis to identify zones since
the proprietary GTI indicator is not available.
"""

import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


class GhostZoneStrategy:
    NAME = "Ghost Trade Zone (GTZ)"

    def __init__(self, zone_lookback: int = 20, volume_threshold: float = 1.2,
                 zone_width_atr_mult: float = 0.8, min_impulse_atr_mult: float = 0.8,
                 sl_atr_mult: float = 0.5, target_rr: float = 2.0):
        """
        Args:
            zone_lookback: Bars to look back for zone identification
            volume_threshold: Volume must be this multiple of average
            zone_width_atr_mult: Zone width as multiple of ATR
            min_impulse_atr_mult: Minimum impulse move to create zone
            sl_atr_mult: Stop loss distance as ATR multiple
            target_rr: Target as risk-reward multiple
        """
        self.zone_lookback = zone_lookback
        self.volume_threshold = volume_threshold
        self.zone_width_atr_mult = zone_width_atr_mult
        self.min_impulse_atr_mult = min_impulse_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.target_rr = target_rr

    def _find_zones(self, df: pd.DataFrame, i: int, atr: float):
        """Find demand and supply zones from recent price action."""
        demand_zones = []
        supply_zones = []

        if i < self.zone_lookback + 5:
            return demand_zones, supply_zones

        lookback = df.iloc[i - self.zone_lookback:i]
        avg_vol = lookback['Volume'].mean()
        zone_width = atr * self.zone_width_atr_mult

        for j in range(2, len(lookback) - 2):
            bar = lookback.iloc[j]
            prev_bar = lookback.iloc[j - 1]
            next_bar = lookback.iloc[j + 1]

            body = abs(bar['Close'] - bar['Open'])
            is_high_volume = bar['Volume'] > avg_vol * self.volume_threshold

            # Demand zone: consolidation followed by strong bullish impulse
            if (next_bar['Close'] - next_bar['Open'] > atr * self.min_impulse_atr_mult
                    and is_high_volume
                    and body < atr * 0.8):  # Relatively small body = consolidation
                zone_low = min(bar['Low'], prev_bar['Low'])
                zone_high = zone_low + zone_width
                demand_zones.append((zone_low, zone_high))

            # Supply zone: consolidation followed by strong bearish impulse
            if (next_bar['Open'] - next_bar['Close'] > atr * self.min_impulse_atr_mult
                    and is_high_volume
                    and body < atr * 0.8):
                zone_high = max(bar['High'], prev_bar['High'])
                zone_low = zone_high - zone_width
                supply_zones.append((zone_low, zone_high))

        return demand_zones, supply_zones

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

        for i in range(self.zone_lookback + 15, len(df)):
            row = df.iloc[i]
            date = df.index[i]
            atr = df.iloc[i]['ATR']

            if pd.isna(atr) or atr == 0:
                engine.record_equity(date)
                continue

            low = row['Low']
            high = row['High']
            close = row['Close']
            open_price = row['Open']

            demand_zones, supply_zones = self._find_zones(df, i, atr)
            trade_taken = False

            # Check demand zone retests (Buy CE)
            for zone_low, zone_high in demand_zones:
                if not trade_taken and low <= zone_high * 1.005 and low >= zone_low * 0.99 and close > zone_high:
                    # Price retested demand zone and bounced
                    entry_spot = zone_high
                    sl_spot = zone_low - atr * self.sl_atr_mult
                    risk = entry_spot - sl_spot
                    target_spot = entry_spot + risk * self.target_rr

                    if high >= target_spot:
                        exit_spot = target_spot
                    elif low <= sl_spot:
                        exit_spot = sl_spot
                    else:
                        exit_spot = close

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
                    break

            # Check supply zone retests (Buy PE)
            for zone_low, zone_high in supply_zones:
                if not trade_taken and high >= zone_low * 0.995 and high <= zone_high * 1.01 and close < zone_low:
                    entry_spot = zone_low
                    sl_spot = zone_high + atr * self.sl_atr_mult
                    risk = sl_spot - entry_spot
                    target_spot = entry_spot - risk * self.target_rr

                    if low <= target_spot:
                        exit_spot = target_spot
                    elif high >= sl_spot:
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
                    break

            engine.record_equity(date)

        return engine.compute_results(self.NAME, symbol, "intraday_simulated")
