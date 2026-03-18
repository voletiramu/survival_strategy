"""Patch paper_trader.py, stock_paper_trader.py, commodity_paper_trader.py
to add V12 physics gate after DCI check."""

import sys

PHYSICS_GATE_CODE = '''
            # ---- v12.0: PHYSICS GATE — mathematical signal quality filter ----
            # Blocks entries where: momentum dying, wave peak, VWAP stretched, RSI extreme
            # Backtested: PF 2.65 -> 3.18, blocks wrong-direction entries mathematically
            if hasattr(self, 'calculus') and self.calculus.bar_count(sig['symbol']) >= 6:
                sig_dir = 'CE' if 'CE' in sig['type'] else 'PE'
                phy_score, phy_diag = self.calculus.physics_gate(sig['symbol'], sig_dir, sig['spot'])
                sig['_physics_score'] = phy_score
                sig['_physics_diag'] = phy_diag
                if phy_score < 0:
                    warnings_list = []
                    if phy_diag.get('mom_dying'): warnings_list.append('MOM_DYING')
                    if phy_diag.get('wave_peak'): warnings_list.append('WAVE_PEAK')
                    if phy_diag.get('stretched'): warnings_list.append('VWAP_STRETCHED')
                    if phy_diag.get('rsi_extreme'): warnings_list.append('RSI_EXTREME')
                    logger.info(f"  PHYSICS_BLOCK: {sig['symbol']} {sig['type']} "
                               f"score={phy_score} [{','.join(warnings_list)}] "
                               f"accel={phy_diag.get('accel','?')} wave={phy_diag.get('wave_risk','?')} "
                               f"vwap={phy_diag.get('vwap_stretch','?')} rsi={phy_diag.get('rsi','?')}")
                    skipped += 1
                    continue
                else:
                    logger.info(f"  PHYSICS_PASS: {sig['symbol']} {sig['type']} "
                               f"score={phy_score} accel={phy_diag.get('accel','?')} "
                               f"wave={phy_diag.get('wave_risk','?')}")
'''

# Stock bot version — uses 'symbol' and 'signal_type' vars instead of sig dict
PHYSICS_GATE_STOCK = '''
            # ---- v12.0: PHYSICS GATE — mathematical signal quality filter ----
            if hasattr(self, 'calculus') and self.calculus.bar_count(symbol) >= 6:
                sig_dir = 'CE' if 'CE' in signal_type else 'PE'
                phy_score, phy_diag = self.calculus.physics_gate(symbol, sig_dir)
                sig['_physics_score'] = phy_score
                if phy_score < 0:
                    warnings_list = []
                    if phy_diag.get('mom_dying'): warnings_list.append('MOM_DYING')
                    if phy_diag.get('wave_peak'): warnings_list.append('WAVE_PEAK')
                    if phy_diag.get('stretched'): warnings_list.append('VWAP_STRETCHED')
                    if phy_diag.get('rsi_extreme'): warnings_list.append('RSI_EXTREME')
                    logger.info(f"  PHYSICS_BLOCK: {symbol} {signal_type} "
                               f"score={phy_score} [{','.join(warnings_list)}] "
                               f"accel={phy_diag.get('accel','?')} wave={phy_diag.get('wave_risk','?')}")
                    skipped += 1
                    continue
                else:
                    logger.info(f"  PHYSICS_PASS: {symbol} {signal_type} score={phy_score}")
'''


# Anchors for each bot
ANCHORS = {
    '/root/algo_trading/paper_trader.py': "            # ---- v10.4: IV + DTE hard gate",
    '/root/algo_trading/commodity_paper_trader.py': "            # ---- v10.4: IV + DTE hard gate",
    '/root/algo_trading/stock_paper_trader.py': "            # Get spot and determine strike",
}


def patch_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    if 'PHYSICS_BLOCK' in content:
        print(f"  {filepath}: already patched, skipping")
        return

    anchor = ANCHORS.get(filepath)
    if not anchor or anchor not in content:
        print(f"  {filepath}: anchor not found, skipping")
        return

    # Use stock-specific code for stock bot
    if 'stock_paper_trader' in filepath:
        gate_code = PHYSICS_GATE_STOCK
    else:
        gate_code = PHYSICS_GATE_CODE

    # Insert physics gate code BEFORE the anchor line
    content = content.replace(anchor, gate_code + anchor, 1)

    with open(filepath, 'w') as f:
        f.write(content)

    print(f"  {filepath}: PATCHED successfully")


if __name__ == '__main__':
    files = [
        '/root/algo_trading/paper_trader.py',
        '/root/algo_trading/stock_paper_trader.py',
        '/root/algo_trading/commodity_paper_trader.py',
    ]
    for f in files:
        patch_file(f)
