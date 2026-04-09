#!/usr/bin/env python3
"""Backtest simulation using actual live signal data collected every 20-30 seconds."""
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
                row["_premium"] = float(row.get("premium", 0))
                row["_spot"] = float(row.get("spot", 0))
                row["_iv"] = float(row.get("iv", 0))
                row["_score"] = float(row.get("quality_score", 0))
                row["_delta"] = float(row.get("delta", 0))
                row["_dte"] = int(float(row.get("dte", 0)))
                row["_strike"] = float(row.get("strike", 0))
                row["_target"] = float(row.get("target", 0))
                row["_sl"] = float(row.get("sl", 0))
                row["_pcr"] = float(row.get("pcr", 0))
                row["_theta"] = float(row.get("theta", 0))
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

print("Unique strike series: " + str(len(series)))

# SIMULATE: For each signal, compute forward premium at 5/10/15/30/60 min
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
    score = s["_score"]
    iv_pct = s["_iv"] * 100
    dte = s["_dte"]
    is_buy = "BUY" in stype

    if entry_prem <= 0 or score <= 0:
        continue

    # Dedup: one per 10-min window
    dedup_key = (s["_date"], sym, direction, strike, entry_time.hour, entry_time.minute // 10)
    if dedup_key in seen:
        continue
    seen.add(dedup_key)

    key = (s["_date"], sym, direction, strike)
    if key not in series:
        continue

    peaks = {5: entry_prem, 10: entry_prem, 15: entry_prem, 30: entry_prem, 60: entry_prem}
    lows = {5: entry_prem, 10: entry_prem, 15: entry_prem, 30: entry_prem, 60: entry_prem}
    finals = {5: entry_prem, 10: entry_prem, 15: entry_prem, 30: entry_prem, 60: entry_prem}

    for ts, prem, spot in series[key]:
        if ts <= entry_time:
            continue
        elapsed = (ts - entry_time).total_seconds() / 60
        for w in [5, 10, 15, 30, 60]:
            if elapsed <= w:
                if prem > peaks[w]: peaks[w] = prem
                if prem < lows[w]: lows[w] = prem
                finals[w] = prem

    results.append({
        "strat": strat, "sym": sym, "type": stype, "dir": direction,
        "time": entry_time.strftime("%H:%M"), "entry": entry_prem,
        "score": score, "iv": iv_pct, "dte": dte, "pcr": s["_pcr"],
        "is_buy": is_buy, "date": s["_date"],
        "p5": peaks[5], "p10": peaks[10], "p15": peaks[15], "p30": peaks[30], "p60": peaks[60],
        "l5": lows[5], "l10": lows[10],
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
print("BACKTEST: CPR vs GAMMA BLAST — What if we held longer?")
print("="*80)

for strat in ["CPR", "Gamma Blast"]:
    buy_data = [r for r in results if r["strat"] == strat and r["is_buy"]]
    print_table(strat + " (ALL BUY signals)", buy_data)

# BY DATE
print("\n" + "="*80)
print("BY DATE — Mar 11 vs Mar 12")
print("="*80)

for d, dl in [("20260311", "Mar 11"), ("20260312", "Mar 12")]:
    for strat in ["CPR", "Gamma Blast"]:
        data = [r for r in results if r["strat"] == strat and r["is_buy"] and r["date"] == d]
        print_table(dl + " | " + strat, data)

# BY IV
print("\n" + "="*80)
print("BY IV RANGE — CPR BUY only")
print("="*80)

cpr_buys = [r for r in results if r["strat"] == "CPR" and r["is_buy"]]
for label, lo, hi in [("<15%", 0, 15), ("15-40%", 15, 40), ("40-60%", 40, 60), (">60%", 60, 999)]:
    data = [r for r in cpr_buys if lo <= r["iv"] < hi]
    print_table("IV " + label, data)

# BY SCORE
print("\n" + "="*80)
print("BY QUALITY SCORE — All BUY")
print("="*80)

all_buys = [r for r in results if r["is_buy"]]
for label, lo, hi in [("<50", 0, 50), ("50-69", 50, 70), ("70-84", 70, 85), ("85+", 85, 101)]:
    data = [r for r in all_buys if lo <= r["score"] < hi]
    print_table("Score " + label, data)

# BY SYMBOL
print("\n" + "="*80)
print("BY SYMBOL — All BUY CPR")
print("="*80)

for sym in sorted(set(r["sym"] for r in cpr_buys)):
    data = [r for r in cpr_buys if r["sym"] == sym]
    print_table(sym, data)

# GAMMA BLAST vs CPR — SAME entry windows
print("\n" + "="*80)
print("CPR vs GAMMA BLAST — Overlapping signals (same sym, same time)")
print("="*80)

cpr_map = {}
gb_map = {}
for r in results:
    if not r["is_buy"]:
        continue
    key = (r["date"], r["sym"], r["dir"], r["time"][:4])
    if r["strat"] == "CPR":
        cpr_map[key] = r
    elif r["strat"] == "Gamma Blast":
        gb_map[key] = r

overlap = set(cpr_map.keys()) & set(gb_map.keys())
print("Overlapping entries: " + str(len(overlap)))

if overlap:
    for w in [5, 10, 15, 30, 60]:
        c_peaks = [(cpr_map[k]["p"+str(w)] - cpr_map[k]["entry"]) / cpr_map[k]["entry"] * 100 for k in overlap]
        g_peaks = [(gb_map[k]["p"+str(w)] - gb_map[k]["entry"]) / gb_map[k]["entry"] * 100 for k in overlap]
        c_finals = [(cpr_map[k]["f"+str(w)] - cpr_map[k]["entry"]) / cpr_map[k]["entry"] * 100 for k in overlap]
        g_finals = [(gb_map[k]["f"+str(w)] - gb_map[k]["entry"]) / gb_map[k]["entry"] * 100 for k in overlap]
        print("{:5s}: CPR peak={:.1f}% final={:.1f}% | GB peak={:.1f}% final={:.1f}%".format(
            str(w)+"min",
            sum(c_peaks)/len(c_peaks), sum(c_finals)/len(c_finals),
            sum(g_peaks)/len(g_peaks), sum(g_finals)/len(g_finals)))

# SELL trades
print("\n" + "="*80)
print("SELL TRADES SIMULATION")
print("="*80)
sell_data = [r for r in results if not r["is_buy"]]
if sell_data:
    print("N=" + str(len(sell_data)))
    for w in [5, 10, 15, 30, 60]:
        # For SELL: profit when premium DROPS
        pk = [(r["entry"] - r["l"+str(min(w,10))]) / r["entry"] * 100 for r in sell_data]
        fn = [(r["entry"] - r["f"+str(w)]) / r["entry"] * 100 for r in sell_data]
        p_profit = sum(1 for x in fn if x > 0) / len(fn)
        print("{:5s}: Avg profit={:.1f}% P(profit)={:.0%}".format(
            str(w)+"min", sum(fn)/len(fn), p_profit))
