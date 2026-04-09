#!/usr/bin/env python3
"""Backtest v2: Include ALL signals (no score filter), compare executed vs all."""
import csv
from collections import defaultdict
from datetime import datetime

signals = []
for date in ["20260311", "20260312"]:
    fn = "/root/algo_trading/paper_trades/signals_" + date + ".csv"
    with open(fn) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_date"] = date
            try:
                row["_ts"] = datetime.fromisoformat(row["timestamp"])
                row["_premium"] = float(row.get("premium", 0) or 0)
                row["_spot"] = float(row.get("spot", 0) or 0)
                row["_iv"] = float(row.get("iv", 0) or 0)
                row["_score"] = float(row.get("quality_score", 0) or 0)
                row["_delta"] = float(row.get("delta", 0) or 0)
                row["_dte"] = int(float(row.get("dte", 0) or 0))
                row["_strike"] = float(row.get("strike", 0) or 0)
                row["_executed"] = row.get("executed", "") == "True"
            except:
                continue
            signals.append(row)

print("Loaded " + str(len(signals)) + " signals")

# Build premium time series per symbol+direction+strike+date
series = defaultdict(list)
for s in signals:
    direction = "PE" if "PE" in s.get("type", "") else "CE"
    key = (s["_date"], s["symbol"], direction, s["_strike"])
    series[key].append((s["_ts"], s["_premium"], s["_spot"]))

for k in series:
    series[k].sort(key=lambda x: x[0])

# SIMULATE
results = []
seen = set()

for s in signals:
    strat = s.get("strategy", "")
    sym = s["symbol"]
    stype = s.get("type", "")
    direction = "PE" if "PE" in stype else "CE"
    strike = s["_strike"]
    entry_time = s["_ts"]
    entry_prem = s["_premium"]
    is_buy = "BUY" in stype
    executed = s["_executed"]

    if entry_prem <= 0:
        continue

    # Dedup: one per 10-min window per strat
    dedup_key = (s["_date"], sym, strat, direction, strike, entry_time.hour, entry_time.minute // 10)
    if dedup_key in seen:
        continue
    seen.add(dedup_key)

    key = (s["_date"], sym, direction, strike)
    if key not in series:
        continue

    peaks = {5: entry_prem, 10: entry_prem, 15: entry_prem, 30: entry_prem, 60: entry_prem}
    finals = {5: entry_prem, 10: entry_prem, 15: entry_prem, 30: entry_prem, 60: entry_prem}

    for ts, prem, spot in series[key]:
        if ts <= entry_time:
            continue
        elapsed = (ts - entry_time).total_seconds() / 60
        for w in [5, 10, 15, 30, 60]:
            if elapsed <= w:
                if prem > peaks[w]: peaks[w] = prem
                finals[w] = prem

    results.append({
        "strat": strat, "sym": sym, "type": stype, "dir": direction,
        "time": entry_time.strftime("%H:%M"), "entry": entry_prem,
        "score": s["_score"], "iv": s["_iv"] * 100, "dte": s["_dte"],
        "is_buy": is_buy, "date": s["_date"], "executed": executed,
        "p5": peaks[5], "p10": peaks[10], "p15": peaks[15], "p30": peaks[30], "p60": peaks[60],
        "f5": finals[5], "f10": finals[10], "f15": finals[15], "f30": finals[30], "f60": finals[60],
    })

print("Simulated entries: " + str(len(results)))

def print_table(label, data):
    if not data:
        print(label + ": No data")
        return
    print("\n" + label + " (N=" + str(len(data)) + ")")
    print("Hold   | Avg Peak% | Avg Final% | P(profit) | P(peak>5%) | P(peak>10%)")
    print("-" * 78)
    for w in [5, 10, 15, 30, 60]:
        pk = [(r["p"+str(w)] - r["entry"]) / r["entry"] * 100 for r in data]
        fn = [(r["f"+str(w)] - r["entry"]) / r["entry"] * 100 for r in data]
        avg_pk = sum(pk)/len(pk)
        avg_fn = sum(fn)/len(fn)
        p_profit = sum(1 for x in fn if x > 0) / len(fn)
        p_pk5 = sum(1 for x in pk if x > 5) / len(pk)
        p_pk10 = sum(1 for x in pk if x > 10) / len(pk)
        print("{:5s}  | {:>8.1f}% | {:>9.1f}% | {:>8.0%}  | {:>9.0%}  | {:>10.0%}".format(
            str(w)+"min", avg_pk, avg_fn, p_profit, p_pk5, p_pk10))

# ============ MAIN RESULTS ============
print("\n" + "="*80)
print("BACKTEST v2: CPR vs GAMMA BLAST - ALL signals (no score filter)")
print("="*80)

for strat in ["CPR", "Gamma Blast"]:
    buy_data = [r for r in results if r["strat"] == strat and r["is_buy"]]
    print_table(strat + " ALL BUY", buy_data)

# EXECUTED ONLY
print("\n" + "="*80)
print("EXECUTED TRADES ONLY - What bot actually traded")
print("="*80)

for strat in ["CPR", "Gamma Blast"]:
    buy_data = [r for r in results if r["strat"] == strat and r["is_buy"] and r["executed"]]
    print_table(strat + " EXECUTED BUY", buy_data)

# BY DATE - EXECUTED
print("\n" + "="*80)
print("BY DATE - EXECUTED ONLY")
print("="*80)

for d, dl in [("20260311", "Mar 11"), ("20260312", "Mar 12")]:
    for strat in ["CPR", "Gamma Blast"]:
        data = [r for r in results if r["strat"] == strat and r["is_buy"] and r["executed"] and r["date"] == d]
        print_table(dl + " | " + strat + " EXEC", data)

# GAMMA BLAST DETAIL
print("\n" + "="*80)
print("GAMMA BLAST - Individual trade detail (EXECUTED)")
print("="*80)

gb_trades = [r for r in results if r["strat"] == "Gamma Blast" and r["is_buy"] and r["executed"]]
print("\nDate       | Time  | Symbol     | Dir | Entry   | Peak5%  | Peak15% | Peak30% | Final30% | Score")
print("-" * 105)
for r in gb_trades:
    p5 = (r["p5"] - r["entry"]) / r["entry"] * 100
    p15 = (r["p15"] - r["entry"]) / r["entry"] * 100
    p30 = (r["p30"] - r["entry"]) / r["entry"] * 100
    f30 = (r["f30"] - r["entry"]) / r["entry"] * 100
    print("{} | {} | {:10s} | {} | {:7.2f} | {:+6.1f}% | {:+6.1f}% | {:+6.1f}% | {:+7.1f}% | {:5.1f}".format(
        r["date"], r["time"], r["sym"], r["dir"],
        r["entry"], p5, p15, p30, f30, r["score"]))

# CPR DETAIL
print("\n" + "="*80)
print("CPR - Individual trade detail (EXECUTED)")
print("="*80)

cpr_trades = [r for r in results if r["strat"] == "CPR" and r["is_buy"] and r["executed"]]
print("\nDate       | Time  | Symbol     | Dir | Entry   | Peak5%  | Peak15% | Peak30% | Final30% | Score")
print("-" * 105)
for r in cpr_trades:
    p5 = (r["p5"] - r["entry"]) / r["entry"] * 100
    p15 = (r["p15"] - r["entry"]) / r["entry"] * 100
    p30 = (r["p30"] - r["entry"]) / r["entry"] * 100
    f30 = (r["f30"] - r["entry"]) / r["entry"] * 100
    marker = " ***" if p30 > 10 else ""
    print("{} | {} | {:10s} | {} | {:7.2f} | {:+6.1f}% | {:+6.1f}% | {:+6.1f}% | {:+7.1f}% | {:5.1f}{}".format(
        r["date"], r["time"], r["sym"], r["dir"],
        r["entry"], p5, p15, p30, f30, r["score"], marker))

# SELL signals
print("\n" + "="*80)
print("SELL TRADES - All strategies")
print("="*80)

for strat in ["CPR", "Gamma Blast"]:
    sell_data = [r for r in results if r["strat"] == strat and not r["is_buy"]]
    if sell_data:
        print_table(strat + " SELL", sell_data)

# HEAD-TO-HEAD
print("\n" + "="*80)
print("Mar 11 HEAD-TO-HEAD: GB vs CPR (same symbol, within 10 min)")
print("="*80)

gb_mar11 = [r for r in results if r["strat"] == "Gamma Blast" and r["is_buy"] and r["date"] == "20260311"]
cpr_mar11 = [r for r in results if r["strat"] == "CPR" and r["is_buy"] and r["date"] == "20260311"]

for gb in gb_mar11:
    gb_min = int(gb["time"][:2])*60 + int(gb["time"][3:])
    matches = []
    for c in cpr_mar11:
        c_min = int(c["time"][:2])*60 + int(c["time"][3:])
        if c["sym"] == gb["sym"] and c["dir"] == gb["dir"] and abs(gb_min - c_min) <= 10:
            matches.append(c)
    if matches:
        cpr = matches[0]
        print("\n{} {} {} @ {}".format(gb["sym"], gb["dir"], gb["date"], gb["time"]))
        for w in [5, 10, 15, 30, 60]:
            gb_pk = (gb["p"+str(w)] - gb["entry"]) / gb["entry"] * 100
            gb_fn = (gb["f"+str(w)] - gb["entry"]) / gb["entry"] * 100
            cp_pk = (cpr["p"+str(w)] - cpr["entry"]) / cpr["entry"] * 100
            cp_fn = (cpr["f"+str(w)] - cpr["entry"]) / cpr["entry"] * 100
            winner = "GB" if gb_fn > cp_fn else "CPR"
            print("  {:5s}: GB peak={:+.1f}% final={:+.1f}% | CPR peak={:+.1f}% final={:+.1f}% --> {}".format(
                str(w)+"min", gb_pk, gb_fn, cp_pk, cp_fn, winner))

# KEY FINDING: Gamma Blast on TRENDING day
print("\n" + "="*80)
print("KEY COMPARISON: Trending (Mar 11) vs Sideways (Mar 12)")
print("="*80)
for d, dl in [("20260311", "Mar 11 TRENDING"), ("20260312", "Mar 12 SIDEWAYS")]:
    all_buy = [r for r in results if r["is_buy"] and r["date"] == d]
    if all_buy:
        print_table(dl + " ALL BUY", all_buy)
