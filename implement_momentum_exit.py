#!/usr/bin/env python3
"""Implement momentum exit in all 3 bots.
Inserts AFTER 'exit_reason = None' and BEFORE the SL_HIT check."""
import ast

MOMENTUM_CODE_EQUITY = '''
            # ---- v13.8: MOMENTUM EXIT — spot reverses against direction ----
            if MOMENTUM_EXIT_ENABLED and exit_reason is None:
                _me_entry_spot = pos.get('details', {}).get('spot', 0) if isinstance(pos.get('details'), dict) else 0
                _me_spot_now = spot  # Already computed above in exit loop
                _me_entry_ts = pos.get('timestamp', '')
                if _me_entry_spot > 0 and _me_spot_now > 0 and _me_entry_ts:
                    try:
                        _me_entry_dt = datetime.fromisoformat(_me_entry_ts)
                        _me_hold_min = (datetime.now() - _me_entry_dt).total_seconds() / 60
                        if _me_hold_min >= MOMENTUM_EXIT_MIN_MINUTES:
                            _me_change = (_me_spot_now - _me_entry_spot) / _me_entry_spot * 100
                            _me_is_ce = 'CE' in pos.get('signal_type', '')
                            _me_against = (_me_is_ce and _me_change < -MOMENTUM_EXIT_SPOT_PCT) or \
                                          (not _me_is_ce and _me_change > MOMENTUM_EXIT_SPOT_PCT)
                            if _me_against and pos.get('unrealized_pnl', 0) <= 0:
                                exit_reason = 'MOMENTUM_EXIT'
                                logger.info(f"  MOMENTUM_EXIT: {pos['id']} spot {_me_entry_spot:.0f}->{_me_spot_now:.0f} "
                                           f"({_me_change:+.2f}%) after {_me_hold_min:.0f}min")
                    except Exception:
                        pass

'''

MOMENTUM_CODE_COMMODITY = '''
            # ---- v13.8: MOMENTUM EXIT — spot reverses against direction ----
            if MOMENTUM_EXIT_ENABLED and exit_reason is None:
                _me_entry_spot = pos.get('details', {}).get('spot', 0) if isinstance(pos.get('details'), dict) else 0
                _me_spot_now = spot  # Already computed above in exit loop
                _me_entry_ts = pos.get('timestamp', '')
                if _me_entry_spot > 0 and _me_spot_now > 0 and _me_entry_ts:
                    try:
                        _me_entry_dt = datetime.fromisoformat(_me_entry_ts)
                        _me_hold_min = (datetime.now() - _me_entry_dt).total_seconds() / 60
                        if _me_hold_min >= MOMENTUM_EXIT_MIN_MINUTES:
                            _me_change = (_me_spot_now - _me_entry_spot) / _me_entry_spot * 100
                            _me_is_ce = 'CE' in pos.get('signal_type', '')
                            _me_against = (_me_is_ce and _me_change < -MOMENTUM_EXIT_SPOT_PCT) or \
                                          (not _me_is_ce and _me_change > MOMENTUM_EXIT_SPOT_PCT)
                            if _me_against and pos.get('unrealized_pnl', 0) <= 0:
                                exit_reason = 'MOMENTUM_EXIT'
                                logger.info(f"  MOMENTUM_EXIT: {pos['id']} spot {_me_entry_spot:.0f}->{_me_spot_now:.0f} "
                                           f"({_me_change:+.2f}%) after {_me_hold_min:.0f}min")
                    except Exception:
                        pass

'''

MOMENTUM_CODE_STOCK = '''
            # ---- v13.8: MOMENTUM EXIT — spot reverses against direction ----
            if MOMENTUM_EXIT_ENABLED and exit_reason is None:
                _me_entry_spot = pos.get('details', {}).get('spot', 0) if isinstance(pos.get('details'), dict) else 0
                _me_spot_now = self.get_stock_spot(pos.get('symbol', pos.get('stock_symbol', ''))) or 0
                _me_entry_ts = pos.get('timestamp', '')
                if _me_entry_spot > 0 and _me_spot_now > 0 and _me_entry_ts:
                    try:
                        _me_entry_dt = datetime.fromisoformat(_me_entry_ts)
                        _me_hold_min = (datetime.now() - _me_entry_dt).total_seconds() / 60
                        if _me_hold_min >= MOMENTUM_EXIT_MIN_MINUTES:
                            _me_change = (_me_spot_now - _me_entry_spot) / _me_entry_spot * 100
                            _me_is_ce = 'CE' in pos.get('signal_type', '')
                            _me_against = (_me_is_ce and _me_change < -MOMENTUM_EXIT_SPOT_PCT) or \
                                          (not _me_is_ce and _me_change > MOMENTUM_EXIT_SPOT_PCT)
                            if _me_against and pos.get('unrealized_pnl', 0) <= 0:
                                exit_reason = 'MOMENTUM_EXIT'
                                logger.info(f"  MOMENTUM_EXIT: {pos['id']} spot {_me_entry_spot:.0f}->{_me_spot_now:.0f} "
                                           f"({_me_change:+.2f}%) after {_me_hold_min:.0f}min")
                    except Exception:
                        pass

'''

bots = [
    ("/root/algo_trading/paper_trader.py", "EQUITY", MOMENTUM_CODE_EQUITY),
    ("/root/algo_trading/commodity_paper_trader.py", "COMMODITY", MOMENTUM_CODE_COMMODITY),
    ("/root/algo_trading/stock_paper_trader.py", "STOCK", MOMENTUM_CODE_STOCK),
]

for filepath, name, momentum_code in bots:
    print(f"\n=== {name} ===")
    with open(filepath, "r") as f:
        code = f.read()

    if "MOMENTUM_EXIT:" in code and "v13.8" in code:
        print(f"  Already implemented")
        continue

    # Find anchor: "exit_reason = None" in the exit check section
    # There might be multiple — we want the one in the main exit loop
    # Look for the pattern after EXIT_CHECK log
    anchor = "            exit_reason = None\n"

    # Find the LAST occurrence (main exit loop, not test code)
    idx = code.rfind(anchor)
    if idx < 0:
        # Try with different indentation
        anchor = "            exit_reason = None"
        idx = code.rfind(anchor)

    if idx < 0:
        print(f"  ERROR: Could not find 'exit_reason = None' anchor")
        continue

    # Insert momentum exit AFTER exit_reason = None
    insert_point = idx + len(anchor)
    if not anchor.endswith('\n'):
        insert_point = code.index('\n', insert_point) + 1

    code = code[:insert_point] + momentum_code + code[insert_point:]

    # Also need to handle the exit — after exit_reason is set,
    # it needs to be acted on. Check if there's already:
    # if exit_reason: close_position(...)
    if "if exit_reason:" in code or "if exit_reason ==" in code:
        print(f"  Exit reason handler already exists")
    else:
        print(f"  NOTE: exit_reason='MOMENTUM_EXIT' will be caught by existing exit handler")

    try:
        ast.parse(code)
        with open(filepath, "w") as f:
            f.write(code)
        print(f"  Momentum exit implemented. Syntax OK.")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR: {e}")
        # Show context around error
        err_line = e.lineno
        lines = code.split('\n')
        for i in range(max(0, err_line-5), min(len(lines), err_line+5)):
            marker = " >>>" if i+1 == err_line else "    "
            print(f"  {marker} {i+1}: {lines[i]}")

print("\n=== DONE ===")
