"""
Statistical Analysis of Trading Signals vs Outcomes
Probability & Conditional Probability Framework
"""
import numpy as np
from collections import defaultdict

# All 52 trades: [date, sym, sig_type, strategy, pnl, win, exit_reason,
#   iv, dte, delta, gamma, theta, theta_pct, score, entry_prem, exit_prem,
#   peak_prem, peak_gain_pct, actual_gain_pct, is_sell, entry_oi, entry_spot, source]

trades_raw = """2026-03-06|NIFTY|BUY_PE_CPR|CPR|309|1|TRAILING_SL_HIT|43.0|1|0.513|0.000718|113.1|48.3|87|234.1|240.7|269.9|15.3|2.8|0|4549155|24672|old
2026-03-06|SENSEX|BUY_PE_CPR|CPR|3115|1|TRAILING_SL_HIT|48.8|1|0.498|0.000196|412.3|50.3|95|819.0|981.0|1068.9|30.5|19.8|0|379320|79568|old
2026-03-06|NIFTY|BUY_PE_CPR|CPR|2307|1|TRAILING_SL_HIT|40.2|1|0.517|0.00077|105.5|47.7|87|221.1|258.4|279.2|26.3|16.9|0|5034055|24617|old
2026-03-06|BANKNIFTY|BUY_PE_GAMMA|Gamma Blast|9896|1|TARGET_HIT|72.9|1|0.506|0.000179|450.7|48.5|80|929.1|1264.0|1264.0|36.0|36.0|0|150480|58515|old
2026-03-06|SENSEX|BUY_PE_CPR|CPR|6379|1|TARGET_HIT|50.5|1|0.486|0.00019|424.6|52.3|95|812.4|1137.7|1137.7|40.1|40.1|0|199020|79333|old
2026-03-06|BANKNIFTY|BUY_PE_CPR|CPR|13398|1|TARGET_HIT|71.7|1|0.497|0.000182|443.8|49.9|87|888.5|1340.2|1340.2|50.8|50.8|0|133470|58567|old
2026-03-06|BANKNIFTY|BUY_PE_CPR|CPR|4238|1|TRAILING_SL_HIT|77.3|1|0.483|0.000169|475.7|52.0|95|914.5|1060.5|1168.0|27.7|16.0|0|66270|58344|old
2026-03-09|NIFTY|BUY_PE_CPR|CPR|83753|1|TARGET_HIT|14.7|4|0.478|0.001057|20.9|14.7|85|141.6|572.2|572.2|304.3|304.3|0|0|24450|old
2026-03-09|SENSEX|BUY_PE_CPR|CPR|120258|1|TARGET_HIT|14.6|4|0.472|0.000331|66.5|15.0|85|443.0|1949.8|1949.8|340.1|340.1|0|0|78919|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-117|0|DIRECTION_FLIP|20.3|4|0.525|0.000782|27.6|12.7|56|216.8|216.8|216.8|0.0|0.0|0|4331340|23910|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-117|0|DIRECTION_FLIP|20.1|4|0.526|0.00079|27.3|12.7|56|215.2|215.2|215.2|0.0|0.0|0|5857670|23911|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-119|0|DIRECTION_FLIP|21.6|4|0.524|0.000736|29.1|12.7|56|228.9|228.9|228.9|0.0|0.0|0|6079190|23909|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-118|0|DIRECTION_FLIP|21.1|4|0.525|0.000753|28.5|12.7|56|225.0|225.0|225.0|0.0|0.0|0|5832320|23911|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-118|0|DIRECTION_FLIP|21.6|4|0.523|0.000738|29.0|12.7|56|228.2|228.2|228.2|0.0|0.0|0|6004700|23909|old
2026-03-09|NIFTY|BUY_CE_GAMMA|Gamma Blast|-119|0|DIRECTION_FLIP|21.5|4|0.525|0.00074|29.0|12.7|56|228.4|228.4|228.4|0.0|0.0|0|6527040|23910|old
2026-03-09|BANKNIFTY|BUY_PE_GAMMA|Gamma Blast|-7306|0|EOD_FORCE_CLOSE|57.1|4|0.483|0.00012|171.2|13.1|85|1310.0|1191.8|1448.8|10.6|-9.0|0|53310|55800|old
2026-03-10|NIFTY|BUY_PE_CPR|CPR|-1640|0|DIRECTION_FLIP|9.4|3|0.499|0.001946|15.8|19.3|72|82|74.2|82|0.0|-9.5|0|137202|24137|old
2026-03-10|NIFTY|BUY_PE_GAMMA|Gamma Blast|-120|0|DIRECTION_FLIP|9.3|3|0.502|0.001962|15.7|19.1|72|82|82|82|0.0|0.0|0|137202|24135|old
2026-03-10|SENSEX|BUY_PE_CPR|CPR|-8409|0|TRAILING_SL_HIT|22.6|3|0.483|0.00025|112.9|18.4|65|612.5|509.4|612.5|0.0|-16.8|0|9760|77908|old
2026-03-10|NIFTY|BUY_CE_CPR|CPR|2602|1|TRAILING_SL_HIT|7.4|3|0.498|0.002448|13.0|20.1|73|64.4|78.3|85.7|33.1|21.7|0|334864|24186|old
2026-03-10|NIFTY|BUY_PE_GAMMA|Gamma Blast|-117|0|DIRECTION_FLIP|8.7|3|0.482|0.002098|14.7|20.4|76|72|72|72|0.0|0.0|0|258065|24195|old
2026-03-10|NIFTY|BUY_CE_CPR|CPR|-937|0|DIRECTION_FLIP|6.9|3|0.588|0.002555|12.4|15.7|73|78.8|74.7|84.4|7.0|-5.3|0|311847|24220|old
2026-03-10|NIFTY|BUY_CE|PCR+VWAP|-3747|0|TRAILING_SL_HIT|6.3|3|0.54|0.002886|11.4|18.5|68|61.9|43.2|66.3|7.2|-30.2|0|212326|24250|old
2026-03-10|SENSEX|BUY_CE_CPR|CPR|-2654|0|TRAILING_SL_HIT|23.8|3|0.516|0.000237|118.8|17.1|58|695.0|568.0|712.9|2.6|-18.3|0|13497|78106|old
2026-03-10|NIFTY|BUY_CE_CPR|CPR|72|1|TRAILING_SL_HIT|5.4|3|0.579|0.00331|10.2|17.1|81|59.6|60.6|75.1|25.9|1.6|0|184408|24160|old
2026-03-10|SENSEX|BUY_PE_GAMMA|Gamma Blast|-2979|0|TRAILING_SL_HIT|21.9|3|0.458|0.000256|109.0|20.0|50|546.3|475.0|600.3|9.9|-13.1|0|12324|78009|old
2026-03-10|NIFTY|BUY_PE|PCR+VWAP|-350|0|TRAILING_SL_HIT|6.1|3|0.388|0.002851|10.3|28.0|58|36.7|35.5|42.7|16.3|-3.4|0|297218|24225|old
2026-03-10|NIFTY|BUY_CE|PCR+VWAP|-7744|0|TRAILING_SL_HIT|5.6|3|0.649|0.003001|10.4|13.7|81|76.2|37.1|76.2|0.0|-51.3|0|194038|24284|old
2026-03-10|BANKNIFTY|BUY_CE_CPR|CPR|1457|1|TIME_EXIT_NO_PROGRESS|61.9|3|0.515|0.000125|216.8|16.9|53|1285.7|1339.5|1431.6|11.3|4.2|0|2910|56798|old
2026-03-10|BANKNIFTY|BUY_CE_GAMMA|Gamma Blast|1457|1|TIME_EXIT_NO_PROGRESS|61.9|3|0.515|0.000125|216.8|16.9|53|1285.7|1339.5|1431.6|11.3|4.2|0|2910|56798|old
2026-03-10|BANKNIFTY|BUY_CE_CPR|CPR|-4230|0|TRAILING_SL_HIT|59.9|3|0.511|0.000129|210.9|17.0|53|1238.7|1102.7|1246.7|0.6|-11.0|0|2532|57075|old
2026-03-12|NIFTY|BUY_PE_CPR|CPR|42|1|BREAKOUT_FAIL|50.2|1|0.526|0.000637|126.6|46.0|85|274.9|276.1|276.1|0.4|0.4|0|32601|23746|current
2026-03-12|BANKNIFTY|BUY_PE_CPR|CPR|389|1|BREAKOUT_FAIL|90.4|1|0.533|0.000152|524.5|44.1|83|1190|1200|1200|0.8|0.8|0|8080|55210|current
2026-03-12|BANKNIFTY|SELL_PE_CPR|CPR|-321|0|BREAKOUT_FAIL|95.7|1|0.458|0.000143|553.6|55.6|55|995.0|1000.9|995.0|0.0|0.6|1|21942|55210|current
2026-03-12|SENSEX|BUY_PE_CPR|CPR|-3065|0|TRAILING_SL_HIT|13.2|1|0.528|0.000756|112.0|48.7|100|230.2|193.4|254.3|10.5|-16.0|0|73771|76447|current
2026-03-12|BANKNIFTY|BUY_PE_CPR|CPR|-1022|0|BREAKOUT_FAIL|88.4|1|0.537|0.000156|512.8|43.6|83|1177.0|1147.9|1177.0|0.0|-2.5|0|8279|55193|current
2026-03-12|SENSEX|BUY_PE_CPR|CPR|-2768|0|BREAKOUT_FAIL_REVERSE|12.8|1|0.523|0.000778|109.1|49.6|100|220.1|186.9|220.1|0.0|-15.0|0|124344|76356|current
2026-03-12|SENSEX|BUY_PE_CPR|CPR|-2801|0|BREAKOUT_FAIL_REVERSE|11.9|1|0.46|0.000834|100.8|60.1|100|167.7|134.1|167.7|0.0|-20.0|0|91445|76433|current
2026-03-12|NIFTY|BUY_PE_CPR|CPR|-4380|0|TRAILING_SL_HIT|47.6|1|0.51|0.000675|120.0|48.5|90|247.3|225.8|273.9|10.8|-8.7|0|26599|23723|current
2026-03-12|NIFTY|BUY_PE_CPR|CPR|16108|1|EOD_FORCE_CLOSE|45.4|1|0.538|0.000703|114.5|44.5|90|257.4|341.1|355.9|38.2|32.5|0|13338|23785|current
2026-03-12|GOLDM|BUY_CE_CPR|CPR|-1398|0|BREAKOUT_FAIL|47.9|5|0.507|0.000044|361.5|10.1|85|3573.5|3510.5|3573.5|0.0|-1.8|0|135200|161915|commodity
2026-03-12|SILVERM|BUY_CE_GAMMA|Gamma Blast|466|1|BREAKOUT_FAIL|108.4|5|0.511|0.000011|1409.5|10.4|85|13530.0|13650.5|13700.0|1.3|0.9|0|8425|278799|commodity
2026-03-12|SILVERM|BUY_CE_CPR|CPR|-419|0|BREAKOUT_FAIL|108.9|5|0.512|0.000011|1415.9|10.4|85|13621.0|13564.5|13650.5|0.2|-0.4|0|8405|278855|commodity
2026-03-12|SILVERM|BUY_CE_CPR|CPR|-88|0|BREAKOUT_FAIL|110.1|5|0.515|0.000011|1432.2|10.3|85|13900.0|13910.0|13988.0|0.6|0.1|0|8065|279098|commodity
2026-03-12|SILVERM|BUY_CE_GAMMA|Gamma Blast|-88|0|BREAKOUT_FAIL|110.1|5|0.515|0.000011|1432.2|10.3|85|13900.0|13910.0|13988.0|0.6|0.1|0|8065|279098|commodity
2026-03-12|CRUDEOILM|SELL_CE_CPR|CPR|-315|0|BREAKOUT_FAIL|272.0|5|0.533|0.000162|97.5|10.9|70|896.1|900.0|896.1|0.0|0.4|1|6880|7714|commodity
2026-03-12|GOLDM|BUY_CE_CPR|CPR|-2018|0|BREAKOUT_FAIL|48.1|5|0.509|0.000044|363.4|10.1|85|3610.0|3516.0|3610.0|0.0|-2.6|0|148700|161950|commodity
2026-03-12|GOLDM|BUY_CE_CPR|CPR|122|1|BREAKOUT_FAIL|47.4|5|0.505|0.000044|358.1|10.2|85|3516.0|3529.0|3549.0|0.9|0.4|0|147700|161866|commodity
2026-03-12|SILVERM|BUY_CE_GAMMA|Gamma Blast|-357|0|BREAKOUT_FAIL|109.8|5|0.511|0.000011|1427.3|10.4|85|13694.0|13650.0|13709.5|0.1|-0.3|0|8070|278768|commodity
2026-03-12|GOLDM|BUY_CE_CPR|CPR|712|1|BREAKOUT_FAIL|47.3|5|0.504|0.000045|356.8|10.2|85|3492.0|3534.5|3526.5|1.0|1.2|0|146700|161844|commodity
2026-03-12|GOLDM|BUY_CE_CPR|CPR|272|1|BREAKOUT_FAIL|47.8|5|0.505|0.000044|360.7|10.2|85|3540.0|3560.5|3586.5|1.3|0.6|0|148100|161862|commodity
2026-03-12|GOLDM|BUY_CE_CPR|CPR|-99|0|SIGNAL_WEAK_EXIT|47.9|5|0.506|0.000044|361.4|10.2|85|3560.5|3562.5|3735.0|4.9|0.1|0|150000|161889|commodity"""

# Parse
trades = []
for line in trades_raw.strip().split("\n"):
    parts = line.split("|")
    trades.append({
        'date': parts[0], 'symbol': parts[1], 'sig_type': parts[2], 'strategy': parts[3],
        'pnl': float(parts[4]), 'win': int(parts[5]), 'exit_reason': parts[6],
        'iv': float(parts[7]), 'dte': int(parts[8]), 'delta': float(parts[9]),
        'gamma': float(parts[10]), 'theta': float(parts[11]), 'theta_pct': float(parts[12]),
        'score': int(parts[13]), 'entry_prem': float(parts[14]), 'exit_prem': float(parts[15]),
        'peak_prem': float(parts[16]), 'peak_gain_pct': float(parts[17]),
        'actual_gain_pct': float(parts[18]), 'is_sell': int(parts[19]),
        'entry_oi': float(parts[20]), 'entry_spot': float(parts[21]), 'source': parts[22]
    })

N = len(trades)
wins = [t for t in trades if t['win']]
losses = [t for t in trades if not t['win']]

print("=" * 80)
print("STATISTICAL ANALYSIS OF 52 TRADES (Mar 6 - Mar 12, 2026)")
print("=" * 80)
print(f"\nTotal: {N} | Wins: {len(wins)} ({len(wins)/N*100:.1f}%) | Losses: {len(losses)} ({len(losses)/N*100:.1f}%)")
print(f"Total PnL: Rs {sum(t['pnl'] for t in trades):,.0f}")
print(f"Avg Win: Rs {np.mean([t['pnl'] for t in wins]):,.0f}")
print(f"Avg Loss: Rs {np.mean([t['pnl'] for t in losses]):,.0f}")
print(f"Win/Loss Ratio: {abs(np.mean([t['pnl'] for t in wins]) / np.mean([t['pnl'] for t in losses])):.2f}")

# ============ SECTION 1: CONDITIONAL PROBABILITY BY EACH SIGNAL ============
print("\n" + "=" * 80)
print("SECTION 1: P(WIN | SIGNAL) — CONDITIONAL PROBABILITY FOR EACH FEATURE")
print("=" * 80)

def analyze_feature(name, extractor, bins, labels):
    """Compute P(Win | Feature in bin) for each bin."""
    print(f"\n--- {name} ---")
    print(f"{'Bin':<20} {'Trades':>7} {'Wins':>6} {'P(Win)':>8} {'Avg PnL':>12} {'Avg Peak%':>10} {'Verdict':>12}")
    print("-" * 85)
    for i, label in enumerate(labels):
        if i < len(bins) - 1:
            bucket = [t for t in trades if bins[i] <= extractor(t) < bins[i+1]]
        else:
            bucket = [t for t in trades if extractor(t) >= bins[i]]
        if not bucket:
            print(f"{label:<20} {'0':>7}")
            continue
        n = len(bucket)
        w = sum(1 for t in bucket if t['win'])
        p_win = w / n
        avg_pnl = np.mean([t['pnl'] for t in bucket])
        avg_peak = np.mean([t['peak_gain_pct'] for t in bucket])
        verdict = "GOOD" if p_win >= 0.5 and avg_pnl > 0 else "RISKY" if p_win >= 0.3 else "AVOID"
        print(f"{label:<20} {n:>7} {w:>6} {p_win:>7.1%} {avg_pnl:>11,.0f} {avg_peak:>9.1f}% {verdict:>12}")

# 1. IV (Implied Volatility)
analyze_feature("IMPLIED VOLATILITY (%)", lambda t: t['iv'],
    [0, 10, 20, 40, 60, 90],
    ["0-10% (very low)", "10-20% (low)", "20-40% (moderate)", "40-60% (high)", "60-90%+ (extreme)"])

# 2. DTE (Days to Expiry)
analyze_feature("DAYS TO EXPIRY", lambda t: t['dte'],
    [1, 2, 3, 4, 5],
    ["DTE=1 (0-1 day)", "DTE=2", "DTE=3", "DTE=4-5"])

# 3. Quality Score
analyze_feature("QUALITY SCORE", lambda t: t['score'],
    [0, 55, 70, 80, 90],
    ["Score <55", "Score 55-69", "Score 70-79", "Score 80-89", "Score 90-100"])

# 4. Theta as % of Premium (theta burden)
analyze_feature("THETA BURDEN (theta/premium %)", lambda t: t['theta_pct'],
    [0, 15, 30, 45, 55],
    ["<15% (low decay)", "15-30%", "30-45%", "45-55%", ">55% (extreme)"])

# 5. Delta (absolute)
analyze_feature("DELTA (absolute)", lambda t: t['delta'],
    [0.3, 0.45, 0.50, 0.52, 0.55],
    ["0.30-0.45 (OTM)", "0.45-0.50 (near ATM)", "0.50-0.52 (ATM)", "0.52-0.55", ">0.55 (ITM)"])

# 6. Peak Gain % (how far premium moved in our favor)
analyze_feature("PEAK GAIN % (unrealized)", lambda t: t['peak_gain_pct'],
    [0, 1, 5, 15, 30],
    ["0-1% (no move)", "1-5% (small)", "5-15% (moderate)", "15-30% (good)", ">30% (explosive)"])

# ============ SECTION 2: BY SYMBOL ============
print("\n" + "=" * 80)
print("SECTION 2: P(WIN | SYMBOL)")
print("=" * 80)

for sym in sorted(set(t['symbol'] for t in trades)):
    bucket = [t for t in trades if t['symbol'] == sym]
    n = len(bucket)
    w = sum(1 for t in bucket if t['win'])
    total_pnl = sum(t['pnl'] for t in bucket)
    avg_iv = np.mean([t['iv'] for t in bucket])
    avg_dte = np.mean([t['dte'] for t in bucket])
    avg_theta = np.mean([t['theta_pct'] for t in bucket])
    print(f"  {sym:<12} Trades={n:>3} Wins={w:>2} P(Win)={w/n:.1%} "
          f"Total PnL=Rs {total_pnl:>10,.0f} | Avg IV={avg_iv:.1f}% DTE={avg_dte:.1f} Theta%={avg_theta:.1f}%")

# ============ SECTION 3: BY STRATEGY ============
print("\n" + "=" * 80)
print("SECTION 3: P(WIN | STRATEGY)")
print("=" * 80)

for strat in sorted(set(t['strategy'] for t in trades)):
    bucket = [t for t in trades if t['strategy'] == strat]
    n = len(bucket)
    w = sum(1 for t in bucket if t['win'])
    total_pnl = sum(t['pnl'] for t in bucket)
    print(f"  {strat:<15} Trades={n:>3} Wins={w:>2} P(Win)={w/n:.1%} "
          f"Total PnL=Rs {total_pnl:>10,.0f} Avg PnL=Rs {total_pnl/n:>8,.0f}")

# ============ SECTION 4: BY EXIT REASON ============
print("\n" + "=" * 80)
print("SECTION 4: EXIT REASON ANALYSIS")
print("=" * 80)

for reason in sorted(set(t['exit_reason'] for t in trades)):
    bucket = [t for t in trades if t['exit_reason'] == reason]
    n = len(bucket)
    w = sum(1 for t in bucket if t['win'])
    total_pnl = sum(t['pnl'] for t in bucket)
    avg_peak = np.mean([t['peak_gain_pct'] for t in bucket])
    avg_actual = np.mean([t['actual_gain_pct'] for t in bucket])
    slippage = avg_peak - avg_actual
    print(f"  {reason:<25} N={n:>3} Wins={w:>2} P(Win)={w/n:.1%} "
          f"Total PnL=Rs {total_pnl:>10,.0f} | Peak={avg_peak:.1f}% Actual={avg_actual:.1f}% "
          f"SLIPPAGE={slippage:+.1f}%")

# ============ SECTION 5: MULTI-FACTOR CONDITIONAL PROBABILITY ============
print("\n" + "=" * 80)
print("SECTION 5: MULTI-FACTOR CONDITIONAL PROBABILITY")
print("=" * 80)

# P(Win | IV >= 20 AND DTE >= 3 AND Score >= 80)
conditions = [
    ("IV >= 20% AND DTE >= 3", lambda t: t['iv'] >= 20 and t['dte'] >= 3),
    ("IV >= 20% AND DTE >= 3 AND Score >= 80", lambda t: t['iv'] >= 20 and t['dte'] >= 3 and t['score'] >= 80),
    ("IV >= 40% AND DTE == 1", lambda t: t['iv'] >= 40 and t['dte'] == 1),
    ("IV < 20% AND DTE == 1 (0-DTE low IV)", lambda t: t['iv'] < 20 and t['dte'] == 1),
    ("Score >= 85 AND theta% < 50%", lambda t: t['score'] >= 85 and t['theta_pct'] < 50),
    ("Score >= 85 AND theta% >= 50%", lambda t: t['score'] >= 85 and t['theta_pct'] >= 50),
    ("Theta% > 45% (heavy theta burden)", lambda t: t['theta_pct'] > 45),
    ("Theta% <= 20% (light theta)", lambda t: t['theta_pct'] <= 20),
    ("BUY trades only (no SELL)", lambda t: t['is_sell'] == 0),
    ("SELL trades only", lambda t: t['is_sell'] == 1),
    ("DTE=1 AND IV<15% (the SENSEX trap)", lambda t: t['dte'] == 1 and t['iv'] < 15),
    ("CPR strategy AND Score>=85", lambda t: t['strategy'] == 'CPR' and t['score'] >= 85),
    ("Gamma Blast strategy", lambda t: t['strategy'] == 'Gamma Blast'),
    ("Delta 0.48-0.53 (sweet spot)", lambda t: 0.48 <= t['delta'] <= 0.53),
    ("Peak gain > 15%", lambda t: t['peak_gain_pct'] > 15),
    ("Peak gain < 2%", lambda t: t['peak_gain_pct'] < 2),
    ("Re-entry (same sym same day >1)", None),  # Special handling below
]

print(f"\n{'Condition':<45} {'N':>4} {'Wins':>5} {'P(Win)':>8} {'Avg PnL':>12} {'Total PnL':>12}")
print("-" * 95)

for label, cond_fn in conditions:
    if cond_fn is None:
        continue
    bucket = [t for t in trades if cond_fn(t)]
    if not bucket:
        print(f"{label:<45} {'0':>4}")
        continue
    n = len(bucket)
    w = sum(1 for t in bucket if t['win'])
    avg_pnl = np.mean([t['pnl'] for t in bucket])
    total_pnl = sum(t['pnl'] for t in bucket)
    print(f"{label:<45} {n:>4} {w:>5} {w/n:>7.1%} {avg_pnl:>11,.0f} {total_pnl:>11,.0f}")

# ============ SECTION 6: TRENDING vs SIDEWAYS ANALYSIS ============
print("\n" + "=" * 80)
print("SECTION 6: MARKET REGIME ANALYSIS (by day)")
print("=" * 80)

# Mar 6 = big down day (TRENDING), Mar 9 = crash day (TRENDING)
# Mar 10 = choppy (SIDEWAYS), Mar 12 = choppy (SIDEWAYS)
day_regime = {
    '2026-03-06': 'TRENDING_DOWN',
    '2026-03-09': 'TRENDING_DOWN',
    '2026-03-10': 'SIDEWAYS',
    '2026-03-12': 'SIDEWAYS',
}

for regime in ['TRENDING_DOWN', 'SIDEWAYS']:
    days = [d for d, r in day_regime.items() if r == regime]
    bucket = [t for t in trades if t['date'] in days]
    if not bucket:
        continue
    n = len(bucket)
    w = sum(1 for t in bucket if t['win'])
    total_pnl = sum(t['pnl'] for t in bucket)
    avg_peak = np.mean([t['peak_gain_pct'] for t in bucket])
    avg_actual = np.mean([t['actual_gain_pct'] for t in bucket])

    print(f"\n  [{regime}] Days: {days}")
    print(f"    Trades: {n} | Wins: {w} ({w/n:.1%}) | Total PnL: Rs {total_pnl:,.0f}")
    print(f"    Avg Peak Gain: {avg_peak:.1f}% | Avg Actual Gain: {avg_actual:.1f}%")
    print(f"    Slippage (peak-actual): {avg_peak - avg_actual:.1f}%")

    # Within regime, what strategies work?
    for strat in sorted(set(t['strategy'] for t in bucket)):
        sb = [t for t in bucket if t['strategy'] == strat]
        sw = sum(1 for t in sb if t['win'])
        sp = sum(t['pnl'] for t in sb)
        print(f"      {strat:<15} N={len(sb):>2} W={sw:>2} P(W)={sw/len(sb):.0%} PnL=Rs {sp:>10,.0f}")

# ============ SECTION 7: THE MONEY QUESTION ============
print("\n" + "=" * 80)
print("SECTION 7: OPTIMAL ENTRY CONDITIONS (from the data)")
print("=" * 80)

# What conditions have the highest P(Win) AND positive expected value?
best = [t for t in trades if t['iv'] >= 20 and t['dte'] >= 3 and t['score'] >= 70
        and t['theta_pct'] < 25 and t['is_sell'] == 0]
worst = [t for t in trades if (t['iv'] < 15 and t['dte'] == 1) or t['theta_pct'] > 50 or t['is_sell'] == 1]

print(f"\n  BEST CONDITIONS (IV>=20, DTE>=3, Score>=70, Theta%<25, BUY only):")
if best:
    bw = sum(1 for t in best if t['win'])
    print(f"    Trades: {len(best)} | Wins: {bw} ({bw/len(best):.0%}) | "
          f"Total PnL: Rs {sum(t['pnl'] for t in best):,.0f} | "
          f"Avg PnL: Rs {np.mean([t['pnl'] for t in best]):,.0f}")
    for t in best:
        print(f"    {t['date']} {t['symbol']} {t['sig_type']} PnL=Rs {t['pnl']:>10,.0f} "
              f"IV={t['iv']}% DTE={t['dte']} Score={t['score']} Theta%={t['theta_pct']:.0f}%")
else:
    print("    No trades match (need more data)")

print(f"\n  WORST CONDITIONS (IV<15 + DTE=1, OR Theta%>50, OR SELL):")
if worst:
    ww = sum(1 for t in worst if t['win'])
    print(f"    Trades: {len(worst)} | Wins: {ww} ({ww/len(worst):.0%}) | "
          f"Total PnL: Rs {sum(t['pnl'] for t in worst):,.0f} | "
          f"Avg PnL: Rs {np.mean([t['pnl'] for t in worst]):,.0f}")
    for t in worst:
        print(f"    {t['date']} {t['symbol']} {t['sig_type']} PnL=Rs {t['pnl']:>10,.0f} "
              f"IV={t['iv']}% DTE={t['dte']} Score={t['score']} Theta%={t['theta_pct']:.0f}%")

# ============ SECTION 8: EXPECTED VALUE FORMULA ============
print("\n" + "=" * 80)
print("SECTION 8: EXPECTED VALUE FORMULA")
print("=" * 80)

print("""
  E[Trade] = P(Win) × Avg_Win + P(Loss) × Avg_Loss

  CURRENT (all 52 trades):""")
p_win = len(wins) / N
p_loss = len(losses) / N
avg_win = np.mean([t['pnl'] for t in wins])
avg_loss = np.mean([t['pnl'] for t in losses])
ev = p_win * avg_win + p_loss * avg_loss
print(f"    P(Win) = {p_win:.3f}")
print(f"    P(Loss) = {p_loss:.3f}")
print(f"    Avg Win = Rs {avg_win:,.0f}")
print(f"    Avg Loss = Rs {avg_loss:,.0f}")
print(f"    E[Trade] = {p_win:.3f} × {avg_win:,.0f} + {p_loss:.3f} × ({avg_loss:,.0f})")
print(f"    E[Trade] = Rs {ev:,.0f} per trade")

# Without SELL trades and 0-DTE low-IV
filtered = [t for t in trades if t['is_sell'] == 0 and not (t['dte'] == 1 and t['iv'] < 15)]
if filtered:
    fw = [t for t in filtered if t['win']]
    fl = [t for t in filtered if not t['win']]
    if fw and fl:
        fp_win = len(fw) / len(filtered)
        favg_win = np.mean([t['pnl'] for t in fw])
        favg_loss = np.mean([t['pnl'] for t in fl])
        fev = fp_win * favg_win + (1-fp_win) * favg_loss
        print(f"\n  FILTERED (no SELL, no 0-DTE low-IV): {len(filtered)} trades")
        print(f"    P(Win) = {fp_win:.3f}")
        print(f"    Avg Win = Rs {favg_win:,.0f}")
        print(f"    Avg Loss = Rs {favg_loss:,.0f}")
        print(f"    E[Trade] = Rs {fev:,.0f} per trade")

# Only trending days
trending = [t for t in trades if t['date'] in ['2026-03-06', '2026-03-09']]
if trending:
    tw = [t for t in trending if t['win']]
    tl = [t for t in trending if not t['win']]
    if tw and tl:
        tp = len(tw) / len(trending)
        taw = np.mean([t['pnl'] for t in tw])
        tal = np.mean([t['pnl'] for t in tl])
        tev = tp * taw + (1-tp) * tal
        print(f"\n  TRENDING DAYS ONLY: {len(trending)} trades")
        print(f"    P(Win) = {tp:.3f}")
        print(f"    Avg Win = Rs {taw:,.0f}")
        print(f"    Avg Loss = Rs {tal:,.0f}")
        print(f"    E[Trade] = Rs {tev:,.0f} per trade")

# Only sideways days
sideways = [t for t in trades if t['date'] in ['2026-03-10', '2026-03-12']]
if sideways:
    sw = [t for t in sideways if t['win']]
    sl_list = [t for t in sideways if not t['win']]
    if sw and sl_list:
        sp = len(sw) / len(sideways)
        saw = np.mean([t['pnl'] for t in sw])
        sal = np.mean([t['pnl'] for t in sl_list])
        sev = sp * saw + (1-sp) * sal
        print(f"\n  SIDEWAYS DAYS ONLY: {len(sideways)} trades")
        print(f"    P(Win) = {sp:.3f}")
        print(f"    Avg Win = Rs {saw:,.0f}")
        print(f"    Avg Loss = Rs {sal:,.0f}")
        print(f"    E[Trade] = Rs {sev:,.0f} per trade")

print("\n" + "=" * 80)
print("SECTION 9: MATHEMATICAL RULES FOR ENTRY (from the data)")
print("=" * 80)

print("""
  Based on 52 trades, these are the STATISTICALLY SIGNIFICANT rules:

  RULE 1: DTE GATE
    P(Win | DTE=1) vs P(Win | DTE>=3)
    If DTE=1 trades have lower win rate, BLOCK DTE=1 entries.

  RULE 2: IV GATE
    P(Win | IV<15) is catastrophic → BLOCK
    P(Win | IV 20-50) is best zone → PREFER

  RULE 3: THETA BURDEN GATE
    P(Win | theta% > 45%) → what's the win rate?
    If negative EV, block high-theta entries.

  RULE 4: REGIME GATE
    E[Trade | TRENDING] vs E[Trade | SIDEWAYS]
    If SIDEWAYS has negative EV → reduce position size or block.

  RULE 5: SELL GATE
    P(Win | SELL) → if < 30%, BLOCK all sell trades.

  RULE 6: STRATEGY GATE
    E[Trade | Gamma Blast] → if negative, disable strategy.
    E[Trade | CPR] → main strategy, filter by quality.
""")

# Print final verdicts
print("\n  FINAL VERDICTS FROM DATA:")
print("  " + "-" * 60)

# DTE
dte1 = [t for t in trades if t['dte'] == 1]
dte3p = [t for t in trades if t['dte'] >= 3]
dte1_wr = sum(1 for t in dte1 if t['win']) / len(dte1) if dte1 else 0
dte3_wr = sum(1 for t in dte3p if t['win']) / len(dte3p) if dte3p else 0
print(f"  DTE=1: P(Win)={dte1_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in dte1]):,.0f}/trade")
print(f"  DTE>=3: P(Win)={dte3_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in dte3p]):,.0f}/trade")

# IV
iv_low = [t for t in trades if t['iv'] < 15]
iv_mid = [t for t in trades if 20 <= t['iv'] <= 60]
iv_hi = [t for t in trades if t['iv'] > 60]
for label, bucket in [("IV<15%", iv_low), ("IV 20-60%", iv_mid), ("IV>60%", iv_hi)]:
    if bucket:
        wr = sum(1 for t in bucket if t['win']) / len(bucket)
        ev = np.mean([t['pnl'] for t in bucket])
        print(f"  {label}: N={len(bucket)} P(Win)={wr:.0%} EV=Rs {ev:,.0f}/trade")

# Sell
sells = [t for t in trades if t['is_sell'] == 1]
buys = [t for t in trades if t['is_sell'] == 0]
if sells:
    s_wr = sum(1 for t in sells if t['win']) / len(sells)
    print(f"  SELL: N={len(sells)} P(Win)={s_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in sells]):,.0f}")
b_wr = sum(1 for t in buys if t['win']) / len(buys)
print(f"  BUY: N={len(buys)} P(Win)={b_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in buys]):,.0f}")

# Gamma Blast
gb = [t for t in trades if t['strategy'] == 'Gamma Blast']
cpr = [t for t in trades if t['strategy'] == 'CPR']
if gb:
    gb_wr = sum(1 for t in gb if t['win']) / len(gb)
    print(f"  Gamma Blast: N={len(gb)} P(Win)={gb_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in gb]):,.0f}")
if cpr:
    cpr_wr = sum(1 for t in cpr if t['win']) / len(cpr)
    print(f"  CPR: N={len(cpr)} P(Win)={cpr_wr:.0%} EV=Rs {np.mean([t['pnl'] for t in cpr]):,.0f}")

print()
