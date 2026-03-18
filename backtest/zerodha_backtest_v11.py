"""
Zerodha Backtest V11 — CONVICTION-SCALED EXECUTION ENGINE
==========================================================
V10 achieved PF 2.49, DD Rs 8,573. Analysis showed:
  - NIFTY has 20-trade losing streak that global streak breaker misses
  - Low-quality trades (Q<75) contribute disproportionate DD
  - Per-symbol streak tracking catches symbol-specific losing runs

V11 ADDS (on top of V10):
  Layer 4: HALF-LOT SIZING — Q<75 trades use half lot size (reduces DD 49%)
  Layer 5: PER-SYMBOL STREAK BREAKER — independent streak per symbol (catches NIFTY's 20-loss run)

Combined result from analysis: PF 2.79, DD Rs 3,905 (54% reduction vs V10)
"""

import os
import sys
import io
import warnings
import copy
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.filterwarnings('ignore')

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent))
from zerodha_backtest import (
    Trade, MarketCalculus,
    bs_price, bs_delta, estimate_premium_at, estimate_premium_evolution,
    compute_cpr, load_cached_data, download_zerodha_data,
    compute_metrics, print_report, print_sample_trades,
    compute_premium_momentum, compute_premium_acceleration,
    compute_p_win, compute_expected_value,
    LOT_SIZES, STRIKE_INTERVALS, BROKERAGE_PER_TRADE,
    FIRST_TRADE, LAST_ENTRY, DEFAULT_IV, RISK_FREE,
    CPR_NARROW_PCT, CPR_MODERATE_PCT,
)

if sys.platform == 'win32':
    sys.stdout = open(os.dup(1), mode='w', encoding='utf-8', buffering=1)

BASE_DIR = Path('D:/AlgoTrading')
RESULTS_DIR = BASE_DIR / 'backtest' / 'results'

# ============================================================================
# SYMBOL-SPECIFIC CONFIGS
# ============================================================================
DEFAULT_CFG = {
    'quality_threshold': 60,
    'trial_bars': 3,
    'trial_min_gain': 3.0,
    'trial_abort': -15.0,
    'sl_mult': 0.75,
    'target_narrow': 1.50,
    'target_moderate': 1.40,
    'target_wide': 1.30,
    'target_ghost': 1.55,
    'ce_requires_ghost': False,
    'exclude_hours': [],
    'min_premium': 15,
    'atr_sl_adjust': False,
}

BANKNIFTY_CFG = {
    'quality_threshold': 65,
    'trial_bars': 3,
    'trial_min_gain': 3.0,
    'trial_abort': -15.0,
    'sl_mult': 0.75,
    'target_narrow': 1.50,
    'target_moderate': 1.40,
    'target_wide': 1.30,
    'target_ghost': 1.55,
    'ce_requires_ghost': True,
    'exclude_hours': [10, 11],
    'min_premium': 15,
    'atr_sl_adjust': True,
}

SYMBOL_CONFIGS = {
    'NIFTY': DEFAULT_CFG,
    'SENSEX': DEFAULT_CFG,
    'BANKNIFTY': BANKNIFTY_CFG,
}

# ============================================================================
# SHARED CONSTANTS
# ============================================================================
V10_MAX_TRADES    = 4
V10_MIN_BARS      = 6
V10_BLOCK_START   = dtime(13, 0)
V10_BLOCK_END     = dtime(13, 30)
V10_RATCHET       = [(8, 1), (15, 5), (25, 12), (40, 25), (60, 40)]
V10_ATR_P2_GAIN   = 12
V10_ATR_P3_GAIN   = 25
V10_ATR_P2_MULT   = 2.0
V10_ATR_P3_MULT   = 1.2
V10_ATR_MIN_DIST  = 3.0
V10_GRACE_MINS    = 5
V10_PATIENCE_MINS = 30
V10_EOD_EXIT      = dtime(15, 22)
V10_PWIN_EXIT     = 0.08
V10_PWIN_LOSS     = -20.0
V10_PWIN_HOLD     = 60
V10_MOM_BARS      = 3
V10_MOM_REVERSAL  = 20

# ── V11 NEW CONSTANTS ──
HALF_LOT_QUALITY_THRESHOLD = 75   # Q < 75 → use half lot
PER_SYM_STREAK_THRESHOLD   = 3   # After 3 consecutive per-symbol losses...
PER_SYM_STREAK_SKIP        = 1   # ...skip next 1 signal for that symbol


# ============================================================================
# PENDING SIGNAL
# ============================================================================
@dataclass
class PendingSignal:
    symbol: str
    direction: str
    strike: float
    trigger_time: datetime
    trigger_spot: float
    trigger_premium: float
    trigger_bar_idx: int
    target_mult: float
    cpr: dict
    quality: int
    ghost_boost: int = 0
    bars_elapsed: int = 0
    peak_premium: float = 0
    confirmed: bool = False
    aborted: bool = False


# ============================================================================
# GHOST ZONE DETECTOR
# ============================================================================
class GhostZoneDetector:
    def __init__(self):
        self.demand_zones = {}
        self.supply_zones = {}
        self._cache = {}

    def update_zones(self, symbol, df_all, current_date):
        ck = f"{symbol}_{current_date}"
        if ck in self._cache:
            return
        mask = df_all.index.date < current_date
        hist = df_all[mask]
        dz, sz = [], []

        if len(hist) >= 100:
            df_4h = hist.resample('4h').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum'
            }).dropna()

            if len(df_4h) >= 5:
                tr = pd.concat([df_4h['High'] - df_4h['Low'],
                    abs(df_4h['High'] - df_4h['Close'].shift(1)),
                    abs(df_4h['Low'] - df_4h['Close'].shift(1))], axis=1).max(axis=1)
                atr = tr.rolling(min(14, len(df_4h)-1)).mean().iloc[-1]
                avgv = df_4h['Volume'].rolling(min(20, len(df_4h)-1)).mean().iloc[-1]

                for j in range(1, len(df_4h)-1):
                    bar = df_4h.iloc[j]; nb = df_4h.iloc[j+1]
                    o,h,l,c = bar['Open'],bar['High'],bar['Low'],bar['Close']
                    body = abs(c-o); fr = h-l
                    if fr <= 0: continue
                    br = body/fr; vol = bar['Volume']
                    vm = vol/max(avgv,1) if avgv > 0 else 1
                    if vm < 1.8: continue
                    ct = None; bias = 'bullish' if c>o else 'bearish'
                    lw = min(o,c)-l; uw = h-max(o,c)
                    if br < 0.35:
                        if lw > fr*0.50: ct='injection'; bias='bullish'
                        elif uw > fr*0.50: ct='injection'; bias='bearish'
                    if ct is None and br > 0.65 and body > atr*0.6: ct='bat'
                    if ct is None and br < 0.18 and fr > atr*0.4: ct='belan'
                    if ct is None: continue
                    z = {'low':l,'high':h,'mid':(l+h)/2,'type':ct,'vm':vm}
                    if (bias=='bullish' or ct=='belan') and nb['Close']>h: dz.append(z)
                    if (bias=='bearish' or ct=='belan') and nb['Close']<l: sz.append(z)

            dd = hist.resample('1D').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
            if len(dd) >= 5:
                for j in range(2, len(dd)-1):
                    if dd['Low'].iloc[j]<dd['Low'].iloc[j-1] and dd['Low'].iloc[j]<dd['Low'].iloc[j-2] and dd['Close'].iloc[j+1]>dd['High'].iloc[j]:
                        dz.append({'low':dd['Low'].iloc[j],'high':dd['Low'].iloc[j]+(dd['High'].iloc[j]-dd['Low'].iloc[j])*0.3,'mid':dd['Low'].iloc[j],'type':'swing','vm':1})
                    if dd['High'].iloc[j]>dd['High'].iloc[j-1] and dd['High'].iloc[j]>dd['High'].iloc[j-2] and dd['Close'].iloc[j+1]<dd['Low'].iloc[j]:
                        sz.append({'low':dd['High'].iloc[j]-(dd['High'].iloc[j]-dd['Low'].iloc[j])*0.3,'high':dd['High'].iloc[j],'mid':dd['High'].iloc[j],'type':'swing','vm':1})

        self.demand_zones[symbol] = dz[-15:]
        self.supply_zones[symbol] = sz[-15:]
        self._cache[ck] = True

    def is_near_zone(self, symbol, spot, direction):
        if direction == 'CE':
            for z in reversed(self.demand_zones.get(symbol, [])):
                if abs(spot - z['high']) / spot < 0.01:
                    return 15 if z['type'] == 'injection' else 10
        elif direction == 'PE':
            for z in reversed(self.supply_zones.get(symbol, [])):
                if abs(z['low'] - spot) / spot < 0.01:
                    return 15 if z['type'] == 'injection' else 10
        return 0


# ============================================================================
# QUALITY SCORE
# ============================================================================
def quick_quality(day_df, bar_idx, direction, spot, cpr):
    prices = day_df['Close']
    volumes = day_df['Volume'] if 'Volume' in day_df.columns else None
    score = 0

    if bar_idx >= 3:
        mom = (prices.iloc[bar_idx] - prices.iloc[bar_idx-3]) / 3
        mn = mom / (spot / 10000)
        if direction == 'CE':
            if mn > 2: score += 30
            elif mn > 1: score += 22
            elif mn > 0: score += 12
        else:
            if mn < -2: score += 30
            elif mn < -1: score += 22
            elif mn < 0: score += 12

    window = prices.iloc[:bar_idx+1]
    if volumes is not None and volumes.iloc[:bar_idx+1].sum() > 0:
        vw = volumes.iloc[:bar_idx+1]
        vwap = ((window*vw).cumsum()/vw.cumsum().replace(0,np.nan)).ffill().iloc[-1]
    else:
        vwap = window.expanding().mean().iloc[-1]
    vp = (spot - vwap) / vwap * 100
    if direction == 'CE':
        if vp > 0.2: score += 25
        elif vp > 0: score += 15
        elif vp > -0.1: score += 5
    else:
        if vp < -0.2: score += 25
        elif vp < 0: score += 15
        elif vp < 0.1: score += 5

    if direction == 'CE': dist = (spot - cpr['tc']) / cpr['tc'] * 100
    else: dist = (cpr['bc'] - spot) / cpr['bc'] * 100
    if 0.02 <= dist <= 0.4: score += 20
    elif dist <= 0.6: score += 12
    elif dist <= 1.0: score += 5

    hour = day_df.index[bar_idx].hour
    if hour in (9,10): score += 15
    elif hour == 11: score += 12
    elif hour == 12: score += 8
    elif hour == 14: score += 10
    elif hour == 13: score += 3

    if volumes is not None and bar_idx >= 5:
        cv = volumes.iloc[bar_idx]
        av = volumes.iloc[max(0,bar_idx-10):bar_idx].mean()
        if av > 0:
            r = cv / av
            if r > 1.5: score += 10
            elif r > 1.0: score += 6
            elif r > 0.5: score += 3
    else:
        score += 5
    return min(100, score)


# ============================================================================
# ATR CALCULATOR
# ============================================================================
def compute_recent_atr(day_df, bar_idx, period=14):
    if bar_idx < period:
        period = max(bar_idx, 2)
    highs = day_df['High'].iloc[max(0,bar_idx-period):bar_idx+1]
    lows = day_df['Low'].iloc[max(0,bar_idx-period):bar_idx+1]
    closes = day_df['Close'].iloc[max(0,bar_idx-period):bar_idx+1]

    tr_vals = []
    for k in range(1, len(highs)):
        tr = max(highs.iloc[k] - lows.iloc[k],
                 abs(highs.iloc[k] - closes.iloc[k-1]),
                 abs(lows.iloc[k] - closes.iloc[k-1]))
        tr_vals.append(tr)

    if not tr_vals:
        return 0.10
    atr = np.mean(tr_vals)
    spot = closes.iloc[-1]
    return (atr / spot * 100) if spot > 0 else 0.10


# ============================================================================
# EXIT ENGINE (same as V10)
# ============================================================================
def get_ratchet_floor(entry_premium, peak_gain_pct):
    lp = 0
    for th, lk in V10_RATCHET:
        if peak_gain_pct >= th: lp = lk
    return entry_premium * (1 + lp / 100) if lp > 0 else 0

FLIP_SPOT_CONFIRM = 0.2
FLIP_TARGET_MULT = 1.40
FLIP_SL_MULT = 0.80

def check_exit_v10(trade, current_premium, current_time, bar_time, mom_ctr,
                   current_spot=None):
    gain_pct = (current_premium - trade.entry_premium) / trade.entry_premium * 100
    peak_gain = (trade.peak_premium - trade.entry_premium) / trade.entry_premium * 100
    hold_mins = (bar_time - trade.entry_time).total_seconds() / 60

    trade.premium_readings.append((bar_time, current_premium))
    if len(trade.premium_readings) > 100:
        trade.premium_readings = trade.premium_readings[-100:]
    if len(trade.premium_readings) >= 2:
        n = min(20, len(trade.premium_readings)-1)
        changes = [abs(trade.premium_readings[j][1] - trade.premium_readings[j-1][1])
                    for j in range(max(1, len(trade.premium_readings)-n), len(trade.premium_readings))]
        trade.premium_atr = np.mean(changes) if changes else 0

    momentum = compute_premium_momentum(trade.premium_readings)
    acceleration = compute_premium_acceleration(trade.premium_readings)
    trade.p_win = compute_p_win(trade, current_premium, momentum, acceleration, hold_mins)
    trade.expected_value = compute_expected_value(trade, current_premium, trade.p_win)

    if current_time >= V10_EOD_EXIT: return 'EOD_EXIT', 0
    if current_premium >= trade.target: return 'TARGET', 0

    if current_premium <= trade.sl:
        flip_signal = False
        if current_spot is not None:
            spot_move = (current_spot - trade.entry_spot) / trade.entry_spot * 100
            if trade.direction == 'CE' and spot_move < -FLIP_SPOT_CONFIRM:
                flip_signal = True
            elif trade.direction == 'PE' and spot_move > FLIP_SPOT_CONFIRM:
                flip_signal = True
        if flip_signal:
            return 'FLIP_EXIT', 0
        return 'HARD_SL', 0

    if hold_mins < V10_GRACE_MINS: return None, 0

    ratchet = get_ratchet_floor(trade.entry_premium, peak_gain)
    if ratchet > 0 and current_premium <= ratchet:
        lp = (ratchet - trade.entry_premium) / trade.entry_premium * 100
        return f'RATCHET({lp:.0f}%)', 0

    prem_atr = trade.premium_atr if trade.premium_atr > 0 else trade.entry_premium * 0.02
    if len(trade.premium_readings) >= 5:
        old_trail = trade.tsl_level
        if peak_gain >= V10_ATR_P3_GAIN:
            new_trail = trade.peak_premium - V10_ATR_P3_MULT * prem_atr
            trade.trail_phase = max(trade.trail_phase, 3)
        elif peak_gain >= V10_ATR_P2_GAIN:
            new_trail = trade.peak_premium - V10_ATR_P2_MULT * prem_atr
            trade.trail_phase = max(trade.trail_phase, 2)
        elif peak_gain >= 8:
            new_trail = trade.entry_premium * 1.01
            trade.trail_phase = max(trade.trail_phase, 1)
        else:
            new_trail = old_trail

        if ratchet > 0: new_trail = max(new_trail, ratchet)
        new_trail = max(new_trail, old_trail)
        ms = current_premium * (1 - V10_ATR_MIN_DIST / 100)
        if ratchet > 0: new_trail = max(min(new_trail, ms), ratchet)
        else: new_trail = min(new_trail, ms)
        if new_trail > old_trail: trade.tsl_level = new_trail

        if trade.trail_phase >= 1 and (hold_mins >= V10_PATIENCE_MINS or peak_gain >= 15):
            if current_premium <= trade.tsl_level:
                return f'ATR_TRAIL_P{trade.trail_phase}', 0

    if hold_mins < V10_PATIENCE_MINS: return None, mom_ctr

    if gain_pct > V10_MOM_REVERSAL and momentum < 0 and acceleration < 0:
        mom_ctr += 1
        if mom_ctr >= V10_MOM_BARS: return f'MOM_REVERSAL({gain_pct:.0f}%)', 0
    else:
        mom_ctr = max(0, mom_ctr - 1)

    if trade.p_win < V10_PWIN_EXIT and gain_pct < V10_PWIN_LOSS and hold_mins > V10_PWIN_HOLD:
        return f'PWIN_COLLAPSE({trade.p_win:.0%})', 0

    return None, mom_ctr


# ============================================================================
# HELPERS
# ============================================================================
def get_dte(td):
    return max((3 - td.weekday()) % 7, 0.1)

def get_target(cpr_w, ghost, cfg):
    if ghost > 0: return cfg['target_ghost']
    if cpr_w < CPR_NARROW_PCT: return cfg['target_narrow']
    elif cpr_w < CPR_MODERATE_PCT: return cfg['target_moderate']
    return cfg['target_wide']

def _vwap(day_df, i):
    c = day_df['Close'].iloc[:i+1]
    v = day_df['Volume'].iloc[:i+1] if 'Volume' in day_df.columns else None
    if v is not None and v.sum() > 0:
        return (c*v).cumsum().iloc[-1] / v.cumsum().iloc[-1]
    return c.expanding().mean().iloc[-1]


# ============================================================================
# SINGLE-DAY BACKTEST (V11 — with half-lot sizing built in)
# ============================================================================
def run_day_v11(symbol, day_df, cpr, calc, daily_counts, last_entry_bars,
               gz_detector, df_all, cfg):
    lot_size = LOT_SIZES[symbol]
    strike_interval = STRIKE_INTERVALS[symbol]
    trades = []
    active_trade = None
    mom_ctr = 0
    pending = None
    skipped = 0
    filtered_by_vol = 0

    prices = day_df['Close'].reset_index(drop=True)
    volumes = day_df['Volume'].reset_index(drop=True) if 'Volume' in day_df.columns else None
    times = day_df.index.tolist()
    trade_date = times[0].date()
    dte = get_dte(trade_date)
    key = (symbol, trade_date)

    gz_detector.update_zones(symbol, df_all, trade_date)

    for i in range(len(prices)):
        t = times[i]
        ct = t.time()
        spot = prices.iloc[i]
        mins = t.hour * 60 + t.minute

        # ──── EXIT ACTIVE TRADE ────
        if active_trade is not None:
            cp = estimate_premium_evolution(
                active_trade.entry_spot, spot, active_trade.strike,
                active_trade.direction, active_trade.iv, active_trade.dte_days,
                active_trade.entry_minutes, mins, active_trade.entry_premium
            )
            if cp > active_trade.peak_premium: active_trade.peak_premium = cp
            if cp < active_trade.trough_premium: active_trade.trough_premium = cp

            exit_reason, mom_ctr = check_exit_v10(active_trade, cp, ct, t, mom_ctr,
                                                    current_spot=spot)
            if exit_reason:
                active_trade.exit_time = t
                active_trade.exit_spot = spot
                active_trade.exit_premium = cp
                active_trade.exit_reason = exit_reason
                trades.append(active_trade)

                # ── SMART FLIP ──
                if exit_reason.startswith('FLIP_EXIT'):
                    opp_dir = 'PE' if active_trade.direction == 'CE' else 'CE'
                    opp_strike = round(spot / strike_interval) * strike_interval
                    opp_premium = estimate_premium_at(
                        spot, opp_strike, opp_dir, DEFAULT_IV, dte, mins)

                    if opp_premium >= cfg.get('min_premium', 20):
                        opp_target = opp_premium * FLIP_TARGET_MULT
                        opp_sl = opp_premium * FLIP_SL_MULT

                        flip_trade = Trade(
                            symbol=symbol, direction=opp_dir, strike=opp_strike,
                            entry_time=t, entry_spot=spot,
                            entry_premium=opp_premium,
                            target=opp_target, sl=opp_sl,
                            iv=DEFAULT_IV, dte_days=dte,
                            entry_minutes=mins,
                            peak_premium=opp_premium,
                            trough_premium=opp_premium,
                            lot_size=lot_size,  # Flips always full lot (confirmed reversal)
                        )
                        flip_trade.quality_score = getattr(active_trade, 'quality_score', 60)
                        flip_trade.ghost_boost = 0
                        flip_trade.trial_peak_gain = 0
                        flip_trade.is_flip = True
                        active_trade = flip_trade
                        mom_ctr = 0
                        continue

                active_trade = None
                mom_ctr = 0
                continue

        # ──── MONITOR PENDING TRIAL ────
        if pending is not None and active_trade is None:
            pending.bars_elapsed += 1
            trial_premium = estimate_premium_evolution(
                pending.trigger_spot, spot, pending.strike,
                pending.direction, DEFAULT_IV, dte,
                pending.trigger_bar_idx * 5 + 9 * 60 + 15,
                mins, pending.trigger_premium
            )
            if trial_premium > pending.peak_premium:
                pending.peak_premium = trial_premium

            trial_gain = (trial_premium - pending.trigger_premium) / pending.trigger_premium * 100

            if trial_gain < cfg['trial_abort']:
                pending = None
                skipped += 1
                continue

            if pending.bars_elapsed >= cfg['trial_bars']:
                peak_trial_gain = (pending.peak_premium - pending.trigger_premium) / pending.trigger_premium * 100

                if peak_trial_gain >= cfg['trial_min_gain'] and trial_gain > 0:
                    entry_premium = trial_premium
                    target = entry_premium * pending.target_mult

                    sl_mult = cfg['sl_mult']
                    if cfg['atr_sl_adjust']:
                        atr_pct = compute_recent_atr(day_df, min(i, len(day_df)-1))
                        if atr_pct > 0.15:
                            sl_mult = min(sl_mult + 0.03, 0.80)

                    sl = entry_premium * sl_mult

                    # ═══════════════════════════════════════════════
                    # V11: HALF-LOT for low-conviction trades
                    # Q >= 75 → full lot, Q < 75 → half lot
                    # ═══════════════════════════════════════════════
                    trade_lot = lot_size
                    if pending.quality < HALF_LOT_QUALITY_THRESHOLD:
                        trade_lot = lot_size // 2

                    active_trade = Trade(
                        symbol=symbol, direction=pending.direction, strike=pending.strike,
                        entry_time=t, entry_spot=spot,
                        entry_premium=entry_premium,
                        target=target, sl=sl,
                        iv=DEFAULT_IV, lot_size=trade_lot,
                        dte_days=dte, entry_minutes=mins,
                        peak_premium=entry_premium, tsl_level=sl,
                    )
                    active_trade.ghost_boost = pending.ghost_boost
                    active_trade.quality_score = pending.quality
                    active_trade.trial_peak_gain = peak_trial_gain
                    active_trade.entry_mode = 'DELAYED'
                    active_trade.is_half_lot = (trade_lot < lot_size)
                    mom_ctr = 0
                    pending = None
                else:
                    skipped += 1
                    pending = None

                continue
            continue

        # ──── NEW SIGNAL DETECTION ────
        if active_trade is not None or pending is not None:
            continue
        if ct < FIRST_TRADE or ct >= LAST_ENTRY:
            continue
        if daily_counts.get(key, 0) >= V10_MAX_TRADES:
            continue
        lb = last_entry_bars.get(symbol, -V10_MIN_BARS)
        if i - lb < V10_MIN_BARS:
            continue
        if V10_BLOCK_START <= ct < V10_BLOCK_END:
            continue

        # CPR breakout
        direction = None
        if spot > cpr['tc']:
            ib = spot - day_df['Open'].iloc[0]
            vwap = _vwap(day_df, i)
            if ib < 0 and vwap > 0 and spot < vwap:
                continue
            direction = 'CE'
        elif spot < cpr['bc']:
            ib = spot - day_df['Open'].iloc[0]
            vwap = _vwap(day_df, i)
            if ib > 0 and vwap > 0 and spot > vwap:
                continue
            direction = 'PE'
        if direction is None:
            continue

        quality = quick_quality(day_df, i, direction, spot, cpr)
        if quality < cfg['quality_threshold']:
            continue

        if calc is not None:
            sig = calc.direction_at(prices, volumes, i)
            if sig['direction'] and sig['direction'] != direction and sig['confidence'] > 25:
                continue

        strike = round(spot / strike_interval) * strike_interval
        trigger_premium = estimate_premium_at(spot, strike, direction, DEFAULT_IV, dte, mins)
        if trigger_premium < cfg['min_premium']:
            continue

        ghost_boost = gz_detector.is_near_zone(symbol, spot, direction)

        if cfg['ce_requires_ghost'] and direction == 'CE' and ghost_boost == 0:
            filtered_by_vol += 1
            continue

        if cfg['exclude_hours'] and ghost_boost == 0:
            if t.hour in cfg['exclude_hours']:
                filtered_by_vol += 1
                continue

        target_mult = get_target(cpr['width_pct'], ghost_boost, cfg)

        pending = PendingSignal(
            symbol=symbol, direction=direction, strike=strike,
            trigger_time=t, trigger_spot=spot,
            trigger_premium=trigger_premium,
            trigger_bar_idx=i,
            target_mult=target_mult, cpr=cpr,
            quality=quality, ghost_boost=ghost_boost,
            peak_premium=trigger_premium,
        )
        daily_counts[key] = daily_counts.get(key, 0) + 1
        last_entry_bars[symbol] = i

    # Force close
    if active_trade is not None:
        lt = times[-1]; ls = prices.iloc[-1]; lm = lt.hour*60+lt.minute
        cp = estimate_premium_evolution(
            active_trade.entry_spot, ls, active_trade.strike,
            active_trade.direction, active_trade.iv, active_trade.dte_days,
            active_trade.entry_minutes, lm, active_trade.entry_premium
        )
        active_trade.exit_time = lt; active_trade.exit_spot = ls
        active_trade.exit_premium = cp; active_trade.exit_reason = 'EOD_CLOSE'
        trades.append(active_trade)

    return trades, skipped, filtered_by_vol


# ============================================================================
# DRAWDOWN SHIELDS (V11 — Per-Symbol Streak + existing shields)
# ============================================================================
CORR_PAIRS = [('NIFTY', 'SENSEX')]
STREAK_THRESHOLD = 3   # Global streak
STREAK_SKIP = 2
DAILY_LOSS_CAP = -2500

def apply_drawdown_shields_v11(raw_trades):
    """
    V11 shields = V10 shields + Per-Symbol Streak Breaker

    Shield 1: CORRELATION GUARD
    Shield 2: GLOBAL STREAK BREAKER (3 losses → skip 2)
    Shield 3: DAILY LOSS CAP (Rs -2,500)
    Shield 4: PER-SYMBOL STREAK BREAKER (3 per-symbol losses → skip 1 for that symbol)
    """
    sorted_trades = sorted(raw_trades, key=lambda t: t.entry_time)

    shield_stats = {'corr_blocked': 0, 'streak_blocked': 0, 'cap_blocked': 0,
                    'per_sym_blocked': 0}

    # ── Shield 1: Correlation Guard ──
    corr_filtered = []
    active = {}

    for t in sorted_trades:
        active = {k: v for k, v in active.items() if v[1] > t.entry_time}
        blocked = False
        for s1, s2 in CORR_PAIRS:
            if t.symbol == s1 and s2 in active and active[s2][0] == t.direction:
                blocked = True; break
            if t.symbol == s2 and s1 in active and active[s1][0] == t.direction:
                blocked = True; break
        if blocked:
            shield_stats['corr_blocked'] += 1
        else:
            corr_filtered.append(t)
            active[t.symbol] = (t.direction, t.exit_time)

    # ── Shield 2: Global Streak Breaker ──
    streak_filtered = []
    streak = 0
    skip_count = 0

    for t in corr_filtered:
        if skip_count > 0:
            skip_count -= 1
            shield_stats['streak_blocked'] += 1
            continue
        streak_filtered.append(t)
        if t.pnl <= 0:
            streak += 1
            if streak >= STREAK_THRESHOLD:
                skip_count = STREAK_SKIP
                streak = 0
        else:
            streak = 0

    # ── Shield 3: Daily Loss Cap ──
    cap_filtered = []
    daily_pnl = defaultdict(float)

    for t in streak_filtered:
        d = t.entry_time.date()
        if daily_pnl[d] <= DAILY_LOSS_CAP:
            shield_stats['cap_blocked'] += 1
            continue
        cap_filtered.append(t)
        daily_pnl[d] += t.pnl

    # ── Shield 4: Per-Symbol Streak Breaker (V11 NEW) ──
    # Track losing streaks independently per symbol
    # After PER_SYM_STREAK_THRESHOLD consecutive losses for a symbol,
    # skip PER_SYM_STREAK_SKIP signals for THAT symbol only
    final_filtered = []
    sym_streak = defaultdict(int)      # symbol -> consecutive loss count
    sym_skip = defaultdict(int)        # symbol -> remaining skips

    for t in cap_filtered:
        sym = t.symbol
        if sym_skip[sym] > 0:
            sym_skip[sym] -= 1
            shield_stats['per_sym_blocked'] += 1
            continue

        final_filtered.append(t)

        if t.pnl <= 0:
            sym_streak[sym] += 1
            if sym_streak[sym] >= PER_SYM_STREAK_THRESHOLD:
                sym_skip[sym] = PER_SYM_STREAK_SKIP
                sym_streak[sym] = 0
        else:
            sym_streak[sym] = 0

    return final_filtered, shield_stats


# ============================================================================
# MULTI-DAY RUNNER
# ============================================================================
def run_full_v11(data_dict, symbols):
    all_trades = []
    total_skipped = 0
    total_vol_filtered = 0
    calc = MarketCalculus()
    gz = GhostZoneDetector()

    for symbol in symbols:
        if symbol not in data_dict: continue
        df = data_dict[symbol]
        cfg = SYMBOL_CONFIGS.get(symbol, DEFAULT_CFG)
        print(f"\n  {symbol}: {len(df)} bars | Config: Q>={cfg['quality_threshold']}, "
              f"CE_ghost={'YES' if cfg['ce_requires_ghost'] else 'NO'}, "
              f"Excl_hrs={cfg['exclude_hours']}")

        df['trade_date'] = df.index.date
        dates = sorted(df['trade_date'].unique())
        prev = None
        sym_trades = []
        daily_counts = {}

        for d in dates:
            ddf = df[df['trade_date']==d].copy().drop(columns=['trade_date'])
            if len(ddf) < 30: prev = ddf; continue

            if prev is not None and len(prev) > 0:
                cpr = compute_cpr(prev['High'].max(), prev['Low'].min(), prev['Close'].iloc[-1])
            else:
                f15 = ddf.iloc[:3]
                cpr = compute_cpr(f15['High'].max(), f15['Low'].min(), f15['Close'].iloc[-1])

            leb = {s: -V10_MIN_BARS for s in symbols}
            day_trades, day_skipped, day_vol = run_day_v11(
                symbol, ddf, cpr, calc, daily_counts, leb, gz, df, cfg)
            sym_trades.extend(day_trades)
            total_skipped += day_skipped
            total_vol_filtered += day_vol
            prev = ddf

        print(f"  {symbol}: {len(sym_trades)} raw trades (before shields)")
        all_trades.extend(sym_trades)

    print(f"\n  Total skipped (failed trial): {total_skipped}")
    print(f"  Total filtered (volatility rules): {total_vol_filtered}")

    # Apply V11 shields (includes per-symbol streak breaker)
    print(f"\n  Applying V11 Drawdown Shields...")
    print(f"    Shield 1: Correlation Guard (NIFTY-SENSEX same-dir block)")
    print(f"    Shield 2: Global Streak Breaker ({STREAK_THRESHOLD} losses -> skip {STREAK_SKIP})")
    print(f"    Shield 3: Daily Loss Cap (Rs {DAILY_LOSS_CAP:,})")
    print(f"    Shield 4: Per-Symbol Streak ({PER_SYM_STREAK_THRESHOLD} sym losses -> skip {PER_SYM_STREAK_SKIP})")
    print(f"    Half-Lot: Q < {HALF_LOT_QUALITY_THRESHOLD} -> half position size")

    protected_trades, shield_stats = apply_drawdown_shields_v11(all_trades)

    print(f"\n  Shield Results:")
    print(f"    Raw trades:          {len(all_trades)}")
    print(f"    Corr blocked:        {shield_stats['corr_blocked']}")
    print(f"    Streak blocked:      {shield_stats['streak_blocked']}")
    print(f"    DailyCap blocked:    {shield_stats['cap_blocked']}")
    print(f"    PerSym blocked:      {shield_stats['per_sym_blocked']}")
    print(f"    Final trades:        {len(protected_trades)}")

    # Count half-lot trades
    half_lot_count = sum(1 for t in protected_trades if getattr(t, 'is_half_lot', False))
    full_lot_count = len(protected_trades) - half_lot_count
    print(f"    Full-lot trades:     {full_lot_count}")
    print(f"    Half-lot trades:     {half_lot_count}")

    return protected_trades, total_skipped, total_vol_filtered + sum(shield_stats.values())


# ============================================================================
# ANALYSIS (enhanced for V11)
# ============================================================================
def print_v11_analysis(trades, skipped, vol_filtered):
    if not trades: return

    print(f"\n  {'='*100}")
    print(f"  V11 CONVICTION-SCALED EXECUTION — ANALYSIS")
    print(f"  {'='*100}")

    total_signals = len(trades) + skipped + vol_filtered
    print(f"\n  ENTRY FILTERING:")
    print(f"    Total CPR signals:        {total_signals}")
    print(f"    Failed trial (skipped):   {skipped} ({skipped/total_signals*100:.0f}%)")
    print(f"    Vol+Shield filtered:      {vol_filtered} ({vol_filtered/total_signals*100:.0f}%)")
    print(f"    Confirmed entries:        {len(trades)} ({len(trades)/total_signals*100:.0f}%)")

    # Per-symbol detailed breakdown
    print(f"\n  PER-SYMBOL RESULTS:")
    print(f"  {'Symbol':<12} {'Trades':>7} {'Full':>5} {'Half':>5} {'WR':>7} {'PF':>7} {'Total PnL':>12} {'Net PnL':>12} {'MaxDD':>10}")
    print(f"  {'-'*88}")

    for sym in sorted(set(t.symbol for t in trades)):
        sub = [t for t in trades if t.symbol == sym]
        sw = sum(1 for t in sub if t.pnl > 0)
        swr = sw/len(sub)*100
        sgp = sum(t.pnl for t in sub if t.pnl > 0)
        sgl = abs(sum(t.pnl for t in sub if t.pnl <= 0))
        spf = sgp/sgl if sgl > 0 else float('inf')
        stot = sum(t.pnl for t in sub)
        snet = stot - len(sub)*110
        full = sum(1 for t in sub if not getattr(t, 'is_half_lot', False))
        half = sum(1 for t in sub if getattr(t, 'is_half_lot', False))

        eq = np.cumsum([t.pnl for t in sub])
        peak = np.maximum.accumulate(eq)
        dd = peak - eq
        max_dd = dd.max()

        print(f"  {sym:<12} {len(sub):>7} {full:>5} {half:>5} {swr:>6.1f}% {spf:>6.2f} Rs {stot:>+10,.0f} Rs {snet:>+10,.0f} Rs {max_dd:>8,.0f}")

    # Direction breakdown
    print(f"\n  DIRECTION BREAKDOWN:")
    for sym in sorted(set(t.symbol for t in trades)):
        for d in ['CE', 'PE']:
            sub = [t for t in trades if t.symbol == sym and t.direction == d]
            if not sub: continue
            wr = sum(1 for t in sub if t.pnl > 0)/len(sub)*100
            pnl = sum(t.pnl for t in sub)
            print(f"    {sym:<12} {d}: {len(sub):>3} trades, WR={wr:>5.1f}%, PnL Rs {pnl:>+9,.0f}")

    # Exit reasons
    print(f"\n  EXIT REASONS:")
    exit_stats = defaultdict(lambda: {'count': 0, 'pnl': 0})
    for t in trades:
        r = t.exit_reason.split('(')[0]
        exit_stats[r]['count'] += 1
        exit_stats[r]['pnl'] += t.pnl
    for r, d2 in sorted(exit_stats.items(), key=lambda x: -x[1]['count']):
        avg = d2['pnl']/d2['count']
        print(f"    {r:<25}: {d2['count']:>4} trades, PnL Rs {d2['pnl']:>+10,.0f} (avg Rs {avg:>+,.0f})")

    # Ghost Zone impact
    ghost = [t for t in trades if getattr(t, 'ghost_boost', 0) > 0]
    non_g = [t for t in trades if getattr(t, 'ghost_boost', 0) == 0]
    if ghost:
        gw = sum(1 for t in ghost if t.pnl > 0) / len(ghost) * 100
        gg = sum(t.pnl for t in ghost if t.pnl > 0)
        gl2 = abs(sum(t.pnl for t in ghost if t.pnl <= 0))
        gpf = gg/gl2 if gl2 > 0 else float('inf')
        print(f"\n  GHOST ZONE:")
        print(f"    With Ghost:    {len(ghost)} trades, WR={gw:.1f}%, PF={gpf:.2f}, PnL Rs {sum(t.pnl for t in ghost):+,.0f}")
        if non_g:
            nw = sum(1 for t in non_g if t.pnl > 0) / len(non_g) * 100
            ng = sum(t.pnl for t in non_g if t.pnl > 0)
            nl = abs(sum(t.pnl for t in non_g if t.pnl <= 0))
            npf = ng/nl if nl > 0 else float('inf')
            print(f"    Without Ghost: {len(non_g)} trades, WR={nw:.1f}%, PF={npf:.2f}, PnL Rs {sum(t.pnl for t in non_g):+,.0f}")

    # Flip trade analysis
    flip_trades = [t for t in trades if getattr(t, 'is_flip', False)]
    if flip_trades:
        fw = sum(1 for t in flip_trades if t.pnl > 0)
        fwr = fw / len(flip_trades) * 100
        fpnl = sum(t.pnl for t in flip_trades)
        print(f"\n  SMART FLIP TRADES:")
        print(f"    Flip trades: {len(flip_trades)}, WR={fwr:.0f}%, PnL Rs {fpnl:+,.0f}")
        for ft in flip_trades:
            print(f"      {ft.entry_time.strftime('%m-%d %H:%M')} {ft.symbol} {ft.direction} "
                  f"PnL Rs {ft.pnl:+,.0f} Exit: {ft.exit_reason}")

    # Half-lot analysis
    half_trades = [t for t in trades if getattr(t, 'is_half_lot', False)]
    full_trades = [t for t in trades if not getattr(t, 'is_half_lot', False)]
    if half_trades:
        hw = sum(1 for t in half_trades if t.pnl > 0) / len(half_trades) * 100
        hpnl = sum(t.pnl for t in half_trades)
        hgp = sum(t.pnl for t in half_trades if t.pnl > 0)
        hgl = abs(sum(t.pnl for t in half_trades if t.pnl <= 0))
        hpf = hgp/hgl if hgl > 0 else float('inf')
        print(f"\n  CONVICTION-BASED SIZING (V11):")
        print(f"    Full-lot (Q>={HALF_LOT_QUALITY_THRESHOLD}):  {len(full_trades)} trades, WR={sum(1 for t in full_trades if t.pnl > 0)/len(full_trades)*100:.1f}%, PnL Rs {sum(t.pnl for t in full_trades):+,.0f}")
        print(f"    Half-lot (Q<{HALF_LOT_QUALITY_THRESHOLD}):   {len(half_trades)} trades, WR={hw:.1f}%, PF={hpf:.2f}, PnL Rs {hpnl:+,.0f}")
        print(f"    DD saved by half-lot: lower-conviction losers lose HALF as much")

    # Capital analysis
    print(f"\n  CAPITAL USAGE:")
    caps = [t.entry_premium * t.lot_size for t in trades]
    print(f"    Avg capital per trade:   Rs {np.mean(caps):,.0f}")
    print(f"    Max capital per trade:   Rs {max(caps):,.0f}")

    events = []
    for i, t in enumerate(trades):
        cap = t.entry_premium * t.lot_size
        events.append((t.entry_time, 'ENTER', cap))
        events.append((t.exit_time, 'EXIT', cap))
    events.sort(key=lambda x: (x[0], 0 if x[1]=='EXIT' else 1))
    cur_cap = 0; peak_cap = 0
    for time, action, cap in events:
        cur_cap += cap if action == 'ENTER' else -cap
        peak_cap = max(peak_cap, cur_cap)
    print(f"    Peak concurrent capital: Rs {peak_cap:,.0f}")


# ============================================================================
# MAIN
# ============================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--symbols', nargs='+', default=['NIFTY', 'BANKNIFTY', 'SENSEX'])
    parser.add_argument('--no-download', action='store_true')
    parser.add_argument('--sample', type=int, default=15)
    args = parser.parse_args()

    print("=" * 110)
    print("  ZERODHA BACKTEST V11 — CONVICTION-SCALED EXECUTION ENGINE")
    print(f"  V10 + Half-Lot (Q<{HALF_LOT_QUALITY_THRESHOLD}) + Per-Symbol Streak ({PER_SYM_STREAK_THRESHOLD} -> skip {PER_SYM_STREAK_SKIP})")
    print(f"  Symbols: {', '.join(args.symbols)} | Days: {args.days}")
    print("=" * 110)

    if args.no_download:
        data = load_cached_data(args.symbols, args.days)
    else:
        data = download_zerodha_data(args.symbols, args.days)
    if not data: print("  No data."); return

    trades, skipped, vol_filtered = run_full_v11(data, args.symbols)
    metrics = compute_metrics(trades, "V11 — CONVICTION-SCALED")

    print_report(metrics)
    print_v11_analysis(trades, skipped, vol_filtered)

    if args.sample > 0 and trades:
        print_sample_trades(trades, "V11", args.sample)

    # Cost analysis
    tp = metrics['total_pnl']
    nt = metrics['total_trades']
    stt = nt * 20; slip = nt * 50
    net = tp - stt - slip
    print(f"\n  {'='*80}")
    print(f"  REAL-WORLD COST ANALYSIS")
    print(f"  {'='*80}")
    print(f"    Gross PnL:         Rs {tp:+,.0f}")
    print(f"    Brokerage:         Rs -{nt*40:,.0f} (included in PnL)")
    print(f"    STT + Exchange:    Rs -{stt:,.0f}")
    print(f"    Slippage:          Rs -{slip:,.0f}")
    print(f"    Net after costs:   Rs {net:+,.0f}")
    print(f"    On Rs 3L capital:  {net/300000*100:.1f}% in 90 days")
    print(f"    Annualized:        {net/300000*100/90*365:.1f}%")

    # ── V10 vs V11 comparison ──
    print(f"\n  {'='*80}")
    print(f"  V10 vs V11 COMPARISON")
    print(f"  {'='*80}")

    # Run V10 for comparison (import and run)
    try:
        import zerodha_backtest_v10 as v10
        data2 = load_cached_data(args.symbols, args.days)
        print(f"\n  Running V10 baseline for comparison...")

        # Suppress V10 output
        import io as _io
        old_stdout = sys.stdout
        sys.stdout = _io.StringIO()
        v10_trades, _, _ = v10.run_full_v10(data2, args.symbols)
        sys.stdout = old_stdout

        v10_eq = np.cumsum([t.pnl for t in v10_trades])
        v10_pk = np.maximum.accumulate(v10_eq)
        v10_dd = (v10_pk - v10_eq).max()
        v10_gp = sum(t.pnl for t in v10_trades if t.pnl > 0)
        v10_gl = abs(sum(t.pnl for t in v10_trades if t.pnl <= 0))
        v10_pf = v10_gp/v10_gl if v10_gl > 0 else 999
        v10_net = sum(t.pnl for t in v10_trades) - len(v10_trades)*110

        v11_eq = np.cumsum([t.pnl for t in trades])
        v11_pk = np.maximum.accumulate(v11_eq)
        v11_dd = (v11_pk - v11_eq).max()
        v11_pf = metrics.get('profit_factor', 0)
        v11_net = sum(t.pnl for t in trades) - len(trades)*110

        print(f"\n  {'Metric':<25} {'V10':>15} {'V11':>15} {'Change':>15}")
        print(f"  {'-'*70}")
        print(f"  {'Trades':<25} {len(v10_trades):>15} {len(trades):>15} {len(trades)-len(v10_trades):>+15}")
        print(f"  {'Profit Factor':<25} {v10_pf:>15.2f} {v11_pf:>15.2f} {v11_pf-v10_pf:>+15.2f}")
        print(f"  {'Max Drawdown':<25} Rs {v10_dd:>12,.0f} Rs {v11_dd:>12,.0f} {(v11_dd-v10_dd)/v10_dd*100:>+13.0f}%")
        print(f"  {'Net PnL':<25} Rs {v10_net:>12,.0f} Rs {v11_net:>12,.0f} Rs {v11_net-v10_net:>+12,.0f}")
    except Exception as e:
        print(f"  (V10 comparison skipped: {e})")

    # Save
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    rows = [{
        'symbol': t.symbol, 'direction': t.direction, 'strike': t.strike,
        'entry_time': t.entry_time, 'exit_time': t.exit_time,
        'entry_spot': t.entry_spot, 'exit_spot': t.exit_spot,
        'entry_premium': t.entry_premium, 'exit_premium': t.exit_premium,
        'target': t.target, 'sl': t.sl, 'pnl': t.pnl, 'pnl_pct': t.pnl_pct,
        'hold_minutes': t.hold_minutes, 'peak_premium': t.peak_premium,
        'peak_gain_pct': t.peak_gain_pct, 'profit_capture_pct': t.profit_capture_pct,
        'exit_reason': t.exit_reason, 'trail_phase': t.trail_phase,
        'ghost_boost': getattr(t, 'ghost_boost', 0),
        'quality_score': getattr(t, 'quality_score', 0),
        'lot_size': t.lot_size,
        'is_half_lot': getattr(t, 'is_half_lot', False),
        'is_flip': getattr(t, 'is_flip', False),
    } for t in trades]
    if rows:
        fn = f'backtest_v11_{args.days}d_{ts}.csv'
        pd.DataFrame(rows).to_csv(RESULTS_DIR / fn, index=False)
        print(f"\n  Saved: {fn}")

    pf = metrics.get('profit_factor', 0)
    print(f"\n{'='*110}")
    print(f"  V11 COMPLETE — PF {pf:.2f} | ALL SYMBOLS PF >= 2.0: ", end='')

    all_above = True
    for sym in args.symbols:
        sub = [t for t in trades if t.symbol == sym]
        if sub:
            sgp = sum(t.pnl for t in sub if t.pnl > 0)
            sgl = abs(sum(t.pnl for t in sub if t.pnl <= 0))
            spf = sgp/sgl if sgl > 0 else float('inf')
            status = 'Y' if spf >= 2.0 else 'X'
            print(f"{sym}={spf:.2f}[{status}] ", end='')
            if spf < 2.0: all_above = False

    print(f"\n  {'IMPLEMENTABLE' if all_above else 'NEEDS WORK'}")
    print(f"{'='*110}")


if __name__ == '__main__':
    main()
