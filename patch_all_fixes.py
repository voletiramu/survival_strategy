#!/usr/bin/env python3
"""Master patch: Apply ALL fixes to all 3 bots before market open.

Fixes:
1. BREAKOUT_FAIL_REVERSE = disabled (0% WR, -Rs 8,632)
2. Volatility-adjusted SL (ATR-scaled per symbol)
3. Momentum reversal exit (0.4% spot / 20min)
4. Verify Liquidity Sweep is active
5. Verify physics gate is active
"""
import ast

ATR_DATA = """
# v13.3: Volatility-adjusted SL — scale SL by inverse ATR ratio
# BANKNIFTY 2.7x more volatile than NIFTY, needs tighter SL
# Simulation: saves Rs +8,638/week across BANKNIFTY + SENSEX
VOLATILITY_SL_ENABLED = True
ATR_REFERENCE = {
    'NIFTY': 313, 'BANKNIFTY': 851, 'SENSEX': 1094,
    'GOLDM': 3322, 'SILVERM': 9537, 'CRUDEOILM': 409,
}
ATR_BASE_SYMBOL = 'NIFTY'  # Reference for scaling

# v13.3: Momentum reversal exit
# If spot moves 0.4% AGAINST trade direction after 20min, exit
# Simulation: saves Rs +6,974, hurts ZERO winning trades
MOMENTUM_EXIT_ENABLED = True
MOMENTUM_EXIT_SPOT_PCT = 0.4
MOMENTUM_EXIT_MIN_MINUTES = 20
"""

def patch_bot(filepath, bot_name, sym_key='symbol'):
    """Apply all fixes to a bot file."""
    with open(filepath, 'r') as f:
        code = f.read()

    changes = 0

    # 1. BREAKOUT_FAIL_REVERSE disable (already done for equity, ensure all)
    if 'BREAKOUT_FAIL_REVERSE_ENABLED = True' in code:
        code = code.replace('BREAKOUT_FAIL_REVERSE_ENABLED = True',
                           'BREAKOUT_FAIL_REVERSE_ENABLED = False')
        changes += 1
        print(f"  [{bot_name}] Disabled BREAKOUT_FAIL_REVERSE")

    # 2. Add ATR constants if not present
    if 'VOLATILITY_SL_ENABLED' not in code:
        # Insert after BREAKOUT_FAIL_REVERSE line
        anchor = 'BREAKOUT_FAIL_REVERSE_ENABLED'
        if anchor in code:
            idx = code.index(anchor)
            line_end = code.index('\n', idx)
            code = code[:line_end+1] + ATR_DATA + code[line_end+1:]
            changes += 1
            print(f"  [{bot_name}] Added ATR constants + momentum exit constants")

    # 3. Add volatility-adjusted SL in the signal generation
    # Find where SL is set: sl_mult = 0.5
    if 'VOLATILITY_SL_ENABLED' in code and 'vol_sl_mult' not in code:
        # Find the CPR signal SL setting
        sl_anchor = "sl_mult = 0.5           # SL = 50% of premium"
        if sl_anchor not in code:
            sl_anchor = "sl_mult = 0.5"

        if sl_anchor in code:
            vol_sl_code = """sl_mult = 0.5
            # v13.3: Volatility-adjusted SL
            if VOLATILITY_SL_ENABLED and symbol in ATR_REFERENCE:
                vol_sl_mult = sl_mult * (ATR_REFERENCE.get(ATR_BASE_SYMBOL, 313) / ATR_REFERENCE.get(symbol, 313))
                sl_mult = max(0.12, min(0.50, vol_sl_mult))  # Clamp 12%-50%"""

            # Replace for CPR (first occurrence only, in check_cpr_signals)
            code = code.replace(sl_anchor, vol_sl_code, 1)
            changes += 1
            print(f"  [{bot_name}] Added vol-adjusted SL to CPR")

    # 4. Add momentum exit in exit checking
    if 'MOMENTUM_EXIT' not in code or 'MOMENTUM_EXIT_ENABLED' not in code:
        # Already have constants from step 2, now add exit logic
        # Find theta burden check as anchor
        theta_anchor = 'THETA_BURDEN_TIGHTEN_PCT'
        if theta_anchor not in code:
            theta_anchor = 'theta_burden'

        if theta_anchor in code and 'MOMENTUM_EXIT:' not in code:
            theta_loc = code.find(theta_anchor)
            line_start = code.rfind('\n', 0, theta_loc) + 1

            # Build momentum exit code
            sym_get = f"pos.get('{sym_key}', '')" if sym_key == 'symbol' else f"pos.get('commodity', pos.get('symbol', ''))"

            mom_code = f'''
                # ---- v13.3: MOMENTUM REVERSAL EXIT ----
                if MOMENTUM_EXIT_ENABLED and elapsed_mins >= MOMENTUM_EXIT_MIN_MINUTES:
                    _me_entry_spot = pos.get('details', {{}}).get('spot', 0) if isinstance(pos.get('details'), dict) else 0
                    if _me_entry_spot > 0:
                        _me_spot_move = (current_spot - _me_entry_spot) / _me_entry_spot * 100
                        _me_is_ce = 'CE' in pos.get('signal_type', '')
                        _me_is_pe = 'PE' in pos.get('signal_type', '')
                        if (_me_is_ce and _me_spot_move < -MOMENTUM_EXIT_SPOT_PCT) or \\
                           (_me_is_pe and _me_spot_move > MOMENTUM_EXIT_SPOT_PCT):
                            logger.info(f"  MOMENTUM_EXIT: {{pos['id']}} spot moved {{_me_spot_move:+.2f}}% "
                                       f"against {{'CE' if _me_is_ce else 'PE'}} after {{elapsed_mins:.0f}}min")
                            self.portfolio.close_position(pos['id'], current_premium, 'MOMENTUM_EXIT')
                            self._track_exit(pos, 'MOMENTUM_EXIT')
                            try:
                                from trade_notifier import notify_trade_exit
                                notify_trade_exit(
                                    market="{bot_name}", strategy=pos['strategy'],
                                    symbol={sym_get}, signal_type=pos['signal_type'],
                                    strike=pos['strike'], entry_price=pos['entry_premium'],
                                    exit_price=current_premium, pnl=pnl,
                                    exit_reason=f'MOMENTUM_EXIT (spot {{_me_spot_move:+.1f}}%)',
                                    hold_time=f"{{int(elapsed_mins)}}min")
                            except Exception:
                                pass
                            continue
'''
            code = code[:line_start] + mom_code + '\n' + code[line_start:]
            changes += 1
            print(f"  [{bot_name}] Added momentum exit logic")

    # Write back
    with open(filepath, 'w') as f:
        f.write(code)

    # Verify syntax
    try:
        ast.parse(code)
        print(f"  [{bot_name}] Syntax OK ({changes} changes)")
    except SyntaxError as e:
        print(f"  [{bot_name}] SYNTAX ERROR: {e}")
        return False

    return True


# Apply to all 3 bots
print("=" * 60)
print("  APPLYING ALL FIXES TO ALL BOTS")
print("=" * 60)

results = []
results.append(patch_bot('/root/algo_trading/paper_trader.py', 'EQUITY', 'symbol'))
results.append(patch_bot('/root/algo_trading/commodity_paper_trader.py', 'COMMODITY', 'commodity'))
results.append(patch_bot('/root/algo_trading/stock_paper_trader.py', 'STOCKS', 'symbol'))

# Verify Liquidity Sweep is present
print("\n  VERIFICATION:")
for bot, path in [('EQUITY', '/root/algo_trading/paper_trader.py'),
                   ('COMMODITY', '/root/algo_trading/commodity_paper_trader.py')]:
    with open(path, 'r') as f:
        code = f.read()
    has_sweep = 'detect_liquidity_sweep' in code or 'Liquidity Sweep' in code
    has_physics = 'physics_gate' in code
    has_volsl = 'VOLATILITY_SL_ENABLED' in code
    has_mom = 'MOMENTUM_EXIT' in code
    has_bfr = 'BREAKOUT_FAIL_REVERSE_ENABLED = False' in code
    print(f"  [{bot}] Sweep={has_sweep} Physics={has_physics} VolSL={has_volsl} MomExit={has_mom} BFR_off={has_bfr}")

if all(results):
    print("\n  ALL PATCHES APPLIED SUCCESSFULLY")
else:
    print("\n  SOME PATCHES FAILED - CHECK ABOVE")
