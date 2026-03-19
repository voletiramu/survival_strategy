#!/usr/bin/env python3
"""Volatility-adjusted SL simulation per symbol.
Tests: current SL vs ATR-scaled SL vs momentum exit vs combined."""
import json
from collections import defaultdict

d = json.load(open('D:/AlgoTrading/temp_equity_state.json'))
cl = d.get('closed_trades', [])
clean = [t for t in cl if t.get('exit_reason') not in ('THETA_DECAY_EXIT', 'BREAKOUT_FAIL_REVERSE')]

# ATR values from live data
ATR = {'NIFTY': 313, 'BANKNIFTY': 851, 'SENSEX': 1094}
LOT = {'NIFTY': 195, 'BANKNIFTY': 30, 'SENSEX': 20}
CURRENT_SL_PCT = 0.50  # 50% of premium

print("=" * 100)
print("  ROOT CAUSE ANALYSIS + VOLATILITY-ADJUSTED SL SIMULATION")
print("=" * 100)

# Show the core problem
print("""
  THE CORE PROBLEM:

  All 3 indices use the SAME SL% (50% of premium), but volatility differs 3x:

  Symbol      ATR    Avg Premium  Lot   Cost/Trade   SL (50%)    Risk/Trade
  --------    ----   ----------   ---   ----------   --------    ----------""")

for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
    trades = [t for t in clean if t.get('symbol') == sym]
    if not trades: continue
    avg_prem = sum(t.get('entry_premium',0) for t in trades)/len(trades)
    lot = LOT[sym]
    cost = avg_prem * lot
    sl_rs = avg_prem * CURRENT_SL_PCT * lot
    print(f"  {sym:<12} {ATR[sym]:>4}   Rs {avg_prem:>6,.0f}     {lot:>3}   Rs {cost:>8,.0f}   Rs {avg_prem*CURRENT_SL_PCT:>5,.0f}     Rs {sl_rs:>6,.0f}")

print("""
  NIFTY premium is CHEAP (Rs 162) but lot is BIG (195) = Rs 31K risk
  BANKNIFTY premium is EXPENSIVE (Rs 1,092) but lot is small (30) = Rs 33K risk

  BUT: BANKNIFTY is 2.7x more volatile than NIFTY!
  When BANKNIFTY reverses, premium drops 16-21% (vs NIFTY 9-13%)
  Same SL% fails because BANKNIFTY moves FASTER through the SL zone

  SOLUTION: Scale SL inversely with volatility
  SL% = base_SL * (NIFTY_ATR / symbol_ATR)
""")

# Calculate volatility-adjusted SL for each symbol
vol_sl = {}
for sym in ATR:
    vol_sl[sym] = CURRENT_SL_PCT * (ATR['NIFTY'] / ATR[sym])

print("  VOLATILITY-ADJUSTED SL:")
for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
    trades = [t for t in clean if t.get('symbol') == sym]
    if not trades: continue
    avg_prem = sum(t.get('entry_premium',0) for t in trades)/len(trades)
    print(f"  {sym:<12} Current: {CURRENT_SL_PCT*100:.0f}% (Rs {avg_prem*CURRENT_SL_PCT:,.0f}) --> "
          f"Vol-Adj: {vol_sl[sym]*100:.0f}% (Rs {avg_prem*vol_sl[sym]:,.0f})")

# Simulate 4 scenarios on each symbol
print(f"\n{'='*100}")
print("  SIMULATION: 4 SCENARIOS PER SYMBOL")
print(f"{'='*100}")

# Momentum exit parameters
MOM_SPOT_PCT = 0.4
MOM_MIN_MINS = 20
# For momentum exit, we use the saved trades data from previous simulation
momentum_saved = {
    'CPR_BANKNIFTY_102610': 3156,
    'CPR_BANKNIFTY_105702': 2033,
    'CPR_SENSEX_103000': 1785,
}

for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
    trades = [t for t in clean if t.get('symbol') == sym]
    if not trades: continue

    s1_pnl = 0  # Actual
    s2_pnl = 0  # Vol-adjusted SL
    s3_pnl = 0  # Momentum exit only
    s4_pnl = 0  # Vol-adjusted SL + Momentum exit

    s1_wins = s2_wins = s3_wins = s4_wins = 0
    details = []

    for t in trades:
        ep = t.get('entry_premium', 0)
        cp = t.get('current_premium', 0)
        pnl = t.get('pnl', 0)
        sig = t.get('signal_type', '')
        lot = t.get('lot_size', 1)
        mult = t.get('multiplier', 1)
        tid = t.get('id', '')

        # S1: Actual
        s1_pnl += pnl
        if pnl > 0: s1_wins += 1

        # S2: Vol-adjusted SL
        if ep > 0:
            prem_drop_pct = (cp - ep) / ep
            if 'SELL' in sig:
                prem_drop_pct = -prem_drop_pct

            if prem_drop_pct < -vol_sl[sym]:
                # Would have been stopped out at vol-adjusted SL
                sl_exit = ep * (1 - vol_sl[sym])
                s2_new_pnl = (sl_exit - ep) * lot * mult
            else:
                s2_new_pnl = pnl
        else:
            s2_new_pnl = pnl
        s2_pnl += s2_new_pnl
        if s2_new_pnl > 0: s2_wins += 1

        # S3: Momentum exit only
        s3_new_pnl = pnl
        if tid in momentum_saved:
            s3_new_pnl = pnl + momentum_saved[tid]
        s3_pnl += s3_new_pnl
        if s3_new_pnl > 0: s3_wins += 1

        # S4: Both
        s4_new_pnl = s2_new_pnl  # Start with vol-SL
        if tid in momentum_saved:
            # Momentum exit might override vol-SL (take the better exit)
            mom_pnl = pnl + momentum_saved[tid]
            s4_new_pnl = max(s4_new_pnl, mom_pnl)  # Take the less-losing option
        s4_pnl += s4_new_pnl
        if s4_new_pnl > 0: s4_wins += 1

        if abs(s2_new_pnl - pnl) > 1 or tid in momentum_saved:
            details.append({
                'ts': t.get('timestamp','')[:16], 'sig': sig,
                'actual': pnl, 'vol_sl': s2_new_pnl,
                'mom': s3_new_pnl, 'both': s4_new_pnl,
            })

    n = len(trades)

    def dd(trades_pnl_list):
        cum = 0; peak = 0; mdd = 0
        for p in trades_pnl_list:
            cum += p;
            if cum > peak: peak = cum
            if peak - cum > mdd: mdd = peak - cum
        return mdd

    # Compute PF
    def pf(total, wins_pnl, losses_pnl):
        return wins_pnl / max(abs(losses_pnl), 0.01) if losses_pnl != 0 else 999

    s1_gw = sum(t.get('pnl',0) for t in trades if t.get('pnl',0) > 0)
    s1_gl = abs(sum(t.get('pnl',0) for t in trades if t.get('pnl',0) <= 0))

    print(f"\n  {sym} ({n} trades):")
    print(f"  {'Scenario':<30} {'PnL':>10} {'WR':>6} {'PF':>6} {'Monthly':>10}")
    print(f"  {'-'*30} {'-'*10} {'-'*6} {'-'*6} {'-'*10}")
    print(f"  {'S1: Actual (50% SL)':<30} Rs {s1_pnl:>+7,.0f} {s1_wins/n*100:>5.0f}% {s1_gw/max(s1_gl,1):>5.2f} Rs {s1_pnl/7*22:>+7,.0f}")
    print(f"  {'S2: Vol-adj SL ('+str(round(vol_sl[sym]*100))+'%)':<30} Rs {s2_pnl:>+7,.0f} {s2_wins/n*100:>5.0f}% {'--':>5} Rs {s2_pnl/7*22:>+7,.0f}")
    print(f"  {'S3: Momentum exit (0.4%/20m)':<30} Rs {s3_pnl:>+7,.0f} {s3_wins/n*100:>5.0f}% {'--':>5} Rs {s3_pnl/7*22:>+7,.0f}")
    print(f"  {'S4: Vol-SL + Momentum':<30} Rs {s4_pnl:>+7,.0f} {s4_wins/n*100:>5.0f}% {'--':>5} Rs {s4_pnl/7*22:>+7,.0f}")

    if details:
        print(f"\n  Affected trades:")
        for d in details:
            print(f"    {d['ts']} {d['sig']:<14} Actual={d['actual']:>+7,.0f} VolSL={d['vol_sl']:>+7,.0f} Mom={d['mom']:>+7,.0f} Both={d['both']:>+7,.0f}")

# Grand total
print(f"\n{'='*100}")
print("  GRAND TOTAL - ALL EQUITY INDICES COMBINED")
print(f"{'='*100}")

scenarios = {'S1: Actual (50% SL)': 0, 'S2: Vol-adjusted SL': 0, 'S3: Momentum exit': 0, 'S4: Vol-SL + Momentum': 0}

for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
    trades = [t for t in clean if t.get('symbol') == sym]
    for t in trades:
        ep = t.get('entry_premium', 0)
        cp = t.get('current_premium', 0)
        pnl = t.get('pnl', 0)
        sig = t.get('signal_type', '')
        lot = t.get('lot_size', 1)
        mult = t.get('multiplier', 1)
        tid = t.get('id', '')

        scenarios['S1: Actual (50% SL)'] += pnl

        # Vol-SL
        if ep > 0:
            prem_drop = (cp - ep) / ep
            if 'SELL' in sig: prem_drop = -prem_drop
            if prem_drop < -vol_sl[sym]:
                s2p = (ep * (1 - vol_sl[sym]) - ep) * lot * mult
            else:
                s2p = pnl
        else:
            s2p = pnl
        scenarios['S2: Vol-adjusted SL'] += s2p

        # Momentum
        s3p = pnl + momentum_saved.get(tid, 0)
        scenarios['S3: Momentum exit'] += s3p

        # Both
        s4p = s2p
        if tid in momentum_saved:
            s4p = max(s2p, pnl + momentum_saved[tid])
        scenarios['S4: Vol-SL + Momentum'] += s4p

print(f"\n  {'Scenario':<30} {'Weekly PnL':>12} {'Monthly':>12} {'vs Actual':>12}")
print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*12}")
for name, pnl in scenarios.items():
    delta = pnl - scenarios['S1: Actual (50% SL)']
    print(f"  {name:<30} Rs {pnl:>+8,.0f} Rs {pnl/7*22:>+8,.0f} Rs {delta:>+8,.0f}")

# Add commodity and stock
comm = json.load(open('D:/AlgoTrading/temp_comm_state.json'))
stock = json.load(open('D:/AlgoTrading/temp_stock_state.json'))
comm_pnl = sum(t.get('pnl',0) for t in comm.get('closed_trades',[]))
stock_pnl = sum(t.get('pnl',0) for t in stock.get('closed_trades',[]))

print(f"\n  + Commodity PnL:  Rs {comm_pnl:>+8,.0f}")
print(f"  + Stock PnL:      Rs {stock_pnl:>+8,.0f}")

best = scenarios['S4: Vol-SL + Momentum']
print(f"\n  BEST SCENARIO (S4: Vol-SL + Momentum):")
print(f"  Equity:    Rs {best:>+8,.0f}")
print(f"  Commodity: Rs {comm_pnl:>+8,.0f}")
print(f"  Stock:     Rs {stock_pnl:>+8,.0f}")
print(f"  TOTAL:     Rs {best+comm_pnl+stock_pnl:>+8,.0f}/week = Rs {(best+comm_pnl+stock_pnl)/7*22:>+8,.0f}/month")

worst = scenarios['S1: Actual (50% SL)']
print(f"\n  vs ACTUAL:")
print(f"  TOTAL:     Rs {worst+comm_pnl+stock_pnl:>+8,.0f}/week = Rs {(worst+comm_pnl+stock_pnl)/7*22:>+8,.0f}/month")
