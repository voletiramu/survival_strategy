#!/usr/bin/env python3
"""Fix commodity sweep: self.engine might not exist if called from engine itself."""

with open("/root/algo_trading/commodity_paper_trader.py", "r") as f:
    code = f.read()

# Replace self.engine.select_strike_live with safe version
# Only in the sweep section (line ~2955)
old = "_opt = self.engine.select_strike_live(commodity, sw_spot, _si, T, RISK_FREE_RATE, 0.30, sw['direction'], MCX_MIN_PREMIUM_BUY)"
new = """_eng = getattr(self, 'engine', self)
                                _opt = _eng.select_strike_live(commodity, sw_spot, _si, T, RISK_FREE_RATE, 0.30, sw['direction'], MCX_MIN_PREMIUM_BUY)"""

code = code.replace(old, new)

import ast
ast.parse(code)
with open("/root/algo_trading/commodity_paper_trader.py", "w") as f:
    f.write(code)
print("Fixed commodity sweep: getattr(self, 'engine', self)")
