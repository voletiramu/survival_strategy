"""
Unit tests for all trading strategy signal generation.
Tests each strategy's backtest method and signal logic.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


# ── Helper to create OHLCV data ─────────────────────────────────────────────

def make_ohlcv(n=100, start_price=20000.0, volatility=0.01, start_date="2024-01-01",
               trend=0.0, seed=42):
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range(start=start_date, periods=n, freq="B")
    closes = [start_price]
    for _ in range(1, n):
        closes.append(closes[-1] * (1 + trend + volatility * rng.randn()))
    closes = np.array(closes)
    highs = closes * (1 + np.abs(rng.randn(n) * volatility * 0.5))
    lows = closes * (1 - np.abs(rng.randn(n) * volatility * 0.5))
    opens = closes * (1 + rng.randn(n) * volatility * 0.3)
    volumes = (rng.lognormal(mean=15, sigma=0.5, size=n)).astype(int)
    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": volumes}, index=dates)


def make_engine(lot_size=25):
    return BacktestEngine(initial_capital=300000, lot_size=lot_size,
                          brokerage_per_order=20, slippage_points=2)


# ══════════════════════════════════════════════════════════════════════════════
# SURVIVOR STRATEGY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSurvivorStrategy:
    def _get_strat(self):
        from strategies.survivor_strategy import SurvivorStrategy
        return SurvivorStrategy(pe_gap=30, ce_gap=30, reset_gap=90, max_positions=3)

    def test_init_params(self):
        s = self._get_strat()
        assert s.pe_gap == 30
        assert s.ce_gap == 30
        assert s.reset_gap == 90
        assert s.max_positions == 3

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "Survivor (Raahi Bhushan)"

    def test_uptrend_generates_pe_sells(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, trend=0.005, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        pe_trades = [t for t in result.trades if t.trade_type == TradeType.SELL_PE]
        assert len(pe_trades) > 0, "Uptrend should generate PE sells"

    def test_downtrend_generates_ce_sells(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, trend=-0.005, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        ce_trades = [t for t in result.trades if t.trade_type == TradeType.SELL_CE]
        assert len(ce_trades) > 0, "Downtrend should generate CE sells"

    def test_only_trades_mon_to_wed(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        for t in result.trades:
            assert t.entry_date.weekday() <= 2, f"Traded on {t.entry_date.strftime('%A')}"

    def test_max_positions_respected(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, trend=0.01, volatility=0.02, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        from collections import defaultdict
        daily_counts = defaultdict(lambda: defaultdict(int))
        for t in result.trades:
            daily_counts[t.entry_date.date()][t.trade_type] += 1
        for date, counts in daily_counts.items():
            for tt, count in counts.items():
                assert count <= s.max_positions + 1, (
                    f"Day {date} had {count} {tt.value} trades (max={s.max_positions})")

    def test_reset_engine_between_runs(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        engine = make_engine()
        r1 = s.backtest(df, engine, "NIFTY50")
        r2 = s.backtest(df, engine, "NIFTY50")
        assert r1.total_trades == r2.total_trades, "Consecutive runs should give same results"


# ══════════════════════════════════════════════════════════════════════════════
# WAVE STRATEGY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestWaveStrategy:
    def _get_strat(self):
        from strategies.wave_strategy import WaveStrategy
        return WaveStrategy(base_gap=25, max_trades_per_day=6)

    def test_init_params(self):
        s = self._get_strat()
        assert s.base_gap == 25
        assert s.max_trades_per_day == 6

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)

    def test_ranging_market_produces_trades(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, volatility=0.005, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert result.total_trades > 0

    def test_both_ce_and_pe_sells_generated(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        ce = [t for t in result.trades if t.trade_type == TradeType.SELL_CE]
        pe = [t for t in result.trades if t.trade_type == TradeType.SELL_PE]
        assert len(ce) > 0 or len(pe) > 0, "Wave should produce at least some trades"

    def test_max_trades_per_day_respected(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, volatility=0.015, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        from collections import Counter
        daily = Counter(t.entry_date.date() for t in result.trades)
        for date, count in daily.items():
            assert count <= s.max_trades_per_day + 1, f"Day {date} had {count} trades"


# ══════════════════════════════════════════════════════════════════════════════
# PCR+VWAP STRATEGY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestPCRVWAPStrategy:
    def _get_strat(self):
        from strategies.pcr_vwap_strategy import PCRVWAPStrategy
        return PCRVWAPStrategy(pcr_lookback=5, vwap_tolerance_pct=0.3)

    def test_init_params(self):
        s = self._get_strat()
        assert s.pcr_lookback == 5
        assert s.vwap_tolerance_pct == 0.3

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)

    def test_compute_simulated_pcr_returns_float(self):
        s = self._get_strat()
        df = make_ohlcv(n=20, seed=42)
        pcr = s._compute_simulated_pcr(df, 10)
        assert isinstance(pcr, (int, float))
        assert pcr > 0

    def test_pcr_early_index_returns_one(self):
        s = self._get_strat()
        df = make_ohlcv(n=10, seed=42)
        pcr = s._compute_simulated_pcr(df, 2)
        assert pcr == 1.0

    def test_compute_daily_vwap(self):
        s = self._get_strat()
        row = pd.Series({"High": 20100, "Low": 19900, "Close": 20050})
        vwap = s._compute_daily_vwap(row)
        expected = (20100 + 19900 + 20050) / 3
        assert vwap == pytest.approx(expected)

    def test_buys_only_ce_or_pe(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        for t in result.trades:
            assert t.trade_type in (TradeType.BUY_CE, TradeType.BUY_PE)


# ══════════════════════════════════════════════════════════════════════════════
# GHOST ZONE STRATEGY TESTS (V1)
# ══════════════════════════════════════════════════════════════════════════════

class TestGhostZoneStrategy:
    def _get_strat(self):
        from strategies.ghost_zone_strategy import GhostZoneStrategy
        return GhostZoneStrategy(zone_lookback=20, volume_threshold=1.2,
                                 min_impulse_atr_mult=0.8, target_rr=2.0)

    def test_init_params(self):
        s = self._get_strat()
        assert s.zone_lookback == 20
        assert s.target_rr == 2.0

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)

    def test_find_zones_returns_lists(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(abs(df['High'] - df['Close'].shift(1)),
                       abs(df['Low'] - df['Close'].shift(1))))
        df['ATR'] = df['TR'].rolling(14).mean()
        atr = df['ATR'].iloc[50]
        demand, supply = s._find_zones(df, 50, atr)
        assert isinstance(demand, list)
        assert isinstance(supply, list)

    def test_only_buys_ce_or_pe(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        for t in result.trades:
            assert t.trade_type in (TradeType.BUY_CE, TradeType.BUY_PE)


# ══════════════════════════════════════════════════════════════════════════════
# CPR STRATEGY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCPRStrategy:
    def _get_strat(self):
        from strategies.cpr_strategy import CPRStrategy
        return CPRStrategy(risk_per_trade_pct=1.0, max_trades_per_day=2,
                           narrow_cpr_threshold_pct=0.3, use_camarilla=True)

    def test_init_params(self):
        s = self._get_strat()
        assert s.risk_per_trade_pct == 1.0
        assert s.use_camarilla is True

    def test_compute_cpr_values(self):
        s = self._get_strat()
        cpr = s._compute_cpr(high=20200, low=19800, close=20000)
        assert 'pivot' in cpr
        assert 'tc' in cpr
        assert 'bc' in cpr
        assert 'r1' in cpr
        assert 's1' in cpr
        expected_pivot = (20200 + 19800 + 20000) / 3
        assert cpr['pivot'] == pytest.approx(expected_pivot)

    def test_cpr_ordering(self):
        s = self._get_strat()
        cpr = s._compute_cpr(high=20200, low=19800, close=20000)
        assert cpr['s3'] < cpr['s2'] < cpr['s1'] < cpr['pivot']
        assert cpr['pivot'] < cpr['r1'] < cpr['r2'] < cpr['r3']

    def test_compute_camarilla(self):
        s = self._get_strat()
        cam = s._compute_camarilla(high=20200, low=19800, close=20000)
        assert 'cam_r3' in cam
        assert 'cam_s3' in cam
        assert cam['cam_r3'] > cam['cam_s3']
        assert cam['cam_r4'] > cam['cam_r3']
        assert cam['cam_s4'] < cam['cam_s3']

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=100, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)

    def test_narrow_cpr_width(self):
        s = self._get_strat()
        cpr = s._compute_cpr(high=20010, low=19990, close=20000)
        assert cpr['cpr_width_pct'] < 0.3, "Tight day should have narrow CPR"

    def test_wide_cpr_width(self):
        s = self._get_strat()
        # Close != (H+L)/2 to ensure TC != BC (CPR width > 0)
        cpr = s._compute_cpr(high=20500, low=19500, close=20200)
        assert cpr['cpr_width_pct'] > 0.1, "Wide range with asymmetric close should have non-zero CPR"


# ══════════════════════════════════════════════════════════════════════════════
# GAMMA BLAST STRATEGY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestGammaBlastStrategy:
    def _get_strat(self):
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        return GammaBlastStrategy(initial_delta=0.45,
                                  gamma_per_point=0.0008)

    def test_init_default_all_days(self):
        s = self._get_strat()
        assert s.initial_delta == 0.45
        assert s.gamma_per_point == 0.0008
        assert s.expiry_days == [0, 1, 2, 3, 4]  # All weekdays

    def test_init_custom_expiry_days(self):
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        s = GammaBlastStrategy(expiry_days=[2, 3])
        assert s.expiry_days == [2, 3]

    def test_backtest_runs_without_error(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "BANKNIFTY")
        assert isinstance(result, BacktestResult)

    def test_only_trades_on_configured_days(self):
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        s = GammaBlastStrategy(expiry_days=[2, 3])
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "BANKNIFTY")
        for t in result.trades:
            assert t.entry_date.weekday() in s.expiry_days, \
                f"Traded on {t.entry_date.strftime('%A')} (weekday={t.entry_date.weekday()})"

    def test_all_days_produces_more_trades(self):
        """All-days mode should produce more trades than expiry-only."""
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        df = make_ohlcv(n=200, seed=42)
        # Expiry-only
        s_exp = GammaBlastStrategy(expiry_days=[2, 3])
        e1 = make_engine()
        r_exp = s_exp.backtest(df, e1, "NIFTY50")
        # All days
        s_all = GammaBlastStrategy()
        e2 = make_engine()
        r_all = s_all.backtest(df, e2, "NIFTY50")
        assert r_all.total_trades >= r_exp.total_trades

    def test_dte_gamma_multiplier(self):
        """Test DTE-aware gamma scaling returns correct multipliers."""
        s = self._get_strat()
        assert s._get_gamma_multiplier(2) == 2.0   # Wed (expiry)
        assert s._get_gamma_multiplier(3) == 2.0   # Thu (expiry)
        assert s._get_gamma_multiplier(1) == 1.5   # Tue (day before)
        assert s._get_gamma_multiplier(0) == 1.0   # Mon (base)
        assert s._get_gamma_multiplier(4) == 1.0   # Fri (base)

    def test_only_buys(self):
        s = self._get_strat()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine()
        result = s.backtest(df, engine, "BANKNIFTY")
        for t in result.trades:
            assert t.trade_type in (TradeType.BUY_CE, TradeType.BUY_PE)

    def test_name_updated(self):
        s = self._get_strat()
        assert s.NAME == "Gamma Blast (All Days)"


# ══════════════════════════════════════════════════════════════════════════════
# GHOST ZONE V3 TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestGhostZoneV3:
    def _get_strat(self):
        from strategies.ghost_zone_v3 import GhostZoneV3
        return GhostZoneV3()

    def _make_zone_df(self, n=200, seed=42):
        rng = np.random.RandomState(seed)
        dates = pd.bdate_range("2024-01-01", periods=n, freq="B")
        closes = [20000.0]
        for _ in range(1, n):
            closes.append(closes[-1] * (1 + 0.01 * rng.randn()))
        closes = np.array(closes)
        highs = closes * (1 + np.abs(rng.randn(n) * 0.005))
        lows = closes * (1 - np.abs(rng.randn(n) * 0.005))
        opens = closes * (1 + rng.randn(n) * 0.003)
        volumes = (rng.lognormal(mean=15, sigma=0.5, size=n)).astype(int)
        highs = np.maximum(highs, np.maximum(opens, closes))
        lows = np.minimum(lows, np.minimum(opens, closes))
        for i in range(40, n, 40):
            volumes[i] = volumes[i] * 8
            lows[i] = lows[i] - (highs[i] - lows[i]) * 1.5
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": volumes, "Date": dates}, index=dates)

    def test_ema_helper(self):
        s = self._get_strat()
        series = pd.Series([1, 2, 3, 4, 5], dtype=float)
        result = s.ema(series, 3)
        assert len(result) == 5
        assert result.iloc[-1] > result.iloc[0]

    def test_atr_helper(self):
        s = self._get_strat()
        df = self._make_zone_df(n=50)
        atr = s.atr(df, 14)
        assert len(atr) == 50
        assert atr.iloc[-1] > 0

    def test_rsi_helper(self):
        s = self._get_strat()
        series = pd.Series(range(1, 31), dtype=float)
        rsi = s.rsi(series, 14)
        assert len(rsi) == 30
        assert rsi.iloc[-1] > 70

    def test_detect_zones_returns_list(self):
        s = self._get_strat()
        df = self._make_zone_df(n=200)
        zones = s.detect_zones(df)
        assert isinstance(zones, list)

    def test_zone_has_required_fields(self):
        s = self._get_strat()
        df = self._make_zone_df(n=200)
        zones = s.detect_zones(df)
        if zones:
            z = zones[0]
            assert hasattr(z, 'zone_type')
            assert hasattr(z, 'low')
            assert hasattr(z, 'high')
            assert hasattr(z, 'mid')
            assert hasattr(z, 'quality')
            assert z.zone_type in ('demand', 'supply')
            assert z.low < z.high

    def test_score_zone_range(self):
        s = self._get_strat()
        score = s._score_zone(vol_ratio=5.0, candle_range=300, atr=100,
                              trigger_type='injection', existing_zones=[],
                              zone_type='demand', zone_low=19900, zone_high=20100)
        assert 1 <= score <= 5

    def test_update_zones_removes_broken(self):
        s = self._get_strat()
        df = self._make_zone_df(n=200)
        zones = s.detect_zones(df)
        if zones:
            active = s.update_zones(zones, df, len(df) - 1)
            assert isinstance(active, list)
            for z in active:
                assert not z.broken

    def test_should_trail_long_activates(self):
        s = self._get_strat()
        position = {'entry': 20000, 'direction': 1, 'sl': 19800, 'original_sl': 19800}
        new_sl = s.should_trail(position, current_price=20200, current_atr=100)
        assert new_sl is not None
        assert new_sl > 19800

    def test_should_trail_returns_none_when_not_triggered(self):
        s = self._get_strat()
        position = {'entry': 20000, 'direction': 1, 'sl': 19800, 'original_sl': 19800}
        new_sl = s.should_trail(position, current_price=20050, current_atr=100)
        assert new_sl is None

    def test_check_exhaustion_nifty(self):
        s = self._get_strat()
        pos = {'entry': 20000, 'direction': 1}
        assert s.check_exhaustion(pos, 20350, 'NIFTY50') is True
        assert s.check_exhaustion(pos, 20100, 'NIFTY50') is False

    def test_check_exhaustion_banknifty(self):
        s = self._get_strat()
        pos = {'entry': 45000, 'direction': 1}
        assert s.check_exhaustion(pos, 46100, 'BANKNIFTY') is True
        assert s.check_exhaustion(pos, 45500, 'BANKNIFTY') is False

    def test_check_exhaustion_crypto(self):
        s = self._get_strat()
        pos = {'entry': 50000, 'direction': 1}
        assert s.check_exhaustion(pos, 51600, 'BTCUSD') is True
        assert s.check_exhaustion(pos, 50500, 'BTCUSD') is False
