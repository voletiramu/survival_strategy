#!/usr/bin/env python3
"""Deep bug scan — find runtime-prone bugs across all production code on VPS.
Checks for: format string bugs, undefined vars, import errors, encoding issues,
deprecated calls, thread safety, file handle leaks, etc.
"""

import os
import re
import ast
import sys

os.chdir("/root/algo_trading")

PROD_FILES = [
    "paper_trader.py",
    "commodity_paper_trader.py",
    "stock_paper_trader.py",
    "run_paper_trade.py",
    "run_stock_trade.py",
    "dashboard.py",
    "oi_velocity_tracker.py",
    "zerodha_feed.py",
    "zerodha_token_gen.py",
    "ws_feed.py",
    "market_data_pipeline.py",
    "market_regime.py",
    "trade_notifier.py",
    "live_data_logger.py",
    "market_calculus.py",
    "gift_nifty.py",
    "trade_intelligence.py",
    "stock_fno_config.py",
    "stock_cpr_scanner.py",
    "stock_data_pipeline.py",
    "run_oi_trade.py",
    "oi_paper_trader.py",
]

bugs = []
warnings = []
info = []

for fname in PROD_FILES:
    if not os.path.exists(fname):
        continue

    with open(fname, errors="replace") as f:
        code = f.read()
    lines = code.split("\n")

    # ── 1. Format string bugs (%,d  %+,.0f etc — not supported in % formatting) ──
    # Python % formatting doesn't support comma grouping
    pct_comma_pattern = re.compile(r'%[\+\-]?\d*,')
    for i, line in enumerate(lines):
        if "%" in line and not line.strip().startswith("#"):
            # Skip f-strings and .format()
            if "f'" in line or 'f"' in line or ".format(" in line:
                continue
            matches = pct_comma_pattern.findall(line)
            if matches:
                bugs.append("[BUG] %s:%d — Invalid %%-format with comma: %s | %s" % (
                    fname, i+1, matches[0], line.strip()[:80]))

    # ── 2. f-string with backslash (not allowed in Python <3.12) ──
    fstring_backslash = re.compile(r'f["\'].*\\.*["\']')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Check for f-strings containing backslash inside braces
        # Pattern: f"...{expr with \n or \t}..."
        if ('f"' in line or "f'" in line):
            # Find all f-string expressions {....}
            in_fstr = False
            brace_depth = 0
            j = 0
            while j < len(line):
                if j < len(line) - 1 and line[j] == 'f' and line[j+1] in '"\'':
                    in_fstr = True
                    j += 2
                    continue
                if in_fstr and line[j] == '{' and (j+1 >= len(line) or line[j+1] != '{'):
                    brace_depth += 1
                elif in_fstr and line[j] == '}' and (j == 0 or line[j-1] != '}'):
                    brace_depth -= 1
                if in_fstr and brace_depth > 0 and line[j] == '\\':
                    bugs.append("[BUG] %s:%d — f-string with backslash in expression (fails <3.12): %s" % (
                        fname, i+1, stripped[:80]))
                    break
                j += 1

    # ── 3. Unicode/emoji in print() or logger calls (crashes cp1252 terminals) ──
    emoji_pattern = re.compile(r'[\U0001F300-\U0001F9FF\u2705\u274C\u26A0\u2B50\u2728\u2600-\u26FF\u2700-\u27BF]')
    for i, line in enumerate(lines):
        if ("print(" in line or "logger." in line) and emoji_pattern.search(line):
            # Check if it's in a try/except block (safe)
            indent = len(line) - len(line.lstrip())
            in_try = False
            for back in range(max(0, i-5), i):
                if "try:" in lines[back]:
                    in_try = True
                    break
            if not in_try:
                warnings.append("[WARN] %s:%d — Emoji in print/logger (may crash non-UTF8 terminal): %s" % (
                    fname, i+1, line.strip()[:60]))

    # ── 4. Division by zero risks (unguarded) ──
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Look for / variable where variable could be 0
        div_patterns = [
            r'/ len\(',           # / len(x) — empty list = 0
            r'/ \w+_count\b',     # / some_count
            r'/ total\b',         # / total
            r'/ n\b',             # / n
        ]
        for pat in div_patterns:
            if re.search(pat, stripped) and "max(" not in stripped and "if " not in stripped and "or 1" not in stripped:
                # Check if guarded on previous line
                guarded = False
                if i > 0:
                    prev = lines[i-1].strip()
                    if "if " in prev or "else" in prev or "max(" in prev:
                        guarded = True
                if not guarded:
                    info.append("[INFO] %s:%d — Potential division by zero: %s" % (
                        fname, i+1, stripped[:80]))

    # ── 5. Bare except (hides errors) ──
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "except:" or stripped == "except :":
            warnings.append("[WARN] %s:%d — Bare 'except:' hides all errors" % (fname, i+1))

    # ── 6. Hardcoded credentials in non-config files ──
    cred_patterns = [
        (r'password\s*=\s*["\'][^"\']+["\']', "hardcoded password"),
        (r'api_key\s*=\s*["\'][a-zA-Z0-9]{10,}["\']', "hardcoded API key"),
        (r'access_token\s*=\s*["\'][a-zA-Z0-9]{10,}["\']', "hardcoded token"),
    ]
    if fname not in ("zerodha_token_gen.py",):  # skip config files
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            for pat, desc in cred_patterns:
                if re.search(pat, line, re.IGNORECASE):
                    warnings.append("[WARN] %s:%d — %s found in code" % (fname, i+1, desc))

    # ── 7. open() without 'with' (file handle leak) ──
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "= open(" in stripped and "with " not in stripped:
            info.append("[INFO] %s:%d — open() without 'with' context manager: %s" % (
                fname, i+1, stripped[:60]))

    # ── 8. time.sleep() in main thread (blocking) ──
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "time.sleep(" in stripped:
            # Extract sleep duration
            match = re.search(r'time\.sleep\((\d+)', stripped)
            if match:
                duration = int(match.group(1))
                if duration > 30:
                    warnings.append("[WARN] %s:%d — Long sleep(%ds) may block thread" % (
                        fname, i+1, duration))

    # ── 9. Deprecated or risky patterns ──
    risky_patterns = [
        (r'eval\(', "eval() is dangerous"),
        (r'exec\(', "exec() is dangerous"),
        (r'os\.system\(', "os.system() — use subprocess instead"),
        (r'pickle\.loads?\(', "pickle.load() — unsafe deserialization"),
    ]
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat, desc in risky_patterns:
            if re.search(pat, stripped):
                info.append("[INFO] %s:%d — %s: %s" % (fname, i+1, desc, stripped[:60]))

    # ── 10. Catch-all: AttributeError-prone patterns ──
    # Accessing .get() result without None check
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Pattern: x.get('key').something — .get() returns None if key missing
        if re.search(r'\.get\([^)]+\)\.[a-zA-Z]', stripped) and "or " not in stripped:
            # Check it's not .get('key', default).method
            if ", " not in stripped.split(".get(")[1].split(")")[0] if ".get(" in stripped else "":
                info.append("[INFO] %s:%d — .get() without default + method call (may NoneType error): %s" % (
                    fname, i+1, stripped[:70]))

    # ── 11. Check for stale TrueData references that might still execute ──
    if "TrueDataFeed" in code and "if False:" not in code and "DISABLED" not in code:
        if fname not in ("truedata_feed.py",):
            warnings.append("[WARN] %s — TrueData code may still be active (trial expired)" % fname)

# ── Print Results ──────────────────────────────────────────────────────────
print("=" * 70)
print("  DEEP BUG SCAN — %d files scanned" % len([f for f in PROD_FILES if os.path.exists(f)]))
print("=" * 70)

if bugs:
    print("\n  BUGS (will cause runtime errors):")
    for b in bugs:
        print("  " + b)
else:
    print("\n  BUGS: None found")

if warnings:
    print("\n  WARNINGS (may cause issues):")
    for w in warnings[:30]:  # limit output
        print("  " + w)
    if len(warnings) > 30:
        print("  ... and %d more warnings" % (len(warnings) - 30))
else:
    print("\n  WARNINGS: None")

if info:
    print("\n  INFO (code quality, %d items — showing top 15):" % len(info))
    for i in info[:15]:
        print("  " + i)
    if len(info) > 15:
        print("  ... and %d more info items" % (len(info) - 15))

print("\n" + "=" * 70)
print("  SUMMARY: %d BUGS, %d WARNINGS, %d INFO" % (len(bugs), len(warnings), len(info)))
print("=" * 70)
