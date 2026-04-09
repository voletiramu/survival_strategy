"""
V12 — Physics-Based Entry Filter integrated into full backtest
Adds wave position, VWAP stretch, acceleration checks to V11 engine
"""
import sys, os, io, warnings, copy
warnings.filterwarnings('ignore')
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')
from zerodha_backtest import *
if sys.platform == 'win32':
    sys.stdout = open(os.dup(1), mode='w', encoding='utf-8', buffering=1)

import zerodha_backtest_v11 as v11
import numpy as np
from collections import defaultdict
from scipy.signal import hilbert

data = load_cached_data(['NIFTY','BANKNIFTY','SENSEX'], 90)

# ============================================================================
# PHYSICS GATE — compute at entry bar, return score
# ============================================================================
def physics_gate(day_df, bar_idx, direction, spot):
    """Compute physics signals and return a score. Negative = don't enter."""
    prices = day_df['Close'].values[:bar_idx+1]
    if len(prices) < 6:
        return 0, {}

    score = 0
    diag = {}

    # 1. ACCELERATION (d²p/dt²)
    if len(prices) >= 5:
        v1 = prices[-3] - prices[-5]
        v2 = prices[-1] - prices[-3]
        accel = (v2 - v1) / spot * 10000
        diag['accel'] = accel

        if direction == 'CE' and accel > 0:
            score += min(15, abs(accel) * 3)
        elif direction == 'PE' and accel < 0:
            score += min(15, abs(accel) * 3)
        else:
            # Momentum DYING — deceleration against our direction
            mom3 = (prices[-1] - prices[-4]) / prices[-4] * 100
            if direction == 'CE' and mom3 > 0 and accel < -2:
                score -= 15
                diag['mom_dying'] = True
            elif direction == 'PE' and mom3 < 0 and accel > 2:
                score -= 15
                diag['mom_dying'] = True

    # 2. WAVE POSITION
    if len(prices) >= 10:
        recent = prices[-10:]
        local_max = max(recent)
        local_min = min(recent)
        wave_range = local_max - local_min
        if wave_range > 0:
            wave_pos = (spot - local_min) / wave_range
            if direction == 'CE':
                wave_risk = wave_pos
            else:
                wave_risk = 1 - wave_pos
            diag['wave_risk'] = wave_risk

            if wave_risk < 0.5:
                score += 10
            elif wave_risk > 0.85:
                score -= 15
                diag['wave_peak'] = True

    # 3. VWAP STRETCH (mean reversion pressure)
    volumes = day_df['Volume'].values[:bar_idx+1] if 'Volume' in day_df.columns else None
    if volumes is not None and np.sum(volumes) > 0:
        vwap = np.cumsum(prices * volumes) / np.cumsum(volumes)
        vwap_dist = (spot - vwap[-1]) / vwap[-1] * 100
    else:
        vwap_val = np.cumsum(prices) / np.arange(1, len(prices)+1)
        vwap_dist = (spot - vwap_val[-1]) / vwap_val[-1] * 100

    if direction == 'CE':
        stretch = vwap_dist
    else:
        stretch = -vwap_dist
    diag['stretch'] = stretch

    if abs(stretch) < 0.1:
        score += 5
    elif abs(stretch) > 0.3:
        score -= 10
        diag['stretched'] = True

    # 4. ENERGY — is it building or dissipating?
    if len(prices) >= 8:
        mom_now = (prices[-1] - prices[-4]) / prices[-4] * 100
        mom_prev = (prices[-4] - prices[-7]) / prices[-7] * 100
        if abs(mom_now) > abs(mom_prev):
            score += 5
        else:
            score -= 5

    # 5. RSI extreme
    if len(prices) >= 15:
        deltas = np.diff(prices[-15:])
        gains = deltas[deltas > 0]
        losses_arr = -deltas[deltas < 0]
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses_arr) if len(losses_arr) > 0 else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        diag['rsi'] = rsi
        if direction == 'CE' and rsi > 75:
            score -= 10
        elif direction == 'PE' and rsi < 25:
            score -= 10

    return score, diag


# ============================================================================
# MONKEY-PATCH V11 to add physics gate at signal detection
# ============================================================================
# Save original run_day
_orig_run_day = v11.run_day_v11

def run_day_v12(symbol, day_df, cpr, calc, daily_counts, last_entry_bars,
                gz_detector, df_all, cfg, physics_threshold=0):
    """V11 day runner with physics gate added before trial starts."""
    lot_size = LOT_SIZES[symbol]
    strike_interval = STRIKE_INTERVALS[symbol]
    trades = []
    active_trade = None
    mom_ctr = 0
    pending = None
    skipped = 0
    filtered_by_vol = 0
    physics_blocked = 0

    prices = day_df['Close'].reset_index(drop=True)
    volumes = day_df['Volume'].reset_index(drop=True) if 'Volume' in day_df.columns else None
    times = day_df.index.tolist()
    trade_date = times[0].date()
    dte = v11.get_dte(trade_date)
    key = (symbol, trade_date)

    gz_detector.update_zones(symbol, df_all, trade_date)

    for i in range(len(prices)):
        t = times[i]
        ct = t.time()
        spot = prices.iloc[i]
        mins = t.hour * 60 + t.minute

        # EXIT ACTIVE TRADE (same as V11)
        if active_trade is not None:
            cp = estimate_premium_evolution(
                active_trade.entry_spot, spot, active_trade.strike,
                active_trade.direction, active_trade.iv, active_trade.dte_days,
                active_trade.entry_minutes, mins, active_trade.entry_premium
            )
            if cp > active_trade.peak_premium: active_trade.peak_premium = cp
            if cp < active_trade.trough_premium: active_trade.trough_premium = cp

            exit_reason, mom_ctr = v11.check_exit_v10(active_trade, cp, ct, t, mom_ctr,
                                                       current_spot=spot)
            if exit_reason:
                active_trade.exit_time = t
                active_trade.exit_spot = spot
                active_trade.exit_premium = cp
                active_trade.exit_reason = exit_reason
                trades.append(active_trade)

                if exit_reason.startswith('FLIP_EXIT'):
                    opp_dir = 'PE' if active_trade.direction == 'CE' else 'CE'
                    opp_strike = round(spot / strike_interval) * strike_interval
                    opp_premium = estimate_premium_at(
                        spot, opp_strike, opp_dir, DEFAULT_IV, dte, mins)
                    if opp_premium >= cfg.get('min_premium', 20):
                        opp_target = opp_premium * v11.FLIP_TARGET_MULT
                        opp_sl = opp_premium * v11.FLIP_SL_MULT
                        flip_trade = Trade(
                            symbol=symbol, direction=opp_dir, strike=opp_strike,
                            entry_time=t, entry_spot=spot,
                            entry_premium=opp_premium,
                            target=opp_target, sl=opp_sl,
                            iv=DEFAULT_IV, dte_days=dte,
                            entry_minutes=mins,
                            peak_premium=opp_premium,
                            trough_premium=opp_premium,
                            lot_size=lot_size,
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

        # MONITOR PENDING TRIAL (same as V11)
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
                        atr_pct = v11.compute_recent_atr(day_df, min(i, len(day_df)-1))
                        if atr_pct > 0.15:
                            sl_mult = min(sl_mult + 0.03, 0.80)
                    sl = entry_premium * sl_mult

                    trade_lot = lot_size
                    if pending.quality < v11.HALF_LOT_QUALITY_THRESHOLD:
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
                    active_trade.physics_score = getattr(pending, '_physics_score', 0)
                    mom_ctr = 0
                    pending = None
                else:
                    skipped += 1
                    pending = None
                continue
            continue

        # NEW SIGNAL DETECTION (V11 + physics gate)
        if active_trade is not None or pending is not None:
            continue
        if ct < FIRST_TRADE or ct >= LAST_ENTRY:
            continue
        if daily_counts.get(key, 0) >= v11.V10_MAX_TRADES:
            continue
        lb = last_entry_bars.get(symbol, -v11.V10_MIN_BARS)
        if i - lb < v11.V10_MIN_BARS:
            continue
        if v11.V10_BLOCK_START <= ct < v11.V10_BLOCK_END:
            continue

        direction = None
        if spot > cpr['tc']:
            ib = spot - day_df['Open'].iloc[0]
            vwap = v11._vwap(day_df, i)
            if ib < 0 and vwap > 0 and spot < vwap:
                continue
            direction = 'CE'
        elif spot < cpr['bc']:
            ib = spot - day_df['Open'].iloc[0]
            vwap = v11._vwap(day_df, i)
            if ib > 0 and vwap > 0 and spot > vwap:
                continue
            direction = 'PE'
        if direction is None:
            continue

        quality = v11.quick_quality(day_df, i, direction, spot, cpr)
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

        # ═══════════════════════════════════════════════
        # V12: PHYSICS GATE — mathematical signal quality
        # ═══════════════════════════════════════════════
        phy_score, phy_diag = physics_gate(day_df, i, direction, spot)
        if phy_score < physics_threshold:
            physics_blocked += 1
            continue

        target_mult = v11.get_target(cpr['width_pct'], ghost_boost, cfg)

        pending_sig = v11.PendingSignal(
            symbol=symbol, direction=direction, strike=strike,
            trigger_time=t, trigger_spot=spot,
            trigger_premium=trigger_premium,
            trigger_bar_idx=i,
            target_mult=target_mult, cpr=cpr,
            quality=quality, ghost_boost=ghost_boost,
            peak_premium=trigger_premium,
        )
        pending_sig._physics_score = phy_score
        pending = pending_sig
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

    return trades, skipped, filtered_by_vol, physics_blocked


def run_full_v12(data_dict, symbols, physics_threshold=0):
    all_trades = []
    total_skipped = 0
    total_vol_filtered = 0
    total_physics_blocked = 0
    calc = MarketCalculus()
    gz = v11.GhostZoneDetector()

    for symbol in symbols:
        if symbol not in data_dict: continue
        df = data_dict[symbol]
        cfg = v11.SYMBOL_CONFIGS.get(symbol, v11.DEFAULT_CFG)

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

            leb = {s: -v11.V10_MIN_BARS for s in symbols}
            day_trades, day_skipped, day_vol, day_phy = run_day_v12(
                symbol, ddf, cpr, calc, daily_counts, leb, gz, df, cfg,
                physics_threshold=physics_threshold)
            sym_trades.extend(day_trades)
            total_skipped += day_skipped
            total_vol_filtered += day_vol
            total_physics_blocked += day_phy
            prev = ddf

        all_trades.extend(sym_trades)

    # Apply V11 shields
    protected, shield_stats = v11.apply_drawdown_shields_v11(all_trades)
    return protected, total_skipped, total_vol_filtered, total_physics_blocked, shield_stats


# ============================================================================
# RUN ALL THRESHOLDS
# ============================================================================
print('='*120)
print('  V12 PHYSICS-BASED BACKTEST — NIFTY + BANKNIFTY + SENSEX (90 days)')
print('='*120)

thresholds = [-25, -20, -15, -10, -5, 0, 5, 10, 15, 20]

print(f'\n  {"Threshold":<12} {"Trades":>7} {"Full":>5} {"Half":>5} {"WR":>7} {"PF":>7} '
      f'{"Net PnL":>12} {"MaxDD":>10} {"Sharpe":>7} {"PhyBlk":>7}')
print(f'  {"-"*100}')

results = []

for th in thresholds:
    trades, sk, vf, pb, ss = run_full_v12(data, ['NIFTY','BANKNIFTY','SENSEX'],
                                           physics_threshold=th)
    if not trades:
        continue

    wr = sum(1 for t in trades if t.pnl > 0) / len(trades) * 100
    gp = sum(t.pnl for t in trades if t.pnl > 0)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    pf = gp/gl if gl > 0 else 999
    net = sum(t.pnl for t in trades) - len(trades) * 110
    eq = np.cumsum([t.pnl for t in trades])
    dd = (np.maximum.accumulate(eq) - eq).max()
    sh = np.mean([t.pnl for t in trades])/np.std([t.pnl for t in trades])*np.sqrt(252) if np.std([t.pnl for t in trades]) > 0 else 0
    full = sum(1 for t in trades if not getattr(t, 'is_half_lot', False))
    half = sum(1 for t in trades if getattr(t, 'is_half_lot', False))

    print(f'  Phy >= {th:>+3}   {len(trades):>7} {full:>5} {half:>5} {wr:>6.1f}% {pf:>6.2f} '
          f'Rs {net:>+10,.0f} Rs {dd:>8,.0f} {sh:>6.2f} {pb:>7}')

    results.append({
        'threshold': th, 'trades': trades, 'count': len(trades),
        'wr': wr, 'pf': pf, 'net': net, 'dd': dd, 'sharpe': sh,
        'physics_blocked': pb, 'full': full, 'half': half
    })

# ============================================================================
# BEST THRESHOLD — Detailed report
# ============================================================================
# Find best by PF * net / dd (risk-adjusted return)
for r in results:
    r['score'] = r['pf'] * max(r['net'], 0) / max(r['dd'], 1)

best = max(results, key=lambda x: x['score'])
print(f'\n  BEST THRESHOLD: Physics >= {best["threshold"]:+d} '
      f'(score={best["score"]:.2f})')

# Detailed per-symbol for top 3
top3 = sorted(results, key=lambda x: -x['score'])[:3]

for r in top3:
    th = r['threshold']
    trades = r['trades']
    print(f'\n  {"="*100}')
    print(f'  DETAILED: Physics >= {th:+d} | {r["count"]} trades | PF {r["pf"]:.2f} | DD Rs {r["dd"]:,.0f} | Net Rs {r["net"]:+,.0f}')
    print(f'  {"="*100}')

    print(f'\n  PER-SYMBOL:')
    print(f'  {"Symbol":<12} {"Trades":>7} {"Full":>5} {"Half":>5} {"WR":>7} {"PF":>7} {"Net PnL":>12} {"MaxDD":>10}')
    print(f'  {"-"*70}')

    for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        sub = [t for t in trades if t.symbol == sym]
        if not sub: continue
        sw = sum(1 for t in sub if t.pnl > 0)
        swr = sw/len(sub)*100
        sgp = sum(t.pnl for t in sub if t.pnl > 0)
        sgl = abs(sum(t.pnl for t in sub if t.pnl <= 0))
        spf = sgp/sgl if sgl > 0 else 999
        snet = sum(t.pnl for t in sub) - len(sub)*110
        seq = np.cumsum([t.pnl for t in sub])
        sdd = (np.maximum.accumulate(seq) - seq).max()
        sf = sum(1 for t in sub if not getattr(t, 'is_half_lot', False))
        sh = sum(1 for t in sub if getattr(t, 'is_half_lot', False))
        print(f'  {sym:<12} {len(sub):>7} {sf:>5} {sh:>5} {swr:>6.1f}% {spf:>6.2f} Rs {snet:>+10,.0f} Rs {sdd:>8,.0f}')

    print(f'\n  EXIT REASONS:')
    exit_stats = defaultdict(lambda: {'count': 0, 'pnl': 0})
    for t in trades:
        r2 = t.exit_reason.split('(')[0]
        exit_stats[r2]['count'] += 1
        exit_stats[r2]['pnl'] += t.pnl
    for r2, d2 in sorted(exit_stats.items(), key=lambda x: -x[1]['count']):
        avg = d2['pnl']/d2['count']
        print(f'    {r2:<22}: {d2["count"]:>3} trades, PnL Rs {d2["pnl"]:>+10,.0f} (avg Rs {avg:>+,.0f})')

    print(f'\n  DIRECTION:')
    for d in ['CE', 'PE']:
        sub = [t for t in trades if t.direction == d]
        if not sub: continue
        dwr = sum(1 for t in sub if t.pnl > 0)/len(sub)*100
        dpnl = sum(t.pnl for t in sub)
        print(f'    {d}: {len(sub)} trades, WR={dwr:.1f}%, PnL Rs {dpnl:+,.0f}')

    # Flip trades
    flips = [t for t in trades if getattr(t, 'is_flip', False)]
    if flips:
        fwr = sum(1 for t in flips if t.pnl > 0)/len(flips)*100
        fpnl = sum(t.pnl for t in flips)
        print(f'\n  SMART FLIPS: {len(flips)} trades, WR={fwr:.0f}%, PnL Rs {fpnl:+,.0f}')

    # Cost analysis
    tp = sum(t.pnl for t in trades)
    nt = len(trades)
    stt = nt * 20; slip = nt * 50
    net_real = tp - stt - slip
    print(f'\n  REAL COSTS: Gross Rs {tp:+,.0f} - STT Rs {stt:,.0f} - Slip Rs {slip:,.0f} = Net Rs {net_real:+,.0f}')
    print(f'  On Rs 3L: {net_real/300000*100:.1f}% in 90d | Annualized: {net_real/300000*100/90*365:.1f}%')

# ============================================================================
# EVOLUTION TABLE
# ============================================================================
print(f'\n\n  {"="*100}')
print(f'  STRATEGY EVOLUTION TABLE')
print(f'  {"="*100}')

print(f'\n  {"Version":<45} {"Trades":>7} {"WR":>7} {"PF":>7} {"Net PnL":>12} {"MaxDD":>10} {"Sharpe":>7}')
print(f'  {"-"*100}')

# Run V10 baseline for comparison
old_stdout = sys.stdout
sys.stdout = io.StringIO()
v10_trades, _, _ = v11.run_full_v11.__wrapped__(data, ['NIFTY','BANKNIFTY','SENSEX']) if hasattr(v11.run_full_v11, '__wrapped__') else ([], 0, 0)
sys.stdout = old_stdout

# Just use stored results from previous runs
baselines = [
    ('V1 CPR Basic', 224, 45.5, 1.35, -2500, 28000, 0.8),
    ('V8 Symbol-Specific', 174, 52.9, 2.36, 55000, 17322, 4.5),
    ('V9 +DD Shields', 117, 53.0, 2.46, 73603, 8573, 6.52),
    ('V10 +Smart Flip', 117, 53.0, 2.49, 77465, 8573, 6.44),
    ('V11 +HalfLot+PerSymStreak', 113, 54.9, 2.65, 61073, 4350, 6.53),
]

for name, nt, wr, pf, net, dd, sh in baselines:
    print(f'  {name:<45} {nt:>7} {wr:>6.1f}% {pf:>6.2f} Rs {net:>+10,.0f} Rs {dd:>8,.0f} {sh:>6.2f}')

# Add V12 results
for r in sorted(results, key=lambda x: -x['score'])[:3]:
    th = r['threshold']
    name = f'V12 Physics(>={th:+d})+HalfLot+PerSym'
    print(f'  {name:<45} {r["count"]:>7} {r["wr"]:>6.1f}% {r["pf"]:>6.2f} Rs {r["net"]:>+10,.0f} Rs {r["dd"]:>8,.0f} {r["sharpe"]:>6.2f}')

print(f'\n  NOTE: All results on NIFTY+BANKNIFTY+SENSEX, 90 trading days, 5-min bars')
print(f'  BS-estimated premiums (10-20%% off from real). Brokerage Rs 110/trade included.')
