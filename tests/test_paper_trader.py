"""
Unit tests for paper_trader.py
Tests: PaperPortfolio, trade cost calculations, position management.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
import json
import tempfile


# ── Test Configuration Constants ─────────────────────────────────────────────

class TestPaperTraderConstants:
    def test_lot_sizes(self):
        from paper_trader import LOT_SIZES
        assert LOT_SIZES['NIFTY'] == 65
        assert LOT_SIZES['BANKNIFTY'] == 30
        assert LOT_SIZES['SENSEX'] == 20

    def test_capital_allocation(self):
        from paper_trader import (TOTAL_CAPITAL, EQUITY_CAPITAL,
                                   COMMODITY_CAPITAL, INITIAL_CAPITAL)
        assert TOTAL_CAPITAL == 600000
        assert EQUITY_CAPITAL == 300000
        assert COMMODITY_CAPITAL == 300000
        assert INITIAL_CAPITAL == 300000

    def test_risk_limits(self):
        from paper_trader import (MAX_RISK_PCT, MAX_DAILY_LOSS_EQUITY,
                                   MAX_POSITIONS_PER_SYMBOL, MAX_TRADES_PER_DAY)
        assert MAX_RISK_PCT == 25
        assert MAX_DAILY_LOSS_EQUITY > 0
        assert MAX_POSITIONS_PER_SYMBOL == 3
        assert MAX_TRADES_PER_DAY == 15

    def test_market_hours(self):
        from paper_trader import MARKET_OPEN, MARKET_CLOSE, LAST_ENTRY_TIME
        assert MARKET_OPEN.hour == 9
        assert MARKET_OPEN.minute == 15
        assert MARKET_CLOSE.hour == 15
        assert MARKET_CLOSE.minute == 30
        assert LAST_ENTRY_TIME.hour == 14
        assert LAST_ENTRY_TIME.minute == 30

    def test_trailing_stop_params(self):
        from paper_trader import (TSL_BREAKEVEN_TRIGGER_PCT,
                                   TSL_TRAIL_TRIGGER_PCT,
                                   TSL_TRAIL_DISTANCE_PCT)
        assert TSL_BREAKEVEN_TRIGGER_PCT == 30
        assert TSL_TRAIL_TRIGGER_PCT == 50
        assert TSL_TRAIL_DISTANCE_PCT == 25

    def test_strategy_weights_sum(self):
        from paper_trader import STRATEGY_WEIGHTS
        total = sum(STRATEGY_WEIGHTS.values())
        assert total > 0.9, f"Strategy weights sum to {total}, expected ~1.0"

    def test_premium_minimums(self):
        from paper_trader import MIN_PREMIUM_BUY, MIN_PREMIUM_SELL
        assert MIN_PREMIUM_BUY == 15
        assert MIN_PREMIUM_SELL == 20

    def test_oi_iv_thresholds(self):
        from paper_trader import (OI_SURGE_PCT, IV_SPIKE_PCT,
                                   GAMMA_SHIELD_THRESHOLD)
        assert OI_SURGE_PCT == 35
        assert IV_SPIKE_PCT == 30
        assert GAMMA_SHIELD_THRESHOLD == 0.002


# ── Test Cost Calculations ───────────────────────────────────────────────────

class TestCostCalculations:
    def test_calc_costs_basic(self):
        from paper_trader import BROKERAGE, STT_SELL, EXCHANGE_CHARGES, GST_RATE
        premium = 100
        lot_size = 25
        turnover = premium * lot_size
        brokerage = min(BROKERAGE, turnover * 0.0003)
        stt = turnover * STT_SELL
        exchange = turnover * EXCHANGE_CHARGES
        gst = (brokerage + exchange) * GST_RATE
        total = brokerage + stt + exchange + gst
        assert total > 0
        assert total < turnover

    def test_lot_tier_calculation(self):
        from paper_trader import (LOT_TIER_ELITE, LOT_TIER_STRONG,
                                   LOT_TIER_STANDARD)
        assert LOT_TIER_ELITE > LOT_TIER_STRONG > LOT_TIER_STANDARD

    def test_max_lots_per_symbol(self):
        from paper_trader import MAX_LOTS
        for symbol, max_lot in MAX_LOTS.items():
            assert max_lot >= 1
            assert max_lot <= 10


# ── Test PaperPortfolio ──────────────────────────────────────────────────────

class TestPaperPortfolio:
    @patch('paper_trader.os.path.exists', return_value=False)
    def test_init_fresh_portfolio(self, mock_exists):
        from paper_trader import PaperPortfolio
        with patch('paper_trader.PAPER_DIR', tempfile.mkdtemp()):
            p = PaperPortfolio(capital=300000)
            assert p.initial_capital == 300000
            assert p.capital == 300000
            assert p.positions == []
            assert p.closed_trades == []

    def test_position_tracking_concept(self):
        position = {
            'id': 'NIFTY_CE_22000_20240301_1',
            'symbol': 'NIFTY',
            'strike': 22000,
            'opt_type': 'CE',
            'is_sell': False,
            'entry_premium': 150,
            'lot_size': 65,
            'lots': 1,
            'entry_time': '2024-03-01 10:30:00',
            'entry_cost': 25.0,
            'sl': 105,
            'target': 300,
            'strategy': 'CPR',
        }
        assert position['symbol'] == 'NIFTY'
        assert position['lot_size'] == 65
        assert position['entry_premium'] > 0


# ── Test VIX-Adaptive Logic ──────────────────────────────────────────────────

class TestVIXAdaptive:
    def test_low_vix_increases_filters(self):
        from paper_trader import (VIX_LOW_THRESHOLD, VIX_HIGH_THRESHOLD,
                                   VIX_LOW_MULTIPLIER, VIX_HIGH_MULTIPLIER)
        vix = 12
        if vix < VIX_LOW_THRESHOLD:
            multiplier = VIX_LOW_MULTIPLIER
        elif vix > VIX_HIGH_THRESHOLD:
            multiplier = VIX_HIGH_MULTIPLIER
        else:
            multiplier = 1.0
        assert multiplier == 1.5

    def test_high_vix_relaxes_filters(self):
        from paper_trader import (VIX_LOW_THRESHOLD, VIX_HIGH_THRESHOLD,
                                   VIX_LOW_MULTIPLIER, VIX_HIGH_MULTIPLIER)
        vix = 25
        if vix < VIX_LOW_THRESHOLD:
            multiplier = VIX_LOW_MULTIPLIER
        elif vix > VIX_HIGH_THRESHOLD:
            multiplier = VIX_HIGH_MULTIPLIER
        else:
            multiplier = 1.0
        assert multiplier == 0.8


# ── Test Strategy Exit Multipliers ───────────────────────────────────────────

class TestStrategyExitMultipliers:
    def test_ghost_zone_exits_faster(self):
        from paper_trader import STRATEGY_EXIT_MULT
        ghost = STRATEGY_EXIT_MULT['Ghost Zone']
        survivor = STRATEGY_EXIT_MULT['Survivor']
        assert ghost['oi'] < survivor['oi']
        assert ghost['iv'] < survivor['iv']

    def test_survivor_tolerates_more(self):
        from paper_trader import STRATEGY_EXIT_MULT
        survivor = STRATEGY_EXIT_MULT['Survivor']
        assert survivor['oi'] == 2.0
        assert survivor['iv'] == 2.0

    def test_all_strategies_have_multipliers(self):
        from paper_trader import STRATEGY_EXIT_MULT
        expected = {'CPR', 'Gamma Blast', 'Ghost Zone', 'PCR+VWAP', 'Survivor'}
        assert set(STRATEGY_EXIT_MULT.keys()) == expected


# ── Test OI/IV Time-Based Thresholds ─────────────────────────────────────────

class TestTimeBasedThresholds:
    def test_first_hour_more_lenient(self):
        from paper_trader import (OI_SURGE_FIRST_HOUR_PCT,
                                   OI_SURGE_MID_DAY_PCT)
        assert OI_SURGE_FIRST_HOUR_PCT > OI_SURGE_MID_DAY_PCT

    def test_oi_thresholds_positive(self):
        from paper_trader import (OI_SURGE_FIRST_HOUR_PCT,
                                   OI_SURGE_MID_DAY_PCT,
                                   OI_SURGE_LAST_HOUR_PCT)
        assert OI_SURGE_FIRST_HOUR_PCT > 0
        assert OI_SURGE_MID_DAY_PCT > 0
        assert OI_SURGE_LAST_HOUR_PCT > 0


# ── Test Cooldown Parameters ─────────────────────────────────────────────────

class TestCooldowns:
    def test_grace_period(self):
        from paper_trader import GRACE_PERIOD_SECONDS
        assert GRACE_PERIOD_SECONDS == 600

    def test_ghost_zone_cooldown(self):
        from paper_trader import GHOST_ZONE_COOLDOWN_SECONDS
        assert GHOST_ZONE_COOLDOWN_SECONDS == 1800

    def test_reentry_cooldown(self):
        from paper_trader import REENTRY_COOLDOWN_SECONDS
        assert REENTRY_COOLDOWN_SECONDS == 600
