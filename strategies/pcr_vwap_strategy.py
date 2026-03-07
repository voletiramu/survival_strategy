"""
CA Nitin Muraka - PCR + VWAP Strategy
========================================
Type: Option BUYING strategy

Instrument-agnostic signal generator (for real-time paper/live trading):
- Uses PCR (Put-Call Ratio) for directional bias:
  PCR > 1.05 → Bullish → Buy CE
  PCR < 0.95 → Bearish → Buy PE
- Entry: Spot near VWAP (within 2x ATR tolerance)
- Target: 50% premium gain (intraday realistic)
- SL: 60% premium loss

Backtest class (PCRVWAPStrategy) preserved below for historical backtesting.

v4: Fixed look-ahead bias by using signal-today, trade-tomorrow approach.
v5: Added instrument-agnostic check_pcr_vwap() for real-time trading.
"""

from .greeks import bs_greeks
from .utils import RISK_FREE_RATE, get_nearest_strike


# ====================================================================
# INSTRUMENT-AGNOSTIC SIGNAL GENERATOR (for paper/live trading)
# ====================================================================

def check_pcr_vwap(spot, indicators, config):
    """PCR+VWAP -- instrument-agnostic signal generator.

    Uses real option chain PCR for directional bias, VWAP for entry timing.
    Generates BUY CE when bullish (PCR > 1.05, near VWAP from below),
    BUY PE when bearish (PCR < 0.95, near VWAP from above).

    Args:
        spot: Current spot price.
        indicators: dict from compute_all_indicators() with keys:
            pcr, vwap, atr, iv.
        config: Strategy config dict:
            'strike_interval': int (e.g. 50 for NIFTY, 10 for CIPLA),
            'min_premium_buy': float (e.g. 15),
            'dte': int (days to expiry),
            'r': float (risk-free rate, default RISK_FREE_RATE),
            'iv_cap': float (max IV for entry, default 0.50),
            'pcr_bull_threshold': float (PCR > this = bullish, default 1.05),
            'pcr_bear_threshold': float (PCR < this = bearish, default 0.95),
            'chain_ltp_ce': float (real LTP from option chain, 0 if unavailable),
            'chain_iv_ce': float (real IV from chain, 0 if unavailable),
            'chain_ltp_pe': float (real LTP from option chain, 0 if unavailable),
            'chain_iv_pe': float (real IV from chain, 0 if unavailable),
            'chain_strike_ce': int (chain-selected CE strike, 0 if unavailable),
            'chain_strike_pe': int (chain-selected PE strike, 0 if unavailable).

    Returns:
        list of signal dicts, each with:
            type, strike, premium, greeks, reason, target, sl.
    """
    signals = []
    ind = indicators
    dte = config.get('dte', 7)
    T = dte / 365
    r = config.get('r', RISK_FREE_RATE)
    strike_interval = config.get('strike_interval', 50)
    min_premium_buy = config.get('min_premium_buy', 15)

    # IV cap — skip very high IV scenarios
    iv_cap = config.get('iv_cap', 0.50)
    if ind.get('iv', 0) > iv_cap:
        return signals

    # PCR and VWAP
    pcr = ind.get('pcr', 1.0)
    vwap = ind.get('vwap', 0)
    atr = ind.get('atr', 0)

    if not vwap or not atr or atr <= 0:
        return signals

    # Tolerance for "near VWAP" — wider for stocks with higher ATR
    tolerance = max(atr * 0.1, spot * 0.003)

    pcr_bull = config.get('pcr_bull_threshold', 1.05)
    pcr_bear = config.get('pcr_bear_threshold', 0.95)

    # ---- BUY CE: Bullish PCR, spot near/above VWAP ----
    if pcr > pcr_bull and abs(spot - vwap) < tolerance * 2 and spot >= vwap * 0.995:
        chain_ltp = config.get('chain_ltp_ce', 0)
        chain_iv = config.get('chain_iv_ce', 0)
        chain_strike = config.get('chain_strike_ce', 0)

        ce_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
        use_iv = chain_iv if chain_iv > 0 else ind['iv']
        g = bs_greeks(spot, ce_strike, T, r, use_iv, 'CE')
        premium = chain_ltp if chain_ltp > 0 else g['price']

        if premium > min_premium_buy and premium < spot * 0.05:
            signals.append({
                'type': 'BUY_CE',
                'strike': ce_strike,
                'premium': premium,
                'greeks': g,
                'reason': (f"PCR+VWAP: Bullish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}"
                           f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                'target': premium * 1.5,   # 50% gain (realistic intraday)
                'sl': premium * 0.4,       # 60% SL
            })

    # ---- BUY PE: Bearish PCR, spot near/below VWAP ----
    elif pcr < pcr_bear and abs(spot - vwap) < tolerance * 2 and spot <= vwap * 1.005:
        chain_ltp = config.get('chain_ltp_pe', 0)
        chain_iv = config.get('chain_iv_pe', 0)
        chain_strike = config.get('chain_strike_pe', 0)

        pe_strike = chain_strike if chain_strike > 0 else get_nearest_strike(spot, strike_interval)
        use_iv = chain_iv if chain_iv > 0 else ind['iv']
        g = bs_greeks(spot, pe_strike, T, r, use_iv, 'PE')
        premium = chain_ltp if chain_ltp > 0 else g['price']

        if premium > min_premium_buy and premium < spot * 0.05:
            signals.append({
                'type': 'BUY_PE',
                'strike': pe_strike,
                'premium': premium,
                'greeks': g,
                'reason': (f"PCR+VWAP: Bearish PCR={pcr:.2f} VWAP={vwap:.0f} spot={spot:.0f}"
                           f"{'  [CHAIN]' if chain_ltp > 0 else ''}"),
                'target': premium * 1.5,   # 50% gain (realistic intraday)
                'sl': premium * 0.4,       # 60% SL
            })

    return signals


# ====================================================================
# BACKTEST CLASS (for historical backtesting — uses backtest_engine)
# ====================================================================
try:
    import pandas as pd
    import numpy as np
    from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult, estimate_iv_from_atr

    class PCRVWAPStrategy:
        NAME = "PCR+VWAP (CA Nitin Muraka)"

        def __init__(self, pcr_lookback: int = 5, vwap_tolerance_pct: float = 0.3,
                     target_atr_mult: float = 2.0, sl_atr_mult: float = 1.0,
                     max_trades_per_day: int = 2):
            self.pcr_lookback = pcr_lookback
            self.vwap_tolerance_pct = vwap_tolerance_pct
            self.target_atr_mult = target_atr_mult
            self.sl_atr_mult = sl_atr_mult
            self.max_trades_per_day = max_trades_per_day

        def _compute_simulated_pcr(self, df: pd.DataFrame, i: int) -> float:
            if i < self.pcr_lookback:
                return 1.0
            lookback = df.iloc[i - self.pcr_lookback:i]
            up_days = lookback[lookback['Close'] > lookback['Open']]
            down_days = lookback[lookback['Close'] <= lookback['Open']]
            up_vol = up_days['Volume'].sum() if len(up_days) > 0 else 1
            down_vol = down_days['Volume'].sum() if len(down_days) > 0 else 1
            return up_vol / max(down_vol, 1)

        def backtest(self, df: pd.DataFrame, engine: BacktestEngine,
                     symbol: str = "NIFTY50") -> BacktestResult:
            engine.reset()
            df = df.copy()

            # VWAP
            df['TypicalPrice'] = (df['High'] + df['Low'] + df['Close']) / 3
            df['CumVolPrice'] = (df['TypicalPrice'] * df['Volume']).rolling(5).sum()
            df['CumVol'] = df['Volume'].rolling(5).sum()
            df['VWAP'] = df['CumVolPrice'] / df['CumVol']

            # ATR for target/SL
            df['TR'] = np.maximum(
                df['High'] - df['Low'],
                np.maximum(
                    abs(df['High'] - df['Close'].shift(1)),
                    abs(df['Low'] - df['Close'].shift(1))
                )
            )
            df['ATR'] = df['TR'].rolling(14).mean()

            df['EMA20'] = df['Close'].ewm(span=20).mean()

            pending_signal = None

            for i in range(max(50, self.pcr_lookback + 1), len(df)):
                row = df.iloc[i]
                date = df.index[i]
                vwap = df.iloc[i]['VWAP']
                atr = df['ATR'].iloc[i]

                if pd.isna(vwap) or pd.isna(atr) or atr == 0:
                    engine.record_equity(date)
                    pending_signal = None
                    continue

                open_price = row['Open']
                high = row['High']
                low = row['Low']
                close = row['Close']

                # === EXECUTE PENDING SIGNAL FROM PREVIOUS DAY ===
                if pending_signal is not None:
                    entry_spot = open_price
                    target_dist = atr * self.target_atr_mult
                    sl_dist = atr * self.sl_atr_mult
                    iv = estimate_iv_from_atr(atr, entry_spot)

                    if pending_signal == 'BUY_CE':
                        sl_spot = entry_spot - sl_dist
                        target_spot = entry_spot + target_dist

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
                        sl_spot = entry_spot + sl_dist
                        target_spot = entry_spot - target_dist

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
                pcr = self._compute_simulated_pcr(df, i)
                vwap_distance_pct = abs(close - vwap) / vwap * 100

                if pcr > 1.0:
                    # Bullish: price bounced off VWAP from below
                    if low <= vwap * 1.003 and close > vwap:
                        pending_signal = 'BUY_CE'
                elif pcr < 1.0:
                    # Bearish: price rejected from VWAP above
                    if high >= vwap * 0.997 and close < vwap:
                        pending_signal = 'BUY_PE'

                engine.record_equity(date)

            return engine.compute_results(self.NAME, symbol, "intraday_simulated")

except ImportError:
    # backtest_engine not available (e.g. on VPS without backtest dependencies)
    pass
