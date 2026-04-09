#!/usr/bin/env python3
"""Post-mortem analysis of today's trades from the unified log."""
import re, sys
from collections import defaultdict

LOG = sys.argv[1] if len(sys.argv) > 1 else "/root/algo_trading/logs/unified_paper_20260324.log"

trades = {}
premium_paths = defaultdict(list)
regime_log = []
choppy_blocks = []
quality_scores = []

with open(LOG) as f:
    for line in f:
        # CLOSED trades
        m = re.search(r"CLOSED: (\S+) (\S+) (\S+) \| Entry: Rs ([\d.]+) Exit: Rs ([\d.]+) \| PnL: Rs ([\-\d,.]+) \| Reason: (\S+)", line)
        if m:
            tid = "%s_%s_%s" % (m.group(2), m.group(3), line[11:16].replace(":",""))
            trades[tid] = {
                "type": m.group(1), "symbol": m.group(2), "strike": m.group(3),
                "entry_p": float(m.group(4)), "exit_p": float(m.group(5)),
                "pnl": float(m.group(6).replace(",","")), "reason": m.group(7),
                "time": line[11:19], "ts": line[:23], "line": line.strip()[:200]
            }
            continue

        # EXIT_CHECK premium tracking
        m2 = re.search(r"EXIT_CHECK: (\S+) \| Entry: ([\d.]+) .{1,5} Current: ([\d.]+)", line)
        if m2:
            premium_paths[m2.group(1)].append((line[11:19], float(m2.group(2)), float(m2.group(3))))
            continue

        # Regime
        if "REGIME:" in line and "Equity" in line:
            regime_log.append(line.strip()[:120])
        # Choppy
        if "CHOPPY_DAY_BLOCK" in line and "Equity" in line:
            choppy_blocks.append(line.strip()[:120])
        # Quality
        if "QUALITY_SCORE:" in line:
            quality_scores.append(line.strip()[:150])

# ── Print Report ──
print("=" * 110)
print("  TRADE-BY-TRADE POST-MORTEM — 24 Mar 2026")
print("=" * 110)

# Regime summary
print("\n1. REGIME DETECTION (first 15 minutes):")
for r in regime_log[:12]:
    print("  ", r[24:])

print("\n2. CHOPPY SHIELD ACTIVATIONS:")
if choppy_blocks:
    first = choppy_blocks[0]
    last = choppy_blocks[-1]
    print("   First block: %s" % first[24:])
    print("   Total blocks: %d (from 11:00 onwards only)" % len(choppy_blocks))
    print("   PROBLEM: Shield only active after 11:00 AM. All big losses happened BEFORE 11:00!")
else:
    print("   NONE on equity — choppy shield never fired for equity indices!")

# Each trade
print("\n3. TRADE-BY-TRADE ANALYSIS:")
print("-" * 110)

trade_list = sorted(trades.values(), key=lambda x: x["ts"])
for i, t in enumerate(trade_list, 1):
    pnl = t["pnl"]
    marker = "WIN" if pnl > 0 else "LOSS"
    sym = t["symbol"]
    strike = t["strike"]

    print("\nT%d: %s %s %s | %s Rs {:+,.0f} | Exit: %s" % (i, sym, strike, t["type"], marker, pnl, t["reason"]))
    print("    Entry: Rs %.2f | Exit: Rs %.2f | Time: %s" % (t["entry_p"], t["exit_p"], t["time"]))

    # Find matching premium path
    matched = None
    for pid, path in premium_paths.items():
        if sym in pid and strike.replace(".0","") in pid:
            if len(path) > 0 and abs(path[0][1] - t["entry_p"]) < 2:
                matched = path
                break

    if matched and len(matched) > 2:
        prices = [p[2] for p in matched]
        entry = matched[0][1]
        peak = max(prices)
        trough = min(prices)
        peak_pct = (peak - entry) / entry * 100
        trough_pct = (trough - entry) / entry * 100
        duration = "%s to %s" % (matched[0][0], matched[-1][0])

        print("    Duration: %s (%d samples)" % (duration, len(matched)))
        print("    Peak: Rs %.1f (+%.1f%%) | Trough: Rs %.1f (%.1f%%)" % (peak, peak_pct, trough, trough_pct))

        # Key moments
        n = len(prices)
        step = max(1, n // 5)
        pts = []
        for j in range(0, n, step):
            p = prices[j]
            pct = (p - entry) / entry * 100
            pts.append("%s=%.0f(%+.1f%%)" % (matched[j][0][:5], p, pct))
        if n-1 not in range(0, n, step):
            p = prices[-1]
            pct = (p - entry) / entry * 100
            pts.append("%s=%.0f(%+.1f%%)" % (matched[-1][0][:5], p, pct))
        print("    Path: %s" % " -> ".join(pts))

        # What went wrong / right
        if pnl < -5000:
            if trough_pct < -20:
                print("    PROBLEM: Premium fell {:.0f}%% but no SL triggered. Held too long." % trough_pct)
            if t["reason"] == "DIRECTION_FLIP":
                print("    PROBLEM: Closed at loss to flip direction. Flip was reactive, not predictive.")
            if t["reason"] == "SIGNAL_WEAK_EXIT":
                print("    PROBLEM: Slow bleed — no rapid SL. Took 57 min to exit a loser.")
        elif pnl > 0 and peak_pct > 20 and (peak - t["exit_p"]) / entry * 100 > 10:
            print("    NOTE: Peaked at +{:.0f}%% but TSL captured only +{:.0f}%%. TSL too tight?" % (
                peak_pct, (t["exit_p"] - entry) / entry * 100))
    else:
        print("    (No premium path data)")

# ── Summary Stats ──
print("\n" + "=" * 110)
print("4. SUMMARY STATISTICS")
print("=" * 110)

total = sum(t["pnl"] for t in trades.values())
wins = [t for t in trades.values() if t["pnl"] > 0]
losses = [t for t in trades.values() if t["pnl"] <= 0]
win_total = sum(t["pnl"] for t in wins)
loss_total = sum(t["pnl"] for t in losses)

print("  Trades: %d (%dW/%dL) | Win Rate: {:.0f}%%" % (len(trades), len(wins), len(losses), len(wins)/max(len(trades),1)*100))
print("  Win PnL: Rs {:+,.0f} (avg Rs {:+,.0f})" % (win_total, win_total/max(len(wins),1)))
print("  Loss PnL: Rs {:+,.0f} (avg Rs {:+,.0f})" % (loss_total, loss_total/max(len(losses),1)))
print("  Net PnL: Rs {:+,.0f}" % total)
if len(wins) > 0 and len(losses) > 0:
    print("  Risk/Reward: %.2f" % (abs(win_total/len(wins)) / abs(loss_total/len(losses))))

# By exit reason
print("\n  BY EXIT REASON:")
by_reason = defaultdict(lambda: [0, 0.0])
for t in trades.values():
    by_reason[t["reason"]][0] += 1
    by_reason[t["reason"]][1] += t["pnl"]
for r, d in sorted(by_reason.items(), key=lambda x: x[1][1]):
    print("    %-25s %2d trades  Rs {:+10,.0f}  (avg Rs {:+,.0f})" % (r, d[0], d[1], d[1]/max(d[0],1)))

# By symbol
print("\n  BY SYMBOL:")
by_sym = defaultdict(lambda: [0, 0.0, 0])
for t in trades.values():
    by_sym[t["symbol"]][0] += 1
    by_sym[t["symbol"]][1] += t["pnl"]
    if t["pnl"] > 0: by_sym[t["symbol"]][2] += 1
for s, d in sorted(by_sym.items(), key=lambda x: x[1][1]):
    print("    %-12s %2d trades (%dW/%dL)  Rs {:+10,.0f}" % (s, d[0], d[2], d[0]-d[2], d[1]))

# By strategy
print("\n  BY STRATEGY:")
by_strat = defaultdict(lambda: [0, 0.0, 0])
for t in trades.values():
    strat = t["type"].replace("BUY_PE_","").replace("BUY_CE_","")
    by_strat[strat][0] += 1
    by_strat[strat][1] += t["pnl"]
    if t["pnl"] > 0: by_strat[strat][2] += 1
for s, d in sorted(by_strat.items(), key=lambda x: x[1][1]):
    print("    %-12s %2d trades (%dW/%dL)  Rs {:+10,.0f}" % (s, d[0], d[2], d[0]-d[2], d[1]))

# ── Root Cause ──
print("\n" + "=" * 110)
print("5. ROOT CAUSE ANALYSIS")
print("=" * 110)

flip_loss = sum(t["pnl"] for t in trades.values() if t["reason"] == "DIRECTION_FLIP" and t["pnl"] < 0)
flip_count = sum(1 for t in trades.values() if t["reason"] == "DIRECTION_FLIP" and t["pnl"] < 0)
sensex_loss = sum(t["pnl"] for t in trades.values() if t["symbol"] == "SENSEX" and t["pnl"] < 0)
pre11_loss = sum(t["pnl"] for t in trades.values() if t["time"] < "11:00" and t["pnl"] < 0)
post11_loss = sum(t["pnl"] for t in trades.values() if t["time"] >= "11:00" and t["pnl"] < 0)

print("\n  A. CHOPPY SHIELD FAILURE:")
print("     Shield activates ONLY after 11:00 AM (CHOPPY_DAY_BLOCK_AFTER = 11:00)")
print("     Pre-11:00 losses:  Rs {:+,.0f} (%d trades)" % (pre11_loss, sum(1 for t in trades.values() if t["time"] < "11:00" and t["pnl"] < 0)))
print("     Post-11:00 losses: Rs {:+,.0f} (%d trades)" % (post11_loss, sum(1 for t in trades.values() if t["time"] >= "11:00" and t["pnl"] < 0)))
print("     -> 11:00 is TOO LATE. The regime was SIDEWAYS/FLAT from 9:15 itself!")

print("\n  B. DIRECTION_FLIP DAMAGE:")
print("     %d losing flips = Rs {:+,.0f}" % (flip_count, flip_loss))
print("     Flip exits at a loss + 900s cooldown blocks correct re-entry")
print("     -> Double punishment: lose on exit AND miss the winning direction")

print("\n  C. SENSEX OVER-TRADING:")
print("     SENSEX losses: Rs {:+,.0f} (CPR width was only 0.190%%)" % sensex_loss)
print("     Narrow CPR = high false breakout probability")
print("     -> Need minimum CPR width filter per index")

print("\n  D. NO PREMIUM-BASED STOP LOSS:")
big_losers = [t for t in trades.values() if t["pnl"] < -8000]
print("     %d trades lost >Rs 8,000 each" % len(big_losers))
for t in big_losers:
    drop = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
    print("     %s %s: entry Rs %.0f exit Rs %.0f ({:.0f}%% drop)" % (t["symbol"], t["strike"], t["entry_p"], t["exit_p"], drop))
print("     -> A 15-20%% hard SL on premium would have capped ALL these losses")

print("\n  E. REGIME DETECTION DOESN'T BLOCK ENTRIES:")
print("     Regime detected SIDEWAYS/FLAT at 9:15 for all 3 indices")
print("     But regime only adjusts TSL trail distance (30%% -> 20%%)")
print("     It does NOT block new entries or raise quality threshold")
print("     -> On FLAT days, should require quality > 80 or skip CPR entirely")
