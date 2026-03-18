"""Analyze WHY losing trades fail — momentum, gamma, premium behavior"""
import sys, os, io, warnings
warnings.filterwarnings('ignore')
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')
from zerodha_backtest import *
if sys.platform == 'win32':
    sys.stdout = open(os.dup(1), mode='w', encoding='utf-8', buffering=1)

import zerodha_backtest_v10 as v10
import numpy as np
from collections import defaultdict
from scipy.stats import norm

data = load_cached_data(['NIFTY','BANKNIFTY','SENSEX'], 90)

# We need raw day data to reconstruct what happened BAR BY BAR
# Run V10 to get trades, but also capture the day data for analysis
trades_raw, _, _ = v10.run_full_v10(data, ['NIFTY','BANKNIFTY','SENSEX'])
trades_raw.sort(key=lambda t: t.entry_time)

losers = [t for t in trades_raw if t.pnl <= 0]
winners = [t for t in trades_raw if t.pnl > 0]

print('='*120)
print('  FORENSIC ANALYSIS: WHY DO LOSING TRADES FAIL?')
print('='*120)

print(f'\n  Total: {len(trades_raw)} trades | {len(winners)} winners | {len(losers)} losers')

# ============================================================================
# 1. PEAK GAIN ANALYSIS — Did the trade EVER go in our favor?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  1. PEAK GAIN BEFORE DEATH — Did the trade ever show life?')
print(f'  {"="*100}')

never_positive = [t for t in losers if t.peak_gain_pct <= 0]
had_peak_small = [t for t in losers if 0 < t.peak_gain_pct <= 3]
had_peak_med   = [t for t in losers if 3 < t.peak_gain_pct <= 10]
had_peak_big   = [t for t in losers if t.peak_gain_pct > 10]

print(f'  Losers with ZERO peak gain (never went positive):  {len(never_positive):>3} ({len(never_positive)/len(losers)*100:.0f}%) — WRONG DIRECTION')
print(f'  Losers with peak 0-3% (barely moved):              {len(had_peak_small):>3} ({len(had_peak_small)/len(losers)*100:.0f}%) — FLAT/WEAK MOMENTUM')
print(f'  Losers with peak 3-10% (had profit, gave it back): {len(had_peak_med):>3} ({len(had_peak_med)/len(losers)*100:.0f}%) — EXIT PROBLEM')
print(f'  Losers with peak >10% (big gain, lost it all):     {len(had_peak_big):>3} ({len(had_peak_big)/len(losers)*100:.0f}%) — EXIT PROBLEM')

total_loss_never = sum(t.pnl for t in never_positive)
total_loss_small = sum(t.pnl for t in had_peak_small)
total_loss_med   = sum(t.pnl for t in had_peak_med)
total_loss_big   = sum(t.pnl for t in had_peak_big)

print(f'\n  Loss from ZERO-peak (wrong direction):   Rs {total_loss_never:>+10,.0f}')
print(f'  Loss from 0-3% peak (flat premium):      Rs {total_loss_small:>+10,.0f}')
print(f'  Loss from 3-10% peak (gave back profit):  Rs {total_loss_med:>+10,.0f}')
print(f'  Loss from >10% peak (gave back big gain): Rs {total_loss_big:>+10,.0f}')

# ============================================================================
# 2. EACH LOSER DETAILED — What happened bar by bar?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  2. EVERY LOSING TRADE — What went wrong?')
print(f'  {"="*100}')

print(f'\n  {"#":>3} {"Date":<12} {"Sym":<10} {"Dir":<4} {"Q":>3} {"EntPrem":>8} {"PkGain":>7} {"Final":>7} '
      f'{"HoldMin":>7} {"SpotMove":>9} {"Exit":<20} {"PnL":>10} {"Diagnosis":<30}')
print(f'  {"-"*145}')

for i, t in enumerate(losers):
    q = getattr(t, 'quality_score', 0)
    pk = t.peak_gain_pct
    final_pct = t.pnl_pct
    hold = t.hold_minutes

    # Spot movement: did spot go in our direction or against?
    spot_move = (t.exit_spot - t.entry_spot) / t.entry_spot * 100
    spot_dir = 'UP' if spot_move > 0 else 'DOWN'

    # Diagnosis
    if pk <= 0:
        if t.direction == 'CE' and spot_move < -0.1:
            diag = 'WRONG DIR: spot fell'
        elif t.direction == 'PE' and spot_move > 0.1:
            diag = 'WRONG DIR: spot rose'
        elif abs(spot_move) < 0.1:
            diag = 'FLAT SPOT: no movement'
        else:
            diag = f'GAMMA DECAY: spot {spot_dir} but prem died'
    elif pk <= 3:
        diag = 'WEAK MOMENTUM: barely moved'
    elif pk <= 10:
        diag = f'GAVE BACK: had +{pk:.0f}% profit'
    else:
        diag = f'BIG REVERSAL: had +{pk:.0f}% profit'

    is_flip = 'FLIP' if getattr(t, 'is_flip', False) else ''

    print(f'  {i+1:>3} {t.entry_time.strftime("%m-%d %H:%M"):<12} {t.symbol:<10} {t.direction:<4} {q:>3} '
          f'{t.entry_premium:>8.1f} {pk:>+6.1f}% {final_pct:>+6.1f}% {hold:>6.0f}m '
          f'{spot_move:>+8.3f}% {t.exit_reason:<20} Rs {t.pnl:>+9,.0f} {diag:<30} {is_flip}')

# ============================================================================
# 3. SPOT vs PREMIUM DIVERGENCE — When spot moves right but premium doesn't
# ============================================================================
print(f'\n  {"="*100}')
print(f'  3. SPOT vs PREMIUM DIVERGENCE (Gamma/Theta problem)')
print(f'  {"="*100}')

print(f'\n  Checking: When spot moved in our favor, did premium follow?')

for t in trades_raw:
    spot_move = (t.exit_spot - t.entry_spot) / t.entry_spot * 100
    prem_move = t.pnl_pct

    # Spot moved in our direction but we lost
    spot_favorable = (t.direction == 'CE' and spot_move > 0.05) or \
                     (t.direction == 'PE' and spot_move < -0.05)

    if spot_favorable and t.pnl < 0:
        print(f'  DIVERGENCE: {t.entry_time.strftime("%m-%d %H:%M")} {t.symbol} {t.direction} '
              f'Spot {spot_move:+.3f}% but Premium {prem_move:+.1f}% '
              f'| Hold={t.hold_minutes:.0f}m Q={getattr(t,"quality_score",0)} '
              f'Exit={t.exit_reason} PnL=Rs {t.pnl:+,.0f}')

# ============================================================================
# 4. QUALITY SCORE ANALYSIS — Are low-Q trades the real problem?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  4. QUALITY SCORE vs OUTCOME')
print(f'  {"="*100}')

q_bins = [(0,50,'Q 0-50'), (50,60,'Q 50-60'), (60,65,'Q 60-65'),
          (65,70,'Q 65-70'), (70,75,'Q 70-75'), (75,80,'Q 75-80'),
          (80,90,'Q 80-90'), (90,101,'Q 90-100')]

print(f'\n  {"Q Range":<12} {"Trades":>7} {"WR":>7} {"PF":>7} {"AvgPnL":>10} {"AvgPkGain":>10} {"TotalPnL":>12} {"ZeroPeak":>9}')
print(f'  {"-"*85}')

for lo, hi, label in q_bins:
    sub = [t for t in trades_raw if lo <= getattr(t, 'quality_score', 0) < hi]
    if not sub: continue
    wr = sum(1 for t in sub if t.pnl > 0) / len(sub) * 100
    gp = sum(t.pnl for t in sub if t.pnl > 0)
    gl = abs(sum(t.pnl for t in sub if t.pnl <= 0))
    pf = gp/gl if gl > 0 else 999
    avg = np.mean([t.pnl for t in sub])
    avg_pk = np.mean([t.peak_gain_pct for t in sub])
    tot = sum(t.pnl for t in sub)
    zero_pk = sum(1 for t in sub if t.peak_gain_pct <= 0)
    print(f'  {label:<12} {len(sub):>7} {wr:>6.1f}% {pf:>6.2f} Rs {avg:>+8,.0f} {avg_pk:>+9.1f}% Rs {tot:>+10,.0f} {zero_pk:>9}')

# ============================================================================
# 5. TIME-OF-DAY ANALYSIS — When do wrong trades happen?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  5. TIME-OF-DAY: When do wrong-direction entries happen?')
print(f'  {"="*100}')

print(f'\n  {"Hour":<8} {"Trades":>7} {"WR":>7} {"PF":>7} {"ZeroPk":>7} {"AvgPkGain":>10} {"TotalPnL":>12}')
print(f'  {"-"*65}')

for hr in range(9, 16):
    sub = [t for t in trades_raw if t.entry_time.hour == hr]
    if not sub: continue
    wr = sum(1 for t in sub if t.pnl > 0) / len(sub) * 100
    gp = sum(t.pnl for t in sub if t.pnl > 0)
    gl = abs(sum(t.pnl for t in sub if t.pnl <= 0))
    pf = gp/gl if gl > 0 else 999
    zero_pk = sum(1 for t in sub if t.peak_gain_pct <= 0)
    avg_pk = np.mean([t.peak_gain_pct for t in sub])
    tot = sum(t.pnl for t in sub)
    print(f'  {hr:>2}:00   {len(sub):>7} {wr:>6.1f}% {pf:>6.2f} {zero_pk:>7} {avg_pk:>+9.1f}% Rs {tot:>+10,.0f}')

# ============================================================================
# 6. BAR-BY-BAR PREMIUM EVOLUTION for worst losers
# ============================================================================
print(f'\n  {"="*100}')
print(f'  6. PREMIUM EVOLUTION — Top 10 worst losers bar-by-bar')
print(f'  {"="*100}')

worst = sorted(losers, key=lambda t: t.pnl)[:10]

for t in worst:
    sym = t.symbol
    df = data[sym]

    # Get the day's data
    mask = df.index.date == t.entry_time.date()
    day_df = df[mask]

    entry_idx = None
    for idx, ts in enumerate(day_df.index):
        if ts >= t.entry_time:
            entry_idx = idx
            break

    if entry_idx is None: continue

    print(f'\n  --- {t.entry_time.strftime("%m-%d %H:%M")} {sym} {t.direction} Strike={t.strike} '
          f'Q={getattr(t,"quality_score",0)} PnL=Rs {t.pnl:+,.0f} Exit={t.exit_reason} ---')

    dte = max((3 - t.entry_time.date().weekday()) % 7, 0.1)

    # Show bar-by-bar evolution
    print(f'  {"Bar":>4} {"Time":<8} {"Spot":>10} {"SpotChg":>8} {"EstPrem":>8} {"PremChg":>8} {"Gain%":>7} {"Status":<20}')

    end_idx = None
    for idx, ts in enumerate(day_df.index):
        if ts >= t.exit_time:
            end_idx = idx + 1
            break
    if end_idx is None: end_idx = len(day_df)

    for j in range(entry_idx, min(end_idx + 2, len(day_df))):
        bar_time = day_df.index[j]
        spot = day_df['Close'].iloc[j]
        mins = bar_time.hour * 60 + bar_time.minute

        prem = estimate_premium_evolution(
            t.entry_spot, spot, t.strike,
            t.direction, t.iv, t.dte_days,
            t.entry_minutes, mins, t.entry_premium
        )

        spot_chg = (spot - t.entry_spot) / t.entry_spot * 100
        prem_chg = (prem - t.entry_premium) / t.entry_premium * 100
        bar_num = j - entry_idx

        status = ''
        if bar_num == 0: status = '<-- ENTRY'
        elif bar_time >= t.exit_time and not status:
            status = f'<-- EXIT ({t.exit_reason})'
        elif prem <= t.sl: status = '<-- BELOW SL'
        elif prem >= t.target: status = '<-- HIT TARGET'

        print(f'  {bar_num:>4} {bar_time.strftime("%H:%M"):<8} {spot:>10.1f} {spot_chg:>+7.3f}% '
              f'{prem:>8.1f} {prem_chg:>+7.1f}% {prem_chg:>+6.1f}% {status}')

# ============================================================================
# 7. GAMMA/THETA DECOMPOSITION — Why does premium not follow spot?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  7. GAMMA vs THETA — Premium decomposition for losers')
print(f'  {"="*100}')

print(f'\n  For each loser: how much of premium change is from delta/gamma vs theta decay?')
print(f'\n  {"Date":<12} {"Sym":<8} {"Dir":<4} {"SpotChg":>8} {"PremChg":>8} {"ExpDelta":>9} {"Theta":>8} {"Gamma":>8} {"Diagnosis":<30}')
print(f'  {"-"*110}')

for t in sorted(losers, key=lambda x: x.pnl)[:20]:
    spot_chg_abs = t.exit_spot - t.entry_spot
    spot_chg_pct = spot_chg_abs / t.entry_spot * 100
    prem_chg_pct = t.pnl_pct
    hold_days = t.hold_minutes / (60 * 24)

    dte = t.dte_days
    strike = t.strike

    # Approximate delta at entry
    moneyness = t.entry_spot / strike
    if t.direction == 'CE':
        d1 = (np.log(moneyness) + (0.05 + 0.5 * (DEFAULT_IV/100)**2) * dte/365) / ((DEFAULT_IV/100) * np.sqrt(dte/365))
        delta = norm.cdf(d1)
    else:
        d1 = (np.log(moneyness) + (0.05 + 0.5 * (DEFAULT_IV/100)**2) * dte/365) / ((DEFAULT_IV/100) * np.sqrt(dte/365))
        delta = norm.cdf(d1) - 1

    # Expected premium change from delta alone
    expected_delta_move = delta * spot_chg_abs
    expected_pct = expected_delta_move / t.entry_premium * 100 if t.entry_premium > 0 else 0

    # Theta decay estimate (per minute)
    if dte > 0:
        theta_per_day = t.entry_premium / max(dte, 0.1) * 0.3  # rough theta
        theta_loss = theta_per_day * hold_days
        theta_pct = -theta_loss / t.entry_premium * 100
    else:
        theta_pct = 0

    # Gamma: what's left after delta + theta
    gamma_pct = prem_chg_pct - expected_pct - theta_pct

    if abs(expected_pct) > abs(prem_chg_pct) and expected_pct * prem_chg_pct > 0:
        diag = 'THETA ATE THE MOVE'
    elif abs(spot_chg_pct) < 0.1:
        diag = 'NO SPOT MOVEMENT'
    elif (t.direction == 'CE' and spot_chg_pct < -0.1) or (t.direction == 'PE' and spot_chg_pct > 0.1):
        diag = 'WRONG DIRECTION'
    elif abs(expected_pct) < 5 and abs(prem_chg_pct) > 15:
        diag = 'GAMMA COLLAPSE (IV crush?)'
    else:
        diag = 'SPOT MOVED AGAINST'

    print(f'  {t.entry_time.strftime("%m-%d %H:%M"):<12} {t.symbol:<8} {t.direction:<4} '
          f'{spot_chg_pct:>+7.3f}% {prem_chg_pct:>+7.1f}% {expected_pct:>+8.1f}% '
          f'{theta_pct:>+7.1f}% {gamma_pct:>+7.1f}% {diag:<30}')

# ============================================================================
# 8. SUMMARY: Root cause breakdown
# ============================================================================
print(f'\n  {"="*100}')
print(f'  8. ROOT CAUSE SUMMARY')
print(f'  {"="*100}')

wrong_dir = 0; wrong_dir_loss = 0
flat_spot = 0; flat_spot_loss = 0
theta_ate = 0; theta_ate_loss = 0
gave_back = 0; gave_back_loss = 0
flip_loss_ct = 0; flip_loss_total = 0

for t in losers:
    spot_chg = (t.exit_spot - t.entry_spot) / t.entry_spot * 100
    pk = t.peak_gain_pct
    is_flip = getattr(t, 'is_flip', False)

    if is_flip:
        flip_loss_ct += 1
        flip_loss_total += t.pnl
    elif pk > 3:
        gave_back += 1
        gave_back_loss += t.pnl
    elif (t.direction == 'CE' and spot_chg < -0.1) or (t.direction == 'PE' and spot_chg > 0.1):
        wrong_dir += 1
        wrong_dir_loss += t.pnl
    elif abs(spot_chg) < 0.1:
        flat_spot += 1
        flat_spot_loss += t.pnl
    else:
        # Spot moved right but premium didn't follow
        theta_ate += 1
        theta_ate_loss += t.pnl

print(f'\n  {"Root Cause":<35} {"Count":>6} {"% of Losers":>12} {"Total Loss":>12} {"Avg Loss":>10}')
print(f'  {"-"*80}')
print(f'  {"WRONG DIRECTION (spot against)":<35} {wrong_dir:>6} {wrong_dir/len(losers)*100:>11.1f}% Rs {wrong_dir_loss:>+10,.0f} Rs {wrong_dir_loss/max(wrong_dir,1):>+8,.0f}')
print(f'  {"FLAT SPOT (no movement)":<35} {flat_spot:>6} {flat_spot/len(losers)*100:>11.1f}% Rs {flat_spot_loss:>+10,.0f} Rs {flat_spot_loss/max(flat_spot,1):>+8,.0f}')
print(f'  {"THETA/GAMMA DECAY":<35} {theta_ate:>6} {theta_ate/len(losers)*100:>11.1f}% Rs {theta_ate_loss:>+10,.0f} Rs {theta_ate_loss/max(theta_ate,1):>+8,.0f}')
print(f'  {"GAVE BACK PROFIT (exit problem)":<35} {gave_back:>6} {gave_back/len(losers)*100:>11.1f}% Rs {gave_back_loss:>+10,.0f} Rs {gave_back_loss/max(gave_back,1):>+8,.0f}')
print(f'  {"FLIP TRADE LOSS":<35} {flip_loss_ct:>6} {flip_loss_ct/len(losers)*100:>11.1f}% Rs {flip_loss_total:>+10,.0f} Rs {flip_loss_total/max(flip_loss_ct,1):>+8,.0f}')

print(f'\n  TOTAL LOSSES: Rs {sum(t.pnl for t in losers):+,.0f} across {len(losers)} trades')

# ============================================================================
# 9. MOMENTUM AT ENTRY — Is the momentum signal actually strong?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  9. MOMENTUM AT ENTRY — 3-bar price momentum at signal time')
print(f'  {"="*100}')

print(f'\n  For each trade: what was the 3-bar momentum when we entered?')
print(f'  (Positive for CE = spot rising, Negative for PE = spot falling)')

for t in trades_raw:
    sym = t.symbol
    df = data[sym]
    mask = df.index.date == t.entry_time.date()
    day_df = df[mask]

    entry_idx = None
    for idx, ts in enumerate(day_df.index):
        if ts >= t.entry_time:
            entry_idx = idx
            break
    if entry_idx is None or entry_idx < 3: continue

    # 3-bar momentum
    mom3 = day_df['Close'].iloc[entry_idx] - day_df['Close'].iloc[entry_idx-3]
    mom_pct = mom3 / day_df['Close'].iloc[entry_idx] * 100

    # Is momentum aligned with direction?
    aligned = (t.direction == 'CE' and mom_pct > 0) or (t.direction == 'PE' and mom_pct < 0)

    t._mom_pct = mom_pct
    t._mom_aligned = aligned

# Summarize
aligned_w = [t for t in trades_raw if hasattr(t, '_mom_aligned') and t._mom_aligned and t.pnl > 0]
aligned_l = [t for t in trades_raw if hasattr(t, '_mom_aligned') and t._mom_aligned and t.pnl <= 0]
counter_w = [t for t in trades_raw if hasattr(t, '_mom_aligned') and not t._mom_aligned and t.pnl > 0]
counter_l = [t for t in trades_raw if hasattr(t, '_mom_aligned') and not t._mom_aligned and t.pnl <= 0]

print(f'\n  {"Momentum":<25} {"Winners":>8} {"Losers":>8} {"WR":>7} {"PF":>7} {"TotalPnL":>12}')
print(f'  {"-"*70}')

aligned_all = aligned_w + aligned_l
if aligned_all:
    aw = len(aligned_w)/(len(aligned_all))*100
    agp = sum(t.pnl for t in aligned_w)
    agl = abs(sum(t.pnl for t in aligned_l)) or 1
    print(f'  {"ALIGNED (mom with dir)":<25} {len(aligned_w):>8} {len(aligned_l):>8} {aw:>6.1f}% {agp/agl:>6.2f} Rs {sum(t.pnl for t in aligned_all):>+10,.0f}')

counter_all = counter_w + counter_l
if counter_all:
    cw = len(counter_w)/(len(counter_all))*100
    cgp = sum(t.pnl for t in counter_w)
    cgl = abs(sum(t.pnl for t in counter_l)) or 1
    print(f'  {"COUNTER (mom against dir)":<25} {len(counter_w):>8} {len(counter_l):>8} {cw:>6.1f}% {cgp/cgl:>6.2f} Rs {sum(t.pnl for t in counter_all):>+10,.0f}')

# Momentum strength
print(f'\n  Momentum strength at entry (absolute %) — Winners vs Losers:')
w_moms = [abs(t._mom_pct) for t in trades_raw if hasattr(t, '_mom_pct') and t.pnl > 0]
l_moms = [abs(t._mom_pct) for t in trades_raw if hasattr(t, '_mom_pct') and t.pnl <= 0]
print(f'  Winners avg momentum: {np.mean(w_moms):.4f}%')
print(f'  Losers avg momentum:  {np.mean(l_moms):.4f}%')
print(f'  Winners median:       {np.median(w_moms):.4f}%')
print(f'  Losers median:        {np.median(l_moms):.4f}%')

# ============================================================================
# 10. PREMIUM FLATNESS — Does premium go flat after entry?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  10. PREMIUM FLATNESS — First 3 bars after entry')
print(f'  {"="*100}')

print(f'\n  How does premium behave in the first 15 minutes after entry?')

for t in sorted(losers, key=lambda x: x.pnl)[:15]:
    readings = t.premium_readings
    if len(readings) < 4: continue

    entry_p = t.entry_premium
    moves = []
    for j in range(min(4, len(readings))):
        bar_p = readings[j][1]
        chg = (bar_p - entry_p) / entry_p * 100
        moves.append(f'{chg:+.1f}%')

    print(f'  {t.entry_time.strftime("%m-%d %H:%M")} {t.symbol:<8} {t.direction} '
          f'Entry={entry_p:.1f} First 4 bars: {" -> ".join(moves)} '
          f'Final={t.pnl_pct:+.1f}% Exit={t.exit_reason} PnL=Rs {t.pnl:+,.0f}')
