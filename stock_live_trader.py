"""
Stock Live Trading Bot v25 — Individual Stock Options (NSE F&O)
===============================================================
Standalone live/shadow bot for individual stock F&O options.
Tier 1+2 stocks: SBIN, RELIANCE, HDFCBANK, INFY, CIPLA, etc.

Architecture:
  Signal generation : Standalone (CPR + Wave + Gamma Blast)
  Data source       : Dhan (primary) + Zerodha (fallback)
  Order execution   : dhan_broker.py (LIMIT orders only — SEBI Apr 2026)
  Risk management   : Built-in (daily loss cap, position limits)
  Position tracking : StockLivePosition (internal)
  OI Exit signals   : oi_heatmap.py (5-factor confidence scoring)
  V-Reversal detect : reversal_detector.py (smart DIRECTION_FLIP re-entry)

Safety filters (v25):
  Physics Gate      : Blocks dying momentum, wave peaks, VWAP stretch, RSI extreme
  DCI Gate          : Direction Confidence Index >= 40 required (6-factor scoring)
  Choppy Shield     : Blocks entries after 11:00 on choppy days (eff < 15%)
  Regime Detector   : Adaptive TSL (TRENDING=wide trail, FLAT=tight trail)

Modes:
  --shadow    : Generate signals + log orders, don't place them (DEFAULT)
  --live      : Place real orders (requires explicit confirmation)

Usage:
    python stock_live_trader.py                  # Shadow mode (safe)
    python stock_live_trader.py --shadow         # Same as above
    python stock_live_trader.py --live           # REAL MONEY — requires confirmation
    python stock_live_trader.py --status         # Show risk status and positions
"""

import sys
import os
import json
import time
import signal
import logging
import argparse
import threading
from datetime import datetime, timedelta, date, time as dtime
from collections import defaultdict
from pathlib import Path

# ── Logging Setup ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
STOCK_LIVE_DIR = os.path.join(BASE_DIR, 'stock_live_trades')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STOCK_LIVE_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"stock_live_{datetime.now().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# ── Imports from existing codebase ────────────────────────────────────────────
from dhan_broker import DhanBroker, EXCHANGE_SEGMENT_MAP
from dhan_feed import DhanFeed

try:
    from stock_fno_config import StockFnOConfig, TIER1_STOCKS, TIER2_STOCKS
except ImportError:
    StockFnOConfig = None
    TIER1_STOCKS = {}
    TIER2_STOCKS = {}

try:
    from oi_heatmap import OIHeatmap
except ImportError:
    OIHeatmap = None

try:
    from reversal_detector import ReversalDetector
except ImportError:
    ReversalDetector = None

try:
    from zerodha_feed import ZerodhaFeed
except ImportError:
    ZerodhaFeed = None

try:
    from market_calculus import MarketCalculus
except ImportError:
    MarketCalculus = None

try:
    from market_regime import RegimeDetector, MarketRegime, get_regime_params
except ImportError:
    RegimeDetector = None
    MarketRegime = None
    get_regime_params = None

try:
    from trade_notifier import (send_message, notify_trade_entry, notify_trade_exit,
                                notify_signal_blocked, notify_broker_reject)
except ImportError:
    def send_message(msg): logger.info(f"[NOTIFY] {msg}")
    def notify_trade_entry(**kw): pass
    def notify_trade_exit(**kw): pass
    def notify_signal_blocked(*a, **kw): pass
    def notify_broker_reject(*a, **kw): pass

import numpy as np
import pandas as pd

# ── Configuration — v25 ──────────────────────────────────────────────────────
STOCK_CAPITAL = 200000               # v28: Rs 2L (MC-optimal for 2 lots)
DAILY_LOSS_LIMIT = 15000             # Rs 15K circuit breaker
MAX_POSITIONS = 5                    # Max 5 open positions
MAX_LOTS_PER_TRADE = 2              # v28: 2 lots (MC-optimal)
MAX_PER_TRADE = 25000               # Rs 25K max per trade
MAX_SAME_DIRECTION_PER_SYMBOL = 2   # Max 2 same-dir per stock
MAX_TRADES_PER_DAY = 20             # Cap total trades/day

# Signal quality filter
MIN_SIGNAL_SCORE = 50

# Active strategies (v25 — same as equity live bot)
ACTIVE_STRATEGIES = {'CPR', 'Wave', 'Gamma Blast'}

# Strategy priority for dedup (higher = preferred)
STRATEGY_PRIORITY = {
    'CPR': 100,
    'Wave': 95,
    'Gamma Blast': 85,
}

# Cooldowns
REENTRY_COOLDOWN_SECONDS = 300      # 5 min
DIRECTION_FLIP_COOLDOWN_SECONDS = 180

# Timing
SCAN_INTERVAL_SECONDS = 45          # Check for signals every 45s (more stocks = slower)
PREMIUM_REFRESH_SECONDS = 15        # Refresh option LTP every 15s
MARKET_OPEN_TIME = dtime(9, 15)
MARKET_CLOSE_TIME = dtime(15, 30)
EOD_SQUAREOFF_TIME = dtime(15, 20)
NO_NEW_ENTRY_AFTER = dtime(14, 30)  # No new entries after 2:30 PM
FIRST_TRADE_TIME = dtime(9, 16)     # Allow after 9:16

# Exit parameters (v25 MC-optimal)
GRACE_PERIOD_SECONDS = 180
TSL_BREAKEVEN_TRIGGER_PCT = 5       # v27: MC-optimal — lock breakeven at +5%
TSL_TRAIL_TRIGGER_PCT = 3           # v27: MC-optimal — start trailing at +3%
PEAK_TRAIL_TRIGGER_PCT = 2          # v27: MC-optimal — peak trail at +2%
PARTIAL_BOOK_TSL_DISTANCE_PCT = 3   # v27: MC-optimal 3% trail from peak
PARTIAL_SL_LOSS_PCT = 15            # v27: MC-optimal 15% SL
PARTIAL_SL_FULL_PCT = 20            # v27: MC-optimal full exit at -20%
MIN_PREMIUM_BUY = 15
CPR_MIN_WIDTH_PCT = 0.03

# v25 Safety filters
DCI_MIN_THRESHOLD = 40
CHOPPY_DAY_EFF_THRESHOLD = 15
CHOPPY_DAY_BLOCK_AFTER = dtime(11, 0)

# Brokerage cost model
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18

# Stock universe — Tier 1 + Tier 2 (most liquid F&O stocks)
STOCK_UNIVERSE = list(TIER1_STOCKS.keys()) + list(TIER2_STOCKS.keys())
# Fallback if config not available
if not STOCK_UNIVERSE:
    STOCK_UNIVERSE = ['SBIN', 'RELIANCE', 'HDFCBANK', 'INFY', 'ITC',
                      'CIPLA', 'TITAN', 'LT', 'BAJFINANCE', 'DLF',
                      'TATAPOWER', 'ASIANPAINT', 'CGPOWER']


def compute_direction_confidence_stock(sig, spot, vwap, ema9, ema20,
                                       three_bar_trend, pcr, open_price,
                                       iv, vix=None):
    """v25: Direction Confidence Index for stocks — adapted from paper_trader.py.

    Uses available live data (no full greeks required).
    Returns: score 0-100 (higher = stronger conviction).
    """
    is_ce = 'CE' in sig.get('type', '')
    score = 50  # Neutral baseline

    # 1. VWAP POSITION (±10)
    if vwap and vwap > 0:
        vwap_dist_pct = (spot - vwap) / vwap * 100
        if is_ce:
            score += min(max(vwap_dist_pct * 5, -10), 10)
        else:
            score += min(max(-vwap_dist_pct * 5, -10), 10)

    # 2. EMA TREND (±12)
    if ema9 and ema20 and ema20 > 0:
        ema_gap_pct = (ema9 - ema20) / ema20 * 100
        if is_ce:
            score += min(max(ema_gap_pct * 10, -12), 12)
        else:
            score += min(max(-ema_gap_pct * 10, -12), 12)

    # 3. 3-BAR MOMENTUM (±8)
    if three_bar_trend != 0:
        if is_ce:
            score += min(three_bar_trend * 3, 8)
        else:
            score += min(-three_bar_trend * 3, 8)

    # 4. PCR SENTIMENT (±8)
    if pcr and pcr > 0:
        if is_ce and pcr > 1.0:
            score += min((pcr - 1.0) * 20, 8)
        elif not is_ce and pcr < 1.0:
            score += min((1.0 - pcr) * 20, 8)

    # 5. IV SIGNAL STRENGTH (±10)
    if iv and iv > 0:
        if iv >= 40:
            score += 10
        elif iv >= 25:
            score += 5
        elif iv < 15:
            score -= 10
        elif iv < 20:
            score -= 5

    # 6. INTRADAY BODY (±5)
    if open_price and open_price > 0:
        body_pct = (spot - open_price) / open_price * 100
        if is_ce:
            score += min(max(body_pct * 3, -5), 5)
        else:
            score += min(max(-body_pct * 3, -5), 5)

    return max(min(score, 100), 0)


class StockLivePosition:
    """Tracks a single stock live trading position."""

    def __init__(self, pos_id, symbol, strategy, signal_type, strike,
                 entry_premium, entry_order_id, lot_size, num_lots,
                 target, sl, spot_at_entry, security_id, exchange_segment):
        self.id = pos_id
        self.symbol = symbol
        self.strategy = strategy
        self.signal_type = signal_type
        self.strike = strike
        self.entry_premium = entry_premium
        self.entry_order_id = entry_order_id
        self.lot_size = lot_size
        self.num_lots = num_lots
        self.quantity = lot_size * num_lots
        self.target = target
        self.sl = sl
        self.spot_at_entry = spot_at_entry
        self.security_id = security_id
        self.exchange_segment = exchange_segment
        self.entry_time = datetime.now()

        # Live tracking
        self.current_premium = entry_premium
        self.peak_premium = entry_premium
        self.trailing_sl = sl
        self.sl_order_id = None
        self.partial_booked = False
        self.partial_sl_done = False
        self.closed = False
        self.exit_premium = 0
        self.exit_reason = ''
        self.exit_time = None
        self.realized_pnl = 0

        # Signal metadata
        self.quality_score = 0
        self.data_source = 'DHAN'
        self.signal_reason = ''
        self.iv_at_entry = 0
        self.pcr_at_entry = 0
        self.indicator_levels = {}
        self.dci_at_entry = 0
        self.physics_score = 0

    def update_premium(self, premium):
        """Update current premium."""
        self.current_premium = premium
        if premium > self.peak_premium:
            self.peak_premium = premium

    @property
    def unrealized_pnl(self):
        if self.closed:
            return self.realized_pnl
        return (self.current_premium - self.entry_premium) * self.quantity

    @property
    def premium_gain_pct(self):
        if self.entry_premium <= 0:
            return 0
        return (self.current_premium - self.entry_premium) / self.entry_premium * 100

    @property
    def peak_gain_pct(self):
        if self.entry_premium <= 0:
            return 0
        return (self.peak_premium - self.entry_premium) / self.entry_premium * 100

    @property
    def elapsed_seconds(self):
        return (datetime.now() - self.entry_time).total_seconds()

    def to_dict(self):
        return {
            'id': self.id, 'symbol': self.symbol, 'strategy': self.strategy,
            'signal_type': self.signal_type, 'strike': self.strike,
            'entry_premium': self.entry_premium, 'current_premium': self.current_premium,
            'peak_premium': self.peak_premium, 'target': self.target,
            'sl': self.sl, 'trailing_sl': self.trailing_sl,
            'quantity': self.quantity, 'num_lots': self.num_lots,
            'unrealized_pnl': self.unrealized_pnl,
            'premium_gain_pct': round(self.premium_gain_pct, 1),
            'entry_time': self.entry_time.isoformat(),
            'elapsed_min': round(self.elapsed_seconds / 60, 1),
            'closed': self.closed, 'exit_reason': self.exit_reason,
            'exit_premium': self.exit_premium,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'realized_pnl': self.realized_pnl,
            'quality_score': self.quality_score,
            'data_source': self.data_source,
            'dci': self.dci_at_entry,
            'physics_score': self.physics_score,
        }


class StockLiveTrader:
    """Main stock live trading orchestrator."""

    def __init__(self, shadow_mode=True):
        self.shadow_mode = shadow_mode
        self.positions = []
        self.closed_positions = []
        self._running = False
        self._trade_counter = 0
        self._daily_signals_log = []

        # ── Initialize components ────────────────────────────────────────
        mode = "SHADOW" if shadow_mode else "LIVE"
        logger.info(f"{'='*60}")
        logger.info(f"  STOCK LIVE TRADER v25 — {mode} MODE")
        logger.info(f"  Capital: Rs {STOCK_CAPITAL:,} | Loss limit: Rs {DAILY_LOSS_LIMIT:,}")
        logger.info(f"  Max lots: {MAX_LOTS_PER_TRADE} | Max positions: {MAX_POSITIONS}")
        logger.info(f"  Strategies: {', '.join(sorted(ACTIVE_STRATEGIES))}")
        logger.info(f"  Priority: CPR(100) > Wave(95) > Gamma Blast(85)")
        logger.info(f"  OI Heatmap: {'ON' if OIHeatmap else 'OFF'} | V-Reversal: {'ON' if ReversalDetector else 'OFF'}")
        logger.info(f"  Physics Gate: {'ON' if MarketCalculus else 'OFF'} | DCI Gate: ON (min {DCI_MIN_THRESHOLD})")
        logger.info(f"  Choppy Shield: ON (eff<{CHOPPY_DAY_EFF_THRESHOLD}% after 11:00)")
        logger.info(f"  Regime Detector: {'ON' if RegimeDetector else 'OFF'} (adaptive TSL)")
        logger.info(f"  Stocks: {len(STOCK_UNIVERSE)} ({', '.join(STOCK_UNIVERSE[:5])}...)")
        logger.info(f"  Order type: LIMIT only (SEBI Apr 2026)")
        logger.info(f"{'='*60}")

        # Broker (order execution)
        self.broker = DhanBroker(shadow_mode=shadow_mode)
        if not self.broker.connect():
            logger.error("FATAL: Cannot connect to Dhan broker")
            sys.exit(1)

        # Data feed
        self.dhan_feed = DhanFeed()
        if not self.dhan_feed.connect():
            logger.error("FATAL: Cannot connect to Dhan data feed")
            sys.exit(1)

        self.zerodha = None
        if ZerodhaFeed:
            try:
                self.zerodha = ZerodhaFeed()
                if self.zerodha.connect():
                    logger.info("[Data] Zerodha connected as fallback")
                else:
                    self.zerodha = None
            except Exception as e:
                logger.warning(f"[Data] Zerodha unavailable: {e}")

        # Stock F&O config (lot sizes, strike intervals)
        self.stock_config = None
        if StockFnOConfig:
            try:
                self.stock_config = StockFnOConfig()
                logger.info(f"[Config] StockFnOConfig loaded — {len(self.stock_config.stocks)} stocks")
            except Exception as e:
                logger.warning(f"[Config] StockFnOConfig failed: {e}")

        # OI Heatmap exit
        self.oi_heatmap = None
        if OIHeatmap:
            try:
                self.oi_heatmap = OIHeatmap(dhan_feed=self.dhan_feed, zerodha_feed=self.zerodha)
                logger.info("[OI] OIHeatmap initialized")
            except Exception as e:
                logger.warning(f"[OI] OIHeatmap init failed: {e}")

        # V-Reversal detector
        self.reversal_detector = None
        if ReversalDetector:
            try:
                self.reversal_detector = ReversalDetector()
                logger.info("[Reversal] ReversalDetector initialized")
            except Exception as e:
                logger.warning(f"[Reversal] init failed: {e}")

        # v25: MarketCalculus (Physics Gate + DCI calculus boost)
        self.calculus = None
        if MarketCalculus:
            try:
                self.calculus = MarketCalculus()
                logger.info("[Calculus] MarketCalculus initialized (Physics Gate + DCI)")
            except Exception as e:
                logger.warning(f"[Calculus] init failed: {e}")

        # v25: Regime Detector
        self.regime_detector = None
        if RegimeDetector:
            try:
                self.regime_detector = RegimeDetector()
                logger.info("[Regime] RegimeDetector initialized")
            except Exception as e:
                logger.warning(f"[Regime] init failed: {e}")

        # Tracking
        self.current_vix = 0
        self._exit_history = defaultdict(list)
        self._trade_count_today = 0
        self._daily_pnl = 0

        # Strategy engine state
        self._spot_bars = defaultdict(list)
        self._last_bar_time = {}
        self._cpr_levels = {}
        self._prev_ohlc = {}
        self._wave_state = defaultdict(lambda: {'last_signal_time': None})
        self._gamma_last_signal = {}

        logger.info("[StockLiveTrader] v25 All components initialized")

    # ── Data Methods ─────────────────────────────────────────────────────────

    def get_spot(self, symbol):
        """Get current stock price from Dhan or Zerodha."""
        try:
            spot = self.dhan_feed.get_spot(symbol)
            if spot and spot > 0:
                return spot
        except Exception:
            pass
        if self.zerodha:
            try:
                spot = self.zerodha.get_spot(symbol)
                if spot and spot > 0:
                    return spot
            except Exception:
                pass
        return None

    def get_option_chain(self, symbol):
        """Get stock option chain from Dhan or Zerodha."""
        try:
            chain = self.dhan_feed.get_option_chain(symbol)
            if chain and len(chain.get('CE', [])) > 0:
                return chain
        except Exception:
            pass
        if self.zerodha:
            try:
                chain = self.zerodha.get_option_chain(symbol)
                if chain:
                    return chain
            except Exception:
                pass
        return None

    def _get_lot_size(self, symbol):
        """Get lot size for a stock."""
        if self.stock_config and symbol in self.stock_config.stocks:
            return self.stock_config.stocks[symbol].get('lot_size', 100)
        # Fallback from tier config
        all_tiers = {**TIER1_STOCKS, **TIER2_STOCKS}
        if symbol in all_tiers:
            return all_tiers[symbol]['lot_size']
        return 100  # Safe default

    def _get_strike_interval(self, symbol):
        """Get strike interval for a stock."""
        if self.stock_config and symbol in self.stock_config.stocks:
            return self.stock_config.stocks[symbol].get('strike_interval', 25)
        all_tiers = {**TIER1_STOCKS, **TIER2_STOCKS}
        if symbol in all_tiers:
            return all_tiers[symbol]['strike_interval']
        return 25  # Safe default

    def get_option_ltp_and_security_id(self, symbol, strike, opt_type):
        """Get live LTP and identifier for a stock option contract.

        v30.3: Accept Zerodha token/tradingsymbol as fallback when Dhan
        security_id is unavailable (shadow bots skip Dhan chain).
        """
        chain = self.get_option_chain(symbol)
        if not chain:
            return 0, None, 0

        for c in chain.get(opt_type, []):
            if abs(float(c.get('strike', 0)) - float(strike)) < 0.01:
                ltp = float(c.get('ltp', 0))
                # Dhan security_id preferred; Zerodha token/tradingsymbol as fallback
                sec_id = c.get('security_id', '') or c.get('tradingsymbol', '') or c.get('token', '')
                iv = float(c.get('iv', 0))
                if ltp > 0 and sec_id:
                    return ltp, str(sec_id), iv
                elif ltp > 0:
                    return ltp, 'SHADOW', iv  # v30.3: non-None for shadow mode
                break
        return 0, None, 0

    def get_atm_strike(self, symbol, spot, opt_type):
        """Get ATM strike with live LTP and security_id."""
        interval = self._get_strike_interval(symbol)
        atm_strike = round(spot / interval) * interval

        candidates = [atm_strike]
        if opt_type == 'CE':
            candidates.append(atm_strike + interval)
        else:
            candidates.append(atm_strike - interval)

        for strike in candidates:
            ltp, sec_id, iv = self.get_option_ltp_and_security_id(symbol, strike, opt_type)
            if ltp > 0:  # v30.3: don't require sec_id — shadow mode uses Zerodha without Dhan IDs
                return strike, ltp, sec_id, iv

        return 0, 0, None, 0

    def _get_pcr(self, chain):
        """Compute Put-Call Ratio from chain."""
        try:
            ce_oi = sum(float(c.get('oi', 0)) for c in chain.get('CE', []))
            pe_oi = sum(float(c.get('oi', 0)) for c in chain.get('PE', []))
            return round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0
        except Exception:
            return 0

    def _get_vwap(self, symbol):
        """Compute VWAP from spot bars."""
        bars = self._spot_bars.get(symbol, [])
        if len(bars) < 2:
            return 0
        tp_sum = 0
        count = 0
        for b in bars:
            if b['close'] > 0:
                tp = (b['high'] + b['low'] + b['close']) / 3
                tp_sum += tp
                count += 1
        return round(tp_sum / count, 2) if count > 0 else 0

    # ── v25 Safety Filter Helpers ────────────────────────────────────────────

    def _get_dci_indicators(self, symbol, spot):
        """Build indicator values for DCI computation from spot bars."""
        bars = self._spot_bars.get(symbol, [])
        closes = [b['close'] for b in bars if b['close'] > 0]
        vwap = self._get_vwap(symbol)

        ema9 = float(np.mean(closes[-9:])) if len(closes) >= 9 else spot
        ema20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else spot

        three_bar_trend = 0
        if len(closes) >= 4:
            for i in range(-3, 0):
                if closes[i] > closes[i - 1]:
                    three_bar_trend += 1
                elif closes[i] < closes[i - 1]:
                    three_bar_trend -= 1

        open_price = closes[0] if closes else spot
        return vwap, ema9, ema20, three_bar_trend, open_price

    def _check_choppy_shield(self, symbol, spot):
        """v25 Choppy Shield: Block entries after 11:00 if efficiency < 15%.

        Returns: (is_choppy, efficiency_pct)
        """
        now = datetime.now()
        if now.time() < CHOPPY_DAY_BLOCK_AFTER:
            return False, 100.0

        bars = self._spot_bars.get(symbol, [])
        if len(bars) < 5:
            return False, 100.0

        closes = [b['close'] for b in bars if b['close'] > 0]
        highs = [b['high'] for b in bars if b['high'] > 0]
        lows = [b['low'] for b in bars if b['low'] > 0]

        if not closes or not highs or not lows:
            return False, 100.0

        session_open = closes[0]
        day_high = max(highs)
        day_low = min(lows)
        day_range = day_high - day_low

        if day_range <= 0:
            return True, 0.0

        net_move = abs(spot - session_open)
        efficiency = net_move / day_range * 100

        return efficiency < CHOPPY_DAY_EFF_THRESHOLD, round(efficiency, 1)

    # ── Signal Generation ────────────────────────────────────────────────────

    def _update_spot_bar(self, symbol, spot):
        """Build 1-min spot bars for Wave detection."""
        now = datetime.now()
        bar_time = now.replace(second=0, microsecond=0)
        bars = self._spot_bars[symbol]

        if symbol not in self._last_bar_time or self._last_bar_time[symbol] != bar_time:
            if bars and bars[-1]['close'] == 0:
                bars[-1]['close'] = spot
            bars.append({'time': bar_time, 'open': spot, 'high': spot, 'low': spot, 'close': 0})
            self._last_bar_time[symbol] = bar_time
            if len(bars) > 60:
                self._spot_bars[symbol] = bars[-60:]
        else:
            if bars:
                bars[-1]['high'] = max(bars[-1]['high'], spot)
                bars[-1]['low'] = min(bars[-1]['low'], spot)
                bars[-1]['close'] = spot

    def _compute_signal_quality(self, symbol, strategy, spot, premium, iv, chain):
        """Standalone signal quality scoring (0-100)."""
        score = 60

        if iv:
            if 15 <= iv <= 40:
                score += 15
            elif 10 <= iv <= 50:
                score += 8

        if 50 <= premium <= 300:
            score += 10
        elif 20 <= premium <= 500:
            score += 5

        try:
            ce_oi = sum(float(c.get('oi', 0)) for c in chain.get('CE', []))
            pe_oi = sum(float(c.get('oi', 0)) for c in chain.get('PE', []))
            if ce_oi > 0 and pe_oi > 0:
                pcr = pe_oi / ce_oi
                if ('CE' in strategy or strategy == 'CPR') and pcr > 1.0:
                    score += 10
                elif ('PE' in strategy) and pcr < 0.8:
                    score += 10
                else:
                    score += 3
        except Exception:
            pass

        if strategy == 'CPR':
            score += 5
        elif strategy == 'Wave':
            score += 3

        return min(100, max(0, score))

    def generate_signals(self, symbol):
        """Generate trading signals for a stock."""
        spot = self.get_spot(symbol)
        if not spot:
            return []

        chain = self.get_option_chain(symbol)
        if not chain:
            return []

        self._update_spot_bar(symbol, spot)

        signals = []

        # CPR signals
        cpr_signals = self._check_cpr(symbol, spot, chain)
        signals.extend(cpr_signals)

        # Wave signals
        wave_signals = self._check_wave(symbol, spot, chain)
        signals.extend(wave_signals)

        # Gamma Blast — stock monthly expiry (last Thursday)
        gamma_signals = self._check_gamma_blast(symbol, spot, chain)
        signals.extend(gamma_signals)

        return signals

    def _check_cpr(self, symbol, spot, chain):
        """CPR strategy for stocks."""
        signals = []

        # Get previous day OHLC from Dhan
        try:
            # For stocks, use Dhan's stock historical data
            hist = self.dhan_feed.get_historical_data(symbol, days=10)
            if not hist or len(hist) < 2:
                return signals

            prev = hist[-2]
            today_open = hist[-1].get('open', spot)
            h = float(prev.get('high', 0))
            l = float(prev.get('low', 0))
            c = float(prev.get('close', 0))
        except Exception as e:
            logger.debug(f"[CPR] Historical data error for {symbol}: {e}")
            return signals

        if h <= 0 or l <= 0 or c <= 0:
            return signals

        # CPR calculation
        pivot = (h + l + c) / 3
        bc = (h + l) / 2
        tc = (pivot - bc) + pivot
        cpr_w = abs(tc - bc) / pivot * 100

        if cpr_w < CPR_MIN_WIDTH_PCT:
            return signals

        target_mult = 1.40
        sl_mult = 0.75

        intraday_body = spot - today_open
        pcr = self._get_pcr(chain)
        vwap = self._get_vwap(symbol)

        cpr_levels = {
            'pivot': round(pivot, 1), 'tc': round(tc, 1), 'bc': round(bc, 1),
            'vwap': vwap, 'today_open': round(today_open, 1),
        }
        self._cpr_levels[symbol] = cpr_levels

        # BUY CE: spot above TC + bullish body
        if spot > tc and intraday_body > 0:
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, 'CE')
            if ltp > MIN_PREMIUM_BUY and sec_id:
                q_score = self._compute_signal_quality(symbol, 'CPR', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_CE_CPR', 'strategy': 'CPR',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * target_mult, 'sl': ltp * sl_mult,
                    'reason': f"CPR ({cpr_w:.3f}%) bullish breakout above TC={tc:.0f}",
                    'quality_score': q_score, 'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': cpr_levels,
                })

        # BUY PE: spot below BC + bearish body
        elif spot < bc and intraday_body < 0:
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, 'PE')
            if ltp > MIN_PREMIUM_BUY and sec_id:
                q_score = self._compute_signal_quality(symbol, 'CPR', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_PE_CPR', 'strategy': 'CPR',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * target_mult, 'sl': ltp * sl_mult,
                    'reason': f"CPR ({cpr_w:.3f}%) bearish breakout below BC={bc:.0f}",
                    'quality_score': q_score, 'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': cpr_levels,
                })

        return signals

    def _check_wave(self, symbol, spot, chain):
        """Wave strategy — 5-bar swing detection for stocks."""
        signals = []
        bars = self._spot_bars.get(symbol, [])
        if len(bars) < 8:
            return signals

        now = datetime.now()
        if now.time() < dtime(9, 30):
            return signals

        ws = self._wave_state[symbol]
        if ws['last_signal_time'] and (now - ws['last_signal_time']).total_seconds() < 300:
            return signals

        recent = [b for b in bars[:-1] if b['close'] > 0][-8:]
        if len(recent) < 6:
            return signals

        highs = [b['high'] for b in recent]
        lows = [b['low'] for b in recent]
        swing_high = max(highs[-5:])
        swing_low = min(lows[-5:])

        last3_closes = [b['close'] for b in recent[-3:]]
        bullish_momentum = all(last3_closes[i] > last3_closes[i-1] for i in range(1, 3))
        bearish_momentum = all(last3_closes[i] < last3_closes[i-1] for i in range(1, 3))

        pcr = self._get_pcr(chain)
        vwap = self._get_vwap(symbol)
        wave_levels = {
            'swing_high': round(swing_high, 1), 'swing_low': round(swing_low, 1),
            'vwap': vwap, 'momentum': 'BULL' if bullish_momentum else ('BEAR' if bearish_momentum else 'FLAT'),
        }

        if spot > swing_high and bullish_momentum:
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, 'CE')
            if ltp > MIN_PREMIUM_BUY and sec_id:
                q_score = self._compute_signal_quality(symbol, 'Wave', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_CE_WAVE', 'strategy': 'Wave',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * 1.40, 'sl': ltp * 0.75,
                    'reason': f"Wave breakout above swing high {swing_high:.0f}",
                    'quality_score': q_score, 'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': wave_levels,
                })
                ws['last_signal_time'] = now

        elif spot < swing_low and bearish_momentum:
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, 'PE')
            if ltp > MIN_PREMIUM_BUY and sec_id:
                q_score = self._compute_signal_quality(symbol, 'Wave', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_PE_WAVE', 'strategy': 'Wave',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * 1.40, 'sl': ltp * 0.75,
                    'reason': f"Wave breakdown below swing low {swing_low:.0f}",
                    'quality_score': q_score, 'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': wave_levels,
                })
                ws['last_signal_time'] = now

        return signals

    def _check_gamma_blast(self, symbol, spot, chain):
        """Gamma Blast — monthly expiry day for stocks (last Thursday)."""
        signals = []
        now = datetime.now()

        # Check if today is monthly expiry (last Thursday)
        today = now.date()
        if today.weekday() != 3:  # Thursday = 3
            return signals
        # Check if it's the LAST Thursday of the month
        next_week = today + timedelta(days=7)
        if next_week.month == today.month:
            return signals  # Not last Thursday

        if now.time() < dtime(10, 0) or now.time() >= dtime(14, 30):
            return signals

        last_gb = self._gamma_last_signal.get(symbol)
        if last_gb and (now - last_gb).total_seconds() < 900:
            return signals

        interval = self._get_strike_interval(symbol)
        atm_strike = round(spot / interval) * interval

        for opt_type in ['CE', 'PE']:
            ltp, sec_id, iv = self.get_option_ltp_and_security_id(symbol, atm_strike, opt_type)
            if not ltp or ltp <= 0 or not sec_id:
                continue
            if ltp < MIN_PREMIUM_BUY or ltp > 300:
                continue

            spot_dist = abs(spot - atm_strike) / interval if interval > 0 else 1
            if spot_dist > 0.6:
                continue

            bars = self._spot_bars.get(symbol, [])
            if len(bars) < 3:
                continue
            recent_closes = [b['close'] for b in bars[-3:] if b['close'] > 0]
            if len(recent_closes) < 2:
                continue
            trending_up = recent_closes[-1] > recent_closes[0]

            if opt_type == 'CE' and not trending_up:
                continue
            if opt_type == 'PE' and trending_up:
                continue

            pcr = self._get_pcr(chain)
            q_score = self._compute_signal_quality(symbol, 'Gamma Blast', spot, ltp, iv, chain)
            signals.append({
                'type': f'BUY_{opt_type}_GAMMA', 'strategy': 'Gamma Blast',
                'symbol': symbol, 'strike': atm_strike, 'premium': ltp,
                'security_id': sec_id, 'iv': iv, 'spot': spot,
                'target': ltp * 1.50, 'sl': ltp * 0.70,
                'reason': f"Gamma Blast expiry ATM={atm_strike}",
                'quality_score': q_score, 'data_source': 'DHAN', 'pcr': pcr,
                'indicator_levels': {'atm_strike': atm_strike, 'expiry_day': True},
            })
            self._gamma_last_signal[symbol] = now
            break

        return signals

    # ── Order Execution ──────────────────────────────────────────────────────

    def execute_signal(self, sig):
        """Execute a qualified stock trading signal."""
        symbol = sig['symbol']
        strategy = sig.get('strategy', 'UNKNOWN')
        sig_type = sig.get('type', '')
        strike = sig.get('strike', 0)
        premium = sig.get('premium', 0)
        sec_id = sig.get('security_id')
        target = sig.get('target', premium * 1.4)
        sl = sig.get('sl', premium * 0.5)
        quality = sig.get('quality_score', 0)

        direction = 'CE' if 'CE' in sig_type else 'PE'
        lot_size = self._get_lot_size(symbol)
        quantity = lot_size * MAX_LOTS_PER_TRADE
        trade_cost = premium * quantity

        # ── Pre-trade checks ─────────────────────────────────────────────
        if trade_cost > MAX_PER_TRADE:
            logger.warning(f"[BLOCKED] {symbol} {strategy}: trade cost Rs {trade_cost:,.0f} > max Rs {MAX_PER_TRADE:,}")
            notify_signal_blocked("LIVE_STOCKS", symbol, sig_type, "RISK_BLOCK", f"Cost Rs {trade_cost:,.0f} > max Rs {MAX_PER_TRADE:,}")
            return False

        if len(self.positions) >= MAX_POSITIONS:
            logger.warning(f"[BLOCKED] {symbol}: max positions ({MAX_POSITIONS}) reached")
            notify_signal_blocked("LIVE_STOCKS", symbol, sig_type, "RISK_BLOCK", f"Max positions ({MAX_POSITIONS}) reached")
            return False

        if abs(self._daily_pnl) >= DAILY_LOSS_LIMIT and self._daily_pnl < 0:
            logger.warning(f"[BLOCKED] Daily loss limit Rs {DAILY_LOSS_LIMIT:,} reached")
            notify_signal_blocked("LIVE_STOCKS", symbol, sig_type, "RISK_BLOCK", f"Daily loss limit Rs {DAILY_LOSS_LIMIT:,} reached")
            return False

        if not sec_id:
            logger.warning(f"[SKIP] {symbol} {strategy}: no security_id")
            return False

        # ── Place entry order ────────────────────────────────────────────
        tag = f"STK_{strategy[:3]}_{symbol}_{direction}_{strike}"
        logger.info(f"\n{'='*50}")
        logger.info(f"  STOCK ENTRY: {symbol} {strategy} {sig_type}")
        logger.info(f"  Strike={strike} Premium=Rs {premium:.2f} Qty={quantity} Lot={lot_size}")
        logger.info(f"  Target=Rs {target:.2f} SL=Rs {sl:.2f} Score={quality}")
        logger.info(f"  DCI={sig.get('_dci', '?')} Physics={sig.get('_physics_score', '?')}")
        logger.info(f"{'='*50}")

        entry_result = self.broker.place_entry_order(
            security_id=sec_id,
            exchange_segment='NSE_FNO',
            transaction_type='BUY',
            quantity=quantity,
            ltp=premium,
            tag=tag,
        )

        if not entry_result['success']:
            logger.error(f"[ENTRY_FAIL] {symbol}: {entry_result['message']}")
            notify_broker_reject("LIVE_STOCKS", symbol, sig_type, strike,
                                "ORDER_REJECTED", entry_result['message'])
            return False

        order_id = entry_result['order_id']
        fill = self.broker.wait_for_fill(order_id, timeout=30)

        if not fill['filled']:
            logger.warning(f"[ENTRY_TIMEOUT] {symbol} order {order_id}")
            self.broker.cancel_order(order_id)
            return False

        fill_price = fill['fill_price']
        fill_qty = fill['fill_qty'] or quantity

        # ── Create position ──────────────────────────────────────────────
        self._trade_counter += 1
        pos_id = f"SLIVE_{symbol}_{self._trade_counter}_{datetime.now().strftime('%H%M%S')}"

        pos = StockLivePosition(
            pos_id=pos_id, symbol=symbol, strategy=strategy,
            signal_type=sig_type, strike=strike,
            entry_premium=fill_price, entry_order_id=order_id,
            lot_size=lot_size, num_lots=MAX_LOTS_PER_TRADE,
            target=target, sl=sl, spot_at_entry=sig.get('spot', 0),
            security_id=sec_id, exchange_segment='NSE_FNO',
        )

        # Place SL order
        sl_result = self.broker.place_sl_order(
            security_id=sec_id, exchange_segment='NSE_FNO',
            quantity=fill_qty, sl_trigger_price=sl, tag=f"SL_{tag}",
        )
        if sl_result['success']:
            pos.sl_order_id = sl_result['order_id']

        pos.quality_score = quality
        pos.dci_at_entry = sig.get('_dci', 0)
        pos.physics_score = sig.get('_physics_score', 0)
        pos.iv_at_entry = sig.get('iv', 0)
        pos.pcr_at_entry = sig.get('pcr', 0)
        pos.indicator_levels = sig.get('indicator_levels', {})
        pos.signal_reason = sig.get('reason', '')

        self.positions.append(pos)

        try:
            notify_trade_entry(
                market="STOCK", strategy=strategy, symbol=symbol,
                signal_type=sig_type, strike=strike, premium=fill_price,
                lots=MAX_LOTS_PER_TRADE, quality_score=quality,
                reason=sig.get('reason', ''),
            )
        except Exception:
            pass

        logger.info(f"[POSITION_OPEN] {pos_id}: {symbol} {strategy} {sig_type} "
                    f"Strike={strike} Fill=Rs {fill_price:.2f} Qty={fill_qty}")

        self._save_trade_log(pos, 'ENTRY')
        return True

    # ── Exit Management ──────────────────────────────────────────────────────

    def check_exits(self):
        """Check all open positions for exit conditions."""
        if not self.positions:
            return

        # Check daily loss limit
        if self._daily_pnl <= -DAILY_LOSS_LIMIT:
            logger.critical(f"[HALT] Daily loss Rs {self._daily_pnl:,.0f} >= limit Rs {DAILY_LOSS_LIMIT:,}")
            self._flatten_all('DAILY_LOSS_LIMIT')
            return

        for pos in list(self.positions):
            if pos.closed:
                continue

            # Update premium
            ltp, _, _ = self.get_option_ltp_and_security_id(
                pos.symbol, pos.strike,
                'CE' if 'CE' in pos.signal_type else 'PE'
            )
            if ltp > 0:
                pos.update_premium(ltp)

            # EOD Force Close
            if datetime.now().time() >= EOD_SQUAREOFF_TIME:
                self._close_position(pos, ltp, 'EOD_FORCE_CLOSE')
                continue

            # Grace Period
            if pos.elapsed_seconds < GRACE_PERIOD_SECONDS:
                continue

            # Check SL order filled (live mode only)
            if pos.sl_order_id and not pos.sl_order_id.startswith('SHADOW_'):
                sl_status = self.broker.get_order_status(pos.sl_order_id)
                if sl_status == 'TRADED':
                    fill = self.broker.wait_for_fill(pos.sl_order_id, timeout=5)
                    exit_price = fill.get('fill_price', pos.sl) if fill['filled'] else pos.sl
                    pos.exit_premium = exit_price
                    pos.exit_reason = 'SL_HIT'
                    pos.closed = True
                    pos.realized_pnl = (exit_price - pos.entry_premium) * pos.quantity
                    self._finalize_exit(pos)
                    continue

            # OI Heatmap Exit (profitable positions only)
            if self.oi_heatmap and pos.unrealized_pnl > 0:
                try:
                    opt_type = 'CE' if 'CE' in pos.signal_type else 'PE'
                    oi_exit = self.oi_heatmap.get_exit_signal(
                        symbol=pos.symbol, spot=self.get_spot(pos.symbol),
                        opt_type=opt_type, strike=pos.strike,
                        entry_premium=pos.entry_premium,
                        current_premium=pos.current_premium)
                    if oi_exit['action'] == 'FULL_EXIT':
                        logger.info(f"[OI_EXIT] {pos.id} conf={oi_exit['confidence']}")
                        self._close_position(pos, ltp, f'OI_EXIT_{oi_exit["confidence"]}')
                        continue
                    elif oi_exit['action'] == 'TRAIL_50%':
                        if pos.entry_premium > pos.trailing_sl:
                            pos.trailing_sl = pos.entry_premium
                            self._update_sl_order(pos, pos.entry_premium)
                except Exception:
                    pass

            # Premium-based exit logic
            gain_pct = pos.premium_gain_pct
            peak_pct = pos.peak_gain_pct

            # v25: Regime-aware TSL distance
            tsl_trail_dist = PARTIAL_BOOK_TSL_DISTANCE_PCT
            if self.regime_detector and get_regime_params:
                try:
                    regime = self.regime_detector.get_regime(pos.symbol)
                    regime_params = get_regime_params(regime)
                    tsl_trail_dist = regime_params.get('tsl_trail_distance_pct', PARTIAL_BOOK_TSL_DISTANCE_PCT)
                except Exception:
                    pass

            # LOSS: Partial SL at -25%
            if gain_pct <= -PARTIAL_SL_LOSS_PCT and not pos.partial_sl_done:
                self._close_position(pos, ltp, f'PARTIAL_SL_{gain_pct:.0f}%')
                continue

            # LOSS: Full SL at -30%
            if gain_pct <= -PARTIAL_SL_FULL_PCT:
                self._close_position(pos, ltp, f'FULL_SL_{gain_pct:.0f}%')
                continue

            # PROFIT: TSL to breakeven at +15%
            if gain_pct >= TSL_BREAKEVEN_TRIGGER_PCT:
                new_tsl = pos.entry_premium
                if new_tsl > pos.trailing_sl:
                    pos.trailing_sl = new_tsl
                    self._update_sl_order(pos, new_tsl)

            # PROFIT: TSL trailing at +15%
            if gain_pct >= TSL_TRAIL_TRIGGER_PCT:
                new_tsl = pos.entry_premium * 1.15
                if new_tsl > pos.trailing_sl:
                    pos.trailing_sl = new_tsl
                    self._update_sl_order(pos, new_tsl)

            # PROFIT: Peak trailing after +10% (regime-aware distance)
            if peak_pct >= PEAK_TRAIL_TRIGGER_PCT and pos.current_premium > pos.entry_premium:
                profit_from_entry = pos.peak_premium - pos.entry_premium
                trail_sl = pos.peak_premium - (profit_from_entry * tsl_trail_dist / 100)
                if trail_sl > pos.trailing_sl:
                    pos.trailing_sl = trail_sl
                    self._update_sl_order(pos, trail_sl)

            # Hit trailing SL
            if pos.current_premium <= pos.trailing_sl and pos.trailing_sl > pos.sl:
                self._close_position(pos, ltp, f'TSL_HIT_{peak_pct:.0f}%peak')
                continue

    def _close_position(self, pos, ltp, reason):
        """Close a stock position."""
        logger.info(f"[EXIT] {pos.id}: {reason} | premium=Rs {ltp:.2f}")

        if pos.sl_order_id:
            self.broker.cancel_order(pos.sl_order_id)

        exit_result = self.broker.place_exit_order(
            security_id=pos.security_id,
            exchange_segment=pos.exchange_segment,
            quantity=pos.quantity,
            ltp=ltp if ltp > 0 else pos.current_premium,
            tag=f"EXIT_{pos.id[:20]}",
        )

        if exit_result['success']:
            fill = self.broker.wait_for_fill(exit_result['order_id'], timeout=30)
            exit_price = fill.get('fill_price', ltp) if fill['filled'] else ltp
        else:
            exit_price = ltp

        pos.exit_premium = exit_price
        pos.exit_reason = reason
        pos.exit_time = datetime.now()
        pos.closed = True
        pnl = (exit_price - pos.entry_premium) * pos.quantity
        pos.realized_pnl = pnl
        self._daily_pnl += pnl

        self._finalize_exit(pos)

    def _finalize_exit(self, pos):
        """Move to closed list and notify."""
        self.positions = [p for p in self.positions if p.id != pos.id]
        self.closed_positions.append(pos)
        self._record_exit(pos, pos.exit_reason)

        pnl_label = f"+Rs {pos.realized_pnl:,.0f}" if pos.realized_pnl >= 0 else f"-Rs {abs(pos.realized_pnl):,.0f}"
        logger.info(f"[CLOSED] {pos.id}: {pos.symbol} {pos.strategy} | "
                    f"Entry=Rs {pos.entry_premium:.2f} Exit=Rs {pos.exit_premium:.2f} | "
                    f"PnL={pnl_label} | Reason={pos.exit_reason}")

        try:
            notify_trade_exit(
                market="STOCK", strategy=pos.strategy, symbol=pos.symbol,
                signal_type=pos.signal_type, strike=pos.strike,
                entry_price=pos.entry_premium, exit_price=pos.exit_premium,
                entry_time=pos.entry_time.isoformat(),
                pnl=pos.realized_pnl, exit_reason=pos.exit_reason,
            )
        except Exception:
            pass

        self._save_trade_log(pos, 'EXIT')

    def _update_sl_order(self, pos, new_trigger):
        """Update SL order trigger price."""
        if not pos.sl_order_id:
            return
        result = self.broker.modify_sl_order(pos.sl_order_id, new_trigger)
        if result['success']:
            logger.info(f"[TSL] {pos.id} SL → Rs {new_trigger:.2f}")

    def _flatten_all(self, reason):
        """Emergency: close all positions."""
        logger.critical(f"[FLATTEN_ALL] {reason}")
        for pos in list(self.positions):
            if not pos.closed:
                self._close_position(pos, pos.current_premium, f'FLATTEN:{reason}')

    # ── Cooldown Logic ───────────────────────────────────────────────────────

    def _check_cooldown(self, symbol, direction, strategy):
        """Cooldown check with V-Reversal support."""
        now = datetime.now()
        for eh in reversed(self._exit_history.get(symbol, [])):
            elapsed = (now - eh['time']).total_seconds()
            if elapsed > REENTRY_COOLDOWN_SECONDS:
                break
            eh_dir = eh.get('direction', '')

            if eh_dir == direction:
                if elapsed < REENTRY_COOLDOWN_SECONDS:
                    logger.info(f"[COOLDOWN] {symbol} {direction} — exit {int(elapsed)}s ago")
                    return False

            if eh.get('reason') == 'DIRECTION_FLIP':
                if elapsed < 45:
                    return False
                if self.reversal_detector:
                    if not self.reversal_detector.is_flip_allowed(symbol):
                        return False
                    spot = self.get_spot(symbol)
                    if spot:
                        rev = self.reversal_detector.check_reversal(symbol, spot, direction)
                        if rev['is_reversal'] and rev['confidence'] >= 60:
                            self.reversal_detector.record_flip(symbol)
                            return True
                        else:
                            return False
                elif elapsed < DIRECTION_FLIP_COOLDOWN_SECONDS:
                    return False
        return True

    def _record_exit(self, pos, reason):
        """Track exit for cooldown."""
        direction = 'CE' if 'CE' in pos.signal_type else 'PE'
        self._exit_history[pos.symbol].append({
            'time': datetime.now(), 'reason': reason,
            'direction': direction, 'strategy': pos.strategy,
        })

    # ── Main Loop ────────────────────────────────────────────────────────────

    def run(self):
        """Main stock trading loop."""
        self._running = True
        logger.info("[StockLiveTrader] Starting main loop...")

        def _sighandler(sig, frame):
            logger.info("[StockLiveTrader] Shutdown signal received")
            self._running = False
        signal.signal(signal.SIGINT, _sighandler)
        signal.signal(signal.SIGTERM, _sighandler)

        try:
            send_message(
                f"<b>STOCK {'SHADOW' if self.shadow_mode else 'LIVE'} TRADER STARTED</b>\n"
                f"Capital: Rs {STOCK_CAPITAL:,}\n"
                f"Stocks: {len(STOCK_UNIVERSE)} | Max positions: {MAX_POSITIONS}\n"
                f"Strategies: {', '.join(sorted(ACTIVE_STRATEGIES))}"
            )
        except Exception:
            pass

        last_scan = {}
        last_exit_check = datetime.now()
        scan_idx = 0  # Rotate through stocks to avoid scanning all at once

        while self._running:
            try:
                now = datetime.now()

                if now.time() < MARKET_OPEN_TIME:
                    time.sleep(30)
                    continue

                if now.time() >= MARKET_CLOSE_TIME:
                    logger.info("[StockLiveTrader] Market closed. EOD report.")
                    self._eod_report()
                    break

                # ── Check exits every 15s ──────────────────────────────────
                if (now - last_exit_check).total_seconds() >= PREMIUM_REFRESH_SECONDS:
                    self.check_exits()
                    self.write_status_file()
                    last_exit_check = now

                # ── Scan stocks in batches (3 per cycle to avoid API throttle) ──
                batch_size = 3
                for batch_i in range(batch_size):
                    idx = (scan_idx + batch_i) % len(STOCK_UNIVERSE)
                    symbol = STOCK_UNIVERSE[idx]

                    last = last_scan.get(symbol, datetime.min)
                    if (now - last).total_seconds() < SCAN_INTERVAL_SECONDS:
                        continue
                    last_scan[symbol] = now

                    if now.time() >= NO_NEW_ENTRY_AFTER:
                        continue
                    if now.time() < FIRST_TRADE_TIME:
                        continue

                    # Daily loss check
                    if self._daily_pnl <= -DAILY_LOSS_LIMIT:
                        continue

                    # ── v25: Get spot + feed to calculus + regime detector ──
                    spot = self.get_spot(symbol)
                    if not spot:
                        continue

                    if self.calculus:
                        try:
                            self.calculus.add_spot_tick(symbol, spot)
                        except Exception:
                            pass

                    if self.regime_detector:
                        try:
                            self.regime_detector.update(symbol, spot, self.current_vix)
                        except Exception:
                            pass

                    # Update VIX (once per batch)
                    if batch_i == 0:
                        vix_val = None
                        try:
                            vix_val = self.dhan_feed.get_spot('INDIA VIX')
                        except Exception:
                            pass
                        # v30.3: Dhan doesn't have INDIA VIX — fallback to Zerodha
                        if not vix_val and self.zerodha:
                            try:
                                vix_val = self.zerodha.get_spot('INDIA VIX')
                            except Exception:
                                pass
                        if vix_val and vix_val > 0:
                            self.current_vix = round(vix_val, 2)

                    # Generate signals
                    raw_signals = self.generate_signals(symbol)

                    # v30.3: Log scan status for visibility
                    spot_val = self.get_spot(symbol)
                    logger.info(f"[SCAN] {symbol} spot={spot_val or 'N/A'} | raw={len(raw_signals)} | "
                               f"positions={len(self.positions)} | trades_today={self._trade_count_today} | "
                               f"VIX={self.current_vix}")

                    # ── FILTER 1: Basic strategy + quality score ──
                    qualified = []
                    for sig in raw_signals:
                        strat = sig.get('strategy', '')
                        if strat not in ACTIVE_STRATEGIES:
                            continue
                        q = sig.get('quality_score', 0)
                        if q < MIN_SIGNAL_SCORE:
                            continue
                        qualified.append(sig)

                    if self._trade_count_today >= MAX_TRADES_PER_DAY:
                        qualified = []

                    # ── FILTER 2: CHOPPY SHIELD ──
                    if qualified:
                        is_choppy, day_eff = self._check_choppy_shield(symbol, spot)
                        if is_choppy:
                            logger.info(f"[CHOPPY_BLOCK] {symbol} eff={day_eff}% — "
                                       f"blocking {len(qualified)} signal(s)")
                            qualified = []

                    # ── FILTER 3: DCI GATE ──
                    if qualified:
                        vwap, ema9, ema20, three_bar_trend, open_price = self._get_dci_indicators(symbol, spot)
                        dci_passed = []
                        for sig in qualified:
                            try:
                                dci = compute_direction_confidence_stock(
                                    sig, spot, vwap, ema9, ema20, three_bar_trend,
                                    sig.get('pcr', 1.0), open_price,
                                    sig.get('iv', 0), self.current_vix)

                                if self.calculus and self.calculus.bar_count(symbol) >= 15:
                                    try:
                                        calc_sig = self.calculus.direction_signal(symbol, spot)
                                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                                        if calc_sig['direction'] == sig_dir:
                                            dci = min(100, dci + 10)
                                        elif calc_sig['direction'] and calc_sig['confidence'] > 30:
                                            dci = max(0, dci - 10)
                                    except Exception:
                                        pass

                                sig['_dci'] = dci
                                if dci < DCI_MIN_THRESHOLD:
                                    dci_detail = f"DCI={dci:.0f} < {DCI_MIN_THRESHOLD}"
                                    logger.info(f"[DCI_BLOCK] {symbol} {sig.get('type')} {dci_detail}")
                                    notify_signal_blocked("LIVE_STOCKS", symbol, sig.get('type',''), "DCI_GATE", dci_detail)
                                    continue
                                dci_passed.append(sig)
                            except Exception:
                                dci_passed.append(sig)
                        qualified = dci_passed

                    # ── FILTER 4: PHYSICS GATE ──
                    if qualified and self.calculus and self.calculus.bar_count(symbol) >= 6:
                        physics_passed = []
                        for sig in qualified:
                            try:
                                sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                                phy_score, phy_diag = self.calculus.physics_gate(symbol, sig_dir, spot)
                                sig['_physics_score'] = phy_score
                                if phy_score < 0:
                                    warnings = []
                                    if phy_diag.get('mom_dying'): warnings.append('MOM_DYING')
                                    if phy_diag.get('wave_peak'): warnings.append('WAVE_PEAK')
                                    if phy_diag.get('stretched'): warnings.append('VWAP_STRETCHED')
                                    if phy_diag.get('rsi_extreme'): warnings.append('RSI_EXTREME')
                                    block_detail = f"score={phy_score} [{','.join(warnings)}]"
                                    logger.info(f"[PHYSICS_BLOCK] {symbol} {sig.get('type')} {block_detail}")
                                    notify_signal_blocked("LIVE_STOCKS", symbol, sig.get('type',''), "PHYSICS_BLOCK", block_detail)
                                    continue
                                physics_passed.append(sig)
                            except Exception:
                                physics_passed.append(sig)
                        qualified = physics_passed

                    # ── Strategy Priority Dedup ──
                    if len(qualified) > 1:
                        qualified.sort(key=lambda s: STRATEGY_PRIORITY.get(s.get('strategy', ''), 0), reverse=True)
                        seen_dirs = {}
                        deduped = []
                        for sig in qualified:
                            sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                            key = f"{symbol}_{sig_dir}"
                            if key not in seen_dirs:
                                seen_dirs[key] = sig.get('strategy', '')
                                deduped.append(sig)
                        qualified = deduped

                    # ── Existing position check ──
                    final_signals = []
                    for sig in qualified:
                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                        existing = [p for p in self.positions if not p.closed
                                    and p.symbol == symbol
                                    and ('CE' if 'CE' in p.signal_type else 'PE') == sig_dir]
                        if existing:
                            if len(existing) >= MAX_SAME_DIRECTION_PER_SYMBOL:
                                logger.info(f"[STRAT_BLOCK] {symbol} {sig_dir} — max same-dir positions")
                                continue
                            profitable = [p for p in existing if p.unrealized_pnl > 0]
                            if not profitable:
                                continue
                        final_signals.append(sig)

                    # ── Cooldown check ──
                    executable = []
                    for sig in final_signals:
                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                        if self._check_cooldown(symbol, sig_dir, sig.get('strategy', '')):
                            executable.append(sig)

                    # ── Execute ──
                    for sig in executable:
                        regime_str = ''
                        if self.regime_detector:
                            try:
                                regime_str = f" Regime={self.regime_detector.get_regime(symbol).value}"
                            except Exception:
                                pass
                        logger.info(f"[SIGNAL] {symbol} {sig.get('strategy')} "
                                   f"{sig.get('type')} Q={sig.get('quality_score')} "
                                   f"DCI={sig.get('_dci', '?')} Phy={sig.get('_physics_score', '?')} "
                                   f"Premium=Rs {sig.get('premium', 0):.2f}{regime_str}")
                        self._daily_signals_log.append({
                            'time': now.isoformat(), 'symbol': symbol,
                            'strategy': sig.get('strategy'), 'type': sig.get('type'),
                            'quality_score': sig.get('quality_score'),
                            'dci': sig.get('_dci'), 'physics_score': sig.get('_physics_score'),
                        })
                        if self.execute_signal(sig):
                            self._trade_count_today += 1

                scan_idx = (scan_idx + batch_size) % len(STOCK_UNIVERSE)
                time.sleep(3)

            except KeyboardInterrupt:
                self._running = False
            except Exception as e:
                logger.error(f"[StockLiveTrader] Loop error: {e}", exc_info=True)
                time.sleep(5)

        logger.info("[StockLiveTrader] Shutting down...")
        if self.positions:
            logger.warning(f"{len(self.positions)} positions still open")

    # ── Reporting ────────────────────────────────────────────────────────────

    def _eod_report(self):
        """Generate end-of-day summary."""
        total_pnl = sum(p.realized_pnl for p in self.closed_positions)
        winners = [p for p in self.closed_positions if p.realized_pnl > 0]
        losers = [p for p in self.closed_positions if p.realized_pnl < 0]
        total_trades = len(self.closed_positions)
        win_rate = len(winners) / total_trades * 100 if total_trades > 0 else 0

        report = (
            f"\n{'='*60}\n"
            f"  STOCK EOD REPORT — {datetime.now().strftime('%Y-%m-%d')}\n"
            f"  Mode: {'SHADOW' if self.shadow_mode else 'LIVE'}\n"
            f"{'='*60}\n"
            f"  Total trades: {total_trades}\n"
            f"  Winners: {len(winners)} | Losers: {len(losers)}\n"
            f"  Win rate: {win_rate:.1f}%\n"
            f"  Total P&L: Rs {total_pnl:+,.0f}\n"
            f"{'='*60}\n"
        )

        for p in self.closed_positions:
            report += (f"  {p.symbol:12s} {p.strategy:16s} {p.signal_type:12s} "
                      f"Entry=Rs {p.entry_premium:7.2f} Exit=Rs {p.exit_premium:7.2f} "
                      f"PnL=Rs {p.realized_pnl:+8,.0f} [{p.exit_reason}]\n")

        logger.info(report)

        try:
            send_message(
                f"<b>STOCK {'SHADOW' if self.shadow_mode else 'LIVE'} EOD</b>\n"
                f"Trades: {total_trades} | WR: {win_rate:.0f}%\n"
                f"P&L: Rs {total_pnl:+,.0f}\n"
                f"Signals: {len(self._daily_signals_log)}"
            )
        except Exception:
            pass

        self._save_eod_report()

    def _save_trade_log(self, pos, action):
        """Save trade to JSON log."""
        log_file = os.path.join(STOCK_LIVE_DIR, f"stock_trades_{datetime.now().strftime('%Y%m%d')}.json")
        entry = {'action': action, 'timestamp': datetime.now().isoformat(), **pos.to_dict()}
        try:
            existing = []
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    existing = json.load(f)
            existing.append(entry)
            with open(log_file, 'w') as f:
                json.dump(existing, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[LOG] Failed: {e}")

    def _save_eod_report(self):
        """Save EOD summary."""
        report_file = os.path.join(STOCK_LIVE_DIR, f"stock_eod_{datetime.now().strftime('%Y%m%d')}.json")
        report = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'mode': 'SHADOW' if self.shadow_mode else 'LIVE',
            'total_trades': len(self.closed_positions),
            'total_pnl': sum(p.realized_pnl for p in self.closed_positions),
            'trades': [p.to_dict() for p in self.closed_positions],
        }
        try:
            with open(report_file, 'w') as f:
                json.dump(report, f, indent=2, default=str)
        except Exception:
            pass

    def get_status(self):
        """Get current status for monitoring."""
        total_realized = sum(p.realized_pnl for p in self.closed_positions)
        total_unrealized = sum(p.unrealized_pnl for p in self.positions)
        total_closed = len(self.closed_positions)
        winners = [p for p in self.closed_positions if p.realized_pnl > 0]

        return {
            'version': 'v25',
            'mode': 'SHADOW' if self.shadow_mode else 'LIVE',
            'capital': STOCK_CAPITAL,
            'stocks_monitored': len(STOCK_UNIVERSE),
            'positions': [p.to_dict() for p in self.positions],
            'closed_trades': [p.to_dict() for p in self.closed_positions],
            'open_count': len(self.positions),
            'trades_today': self._trade_count_today,
            'unrealized_pnl': round(total_unrealized, 2),
            'realized_pnl': round(total_realized, 2),
            'total_pnl': round(total_realized + total_unrealized, 2),
            'win_rate': round(len(winners) / total_closed * 100, 1) if total_closed > 0 else 0,
            'timestamp': datetime.now().isoformat(),
        }

    def write_status_file(self):
        """Write status JSON for dashboard."""
        status = self.get_status()
        status_file = os.path.join(STOCK_LIVE_DIR, 'stock_live_status.json')
        try:
            with open(status_file, 'w') as f:
                json.dump(status, f, indent=2, default=str)
        except Exception:
            pass


# ── Entry Point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Stock Live Trading Bot')
    parser.add_argument('--live', action='store_true',
                        help='LIVE mode — places real orders')
    parser.add_argument('--shadow', action='store_true', default=True,
                        help='Shadow mode — log only (DEFAULT)')
    parser.add_argument('--status', action='store_true',
                        help='Show status and exit')
    args = parser.parse_args()

    shadow_mode = not args.live

    if args.live:
        print("\n" + "="*60)
        print("  WARNING: STOCK LIVE TRADING — REAL MONEY AT RISK")
        print(f"  Capital: Rs {STOCK_CAPITAL:,}")
        print(f"  Daily loss limit: Rs {DAILY_LOSS_LIMIT:,}")
        print("="*60)
        confirm = input("  Type 'YES I CONFIRM' to proceed: ")
        if confirm.strip() != 'YES I CONFIRM':
            print("  Aborted. Use --shadow for safe mode.")
            sys.exit(0)

    if args.status:
        trader = StockLiveTrader(shadow_mode=True)
        status = trader.get_status()
        print(json.dumps(status, indent=2, default=str))
        sys.exit(0)

    trader = StockLiveTrader(shadow_mode=shadow_mode)
    trader.run()


if __name__ == '__main__':
    main()
