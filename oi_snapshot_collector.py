#!/usr/bin/env python3
"""
Daily OI Snapshot Collector.

Fetches option chain data with per-strike Open Interest from NSE/BSE
and saves to timestamped CSV files for building historical OI database.

Run this daily at ~3:25 PM IST (before market close) via:
  - Windows Task Scheduler (use run_oi_collector.bat)
  - Manual: python oi_snapshot_collector.py

After 100 days of collection, use the saved snapshots to backtest
OI strategies (Max Pain, OI Wall Bouncer) with REAL per-strike OI data.

Output: data/oi_history/oi_snapshot_{SYMBOL}_{YYYYMMDD}.csv
"""

import os
import sys
import logging
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OI_HISTORY_DIR = os.path.join(BASE_DIR, 'data', 'oi_history')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Symbols to collect
SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']


def collect_oi_snapshots():
    """Fetch and save OI snapshots for all symbols."""
    from market_data_pipeline import MarketDataPipeline
    from strategies.oi_strategies import calculate_max_pain

    os.makedirs(OI_HISTORY_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print("=" * 70)
    print("  OI SNAPSHOT COLLECTOR")
    print(f"  Date: {today}  Time: {timestamp}")
    print("=" * 70)

    # Initialize pipeline and fetch data
    print("\n  Initializing MarketDataPipeline...")
    pipeline = MarketDataPipeline()

    print("  Fetching option chain data from NSE/BSE...")
    pipeline.fetch_once()

    # Get VIX
    vix = pipeline.get_vix() or 0

    saved_count = 0

    for symbol in SYMBOLS:
        print(f"\n  --- {symbol} ---")

        # Get snapshot and chain data
        snapshot = pipeline.get_snapshot(symbol)
        spot = snapshot.get('spot', 0)
        pcr = snapshot.get('pcr', 0)

        if not spot or spot <= 0:
            print(f"  [WARN] No spot data for {symbol}, skipping")
            continue

        # Get per-strike chain
        chain = None
        with pipeline._lock:
            chain = pipeline._option_chains.get(symbol)

        if not chain:
            print(f"  [WARN] No option chain for {symbol}, skipping")
            continue

        ce_contracts = chain.get('CE', [])
        pe_contracts = chain.get('PE', [])
        expiry = chain.get('expiry', 'N/A')

        if not ce_contracts and not pe_contracts:
            print(f"  [WARN] Empty chain for {symbol}, skipping")
            continue

        # Build per-strike combined data
        strikes = {}

        for c in ce_contracts:
            s = c.get('strike', 0)
            if s not in strikes:
                strikes[s] = {}
            strikes[s]['ce_oi'] = c.get('oi', 0)
            strikes[s]['ce_oi_change'] = c.get('oi_change', 0)
            strikes[s]['ce_ltp'] = c.get('ltp', 0)
            strikes[s]['ce_iv'] = c.get('iv', 0)
            strikes[s]['ce_volume'] = c.get('volume', 0)

        for p in pe_contracts:
            s = p.get('strike', 0)
            if s not in strikes:
                strikes[s] = {}
            strikes[s]['pe_oi'] = p.get('oi', 0)
            strikes[s]['pe_oi_change'] = p.get('oi_change', 0)
            strikes[s]['pe_ltp'] = p.get('ltp', 0)
            strikes[s]['pe_iv'] = p.get('iv', 0)
            strikes[s]['pe_volume'] = p.get('volume', 0)

        if not strikes:
            print(f"  [WARN] No strikes for {symbol}, skipping")
            continue

        # Compute max pain from real OI
        chain_data_for_mp = []
        for s, data in strikes.items():
            if data.get('ce_oi', 0) > 0:
                chain_data_for_mp.append({
                    'strikePrice': s,
                    'optionType': 'CE',
                    'openInterest': data.get('ce_oi', 0),
                })
            if data.get('pe_oi', 0) > 0:
                chain_data_for_mp.append({
                    'strikePrice': s,
                    'optionType': 'PE',
                    'openInterest': data.get('pe_oi', 0),
                })

        strike_interval = 50 if symbol in ('NIFTY',) else 100
        max_pain = calculate_max_pain(chain_data_for_mp, spot, strike_interval)

        # Save CSV
        csv_path = os.path.join(OI_HISTORY_DIR, f'oi_snapshot_{symbol}_{today}.csv')
        with open(csv_path, 'w', encoding='utf-8') as f:
            f.write('strike,ce_oi,ce_oi_change,ce_ltp,ce_iv,ce_volume,'
                    'pe_oi,pe_oi_change,pe_ltp,pe_iv,pe_volume,'
                    'spot,max_pain,pcr,vix,expiry,timestamp\n')

            for s in sorted(strikes.keys()):
                d = strikes[s]
                f.write(f"{s},"
                        f"{d.get('ce_oi', 0)},{d.get('ce_oi_change', 0)},"
                        f"{d.get('ce_ltp', 0)},{d.get('ce_iv', 0)},{d.get('ce_volume', 0)},"
                        f"{d.get('pe_oi', 0)},{d.get('pe_oi_change', 0)},"
                        f"{d.get('pe_ltp', 0)},{d.get('pe_iv', 0)},{d.get('pe_volume', 0)},"
                        f"{spot},{max_pain},{pcr},{vix},{expiry},{timestamp}\n")

        saved_count += 1
        total_oi = sum(d.get('ce_oi', 0) + d.get('pe_oi', 0) for d in strikes.values())

        print(f"  Saved: {csv_path}")
        print(f"  Spot: {spot:.2f}  |  Max Pain: {max_pain:.0f}  |  PCR: {pcr:.3f}")
        print(f"  Strikes: {len(strikes)}  |  Total OI: {total_oi:,}  |  Expiry: {expiry}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  COLLECTION COMPLETE")
    print(f"  Saved {saved_count}/{len(SYMBOLS)} snapshots to {OI_HISTORY_DIR}")
    print(f"  VIX: {vix:.2f}")

    # Count existing snapshots
    existing = [f for f in os.listdir(OI_HISTORY_DIR) if f.endswith('.csv')]
    unique_dates = len(set(f.split('_')[-1].replace('.csv', '') for f in existing))
    print(f"  Total snapshots on disk: {len(existing)} files ({unique_dates} unique dates)")
    print(f"  Target: 100 dates for reliable OI backtest")
    print(f"{'='*70}\n")

    return saved_count


if __name__ == '__main__':
    try:
        collect_oi_snapshots()
    except Exception as e:
        logger.error(f"OI Snapshot collection failed: {e}", exc_info=True)
        sys.exit(1)
