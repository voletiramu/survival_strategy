"""
Integration tests - run full backtest pipelines on sample data.
Also tests Ghost Zone V2 helper functions and volume profile.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, BacktestResult, TradeType


# ── Helper ───────────────────────────────────────────────────────────────────

def make_ohlcv(n=200, start_price=20000.0, volatility=0.01, start_date="2024-01-01",
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
# FULL PIPELINE INTEGRATION TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestFullPipeline:
    def test_survivor_full_pipeline(self):
        from strategies.survivor_strategy import SurvivorStrategy
        strat = SurvivorStrategy(pe_gap=30, ce_gap=30, reset_gap=90, max_positions=3)
        df = make_ohlcv(n=200, seed=1)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        self._validate_result(result, "NIFTY50")

    def test_wave_full_pipeline(self):
        from strategies.wave_strategy import WaveStrategy
        strat = WaveStrategy(base_gap=25, max_trades_per_day=6)
        df = make_ohlcv(n=200, seed=2)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        self._validate_result(result, "NIFTY50")

    def test_pcr_vwap_full_pipeline(self):
        from strategies.pcr_vwap_strategy import PCRVWAPStrategy
        strat = PCRVWAPStrategy(pcr_lookback=5, vwap_tolerance_pct=0.3)
        df = make_ohlcv(n=200, seed=3)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        self._validate_result(result, "NIFTY50")

    def test_ghost_zone_full_pipeline(self):
        from strategies.ghost_zone_strategy import GhostZoneStrategy
        strat = GhostZoneStrategy(zone_lookback=20, volume_threshold=1.2)
        df = make_ohlcv(n=200, seed=4)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        self._validate_result(result, "NIFTY50")

    def test_cpr_full_pipeline(self):
        from strategies.cpr_strategy import CPRStrategy
        strat = CPRStrategy(risk_per_trade_pct=1.0, max_trades_per_day=2)
        df = make_ohlcv(n=200, seed=5)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        self._validate_result(result, "NIFTY50")

    def test_gamma_blast_full_pipeline(self):
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        strat = GammaBlastStrategy(expiry_days=[2, 3])
        df = make_ohlcv(n=200, seed=6)
        engine = make_engine(lot_size=30)
        result = strat.backtest(df, engine, "BANKNIFTY")
        self._validate_result(result, "BANKNIFTY")

    def _validate_result(self, result, symbol):
        assert isinstance(result, BacktestResult)
        assert result.symbol == symbol
        assert result.total_trades >= 0
        assert result.winning_trades >= 0
        assert result.losing_trades >= 0
        assert result.winning_trades + result.losing_trades == result.total_trades
        if result.total_trades > 0:
            assert 0 <= result.win_rate <= 100
            assert result.largest_win >= result.avg_win or result.avg_win == 0
        for t in result.trades:
            assert t.status == "CLOSED"


# ══════════════════════════════════════════════════════════════════════════════
# DETERMINISM TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestDeterminism:
    def test_survivor_deterministic(self):
        from strategies.survivor_strategy import SurvivorStrategy
        strat = SurvivorStrategy()
        df = make_ohlcv(n=100, seed=42)
        r1 = strat.backtest(df, make_engine(), "NIFTY50")
        r2 = strat.backtest(df, make_engine(), "NIFTY50")
        assert r1.total_trades == r2.total_trades
        assert r1.total_pnl == pytest.approx(r2.total_pnl)

    def test_cpr_deterministic(self):
        from strategies.cpr_strategy import CPRStrategy
        strat = CPRStrategy()
        df = make_ohlcv(n=100, seed=42)
        r1 = strat.backtest(df, make_engine(), "NIFTY50")
        r2 = strat.backtest(df, make_engine(), "NIFTY50")
        assert r1.total_trades == r2.total_trades
        assert r1.total_pnl == pytest.approx(r2.total_pnl)


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-SYMBOL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiSymbol:
    def test_survivor_banknifty(self):
        from strategies.survivor_strategy import SurvivorStrategy
        strat = SurvivorStrategy()
        df = make_ohlcv(n=100, start_price=45000, seed=42)
        engine = make_engine(lot_size=30)
        result = strat.backtest(df, engine, "BANKNIFTY")
        assert result.symbol == "BANKNIFTY"

    def test_cpr_sensex(self):
        from strategies.cpr_strategy import CPRStrategy
        strat = CPRStrategy()
        df = make_ohlcv(n=100, start_price=70000, seed=42)
        engine = make_engine(lot_size=20)
        result = strat.backtest(df, engine, "SENSEX")
        assert result.symbol == "SENSEX"


# ══════════════════════════════════════════════════════════════════════════════
# GHOST ZONE V2 HELPER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestGhostZoneV2Helpers:
    def test_aggregate_to_4h(self):
        from strategies.ghost_zone_v2 import aggregate_to_4h
        dates = pd.date_range("2024-01-02 09:00", periods=24, freq="h")
        df = pd.DataFrame({
            'Open': range(100, 124),
            'High': range(101, 125),
            'Low': range(99, 123),
            'Close': range(100, 124),
            'Volume': [1000] * 24,
            'Date': dates,
        })
        result = aggregate_to_4h(df)
        assert isinstance(result, pd.DataFrame)
        assert 'Open' in result.columns
        assert len(result) <= len(df)

    def test_ghost_zone_v2_zone_detection(self):
        from strategies.ghost_zone_v2 import GhostZoneV2
        gz = GhostZoneV2()
        rng = np.random.RandomState(42)
        n = 100
        dates = pd.bdate_range("2024-01-01", periods=n, freq="B")
        closes = [20000.0]
        for _ in range(1, n):
            closes.append(closes[-1] * (1 + 0.008 * rng.randn()))
        closes = np.array(closes)
        highs = closes * (1 + np.abs(rng.randn(n) * 0.004))
        lows = closes * (1 - np.abs(rng.randn(n) * 0.004))
        opens = closes * (1 + rng.randn(n) * 0.002)
        volumes = (rng.lognormal(mean=15, sigma=0.5, size=n)).astype(int)
        highs = np.maximum(highs, np.maximum(opens, closes))
        lows = np.minimum(lows, np.minimum(opens, closes))
        df = pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                           "Close": closes, "Volume": volumes, "Date": dates}, index=dates)
        zones = gz.detect_zones(df)
        assert isinstance(zones, list)

    def test_ghost_zone_v2_should_trail(self):
        from strategies.ghost_zone_v2 import GhostZoneV2
        gz = GhostZoneV2()
        position = {'entry': 20000, 'direction': 1, 'sl': 19800, 'original_sl': 19800}
        new_sl = gz.should_trail(position, 20200, current_atr=100)
        assert new_sl is not None
        assert new_sl > 19800

    def test_ghost_zone_v2_check_exhaustion(self):
        from strategies.ghost_zone_v2 import GhostZoneV2
        gz = GhostZoneV2()
        pos = {'entry': 20000, 'direction': 1}
        assert gz.check_exhaustion(pos, 20350, 'NIFTY50') is True
        assert gz.check_exhaustion(pos, 20100, 'NIFTY50') is False


# ══════════════════════════════════════════════════════════════════════════════
# VOLUME PROFILE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestVolumeProfile:
    def test_compute_volume_profile(self):
        from strategies.ghost_zone_v3 import compute_volume_profile
        rng = np.random.RandomState(42)
        n = 50
        df = pd.DataFrame({
            'Open': 20000 + rng.randn(n) * 50,
            'High': 20050 + np.abs(rng.randn(n)) * 30,
            'Low': 19950 - np.abs(rng.randn(n)) * 30,
            'Close': 20000 + rng.randn(n) * 50,
            'Volume': rng.randint(100000, 500000, n),
        })
        result = compute_volume_profile(df, 0, 50)
        assert result is not None
        assert 'poc' in result
        assert 'va_low' in result
        assert 'va_high' in result
        assert 'shape' in result
        assert result['shape'] in ('P', 'b', 'D')
        assert result['va_low'] < result['va_high']
        assert result['poc'] >= result['va_low']
        assert result['poc'] <= result['va_high']

    def test_volume_profile_invalid_range(self):
        from strategies.ghost_zone_v3 import compute_volume_profile
        df = pd.DataFrame({'Open': [100], 'High': [101], 'Low': [99],
                          'Close': [100], 'Volume': [1000]})
        result = compute_volume_profile(df, 5, 3)
        assert result is None

    def test_volume_profile_zero_range(self):
        from strategies.ghost_zone_v3 import compute_volume_profile
        df = pd.DataFrame({
            'Open': [100]*5, 'High': [100]*5, 'Low': [100]*5,
            'Close': [100]*5, 'Volume': [1000]*5,
        })
        result = compute_volume_profile(df, 0, 5)
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# EDGE CASE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_strategy_with_minimal_data(self):
        from strategies.survivor_strategy import SurvivorStrategy
        strat = SurvivorStrategy()
        df = make_ohlcv(n=5, seed=42)
        engine = make_engine()
        result = strat.backtest(df, engine, "NIFTY50")
        assert result.total_trades >= 0

    def test_engine_with_zero_trades(self):
        engine = make_engine()
        result = engine.compute_results("Empty", "NIFTY50")
        assert result.total_trades == 0
        assert result.sharpe_ratio == 0
        assert result.sortino_ratio == 0

    def test_all_strategies_import(self):
        from strategies.survivor_strategy import SurvivorStrategy
        from strategies.wave_strategy import WaveStrategy
        from strategies.pcr_vwap_strategy import PCRVWAPStrategy
        from strategies.ghost_zone_strategy import GhostZoneStrategy
        from strategies.cpr_strategy import CPRStrategy
        from strategies.gamma_blast_strategy import GammaBlastStrategy
        from strategies.ghost_zone_v2 import GhostZoneV2
        from strategies.ghost_zone_v3 import GhostZoneV3
        from strategies.pcr_vwap_v2 import PCRVWAPV2Strategy
        from strategies.survivor_v2_oi_gamma import SurvivorV2Strategy
        assert True

    def test_v2_strategies_pipeline(self):
        from strategies.pcr_vwap_v2 import PCRVWAPV2Strategy
        strat = PCRVWAPV2Strategy()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine(lot_size=65)
        result = strat.backtest(df, engine, "NIFTY50")
        assert isinstance(result, BacktestResult)

    def test_survivor_v2_pipeline(self):
        from strategies.survivor_v2_oi_gamma import SurvivorV2Strategy
        strat = SurvivorV2Strategy()
        df = make_ohlcv(n=200, seed=42)
        engine = make_engine(lot_size=30)
        result = strat.backtest(df, engine, "BANKNIFTY")
        assert isinstance(result, BacktestResult)
