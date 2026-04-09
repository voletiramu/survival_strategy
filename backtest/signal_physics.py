"""
SIGNAL PHYSICS — Mathematical analysis of WHY trades fail
==========================================================
Instead of blind time filters, compute the actual physics at entry:
  1. Momentum (dp/dt) — 1st derivative of price
  2. Acceleration (d²p/dt²) — 2nd derivative: is momentum growing or dying?
  3. Wave position — Are we at a wave peak (about to reverse)?
  4. Mean reversion pressure — How far from VWAP/EMA? Rubber band effect
  5. Realized vs Implied volatility — Is premium overpriced (will decay)?
  6. Volume-price divergence — Volume confirming or denying the move?
  7. Fourier phase — Where in the dominant cycle are we?
"""
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
from scipy.signal import hilbert
from scipy.fft import fft, fftfreq

data = load_cached_data(['NIFTY','BANKNIFTY','SENSEX'], 90)

# Suppress V10 output
old_stdout = sys.stdout
sys.stdout = io.StringIO()
trades_raw, _, _ = v10.run_full_v10(data, ['NIFTY','BANKNIFTY','SENSEX'])
sys.stdout = old_stdout

trades_raw.sort(key=lambda t: t.entry_time)

print('='*120)
print('  SIGNAL PHYSICS — Mathematical Analysis at Entry Point')
print('='*120)

# ============================================================================
# COMPUTE PHYSICS SIGNALS AT ENTRY FOR EACH TRADE
# ============================================================================

def compute_entry_physics(trade, data_dict):
    """Compute ALL mathematical signals at the exact entry bar."""
    sym = trade.symbol
    df = data_dict[sym]

    # Get day data up to entry
    mask = df.index.date == trade.entry_time.date()
    day_df = df[mask]

    # Find entry bar index
    entry_idx = None
    for idx, ts in enumerate(day_df.index):
        if ts >= trade.entry_time:
            entry_idx = idx
            break
    if entry_idx is None or entry_idx < 5:
        return None

    prices = day_df['Close'].values[:entry_idx+1]
    volumes = day_df['Volume'].values[:entry_idx+1] if 'Volume' in day_df.columns else None
    spot = prices[-1]

    signals = {}
    signals['entry_idx'] = entry_idx
    signals['bars_available'] = len(prices)

    # ────────────────────────────────────────────────
    # 1. MOMENTUM (dp/dt) — 1st derivative of price
    # ────────────────────────────────────────────────
    # 3-bar momentum (current method)
    if len(prices) >= 4:
        mom3 = (prices[-1] - prices[-4]) / prices[-4] * 100
        signals['momentum_3bar'] = mom3
    else:
        signals['momentum_3bar'] = 0

    # 5-bar momentum (longer view)
    if len(prices) >= 6:
        mom5 = (prices[-1] - prices[-6]) / prices[-6] * 100
        signals['momentum_5bar'] = mom5
    else:
        signals['momentum_5bar'] = 0

    # 1-bar momentum (instantaneous)
    if len(prices) >= 2:
        mom1 = (prices[-1] - prices[-2]) / prices[-2] * 100
        signals['momentum_1bar'] = mom1
    else:
        signals['momentum_1bar'] = 0

    # ────────────────────────────────────────────────
    # 2. ACCELERATION (d²p/dt²) — Is momentum growing or dying?
    # ────────────────────────────────────────────────
    if len(prices) >= 5:
        # Compute velocity at consecutive points
        v1 = prices[-3] - prices[-5]  # velocity 2 bars ago
        v2 = prices[-1] - prices[-3]  # velocity now
        accel = (v2 - v1) / spot * 10000  # normalized acceleration
        signals['acceleration'] = accel

        # Is momentum DECELERATING? (positive mom but negative accel = about to reverse)
        if trade.direction == 'CE':
            signals['mom_dying'] = (signals['momentum_3bar'] > 0 and accel < 0)
        else:
            signals['mom_dying'] = (signals['momentum_3bar'] < 0 and accel > 0)
    else:
        signals['acceleration'] = 0
        signals['mom_dying'] = False

    # ────────────────────────────────────────────────
    # 3. JERK (d³p/dt³) — Rate of change of acceleration
    # ────────────────────────────────────────────────
    if len(prices) >= 7:
        v1 = prices[-5] - prices[-7]
        v2 = prices[-3] - prices[-5]
        v3 = prices[-1] - prices[-3]
        a1 = v2 - v1
        a2 = v3 - v2
        jerk = (a2 - a1) / spot * 10000
        signals['jerk'] = jerk
    else:
        signals['jerk'] = 0

    # ────────────────────────────────────────────────
    # 4. WAVE POSITION — Are we at a local peak/trough?
    # ────────────────────────────────────────────────
    # Use recent price oscillation to detect wave phase
    if len(prices) >= 10:
        recent = prices[-10:]
        local_max = max(recent)
        local_min = min(recent)
        wave_range = local_max - local_min

        if wave_range > 0:
            # Position in wave: 0 = at bottom, 1 = at top
            wave_pos = (spot - local_min) / wave_range
            signals['wave_position'] = wave_pos

            # For CE: entering near wave top (>0.8) = risky (about to mean-revert)
            # For PE: entering near wave bottom (<0.2) = risky
            if trade.direction == 'CE':
                signals['wave_risk'] = wave_pos  # Higher = riskier for CE
            else:
                signals['wave_risk'] = 1 - wave_pos  # Lower position = riskier for PE (already extended down)
        else:
            signals['wave_position'] = 0.5
            signals['wave_risk'] = 0.5
    else:
        signals['wave_position'] = 0.5
        signals['wave_risk'] = 0.5

    # ────────────────────────────────────────────────
    # 5. HILBERT TRANSFORM — Instantaneous phase of price wave
    # ────────────────────────────────────────────────
    if len(prices) >= 20:
        # Detrend prices
        detrended = prices - np.linspace(prices[0], prices[-1], len(prices))
        try:
            analytic = hilbert(detrended)
            inst_phase = np.angle(analytic)
            # Phase at entry: near π/2 = peak, near -π/2 = trough
            signals['hilbert_phase'] = inst_phase[-1]

            # Instantaneous frequency (rate of phase change)
            if len(inst_phase) >= 2:
                signals['hilbert_freq'] = abs(inst_phase[-1] - inst_phase[-2])
            else:
                signals['hilbert_freq'] = 0
        except:
            signals['hilbert_phase'] = 0
            signals['hilbert_freq'] = 0
    else:
        signals['hilbert_phase'] = 0
        signals['hilbert_freq'] = 0

    # ────────────────────────────────────────────────
    # 6. FOURIER — Dominant cycle frequency and phase
    # ────────────────────────────────────────────────
    if len(prices) >= 20:
        detrended = prices - np.linspace(prices[0], prices[-1], len(prices))
        N = len(detrended)
        yf = fft(detrended)
        xf = fftfreq(N, 5)  # 5-min bars

        # Find dominant frequency (exclude DC component)
        magnitudes = np.abs(yf[1:N//2])
        if len(magnitudes) > 0:
            dom_idx = np.argmax(magnitudes) + 1
            signals['dom_frequency'] = abs(xf[dom_idx])
            signals['dom_period_bars'] = 1 / abs(xf[dom_idx]) / 5 if abs(xf[dom_idx]) > 0 else 999
            signals['dom_phase'] = np.angle(yf[dom_idx])
            signals['dom_power'] = magnitudes[dom_idx-1] / np.sum(magnitudes) if np.sum(magnitudes) > 0 else 0
        else:
            signals['dom_frequency'] = 0
            signals['dom_period_bars'] = 999
            signals['dom_phase'] = 0
            signals['dom_power'] = 0
    else:
        signals['dom_frequency'] = 0
        signals['dom_period_bars'] = 999
        signals['dom_phase'] = 0
        signals['dom_power'] = 0

    # ────────────────────────────────────────────────
    # 7. MEAN REVERSION PRESSURE — Distance from VWAP/EMA
    # ────────────────────────────────────────────────
    # VWAP distance
    if volumes is not None and np.sum(volumes) > 0:
        vwap = np.cumsum(prices * volumes) / np.cumsum(volumes)
        vwap_dist = (spot - vwap[-1]) / vwap[-1] * 100
    else:
        vwap = np.cumsum(prices) / np.arange(1, len(prices)+1)
        vwap_dist = (spot - vwap[-1]) / vwap[-1] * 100
    signals['vwap_distance'] = vwap_dist

    # For CE: positive vwap_dist means ABOVE vwap (extended, mean-reversion risk)
    # For PE: negative vwap_dist means BELOW vwap (extended, mean-reversion risk)
    if trade.direction == 'CE':
        signals['mean_revert_pressure'] = vwap_dist  # Higher = more rubber band tension
    else:
        signals['mean_revert_pressure'] = -vwap_dist  # More negative = more rubber band

    # EMA(10) distance
    if len(prices) >= 10:
        ema = prices[-1]  # Start with current
        alpha = 2 / 11
        for p in reversed(prices[-10:]):
            ema = alpha * p + (1-alpha) * ema
        ema_dist = (spot - ema) / ema * 100
        signals['ema10_distance'] = ema_dist
    else:
        signals['ema10_distance'] = 0

    # Bollinger Band position
    if len(prices) >= 20:
        sma20 = np.mean(prices[-20:])
        std20 = np.std(prices[-20:])
        if std20 > 0:
            bb_pos = (spot - sma20) / (2 * std20)  # -1 to +1 = within bands
            signals['bollinger_position'] = bb_pos
        else:
            signals['bollinger_position'] = 0
    else:
        signals['bollinger_position'] = 0

    # ────────────────────────────────────────────────
    # 8. REALIZED vs IMPLIED VOLATILITY
    # ────────────────────────────────────────────────
    if len(prices) >= 10:
        returns = np.diff(np.log(prices[-10:]))
        realized_vol = np.std(returns) * np.sqrt(252 * 75) * 100  # annualized from 5-min
        signals['realized_vol'] = realized_vol
        signals['rv_iv_ratio'] = realized_vol / DEFAULT_IV if DEFAULT_IV > 0 else 1
    else:
        signals['realized_vol'] = DEFAULT_IV
        signals['rv_iv_ratio'] = 1

    # ────────────────────────────────────────────────
    # 9. VOLUME-PRICE DIVERGENCE
    # ────────────────────────────────────────────────
    if volumes is not None and len(volumes) >= 5:
        # Volume trend
        vol_trend = (np.mean(volumes[-3:]) - np.mean(volumes[-6:-3])) / max(np.mean(volumes[-6:-3]), 1)
        signals['volume_trend'] = vol_trend

        # Price moving with declining volume = weak move
        price_up = prices[-1] > prices[-3]
        vol_declining = vol_trend < -0.1
        signals['vol_price_divergence'] = price_up and vol_declining

        # On-Balance Volume slope
        n_obv = min(10, len(prices), len(volumes))
        obv = np.cumsum(np.where(np.diff(prices[-n_obv:]) > 0, volumes[-(n_obv-1):], -volumes[-(n_obv-1):]))
        if len(obv) >= 3:
            obv_slope = (obv[-1] - obv[-3]) / max(abs(obv[-3]), 1)
            signals['obv_slope'] = obv_slope
        else:
            signals['obv_slope'] = 0
    else:
        signals['volume_trend'] = 0
        signals['vol_price_divergence'] = False
        signals['obv_slope'] = 0

    # ────────────────────────────────────────────────
    # 10. ENERGY (kinetic + potential from physics analogy)
    # ────────────────────────────────────────────────
    # Kinetic energy ∝ velocity²
    kinetic = signals['momentum_3bar'] ** 2
    signals['kinetic_energy'] = kinetic

    # Potential energy ∝ distance from equilibrium (VWAP)
    potential = signals['vwap_distance'] ** 2
    signals['potential_energy'] = potential

    # Total energy — higher energy = bigger expected move
    signals['total_energy'] = kinetic + potential

    # Energy direction: is energy building (entering trade) or dissipating (leaving trade)?
    if len(prices) >= 8:
        prev_mom = (prices[-4] - prices[-7]) / prices[-7] * 100
        prev_kinetic = prev_mom ** 2
        signals['energy_building'] = kinetic > prev_kinetic
    else:
        signals['energy_building'] = True

    # ────────────────────────────────────────────────
    # 11. RSI at entry (momentum oscillator)
    # ────────────────────────────────────────────────
    if len(prices) >= 15:
        deltas = np.diff(prices[-15:])
        gains = deltas[deltas > 0]
        losses = -deltas[deltas < 0]
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        signals['rsi'] = rsi
    else:
        signals['rsi'] = 50

    return signals


print(f'\n  Computing physics signals for {len(trades_raw)} trades...')

trade_signals = []
for t in trades_raw:
    sig = compute_entry_physics(t, data)
    if sig is not None:
        trade_signals.append((t, sig))

winners = [(t, s) for t, s in trade_signals if t.pnl > 0]
losers = [(t, s) for t, s in trade_signals if t.pnl <= 0]

print(f'  Computed: {len(trade_signals)} trades ({len(winners)} W, {len(losers)} L)')

# ============================================================================
# ANALYZE EACH SIGNAL — Does it predict winners vs losers?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  SIGNAL DISCRIMINATING POWER — Which physics predict outcome?')
print(f'  {"="*100}')

signal_names = [
    ('momentum_1bar', 'dp/dt (1-bar instant)', 'higher=stronger'),
    ('momentum_3bar', 'dp/dt (3-bar trend)', 'higher=stronger'),
    ('momentum_5bar', 'dp/dt (5-bar trend)', 'higher=stronger'),
    ('acceleration', 'd²p/dt² (acceleration)', 'pos=accelerating'),
    ('jerk', 'd³p/dt³ (jerk)', 'rate of accel change'),
    ('wave_position', 'Wave position (0=bottom 1=top)', ''),
    ('wave_risk', 'Wave risk (higher=riskier)', ''),
    ('hilbert_phase', 'Hilbert phase (wave position)', ''),
    ('hilbert_freq', 'Hilbert freq (cycle speed)', ''),
    ('dom_period_bars', 'Dominant cycle period (bars)', ''),
    ('dom_power', 'Dominant cycle power', ''),
    ('vwap_distance', 'VWAP distance %', ''),
    ('mean_revert_pressure', 'Mean reversion pressure', 'higher=more stretched'),
    ('ema10_distance', 'EMA(10) distance %', ''),
    ('bollinger_position', 'Bollinger Band position', '-1 to +1'),
    ('realized_vol', 'Realized volatility', ''),
    ('rv_iv_ratio', 'RV/IV ratio', '<1=overpriced premium'),
    ('volume_trend', 'Volume trend', 'neg=declining'),
    ('obv_slope', 'OBV slope', ''),
    ('kinetic_energy', 'Kinetic energy (v²)', ''),
    ('potential_energy', 'Potential energy (VWAP²)', ''),
    ('total_energy', 'Total energy', ''),
    ('rsi', 'RSI(14)', '>70=overbought'),
]

print(f'\n  {"Signal":<35} {"W avg":>10} {"L avg":>10} {"Ratio":>8} {"Separable?":>12}')
print(f'  {"-"*80}')

best_signals = []

for key, name, desc in signal_names:
    w_vals = [s[key] for _, s in winners if isinstance(s[key], (int, float)) and not isinstance(s[key], bool)]
    l_vals = [s[key] for _, s in losers if isinstance(s[key], (int, float)) and not isinstance(s[key], bool)]

    if not w_vals or not l_vals:
        continue

    w_mean = np.mean(w_vals)
    l_mean = np.mean(l_vals)

    # Compute separation: how different are the distributions?
    combined_std = np.std(w_vals + l_vals)
    if combined_std > 0:
        separation = abs(w_mean - l_mean) / combined_std
    else:
        separation = 0

    # Ratio
    ratio = w_mean / l_mean if l_mean != 0 else 999

    mark = ''
    if separation > 0.3: mark = '*** USEFUL'
    elif separation > 0.2: mark = '** MODERATE'
    elif separation > 0.1: mark = '* WEAK'

    if separation > 0.15:
        best_signals.append((key, name, separation, w_mean, l_mean))

    print(f'  {name:<35} {w_mean:>10.4f} {l_mean:>10.4f} {ratio:>7.2f}x {mark:>12}')

# ============================================================================
# BOOLEAN SIGNALS
# ============================================================================
print(f'\n  {"="*100}')
print(f'  BOOLEAN SIGNAL ANALYSIS')
print(f'  {"="*100}')

bool_signals = ['mom_dying', 'vol_price_divergence', 'energy_building']

print(f'\n  {"Signal":<30} {"W True":>8} {"W False":>8} {"L True":>8} {"L False":>8} {"True WR":>8} {"False WR":>8} {"Useful?":>10}')
print(f'  {"-"*90}')

for key in bool_signals:
    w_true = sum(1 for _, s in winners if s.get(key, False))
    w_false = len(winners) - w_true
    l_true = sum(1 for _, s in losers if s.get(key, False))
    l_false = len(losers) - l_true

    true_wr = w_true / (w_true + l_true) * 100 if (w_true + l_true) > 0 else 0
    false_wr = w_false / (w_false + l_false) * 100 if (w_false + l_false) > 0 else 0

    useful = 'YES' if abs(true_wr - false_wr) > 10 else 'no'

    print(f'  {key:<30} {w_true:>8} {w_false:>8} {l_true:>8} {l_false:>8} {true_wr:>7.1f}% {false_wr:>7.1f}% {useful:>10}')

# ============================================================================
# TOP DISCRIMINATING SIGNALS — Detailed breakdown
# ============================================================================
print(f'\n  {"="*100}')
print(f'  TOP DISCRIMINATING SIGNALS — Detailed Distribution')
print(f'  {"="*100}')

for key, name, sep, w_mean, l_mean in sorted(best_signals, key=lambda x: -x[2]):
    w_vals = sorted([s[key] for _, s in winners if isinstance(s[key], (int, float)) and not isinstance(s[key], bool)])
    l_vals = sorted([s[key] for _, s in losers if isinstance(s[key], (int, float)) and not isinstance(s[key], bool)])

    print(f'\n  {name} (separation={sep:.3f}):')
    print(f'    Winners: min={min(w_vals):.4f} Q1={np.percentile(w_vals,25):.4f} '
          f'med={np.median(w_vals):.4f} Q3={np.percentile(w_vals,75):.4f} max={max(w_vals):.4f}')
    print(f'    Losers:  min={min(l_vals):.4f} Q1={np.percentile(l_vals,25):.4f} '
          f'med={np.median(l_vals):.4f} Q3={np.percentile(l_vals,75):.4f} max={max(l_vals):.4f}')

    # Find threshold that best separates
    all_vals = w_vals + l_vals
    best_thresh = None
    best_accuracy = 0

    for pct in range(10, 91, 5):
        thresh = np.percentile(all_vals, pct)

        # Try both directions
        for direction in ['above_win', 'below_win']:
            if direction == 'above_win':
                correct = sum(1 for v in w_vals if v >= thresh) + sum(1 for v in l_vals if v < thresh)
            else:
                correct = sum(1 for v in w_vals if v < thresh) + sum(1 for v in l_vals if v >= thresh)

            accuracy = correct / len(all_vals) * 100
            if accuracy > best_accuracy:
                best_accuracy = accuracy
                best_thresh = thresh
                best_dir = direction

    if best_thresh is not None:
        # Calculate WR above and below threshold
        w_above = sum(1 for v in w_vals if v >= best_thresh)
        l_above = sum(1 for v in l_vals if v >= best_thresh)
        w_below = sum(1 for v in w_vals if v < best_thresh)
        l_below = sum(1 for v in l_vals if v < best_thresh)

        wr_above = w_above / (w_above + l_above) * 100 if (w_above + l_above) > 0 else 0
        wr_below = w_below / (w_below + l_below) * 100 if (w_below + l_below) > 0 else 0

        # PnL split
        pnl_above = sum(t.pnl for t, s in trade_signals if isinstance(s[key], (int, float)) and not isinstance(s[key], bool) and s[key] >= best_thresh)
        pnl_below = sum(t.pnl for t, s in trade_signals if isinstance(s[key], (int, float)) and not isinstance(s[key], bool) and s[key] < best_thresh)
        n_above = sum(1 for t, s in trade_signals if isinstance(s[key], (int, float)) and not isinstance(s[key], bool) and s[key] >= best_thresh)
        n_below = sum(1 for t, s in trade_signals if isinstance(s[key], (int, float)) and not isinstance(s[key], bool) and s[key] < best_thresh)

        print(f'    Best threshold: {best_thresh:.4f} (accuracy={best_accuracy:.1f}%)')
        print(f'    Above threshold: {n_above} trades, WR={wr_above:.1f}%, PnL Rs {pnl_above:+,.0f}')
        print(f'    Below threshold: {n_below} trades, WR={wr_below:.1f}%, PnL Rs {pnl_below:+,.0f}')

# ============================================================================
# COMBINED SIGNAL — Multi-factor physics score
# ============================================================================
print(f'\n  {"="*100}')
print(f'  COMBINED PHYSICS SCORE — Multi-factor signal quality')
print(f'  {"="*100}')

def compute_physics_score(signals, direction):
    """Combine multiple physics signals into a single quality score."""
    score = 0

    # 1. Acceleration aligned with direction (d²p/dt² confirms momentum)
    accel = signals['acceleration']
    if direction == 'CE' and accel > 0:
        score += min(25, abs(accel) * 5)  # Accelerating upward
    elif direction == 'PE' and accel < 0:
        score += min(25, abs(accel) * 5)  # Accelerating downward
    elif signals.get('mom_dying', False):
        score -= 20  # Momentum is DYING — dangerous

    # 2. Wave position risk
    wave_risk = signals['wave_risk']
    if wave_risk < 0.5:
        score += 15  # Not at extreme — room to move
    elif wave_risk > 0.8:
        score -= 15  # At wave extreme — about to reverse

    # 3. Mean reversion pressure (rubber band effect)
    mrp = abs(signals['mean_revert_pressure'])
    if mrp < 0.1:
        score += 10  # Near equilibrium — can move freely
    elif mrp > 0.3:
        score -= 15  # Stretched — rubber band will snap back

    # 4. Energy building
    if signals.get('energy_building', False):
        score += 10
    else:
        score -= 5

    # 5. Volume confirmation
    if not signals.get('vol_price_divergence', False):
        score += 5  # Volume confirms
    else:
        score -= 10  # Volume divergence — move not confirmed

    # 6. RV/IV ratio
    rv_iv = signals.get('rv_iv_ratio', 1)
    if rv_iv > 1.2:
        score += 10  # Premium underpriced relative to actual vol
    elif rv_iv < 0.6:
        score -= 15  # Premium overpriced — will decay

    # 7. RSI extreme check
    rsi = signals.get('rsi', 50)
    if direction == 'CE' and rsi > 75:
        score -= 15  # Overbought for CE
    elif direction == 'PE' and rsi < 25:
        score -= 15  # Oversold for PE
    elif direction == 'CE' and 40 < rsi < 65:
        score += 5  # Sweet spot
    elif direction == 'PE' and 35 < rsi < 60:
        score += 5  # Sweet spot

    # 8. Bollinger position
    bb = signals.get('bollinger_position', 0)
    if direction == 'CE' and bb > 1.5:
        score -= 10  # Above upper band — extended
    elif direction == 'PE' and bb < -1.5:
        score -= 10  # Below lower band — extended

    return score

# Compute physics score for each trade
for t, s in trade_signals:
    s['physics_score'] = compute_physics_score(s, t.direction)

# Analyze by physics score bins
print(f'\n  {"Physics Score":<20} {"Trades":>7} {"WR":>7} {"PF":>7} {"AvgPnL":>10} {"TotalPnL":>12} {"AvgPkGain":>10}')
print(f'  {"-"*80}')

score_bins = [(-999, -15, 'Score < -15'), (-15, 0, 'Score -15 to 0'), (0, 15, 'Score 0 to 15'),
              (15, 30, 'Score 15 to 30'), (30, 999, 'Score 30+')]

for lo, hi, label in score_bins:
    sub = [(t, s) for t, s in trade_signals if lo <= s['physics_score'] < hi]
    if not sub: continue

    trades_sub = [t for t, s in sub]
    wr = sum(1 for t in trades_sub if t.pnl > 0) / len(trades_sub) * 100
    gp = sum(t.pnl for t in trades_sub if t.pnl > 0)
    gl = abs(sum(t.pnl for t in trades_sub if t.pnl <= 0))
    pf = gp/gl if gl > 0 else 999
    avg = np.mean([t.pnl for t in trades_sub])
    tot = sum(t.pnl for t in trades_sub)
    avg_pk = np.mean([t.peak_gain_pct for t in trades_sub])

    print(f'  {label:<20} {len(sub):>7} {wr:>6.1f}% {pf:>6.2f} Rs {avg:>+8,.0f} Rs {tot:>+10,.0f} {avg_pk:>+9.1f}%')

# ============================================================================
# WORST TRADES — What did physics say?
# ============================================================================
print(f'\n  {"="*100}')
print(f'  WORST 15 LOSERS — What physics said at entry')
print(f'  {"="*100}')

worst = sorted([(t, s) for t, s in trade_signals if t.pnl <= 0], key=lambda x: x[0].pnl)[:15]

print(f'\n  {"Date":<12} {"Sym":<8} {"Dir":<4} {"PnL":>9} {"PhySc":>6} {"Accel":>7} {"WaveRisk":>9} '
      f'{"MeanRev":>8} {"RV/IV":>6} {"RSI":>5} {"MomDy":>6} {"Diagnosis":<25}')
print(f'  {"-"*115}')

for t, s in worst:
    diag = []
    if s.get('mom_dying', False): diag.append('MOM_DYING')
    if s['wave_risk'] > 0.8: diag.append('WAVE_PEAK')
    if abs(s['mean_revert_pressure']) > 0.3: diag.append('STRETCHED')
    if s['rv_iv_ratio'] < 0.6: diag.append('PREM_OVERPRICED')
    if s.get('vol_price_divergence', False): diag.append('VOL_DIVG')
    if (t.direction == 'CE' and s['rsi'] > 75) or (t.direction == 'PE' and s['rsi'] < 25):
        diag.append('RSI_EXTREME')
    if not diag: diag.append('NO_WARNING')

    print(f'  {t.entry_time.strftime("%m-%d %H:%M"):<12} {t.symbol:<8} {t.direction:<4} '
          f'Rs {t.pnl:>+7,.0f} {s["physics_score"]:>+5.0f} {s["acceleration"]:>+6.2f} '
          f'{s["wave_risk"]:>8.3f} {s["mean_revert_pressure"]:>+7.3f} {s["rv_iv_ratio"]:>5.2f} '
          f'{s["rsi"]:>5.1f} {"Y" if s.get("mom_dying") else "N":>5} {",".join(diag):<25}')

# ============================================================================
# BEST TRADES — What physics said at entry
# ============================================================================
print(f'\n  {"="*100}')
print(f'  BEST 15 WINNERS — What physics said at entry')
print(f'  {"="*100}')

best = sorted([(t, s) for t, s in trade_signals if t.pnl > 0], key=lambda x: -x[0].pnl)[:15]

print(f'\n  {"Date":<12} {"Sym":<8} {"Dir":<4} {"PnL":>9} {"PhySc":>6} {"Accel":>7} {"WaveRisk":>9} '
      f'{"MeanRev":>8} {"RV/IV":>6} {"RSI":>5} {"MomDy":>6}')
print(f'  {"-"*90}')

for t, s in best:
    print(f'  {t.entry_time.strftime("%m-%d %H:%M"):<12} {t.symbol:<8} {t.direction:<4} '
          f'Rs {t.pnl:>+7,.0f} {s["physics_score"]:>+5.0f} {s["acceleration"]:>+6.2f} '
          f'{s["wave_risk"]:>8.3f} {s["mean_revert_pressure"]:>+7.3f} {s["rv_iv_ratio"]:>5.2f} '
          f'{s["rsi"]:>5.1f} {"Y" if s.get("mom_dying") else "N":>5}')

# ============================================================================
# FILTER SIMULATION — Block trades with negative physics
# ============================================================================
print(f'\n  {"="*100}')
print(f'  FILTER SIMULATION — What if we blocked low physics scores?')
print(f'  {"="*100}')

for threshold in [-20, -15, -10, -5, 0, 5, 10, 15]:
    passed = [(t, s) for t, s in trade_signals if s['physics_score'] >= threshold]
    blocked = [(t, s) for t, s in trade_signals if s['physics_score'] < threshold]

    if not passed: continue

    p_trades = [t for t, s in passed]
    wr = sum(1 for t in p_trades if t.pnl > 0) / len(p_trades) * 100
    gp = sum(t.pnl for t in p_trades if t.pnl > 0)
    gl = abs(sum(t.pnl for t in p_trades if t.pnl <= 0))
    pf = gp/gl if gl > 0 else 999
    tot = sum(t.pnl for t in p_trades)

    eq = np.cumsum([t.pnl for t in p_trades])
    dd = (np.maximum.accumulate(eq) - eq).max()

    blocked_pnl = sum(t.pnl for t, s in blocked)

    print(f'  Physics >= {threshold:>+3}: {len(passed):>3} trades, WR={wr:.1f}%, PF={pf:.2f}, '
          f'PnL Rs {tot:+,.0f}, DD Rs {dd:,.0f} | Blocked: {len(blocked)} trades (PnL Rs {blocked_pnl:+,.0f})')
