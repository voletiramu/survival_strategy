#!/usr/bin/env python3
"""Patch paper_trader.py for v17 changes:
1. Import OI velocity tracker
2. Initialize tracker in StrategyEngine
3. Replace Gamma Blast with OI velocity version
4. Disable BREAKOUT_FAIL completely
5. Disable MOMENTUM_EXIT
"""

import sys

PAPER_TRADER = "/root/algo_trading/paper_trader.py"

with open(PAPER_TRADER) as fh:
    code = fh.read()

changes = 0

# 1. Add import for oi_velocity_tracker
if "from oi_velocity_tracker import" not in code:
    marker = "from collections import defaultdict"
    replacement = marker + "\nfrom oi_velocity_tracker import OIVelocityTracker, make_gamma_blast_signal"
    code = code.replace(marker, replacement, 1)
    changes += 1
    print("[1] Added oi_velocity_tracker import")

# 2. Add OI tracker initialization in StrategyEngine.__init__
marker_init = "self.truedata = None  # v11.2: Set by PaperTrader if available"
if "self.oi_tracker" not in code:
    replacement_init = marker_init + "\n        self.oi_tracker = OIVelocityTracker()  # v17: OI velocity Gamma Blast"
    code = code.replace(marker_init, replacement_init, 1)
    changes += 1
    print("[2] Added OI tracker to StrategyEngine.__init__")

# 3. Replace check_gamma_blast_signals with OI velocity version
old_gamma_start = "    def check_gamma_blast_signals(self, symbol, spot, ohlc, indicators, dow, dte):"
old_gamma_end = "    def check_ghost_zone_signals"

if old_gamma_start in code:
    start_idx = code.index(old_gamma_start)
    end_idx = code.index(old_gamma_end)

    new_gamma = '''    def check_gamma_blast_signals(self, symbol, spot, ohlc, indicators, dow, dte):
        """Gamma Blast v17 - OI Velocity based (replaces price-action breakout).

        Detects sudden OI surges at ATM+-2 strikes as leading indicator.
        Based on Raahi Bhushan article + backtest optimization.
        Backtest: NIFTY 80% WR, PF 4.58, +16.5% return.
        """
        signals = []

        # Fetch option chain OI if enough time has passed
        if self.oi_tracker.should_fetch(symbol):
            chain = None
            # Source 1: Zerodha
            if self.zerodha:
                try:
                    chain = self.zerodha.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: Zerodha failed for {symbol}: {e}")
            # Source 2: TrueData
            if chain is None and self.truedata:
                try:
                    chain = self.truedata.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: TrueData failed for {symbol}: {e}")
            # Source 3: Market Pipeline
            if chain is None and self.market_pipeline:
                try:
                    chain = self.market_pipeline.get_option_chain(symbol)
                except Exception as e:
                    logger.debug(f"  OI_FETCH: Pipeline failed for {symbol}: {e}")

            if chain:
                self.oi_tracker.update_oi(symbol, spot, chain)
                logger.debug(f"  {self.oi_tracker.get_status(symbol)}")

        # Check cooldown
        if self.oi_tracker.check_cooldown(symbol):
            return signals

        # Check blast signal
        blast = self.oi_tracker.check_blast(symbol, spot)
        if blast["signal"] is None or blast["signal"] == "STRADDLE":
            return signals

        # Determine expiry info
        if symbol == "BANKNIFTY":
            days_to_expiry = (2 - dow) % 7
        elif symbol == "SENSEX":
            days_to_expiry = (0 - dow) % 7
        else:
            days_to_expiry = (3 - dow) % 7
        is_expiry = (days_to_expiry == 0 or days_to_expiry == 7)
        T = max(dte, days_to_expiry) / 365 if not is_expiry else 1 / 365

        # Get ATM strike and live premium
        opt_type = "CE" if blast["signal"] == "BUY_CE" else "PE"
        strike, chain_ltp, chain_iv = self._get_strike_from_chain(symbol, spot, opt_type, dte)

        if chain_ltp <= 0:
            logger.info(f"  GAMMA_OI_NO_LTP: {symbol} {opt_type} strike={strike} - no live premium")
            return signals

        if chain_ltp < MIN_PREMIUM_BUY:
            return signals

        # Compute greeks
        use_iv = chain_iv if chain_iv > 0 else indicators["iv"] * 1.3
        if chain_iv > 0:
            g = greeks_from_market_price(chain_ltp, spot, strike, T, RISK_FREE_RATE, opt_type)
        else:
            g = bs_greeks(spot, strike, T, RISK_FREE_RATE, use_iv, opt_type)

        # Build signal
        sig = make_gamma_blast_signal(blast, symbol, spot, strike, chain_ltp, g, dte, is_expiry)
        if sig:
            signals.append(sig)
            self.oi_tracker.set_cooldown(symbol)
            surge_pct = blast["ce_pct"] if opt_type == "CE" else blast["pe_pct"]
            logger.info(f"  GAMMA_OI_SIGNAL: {symbol} {blast['signal']} "
                       f"surge={surge_pct:.0%} strike={strike} prem={chain_ltp:.2f}")

        # Periodic cleanup
        self.oi_tracker.cleanup(symbol)

        return signals

'''
    code = code[:start_idx] + new_gamma + code[end_idx:]
    changes += 1
    print("[3] Replaced check_gamma_blast_signals with OI velocity version")

# 4. Disable BREAKOUT_FAIL completely
old_bf = "BREAKOUT_FAIL_CHECK_MINUTES = 20"
if old_bf in code:
    code = code.replace(old_bf, "BREAKOUT_FAIL_CHECK_MINUTES = 999  # DISABLED v17 -- backtest proves net negative")
    changes += 1
    print("[4] Disabled BREAKOUT_FAIL (set to 999 min)")

# Also try the =5 version
old_bf5 = "BREAKOUT_FAIL_CHECK_MINUTES = 5"
if old_bf5 in code:
    code = code.replace(old_bf5, "BREAKOUT_FAIL_CHECK_MINUTES = 999  # DISABLED v17")
    changes += 1
    print("[4b] Disabled BREAKOUT_FAIL (was 5 min)")

# Disable reverse
old_bf_rev = "BREAKOUT_FAIL_REVERSE_ENABLED = True"
if old_bf_rev in code:
    code = code.replace(old_bf_rev, "BREAKOUT_FAIL_REVERSE_ENABLED = False  # DISABLED v17")
    changes += 1
    print("[4c] Disabled BREAKOUT_FAIL reverse")

# 5. Disable MOMENTUM_EXIT
old_mom = "MOMENTUM_EXIT_ENABLED = True"
if old_mom in code:
    code = code.replace(old_mom, "MOMENTUM_EXIT_ENABLED = False  # DISABLED v17 -- 0% WR, net -Rs 9,559")
    changes += 1
    print("[5] Disabled MOMENTUM_EXIT")

with open(PAPER_TRADER, "w") as fh:
    fh.write(code)

print(f"\nTotal changes applied: {changes}")
print("Patch complete! Verify with: python3 -c 'import paper_trader; print(\"OK\")'")
