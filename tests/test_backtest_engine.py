"""
Unit tests for backtest_engine.py
Tests: BacktestEngine, Trade, TradeType, BacktestResult
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from backtest_engine import BacktestEngine, Trade, TradeType, BacktestResult


# ── Trade & TradeType Tests ─────────────────────────────────────────────────

class TestTradeType:
    def test_trade_types_exist(self):
        assert TradeType.BUY_CE.value == "BUY_CE"
        assert TradeType.BUY_PE.value == "BUY_PE"
        assert TradeType.SELL_CE.value == "SELL_CE"
        assert TradeType.SELL_PE.value == "SELL_PE"

    def test_trade_type_enum_count(self):
        assert len(TradeType) == 4


class TestTrade:
    def test_trade_defaults(self):
        t = Trade(entry_date=pd.Timestamp("2024-01-01"), exit_date=None,
                  trade_type=TradeType.BUY_CE, entry_price=20000.0)
        assert t.exit_price == 0.0
        assert t.pnl == 0.0
        assert t.status == "OPEN"
        assert t.quantity == 1

    def test_trade_with_pnl(self):
        t = Trade(entry_date=pd.Timestamp("2024-01-01"),
                  exit_date=pd.Timestamp("2024-01-02"),
                  trade_type=TradeType.SELL_PE,
                  entry_price=20000.0, exit_price=20100.0,
                  pnl=1500.0, status="CLOSED")
        assert t.pnl == 1500.0
        assert t.status == "CLOSED"


# ── BacktestEngine Initialization Tests ──────────────────────────────────────

class TestBacktestEngineInit:
    def test_default_init(self):
        e = BacktestEngine()
        assert e.initial_capital == 300000.0
        assert e.lot_size == 25
        assert e.capital == 300000.0
        assert e.trades == []
        assert e.equity_history == []

    def test_custom_init(self):
        e = BacktestEngine(initial_capital=500000, lot_size=65,
                           brokerage_per_order=40.0, slippage_points=5.0)
        assert e.initial_capital == 500000
        assert e.lot_size == 65
        assert e.brokerage == 40.0
        assert e.slippage == 5.0

    def test_max_risk_calculation(self):
        e = BacktestEngine(initial_capital=300000, max_risk_per_trade_pct=2.0)
        assert e.max_risk_per_trade == 6000.0

    def test_reset(self):
        e = BacktestEngine()
        e.capital = 350000
        e.trades.append(Trade(entry_date=pd.Timestamp.now(), exit_date=None,
                              trade_type=TradeType.BUY_CE, entry_price=20000))
        e.equity_history.append(350000)
        e.reset()
        assert e.capital == 300000.0
        assert e.trades == []
        assert e.equity_history == []


# ── Option Premium Estimation Tests ──────────────────────────────────────────

class TestOptionPremiumEstimation:
    def test_atm_call_has_time_value(self):
        e = BacktestEngine()
        premium = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=30)
        assert premium > 0
        assert premium > 50

    def test_atm_put_has_time_value(self):
        e = BacktestEngine()
        premium = e.estimate_option_premium(spot=20000, strike=20000, is_call=False, dte=30)
        assert premium > 0

    def test_deep_itm_call(self):
        e = BacktestEngine()
        premium = e.estimate_option_premium(spot=20500, strike=20000, is_call=True, dte=30)
        assert premium >= 500

    def test_deep_otm_call_minimum_premium(self):
        e = BacktestEngine()
        premium = e.estimate_option_premium(spot=20000, strike=22000, is_call=True, dte=1)
        assert premium >= 1.0

    def test_zero_dte_gets_set_to_one(self):
        e = BacktestEngine()
        premium = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=0)
        assert premium >= 1.0

    def test_higher_iv_gives_higher_premium(self):
        e = BacktestEngine()
        low_iv = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=30, iv_pct=10)
        high_iv = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=30, iv_pct=30)
        assert high_iv > low_iv

    def test_longer_dte_gives_higher_premium(self):
        e = BacktestEngine()
        short_dte = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=5)
        long_dte = e.estimate_option_premium(spot=20000, strike=20000, is_call=True, dte=60)
        assert long_dte > short_dte


# ── Option PnL Simulation Tests ──────────────────────────────────────────────

class TestSimulateOptionPnl:
    def test_buy_ce_profit_on_up_move(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 20200, TradeType.BUY_CE, lots=1)
        # raw = 200 * 0.5 * 25 = 2500, BUY profit *= 0.75 → 1875
        # slippage_mult = 1 + min(200/20000*10, 1) = 1.1
        # costs = 20*2 + 2*25*1.1 = 40 + 55 = 95
        expected = 200 * 0.5 * 25 * 0.75 - (20 * 2 + 2 * 25 * 1.1)
        assert pnl == pytest.approx(expected)

    def test_buy_ce_loss_on_down_move(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 19800, TradeType.BUY_CE, lots=1)
        assert pnl < 0

    def test_buy_pe_profit_on_down_move(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 19800, TradeType.BUY_PE, lots=1)
        # raw = 200 * 0.5 * 25 = 2500, BUY profit *= 0.75 → 1875
        # slippage_mult = 1.1, costs = 40 + 55 = 95
        expected = 200 * 0.5 * 25 * 0.75 - (20 * 2 + 2 * 25 * 1.1)
        assert pnl == pytest.approx(expected)

    def test_sell_ce_profit_on_down_move(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 19800, TradeType.SELL_CE, lots=1)
        # raw = 200 * 0.5 * 25 = 2500 (no 0.75 for SELL, capped at premium*qty=7500)
        # slippage_mult = 1.1, costs = 40 + 55 = 95
        expected = 200 * 0.5 * 25 - (20 * 2 + 2 * 25 * 1.1)
        assert pnl == pytest.approx(expected)

    def test_sell_pe_profit_on_up_move(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 20200, TradeType.SELL_PE, lots=1)
        # raw = 200 * 0.5 * 25 = 2500 (no 0.75 for SELL, capped at premium*qty=7500)
        # slippage_mult = 1.1, costs = 40 + 55 = 95
        expected = 200 * 0.5 * 25 - (20 * 2 + 2 * 25 * 1.1)
        assert pnl == pytest.approx(expected)

    def test_flat_move_only_costs(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl = e.simulate_option_pnl_from_spot(20000, 20000, TradeType.BUY_CE, lots=1)
        # pnl = 0 (no profit, no 0.75 multiplier), slippage_mult = 1.0
        costs = 20 * 2 + 2 * 25 * 1.0
        assert pnl == pytest.approx(-costs)

    def test_multiple_lots(self):
        e = BacktestEngine(lot_size=25, brokerage_per_order=20, slippage_points=2)
        pnl_1 = e.simulate_option_pnl_from_spot(20000, 20200, TradeType.BUY_CE, lots=1)
        pnl_3 = e.simulate_option_pnl_from_spot(20000, 20200, TradeType.BUY_CE, lots=3)
        # lots=1: raw=2500*0.75=1875, slippage_mult=1.1, costs=40+55=95 → 1780
        expected_1 = 200 * 0.5 * 25 * 0.75 - (20 * 2 + 2 * 25 * 1.1)
        # lots=3: raw=7500*0.75=5625, slippage_mult=1.1, costs=40+165=205 → 5420
        expected_3 = 200 * 0.5 * 75 * 0.75 - (20 * 2 + 2 * 75 * 1.1)
        assert pnl_1 == pytest.approx(expected_1)
        assert pnl_3 == pytest.approx(expected_3)


# ── Trade Recording & Capital Tests ──────────────────────────────────────────

class TestTradeManagement:
    def test_add_trade_updates_capital(self):
        e = BacktestEngine(initial_capital=300000)
        t = Trade(entry_date=pd.Timestamp("2024-01-01"), exit_date=pd.Timestamp("2024-01-01"),
                  trade_type=TradeType.BUY_CE, entry_price=20000, pnl=5000, status="CLOSED")
        e.add_trade(t)
        assert e.capital == 305000
        assert len(e.trades) == 1

    def test_add_losing_trade(self):
        e = BacktestEngine(initial_capital=300000)
        t = Trade(entry_date=pd.Timestamp("2024-01-01"), exit_date=pd.Timestamp("2024-01-01"),
                  trade_type=TradeType.BUY_CE, entry_price=20000, pnl=-3000, status="CLOSED")
        e.add_trade(t)
        assert e.capital == 297000

    def test_record_equity(self):
        e = BacktestEngine(initial_capital=300000)
        date = pd.Timestamp("2024-01-01")
        e.record_equity(date)
        assert len(e.equity_history) == 1
        assert e.equity_history[0] == 300000
        assert e.date_history[0] == date


# ── Compute Results Tests ────────────────────────────────────────────────────

class TestComputeResults:
    def test_no_trades_returns_empty_result(self):
        e = BacktestEngine()
        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.total_trades == 0
        assert result.total_pnl == 0
        assert result.win_rate == 0

    def test_all_winners(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=5)
        for i, d in enumerate(dates):
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=1000, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.total_trades == 5
        assert result.winning_trades == 5
        assert result.losing_trades == 0
        assert result.win_rate == 100.0
        assert result.total_pnl == 5000
        assert result.profit_factor == float('inf')

    def test_all_losers(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=3)
        for d in dates:
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=-500, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.total_trades == 3
        assert result.winning_trades == 0
        assert result.losing_trades == 3
        assert result.win_rate == 0.0
        assert result.avg_win == 0
        assert result.largest_win == 0

    def test_mixed_trades_stats(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=4)
        pnls = [2000, -500, 1500, -200]
        for d, p in zip(dates, pnls):
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=p, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.total_trades == 4
        assert result.winning_trades == 2
        assert result.losing_trades == 2
        assert result.win_rate == 50.0
        assert result.total_pnl == 2800
        assert result.avg_win == pytest.approx(1750.0)
        assert result.avg_loss == pytest.approx(-350.0)
        assert result.largest_win == 2000
        assert result.largest_loss == -500
        assert result.profit_factor == pytest.approx(3500 / 700)

    def test_drawdown_calculation(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=5)
        pnls = [5000, 5000, -8000, -3000, 2000]
        for d, p in zip(dates, pnls):
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=p, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.max_drawdown < 0

    def test_trade_duration(self):
        e = BacktestEngine(initial_capital=300000)
        t1 = Trade(entry_date=pd.Timestamp("2024-01-01"),
                   exit_date=pd.Timestamp("2024-01-03"),
                   trade_type=TradeType.BUY_CE, entry_price=20000, pnl=100, status="CLOSED")
        t2 = Trade(entry_date=pd.Timestamp("2024-01-05"),
                   exit_date=pd.Timestamp("2024-01-06"),
                   trade_type=TradeType.BUY_PE, entry_price=20000, pnl=200, status="CLOSED")
        e.add_trade(t1)
        e.add_trade(t2)
        dates = pd.bdate_range("2024-01-01", periods=6)
        for d in dates:
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.avg_trade_duration == pytest.approx(1.5)

    def test_result_strategy_name_and_symbol(self):
        e = BacktestEngine()
        result = e.compute_results("MyStrategy", "BANKNIFTY", "daily")
        assert result.strategy_name == "MyStrategy"
        assert result.symbol == "BANKNIFTY"
        assert result.timeframe == "daily"

    def test_sharpe_and_sortino_positive_for_net_winners(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=60)
        rng = np.random.RandomState(42)
        for d in dates:
            pnl = 300 + rng.randn() * 500  # mix of wins and losses, net positive
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=pnl, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        assert result.sharpe_ratio > 0
        # Sortino needs downside returns to be non-zero; with mixed PnLs it should be
        if result.sortino_ratio != 0:
            assert result.sortino_ratio > 0

    def test_calmar_ratio_calculation(self):
        e = BacktestEngine(initial_capital=300000)
        dates = pd.bdate_range("2024-01-01", periods=60)
        for i, d in enumerate(dates):
            pnl = 300 if i % 3 != 0 else -600
            t = Trade(entry_date=d, exit_date=d, trade_type=TradeType.BUY_CE,
                      entry_price=20000, pnl=pnl, status="CLOSED")
            e.add_trade(t)
            e.record_equity(d)

        result = e.compute_results("TestStrat", "NIFTY50")
        if result.max_drawdown_pct != 0:
            expected_calmar = result.annualized_return_pct / abs(result.max_drawdown_pct)
            assert result.calmar_ratio == pytest.approx(expected_calmar, rel=0.01)
