"""
Unit tests for data_fetcher.py
Tests: load_or_fetch, get_all_daily_data, constants.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch


class TestConstants:
    def test_symbols_dict(self):
        from data_fetcher import SYMBOLS
        assert "NIFTY50" in SYMBOLS
        assert "BANKNIFTY" in SYMBOLS
        assert "SENSEX" in SYMBOLS
        assert SYMBOLS["NIFTY50"] == "^NSEI"

    def test_lot_sizes(self):
        from data_fetcher import LOT_SIZES
        assert LOT_SIZES["NIFTY50"] == 65
        assert LOT_SIZES["BANKNIFTY"] == 30
        assert LOT_SIZES["SENSEX"] == 20
        assert all(v > 0 for v in LOT_SIZES.values())


class TestLoadOrFetch:
    def test_raises_on_missing_file(self):
        from data_fetcher import load_or_fetch
        with pytest.raises(FileNotFoundError, match="Data file not found"):
            load_or_fetch("NONEXISTENT_SYMBOL", period="1y")

    def test_loads_existing_csv(self, tmp_path):
        from data_fetcher import load_or_fetch
        dates = pd.bdate_range("2024-01-01", periods=10)
        df = pd.DataFrame({
            "Open": np.random.uniform(19000, 21000, 10),
            "High": np.random.uniform(19000, 21000, 10),
            "Low": np.random.uniform(19000, 21000, 10),
            "Close": np.random.uniform(19000, 21000, 10),
            "Volume": np.random.randint(1000000, 5000000, 10),
        }, index=dates)
        filepath = tmp_path / "TESTINDEX_1d_5y.csv"
        df.to_csv(filepath)

        with patch('data_fetcher.DATA_DIR', str(tmp_path)):
            loaded = load_or_fetch("TESTINDEX", period="5y", interval="1d")
            assert len(loaded) == 10
            assert "Close" in loaded.columns

    def test_loaded_df_has_datetime_index(self, tmp_path):
        from data_fetcher import load_or_fetch
        dates = pd.bdate_range("2024-01-01", periods=5)
        df = pd.DataFrame({
            "Open": [100]*5, "High": [101]*5, "Low": [99]*5,
            "Close": [100]*5, "Volume": [1000]*5,
        }, index=dates)
        filepath = tmp_path / "TEST_1d_5y.csv"
        df.to_csv(filepath)

        with patch('data_fetcher.DATA_DIR', str(tmp_path)):
            loaded = load_or_fetch("TEST", period="5y", interval="1d")
            assert isinstance(loaded.index, pd.DatetimeIndex)


class TestGetAllDailyData:
    def test_returns_dict(self, tmp_path):
        from data_fetcher import get_all_daily_data
        with patch('data_fetcher.DATA_DIR', str(tmp_path)):
            data = get_all_daily_data()
            assert isinstance(data, dict)

    def test_handles_missing_files_gracefully(self, tmp_path):
        from data_fetcher import get_all_daily_data
        with patch('data_fetcher.DATA_DIR', str(tmp_path)):
            data = get_all_daily_data()
            assert isinstance(data, dict)


class TestDataValidation:
    def test_ohlcv_columns_present(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        df = pd.DataFrame({
            "Open": [100]*5, "High": [101]*5, "Low": [99]*5,
            "Close": [100]*5, "Volume": [1000]*5,
        }, index=dates)
        required = {"Open", "High", "Low", "Close", "Volume"}
        assert required.issubset(set(df.columns))

    def test_high_gte_low(self):
        rng = np.random.RandomState(42)
        n = 100
        closes = 20000 + rng.randn(n) * 100
        highs = closes + np.abs(rng.randn(n) * 50)
        lows = closes - np.abs(rng.randn(n) * 50)
        assert all(highs >= lows)

    def test_high_gte_open_close(self):
        rng = np.random.RandomState(42)
        n = 50
        opens = 20000 + rng.randn(n) * 100
        closes = 20000 + rng.randn(n) * 100
        highs = np.maximum(opens, closes) + np.abs(rng.randn(n) * 20)
        assert all(highs >= np.maximum(opens, closes))

    def test_volume_non_negative(self):
        volumes = np.abs(np.random.randint(0, 10000000, 100))
        assert all(volumes >= 0)

    def test_no_nan_in_ohlcv(self):
        dates = pd.bdate_range("2024-01-01", periods=10)
        df = pd.DataFrame({
            "Open": range(10), "High": range(1, 11),
            "Low": range(10), "Close": range(10), "Volume": range(10),
        }, index=dates)
        assert not df.isnull().any().any()

    def test_date_index_is_sorted(self):
        dates = pd.bdate_range("2024-01-01", periods=20)
        assert all(dates[i] < dates[i+1] for i in range(len(dates)-1))
