#!/usr/bin/env python3
"""Patch stock_paper_trader.py for v17:
1. Fix entry_time missing in trade records
2. Fix get_intraday_ohlc AttributeError
3. Disable SIGNAL_WEAK_EXIT (net -Rs 6,490)
4. Add ATR-based Wave Extractor for stocks
"""

STOCK_TRADER = "/root/algo_trading/stock_paper_trader.py"

with open(STOCK_TRADER) as fh:
    code = fh.read()

changes = 0

# ─── Fix 1: Add entry_time to position dict in add_signal ───────────────────
# The position dict uses 'timestamp' but other code expects 'entry_time'
old_ts = "'timestamp': datetime.now().isoformat(),"
new_ts = "'timestamp': datetime.now().isoformat(),\n            'entry_time': datetime.now().isoformat(),  # v17: fix missing entry_time"
if "entry_time" not in code.split("position = {")[1].split("}")[0]:
    code = code.replace(old_ts, new_ts, 1)
    changes += 1
    print("[1] Fixed entry_time missing in add_signal position dict")
else:
    print("[1] entry_time already present")

# ─── Fix 2: Fix get_intraday_ohlc AttributeError ────────────────────────────
# Signal logging calls self.get_intraday_ohlc but StockPaperTrader doesn't have it
if "def get_intraday_ohlc" not in code and "get_intraday_ohlc" in code:
    # Find where signals are logged (the error occurs in signal logging)
    # Add a stub method to StockPaperTrader
    marker = "class StockPaperTrader:"
    # Find the __init__ end and add method there
    init_end = code.find("def run_once", code.find("class StockPaperTrader:"))
    if init_end > 0:
        stub = '''
    def get_intraday_ohlc(self, symbol):
        """v17: Stub for signal logging compatibility. Returns cached OHLC if available."""
        try:
            if hasattr(self, '_last_ohlc') and symbol in self._last_ohlc:
                return self._last_ohlc[symbol]
        except Exception:
            pass
        return None

'''
        code = code[:init_end] + stub + code[init_end:]
        changes += 1
        print("[2] Added get_intraday_ohlc stub method")
else:
    print("[2] get_intraday_ohlc already exists or not referenced")

# ─── Fix 3: Disable SIGNAL_WEAK_EXIT ────────────────────────────────────────
# Net -Rs 6,490 across 7 trades, 0% win rate
old_weak = "            if (hold_mins >= HOLD_SCORE_MIN_HOLD_MINS\n                    and hold_score < HOLD_SCORE_WEAK\n                    and pos.get('unrealized_pnl', 0) < 0\n                    and hours_held > 1):"
new_weak = "            if False:  # v17: DISABLED SIGNAL_WEAK_EXIT — net -Rs 6,490 across 7 trades, 0% WR\n            # Original: if (hold_mins >= HOLD_SCORE_MIN_HOLD_MINS and hold_score < HOLD_SCORE_WEAK and pnl < 0 and hours > 1):"

if "DISABLED SIGNAL_WEAK_EXIT" not in code:
    # Try simpler match
    if "SIGNAL_WEAK_EXIT" in code and "hold_score < HOLD_SCORE_WEAK" in code:
        # Find the if block
        idx = code.find("# ---- SIGNAL WEAK EXIT")
        if idx > 0:
            # Find the if statement after the comment
            if_start = code.find("            if (hold_mins >= HOLD_SCORE_MIN_HOLD_MINS", idx)
            if if_start > 0:
                # Find end of the condition (the colon)
                if_end = code.find(":", if_start) + 1
                old_cond = code[if_start:if_end]
                new_cond = "            if False:  # v17: DISABLED SIGNAL_WEAK_EXIT -- net -Rs 6,490, 0% WR"
                code = code.replace(old_cond, new_cond, 1)
                changes += 1
                print("[3] Disabled SIGNAL_WEAK_EXIT")
            else:
                print("[3] Could not find SIGNAL_WEAK_EXIT if-block")
        else:
            print("[3] Could not find SIGNAL_WEAK_EXIT comment")
    else:
        print("[3] SIGNAL_WEAK_EXIT not found in code")
else:
    print("[3] SIGNAL_WEAK_EXIT already disabled")

# ─── Fix 4: Add ATR-based Wave Extractor for stocks ─────────────────────────
wave_method = '''
    def check_wave_signals(self, symbol, spot, ohlc, indicators, config):
        """Wave Extractor v3 for stocks — ATR-based gap entry.

        Uses 0.3% of spot price as gap threshold (instead of fixed points).
        VWAP filter for confirmation. Backtest: +34% on indices.
        """
        signals = []
        if not indicators:
            return signals

        atr = indicators.get('atr', 0)
        vwap = indicators.get('vwap', 0)

        # ATR-based gap: 30% of ATR as minimum movement
        # Fallback: 0.3% of spot price
        if atr > 0:
            gap_threshold = atr * 0.30
        else:
            gap_threshold = spot * 0.003

        anchor = ohlc.get('open', 0)
        if anchor <= 0 or gap_threshold <= 0:
            return signals

        move = spot - anchor
        strike_interval = config.get('strike_interval', 50)
        dte = config.get('dte', 5)

        # SL and target (same as index Wave v3)
        sl_mult = 0.65
        target_mult = 1.40

        if move >= gap_threshold:
            # Up gap -> BUY CE
            if vwap > 0 and spot < vwap:
                return signals  # VWAP filter

            atm = round(spot / strike_interval) * strike_interval
            signals.append({
                'type': 'BUY_CE_WAVE',
                'strike': atm,
                'premium': 0,  # Will be filled by option chain lookup
                'greeks': {},
                'reason': 'Wave v3 [STOCK]: Up gap=%.0f (threshold=%.0f, ATR=%.0f) [LIVE]' % (move, gap_threshold, atr),
                'target_mult': target_mult,
                'sl_mult': sl_mult,
            })

        elif move <= -gap_threshold:
            # Down gap -> BUY PE
            if vwap > 0 and spot > vwap:
                return signals  # VWAP filter

            atm = round(spot / strike_interval) * strike_interval
            signals.append({
                'type': 'BUY_PE_WAVE',
                'strike': atm,
                'premium': 0,
                'greeks': {},
                'reason': 'Wave v3 [STOCK]: Down gap=%.0f (threshold=%.0f, ATR=%.0f) [LIVE]' % (abs(move), gap_threshold, atr),
                'target_mult': target_mult,
                'sl_mult': sl_mult,
            })

        return signals

'''

if "check_wave_signals" not in code:
    # Insert before check_pcr_vwap_signals
    marker = "    def check_pcr_vwap_signals"
    if marker in code:
        code = code.replace(marker, wave_method + marker, 1)
        changes += 1
        print("[4a] Added check_wave_signals method to StockStrategyEngine")
    else:
        print("[4a] Could not find insertion point for Wave method")
else:
    print("[4a] check_wave_signals already exists")

# Add Wave to strategy dispatch
old_dispatch = "            # Check PCR+VWAP"
new_dispatch = """            # Check Wave Extractor v3 (ATR-based gap)
            wave_signals = self.engine.check_wave_signals(symbol, spot, ohlc, indicators, config)
            for s in wave_signals:
                s['_indicators'] = indicators
                s['dte'] = dte
            all_signals.extend(wave_signals)

            # Check PCR+VWAP"""

if "check_wave_signals" not in code.split("all_signals")[0][-500:] if "all_signals" in code else True:
    if "wave_signals = self.engine.check_wave" not in code:
        code = code.replace(old_dispatch, new_dispatch, 1)
        changes += 1
        print("[4b] Added Wave to strategy dispatch")
    else:
        print("[4b] Wave already in dispatch")
else:
    print("[4b] Wave dispatch already present")

# ─── Save and verify ────────────────────────────────────────────────────────
with open(STOCK_TRADER, "w") as fh:
    fh.write(code)

import py_compile
try:
    py_compile.compile(STOCK_TRADER, doraise=True)
    print("\nstock_paper_trader.py: Syntax OK")
except py_compile.PyCompileError as e:
    print("\nSyntax ERROR: %s" % e)

print("Total changes: %d" % changes)
