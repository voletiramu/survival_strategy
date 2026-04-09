#!/usr/bin/env python3
"""Analyze SL re-entry patterns in equity and stock bots.
Question: After an SL_HIT, do subsequent same-symbol same-direction trades win or lose?
"""
import json
from datetime import datetime, timedelta
from collections import defaultdict

def analyze_bot(name, portfolio_file):
    print("\n" + "=" * 70)
    print("  %s — SL Re-Entry Analysis" % name)
    print("=" * 70)

    with open(portfolio_file) as f:
        d = json.load(f)

    trades = d.get("closed_trades", [])
    if not trades:
        print("  No closed trades")
        return

    # Use correct PnL field
    pnl_key = "pnl" if trades[0].get("pnl") is not None else "net_pnl"

    # Group trades by date
    by_date = defaultdict(list)
    for t in trades:
        exit_time = t.get("exit_time", t.get("timestamp", ""))
        if exit_time:
            day = exit_time[:10]
            by_date[day].append(t)

    # For each day, find SL_HIT exits and check what happens next
    sl_exits = []
    post_sl_trades = []  # trades that entered after an SL on same symbol+direction
    post_sl_trades_30min = []  # trades that entered within 30 min of SL

    for day, day_trades in sorted(by_date.items()):
        # Sort by exit time
        day_trades.sort(key=lambda t: t.get("exit_time", t.get("timestamp", "")))

        # Track SL exits
        sl_history = {}  # (symbol, direction) -> exit_time

        for t in day_trades:
            sym = t.get("symbol", t.get("stock_symbol", "?"))
            sig = t.get("signal_type", "")
            direction = "CE" if "CE" in sig else "PE"
            exit_reason = t.get("exit_reason", "")
            exit_time_str = t.get("exit_time", t.get("timestamp", ""))
            entry_time_str = t.get("entry_time", t.get("timestamp", ""))
            pnl = t.get(pnl_key, 0)

            # Check if this trade entered AFTER a previous SL on same symbol+direction
            key = (sym, direction)
            if key in sl_history:
                sl_exit_time = sl_history[key]
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str) if entry_time_str else None
                    sl_dt = datetime.fromisoformat(sl_exit_time) if sl_exit_time else None
                    if entry_dt and sl_dt and entry_dt > sl_dt:
                        gap_mins = (entry_dt - sl_dt).total_seconds() / 60
                        post_sl_trades.append({
                            "day": day, "symbol": sym, "direction": direction,
                            "pnl": pnl, "exit_reason": exit_reason,
                            "gap_mins": gap_mins, "strategy": t.get("strategy", "?"),
                        })
                        if gap_mins <= 30:
                            post_sl_trades_30min.append(post_sl_trades[-1])
                except Exception:
                    pass

            # Record SL exits
            if "SL" in exit_reason:
                sl_exits.append({
                    "day": day, "symbol": sym, "direction": direction,
                    "pnl": pnl, "exit_time": exit_time_str,
                })
                sl_history[key] = exit_time_str

    # Report
    print("\n  Total trades: %d" % len(trades))
    print("  SL_HIT exits: %d" % len(sl_exits))
    total_sl_pnl = sum(s["pnl"] for s in sl_exits)
    print("  SL total PnL: Rs %+.0f" % total_sl_pnl)

    print("\n  --- Post-SL Re-Entries (ALL) ---")
    print("  Trades after SL on same symbol+direction: %d" % len(post_sl_trades))
    if post_sl_trades:
        wins = sum(1 for t in post_sl_trades if t["pnl"] > 0)
        losses = len(post_sl_trades) - wins
        total = sum(t["pnl"] for t in post_sl_trades)
        avg_gap = sum(t["gap_mins"] for t in post_sl_trades) / len(post_sl_trades)
        print("  Winners: %d, Losers: %d, Win Rate: %.1f%%" % (wins, losses, wins/len(post_sl_trades)*100))
        print("  Total PnL: Rs %+.0f" % total)
        print("  Avg gap from SL: %.0f min" % avg_gap)

        # Breakdown by gap time
        for bucket_name, bucket_max in [("0-10 min", 10), ("10-30 min", 30), ("30-60 min", 60), ("60+ min", 9999)]:
            bucket_min = 0 if bucket_max == 10 else (10 if bucket_max == 30 else (30 if bucket_max == 60 else 60))
            bucket = [t for t in post_sl_trades if bucket_min <= t["gap_mins"] < bucket_max]
            if bucket:
                b_wins = sum(1 for t in bucket if t["pnl"] > 0)
                b_total = sum(t["pnl"] for t in bucket)
                print("    %s: %d trades, %d wins (%.0f%%), PnL Rs %+.0f" % (
                    bucket_name, len(bucket), b_wins, b_wins/len(bucket)*100, b_total))

        # Show each post-SL trade
        print("\n  Trade-by-trade:")
        for t in post_sl_trades:
            print("    %s %s %s gap=%3.0fm PnL=Rs %+8.0f %s [%s]" % (
                t["day"], t["symbol"], t["direction"],
                t["gap_mins"], t["pnl"], t["exit_reason"], t["strategy"]))
    else:
        print("  No post-SL re-entries found")

    # Recommendation
    print("\n  --- RECOMMENDATION ---")
    if post_sl_trades:
        quick_reentries = [t for t in post_sl_trades if t["gap_mins"] < 30]
        if quick_reentries:
            quick_pnl = sum(t["pnl"] for t in quick_reentries)
            quick_wins = sum(1 for t in quick_reentries if t["pnl"] > 0)
            quick_wr = quick_wins / len(quick_reentries) * 100
            print("  Quick re-entries (<30min): %d trades, %.0f%% WR, Rs %+.0f" % (
                len(quick_reentries), quick_wr, quick_pnl))
            if quick_pnl < 0 and quick_wr < 50:
                print("  >>> IMPLEMENT 30min SL re-entry block (saves Rs %+.0f)" % abs(quick_pnl))
            elif quick_pnl < 0:
                print("  >>> CONSIDER 30min block (net negative but some winners)")
            else:
                print("  >>> DO NOT block — quick re-entries are profitable")
        else:
            print("  No quick re-entries (<30min) — block not needed")
    else:
        print("  No post-SL re-entries — block not needed")


# Run analysis
analyze_bot("EQUITY BOT", "/root/algo_trading/paper_trades/portfolio_state.json")
analyze_bot("STOCK BOT", "/root/algo_trading/stock_paper_trades/stock_portfolio_state.json")
analyze_bot("COMMODITY BOT", "/root/algo_trading/paper_trades_commodity/commodity_portfolio_state.json")
