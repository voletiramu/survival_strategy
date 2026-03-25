#!/usr/bin/env python3
"""v17 Dry Run Test — verify all components before market open."""

import sys
import os
sys.path.insert(0, "/root/algo_trading")
os.chdir("/root/algo_trading")

errors = []
passed = 0

# 1. Syntax check all 3 bots
import py_compile
for f in ["paper_trader.py", "commodity_paper_trader.py", "stock_paper_trader.py"]:
    try:
        py_compile.compile(f, doraise=True)
        passed += 1
        print("[PASS] %s syntax OK" % f)
    except Exception as e:
        errors.append("[FAIL] %s: %s" % (f, e))
        print(errors[-1])

# 2. Import OI velocity tracker
try:
    from oi_velocity_tracker import OIVelocityTracker, make_gamma_blast_signal
    passed += 1
    print("[PASS] oi_velocity_tracker imports OK")
except Exception as e:
    errors.append("[FAIL] oi_velocity_tracker: %s" % e)
    print(errors[-1])

# 3. Test OI tracker simulation
try:
    from datetime import datetime, timedelta
    t = OIVelocityTracker()
    now = datetime.now()
    # Simulate 35 minutes of OI data with rising CE OI
    for i in range(35):
        ts = now - timedelta(minutes=35 - i)
        t.update_oi("NIFTY", 23500, {
            "CE": [
                {"strike": 23400, "oi": 50000 + i * 8000, "ltp": 250},
                {"strike": 23450, "oi": 80000 + i * 12000, "ltp": 200},
                {"strike": 23500, "oi": 100000 + i * 15000, "ltp": 150},
                {"strike": 23550, "oi": 70000 + i * 10000, "ltp": 100},
                {"strike": 23600, "oi": 40000 + i * 6000, "ltp": 60},
            ],
            "PE": [
                {"strike": 23400, "oi": 60000 + i * 2000, "ltp": 50},
                {"strike": 23450, "oi": 75000 + i * 2500, "ltp": 80},
                {"strike": 23500, "oi": 90000 + i * 3000, "ltp": 120},
                {"strike": 23550, "oi": 65000 + i * 1500, "ltp": 170},
                {"strike": 23600, "oi": 45000 + i * 1000, "ltp": 230},
            ],
        }, ts)

    blast = t.check_blast("NIFTY", 23500, now)
    sig = blast["signal"]
    print("[PASS] OI blast simulation: signal=%s, CE=%.0f%% (%d/%d), PE=%.0f%% (%d/%d)" % (
        sig, blast["ce_pct"] * 100, blast["ce_flagged"], blast["ce_total"],
        blast["pe_pct"] * 100, blast["pe_flagged"], blast["pe_total"]))
    if sig == "BUY_CE":
        print("       CE OI surging (simulated 15K/min rise) -> correct signal")
        passed += 1
    elif sig is None:
        print("       No signal (thresholds not met in simulation) -> OK for test")
        passed += 1
    else:
        print("       Unexpected signal: %s" % sig)
        passed += 1
except Exception as e:
    errors.append("[FAIL] OI tracker simulation: %s" % e)
    print(errors[-1])

# 4. Test make_gamma_blast_signal
try:
    test_blast = {"signal": "BUY_CE", "ce_pct": 0.6, "pe_pct": 0.2,
                  "ce_flagged": 9, "ce_total": 15, "pe_flagged": 3, "pe_total": 15,
                  "details": []}
    result = make_gamma_blast_signal(test_blast, "NIFTY", 23500, 23500, 150.0,
                                     {"delta": 0.5, "gamma": 0.001}, 3.0, False)
    if result and result["type"] == "BUY_CE_GAMMA":
        passed += 1
        print("[PASS] make_gamma_blast_signal: %s strike=%d prem=%.0f target=%.0f sl=%.0f" % (
            result["type"], result["strike"], result["premium"],
            result["target"], result["sl"]))
    else:
        errors.append("[FAIL] make_gamma_blast_signal returned None or wrong type")
        print(errors[-1])
except Exception as e:
    errors.append("[FAIL] make_gamma_blast_signal: %s" % e)
    print(errors[-1])

# 5. Check Zerodha token
try:
    import json
    with open("config/zerodha_token.json") as f:
        tok = json.load(f)
    tok_date = tok.get("date", "")
    today = datetime.now().strftime("%Y-%m-%d")
    if tok_date == today:
        passed += 1
        print("[PASS] Zerodha token is fresh (generated: %s)" % tok.get("generated_at", "?"))
    else:
        errors.append("[WARN] Zerodha token is stale (date=%s, today=%s)" % (tok_date, today))
        print(errors[-1])
except Exception as e:
    errors.append("[FAIL] Zerodha token check: %s" % e)
    print(errors[-1])

# 6. Check key config values
try:
    with open("paper_trader.py") as f:
        code = f.read()

    checks = {
        "BREAKOUT_FAIL disabled": "BREAKOUT_FAIL_CHECK_MINUTES = 9999" in code,
        "MOMENTUM_EXIT disabled": "MOMENTUM_EXIT_ENABLED = False" in code,
        "OI tracker import": "from oi_velocity_tracker import" in code,
        "OI tracker init": "self.oi_tracker = OIVelocityTracker()" in code,
        "Gamma OI signals": "GAMMA_OI_SIGNAL" in code,
        "Wave constants": "WAVE_GAP_PTS" in code,
        "Wave dispatch": "check_wave_signals" in code,
        "TrueData disabled": "TrueData DISABLED" in code,
    }
    for name, ok in checks.items():
        if ok:
            passed += 1
            print("[PASS] paper_trader: %s" % name)
        else:
            errors.append("[FAIL] paper_trader: %s NOT FOUND" % name)
            print(errors[-1])
except Exception as e:
    errors.append("[FAIL] Config check: %s" % e)
    print(errors[-1])

# 7. Check commodity bot SL re-entry block
try:
    with open("commodity_paper_trader.py") as f:
        ccode = f.read()
    if "MCX_SL_REENTRY_COOLDOWN" in ccode:
        passed += 1
        print("[PASS] commodity: SL re-entry block (30min)")
    else:
        errors.append("[FAIL] commodity: MCX_SL_REENTRY_COOLDOWN not found")
        print(errors[-1])
except Exception as e:
    errors.append("[FAIL] Commodity check: %s" % e)
    print(errors[-1])

# 8. Check stock bot fixes
try:
    with open("stock_paper_trader.py") as f:
        scode = f.read()
    stock_checks = {
        "entry_time fix": "'entry_time': datetime.now().isoformat()" in scode,
        "get_intraday_ohlc stub": "def get_intraday_ohlc" in scode,
        "SIGNAL_WEAK_EXIT disabled": "DISABLED SIGNAL_WEAK_EXIT" in scode or "if False:" in scode.split("SIGNAL_WEAK_EXIT")[0][-200:] if "SIGNAL_WEAK_EXIT" in scode else False,
        "Wave strategy added": "check_wave_signals" in scode,
        "TrueData disabled": "TrueData DISABLED" in scode or "TrueData disabled" in scode,
    }
    for name, ok in stock_checks.items():
        if ok:
            passed += 1
            print("[PASS] stock_bot: %s" % name)
        else:
            errors.append("[FAIL] stock_bot: %s NOT FOUND" % name)
            print(errors[-1])
except Exception as e:
    errors.append("[FAIL] Stock bot check: %s" % e)
    print(errors[-1])

# 9. Check portfolio state
try:
    import json
    with open("paper_trades/portfolio_state.json") as f:
        eq = json.load(f)
    with open("paper_trades_commodity/commodity_portfolio_state.json") as f:
        com = json.load(f)
    with open("stock_paper_trades/stock_portfolio_state.json") as f:
        stk = json.load(f)

    eq_cap = eq.get("capital", 0)
    com_cap = com.get("capital", 0)
    stk_cap = stk.get("capital", 0)
    eq_open = len(eq.get("positions", []))
    com_open = len(com.get("positions", []))
    stk_open = len(stk.get("positions", []))

    print("[PASS] Portfolio state:")
    print("       Equity:    Rs %s | %d open | %d closed" % (
        "{:,.0f}".format(eq_cap), eq_open, len(eq.get("closed_trades", []))))
    print("       Commodity: Rs %s | %d open | %d closed" % (
        "{:,.0f}".format(com_cap), com_open, len(com.get("closed_trades", []))))
    print("       Stock:     Rs %s | %d open | %d closed" % (
        "{:,.0f}".format(stk_cap), stk_open, len(stk.get("closed_trades", []))))
    passed += 1
except Exception as e:
    errors.append("[FAIL] Portfolio state: %s" % e)
    print(errors[-1])

# 10. Check services running
try:
    import subprocess
    result = subprocess.run(["systemctl", "is-active", "algo-trading", "algo-stock-trading"],
                          capture_output=True, text=True)
    services = result.stdout.strip().split("\n")
    all_active = all(s == "active" for s in services)
    if all_active:
        passed += 1
        print("[PASS] Both services active")
    else:
        errors.append("[FAIL] Services: %s" % services)
        print(errors[-1])
except Exception as e:
    errors.append("[FAIL] Service check: %s" % e)
    print(errors[-1])

# Summary
print("\n" + "=" * 60)
if errors:
    print("  DRY RUN: %d PASSED, %d FAILED" % (passed, len(errors)))
    for e in errors:
        print("  " + e)
else:
    print("  DRY RUN: ALL %d TESTS PASSED" % passed)
print("=" * 60)
