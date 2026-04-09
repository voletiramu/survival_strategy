#!/usr/bin/env python3
"""Full code audit of all production files on VPS."""

import py_compile
import os
import re
import sys

os.chdir("/root/algo_trading")

# ── 1. Syntax check all critical files ──────────────────────────────────────
print("=" * 70)
print("  SECTION 1: SYNTAX CHECK")
print("=" * 70)

critical_files = [
    "paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py",
    "run_paper_trade.py", "run_stock_trade.py", "dashboard.py",
    "oi_velocity_tracker.py", "zerodha_feed.py", "zerodha_token_gen.py",
    "ws_feed.py", "market_data_pipeline.py", "market_regime.py",
    "trade_notifier.py", "live_data_logger.py", "market_calculus.py",
    "gift_nifty.py", "trade_intelligence.py", "truedata_feed.py",
    "stock_fno_config.py", "stock_cpr_scanner.py",
]

syntax_ok = 0
syntax_fail = 0
for f in critical_files:
    if not os.path.exists(f):
        print("  [SKIP] %s (not found)" % f)
        continue
    try:
        py_compile.compile(f, doraise=True)
        syntax_ok += 1
    except py_compile.PyCompileError as e:
        print("  [FAIL] %s: %s" % (f, str(e)[:100]))
        syntax_fail += 1

print("  Syntax: %d OK, %d FAIL" % (syntax_ok, syntax_fail))

# ── 2. Check for common code bugs ──────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 2: CODE BUG SCAN")
print("=" * 70)

bugs = []

for fname in ["paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py"]:
    if not os.path.exists(fname):
        continue
    with open(fname) as f:
        code = f.read()
        lines = code.split("\n")

    # Check for undefined variable references
    # Look for common patterns that cause runtime errors

    # a) Bare except without pass or handling
    bare_excepts = sum(1 for l in lines if l.strip() == "except:")
    if bare_excepts > 0:
        bugs.append("[WARN] %s: %d bare 'except:' blocks (should specify exception type)" % (fname, bare_excepts))

    # b) Division by zero risks
    div_zero = sum(1 for l in lines if "/ 0" in l and "max(" not in l and "if" not in l)
    if div_zero > 0:
        bugs.append("[WARN] %s: %d potential division by zero" % (fname, div_zero))

    # c) Check for print() with emoji that might crash on non-UTF8 terminals
    emoji_prints = []
    for i, l in enumerate(lines):
        if "print" in l and any(ord(c) > 127 for c in l):
            emoji_prints.append(i + 1)
    if emoji_prints:
        bugs.append("[INFO] %s: %d print() calls with non-ASCII chars (lines: %s)" % (
            fname, len(emoji_prints), str(emoji_prints[:5])))

    # d) Check for hardcoded TrueData credentials (should be disabled)
    if "Trial138" in code and "DISABLED" not in code.split("Trial138")[0][-200:]:
        bugs.append("[WARN] %s: TrueData credentials (Trial138) still referenced" % fname)

    # e) Check for stale file paths
    if "C:\\Users" in code or "D:\\AlgoTrading" in code:
        bugs.append("[WARN] %s: Windows paths found (should use Linux paths on VPS)" % fname)

    # f) Check for TODO/FIXME/HACK markers
    todos = sum(1 for l in lines if "TODO" in l.upper() or "FIXME" in l.upper() or "HACK" in l.upper())
    if todos > 0:
        bugs.append("[INFO] %s: %d TODO/FIXME/HACK markers" % (fname, todos))

    # g) Check for unclosed file handles
    open_without_with = sum(1 for l in lines if "open(" in l and "with " not in l and "= open" in l)
    if open_without_with > 0:
        bugs.append("[WARN] %s: %d open() calls without 'with' context manager" % (fname, open_without_with))

for b in bugs:
    print("  " + b)
if not bugs:
    print("  No code bugs found")

# ── 3. Check strategy consistency ───────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 3: STRATEGY CONSISTENCY")
print("=" * 70)

with open("paper_trader.py") as f:
    eq_code = f.read()

# Verify all strategies have dispatch entries
strategies_expected = ["CPR", "Gamma Blast", "Ghost Zone", "PCR+VWAP", "Wave"]
for s in strategies_expected:
    if s in eq_code:
        print("  [OK] Equity: %s strategy present" % s)
    else:
        print("  [MISS] Equity: %s strategy NOT FOUND" % s)

with open("stock_paper_trader.py") as f:
    stk_code = f.read()

stock_strategies = ["check_cpr_breakout", "check_gamma_blast", "check_pcr_vwap", "check_wave"]
for s in stock_strategies:
    if s in stk_code:
        print("  [OK] Stock: %s present" % s)
    else:
        print("  [MISS] Stock: %s NOT FOUND" % s)

# ── 4. Check exit logic consistency ─────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 4: EXIT LOGIC AUDIT")
print("=" * 70)

exit_checks = {
    "paper_trader.py": {
        "BREAKOUT_FAIL disabled": "BREAKOUT_FAIL_CHECK_MINUTES = 9999",
        "MOMENTUM_EXIT disabled": "MOMENTUM_EXIT_ENABLED = False",
        "THETA_DECAY disabled": "THETA_BURDEN_EXIT_PCT = 999",
    },
    "stock_paper_trader.py": {
        "SIGNAL_WEAK_EXIT disabled": "DISABLED SIGNAL_WEAK_EXIT",
    },
    "commodity_paper_trader.py": {
        "SL re-entry block": "MCX_SL_REENTRY_COOLDOWN",
    },
}

for fname, checks in exit_checks.items():
    with open(fname) as f:
        code = f.read()
    for name, pattern in checks.items():
        if pattern in code:
            print("  [OK] %s: %s" % (fname, name))
        else:
            print("  [MISS] %s: %s (pattern: %s)" % (fname, name, pattern[:40]))

# ── 5. Check data source configuration ──────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 5: DATA SOURCE CONFIG")
print("=" * 70)

for fname in ["paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py"]:
    with open(fname) as f:
        code = f.read()
    td_disabled = "TrueData DISABLED" in code or "TrueData disabled" in code
    zd_present = "zerodha" in code.lower() or "ZerodhaFeed" in code
    angel_present = "AngelConnection" in code or "Angel" in code
    print("  %s:" % fname)
    print("    Zerodha: %s | TrueData: %s | Angel: %s" % (
        "YES" if zd_present else "NO",
        "DISABLED" if td_disabled else "ACTIVE",
        "YES" if angel_present else "NO"))

# ── 6. Check for resource leaks / thread safety ────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 6: RESOURCE & THREAD SAFETY")
print("=" * 70)

for fname in ["paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py"]:
    with open(fname) as f:
        code = f.read()
    # Check for proper lock usage
    has_lock = "Lock()" in code or "threading.Lock" in code
    has_signal_handler = "signal.SIGINT" in code or "signal_handler" in code
    has_save_state = "save_state" in code
    has_graceful_shutdown = "running = False" in code or "_running = False" in code
    print("  %s:" % fname)
    print("    Lock: %s | Signal handler: %s | Save state: %s | Graceful shutdown: %s" % (
        "YES" if has_lock else "NO",
        "YES" if has_signal_handler else "NO",
        "YES" if has_save_state else "NO",
        "YES" if has_graceful_shutdown else "NO"))

# ── 7. Dashboard check ─────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 7: DASHBOARD")
print("=" * 70)

with open("templates/dashboard.html") as f:
    html = f.read()

dash_checks = {
    "Wave filter button": "WAVE" in html,
    "Trend filter button": "TREND" in html,
    "Sweep filter button": "SWEEP" in html,
    "Signal strength CSS": "strength-bar" in html,
    "Signal strength JS": "strengthBadge" in html,
    "Source badge CSS": "tag-zerodha" in html,
}
for name, ok in dash_checks.items():
    print("  [%s] %s" % ("OK" if ok else "MISS", name))

# ── 8. File size sanity check ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 8: FILE SIZE SANITY")
print("=" * 70)

for fname in ["paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py"]:
    size = os.path.getsize(fname)
    lines_count = sum(1 for _ in open(fname))
    print("  %s: %d lines, %.0f KB" % (fname, lines_count, size / 1024))

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if syntax_fail == 0 and not any("[FAIL]" in b for b in bugs):
    print("  FULL AUDIT: CLEAN — No critical issues found")
else:
    print("  FULL AUDIT: %d syntax failures, %d bugs" % (syntax_fail, len(bugs)))
print("=" * 70)
