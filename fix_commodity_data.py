#!/usr/bin/env python3
"""Fix commodity historical data using Kite (Zerodha) instead of Angel.

Problem: Bot uses Angel data which maps to wrong contract (SILVER full vs SILVERM mini).
Solution: Download correct futures data from Kite for GOLDM, SILVERM, CRUDEOILM.
"""
from kiteconnect import KiteConnect
import json, csv, os
from datetime import datetime, timedelta

token = json.load(open("/root/algo_trading/config/zerodha_creds.json"))
creds = json.load(open("/root/algo_trading/config/zerodha_token.json"))
kite = KiteConnect(api_key=token["api_key"])
kite.set_access_token(creds["access_token"])

instruments = kite.instruments("MCX")

# Find nearest futures
comms = {}
for comm_name in ["GOLDM", "SILVERM", "CRUDEOILM"]:
    matches = [i for i in instruments
               if i.get("tradingsymbol", "").startswith(comm_name)
               and i.get("instrument_type") == "FUT"]
    if matches:
        nearest = min(matches, key=lambda x: x.get("expiry", datetime(2099, 1, 1)))
        comms[comm_name] = nearest
        ts = nearest.get("tradingsymbol", "?")
        tk = nearest.get("instrument_token", "?")
        ex = nearest.get("expiry", "?")
        print(f"  {comm_name}: {ts} token={tk} expiry={ex}")

data_dir = "/root/algo_trading/data/options"
from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
to_date = datetime.now().strftime("%Y-%m-%d")

for comm_name, inst in comms.items():
    tk = inst["instrument_token"]
    print(f"\nDownloading {comm_name} (token={tk})...")

    hist = kite.historical_data(
        instrument_token=tk,
        from_date=from_date,
        to_date=to_date,
        interval="day"
    )
    print(f"  Got {len(hist)} candles")

    if not hist:
        print(f"  SKIP: no data")
        continue

    filepath = os.path.join(data_dir, f"{comm_name}_spot_one_day_2000d.csv")

    # Read existing to merge
    existing = []
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                existing.append(row)
        print(f"  Existing: {len(existing)} rows")

    # Convert new data
    new_dates = set()
    new_rows = []
    for h in hist:
        dt = h["date"]
        ds = dt.strftime("%Y-%m-%d %H:%M:%S+05:30") if hasattr(dt, "strftime") else str(dt)
        new_rows.append([ds, h["open"], h["high"], h["low"], h["close"], h.get("volume", 0)])
        new_dates.add(ds[:10])

    # Merge: keep old data not in new dates + new data
    merged = [r for r in existing if r[0][:10] not in new_dates]
    merged.extend(new_rows)
    merged.sort(key=lambda x: x[0])

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DateTime", "Open", "High", "Low", "Close", "Volume"])
        writer.writerows(merged)

    print(f"  Saved {len(merged)} rows to {filepath}")

    # Verify last 3 rows
    for r in merged[-3:]:
        pivot = (float(r[2]) + float(r[3]) + float(r[4])) / 3
        print(f"  {r[0][:10]} O={r[1]} H={r[2]} L={r[3]} C={r[4]} Pivot={pivot:.0f}")

print("\nDone! Restart algo-trading to pick up new data.")
