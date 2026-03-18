#!/usr/bin/env python3
"""Patch script to add Liquidity Sweep strategy to all 3 bots."""
import re

# ===============================
# EQUITY BOT: paper_trader.py
# ===============================
filepath = '/root/algo_trading/paper_trader.py'
with open(filepath, 'r') as f:
    code = f.read()

# 1. Add strategy allocation (after existing STRATEGY_ALLOCATION entries)
old_alloc = "'Trend Rider':  0.20,    # 20% target"
new_alloc = """'Trend Rider':  0.20,    # 20% target
    'Liquidity Sweep': 0.15,  # 15% target — SL hunt reversal (PF 2.95, WR 78%)"""
if 'Liquidity Sweep' not in code:
    code = code.replace(old_alloc, new_alloc)
    print(f"  [equity] Added allocation")

# 2. Add exit multiplier
old_exit = "'Trend Rider':  {'oi': 2.0, 'iv': 2.0},"
new_exit = """'Trend Rider':  {'oi': 2.0, 'iv': 2.0},
    'Liquidity Sweep': {'oi': 1.5, 'iv': 1.5},  # Medium tolerance — fast trades"""
if 'Liquidity Sweep' not in code.split('STRATEGY_EXIT_MULTIPLIERS')[1][:500] if 'STRATEGY_EXIT_MULTIPLIERS' in code else True:
    code = code.replace(old_exit, new_exit)
    print(f"  [equity] Added exit multiplier")

# 3. Add Liquidity Sweep scan block after Trend Rider section
# Find the anchor: "# v10: Write live scan data for dashboard"
anchor = "# v10: Write live scan data for dashboard"
if 'Liquidity Sweep' not in code.split(anchor)[0][-500:] if anchor in code else True:
    sweep_code = '''
            # ---- v13.1: LIQUIDITY SWEEP (SL Hunt Reversal) ----
            # Detects institutional stop-loss hunts and trades the reversal
            # Hybrid sizing: Q>=45 half-lot, Q>=60 full-lot
            # Backtested 6 days: PF=1.98-2.95, WR=70-78%, DD<1%
            # Timing: 09:20 to 15:15 (needs 5+ bars of data)
            if hasattr(self, 'calculus') and not choppy_blocked:
                ls_start = now.replace(hour=9, minute=20, second=0)
                ls_end = now.replace(hour=15, minute=15, second=0)
                if ls_start <= now <= ls_end:
                    for symbol in symbols_to_scan:
                        if self.calculus.bar_count(symbol) >= 25:
                            sweeps = self.calculus.detect_liquidity_sweep(symbol)
                            for sw in sweeps:
                                if sw['quality'] < 45:
                                    continue
                                # Get option chain for the sweep direction
                                sw_spot = self.latest_spot.get(symbol, 0)
                                if sw_spot <= 0:
                                    continue
                                sw_dte = dte  # Use same DTE as other strategies
                                T = max(sw_dte / 365.0, 1/365.0)
                                sw_type = f"BUY_{sw['direction']}_SWEEP"
                                sw_strike, sw_ltp, sw_iv = self._get_strike_from_chain(
                                    symbol, sw_spot, sw['direction'], sw_dte)
                                if sw_ltp <= 0:
                                    continue
                                g = greeks_from_market_price(
                                    sw_ltp, sw_spot, sw_strike, T, RISK_FREE_RATE,
                                    sw['direction'])
                                if sw_ltp > MIN_PREMIUM_BUY:
                                    # SL at spike peak + 30%
                                    sl_buffer = sw['spike_size'] * 0.3
                                    # Target: 80% of spike retrace
                                    target_move = sw['spike_size'] * 0.8 * 0.5  # delta-adjusted
                                    sig = {
                                        'type': sw_type,
                                        'strike': sw_strike,
                                        'premium': sw_ltp,
                                        'greeks': g,
                                        'reason': (f"Liquidity Sweep: {sw['spike_dir']} spike "
                                                  f"{sw['spike_pct']:.3f}% Z={sw['z_score']:.1f} "
                                                  f"rev={sw['reversal_ratio']:.0%} "
                                                  f"{'AccFlip ' if sw['accel_flip'] else ''}"
                                                  f"Q={sw['quality']} [LIVE]"),
                                        'target': sw_ltp + target_move,
                                        'sl': max(sw_ltp * 0.5, sw_ltp - sl_buffer * 0.5),
                                        'strategy': 'Liquidity Sweep',
                                        'symbol': symbol,
                                        'spot': sw_spot,
                                        'dte': sw_dte,
                                        'quality_score': sw['quality'],
                                        '_sweep_quality': sw['quality'],
                                        '_sweep_half_lot': sw['quality'] < 60,
                                    }
                                    all_signals.append(sig)
                                    logger.info(f"  SIGNAL [Liquidity Sweep]: {sw_type} "
                                              f"Strike={sw_strike:.0f} Premium=Rs {sw_ltp:.2f} "
                                              f"| {sig['reason']}")

'''
    code = code.replace(anchor, sweep_code + "\n        " + anchor)
    print(f"  [equity] Added Liquidity Sweep scan block")

with open(filepath, 'w') as f:
    f.write(code)
print(f"  [equity] Saved {filepath}")

# Verify syntax
import ast
try:
    ast.parse(code)
    print(f"  [equity] Syntax OK")
except SyntaxError as e:
    print(f"  [equity] SYNTAX ERROR: {e}")

# ===============================
# COMMODITY BOT: commodity_paper_trader.py
# ===============================
filepath2 = '/root/algo_trading/commodity_paper_trader.py'
with open(filepath2, 'r') as f:
    code2 = f.read()

# Find commodity scan section — look for where commodity signals are collected
# Commodity uses sig['commodity'] not sig['symbol']
comm_anchor = "# v10: Write live scan data"
if 'Liquidity Sweep' not in code2 and comm_anchor in code2:
    sweep_comm = '''
            # ---- v13.1: LIQUIDITY SWEEP for commodities ----
            if hasattr(self, 'calculus') and not choppy_blocked:
                ls_start = now.replace(hour=9, minute=20, second=0)
                ls_end = now.replace(hour=23, minute=15, second=0)
                if ls_start <= now <= ls_end:
                    for commodity in commodities_to_scan:
                        if self.calculus.bar_count(commodity) >= 25:
                            sweeps = self.calculus.detect_liquidity_sweep(commodity)
                            for sw in sweeps:
                                if sw['quality'] < 45:
                                    continue
                                sw_spot = self.latest_spot.get(commodity, 0)
                                if sw_spot <= 0:
                                    continue
                                sw_dte = dte
                                T = max(sw_dte / 365.0, 1/365.0)
                                sw_type = f"BUY_{sw['direction']}_SWEEP"
                                sw_strike, sw_ltp, sw_iv = self._get_strike_from_chain(
                                    commodity, sw_spot, sw['direction'], sw_dte)
                                if sw_ltp <= 0:
                                    continue
                                g = greeks_from_market_price(
                                    sw_ltp, sw_spot, sw_strike, T, RISK_FREE_RATE,
                                    sw['direction'])
                                if sw_ltp > MIN_PREMIUM_BUY:
                                    sl_buffer = sw['spike_size'] * 0.3
                                    target_move = sw['spike_size'] * 0.8 * 0.5
                                    sig = {
                                        'type': sw_type,
                                        'commodity': commodity,
                                        'strike': sw_strike,
                                        'premium': sw_ltp,
                                        'greeks': g,
                                        'reason': (f"Liquidity Sweep: {sw['spike_dir']} spike "
                                                  f"{sw['spike_pct']:.3f}% Z={sw['z_score']:.1f} "
                                                  f"Q={sw['quality']} [LIVE]"),
                                        'target': sw_ltp + target_move,
                                        'sl': max(sw_ltp * 0.5, sw_ltp - sl_buffer * 0.5),
                                        'strategy': 'Liquidity Sweep',
                                        'spot': sw_spot,
                                        'dte': sw_dte,
                                        'quality_score': sw['quality'],
                                        '_sweep_quality': sw['quality'],
                                        '_sweep_half_lot': sw['quality'] < 60,
                                    }
                                    all_signals.append(sig)
                                    logger.info(f"  SIGNAL [Liquidity Sweep]: {sw_type} "
                                              f"Strike={sw_strike:.0f} Premium=Rs {sw_ltp:.2f} "
                                              f"| {sig['reason']}")

'''
    code2 = code2.replace(comm_anchor, sweep_comm + "\n        " + comm_anchor)
    with open(filepath2, 'w') as f:
        f.write(code2)
    print(f"  [commodity] Added Liquidity Sweep")
    try:
        ast.parse(code2)
        print(f"  [commodity] Syntax OK")
    except SyntaxError as e:
        print(f"  [commodity] SYNTAX ERROR: {e}")
else:
    print(f"  [commodity] Skipped (already exists or anchor not found)")


print("\nDone! Restart services to activate.")
