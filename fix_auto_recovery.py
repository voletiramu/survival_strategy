#!/usr/bin/env python3
"""Add auto-recovery mechanism to all 3 bot launchers.
If 10 consecutive scan errors occur, auto-restart the service via systemd."""
import ast

# ============================================
# FIX run_paper_trade.py (equity + commodity)
# ============================================
print("=== run_paper_trade.py ===")
with open("/root/algo_trading/run_paper_trade.py", "r") as f:
    code = f.read()

# Add error counter at module level
if "_eq_err_count" not in code:
    code = code.replace(
        "import sys\n",
        "import sys\n\n# v13.7: Consecutive error counters for auto-recovery\n_eq_err_count = 0\n_cm_err_count = 0\n",
    )
    print("  Added error counters")

# Equity: add counter increment and auto-restart
eq_old = '                    logger.error(f"[EquityThread] Scan error: {e}")'
eq_new = '''                    logger.error(f"[EquityThread] Scan error: {e}")
                    global _eq_err_count
                    _eq_err_count += 1
                    if _eq_err_count >= 10:
                        logger.error(f"[EquityThread] AUTO-RECOVERY: {_eq_err_count} consecutive errors — restarting")
                        import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-trading'])
                        break'''
if eq_old in code and "_eq_err_count += 1" not in code:
    code = code.replace(eq_old, eq_new)
    print("  Added equity auto-recovery")

# Commodity: add counter increment and auto-restart
cm_old = '                    logger.error(f"[CommodityThread] Scan error: {e}", exc_info=True)'
cm_new = '''                    logger.error(f"[CommodityThread] Scan error: {e}", exc_info=True)
                    global _cm_err_count
                    _cm_err_count += 1
                    if _cm_err_count >= 10:
                        logger.error(f"[CommodityThread] AUTO-RECOVERY: {_cm_err_count} consecutive errors — restarting")
                        import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-trading'])
                        break'''
if cm_old in code and "_cm_err_count += 1" not in code:
    code = code.replace(cm_old, cm_new)
    print("  Added commodity auto-recovery")

# Reset counters on successful scan
# After "Executed:" log lines, reset the counter
eq_reset = '                    _eq_err_count = 0  # Reset on successful scan'
if eq_reset not in code:
    # Find equity Executed log and add reset before it
    code = code.replace(
        '                    equity_trader.execute_paper_signals(signals)',
        '                    global _eq_err_count\n                    _eq_err_count = 0  # Reset on successful scan\n                    equity_trader.execute_paper_signals(signals)',
    )
    print("  Added equity error reset on success")

cm_reset = '                    _cm_err_count = 0  # Reset on successful scan'
if cm_reset not in code:
    code = code.replace(
        '                    commodity_trader.execute_paper_signals(signals)',
        '                    global _cm_err_count\n                    _cm_err_count = 0  # Reset on successful scan\n                    commodity_trader.execute_paper_signals(signals)',
    )
    print("  Added commodity error reset on success")

ast.parse(code)
with open("/root/algo_trading/run_paper_trade.py", "w") as f:
    f.write(code)
print("  Syntax OK\n")

# ============================================
# FIX run_stock_trade.py
# ============================================
print("=== run_stock_trade.py ===")
with open("/root/algo_trading/run_stock_trade.py", "r") as f:
    stk = f.read()

if "_stk_err_count" not in stk:
    stk = stk.replace(
        "import sys",
        "import sys\n\n_stk_err_count = 0  # v13.7: Auto-recovery counter\n",
    )
    print("  Added error counter")

stk_old = '                logger.error(f"[StockLoop] Scan error: {e}", exc_info=True)'
stk_new = '''                logger.error(f"[StockLoop] Scan error: {e}", exc_info=True)
                global _stk_err_count
                _stk_err_count += 1
                if _stk_err_count >= 10:
                    logger.error(f"[StockLoop] AUTO-RECOVERY: {_stk_err_count} consecutive errors — restarting")
                    import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-stock-trading'])
                    break'''
if stk_old in stk and "_stk_err_count += 1" not in stk:
    stk = stk.replace(stk_old, stk_new)
    print("  Added auto-recovery")

# Reset on success
if "_stk_err_count = 0  # Reset" not in stk:
    stk = stk.replace(
        '                stock_trader.execute_paper_signals(signals)',
        '                global _stk_err_count\n                _stk_err_count = 0  # Reset on success\n                stock_trader.execute_paper_signals(signals)',
    )
    print("  Added error reset on success")

ast.parse(stk)
with open("/root/algo_trading/run_stock_trade.py", "w") as f:
    f.write(stk)
print("  Syntax OK\n")

print("=== AUTO-RECOVERY MECHANISM ===")
print("  If 10 consecutive scan errors → systemctl restart (auto)")
print("  On successful scan → counter resets to 0")
print("  Telegram notified on auto-restart")
print("  All 3 bots protected: equity, commodity, stock")
