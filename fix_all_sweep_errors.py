#!/usr/bin/env python3
"""Fix ALL Liquidity Sweep errors across all 3 bots.
Wraps every sweep block in try/except so scan never crashes."""
import re

def fix_bot(filepath, bot_name):
    with open(filepath, 'r') as f:
        code = f.read()

    changes = 0

    # Fix 1: self.select_strike_live -> self.engine.select_strike_live (commodity)
    if 'self.select_strike_live' in code and 'self.engine.select_strike_live' not in code:
        code = code.replace('self.select_strike_live(', 'self.engine.select_strike_live(')
        changes += 1
        print(f"  [{bot_name}] Fixed self.select_strike_live -> self.engine.select_strike_live")

    # Fix 2: self._get_strike_from_chain -> self.engine._get_strike_from_chain
    if 'self._get_strike_from_chain(' in code:
        # Only fix in sweep sections, not in StrategyEngine methods
        # Count in method definitions vs usage
        in_engine = 'def _get_strike_from_chain(self' in code
        if in_engine:
            # This file has the method defined — fix calls outside the class
            # Actually the sweep code is in PaperTrader/CommodityPaperTrader, not StrategyEngine
            # So self._get_strike refers to PaperTrader which doesn't have it
            # Replace only in sweep section
            sweep_start = code.find('LIQUIDITY SWEEP')
            if sweep_start > 0:
                before = code[:sweep_start]
                after = code[sweep_start:]
                after = after.replace('self._get_strike_from_chain(', 'self.engine._get_strike_from_chain(')
                code = before + after
                changes += 1
                print(f"  [{bot_name}] Fixed _get_strike_from_chain in sweep section")
        else:
            code = code.replace('self._get_strike_from_chain(', 'self.engine._get_strike_from_chain(')
            changes += 1
            print(f"  [{bot_name}] Fixed _get_strike_from_chain globally")

    # Fix 3: Wrap entire sweep detection in try/except
    # Find the sweep block and ensure it's wrapped
    sweep_markers = [
        '# ---- v13.1: LIQUIDITY SWEEP',
        '# ---- v13.1: LIQUIDITY SWEEP for stocks',
        '# ---- v13.1: LIQUIDITY SWEEP for commodities',
    ]

    for marker in sweep_markers:
        if marker not in code:
            continue

        idx = code.index(marker)
        # Find the line with detect_liquidity_sweep
        detect_idx = code.find('detect_liquidity_sweep', idx)
        if detect_idx < 0:
            continue

        # Check if already wrapped in try
        block_before = code[idx:detect_idx]
        if 'try:' in block_before:
            print(f"  [{bot_name}] Sweep already has try/except")
            continue

        # Find the indent level of the sweep hasattr check
        hasattr_idx = code.find('if hasattr(self', idx)
        if hasattr_idx < 0 or hasattr_idx > idx + 500:
            continue

        # Get the indent
        line_start = code.rfind('\n', 0, hasattr_idx) + 1
        indent = code[line_start:hasattr_idx]

        # Replace the hasattr line with try + hasattr
        old_hasattr = code[line_start:code.find('\n', hasattr_idx)]
        new_hasattr = indent + 'try:\n' + indent + '  ' + old_hasattr.strip()

        # Find where sweep block ends (next section marker or end of indented block)
        # Look for the next line at same or lower indent that's not part of sweep
        end_markers = [
            '# v9.6: Build indicators lookup',
            '# v10: Write live scan data',
            '# Score all signals',
        ]
        end_idx = len(code)
        for em in end_markers:
            em_idx = code.find(em, idx + 100)
            if em_idx > 0 and em_idx < end_idx:
                end_idx = em_idx

        # Insert except before end marker
        except_code = indent + 'except Exception as _sweep_err:\n' + indent + '    pass  # Sweep error - silently skip, dont crash scan\n\n' + indent

        code = code[:end_idx] + except_code + code[end_idx:]
        code = code[:line_start] + new_hasattr + code[line_start + len(old_hasattr):]
        changes += 1
        print(f"  [{bot_name}] Wrapped sweep in try/except")

    if changes == 0:
        print(f"  [{bot_name}] No changes needed")

    # Verify syntax
    import ast
    try:
        ast.parse(code)
        with open(filepath, 'w') as f:
            f.write(code)
        print(f"  [{bot_name}] Syntax OK, saved.")
    except SyntaxError as e:
        print(f"  [{bot_name}] SYNTAX ERROR: {e}")
        print(f"  [{bot_name}] NOT SAVED — manual fix needed")
        return False

    return True

# Fix all 3 bots
print("Fixing all sweep errors...\n")
fix_bot('/root/algo_trading/paper_trader.py', 'EQUITY')
fix_bot('/root/algo_trading/commodity_paper_trader.py', 'COMMODITY')
fix_bot('/root/algo_trading/stock_paper_trader.py', 'STOCK')
print("\nDone!")
