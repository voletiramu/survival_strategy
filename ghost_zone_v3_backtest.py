"""
Ghost Zone V3 Backtest - Institutional Footprint Strategy
=========================================================
Based on Manish Maheshwari (Ghost Trader) F&O Masterclass:

Key differences from V2:
  - Zones from single HIGH-VOLUME candles (Injection/Bat/Belan), NOT RBD/DBR
  - Volume threshold: 3x-5x average (not 1.1x)
  - Entry at 50% of zone after fake breakout / liquidity sweep
  - Exhaustion-based trailing stops
  - Volume Profile shape detection (P/b/D)

Usage:
    python ghost_zone_v3_backtest.py
    python ghost_zone_v3_backtest.py --crypto-only --days 120
"""

import sys
import os
import argparse
import time
import warnings
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from strategies.ghost_zone_v3 import GhostZoneV3, compute_volume_profile

# ── CONFIG ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 300000
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
CRYPTO_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
CRYPTO_FEE_RATE = 0.001  # 0.1%

REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT_PCT = 0.02   # 2%
RISK_PER_TRADE_PCT = 0.01     # 1% risk per trade
TRADE_COOLDOWN_BARS = 12      # Min 12 bars (1hr on 5m) between trades
MAX_ZONE_RETESTS = 2          # Max trades per zone (fresh zones are key)


# ── DATA FETCHING ──────────────────────────────────────────────────────────────

def fetch_binance(symbol, interval='5m', days=60):
    """Fetch crypto candles from Binance public API."""
    print(f"  Fetching {symbol} ({interval}, {days}d)...", end=" ", flush=True)
    all_data = []
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

    while start_time < end_time:
        params = {
            'symbol': symbol, 'interval': interval,
            'startTime': start_time, 'endTime': end_time, 'limit': 1000,
        }
        try:
            r = requests.get(BINANCE_BASE, params=params, timeout=15)
            data = r.json()
            if not data or isinstance(data, dict):
                break
            all_data.extend(data)
            start_time = data[-1][0] + 1
            time.sleep(0.15)
        except Exception as e:
            print(f"Error: {e}")
            break

    if not all_data:
        print("FAILED")
        return None

    df = pd.DataFrame(all_data, columns=[
        'timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
        'close_time', 'quote_vol', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignore'
    ])
    df['Date'] = pd.to_datetime(df['timestamp'], unit='ms')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        df[col] = df[col].astype(float)
    df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].drop_duplicates('Date')
    df = df.sort_values('Date').reset_index(drop=True)
    print(f"{len(df)} candles ({df['Date'].iloc[0].strftime('%Y-%m-%d')} to "
          f"{df['Date'].iloc[-1].strftime('%Y-%m-%d')})")
    return df


# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────

def backtest_crypto_v3(df_4h, df_5m, symbol, capital=INITIAL_CAPITAL):
    """
    Backtest Ghost Zone V3 on crypto using multi-TF institutional footprint.

    4H: Zone detection (Injection/Bat/Belan with 3x+ volume)
    5m: Entry timing at 50% of zone after fake breakout
    """
    df = df_5m  # Entry timeframe
    print(f"\n  === Ghost Zone V3 Crypto: {symbol} ===")

    gz = GhostZoneV3()

    # Detect zones on 4H data (institutional footprint is meaningful on higher TF)
    # Increase zone expiry for 4H zones viewed on 5m chart
    gz.ZONE_EXPIRY_BARS = 5000  # ~17 days on 5m
    zones = gz.detect_zones(df_4h)
    demand_zones = [z for z in zones if z.zone_type == 'demand']
    supply_zones = [z for z in zones if z.zone_type == 'supply']
    inj_count = sum(1 for z in zones if z.trigger_type == 'injection')
    bat_count = sum(1 for z in zones if z.trigger_type == 'bat')
    bel_count = sum(1 for z in zones if z.trigger_type == 'belan')
    unmit_count = sum(1 for z in zones if z.unmitigated)

    print(f"  Zones detected on 4H: {len(zones)} "
          f"(Demand: {len(demand_zones)}, Supply: {len(supply_zones)})")
    print(f"    Injection: {inj_count} | Bat: {bat_count} | Belan: {bel_count} "
          f"| Unmitigated: {unmit_count}")

    if not zones:
        print("  No zones found -- skipping")
        return None

    # Convert 4H zone created_time to timestamps for 5m alignment
    zone_times = {}
    for z in zones:
        if isinstance(z.created_time, (pd.Timestamp, np.datetime64)):
            zone_times[id(z)] = pd.Timestamp(z.created_time)
        elif 'Date' in df_4h.columns and z.created_at < len(df_4h):
            zone_times[id(z)] = pd.Timestamp(df_4h['Date'].iloc[z.created_at])
        else:
            zone_times[id(z)] = pd.Timestamp.min

    # Precompute 5m indicators for entry
    atr_series = gz.atr(df, 14)

    # Backtest state
    equity = capital
    peak = capital
    max_dd = 0
    trades = []
    position = None
    daily_trades = 0
    daily_pnl = 0
    current_day = None
    equity_curve = []
    last_trade_close_bar = -999
    zone_trade_counts = {}

    warmup = max(gz.VOL_AVG_PERIOD + 14, gz.FAKE_BREAKOUT_LOOKBACK + 5)

    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        bar_time = bar['Date']
        day = bar_time.date()

        # Reset daily counters
        if day != current_day:
            daily_trades = 0
            daily_pnl = 0
            current_day = day

        # Check daily loss limit
        if daily_pnl < -capital * DAILY_LOSS_LIMIT_PCT:
            equity_curve.append(equity)
            continue

        # Get active zones: 4H zones created before this 5m bar time
        bar_ts = pd.Timestamp(bar_time)
        active_zones = [z for z in zones
                       if (zone_times.get(id(z), pd.Timestamp.max) < bar_ts
                           and not z.broken)]
        # Also check if zone is broken by current price
        current_atr_4h = gz.atr(df_4h, 14).iloc[-1] if len(df_4h) > 14 else 0
        active_zones = [z for z in active_zones
                       if not (z.zone_type == 'demand' and bar['Close'] < z.low - current_atr_4h * 0.5)
                       and not (z.zone_type == 'supply' and bar['Close'] > z.high + current_atr_4h * 0.5)]

        if not active_zones:
            equity_curve.append(equity)
            continue

        # ── CHECK EXISTING POSITION ──
        if position is not None:
            close = bar['Close']
            high = bar['High']
            low = bar['Low']
            current_atr = atr_series.iloc[i] if not pd.isna(atr_series.iloc[i]) else 0

            hit_sl = False
            hit_target = False
            hit_exhaustion = False
            exit_price = None

            # Check SL/Target
            if position['direction'] == 1:  # LONG
                if low <= position['sl']:
                    hit_sl = True
                    exit_price = position['sl']
                elif high >= position['target']:
                    hit_target = True
                    exit_price = position['target']
            else:  # SHORT
                if high >= position['sl']:
                    hit_sl = True
                    exit_price = position['sl']
                elif low <= position['target']:
                    hit_target = True
                    exit_price = position['target']

            # Check exhaustion
            if not hit_sl and not hit_target:
                if gz.check_exhaustion(position, close, symbol):
                    hit_exhaustion = True
                    exit_price = close

            # Trailing stop update
            if not hit_sl and not hit_target and not hit_exhaustion and current_atr > 0:
                new_sl = gz.should_trail(position, close, current_atr)
                if new_sl is not None:
                    position['sl'] = new_sl

            if hit_sl or hit_target or hit_exhaustion:
                # Calculate PnL
                if position['direction'] == 1:
                    pnl_pct = (exit_price - position['entry']) / position['entry'] * 100
                else:
                    pnl_pct = (position['entry'] - exit_price) / position['entry'] * 100

                alloc = position.get('allocation', equity * 0.33)
                pnl = alloc * (pnl_pct / 100) - alloc * CRYPTO_FEE_RATE

                equity += pnl
                daily_pnl += pnl

                exit_type = 'SL' if hit_sl else ('TARGET' if hit_target else 'EXHAUST')

                trades.append({
                    'symbol': symbol,
                    'signal_type': position['type'],
                    'direction': 'LONG' if position['direction'] == 1 else 'SHORT',
                    'entry_price': position['entry'],
                    'exit_price': exit_price,
                    'sl': position['original_sl'],
                    'target': position['target'],
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'exit_type': exit_type,
                    'entry_time': df['Date'].iloc[position['entry_idx']],
                    'exit_time': bar_time,
                    'hold_bars': i - position['entry_idx'],
                    'zone_quality': position.get('quality', 0),
                    'trigger_type': position.get('trigger', ''),
                    'fake_breakout': position.get('fake_breakout', False),
                    'reason': position.get('reason', ''),
                })

                last_trade_close_bar = i
                position = None

        # ── CHECK FOR NEW ENTRIES ──
        cooldown_ok = (i - last_trade_close_bar) >= TRADE_COOLDOWN_BARS
        if position is None and daily_trades < MAX_TRADES_PER_DAY and cooldown_ok:
            signals = gz.find_entries(df, active_zones, i, atr_series)

            if signals:
                # Filter by zone retest limits
                valid_signals = []
                for sig in signals:
                    zone_key = sig['zone'].created_at
                    retest_count = zone_trade_counts.get(zone_key, 0)
                    if retest_count < MAX_ZONE_RETESTS:
                        valid_signals.append(sig)

                if valid_signals:
                    sig = valid_signals[0]  # Best quality signal

                    # Track zone retest
                    zone_key = sig['zone'].created_at
                    zone_trade_counts[zone_key] = zone_trade_counts.get(zone_key, 0) + 1

                    # Position sizing: risk 1% of equity
                    risk_amount = equity * RISK_PER_TRADE_PCT
                    risk_per_unit = abs(sig['entry'] - sig['sl'])
                    if risk_per_unit > 0:
                        qty = risk_amount / risk_per_unit
                        alloc = min(qty * sig['entry'], equity * 0.40)
                    else:
                        alloc = equity * 0.10

                    position = {
                        'direction': sig['direction'],
                        'entry': sig['entry'],
                        'sl': sig['sl'],
                        'original_sl': sig['sl'],  # Keep original for trailing
                        'target': sig['target'],
                        'entry_idx': i,
                        'type': sig['type'],
                        'quality': sig['quality'],
                        'trigger': sig.get('trigger', ''),
                        'fake_breakout': sig.get('fake_breakout', False),
                        'reason': sig['reason'],
                        'allocation': alloc,
                    }
                    equity -= alloc * CRYPTO_FEE_RATE  # Entry fee
                    daily_trades += 1

        # Track drawdown
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)

    # Close final position at last price
    if position is not None:
        close = df['Close'].iloc[-1]
        if position['direction'] == 1:
            pnl_pct = (close - position['entry']) / position['entry'] * 100
        else:
            pnl_pct = (position['entry'] - close) / position['entry'] * 100
        alloc = position.get('allocation', equity * 0.33)
        pnl = alloc * (pnl_pct / 100) - alloc * CRYPTO_FEE_RATE
        equity += pnl
        trades.append({
            'symbol': symbol, 'signal_type': position['type'],
            'direction': 'LONG' if position['direction'] == 1 else 'SHORT',
            'entry_price': position['entry'], 'exit_price': close,
            'sl': position['original_sl'], 'target': position['target'],
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
            'exit_type': 'EOD',
            'entry_time': df['Date'].iloc[position['entry_idx']],
            'exit_time': df['Date'].iloc[-1],
            'hold_bars': len(df) - position['entry_idx'],
            'zone_quality': position.get('quality', 0),
            'trigger_type': position.get('trigger', ''),
            'fake_breakout': position.get('fake_breakout', False),
            'reason': position.get('reason', ''),
        })

    return compute_metrics(trades, equity, capital, equity_curve, symbol, 'crypto_v3')


# ── METRICS ────────────────────────────────────────────────────────────────────

def compute_metrics(trades, equity, capital, equity_curve, symbol, market):
    """Compute comprehensive backtest metrics."""
    if not trades:
        print(f"  {symbol}: 0 trades -- no metrics")
        return None

    num_trades = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    num_wins = len(wins)
    num_losses = len(losses)
    win_rate = num_wins / num_trades * 100

    total_pnl = sum(t['pnl'] for t in trades)
    gross_profit = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.99

    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losses]) if losses else 0
    avg_win_pct = np.mean([t['pnl_pct'] for t in wins]) if wins else 0
    avg_loss_pct = np.mean([abs(t['pnl_pct']) for t in losses]) if losses else 0

    # Signal type distribution
    se_count = sum(1 for t in trades if t['signal_type'] == 'GHST_SE')
    be_count = sum(1 for t in trades if t['signal_type'] == 'GHST_BE')
    trp_count = sum(1 for t in trades if t['signal_type'] == 'GHST_TRP')

    sl_exits = sum(1 for t in trades if t['exit_type'] == 'SL')
    target_exits = sum(1 for t in trades if t['exit_type'] == 'TARGET')
    exhaust_exits = sum(1 for t in trades if t['exit_type'] == 'EXHAUST')

    # Trigger type distribution
    inj_trades = sum(1 for t in trades if t.get('trigger_type') == 'injection')
    bat_trades = sum(1 for t in trades if t.get('trigger_type') == 'bat')
    bel_trades = sum(1 for t in trades if t.get('trigger_type') == 'belan')
    fbo_trades = sum(1 for t in trades if t.get('fake_breakout', False))

    # Sharpe ratio
    if len(equity_curve) > 1:
        rets = pd.Series(equity_curve).pct_change().dropna()
        sharpe = (rets.mean() / max(rets.std(), 1e-10)) * np.sqrt(2190)
    else:
        sharpe = 0

    # Max drawdown
    peak_eq = capital
    max_dd = 0
    for e in equity_curve:
        peak_eq = max(peak_eq, e)
        dd = (peak_eq - e) / peak_eq * 100
        max_dd = max(max_dd, dd)

    # Quality analysis
    avg_win_quality = np.mean([t['zone_quality'] for t in wins]) if wins else 0
    avg_loss_quality = np.mean([t['zone_quality'] for t in losses]) if losses else 0

    result = {
        'symbol': symbol,
        'market': market,
        'total_pnl': round(total_pnl, 2),
        'total_return': round((equity - capital) / capital * 100, 2),
        'num_trades': num_trades,
        'wins': num_wins,
        'losses': num_losses,
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'avg_win_pct': round(avg_win_pct, 2),
        'avg_loss_pct': round(avg_loss_pct, 2),
        'se_signals': se_count,
        'be_signals': be_count,
        'trp_signals': trp_count,
        'sl_exits': sl_exits,
        'target_exits': target_exits,
        'exhaust_exits': exhaust_exits,
        'inj_trades': inj_trades,
        'bat_trades': bat_trades,
        'bel_trades': bel_trades,
        'fbo_trades': fbo_trades,
        'avg_win_quality': round(avg_win_quality, 1),
        'avg_loss_quality': round(avg_loss_quality, 1),
        'trades': trades,
    }

    print(f"  {symbol}: {num_trades} trades | WR={win_rate:.1f}% | PnL=Rs {total_pnl:,.0f} | "
          f"PF={profit_factor:.2f} | MaxDD={max_dd:.1f}% | Sharpe={sharpe:.2f}")
    print(f"    Signals: SE={se_count} BE={be_count} TRP={trp_count} | "
          f"Exits: SL={sl_exits} TGT={target_exits} EXH={exhaust_exits}")
    print(f"    Triggers: Injection={inj_trades} Bat={bat_trades} Belan={bel_trades} "
          f"| FakeBreakout={fbo_trades}")

    return result


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Ghost Zone V3 Backtest')
    parser.add_argument('--crypto-only', action='store_true')
    parser.add_argument('--equity-only', action='store_true')
    parser.add_argument('--days', type=int, default=60, help='Days of 5m data')
    args = parser.parse_args()

    print("=" * 120)
    print("  GHOST ZONE V3 BACKTEST - INSTITUTIONAL FOOTPRINT STRATEGY")
    print("  Based on Manish Maheshwari (Ghost Trader) Masterclass")
    print("  Zone Detection: Injection (Pin Bar) | Bat (Expansion) | Belan (Doji)")
    print("  Entry: 50%% of zone after fake breakout / liquidity sweep")
    print("  Exits: SL below zone | Exhaustion trailing | Target at opposite zone")
    print(f"  Capital: Rs {INITIAL_CAPITAL:,} | Max {MAX_TRADES_PER_DAY} trades/day | "
          f"1%% risk/trade | Volume >= 3x avg")
    print("=" * 120)

    all_results = []

    # ── CRYPTO BACKTEST ──
    if not args.equity_only:
        print("\n" + "=" * 80)
        print("  CRYPTO: BTCUSDT, ETHUSDT, SOLUSDT (5m candles)")
        print("=" * 80)

        for sym in CRYPTO_SYMBOLS:
            # Fetch 4H for zone detection (wider window)
            df_4h = fetch_binance(sym, '4h', max(args.days, 120))
            time.sleep(0.3)
            # Fetch 5m for entry timing
            df_5m = fetch_binance(sym, '5m', args.days)
            time.sleep(0.3)

            if df_4h is None or len(df_4h) < 100:
                print(f"  {sym}: Insufficient 4H data")
                continue
            if df_5m is None or len(df_5m) < 1000:
                print(f"  {sym}: Insufficient 5m data")
                continue

            result = backtest_crypto_v3(df_4h, df_5m, sym)
            if result:
                all_results.append(result)

    # ── RESULTS SUMMARY ──
    if all_results:
        print("\n" + "=" * 120)
        print("  GHOST ZONE V3 - RESULTS SUMMARY")
        print("=" * 120)

        print(f"\n  {'Symbol':<12} {'Market':<12} {'Trades':>6} {'WinR%':>7} {'PnL':>14} "
              f"{'PF':>6} {'Sharpe':>7} {'MaxDD%':>7} "
              f"{'SE':>4} {'BE':>4} {'TRP':>4} {'SL':>4} {'TGT':>4} {'EXH':>4}")
        print("  " + "-" * 116)

        for r in all_results:
            print(f"  {r['symbol']:<12} {r['market']:<12} {r['num_trades']:>5} {r['win_rate']:>6.1f}% "
                  f"Rs{r['total_pnl']:>12,.0f} {r['profit_factor']:>5.2f} {r['sharpe']:>6.2f} "
                  f"{r['max_drawdown']:>6.1f}% "
                  f"{r['se_signals']:>3} {r['be_signals']:>3} {r['trp_signals']:>3} "
                  f"{r['sl_exits']:>3} {r['target_exits']:>3} {r['exhaust_exits']:>3}")

        # Aggregate
        total_trades = sum(r['num_trades'] for r in all_results)
        total_pnl = sum(r['total_pnl'] for r in all_results)
        avg_wr = np.mean([r['win_rate'] for r in all_results])
        avg_pf = np.mean([r['profit_factor'] for r in all_results])
        avg_sharpe = np.mean([r['sharpe'] for r in all_results])
        worst_dd = max(r['max_drawdown'] for r in all_results)

        print(f"\n  {'TOTAL':<12} {'':12} {total_trades:>5} {avg_wr:>6.1f}% "
              f"Rs{total_pnl:>12,.0f} {avg_pf:>5.2f} {avg_sharpe:>6.2f} "
              f"{worst_dd:>6.1f}%")

        # Trigger analysis
        print(f"\n  TRIGGER TYPE ANALYSIS:")
        all_trades = []
        for r in all_results:
            all_trades.extend(r['trades'])

        if all_trades:
            for trigger in ['injection', 'bat', 'belan']:
                t_trades = [t for t in all_trades if t.get('trigger_type') == trigger]
                if t_trades:
                    t_wins = sum(1 for t in t_trades if t['pnl'] > 0)
                    t_pnl = sum(t['pnl'] for t in t_trades)
                    t_wr = t_wins / len(t_trades) * 100
                    print(f"    {trigger.upper():<12}: {len(t_trades)} trades | "
                          f"WR={t_wr:.0f}% | PnL=Rs {t_pnl:,.0f}")

            # Fake breakout analysis
            fbo_trades = [t for t in all_trades if t.get('fake_breakout', False)]
            nonfbo_trades = [t for t in all_trades if not t.get('fake_breakout', False)]
            if fbo_trades:
                fbo_wins = sum(1 for t in fbo_trades if t['pnl'] > 0)
                fbo_pnl = sum(t['pnl'] for t in fbo_trades)
                print(f"\n    FakeBreakout: {len(fbo_trades)} trades | "
                      f"WR={fbo_wins/len(fbo_trades)*100:.0f}% | PnL=Rs {fbo_pnl:,.0f}")
            if nonfbo_trades:
                nf_wins = sum(1 for t in nonfbo_trades if t['pnl'] > 0)
                nf_pnl = sum(t['pnl'] for t in nonfbo_trades)
                print(f"    No FakeBO:    {len(nonfbo_trades)} trades | "
                      f"WR={nf_wins/len(nonfbo_trades)*100:.0f}% | PnL=Rs {nf_pnl:,.0f}")

            # Quality analysis
            win_trades = [t for t in all_trades if t['pnl'] > 0]
            loss_trades = [t for t in all_trades if t['pnl'] <= 0]
            if win_trades:
                print(f"\n    Avg quality WINNING: {np.mean([t['zone_quality'] for t in win_trades]):.1f}")
            if loss_trades:
                print(f"    Avg quality LOSING:  {np.mean([t['zone_quality'] for t in loss_trades]):.1f}")

        # Save results
        csv_rows = [{k: v for k, v in r.items() if k != 'trades'} for r in all_results]
        csv_df = pd.DataFrame(csv_rows)
        csv_path = os.path.join(REPORT_DIR, 'ghost_zone_v3_backtest.csv')
        csv_df.to_csv(csv_path, index=False)
        print(f"\n  Results saved to: {csv_path}")

        # Save trade details
        if all_trades:
            trades_df = pd.DataFrame(all_trades)
            trades_path = os.path.join(REPORT_DIR, 'ghost_zone_v3_trades.csv')
            trades_df.to_csv(trades_path, index=False)
            print(f"  Trade details saved to: {trades_path}")

        # V2 vs V3 comparison (if V2 results exist)
        v2_path = os.path.join(REPORT_DIR, 'ghost_zone_v2_backtest.csv')
        if os.path.exists(v2_path):
            v2_df = pd.read_csv(v2_path)
            v2_total_pnl = v2_df['total_pnl'].sum()
            v2_avg_wr = v2_df['win_rate'].mean()
            v2_avg_pf = v2_df['profit_factor'].mean()

            print(f"\n  V2 vs V3 COMPARISON:")
            print(f"  {'':12} {'WR%':>7} {'PnL':>14} {'PF':>7} {'MaxDD%':>7}")
            print(f"  {'V2':<12} {v2_avg_wr:>6.1f}% Rs{v2_total_pnl:>12,.0f} "
                  f"{v2_avg_pf:>6.2f}")
            print(f"  {'V3':<12} {avg_wr:>6.1f}% Rs{total_pnl:>12,.0f} "
                  f"{avg_pf:>6.2f} {worst_dd:>6.1f}%")

    else:
        print("\n  No results generated -- check data availability")

    print(f"\n{'='*120}")
    print("  DONE!")
    print(f"{'='*120}")


if __name__ == '__main__':
    main()
