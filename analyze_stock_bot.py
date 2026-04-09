#!/usr/bin/env python3
"""Analyze stock bot performance by strategy."""
import json

with open("/root/algo_trading/stock_paper_trades/stock_portfolio_state.json") as f:
    d = json.load(f)

trades = d.get("closed_trades", [])
capital = d.get("capital", 0)
print(f"Total trades: {len(trades)}")
wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
losers = len(trades) - wins
print(f"Winners: {wins}, Losers: {losers}")
if trades:
    wr = wins / len(trades) * 100
    print(f"Win rate: {wr:.1f}%")
total = sum(t.get("net_pnl", 0) for t in trades)
print(f"Total PnL: Rs {total:+,.0f}")
print(f"Capital: Rs {capital:,.0f}")

# By strategy
strats = {}
strat_details = {}
for t in trades:
    s = t.get("strategy", "unknown")
    if s not in strats:
        strats[s] = 0
        strat_details[s] = {"count": 0, "wins": 0}
    strats[s] += t.get("net_pnl", 0)
    strat_details[s]["count"] += 1
    if t.get("net_pnl", 0) > 0:
        strat_details[s]["wins"] += 1

print("\nPnL by strategy:")
for k in sorted(strats.keys(), key=lambda x: strats[x], reverse=True):
    v = strats[k]
    sd = strat_details[k]
    wr = sd["wins"] / sd["count"] * 100 if sd["count"] else 0
    avg = v / sd["count"] if sd["count"] else 0
    print(f"  {k:20s}: {sd['count']:3d} trades, {wr:5.1f}% WR, Rs {v:+10,.0f} (avg Rs {avg:+,.0f})")

# By symbol
syms = {}
for t in trades:
    s = t.get("symbol", "?")
    if s not in syms:
        syms[s] = 0
    syms[s] += t.get("net_pnl", 0)
print("\nTop 5 symbols:")
for k in sorted(syms.keys(), key=lambda x: syms[x], reverse=True)[:5]:
    print(f"  {k:15s}: Rs {syms[k]:+,.0f}")
print("Bottom 5 symbols:")
for k in sorted(syms.keys(), key=lambda x: syms[x])[:5]:
    print(f"  {k:15s}: Rs {syms[k]:+,.0f}")

# Last 10 trades
print("\nLast 10 trades:")
for t in trades[-10:]:
    sym = t.get("symbol", "?")
    strat = t.get("strategy", "?")
    pnl = t.get("net_pnl", 0)
    sig = t.get("signal_type", "?")
    entry = t.get("entry_time", "?")[:16]
    reason = t.get("exit_reason", "?")
    print(f"  {entry} {sym:12s} {sig:15s} {strat:10s} Rs {pnl:+8,.0f} {reason}")

# Check OI availability for stock options
print("\n--- OI Feasibility for Stock Options ---")
oi_counts = {}
for t in trades:
    sym = t.get("symbol", "?")
    oi = t.get("oi", 0)
    if sym not in oi_counts:
        oi_counts[sym] = []
    oi_counts[sym].append(oi)
has_oi = sum(1 for s, vals in oi_counts.items() if any(v > 0 for v in vals))
print(f"Symbols with OI data: {has_oi}/{len(oi_counts)}")
