"""
Live Trading Bot v25 — Equity Index Options (NIFTY, BANKNIFTY, SENSEX)
======================================================================
MC-Optimal parameters from 13-day backtest (Mar 11-24, 2026).
Strategies: CPR (72% WR) + Wave (85% WR) + Gamma Blast (77% WR)
Capital: Rs 1,00,000 | 1 lot per trade | Peak capital governs positions

Architecture:
  Signal generation : Standalone (CPR + Wave + Gamma Blast, no paper_trader dependency)
  Data source       : Dhan (primary) + Zerodha (fallback)
  Order execution   : dhan_broker.py (LIMIT orders only — SEBI Apr 2026)
  Risk management   : risk_controller.py (daily loss cap, kill switch, etc.)
  Position tracking : LivePortfolio (reconciled with Dhan every 60s)
  OI Exit signals   : oi_heatmap.py (5-factor confidence scoring)
  V-Reversal detect : reversal_detector.py (smart DIRECTION_FLIP re-entry)

Safety filters (v25):
  Physics Gate      : Blocks dying momentum, wave peaks, VWAP stretch, RSI extreme
  DCI Gate          : Direction Confidence Index ≥ 40 required (8-factor scoring)
  Choppy Shield     : Blocks entries after 11:00 on choppy days (eff < 15%)
  Regime Detector   : Adaptive TSL (TRENDING=wide trail, FLAT=tight trail)

Modes:
  --shadow    : Generate signals + log orders, but don't place them (DEFAULT)
  --live      : Place real orders (requires explicit flag)

Usage:
    python live_trader.py                  # Shadow mode (safe)
    python live_trader.py --shadow         # Same as above
    python live_trader.py --live           # REAL MONEY — requires confirmation
    python live_trader.py --status         # Show risk status and positions
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
LIVE_TRADES_DIR = os.path.join(BASE_DIR, 'live_trades')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(LIVE_TRADES_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"live_trader_{datetime.now().strftime('%Y%m%d')}.log")
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

# ── Imports from existing codebase (NO paper_trader dependency) ─────────────
from dhan_broker import DhanBroker, EXCHANGE_SEGMENT_MAP
from dhan_feed import DhanFeed, INDEX_MAP
from risk_controller import RiskController

try:
    from ghost_zone_v8 import GhostZoneV8
except ImportError:
    GhostZoneV8 = None

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

# ── Configuration — v24 MC-Optimal ──────────────────────────────────────────
LIVE_CAPITAL = 300000               # v30.3: Rs 3L (matches paper bot capital)
DAILY_LOSS_LIMIT = 22500            # v30.3: Rs 22.5K circuit breaker (7.5% of 3L)
MAX_POSITIONS = 999                 # Peak capital governs (v24)
MAX_LOTS_PER_TRADE = 1              # v30.3: 1 lot (shadow mode — track signals, not maximize profit)
MAX_LOTS = {'NIFTY': 1, 'BANKNIFTY': 1, 'SENSEX': 1}
MAX_EQUITY_PER_TRADE = 100000       # v30.3: Rs 1L per trade (1-lot BANKNIFTY at Rs 1800 × 30 = Rs 54K)
MIN_MARGIN_BUFFER = 15000           # Keep Rs 15K free at all times
MAX_SAME_DIRECTION_PER_SYMBOL = 1   # v30.3: 1 position per symbol/direction (no lot-add in shadow mode)
MAX_TRADES_PER_DAY = 24             # v24 MC-optimal

LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}

# Signal quality filter (v24 MC-optimal — lowered from 83)
MIN_SIGNAL_SCORE = 50

# Active strategies for live (v24 — Wave re-enabled)
ACTIVE_STRATEGIES = {'CPR', 'Wave', 'Gamma Blast'}

# Removed strategies (v24 — Wave RE-ENABLED, WaveBC still removed)
REMOVED_STRATEGIES = {'WaveBC', 'PCR+VWAP'}

# Strategy priority for dedup (v24 — higher = preferred)
STRATEGY_PRIORITY = {
    'CPR': 100,
    'Wave': 95,
    'Trend Rider': 90,
    'Gamma Blast': 85,
    'Liquidity Sweep': 80,
    'Ghost Zone v8': 70,
}

# Strategy allocation (v24 MC-optimal)
STRATEGY_ALLOCATION = {
    'CPR': 0.40,
    'Wave': 0.30,
    'Gamma Blast': 0.20,
    'Ghost Zone v8': 0.05,
    'Liquidity Sweep': 0.05,
}

# Cooldowns (v24 MC-optimal)
REENTRY_COOLDOWN_SECONDS = 300      # 5 min (reduced from 600s)
DIRECTION_FLIP_COOLDOWN_SECONDS = 180  # 3 min (v24)

# Timing
SCAN_INTERVAL_SECONDS = 30         # Check for signals every 30s
PREMIUM_REFRESH_SECONDS = 10       # Refresh option LTP every 10s
RECONCILE_INTERVAL_SECONDS = 60    # Reconcile with Dhan every 60s
PRE_MARKET_TIME = dtime(8, 45)
MARKET_OPEN_TIME = dtime(9, 15)
MARKET_CLOSE_TIME = dtime(15, 30)
EOD_SQUAREOFF_TIME = dtime(15, 20)
NO_NEW_ENTRY_AFTER = dtime(15, 0)  # No new entries after 3 PM

# Exit parameters (v24 MC-optimal — all validated by MC backtest)
GRACE_PERIOD_SECONDS = 180
PARTIAL_BOOK_GAIN_PCT = 30          # v27: MC-optimal 30% target
TSL_BREAKEVEN_TRIGGER_PCT = 5       # v27: MC-optimal — lock breakeven at +5%
TSL_TRAIL_TRIGGER_PCT = 3           # v27: MC-optimal — start trailing at +3%
PEAK_TRAIL_TRIGGER_PCT = 2          # v27: MC-optimal — peak trail at +2%
PARTIAL_BOOK_TSL_DISTANCE_PCT = 3   # v27: MC-optimal 3% trail from peak
PARTIAL_SL_LOSS_PCT = 15            # v27: MC-optimal 15% SL
PARTIAL_SL_FULL_PCT = 20            # v27: MC-optimal full exit at -20%
MIN_PREMIUM_BUY = 15
MIN_PREMIUM_BUY_BN = 40
CPR_MIN_WIDTH_PCT = 0.03             # Min CPR width to generate signal

# v25 Safety filters (from paper_trader proven parameters)
# v31: MC-optimal params via PARAM_SET env var
_PARAM_SET = os.environ.get('PARAM_SET', 'current')
if _PARAM_SET == 'mc_optimal':
    DCI_MIN_THRESHOLD = 48           # MC-optimal: DCI>=48 (Sharpe 6.62)
    CHOPPY_DAY_EFF_THRESHOLD = 20    # MC-optimal: 20% (Sharpe 3.29)
else:
    DCI_MIN_THRESHOLD = 40           # Minimum Direction Confidence Index (0-100)
    CHOPPY_DAY_EFF_THRESHOLD = 15    # Block entries if 11:00+ efficiency < 15%
CHOPPY_DAY_BLOCK_AFTER = dtime(11, 0)  # Choppy shield kicks in after 11:00 AM

# Brokerage cost model
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18


def compute_direction_confidence_live(sig, spot, vwap, ema9, ema20, three_bar_trend,
                                      pcr, open_price, iv, vix=None):
    """v25: Direction Confidence Index — adapted from paper_trader.py for live bot.

    Uses available live market data (no full greeks required).
    Factors: VWAP(±10), EMA trend(±12), 3-bar momentum(±8), PCR(±8),
             IV strength(±10), Intraday body(±5).
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
    # v30.5: Fixed — was missing lower cap, negative trends subtracted unlimited points
    if three_bar_trend != 0:
        if is_ce:
            score += min(max(three_bar_trend * 3, -8), 8)
        else:
            score += min(max(-three_bar_trend * 3, -8), 8)

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

    final = max(min(score, 100), 0)

    # v30.5: Store diagnostics for monitoring
    sig['_dci_diag'] = {
        'base': 50, 'vwap_d': round(vwap_dist_pct * 5, 1) if vwap and vwap > 0 else 0,
        'ema_d': round(ema_gap_pct * 10, 1) if ema9 and ema20 and ema20 > 0 else 0,
        'mom_d': three_bar_trend * 3 if three_bar_trend != 0 else 0,
        'body_pct': round(body_pct, 2) if open_price and open_price > 0 else 0,
        'open_used': round(open_price, 1) if open_price else 0,
        'raw': round(final, 1),
    }

    return final


class LivePosition:
    """Tracks a single live trading position."""

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
        self.target_order_id = None
        self.partial_booked = False
        self.partial_sl_done = False
        self.closed = False
        self.exit_premium = 0
        self.exit_reason = ''
        self.exit_time = None
        self.realized_pnl = 0

        # Signal metadata (for dashboard)
        self.quality_score = 0
        self.data_source = 'DHAN'       # DHAN or ZERODHA
        self.signal_reason = ''
        self.indicator_levels = {}       # Strategy-specific: {pivot, tc, bc, swing_high, swing_low, vwap, ...}
        self.iv_at_entry = 0
        self.pcr_at_entry = 0

        # Premium tick history for chart (v24 dashboard)
        self.premium_ticks = []  # [{time: epoch_s, price: float}]
        self._current_candle = None
        self.candles_1m = []  # [{time, open, high, low, close}]
        self._record_tick(entry_premium)  # Must be AFTER _current_candle init

    def _record_tick(self, premium):
        """Record a premium tick for chart generation."""
        now = datetime.now()
        epoch = int(now.timestamp())
        self.premium_ticks.append({'time': epoch, 'price': premium})

        # Build 1-min candle
        candle_time = epoch - (epoch % 60)  # Floor to minute
        if self._current_candle is None or self._current_candle['time'] != candle_time:
            # New candle
            if self._current_candle is not None:
                self.candles_1m.append(self._current_candle)
            self._current_candle = {
                'time': candle_time,
                'open': premium, 'high': premium,
                'low': premium, 'close': premium,
            }
        else:
            self._current_candle['high'] = max(self._current_candle['high'], premium)
            self._current_candle['low'] = min(self._current_candle['low'], premium)
            self._current_candle['close'] = premium

    def update_premium(self, premium):
        """Update current premium and record tick."""
        self.current_premium = premium
        if premium > self.peak_premium:
            self.peak_premium = premium
        self._record_tick(premium)

    def get_chart_data(self):
        """Return candle data + levels + indicator overlays for chart rendering."""
        candles = list(self.candles_1m)
        if self._current_candle:
            candles.append(self._current_candle)
        return {
            'candles': candles,
            'entry_price': self.entry_premium,
            'target': self.target,
            'sl': self.sl,
            'trailing_sl': self.trailing_sl,
            'entry_time': int(self.entry_time.timestamp()),
            'exit_price': self.exit_premium if self.closed else None,
            'exit_time': int(self.exit_time.timestamp()) if self.exit_time else None,
            'strategy': self.strategy,
            'indicator_levels': self.indicator_levels,  # CPR, swing, VWAP etc.
        }

    @property
    def unrealized_pnl(self):
        if self.closed:
            return self.realized_pnl
        diff = self.current_premium - self.entry_premium
        return diff * self.quantity

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

    def to_dict(self, include_chart=False):
        d = {
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
            'sl_order_id': self.sl_order_id,
            'security_id': self.security_id,
            'closed': self.closed, 'exit_reason': self.exit_reason,
            'exit_premium': self.exit_premium,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'realized_pnl': self.realized_pnl,
            # Signal metadata
            'quality_score': self.quality_score,
            'data_source': self.data_source,
            'signal_reason': self.signal_reason,
            'iv_at_entry': self.iv_at_entry,
            'pcr_at_entry': round(self.pcr_at_entry, 2) if self.pcr_at_entry else 0,
            'indicator_levels': self.indicator_levels,
        }
        if include_chart:
            d['chart'] = self.get_chart_data()
        return d


class LiveTrader:
    """Main live trading orchestrator."""

    def __init__(self, shadow_mode=True):
        self.shadow_mode = shadow_mode
        self.positions = []         # List of LivePosition
        self.closed_positions = []  # Today's closed trades
        self._running = False
        self._trade_counter = 0
        self._daily_signals_log = []

        # ── Initialize components ────────────────────────────────────────────
        mode = "SHADOW" if shadow_mode else "🔴 LIVE"
        logger.info(f"{'='*60}")
        logger.info(f"  LIVE TRADER v25 — {mode} MODE")
        logger.info(f"  Capital: Rs {LIVE_CAPITAL:,} | Loss limit: Rs {DAILY_LOSS_LIMIT:,}")
        logger.info(f"  Max lots: {MAX_LOTS_PER_TRADE} | Max trades/day: {MAX_TRADES_PER_DAY}")
        logger.info(f"  Strategies: {', '.join(sorted(ACTIVE_STRATEGIES))}")
        logger.info(f"  Priority: CPR(100) > Wave(95) > Gamma Blast(85)")
        logger.info(f"  OI Heatmap: {'ON' if OIHeatmap else 'OFF'} | V-Reversal: {'ON' if ReversalDetector else 'OFF'}")
        logger.info(f"  Physics Gate: {'ON' if MarketCalculus else 'OFF'} | DCI Gate: ON (min {DCI_MIN_THRESHOLD})")
        logger.info(f"  Choppy Shield: ON (eff<{CHOPPY_DAY_EFF_THRESHOLD}% after 11:00)")
        logger.info(f"  Regime Detector: {'ON' if RegimeDetector else 'OFF'} (adaptive TSL)")
        logger.info(f"  Order type: LIMIT only (SEBI Apr 2026)")
        logger.info(f"{'='*60}")

        # Broker (order execution)
        self.broker = DhanBroker(shadow_mode=shadow_mode)
        if not self.broker.connect():
            logger.error("FATAL: Cannot connect to Dhan broker")
            sys.exit(1)

        # Data feed (Dhan primary, Zerodha fallback)
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

        # Risk controller
        self.risk = RiskController(
            capital=LIVE_CAPITAL,
            daily_loss_limit=DAILY_LOSS_LIMIT,
            max_positions=MAX_POSITIONS,
            max_lots_per_trade=MAX_LOTS_PER_TRADE,
            min_margin_buffer=MIN_MARGIN_BUFFER,
        )

        # Ghost Zone detector (optional — not in v24 active strategies)
        self.ghost_v8 = None
        if GhostZoneV8:
            try:
                self.ghost_v8 = GhostZoneV8()
            except Exception:
                pass

        # OI Heatmap exit (v24)
        self.oi_heatmap = None
        if OIHeatmap:
            try:
                self.oi_heatmap = OIHeatmap(dhan_feed=self.dhan_feed,
                                            zerodha_feed=self.zerodha)
                logger.info("[OI] OIHeatmap initialized for exit signals")
            except Exception as e:
                logger.warning(f"[OI] OIHeatmap init failed: {e}")

        # V-Reversal detector (v24)
        self.reversal_detector = None
        if ReversalDetector:
            try:
                self.reversal_detector = ReversalDetector()
                logger.info("[Reversal] ReversalDetector initialized")
            except Exception as e:
                logger.warning(f"[Reversal] ReversalDetector init failed: {e}")

        # v25: MarketCalculus (Physics Gate + DCI calculus boost)
        self.calculus = None
        if MarketCalculus:
            try:
                self.calculus = MarketCalculus()
                logger.info("[Calculus] MarketCalculus initialized (Physics Gate + DCI)")
            except Exception as e:
                logger.warning(f"[Calculus] MarketCalculus init failed: {e}")

        # v25: Regime Detector (adaptive TSL + Choppy Shield)
        self.regime_detector = None
        if RegimeDetector:
            try:
                self.regime_detector = RegimeDetector()
                logger.info("[Regime] RegimeDetector initialized (adaptive TSL)")
            except Exception as e:
                logger.warning(f"[Regime] RegimeDetector init failed: {e}")

        # v25: Track VIX for DCI and regime
        self.current_vix = 0

        # Cooldown tracking (v24)
        self._exit_history = defaultdict(list)  # symbol → [{'time': dt, 'reason': str, 'direction': str}]
        self._trade_count_today = 0

        # Strategy engine (standalone — no paper_trader dependency)
        self._init_strategy_engine()

        logger.info("[LiveTrader] v25 All components initialized")

    def _init_strategy_engine(self):
        """Initialize standalone strategy engine — NO paper_trader dependency.

        Live bot is 100% independent. Strategies: CPR, Wave, Gamma Blast.
        """
        # Spot bar history for Wave detection (symbol → list of {time, open, high, low, close})
        self._spot_bars = defaultdict(list)
        self._last_bar_time = {}
        # CPR levels cache (computed once at 9:15)
        self._cpr_levels = {}
        # Yesterday OHLC cache
        self._prev_ohlc = {}
        # v30.4: Today's opening price per symbol — fetch from Dhan intraday on restart
        self._today_open = {}
        self._fetch_today_open_from_dhan()
        # Wave state tracking
        self._wave_state = defaultdict(lambda: {'trend': None, 'waves': [], 'last_signal_time': None})
        # Gamma Blast state
        self._gamma_last_signal = {}
        logger.info("[Strategy] Standalone engine initialized (CPR + Wave + Gamma Blast)")

    def _fetch_today_open_from_dhan(self):
        """v30.4: Fetch today's 9:15 opening price from Dhan intraday minute data.
        Needed on mid-day restarts so intraday body calculation is correct.
        Without this, body = spot - spot = 0, which bypasses direction filter.
        """
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
                config = INDEX_MAP.get(sym)
                if not config:
                    continue
                intra = self.dhan_feed.dhan.intraday_minute_data(
                    security_id=str(config['security_id']),
                    exchange_segment=config['exchange_segment'],
                    instrument_type='INDEX',
                    from_date=today,
                    to_date=today,
                )
                raw = intra.get('data', {}) if intra and intra.get('status') == 'success' else {}
                opens = raw.get('open', [])
                if opens:
                    self._today_open[sym] = float(opens[0])
                    logger.info(f"[OPEN] {sym} today_open={opens[0]} (from Dhan 9:15 bar)")
        except Exception as e:
            logger.warning(f"[OPEN] Failed to fetch today's open from Dhan: {e}")

    # ── Data Methods ─────────────────────────────────────────────────────────

    def get_spot(self, symbol):
        """Get current spot price. Dhan primary, Zerodha fallback."""
        spot = self.dhan_feed.get_spot(symbol)
        if spot and spot > 0:
            return spot
        if self.zerodha:
            try:
                spot = self.zerodha.get_spot(symbol)
                if spot and spot > 0:
                    return spot
            except Exception:
                pass
        return None

    def get_option_chain(self, symbol):
        """Get option chain. Dhan primary, Zerodha fallback."""
        chain = self.dhan_feed.get_option_chain(symbol)
        if chain and len(chain.get('CE', [])) > 0:
            return chain
        if self.zerodha:
            try:
                chain = self.zerodha.get_option_chain(symbol)
                if chain:
                    return chain
            except Exception:
                pass
        return None

    def get_option_ltp_and_security_id(self, symbol, strike, opt_type):
        """Get live LTP and identifier for an option contract.

        v30.3: Accept Zerodha token/tradingsymbol as fallback when Dhan
        security_id is unavailable (shadow bots skip Dhan chain).

        Returns:
            (ltp, security_id, iv) or (0, None, 0) if unavailable.
        """
        chain = self.get_option_chain(symbol)
        if not chain:
            return 0, None, 0

        for c in chain.get(opt_type, []):
            if int(c.get('strike', 0)) == int(strike):
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
        """Get ATM strike with live LTP and security_id from chain.

        Returns:
            (strike, ltp, security_id, iv) or (0, 0, None, 0)
        """
        interval = STRIKE_INTERVALS.get(symbol, 100)
        atm_strike = round(spot / interval) * interval

        # For CE: use ATM or 1 strike OTM. For PE: same.
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
        """Compute Put-Call Ratio from option chain."""
        try:
            ce_oi = sum(float(c.get('oi', 0)) for c in chain.get('CE', []))
            pe_oi = sum(float(c.get('oi', 0)) for c in chain.get('PE', []))
            return round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0
        except Exception:
            return 0

    def _get_vwap(self, symbol):
        """Compute VWAP from spot bars (volume-weighted avg price approximation)."""
        bars = self._spot_bars.get(symbol, [])
        if len(bars) < 2:
            return 0
        # Simple VWAP approx: cumulative (typical_price) / count
        tp_sum = 0
        count = 0
        for b in bars:
            if b['close'] > 0:
                tp = (b['high'] + b['low'] + b['close']) / 3
                tp_sum += tp
                count += 1
        return round(tp_sum / count, 2) if count > 0 else 0

    def _get_market_data(self):
        """Collect live market indicators for dashboard."""
        mkt = {}
        for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            spot = self.get_spot(symbol)
            chain = self.get_option_chain(symbol)
            pcr = self._get_pcr(chain) if chain else 0
            vwap = self._get_vwap(symbol)

            # Get ATM IV
            atm_iv = 0
            if spot and chain:
                interval = STRIKE_INTERVALS.get(symbol, 100)
                atm = round(spot / interval) * interval
                for c in chain.get('CE', []):
                    if int(c.get('strike', 0)) == atm:
                        atm_iv = float(c.get('iv', 0))
                        break

            # Total OI
            total_oi = 0
            if chain:
                total_oi = sum(float(c.get('oi', 0)) for c in chain.get('CE', [])) + \
                           sum(float(c.get('oi', 0)) for c in chain.get('PE', []))

            mkt[symbol] = {
                'spot': spot or 0,
                'pcr': pcr,
                'atm_iv': round(atm_iv, 1),
                'total_oi': int(total_oi),
                'vwap': vwap,
            }

        # India VIX from Dhan or Zerodha
        vix = 0
        try:
            vix_spot = self.dhan_feed.get_spot('INDIA VIX')
            if vix_spot and vix_spot > 0:
                vix = round(vix_spot, 2)
        except Exception:
            pass
        if not vix and self.zerodha:
            try:
                vix = round(self.zerodha.get_spot('INDIA VIX') or 0, 2)
            except Exception:
                pass
        mkt['vix'] = vix
        return mkt

    # ── v25 Safety Filter Helpers ────────────────────────────────────────────

    def _get_dci_indicators(self, symbol, spot):
        """Build indicator dict for DCI computation from spot bars."""
        bars = self._spot_bars.get(symbol, [])
        closes = [b['close'] for b in bars if b['close'] > 0]
        vwap = self._get_vwap(symbol)

        # EMA approximation from spot bars (simple moving average as proxy)
        ema9 = float(np.mean(closes[-9:])) if len(closes) >= 9 else spot
        ema20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else spot

        # 3-bar trend: count direction of last 3 closes
        three_bar_trend = 0
        if len(closes) >= 4:
            for i in range(-3, 0):
                if closes[i] > closes[i - 1]:
                    three_bar_trend += 1
                elif closes[i] < closes[i - 1]:
                    three_bar_trend -= 1

        # v30.5: Use real 9:15 open from Dhan, not first bar close after restart
        open_price = self._today_open.get(symbol, closes[0] if closes else spot)

        return vwap, ema9, ema20, three_bar_trend, open_price

    def _check_choppy_shield(self, symbol, spot):
        """v25 Choppy Shield: Block new entries after 11:00 if efficiency < 15%.

        Efficiency = |spot - open| / (high - low) * 100.
        Low efficiency = market going nowhere → save capital.

        v30.5: Use _today_open (Dhan 9:15 bar) for session_open instead of
        first bar close from _spot_bars. After mid-day restarts, _spot_bars
        only has recent bars causing wildly unstable efficiency (3.7% → 30%
        on 10-pt move). Also track intraday high/low persistently.

        Returns:
            (bool, float): (is_choppy, efficiency_pct)
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

        # v30.5: Use real 9:15 open (from Dhan), not first bar close after restart
        session_open = self._today_open.get(symbol, closes[0])

        # v30.5: Track persistent intraday high/low across restarts
        if not hasattr(self, '_intraday_hl'):
            self._intraday_hl = {}
        hl_key = symbol
        bar_high = max(highs)
        bar_low = min(lows)
        if hl_key in self._intraday_hl:
            self._intraday_hl[hl_key] = (
                max(self._intraday_hl[hl_key][0], bar_high, spot),
                min(self._intraday_hl[hl_key][1], bar_low, spot)
            )
        else:
            # On first call, seed with session_open range
            self._intraday_hl[hl_key] = (
                max(bar_high, spot, session_open),
                min(bar_low, spot, session_open)
            )
        day_high, day_low = self._intraday_hl[hl_key]
        day_range = day_high - day_low

        if day_range <= 0:
            return True, 0.0

        net_move = abs(spot - session_open)
        efficiency = net_move / day_range * 100

        return efficiency < CHOPPY_DAY_EFF_THRESHOLD, round(efficiency, 1)

    # ── Signal Generation (STANDALONE — no paper_trader dependency) ─────────

    def _update_spot_bar(self, symbol, spot):
        """Build 1-min spot bars for Wave detection."""
        now = datetime.now()
        bar_time = now.replace(second=0, microsecond=0)
        bars = self._spot_bars[symbol]

        if symbol not in self._last_bar_time or self._last_bar_time[symbol] != bar_time:
            # New bar
            if bars and bars[-1]['close'] == 0:
                bars[-1]['close'] = spot
            bars.append({'time': bar_time, 'open': spot, 'high': spot, 'low': spot, 'close': 0})
            self._last_bar_time[symbol] = bar_time
            # Keep last 60 bars
            if len(bars) > 60:
                self._spot_bars[symbol] = bars[-60:]
        else:
            # Update current bar
            if bars:
                bars[-1]['high'] = max(bars[-1]['high'], spot)
                bars[-1]['low'] = min(bars[-1]['low'], spot)
                bars[-1]['close'] = spot

    def _compute_signal_quality(self, symbol, strategy, spot, premium, iv, chain):
        """Standalone signal quality scoring (0-100).

        Factors: IV rank, premium level, OI support, spread tightness.
        """
        score = 60  # Base score

        # IV bonus: moderate IV (15-35) is ideal
        if iv:
            if 15 <= iv <= 35:
                score += 15
            elif 10 <= iv <= 45:
                score += 8

        # Premium level: sweet spot Rs 80-300
        if 80 <= premium <= 300:
            score += 10
        elif 40 <= premium <= 500:
            score += 5

        # OI support from chain
        try:
            ce_oi = sum(float(c.get('oi', 0)) for c in chain.get('CE', []))
            pe_oi = sum(float(c.get('oi', 0)) for c in chain.get('PE', []))
            if ce_oi > 0 and pe_oi > 0:
                pcr = pe_oi / ce_oi
                # PCR > 1 = bullish, < 0.7 = bearish
                if ('CE' in strategy or strategy == 'CPR') and pcr > 1.0:
                    score += 10
                elif ('PE' in strategy) and pcr < 0.8:
                    score += 10
                else:
                    score += 3
        except Exception:
            pass

        # Strategy bonus
        if strategy == 'CPR':
            score += 5  # Most reliable
        elif strategy == 'Wave':
            score += 3

        return min(100, max(0, score))

    def generate_signals(self, symbol):
        """Generate trading signals — fully standalone, no paper_trader imports.

        Strategies: CPR (breakout), Wave (momentum), Gamma Blast (expiry).
        Data: Dhan (primary) + Zerodha (fallback).

        Returns: list of signal dicts
        """
        spot = self.get_spot(symbol)
        if not spot:
            return []

        chain = self.get_option_chain(symbol)
        if not chain:
            return []

        # Update spot bar history for Wave detection
        self._update_spot_bar(symbol, spot)

        signals = []

        # CPR signals (always active)
        cpr_signals = self._check_cpr(symbol, spot, chain)
        signals.extend(cpr_signals)

        # Wave signals (momentum breakout, v24 re-enabled)
        wave_signals = self._check_wave(symbol, spot, chain)
        signals.extend(wave_signals)

        # Gamma Blast signals (expiry day only)
        gamma_signals = self._check_gamma_blast(symbol, spot, chain)
        signals.extend(gamma_signals)

        return signals

    def _check_cpr(self, symbol, spot, chain):
        """CPR strategy check — simplified from paper_trader.check_cpr_signals."""
        signals = []

        # v30.4: Cache CPR values per symbol per day — Dhan API is intermittent
        # (~50% of calls return empty). Cache once, use all day.
        if not hasattr(self, '_cpr_cache'):
            self._cpr_cache = {}

        cache_key = f"{symbol}_{date.today().isoformat()}"
        cached = self._cpr_cache.get(cache_key)

        if cached:
            h, l, c = cached['h'], cached['l'], cached['c']
        else:
            # Fetch from Dhan historical_daily_data
            try:
                hist = self.dhan_feed.dhan.historical_daily_data(
                    security_id=str(INDEX_MAP[symbol]['security_id']),
                    exchange_segment=INDEX_MAP[symbol]['exchange_segment'],
                    instrument_type='INDEX',
                    from_date=(datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'),
                    to_date=datetime.now().strftime('%Y-%m-%d'),
                )
                raw_data = hist.get('data', {}) if hist and hist.get('status') == 'success' else {}
                opens = raw_data.get('open', []) if isinstance(raw_data, dict) else []
                if len(opens) < 1:
                    logger.info(f"[CPR] {symbol} Dhan returned 0 bars — will retry next cycle")
                    return signals

                h = float(raw_data['high'][-1])
                l = float(raw_data['low'][-1])
                c = float(raw_data['close'][-1])
                self._cpr_cache[cache_key] = {'h': h, 'l': l, 'c': c}
                logger.info(f"[CPR_CACHE] {symbol} cached prev day OHLC: H={h:.0f} L={l:.0f} C={c:.0f}")
            except Exception as e:
                logger.warning(f"[CPR] Historical data error for {symbol}: {e}")
                return signals

        if h <= 0 or l <= 0 or c <= 0:
            return signals

        today_open = self._today_open.get(symbol, spot)

        # CPR calculation
        pivot = (h + l + c) / 3
        bc = (h + l) / 2
        tc = (pivot - bc) + pivot
        cpr_w = abs(tc - bc) / pivot * 100

        logger.info(f"[CPR] {symbol} CPR={cpr_w:.3f}% P={pivot:.0f} TC={tc:.0f} BC={bc:.0f} "
                    f"Spot={spot:.0f} H={h:.0f} L={l:.0f} C={c:.0f}")

        if cpr_w < CPR_MIN_WIDTH_PCT:
            logger.info(f"[CPR] {symbol} CPR too narrow ({cpr_w:.3f}% < {CPR_MIN_WIDTH_PCT}%) — skipped")
            return signals

        # Target/SL based on CPR width
        if cpr_w < 0.3:
            target_mult, sl_mult = 1.5, 0.5
        elif cpr_w <= 0.6:
            target_mult, sl_mult = 1.4, 0.5
        else:
            target_mult, sl_mult = 1.3, 0.5

        # Compute supplementary indicators
        intraday_body = spot - today_open
        pcr = self._get_pcr(chain)
        vwap = self._get_vwap(symbol)

        # Camarilla levels (for chart overlay)
        r1 = c + (h - l) * 1.1 / 12
        r2 = c + (h - l) * 1.1 / 6
        r3 = c + (h - l) * 1.1 / 4
        s1 = c - (h - l) * 1.1 / 12
        s2 = c - (h - l) * 1.1 / 6
        s3 = c - (h - l) * 1.1 / 4

        cpr_indicator_levels = {
            'pivot': round(pivot, 1), 'tc': round(tc, 1), 'bc': round(bc, 1),
            'r1': round(r1, 1), 'r2': round(r2, 1), 'r3': round(r3, 1),
            's1': round(s1, 1), 's2': round(s2, 1), 's3': round(s3, 1),
            'vwap': vwap, 'today_open': round(today_open, 1),
        }

        # Cache CPR levels for this symbol
        self._cpr_levels[symbol] = cpr_indicator_levels

        # BUY CE: spot above TC
        if spot > tc:
            if intraday_body < 0:  # Bearish body = skip
                logger.info(f"[CPR] {symbol} CE above TC but bearish intraday (body={intraday_body:+.0f})")
                return signals
            opt_type = 'CE'
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, opt_type)
            min_prem = MIN_PREMIUM_BUY_BN if symbol == 'BANKNIFTY' else MIN_PREMIUM_BUY
            if ltp > min_prem and sec_id:
                q_score = self._compute_signal_quality(symbol, 'CPR', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_CE_CPR', 'strategy': 'CPR',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * target_mult, 'sl': ltp * sl_mult,
                    'reason': f"CPR ({cpr_w:.3f}%) bullish breakout above TC={tc:.0f}",
                    'quality_score': q_score,
                    'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': cpr_indicator_levels,
                })

        # BUY PE: spot below BC
        elif spot < bc:
            if intraday_body > 0:  # Bullish body = skip
                logger.info(f"[CPR] {symbol} PE below BC but bullish intraday (body={intraday_body:+.0f})")
                return signals
            opt_type = 'PE'
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, opt_type)
            min_prem = MIN_PREMIUM_BUY_BN if symbol == 'BANKNIFTY' else MIN_PREMIUM_BUY
            if ltp > min_prem and sec_id:
                q_score = self._compute_signal_quality(symbol, 'CPR', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_PE_CPR', 'strategy': 'CPR',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * target_mult, 'sl': ltp * sl_mult,
                    'reason': f"CPR ({cpr_w:.3f}%) bearish breakout below BC={bc:.0f}",
                    'quality_score': q_score,
                    'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': cpr_indicator_levels,
                })

        return signals

    def _check_wave(self, symbol, spot, chain):
        """Wave strategy — momentum breakout using 5-bar swing detection.

        Logic:
          1. Detect 5-bar swing high/low pattern in spot bars
          2. Breakout above swing high = BUY CE, below swing low = BUY PE
          3. Requires momentum confirmation (3 consecutive higher/lower closes)
          4. Quality: 85% WR in MC backtest

        v24 MC-optimal: target=40% of premium, SL=25%, trail at 20%
        """
        signals = []
        bars = self._spot_bars.get(symbol, [])
        if len(bars) < 8:  # Need at least 8 bars for pattern
            return signals

        now = datetime.now()
        # Don't fire in first 15 min (9:15-9:30) — let market settle
        if now.time() < dtime(9, 30):
            return signals

        # Cooldown: no Wave signal if one fired in last 5 min for this symbol
        ws = self._wave_state[symbol]
        if ws['last_signal_time'] and (now - ws['last_signal_time']).total_seconds() < 300:
            return signals

        # Get recent completed bars (exclude current incomplete bar)
        recent = [b for b in bars[:-1] if b['close'] > 0][-8:]
        if len(recent) < 6:
            return signals

        # Find 5-bar swing high and swing low
        highs = [b['high'] for b in recent]
        lows = [b['low'] for b in recent]
        swing_high = max(highs[-5:])
        swing_low = min(lows[-5:])

        # Momentum: last 3 bars trending?
        last3_closes = [b['close'] for b in recent[-3:]]
        bullish_momentum = all(last3_closes[i] > last3_closes[i-1] for i in range(1, 3))
        bearish_momentum = all(last3_closes[i] < last3_closes[i-1] for i in range(1, 3))

        # Breakout above swing high + bullish momentum
        pcr = self._get_pcr(chain)
        vwap = self._get_vwap(symbol)
        wave_levels = {
            'swing_high': round(swing_high, 1), 'swing_low': round(swing_low, 1),
            'vwap': vwap, 'momentum': 'BULL' if bullish_momentum else ('BEAR' if bearish_momentum else 'FLAT'),
        }
        # Add CPR levels if available
        if symbol in self._cpr_levels:
            wave_levels.update(self._cpr_levels[symbol])

        if spot > swing_high and bullish_momentum:
            opt_type = 'CE'
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, opt_type)
            min_prem = MIN_PREMIUM_BUY_BN if symbol == 'BANKNIFTY' else MIN_PREMIUM_BUY
            if ltp > min_prem and sec_id:
                q_score = self._compute_signal_quality(symbol, 'Wave', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_CE_WAVE', 'strategy': 'Wave',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * 1.40, 'sl': ltp * 0.75,
                    'reason': f"Wave breakout above swing high {swing_high:.0f}, 3-bar momentum",
                    'quality_score': q_score,
                    'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': wave_levels,
                })
                ws['last_signal_time'] = now

        # Breakout below swing low + bearish momentum
        elif spot < swing_low and bearish_momentum:
            opt_type = 'PE'
            strike, ltp, sec_id, iv = self.get_atm_strike(symbol, spot, opt_type)
            min_prem = MIN_PREMIUM_BUY_BN if symbol == 'BANKNIFTY' else MIN_PREMIUM_BUY
            if ltp > min_prem and sec_id:
                q_score = self._compute_signal_quality(symbol, 'Wave', spot, ltp, iv, chain)
                signals.append({
                    'type': 'BUY_PE_WAVE', 'strategy': 'Wave',
                    'symbol': symbol, 'strike': strike, 'premium': ltp,
                    'security_id': sec_id, 'iv': iv, 'spot': spot,
                    'target': ltp * 1.40, 'sl': ltp * 0.75,
                    'reason': f"Wave breakdown below swing low {swing_low:.0f}, 3-bar momentum",
                    'quality_score': q_score,
                    'data_source': 'DHAN', 'pcr': pcr,
                    'indicator_levels': wave_levels,
                })
                ws['last_signal_time'] = now

        return signals

    def _check_gamma_blast(self, symbol, spot, chain):
        """Gamma Blast — expiry-day gamma spike trades.

        Logic:
          1. Only fires on weekly expiry day (Thu for NIFTY/BANKNIFTY, Fri for SENSEX)
          2. Detects ATM gamma spike: rapid premium movement near strike
          3. Looks for OTM options with high gamma (near ATM, low premium)
          4. v24 MC-optimal: target=50% gain, SL=30%, 77% WR

        Expiry schedule:
          NIFTY:     Thursday
          BANKNIFTY: Wednesday
          SENSEX:    Friday
        """
        signals = []
        now = datetime.now()

        # Check if today is expiry day for this symbol
        weekday = now.weekday()  # 0=Mon ... 6=Sun
        expiry_days = {'NIFTY': 3, 'BANKNIFTY': 2, 'SENSEX': 4}  # Thu, Wed, Fri
        if weekday != expiry_days.get(symbol, -1):
            return signals

        # Only after 10:00 AM on expiry (let morning volatility settle)
        if now.time() < dtime(10, 0):
            return signals

        # No entries after 2:30 PM on expiry (too close to settlement)
        if now.time() >= dtime(14, 30):
            return signals

        # Cooldown: one Gamma signal per symbol per 15 min
        last_gb = self._gamma_last_signal.get(symbol)
        if last_gb and (now - last_gb).total_seconds() < 900:
            return signals

        interval = STRIKE_INTERVALS.get(symbol, 100)
        atm_strike = round(spot / interval) * interval

        # Check both CE and PE at ATM
        for opt_type in ['CE', 'PE']:
            # Get ATM option
            ltp, sec_id, iv = self.get_option_ltp_and_security_id(symbol, atm_strike, opt_type)
            if not ltp or ltp <= 0 or not sec_id:
                continue

            min_prem = MIN_PREMIUM_BUY_BN if symbol == 'BANKNIFTY' else MIN_PREMIUM_BUY

            # Gamma Blast criteria:
            # - Premium in sweet spot (Rs 30-200 for gamma plays)
            # - IV elevated (> 15 on expiry = gamma rich)
            # - Spot near ATM (within 0.5 * interval)
            spot_dist = abs(spot - atm_strike) / interval
            if spot_dist > 0.6:
                continue  # Too far from ATM, low gamma

            if ltp < min_prem or ltp > 250:
                continue  # Too cheap or too expensive for gamma play

            # Direction: CE if spot trending up, PE if trending down
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

            q_score = self._compute_signal_quality(symbol, 'Gamma Blast', spot, ltp, iv, chain)
            pcr = self._get_pcr(chain)
            vwap = self._get_vwap(symbol)
            gamma_levels = {
                'atm_strike': atm_strike, 'spot_dist': round(spot_dist, 3),
                'vwap': vwap, 'expiry_day': True,
            }
            if symbol in self._cpr_levels:
                gamma_levels.update(self._cpr_levels[symbol])

            signals.append({
                'type': f'BUY_{opt_type}_GAMMA', 'strategy': 'Gamma Blast',
                'symbol': symbol, 'strike': atm_strike, 'premium': ltp,
                'security_id': sec_id, 'iv': iv, 'spot': spot,
                'target': ltp * 1.50, 'sl': ltp * 0.70,
                'reason': f"Gamma Blast expiry ATM={atm_strike} dist={spot_dist:.2f} IV={iv:.1f}",
                'quality_score': q_score,
                'data_source': 'DHAN', 'pcr': pcr,
                'indicator_levels': gamma_levels,
            })
            self._gamma_last_signal[symbol] = now
            break  # One direction per check

        return signals

    # ── Order Execution ──────────────────────────────────────────────────────

    def execute_signal(self, signal):
        """Execute a qualified trading signal.

        Flow:
          1. Pre-trade risk check
          2. Place LIMIT entry order
          3. Wait for fill (30s timeout)
          4. If filled → place SL-L order → create LivePosition
          5. If timeout → cancel entry order
        """
        symbol = signal['symbol']
        strategy = signal.get('strategy', 'UNKNOWN')
        sig_type = signal.get('type', '')
        strike = signal.get('strike', 0)
        premium = signal.get('premium', 0)
        sec_id = signal.get('security_id')
        target = signal.get('target', premium * 1.4)
        sl = signal.get('sl', premium * 0.5)
        quality = signal.get('quality_score', 0)

        # Determine direction
        direction = 'CE' if 'CE' in sig_type else 'PE'

        # Exchange segment
        exch = EXCHANGE_SEGMENT_MAP.get(symbol, 'NSE_FNO')

        # Lot size
        lot_size = LOT_SIZES.get(symbol, 50)
        quantity = lot_size * MAX_LOTS_PER_TRADE
        trade_cost = premium * quantity

        # ── Pre-trade risk check ─────────────────────────────────────────
        funds = self.broker.get_funds()
        available_margin = funds.get('available', 0) if funds else None

        ok, reason = self.risk.pre_trade_check(
            trade_cost=trade_cost,
            current_positions=len(self.positions),
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            available_margin=available_margin,
        )

        if not ok:
            logger.warning(f"[BLOCKED] {symbol} {strategy} {sig_type}: {reason}")
            notify_signal_blocked("LIVE_EQUITY", symbol, sig_type, "RISK_BLOCK", reason)
            return False

        if not sec_id:
            logger.warning(f"[SKIP] {symbol} {strategy}: no security_id for strike {strike}")
            return False

        # ── Place entry order ────────────────────────────────────────────
        tag = f"{strategy[:3]}_{symbol}_{direction}_{strike}"
        logger.info(f"\n{'='*50}")
        logger.info(f"  ENTRY: {symbol} {strategy} {sig_type}")
        logger.info(f"  Strike={strike} Premium=Rs {premium:.2f} Qty={quantity}")
        logger.info(f"  Target=Rs {target:.2f} SL=Rs {sl:.2f} Score={quality}")
        logger.info(f"  SecurityID={sec_id} Exchange={exch}")
        logger.info(f"{'='*50}")

        entry_result = self.broker.place_entry_order(
            security_id=sec_id,
            exchange_segment=exch,
            transaction_type='BUY',
            quantity=quantity,
            ltp=premium,
            tag=tag,
        )

        if not entry_result['success']:
            logger.error(f"[ENTRY_FAIL] {symbol}: {entry_result['message']}")
            notify_broker_reject("LIVE_EQUITY", symbol, sig_type, strike,
                                "ORDER_REJECTED", entry_result['message'])
            return False

        order_id = entry_result['order_id']

        # ── Wait for fill ────────────────────────────────────────────────
        fill = self.broker.wait_for_fill(order_id, timeout=30)

        if not fill['filled']:
            logger.warning(f"[ENTRY_TIMEOUT] {symbol} order {order_id} — {fill['message']}")
            # Cancel unfilled order
            self.broker.cancel_order(order_id)
            return False

        fill_price = fill['fill_price']
        fill_qty = fill['fill_qty'] or quantity

        # ── Create position ──────────────────────────────────────────────
        self._trade_counter += 1
        pos_id = f"LIVE_{symbol}_{self._trade_counter}_{datetime.now().strftime('%H%M%S')}"

        pos = LivePosition(
            pos_id=pos_id, symbol=symbol, strategy=strategy,
            signal_type=sig_type, strike=strike,
            entry_premium=fill_price, entry_order_id=order_id,
            lot_size=lot_size, num_lots=MAX_LOTS_PER_TRADE,
            target=target, sl=sl,
            spot_at_entry=signal.get('spot', 0),
            security_id=sec_id, exchange_segment=exch,
        )

        # ── Place SL order ───────────────────────────────────────────────
        sl_result = self.broker.place_sl_order(
            security_id=sec_id,
            exchange_segment=exch,
            quantity=fill_qty,
            sl_trigger_price=sl,
            tag=f"SL_{tag}",
        )
        if sl_result['success']:
            pos.sl_order_id = sl_result['order_id']
            logger.info(f"[SL_PLACED] {pos_id} SL order: {pos.sl_order_id} trigger=Rs {sl:.2f}")
        else:
            logger.error(f"[SL_FAIL] {pos_id}: {sl_result['message']} — MANUAL SL NEEDED!")

        # Populate signal metadata for dashboard
        pos.quality_score = quality
        pos.data_source = signal.get('data_source', 'DHAN')
        pos.signal_reason = signal.get('reason', '')
        pos.iv_at_entry = signal.get('iv', 0)
        pos.pcr_at_entry = signal.get('pcr', 0)
        pos.indicator_levels = signal.get('indicator_levels', {})

        self.positions.append(pos)
        self.risk.record_entry(symbol, strategy, direction)

        # Notify
        try:
            notify_trade_entry(
                market="EQUITY", strategy=strategy, symbol=symbol,
                signal_type=sig_type, strike=strike, premium=fill_price,
                lots=MAX_LOTS_PER_TRADE, quality_score=quality,
                reason=signal.get('reason', ''),
            )
        except Exception:
            pass

        logger.info(f"[POSITION_OPEN] {pos_id}: {symbol} {strategy} {sig_type} "
                    f"Strike={strike} Fill=Rs {fill_price:.2f} Qty={fill_qty}")

        self._save_trade_log(pos, 'ENTRY')
        return True

    # ── Exit Management ──────────────────────────────────────────────────────

    def check_exits(self):
        """Check all open positions for exit conditions.

        Exit layers (same priority as paper_trader v23):
          1. Kill switch / circuit breaker → flatten all
          2. EOD force close (after 15:20)
          3. Grace period (3 min)
          4. SL hit (monitored via Dhan SL-L order)
          5. TSL updates (trailing stop adjustments)
          6. Partial book at +50% gain
          7. Partial SL at -20% loss
          8. Full SL at -35% loss
        """
        if not self.positions:
            return

        # Check halt conditions
        halt, halt_reason = self.risk.should_halt()
        if halt:
            logger.critical(f"[HALT] {halt_reason} — flattening all positions!")
            self._flatten_all(halt_reason)
            return

        for pos in list(self.positions):
            if pos.closed:
                continue

            # Update current premium (+ record tick for charts)
            ltp, _, _ = self.get_option_ltp_and_security_id(
                pos.symbol, pos.strike,
                'CE' if 'CE' in pos.signal_type else 'PE'
            )
            if ltp > 0:
                pos.update_premium(ltp)

            # ── EOD Force Close ──────────────────────────────────────────
            if self.risk.is_eod_squareoff_time():
                self._close_position(pos, ltp, 'EOD_FORCE_CLOSE', aggressive=True)
                continue

            # ── Grace Period ─────────────────────────────────────────────
            if pos.elapsed_seconds < GRACE_PERIOD_SECONDS:
                continue

            # ── Check if SL order already filled ─────────────────────────
            if pos.sl_order_id and not pos.sl_order_id.startswith('SHADOW_'):
                sl_status = self.broker.get_order_status(pos.sl_order_id)
                if sl_status == 'TRADED':
                    # SL was hit by the exchange
                    fill = self.broker.wait_for_fill(pos.sl_order_id, timeout=5)
                    exit_price = fill.get('fill_price', pos.sl) if fill['filled'] else pos.sl
                    pos.exit_premium = exit_price
                    pos.exit_reason = 'SL_HIT'
                    pos.closed = True
                    pnl = (exit_price - pos.entry_premium) * pos.quantity
                    pos.realized_pnl = pnl
                    self.risk.record_exit(pnl, pos.symbol,
                                         'CE' if 'CE' in pos.signal_type else 'PE')
                    self._finalize_exit(pos)
                    continue

            # ── OI Heatmap Exit (v24 — only on profitable positions) ───
            if self.oi_heatmap and pos.unrealized_pnl > 0:
                try:
                    opt_type = 'CE' if 'CE' in pos.signal_type else 'PE'
                    oi_exit = self.oi_heatmap.get_exit_signal(
                        symbol=pos.symbol, spot=self.get_spot(pos.symbol),
                        opt_type=opt_type, strike=pos.strike,
                        entry_premium=pos.entry_premium,
                        current_premium=pos.current_premium)
                    if oi_exit['action'] == 'FULL_EXIT':
                        logger.info(f"[OI_EXIT] {pos.id} conf={oi_exit['confidence']} "
                                   f"reason={oi_exit['reason']}")
                        self._close_position(pos, ltp, f'OI_HEATMAP_EXIT_{oi_exit["confidence"]}')
                        continue
                    elif oi_exit['action'] == 'TRAIL_50%':
                        # Tighten TSL to breakeven
                        if pos.entry_premium > pos.trailing_sl:
                            pos.trailing_sl = pos.entry_premium
                            self._update_sl_order(pos, pos.entry_premium)
                            logger.info(f"[OI_TRAIL] {pos.id} conf={oi_exit['confidence']} "
                                       f"— TSL tightened to breakeven Rs {pos.entry_premium:.2f}")
                except Exception as e:
                    logger.debug(f"[OI_EXIT] {pos.id} check failed: {e}")

            # ── Premium-based exit logic (TSL, partial book) ─────────────
            gain_pct = pos.premium_gain_pct
            peak_pct = pos.peak_gain_pct

            # v25: Get regime-aware TSL parameters
            tsl_trail_dist = PARTIAL_BOOK_TSL_DISTANCE_PCT  # default 20%
            if self.regime_detector and get_regime_params:
                try:
                    regime = self.regime_detector.get_regime(pos.symbol)
                    regime_params = get_regime_params(regime)
                    tsl_trail_dist = regime_params.get('tsl_trail_distance_pct', PARTIAL_BOOK_TSL_DISTANCE_PCT)
                except Exception:
                    pass

            # LOSS SIDE: Partial SL at -25% (v24 MC-optimal, was -20%)
            if gain_pct <= -PARTIAL_SL_LOSS_PCT and not pos.partial_sl_done:
                logger.info(f"[PARTIAL_SL] {pos.id} at {gain_pct:.1f}% loss")
                # For 1-lot trading, this is the full SL
                self._close_position(pos, ltp, f'PARTIAL_SL_{gain_pct:.0f}%')
                continue

            # LOSS SIDE: Full SL at -35%
            if gain_pct <= -PARTIAL_SL_FULL_PCT:
                self._close_position(pos, ltp, f'FULL_SL_{gain_pct:.0f}%')
                continue

            # PROFIT SIDE: TSL to breakeven at +15%
            if gain_pct >= TSL_BREAKEVEN_TRIGGER_PCT:
                new_tsl = pos.entry_premium
                if new_tsl > pos.trailing_sl:
                    pos.trailing_sl = new_tsl
                    self._update_sl_order(pos, new_tsl)

            # PROFIT SIDE: TSL to +15% at +30%
            if gain_pct >= TSL_TRAIL_TRIGGER_PCT:
                new_tsl = pos.entry_premium * 1.15
                if new_tsl > pos.trailing_sl:
                    pos.trailing_sl = new_tsl
                    self._update_sl_order(pos, new_tsl)

            # PROFIT SIDE: Peak trailing after +10% (v25: regime-aware distance)
            if peak_pct >= PEAK_TRAIL_TRIGGER_PCT and pos.current_premium > pos.entry_premium:
                profit_from_entry = pos.peak_premium - pos.entry_premium
                trail_sl = pos.peak_premium - (profit_from_entry * tsl_trail_dist / 100)
                if trail_sl > pos.trailing_sl:
                    pos.trailing_sl = trail_sl
                    self._update_sl_order(pos, trail_sl)

            # Check if current premium hit trailing SL
            if pos.current_premium <= pos.trailing_sl and pos.trailing_sl > pos.sl:
                self._close_position(pos, ltp, f'TSL_HIT_{peak_pct:.0f}%peak')
                continue

    def _close_position(self, pos, ltp, reason, aggressive=False):
        """Close a position by placing a sell order."""
        logger.info(f"[EXIT] {pos.id}: {reason} | premium=Rs {ltp:.2f}")

        # Cancel existing SL order first
        if pos.sl_order_id:
            self.broker.cancel_order(pos.sl_order_id)

        # Cancel existing target order
        if pos.target_order_id:
            self.broker.cancel_order(pos.target_order_id)

        # Place exit order
        exit_result = self.broker.place_exit_order(
            security_id=pos.security_id,
            exchange_segment=pos.exchange_segment,
            quantity=pos.quantity,
            ltp=ltp if ltp > 0 else pos.current_premium,
            tag=f"EXIT_{pos.id[:20]}",
            aggressive=aggressive,
        )

        if exit_result['success']:
            fill = self.broker.wait_for_fill(exit_result['order_id'], timeout=30)
            exit_price = fill.get('fill_price', ltp) if fill['filled'] else ltp
        else:
            logger.error(f"[EXIT_FAIL] {pos.id}: {exit_result['message']}")
            exit_price = ltp

        pos.exit_premium = exit_price
        pos.exit_reason = reason
        pos.exit_time = datetime.now()
        pos.closed = True
        pnl = (exit_price - pos.entry_premium) * pos.quantity
        pos.realized_pnl = pnl

        direction = 'CE' if 'CE' in pos.signal_type else 'PE'
        self.risk.record_exit(pnl, pos.symbol, direction)
        self._finalize_exit(pos)

    def _finalize_exit(self, pos):
        """Move position to closed list and notify."""
        self.positions = [p for p in self.positions if p.id != pos.id]
        self.closed_positions.append(pos)
        self._record_exit(pos, pos.exit_reason)

        pnl_label = f"+Rs {pos.realized_pnl:,.0f}" if pos.realized_pnl >= 0 else f"-Rs {abs(pos.realized_pnl):,.0f}"
        logger.info(f"[CLOSED] {pos.id}: {pos.symbol} {pos.strategy} | "
                    f"Entry=Rs {pos.entry_premium:.2f} Exit=Rs {pos.exit_premium:.2f} | "
                    f"PnL={pnl_label} | Reason={pos.exit_reason}")

        try:
            notify_trade_exit(
                market="EQUITY", strategy=pos.strategy, symbol=pos.symbol,
                signal_type=pos.signal_type, strike=pos.strike,
                entry_price=pos.entry_premium, exit_price=pos.exit_premium,
                entry_time=pos.entry_time.isoformat(),
                pnl=pos.realized_pnl, capital_used=pos.entry_premium * pos.quantity,
                exit_reason=pos.exit_reason,
            )
        except Exception:
            pass

        self._save_trade_log(pos, 'EXIT')

    def _update_sl_order(self, pos, new_trigger):
        """Update SL order with new trigger price."""
        if not pos.sl_order_id:
            return
        result = self.broker.modify_sl_order(pos.sl_order_id, new_trigger)
        if result['success']:
            logger.info(f"[TSL] {pos.id} SL updated → Rs {new_trigger:.2f}")
        else:
            logger.warning(f"[TSL_FAIL] {pos.id}: {result['message']}")

    def _flatten_all(self, reason):
        """Emergency: close all positions."""
        logger.critical(f"[FLATTEN_ALL] {reason}")
        for pos in list(self.positions):
            if not pos.closed:
                ltp = pos.current_premium
                self._close_position(pos, ltp, f'FLATTEN:{reason}', aggressive=True)

        try:
            send_message(f"<b>CIRCUIT BREAKER</b>\n{reason}\nAll positions closed.")
        except Exception:
            pass

    # ── Main Loop ────────────────────────────────────────────────────────────

    def run(self):
        """Main trading loop."""
        self._running = True
        logger.info("[LiveTrader] Starting main loop...")

        # Signal handler for graceful shutdown
        def _sighandler(sig, frame):
            logger.info("[LiveTrader] Shutdown signal received")
            self._running = False
        signal.signal(signal.SIGINT, _sighandler)
        signal.signal(signal.SIGTERM, _sighandler)

        # Pre-market checks
        if not self.risk.is_trading_day():
            logger.info("[LiveTrader] Not a trading day. Exiting.")
            return

        try:
            send_message(
                f"<b>{'SHADOW' if self.shadow_mode else 'LIVE'} TRADER STARTED</b>\n"
                f"Capital: Rs {LIVE_CAPITAL:,}\n"
                f"Strategies: {', '.join(sorted(ACTIVE_STRATEGIES))}\n"
                f"Max positions: {MAX_POSITIONS}"
            )
        except Exception:
            pass

        symbols = ['NIFTY', 'BANKNIFTY', 'SENSEX']
        last_scan = {}
        last_exit_check = datetime.now()

        while self._running:
            try:
                now = datetime.now()

                # Outside trading hours — sleep
                if now.time() < MARKET_OPEN_TIME:
                    time.sleep(30)
                    continue

                if now.time() >= MARKET_CLOSE_TIME:
                    logger.info("[LiveTrader] Market closed. Generating EOD report.")
                    self._eod_report()
                    break

                # ── Check exits every 10 seconds ────────────────────────
                if (now - last_exit_check).total_seconds() >= PREMIUM_REFRESH_SECONDS:
                    self.check_exits()
                    self.write_status_file()  # Dashboard JSON update
                    last_exit_check = now

                # ── Scan for new signals every 30 seconds ───────────────
                for symbol in symbols:
                    last = last_scan.get(symbol, datetime.min)
                    if (now - last).total_seconds() < SCAN_INTERVAL_SECONDS:
                        continue
                    last_scan[symbol] = now

                    # No new entries after 3 PM
                    if now.time() >= NO_NEW_ENTRY_AFTER:
                        continue

                    # Halt check
                    halt, _ = self.risk.should_halt()
                    if halt:
                        continue

                    # ── v25: Get spot + feed to calculus + regime detector ───
                    spot = self.get_spot(symbol)
                    if not spot:
                        continue

                    # v30.3: Track today's opening price (first spot of the day)
                    if symbol not in self._today_open:
                        self._today_open[symbol] = spot
                        logger.info(f"[OPEN] {symbol} today_open={spot:.2f}")

                    # Feed spot tick to MarketCalculus (builds bar history for Physics Gate + DCI)
                    if self.calculus:
                        try:
                            self.calculus.add_spot_tick(symbol, spot)
                        except Exception as e:
                            logger.debug(f"[Calculus] add_spot_tick failed: {e}")

                    # Feed spot to RegimeDetector (classifies TRENDING/SIDEWAYS/FLAT)
                    if self.regime_detector:
                        try:
                            self.regime_detector.update(symbol, spot, self.current_vix)
                        except Exception as e:
                            logger.debug(f"[Regime] update failed: {e}")

                    # Update VIX (shared across symbols, done once per cycle)
                    if symbol == 'NIFTY':  # Only fetch VIX once per scan cycle
                        vix_val = None
                        try:
                            vix_val = self.dhan_feed.get_spot('INDIA VIX')
                        except Exception:
                            pass
                        # v30.3: Dhan doesn't have INDIA VIX in INDEX_MAP — fallback to Zerodha
                        if not vix_val and self.zerodha:
                            try:
                                vix_val = self.zerodha.get_spot('INDIA VIX')
                            except Exception:
                                pass
                        if vix_val and vix_val > 0:
                            self.current_vix = round(vix_val, 2)

                    # Generate signals
                    raw_signals = self.generate_signals(symbol)

                    # v30.2: Log scan status for visibility (every symbol, every cycle)
                    spot_val = self.get_spot(symbol)
                    logger.info(f"[SCAN] {symbol} spot={spot_val or 'N/A'} | raw={len(raw_signals)} | "
                               f"positions={len(self.positions)} | trades_today={self._trade_count_today} | "
                               f"VIX={self.current_vix}")

                    # ── FILTER 1: Basic strategy + quality score filter ─────
                    qualified = []
                    for sig in raw_signals:
                        strat = sig.get('strategy', '')
                        if strat in REMOVED_STRATEGIES:
                            continue
                        if strat not in ACTIVE_STRATEGIES:
                            continue
                        q = sig.get('quality_score', 0)
                        if q < MIN_SIGNAL_SCORE:
                            logger.debug(f"[SCORE_REJECT] {symbol} {strat} score={q} < {MIN_SIGNAL_SCORE}")
                            continue
                        qualified.append(sig)

                    # Daily trade limit
                    if self._trade_count_today >= MAX_TRADES_PER_DAY:
                        if qualified:
                            logger.info(f"[V24_DAILY_LIMIT] {symbol} — {self._trade_count_today} trades today, max {MAX_TRADES_PER_DAY}")
                        qualified = []

                    # ── FILTER 2: v25 CHOPPY SHIELD (per-symbol, before per-signal filters) ──
                    if qualified:
                        is_choppy, day_eff = self._check_choppy_shield(symbol, spot)
                        if is_choppy:
                            logger.info(f"[CHOPPY_BLOCK] {symbol} efficiency={day_eff}% < {CHOPPY_DAY_EFF_THRESHOLD}% — "
                                       f"blocking {len(qualified)} signal(s) after 11:00")
                            qualified = []

                    # ── FILTER 3: v25 DCI GATE (per-signal, Direction Confidence Index) ──
                    # ORDER: DCI before Physics Gate — same as paper_trader.py pipeline
                    if qualified:
                        vwap, ema9, ema20, three_bar_trend, open_price = self._get_dci_indicators(symbol, spot)
                        dci_passed = []
                        for sig in qualified:
                            try:
                                dci = compute_direction_confidence_live(
                                    sig, spot, vwap, ema9, ema20, three_bar_trend,
                                    sig.get('pcr', 1.0), open_price,
                                    sig.get('iv', 0), self.current_vix)

                                # v25: Boost/penalize DCI based on calculus momentum alignment
                                if self.calculus and self.calculus.bar_count(symbol) >= 15:
                                    try:
                                        calc_sig = self.calculus.direction_signal(symbol, spot)
                                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                                        if calc_sig['direction'] == sig_dir:
                                            dci = min(100, dci + 10)  # Calculus agrees — boost
                                        elif calc_sig['direction'] and calc_sig['confidence'] > 30:
                                            dci = max(0, dci - 10)    # Calculus opposes — penalize
                                    except Exception:
                                        pass

                                sig['_dci'] = dci
                                # v30.5: Diagnostic logging for DCI components
                                diag = sig.get('_dci_diag', {})
                                diag_str = (f"vwap={diag.get('vwap_d',0):+.0f} ema={diag.get('ema_d',0):+.0f} "
                                           f"mom={diag.get('mom_d',0):+.0f} body={diag.get('body_pct',0):+.1f}% "
                                           f"open={diag.get('open_used',0):.0f}")
                                if dci < DCI_MIN_THRESHOLD:
                                    logger.info(f"[DCI_BLOCK] {symbol} {sig.get('type')} DCI={dci:.0f} < {DCI_MIN_THRESHOLD} "
                                               f"— weak direction [{diag_str}]")
                                    notify_signal_blocked("LIVE_EQUITY", symbol, sig.get('type',''), "DCI_GATE",
                                                         f"DCI={dci:.0f} < {DCI_MIN_THRESHOLD}")
                                    continue
                                logger.info(f"[DCI_PASS] {symbol} {sig.get('type')} DCI={dci:.0f}/100 [{diag_str}]")
                                dci_passed.append(sig)
                            except Exception as e:
                                logger.debug(f"[DCI] Error: {e}")
                                dci_passed.append(sig)  # Pass through on error
                        qualified = dci_passed

                    # ── FILTER 4: v25 PHYSICS GATE (per-signal, momentum + wave + VWAP + RSI) ──
                    # ORDER: After DCI — same as paper_trader.py pipeline
                    if qualified and self.calculus and self.calculus.bar_count(symbol) >= 6:
                        physics_passed = []
                        for sig in qualified:
                            try:
                                sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                                phy_score, phy_diag = self.calculus.physics_gate(symbol, sig_dir, spot)
                                sig['_physics_score'] = phy_score
                                # v30.5: Wave strategy needs stronger physics confirmation
                                # Physics=1.7 FLAT regime PE entry lost Rs 3,462 on 2026-04-06
                                sig_strat = sig.get('strategy', '')
                                # v31: MC-optimal uses PHY>=0 for Wave (vs 10 current)
                                if _PARAM_SET == 'mc_optimal':
                                    min_physics = 0 if sig_strat == 'Wave' else 0
                                else:
                                    min_physics = 10 if sig_strat == 'Wave' else 0
                                if phy_score < min_physics:
                                    warnings_list = []
                                    if phy_diag.get('mom_dying'):
                                        warnings_list.append('MOM_DYING')
                                    if phy_diag.get('wave_peak'):
                                        warnings_list.append('WAVE_PEAK')
                                    if phy_diag.get('stretched'):
                                        warnings_list.append('VWAP_STRETCHED')
                                    if phy_diag.get('rsi_extreme'):
                                        warnings_list.append('RSI_EXTREME')
                                    block_detail = (f"score={phy_score} [{','.join(warnings_list)}] "
                                                   f"accel={phy_diag.get('accel','?')} rsi={phy_diag.get('rsi','?')}")
                                    logger.info(f"[PHYSICS_BLOCK] {symbol} {sig.get('type')} {block_detail}")
                                    notify_signal_blocked("LIVE_EQUITY", symbol, sig.get('type',''), "PHYSICS_BLOCK", block_detail)
                                    continue
                                logger.info(f"[PHYSICS_PASS] {symbol} {sig.get('type')} "
                                           f"score={phy_score} accel={phy_diag.get('accel', '?')}")
                                physics_passed.append(sig)
                            except Exception as e:
                                logger.debug(f"[Physics] Error: {e}")
                                physics_passed.append(sig)  # Pass through on error
                        qualified = physics_passed

                    # ── v24 Strategy Priority Dedup ─────────────────────────
                    if len(qualified) > 1:
                        qualified.sort(key=lambda s: STRATEGY_PRIORITY.get(s.get('strategy', ''), 0), reverse=True)
                        # Within same direction — keep only highest priority
                        seen_dirs = {}
                        deduped = []
                        for sig in qualified:
                            sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                            key = f"{symbol}_{sig_dir}"
                            if key not in seen_dirs:
                                seen_dirs[key] = sig.get('strategy', '')
                                deduped.append(sig)
                            else:
                                logger.info(f"[V24_PRIORITY_DEDUP] {symbol} {sig.get('strategy')} "
                                           f"{sig_dir} blocked by {seen_dirs[key]}")
                        qualified = deduped

                    # v24 Check existing positions — strategy block or lot-add
                    final_signals = []
                    for sig in qualified:
                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                        sig_strat = sig.get('strategy', '')
                        sig_prio = STRATEGY_PRIORITY.get(sig_strat, 0)

                        # Check existing same-direction positions
                        existing_same_dir = [
                            p for p in self.positions if not p.closed
                            and p.symbol == symbol
                            and ('CE' if 'CE' in p.signal_type else 'PE') == sig_dir
                        ]

                        if existing_same_dir:
                            # Check if higher-priority strategy already holds
                            best_existing_prio = max(
                                STRATEGY_PRIORITY.get(p.strategy, 0) for p in existing_same_dir)
                            if sig_prio <= best_existing_prio:
                                # Check if profitable → lot-add
                                profitable_pos = [p for p in existing_same_dir if p.unrealized_pnl > 0]
                                if profitable_pos and len(existing_same_dir) < MAX_SAME_DIRECTION_PER_SYMBOL:
                                    logger.info(f"[V24_LOT_ADD] {symbol} {sig_strat} {sig_dir} "
                                               f"adding to profitable position")
                                    sig['_add_to_existing'] = True
                                    final_signals.append(sig)
                                else:
                                    logger.info(f"[V24_STRAT_BLOCK] {symbol} {sig_strat} {sig_dir} "
                                               f"blocked by existing {best_existing_prio}-prio position")
                            else:
                                final_signals.append(sig)
                        else:
                            final_signals.append(sig)

                    # v24 Cooldown check
                    executable = []
                    for sig in final_signals:
                        sig_dir = 'CE' if 'CE' in sig.get('type', '') else 'PE'
                        cooldown_ok = self._check_cooldown(symbol, sig_dir, sig.get('strategy', ''))
                        if cooldown_ok:
                            executable.append(sig)

                    # Execute
                    for sig in executable:
                        # v25: Log regime alongside signal
                        regime_str = ''
                        regime_val = None
                        if self.regime_detector:
                            try:
                                regime_val = self.regime_detector.get_regime(symbol).value
                                regime_str = f" Regime={regime_val}"
                            except Exception:
                                pass

                        # v30.5: Block Wave entries in FLAT regime — weak momentum
                        # leads to EOD_FORCE_CLOSE losses (BANKNIFTY PE -3462 on 2026-04-06)
                        if sig.get('strategy') == 'Wave' and regime_val == 'FLAT':
                            logger.info(f"[WAVE_FLAT_BLOCK] {symbol} {sig.get('type')} "
                                       f"blocked — Wave needs TRENDING/SIDEWAYS, got FLAT")
                            continue
                        logger.info(f"[SIGNAL] {symbol} {sig.get('strategy')} "
                                   f"{sig.get('type')} Q={sig.get('quality_score')} "
                                   f"DCI={sig.get('_dci', '?')} Phy={sig.get('_physics_score', '?')} "
                                   f"Premium=Rs {sig.get('premium', 0):.2f}{regime_str}")
                        self._daily_signals_log.append({
                            'time': now.isoformat(),
                            'symbol': symbol,
                            'strategy': sig.get('strategy'),
                            'type': sig.get('type'),
                            'quality_score': sig.get('quality_score'),
                            'premium': sig.get('premium'),
                            'dci': sig.get('_dci'),
                            'physics_score': sig.get('_physics_score'),
                            'executed': True,
                        })
                        if self.execute_signal(sig):
                            self._trade_count_today += 1

                time.sleep(2)  # Small sleep between iterations

            except KeyboardInterrupt:
                logger.info("[LiveTrader] Keyboard interrupt")
                self._running = False
            except Exception as e:
                logger.error(f"[LiveTrader] Loop error: {e}", exc_info=True)
                time.sleep(5)

        # Shutdown
        logger.info("[LiveTrader] Shutting down...")
        if self.positions:
            logger.warning(f"[LiveTrader] {len(self.positions)} positions still open at shutdown")

    # ── Cooldown Logic (v24) ────────────────────────────────────────────────

    def _check_cooldown(self, symbol, direction, strategy):
        """v24 cooldown check with V-Reversal support."""
        now = datetime.now()
        for eh in reversed(self._exit_history.get(symbol, [])):
            elapsed = (now - eh['time']).total_seconds()
            if elapsed > REENTRY_COOLDOWN_SECONDS:
                break
            eh_dir = eh.get('direction', '')

            # Same direction cooldown
            if eh_dir == direction:
                if elapsed < REENTRY_COOLDOWN_SECONDS:
                    logger.info(f"[COOLDOWN] {symbol} {strategy} {direction} — "
                               f"same dir exit {int(elapsed)}s ago (need {REENTRY_COOLDOWN_SECONDS}s)")
                    return False

            # DIRECTION_FLIP — use V-Reversal detector
            if eh.get('reason') == 'DIRECTION_FLIP':
                if elapsed < 45:  # Absolute minimum anti-whipsaw
                    logger.info(f"[SKIP_FLIP_MINGAP] {symbol} {direction} — "
                               f"DIRECTION_FLIP {int(elapsed)}s ago (min 45s)")
                    return False
                if self.reversal_detector:
                    if not self.reversal_detector.is_flip_allowed(symbol):
                        logger.info(f"[SKIP_FLIP_LIMIT] {symbol} — max daily flips reached")
                        return False
                    spot = self.get_spot(symbol)
                    if spot:
                        rev = self.reversal_detector.check_reversal(symbol, spot, direction)
                        if rev['is_reversal'] and rev['confidence'] >= 60:
                            logger.info(f"[REVERSAL_CONFIRMED] {symbol} {direction} "
                                       f"conf={rev['confidence']} recovery={rev.get('recovery_pct', 0):.1f}%")
                            self.reversal_detector.record_flip(symbol)
                            return True
                        else:
                            logger.info(f"[REVERSAL_PENDING] {symbol} {direction} "
                                       f"conf={rev['confidence']} — blocked")
                            return False
                elif elapsed < DIRECTION_FLIP_COOLDOWN_SECONDS:
                    return False
        return True

    def _record_exit(self, pos, reason):
        """Track exit for cooldown logic."""
        direction = 'CE' if 'CE' in pos.signal_type else 'PE'
        self._exit_history[pos.symbol].append({
            'time': datetime.now(),
            'reason': reason,
            'direction': direction,
            'strategy': pos.strategy,
        })

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
            f"  EOD REPORT — {datetime.now().strftime('%Y-%m-%d')}\n"
            f"  Mode: {'SHADOW' if self.shadow_mode else 'LIVE'}\n"
            f"{'='*60}\n"
            f"  Total trades: {total_trades}\n"
            f"  Winners: {len(winners)} | Losers: {len(losers)}\n"
            f"  Win rate: {win_rate:.1f}%\n"
            f"  Total P&L: Rs {total_pnl:+,.0f}\n"
            f"  P&L %: {total_pnl / LIVE_CAPITAL * 100:+.2f}%\n"
            f"{'='*60}\n"
        )

        for p in self.closed_positions:
            report += (f"  {p.symbol:10s} {p.strategy:16s} {p.signal_type:12s} "
                      f"Entry=Rs {p.entry_premium:7.2f} Exit=Rs {p.exit_premium:7.2f} "
                      f"PnL=Rs {p.realized_pnl:+8,.0f} [{p.exit_reason}]\n")

        logger.info(report)

        try:
            send_message(
                f"<b>{'SHADOW' if self.shadow_mode else 'LIVE'} EOD REPORT</b>\n"
                f"Trades: {total_trades} | WR: {win_rate:.0f}%\n"
                f"P&L: Rs {total_pnl:+,.0f} ({total_pnl / LIVE_CAPITAL * 100:+.2f}%)\n"
                f"Winners: {len(winners)} | Losers: {len(losers)}"
            )
        except Exception:
            pass

        # Save to file
        self._save_eod_report()

    def _save_trade_log(self, pos, action):
        """Save trade to daily JSON log."""
        log_file = os.path.join(LIVE_TRADES_DIR, f"trades_{datetime.now().strftime('%Y%m%d')}.json")
        entry = {
            'action': action,
            'timestamp': datetime.now().isoformat(),
            **pos.to_dict(),
        }
        try:
            existing = []
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    existing = json.load(f)
            existing.append(entry)
            with open(log_file, 'w') as f:
                json.dump(existing, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[LOG] Failed to save trade log: {e}")

    def _save_eod_report(self):
        """Save EOD summary to file."""
        report_file = os.path.join(LIVE_TRADES_DIR, f"eod_{datetime.now().strftime('%Y%m%d')}.json")
        report = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'mode': 'SHADOW' if self.shadow_mode else 'LIVE',
            'capital': LIVE_CAPITAL,
            'total_trades': len(self.closed_positions),
            'total_pnl': sum(p.realized_pnl for p in self.closed_positions),
            'trades': [p.to_dict() for p in self.closed_positions],
            'signals_generated': len(self._daily_signals_log),
        }
        try:
            with open(report_file, 'w') as f:
                json.dump(report, f, indent=2, default=str)
            logger.info(f"[EOD] Report saved: {report_file}")
        except Exception as e:
            logger.error(f"[EOD] Failed to save report: {e}")

    def get_status(self):
        """Get current status for dashboard/CLI."""
        total_realized = sum(p.realized_pnl for p in self.closed_positions)
        total_unrealized = sum(p.unrealized_pnl for p in self.positions)
        winners = [p for p in self.closed_positions if p.realized_pnl > 0]
        losers = [p for p in self.closed_positions if p.realized_pnl < 0]
        total_closed = len(self.closed_positions)

        # Collect live market data for dashboard
        try:
            market_data = self._get_market_data()
        except Exception:
            market_data = {}

        return {
            'version': 'v25',
            'mode': 'SHADOW' if self.shadow_mode else 'LIVE',
            'capital': LIVE_CAPITAL,
            'daily_loss_limit': DAILY_LOSS_LIMIT,
            'strategies': sorted(ACTIVE_STRATEGIES),
            'positions': [p.to_dict() for p in self.positions],
            'closed_trades': [p.to_dict() for p in self.closed_positions],
            'open_count': len(self.positions),
            'closed_today': total_closed,
            'trades_today': self._trade_count_today,
            'max_trades': MAX_TRADES_PER_DAY,
            'unrealized_pnl': round(total_unrealized, 2),
            'realized_pnl': round(total_realized, 2),
            'total_pnl': round(total_realized + total_unrealized, 2),
            'win_rate': round(len(winners) / total_closed * 100, 1) if total_closed > 0 else 0,
            'winners': len(winners),
            'losers': len(losers),
            'risk': self.risk.get_status(),
            'oi_heatmap': self.oi_heatmap is not None,
            'reversal_detector': self.reversal_detector is not None,
            'market_data': market_data,
            'timestamp': datetime.now().isoformat(),
        }

    def get_chart_data_for_position(self, pos_id):
        """Get chart data for a specific position (open or closed)."""
        for pos in self.positions:
            if pos.id == pos_id:
                return pos.get_chart_data()
        for pos in self.closed_positions:
            if pos.id == pos_id:
                return pos.get_chart_data()
        return None

    def write_status_file(self):
        """Write status JSON for dashboard consumption."""
        status = self.get_status()
        # Include chart data for all positions
        for p_dict in status.get('positions', []):
            pos_id = p_dict.get('id')
            chart = self.get_chart_data_for_position(pos_id)
            if chart:
                p_dict['chart'] = chart
        for p_dict in status.get('closed_trades', []):
            pos_id = p_dict.get('id')
            chart = self.get_chart_data_for_position(pos_id)
            if chart:
                p_dict['chart'] = chart

        status_file = os.path.join(LIVE_TRADES_DIR, 'live_status.json')
        try:
            with open(status_file, 'w') as f:
                json.dump(status, f, indent=2, default=str)
        except Exception:
            pass


# ── Entry Point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Live Trading Bot — Equity Index Options')
    parser.add_argument('--live', action='store_true',
                        help='LIVE mode — places real orders with real money')
    parser.add_argument('--shadow', action='store_true', default=True,
                        help='Shadow mode — log signals only (DEFAULT)')
    parser.add_argument('--status', action='store_true',
                        help='Show risk status and exit')
    args = parser.parse_args()

    shadow_mode = not args.live

    if args.live:
        print("\n" + "="*60)
        print("  WARNING: LIVE TRADING MODE — REAL MONEY AT RISK")
        print(f"  Capital: Rs {LIVE_CAPITAL:,}")
        print(f"  Daily loss limit: Rs {DAILY_LOSS_LIMIT:,}")
        print("="*60)
        confirm = input("  Type 'YES I CONFIRM' to proceed: ")
        if confirm.strip() != 'YES I CONFIRM':
            print("  Aborted. Use --shadow for safe mode.")
            sys.exit(0)

    if args.status:
        trader = LiveTrader(shadow_mode=True)
        status = trader.get_status()
        print(json.dumps(status, indent=2, default=str))
        sys.exit(0)

    trader = LiveTrader(shadow_mode=shadow_mode)
    trader.run()


if __name__ == '__main__':
    main()
