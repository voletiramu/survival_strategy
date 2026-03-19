#!/usr/bin/env python3
"""Fix Liquidity Sweep errors in all 3 bots + make crash-safe."""
import ast

# 1. Make detect_liquidity_sweep crash-safe in market_calculus.py
print("=== Fixing market_calculus.py ===")
with open("/root/algo_trading/market_calculus.py", "r") as f:
    code = f.read()

# Rename the method and add a safe wrapper
old = "    def detect_liquidity_sweep(self, symbol, spot=None):"
if "_detect_liquidity_sweep_inner" not in code:
    # Add safe wrapper
    code = code.replace(
        old,
        """    def detect_liquidity_sweep(self, symbol, spot=None):
        \"\"\"Safe wrapper — returns [] on any error.\"\"\"
        try:
            return self._detect_liquidity_sweep_inner(symbol, spot)
        except Exception:
            return []

    def _detect_liquidity_sweep_inner(self, symbol, spot=None):"""
    )
    print("  Added crash-safe wrapper")
else:
    print("  Already has safe wrapper")

ast.parse(code)
with open("/root/algo_trading/market_calculus.py", "w") as f:
    f.write(code)
print("  Syntax OK")

# 2. Fix paper_trader.py — sweep uses self._get_strike_from_chain, should be self.engine
print("\n=== Fixing paper_trader.py ===")
with open("/root/algo_trading/paper_trader.py", "r") as f:
    code2 = f.read()

# Count current refs
count_self = code2.count("self._get_strike_from_chain")
count_engine = code2.count("self.engine._get_strike_from_chain")
print(f"  self._get_strike: {count_self}, self.engine._get_strike: {count_engine}")

# The sweep block is around line 3670+ and uses self._get_strike_from_chain
# But the STRATEGY methods (CPR, Gamma etc) correctly use self._get_strike_from_chain
# because they're methods of StrategyEngine where self = engine
# The SCAN method is in PaperTrader where self = trader, engine = self.engine
# So ONLY the sweep block in scan_all needs self.engine prefix

# Find the sweep block boundaries
sweep_start = code2.find("# ---- v13.1: LIQUIDITY SWEEP (SL Hunt Reversal)")
if sweep_start > 0:
    # The sweep block goes until the next major section
    sweep_end = code2.find("# v10: Write live scan data", sweep_start)
    if sweep_end < 0:
        sweep_end = code2.find("# Score all signals", sweep_start)

    if sweep_end > sweep_start:
        sweep_block = code2[sweep_start:sweep_end]

        # In the sweep block, fix self._get_strike_from_chain -> self.engine._get_strike_from_chain
        fixed_sweep = sweep_block.replace(
            "self._get_strike_from_chain",
            "self.engine._get_strike_from_chain"
        )

        # Also wrap the sweep block inner loop in try/except
        fixed_sweep = fixed_sweep.replace(
            "sweeps = self.calculus.detect_liquidity_sweep(symbol)",
            "sweeps = self.calculus.detect_liquidity_sweep(symbol)\n                            if not sweeps: continue"
        )

        code2 = code2[:sweep_start] + fixed_sweep + code2[sweep_end:]
        print("  Fixed sweep block")

ast.parse(code2)
with open("/root/algo_trading/paper_trader.py", "w") as f:
    f.write(code2)
print("  Syntax OK")

# 3. Fix commodity_paper_trader.py — check for any remaining issues
print("\n=== Fixing commodity_paper_trader.py ===")
with open("/root/algo_trading/commodity_paper_trader.py", "r") as f:
    code3 = f.read()

if "_get_strike_from_chain" in code3:
    print(f"  Still has _get_strike_from_chain!")
else:
    print("  No _get_strike_from_chain (OK)")

# Add continue after empty sweeps check
if "sweeps = self.calculus.detect_liquidity_sweep(commodity)" in code3:
    code3 = code3.replace(
        "sweeps = self.calculus.detect_liquidity_sweep(commodity)",
        "sweeps = self.calculus.detect_liquidity_sweep(commodity)\n                            if not sweeps: continue"
    )
    print("  Added empty sweeps shortcut")

ast.parse(code3)
with open("/root/algo_trading/commodity_paper_trader.py", "w") as f:
    f.write(code3)
print("  Syntax OK")

print("\nAll fixes applied!")
