#!/usr/bin/env python3
"""Patch paper_trader.py for v17b changes:
1. Add Wave Extractor v3 strategy
2. Add commodity re-entry block (30min after SL)
3. Remove per-strategy allocation caps (first-come-first-serve)
"""

PAPER_TRADER = "/root/algo_trading/paper_trader.py"
COMMODITY_TRADER = "/root/algo_trading/commodity_paper_trader.py"

# ─── Patch 1: Add Wave Extractor v3 to paper_trader.py ───────────────────────

with open(PAPER_TRADER) as fh:
    code = fh.read()

changes = 0

# 1a. Add Wave constants after existing strategy constants
wave_constants = """
# ─── Wave Extractor v3 constants (v17) ─────────────────────────────────────
# Gap-based entry: BUY CE when spot rises by GAP points, BUY PE when drops
WAVE_GAP_PTS = {'NIFTY': 75, 'BANKNIFTY': 150, 'SENSEX': 300}
WAVE_VWAP_FILTER = {'NIFTY': True, 'BANKNIFTY': False, 'SENSEX': True}
WAVE_SL_MULT = {'NIFTY': 0.70, 'BANKNIFTY': 0.65, 'SENSEX': 0.55}
WAVE_TARGET_MULT = 1.40
WAVE_COOLOFF_BARS = 5
WAVE_MAX_DAILY_TRADES = 10
"""

if "WAVE_GAP_PTS" not in code:
    # Insert after SCAN_INTERVAL or similar config block
    marker = "REENTRY_COOLDOWN_SECONDS"
    lines = code.split("\n")
    insert_after = None
    for i, line in enumerate(lines):
        if MARKER := "REENTRY_COOLDOWN_SECONDS" in line and "=" in line:
            insert_after = i
    if insert_after is None:
        # Fallback: insert after line containing MIN_PREMIUM_BUY
        for i, line in enumerate(lines):
            if "MIN_PREMIUM_BUY" in line and "=" in line:
                insert_after = i
                break
    if insert_after:
        lines.insert(insert_after + 1, wave_constants)
        code = "\n".join(lines)
        changes += 1
        print("[1a] Added Wave Extractor v3 constants")

# 1b. Add wave anchor tracking to PaperTrader.__init__
if "_wave_anchors" not in code:
    marker = "self._running = False"
    replacement = marker + "\n        self._wave_anchors = {}  # v17: {symbol: anchor_price} for Wave Extractor\n        self._wave_trade_count = defaultdict(int)  # v17: daily trade count per symbol"
    code = code.replace(marker, replacement, 1)
    changes += 1
    print("[1b] Added wave anchor tracking to PaperTrader.__init__")

# 1c. Add check_wave_signals method to StrategyEngine
wave_method = '''
    def check_wave_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Wave Extractor v3 - Gap-based entry with VWAP filter.

        Backtest: +Rs 1,02,437 (+34.15%) in 42 days.
        SENSEX star performer: PF 2.62, Rs 3K drawdown.
        """
        signals = []

        gap = WAVE_GAP_PTS.get(symbol)
        if gap is None:
            return signals

        use_vwap = WAVE_VWAP_FILTER.get(symbol, False)
        sl_mult = WAVE_SL_MULT.get(symbol, 0.65)
        target_mult = WAVE_TARGET_MULT

        # Anchor: day open price (9:15-9:30 open)
        anchor = ohlc.get('open', 0)
        if anchor <= 0:
            return signals

        move = spot - anchor
        vwap = indicators.get('vwap', 0)

        # Determine expiry
        if symbol == "BANKNIFTY":
            days_to_expiry = (2 - dow) % 7
        elif symbol == "SENSEX":
            days_to_expiry = (0 - dow) % 7
        else:
            days_to_expiry = (3 - dow) % 7
        is_expiry = (days_to_expiry == 0 or days_to_expiry == 7)
        T = max(dte, days_to_expiry) / 365 if not is_expiry else 1 / 365
        day_label = "EXPIRY" if is_expiry else f"{days_to_expiry}DTE"

        if move >= gap:
            # UP gap -> BUY CE
            if use_vwap and vwap > 0 and spot < vwap:
                logger.info(f"  WAVE_VWAP_SKIP: {symbol} CE spot={spot:.0f} < VWAP={vwap:.0f}")
                return signals

            strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'CE', dte)
            if chain_ltp > MIN_PREMIUM_BUY:
                use_iv = chain_iv if chain_iv > 0 else indicators['iv'] * 1.3
                if chain_iv > 0:
                    g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, 'CE')
                else:
                    g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, 'CE')
                signals.append({
                    'type': 'BUY_CE_WAVE',
                    'strike': strike,
                    'premium': chain_ltp,
                    'greeks': g,
                    'reason': f"Wave v3 [{day_label}]: Up gap={move:.0f}pts (threshold={gap}) [LIVE]",
                    'target': chain_ltp * target_mult,
                    'sl': chain_ltp * sl_mult,
                })

        elif move <= -gap:
            # DOWN gap -> BUY PE
            if use_vwap and vwap > 0 and spot > vwap:
                logger.info(f"  WAVE_VWAP_SKIP: {symbol} PE spot={spot:.0f} > VWAP={vwap:.0f}")
                return signals

            strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, 'PE', dte)
            if chain_ltp > MIN_PREMIUM_BUY:
                use_iv = chain_iv if chain_iv > 0 else indicators['iv'] * 1.3
                if chain_iv > 0:
                    g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, 'PE')
                else:
                    g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, 'PE')
                signals.append({
                    'type': 'BUY_PE_WAVE',
                    'strike': strike,
                    'premium': chain_ltp,
                    'greeks': g,
                    'reason': f"Wave v3 [{day_label}]: Down gap={move:.0f}pts (threshold={gap}) [LIVE]",
                    'target': chain_ltp * target_mult,
                    'sl': chain_ltp * sl_mult,
                })

        return signals

'''

# Insert Wave method right before check_ghost_zone_signals
if "check_wave_signals" not in code:
    marker = "    def check_ghost_zone_signals"
    code = code.replace(marker, wave_method + marker, 1)
    changes += 1
    print("[1c] Added check_wave_signals method to StrategyEngine")

# 1d. Add Wave to strategy dispatch list
old_dispatch = "('PCR+VWAP', self.engine.check_pcr_vwap_signals),"
if "Wave" not in code.split(old_dispatch)[1][:200] if old_dispatch in code else True:
    new_dispatch = old_dispatch + "\n                ('Wave', self.engine.check_wave_signals),"
    code = code.replace(old_dispatch, new_dispatch, 1)
    changes += 1
    print("[1d] Added Wave to strategy dispatch list")

# ─── Patch 2: Remove per-strategy allocation caps ────────────────────────────

# The STRATEGY_ALLOCATION dict is monitoring-only, but let's make it explicit
if "'Wave'" not in code or "Wave" not in str(code.split("STRATEGY_ALLOCATION")[1][:300] if "STRATEGY_ALLOCATION" in code else ""):
    old_alloc = "'PCR+VWAP':     0.10,"
    if old_alloc in code:
        new_alloc = old_alloc + "\n    'Wave':         0.20,    # v17: Wave Extractor v3"
        code = code.replace(old_alloc, new_alloc, 1)
        changes += 1
        print("[2] Added Wave to STRATEGY_ALLOCATION (monitoring only)")

with open(PAPER_TRADER, "w") as fh:
    fh.write(code)

print(f"\npaper_trader.py: {changes} changes applied")

# ─── Patch 3: Add commodity re-entry block (30min after SL) ──────────────────

with open(COMMODITY_TRADER) as fh:
    ccode = fh.read()

c_changes = 0

# Add SL-specific cooldown constant
if "MCX_SL_REENTRY_COOLDOWN" not in ccode:
    marker = "MCX_REENTRY_COOLDOWN_SECONDS = 600"
    replacement = marker + "\nMCX_SL_REENTRY_COOLDOWN = 1800  # v17: 30min cooldown after SL_HIT (saves Rs 20K+)"
    ccode = ccode.replace(marker, replacement, 1)
    c_changes += 1
    print("[3a] Added MCX_SL_REENTRY_COOLDOWN constant (30min)")

# Modify the escalated cooldown to use longer cooldown after SL
# Find the escalated_cooldown calculation and add SL check
old_escalated = "escalated_cooldown = MCX_REENTRY_COOLDOWN_SECONDS * (2 ** min(dir_losses, 4))"
if old_escalated in ccode and "MCX_SL_REENTRY_COOLDOWN" not in ccode.split(old_escalated)[1][:300]:
    new_escalated = """# v17: Extra-long cooldown after SL_HIT to prevent re-entering losing patterns
                last_sl_exit = None
                for eh in reversed(self.exit_history):
                    if eh['commodity'] == sig['commodity'] and eh['direction'] == sig_opt_type:
                        if 'SL' in eh.get('reason', ''):
                            last_sl_exit = eh
                            break
                if last_sl_exit and (now - last_sl_exit['time']).total_seconds() < MCX_SL_REENTRY_COOLDOWN:
                    elapsed_sl = int((now - last_sl_exit['time']).total_seconds())
                    logger.info(f"  SL_REENTRY_BLOCK: {sig['commodity']} {sig_opt_type} SL exit {elapsed_sl}s ago "
                               f"(need {MCX_SL_REENTRY_COOLDOWN}s = 30min)")
                    skipped += 1
                    cooldown_hit = True
                    continue
                escalated_cooldown = MCX_REENTRY_COOLDOWN_SECONDS * (2 ** min(dir_losses, 4))"""
    ccode = ccode.replace(old_escalated, new_escalated, 1)
    c_changes += 1
    print("[3b] Added SL re-entry block in commodity cooldown logic")

with open(COMMODITY_TRADER, "w") as fh:
    fh.write(ccode)

print(f"\ncommodity_paper_trader.py: {c_changes} changes applied")
print(f"\nAll patches complete! Total: {changes + c_changes} changes")
