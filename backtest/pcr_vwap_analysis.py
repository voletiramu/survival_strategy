#!/usr/bin/env python3
"""PCR+VWAP Strategy Redesign Analysis.

Analyzes live PCR data from signal CSV files and pipeline logs to:
1. Map PCR values across the day for each symbol
2. Identify what PCR levels/shifts actually predict profitable moves
3. Test redesigned entry/exit logic
4. Output findings for strategy redesign

Data sources:
- Signal CSVs: D:/AlgoTrading/data/live/ or VPS logs
- Pipeline PCR logs from unified_paper_*.log
"""
import re
import os
import json
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

# Parse PCR data from log files
def parse_pcr_from_log(log_path):
    """Extract PCR values with timestamps from pipeline logs."""
    pcr_data = defaultdict(list)  # symbol -> [(time, pcr, call_oi, put_oi, oi_change_ce, oi_change_pe, spot)]

    pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*Pipeline.*'
        r'(NIFTY|BANKNIFTY|SENSEX).*'
        r'PCR=([\d.]+).*'
        r'Call OI=([\d,.]+).*'
        r'Put OI=([\d,.]+).*'
        r'OI Change: CE=([+\-\d,.]+)\s+PE=([+\-\d,.]+).*'
        r'Spot=([\d.]+)'
    )

    with open(log_path, 'r', errors='ignore') as f:
        for line in f:
            if 'Pipeline' not in line or 'PCR=' not in line:
                continue
            m = pattern.search(line)
            if m:
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                sym = m.group(2)
                pcr = float(m.group(3))
                call_oi = float(m.group(4).replace(',', ''))
                put_oi = float(m.group(5).replace(',', ''))
                oi_ce = float(m.group(6).replace(',', '').replace('+', ''))
                oi_pe = float(m.group(7).replace(',', '').replace('+', ''))
                spot = float(m.group(8))
                pcr_data[sym].append({
                    'time': ts, 'pcr': pcr,
                    'call_oi': call_oi, 'put_oi': put_oi,
                    'oi_change_ce': oi_ce, 'oi_change_pe': oi_pe,
                    'spot': spot,
                })

    return pcr_data


def parse_spot_from_log(log_path):
    """Extract spot prices with timestamps."""
    spots = defaultdict(list)

    pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*'
        r'Spot:\s+([\d,.]+).*O:\s+([\d,.]+).*H:\s+([\d,.]+).*L:\s+([\d,.]+).*C:\s+([\d,.]+)'
    )

    # Match which symbol from preceding lines
    current_sym = None
    with open(log_path, 'r', errors='ignore') as f:
        for line in f:
            if '--- NIFTY' in line:
                current_sym = 'NIFTY'
            elif '--- BANKNIFTY' in line or '--- Bank Nifty' in line:
                current_sym = 'BANKNIFTY'
            elif '--- SENSEX' in line:
                current_sym = 'SENSEX'
            elif 'Spot:' in line and current_sym:
                m = pattern.search(line)
                if m:
                    ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                    spot = float(m.group(2).replace(',', ''))
                    high = float(m.group(4).replace(',', ''))
                    low = float(m.group(5).replace(',', ''))
                    close = float(m.group(6).replace(',', ''))
                    spots[current_sym].append({
                        'time': ts, 'spot': spot,
                        'high': high, 'low': low, 'close': close,
                    })

    return spots


def analyze_pcr_shifts(pcr_data, spots):
    """Analyze PCR shifts and their predictive power on spot direction."""

    for sym in pcr_data:
        data = pcr_data[sym]
        if len(data) < 5:
            print(f"\n{sym}: Insufficient data ({len(data)} points)")
            continue

        print(f"\n{'='*60}")
        print(f"  {sym} PCR ANALYSIS")
        print(f"{'='*60}")

        pcr_vals = [d['pcr'] for d in data]
        spots_list = [d['spot'] for d in data]

        print(f"\n  PCR Range: {min(pcr_vals):.3f} — {max(pcr_vals):.3f}")
        print(f"  PCR Mean: {np.mean(pcr_vals):.3f}")
        print(f"  PCR Std: {np.std(pcr_vals):.3f}")
        print(f"  Spot Range: {min(spots_list):,.0f} — {max(spots_list):,.0f}")
        print(f"  Day Move: {spots_list[-1] - spots_list[0]:+,.0f}")

        # PCR shift analysis (30-min window)
        shifts = []
        for i in range(10, len(data)):
            # Look back ~10 readings (~30 min at 3-min intervals)
            lookback = max(0, i - 10)
            pcr_start = data[lookback]['pcr']
            pcr_now = data[i]['pcr']
            shift = pcr_now - pcr_start

            # Look forward 10 readings for spot move
            forward = min(len(data) - 1, i + 10)
            spot_now = data[i]['spot']
            spot_future = data[forward]['spot']
            spot_move_pct = (spot_future - spot_now) / spot_now * 100

            # OI change acceleration
            oi_ce_now = data[i]['oi_change_ce']
            oi_pe_now = data[i]['oi_change_pe']
            delta_oi = oi_pe_now - oi_ce_now  # Positive = more put writing = bullish

            shifts.append({
                'time': data[i]['time'],
                'pcr': pcr_now,
                'pcr_shift': shift,
                'spot': spot_now,
                'spot_move_pct': spot_move_pct,
                'delta_oi': delta_oi,
                'oi_ce': oi_ce_now,
                'oi_pe': oi_pe_now,
            })

        # Print PCR timeline
        print(f"\n  PCR Timeline:")
        for i, d in enumerate(data):
            if i % 5 == 0:
                t = d['time'].strftime('%H:%M')
                print(f"    {t}  PCR={d['pcr']:.3f}  Spot={d['spot']:,.0f}  "
                      f"OI(CE+={d['oi_change_ce']:+,.0f} PE+={d['oi_change_pe']:+,.0f})")

        # Analyze: When PCR shifts > 0.05, what happens to spot?
        print(f"\n  PCR SHIFT ANALYSIS (30-min window):")
        bull_shifts = [s for s in shifts if s['pcr_shift'] > 0.05]
        bear_shifts = [s for s in shifts if s['pcr_shift'] < -0.05]

        if bull_shifts:
            print(f"\n    Bullish PCR shifts (> +0.05): {len(bull_shifts)}")
            correct = sum(1 for s in bull_shifts if s['spot_move_pct'] > 0)
            avg_move = np.mean([s['spot_move_pct'] for s in bull_shifts])
            print(f"    Spot moved UP: {correct}/{len(bull_shifts)} ({correct/len(bull_shifts)*100:.0f}%)")
            print(f"    Avg spot move: {avg_move:+.3f}%")

        if bear_shifts:
            print(f"\n    Bearish PCR shifts (< -0.05): {len(bear_shifts)}")
            correct = sum(1 for s in bear_shifts if s['spot_move_pct'] < 0)
            avg_move = np.mean([s['spot_move_pct'] for s in bear_shifts])
            print(f"    Spot moved DOWN: {correct}/{len(bear_shifts)} ({correct/len(bear_shifts)*100:.0f}%)")
            print(f"    Avg spot move: {avg_move:+.3f}%")

        # Dynamic threshold analysis
        print(f"\n  OPTIMAL PCR THRESHOLDS:")
        for thresh in [0.03, 0.05, 0.08, 0.10, 0.15]:
            bull = [s for s in shifts if s['pcr_shift'] > thresh]
            bear = [s for s in shifts if s['pcr_shift'] < -thresh]

            bull_correct = sum(1 for s in bull if s['spot_move_pct'] > 0) if bull else 0
            bear_correct = sum(1 for s in bear if s['spot_move_pct'] < 0) if bear else 0

            total = len(bull) + len(bear)
            correct = bull_correct + bear_correct

            bull_pct = bull_correct / len(bull) * 100 if bull else 0
            bear_pct = bear_correct / len(bear) * 100 if bear else 0

            print(f"    Shift ±{thresh:.2f}: Bull={len(bull)} ({bull_pct:.0f}% acc) "
                  f"Bear={len(bear)} ({bear_pct:.0f}% acc) Total={total}")

        # Delta OI analysis
        print(f"\n  DELTA OI ANALYSIS (Put OI change - Call OI change):")
        for d in shifts[::5]:
            t = d['time'].strftime('%H:%M')
            direction = 'BULL' if d['delta_oi'] > 0 else 'BEAR'
            print(f"    {t}  ΔOI={d['delta_oi']:+,.0f} ({direction})  "
                  f"PCR={d['pcr']:.3f}  Spot={d['spot']:,.0f}  "
                  f"→ spot {d['spot_move_pct']:+.3f}%")


def main():
    # Try to find log files
    log_paths = []

    # VPS logs copied locally
    log_dir = 'D:/AlgoTrading/data/live'
    if os.path.exists(log_dir):
        for f in sorted(os.listdir(log_dir)):
            if f.startswith('unified_paper_') and f.endswith('.log'):
                log_paths.append(os.path.join(log_dir, f))

    # Direct log path
    for d in ['2026031{}'.format(i) for i in range(2, 9)]:
        p = f'D:/AlgoTrading/data/live/unified_paper_{d}.log'
        if os.path.exists(p):
            log_paths.append(p)

    # If no local logs, try single file
    single = 'D:/AlgoTrading/temp_unified_log.log'
    if os.path.exists(single):
        log_paths.append(single)

    if not log_paths:
        print("No log files found! Copy from VPS first:")
        print("  scp root@65.20.69.104:/root/algo_trading/logs/unified_paper_20260318.log D:/AlgoTrading/temp_unified_log.log")
        return

    for log_path in log_paths:
        print(f"\n{'#'*60}")
        print(f"  Analyzing: {os.path.basename(log_path)}")
        print(f"{'#'*60}")

        pcr_data = parse_pcr_from_log(log_path)
        spots = parse_spot_from_log(log_path)

        if not pcr_data:
            print("  No PCR data found in log")
            continue

        analyze_pcr_shifts(pcr_data, spots)


if __name__ == '__main__':
    main()
