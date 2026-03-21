#!/usr/bin/env python3
"""Audit all ENABLED flags vs actual usage in all 3 bots.
Finds bugs where a flag is defined but never checked before the action."""
import re

print("=" * 90)
print("  COMPREHENSIVE FLAG vs USAGE AUDIT — ALL 3 BOTS")
print("=" * 90)

bots = [
    ("/root/algo_trading/paper_trader.py", "EQUITY"),
    ("/root/algo_trading/commodity_paper_trader.py", "COMMODITY"),
    ("/root/algo_trading/stock_paper_trader.py", "STOCK"),
]

all_issues = []

for filepath, market in bots:
    try:
        with open(filepath, "r") as f:
            code = f.read()
            lines = code.split("\n")
    except Exception as e:
        print(f"\n  {market}: Error reading {filepath}: {e}")
        continue

    print(f"\n{'='*90}")
    print(f"  {market} BOT")
    print(f"{'='*90}")

    # 1. Find all _ENABLED flags
    enabled_flags = {}
    for i, line in enumerate(lines):
        m = re.match(r'^(\w+_ENABLED)\s*=\s*(\w+)', line.strip())
        if m:
            enabled_flags[m.group(1)] = (i+1, m.group(2))

    print(f"\n  ENABLED FLAGS:")
    for flag, (line_no, value) in sorted(enabled_flags.items()):
        # Check if flag is used in any if-condition
        uses = [i+1 for i, l in enumerate(lines) if flag in l and ("if " in l or "elif " in l)]
        status = "OK" if uses else "BUG: NEVER CHECKED!"
        marker = " <<<" if not uses else ""
        print(f"    Line {line_no:>5}: {flag} = {value:>6} | if-checks: {len(uses)} | {status}{marker}")
        if not uses and value == "False":
            all_issues.append(f"{market}: {flag}=False but never checked in if-condition")

    # 2. Find all exit reasons and check if they have proper gates
    print(f"\n  EXIT REASONS (close_position calls):")
    exit_pattern = re.compile(r"close_position\([^)]*'(\w+)'\s*\)")
    all_exits = {}
    for i, line in enumerate(lines):
        for m in exit_pattern.finditer(line):
            reason = m.group(1)
            if reason not in all_exits:
                all_exits[reason] = []
            all_exits[reason].append(i+1)

    for reason in sorted(all_exits.keys()):
        line_nos = all_exits[reason]
        # Check if there's an ENABLED flag for this exit type
        flag_name = reason + "_ENABLED"
        # Also check shorter versions
        parts = reason.split("_")
        possible_flags = [reason + "_ENABLED", "_".join(parts[:-1]) + "_ENABLED" if len(parts) > 1 else ""]

        has_flag = any(f in enabled_flags for f in possible_flags if f)
        flag_checked = False
        if has_flag:
            matching_flag = [f for f in possible_flags if f in enabled_flags][0]
            # Check if the flag is in an if-condition NEAR the close_position line
            for ln in line_nos:
                # Look 20 lines above for the flag check
                nearby = lines[max(0,ln-20):ln]
                if any(matching_flag in l and "if " in l for l in nearby):
                    flag_checked = True
                    break

        status = ""
        if has_flag and not flag_checked:
            status = " <<< BUG: has flag but not checked before exit!"
            all_issues.append(f"{market}: {reason} exit at line(s) {line_nos} — flag exists but not checked")
        elif has_flag and flag_checked:
            status = " (gated)"

        print(f"    {reason:<30} at lines {line_nos}{status}")

    # 3. Check for dead code — variables defined but never used
    print(f"\n  THRESHOLD CONSTANTS (check for dead variables):")
    const_pattern = re.compile(r'^([A-Z][A-Z_0-9]+)\s*=\s*(.+?)(?:\s*#.*)?$')
    for i, line in enumerate(lines[:250]):  # Check first 250 lines (constants section)
        m = const_pattern.match(line.strip())
        if m:
            const_name = m.group(1)
            const_value = m.group(2).strip()
            # Count usage (excluding definition line)
            uses = sum(1 for j, l in enumerate(lines) if const_name in l and j != i)
            if uses <= 1:  # Only defined, never or barely used
                print(f"    Line {i+1:>5}: {const_name} = {const_value[:30]} | used {uses}x {'<<< DEAD?' if uses == 0 else ''}")

# Summary
print(f"\n{'='*90}")
print(f"  ISSUES FOUND: {len(all_issues)}")
print(f"{'='*90}")
for issue in all_issues:
    print(f"  - {issue}")

if not all_issues:
    print("  No issues found! All flags properly gate their actions.")
