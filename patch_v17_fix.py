#!/usr/bin/env python3
"""Fix the misplaced Wave constants in paper_trader.py."""

PAPER_TRADER = "/root/algo_trading/paper_trader.py"

with open(PAPER_TRADER) as fh:
    code = fh.read()

# Remove the misplaced Wave constants block (inserted in wrong location)
misplaced = """
# --- Wave Extractor v3 constants (v17) -------------------------------------------
# Gap-based entry: BUY CE when spot rises by GAP points, BUY PE when drops
WAVE_GAP_PTS = {'NIFTY': 75, 'BANKNIFTY': 150, 'SENSEX': 300}
WAVE_VWAP_FILTER = {'NIFTY': True, 'BANKNIFTY': False, 'SENSEX': True}
WAVE_SL_MULT = {'NIFTY': 0.70, 'BANKNIFTY': 0.65, 'SENSEX': 0.55}
WAVE_TARGET_MULT = 1.40
WAVE_COOLOFF_BARS = 5
WAVE_MAX_DAILY_TRADES = 10
"""

# Also try with unicode dashes
misplaced_alt = misplaced.replace("---", "\u2500\u2500\u2500")

for mp in [misplaced, misplaced_alt]:
    if mp in code:
        code = code.replace(mp, "\n", 1)
        print("Removed misplaced Wave constants")
        break
else:
    # Try finding by content
    lines = code.split("\n")
    new_lines = []
    skip = False
    removed = 0
    for i, line in enumerate(lines):
        if "Wave Extractor v3 constants" in line:
            skip = True
            removed += 1
            continue
        if skip and line.strip() == "":
            skip = False
            continue
        if skip and (line.startswith("WAVE_") or line.startswith("# Gap-based")):
            removed += 1
            continue
        if skip and not line.startswith("WAVE_") and not line.startswith("#"):
            skip = False
        if not skip:
            new_lines.append(line)
    if removed > 0:
        code = "\n".join(new_lines)
        print(f"Removed {removed} misplaced lines")

# Now insert Wave constants at the TOP-LEVEL config section (after existing strategy constants)
# Find a good insertion point - after the BROKERAGE/cost constants or after THETA_BURDEN
wave_block = """
# Wave Extractor v3 constants (v17)
WAVE_GAP_PTS = {'NIFTY': 75, 'BANKNIFTY': 150, 'SENSEX': 300}
WAVE_VWAP_FILTER = {'NIFTY': True, 'BANKNIFTY': False, 'SENSEX': True}
WAVE_SL_MULT = {'NIFTY': 0.70, 'BANKNIFTY': 0.65, 'SENSEX': 0.55}
WAVE_TARGET_MULT = 1.40
"""

if "WAVE_GAP_PTS" not in code:
    # Insert after GST_RATE line (top-level cost section)
    marker = "GST_RATE = 0.18"
    if marker in code:
        code = code.replace(marker, marker + "\n" + wave_block, 1)
        print("Inserted Wave constants after GST_RATE")
    else:
        # Fallback: after MIN_PREMIUM_BUY
        for possible_marker in ["MIN_PREMIUM_BUY", "RISK_FREE_RATE = 0.065"]:
            if possible_marker in code:
                # Find the line
                lines = code.split("\n")
                for i, line in enumerate(lines):
                    if possible_marker in line and "=" in line and not line.strip().startswith("#"):
                        lines.insert(i + 1, wave_block)
                        code = "\n".join(lines)
                        print(f"Inserted Wave constants after {possible_marker}")
                        break
                break

with open(PAPER_TRADER, "w") as fh:
    fh.write(code)

# Verify syntax
import py_compile
try:
    py_compile.compile(PAPER_TRADER, doraise=True)
    print("\npaper_trader.py: Syntax OK")
except py_compile.PyCompileError as e:
    print(f"\nSyntax error: {e}")
    print("Checking around the error...")
