#!/usr/bin/env python3
"""
Signal Replay Backtest Engine.

Replays REAL live signals from paper trading against 5-min spot data
to compute actual P&L outcomes. Uses Black-Scholes premium repricing
at each 5-min bar to check target/SL hits.

Data sources:
- paper_trades/signals_*.csv  -> 1,118 real signals (Feb 16 - Mar 2, 2026)
- data/options/{SYMBOL}_spot_five_min_100d.csv -> 5-min OHLC (120 days)

Usage:
    python backtest_signal_replay.py                    # All strategies
    python backtest_signal_replay.py --strategy CPR     # Single strategy
    python backtest_signal_replay.py --symbol NIFTY     # Single symbol
"""

import os
import sys
import argparse
import pandas as pd
import numpy as np
from datetime import timedelta
from dataclasses import dataclass
from typing import List, Optional

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

# Reuse BS pricing from existing engine
from backtest_engine import bs_price, RISK_FREE_RATE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_DIR = os.path.join(BASE_DIR, 'paper_trades')
DATA_DIR = os.path.join(BASE_DIR, 'data', 'options')
RESULTS_DIR = os.path.join(BASE_DIR, 'results')

# Lot sizes
LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20}

# Brokerage + costs
BROKERAGE_PER_ORDER = 20  # Rs 20 per order
SLIPPAGE_POINTS = 2       # Rs 2 slippage per lot

# Market timings
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
EOD_EXIT_HOUR, EOD_EXIT_MIN = 15, 20  # Force close at 3:20 PM
MAX_HOLD_DAYS = 2  # Max days to carry position


@dataclass
class ReplayTrade:
    """Single trade from signal replay."""
    signal_time: str
    strategy: str
    symbol: str
    signal_type: str
    strike: float
    entry_premium: float
    exit_premium: float
    target: float
    sl: float
    entry_spot: float
    exit_spot: float
    exit_reason: str  # TARGET_HIT, SL_HIT, EOD_EXIT, NEXT_DAY_EXIT
    exit_time: str
    pnl: float
    lot_size: int
    hold_bars: int
    hold_minutes: int
    iv: float
    dte: int


def load_signals():
    """Load all signal CSVs from paper_trades/."""
    all_dfs = []
    for f in sorted(os.listdir(SIGNALS_DIR)):
        if f.startswith('signals_') and f.endswith('.csv'):
            path = os.path.join(SIGNALS_DIR, f)
            try:
                df = pd.read_csv(path, parse_dates=['timestamp'])
                all_dfs.append(df)
            except Exception as e:
                print(f"  [WARN] Error loading {f}: {e}")

    if not all_dfs:
        print("  [ERROR] No signal files found in paper_trades/")
        return None

    signals = pd.concat(all_dfs, ignore_index=True)
    signals = signals.sort_values('timestamp').reset_index(drop=True)
    print(f"  Loaded {len(signals)} signals from {len(all_dfs)} files")
    print(f"  Date range: {signals['timestamp'].min().date()} to {signals['timestamp'].max().date()}")
    return signals


def load_spot_5min(symbol):
    """Load 5-min spot data for a symbol."""
    path = os.path.join(DATA_DIR, f'{symbol}_spot_five_min_100d.csv')
    if not os.path.exists(path):
        print(f"  [WARN] No 5-min data: {path}")
        return None

    df = pd.read_csv(path, parse_dates=['DateTime'])
    df = df.sort_values('DateTime').reset_index(drop=True)
    print(f"  {symbol} 5-min: {len(df)} bars ({df['DateTime'].min().date()} to {df['DateTime'].max().date()})")
    return df


def extract_opt_type(signal_type):
    """Extract option type (CE/PE) from signal type string."""
    st = signal_type.upper()
    if 'CE' in st or 'BUY_CE' in st:
        return 'CE'
    elif 'PE' in st or 'BUY_PE' in st or 'SELL_PE' in st:
        return 'PE'
    return 'CE'  # default


def is_sell_signal(signal_type):
    """Check if the signal is a SELL (option writing) signal."""
    return 'SELL' in signal_type.upper()


def implied_vol_from_premium(spot, strike, T, r, premium, opt_type='CE',
                              tol=1e-6, max_iter=50):
    """Newton-Raphson to find IV that reproduces the given premium.

    This is critical: the signal's entry premium was computed with a specific IV.
    We must recover that exact IV so the replay uses the same pricing basis.
    """
    from scipy.stats import norm

    if premium <= 0 or spot <= 0 or strike <= 0 or T <= 0:
        return 0.15

    # Initial guess
    sigma = 0.20

    for _ in range(max_iter):
        d1 = (np.log(spot / strike) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)

        if opt_type == 'CE':
            price = spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
        else:
            price = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)

        vega = spot * norm.pdf(d1) * np.sqrt(T)

        if vega < 1e-10:
            break

        diff = price - premium
        sigma -= diff / vega

        # Clamp to reasonable range
        sigma = max(0.01, min(2.0, sigma))

        if abs(diff) < tol:
            break

    return max(0.05, min(1.5, sigma))


def estimate_iv_from_signal(row):
    """Reverse-engineer IV from signal's entry premium using Newton-Raphson.

    The signal CSV has: premium, spot, strike, dte, type.
    We solve for the IV that produces that exact premium via BS.
    This ensures the replay uses the same pricing basis as the live system.
    """
    spot = row.get('spot', 0)
    strike = row.get('strike', 0)
    premium = row.get('premium', 0)
    dte = max(row.get('dte', 1), 0.5)
    signal_type = row.get('type', 'BUY_CE')
    opt_type = extract_opt_type(signal_type)

    if spot <= 0 or strike <= 0 or premium <= 0:
        return 0.15

    T = dte / 365
    iv = implied_vol_from_premium(spot, strike, T, RISK_FREE_RATE, premium, opt_type)
    return iv


def replay_signal(signal_row, spot_df, lot_size):
    """Replay a single signal against 5-min spot data.

    Args:
        signal_row: Row from signals CSV.
        spot_df: 5-min spot DataFrame for the symbol.
        lot_size: Lot size for the symbol.

    Returns:
        ReplayTrade or None if signal can't be replayed.
    """
    sig_time = signal_row['timestamp']
    strike = signal_row['strike']
    entry_premium = signal_row['premium']
    target = signal_row['target']
    sl = signal_row['sl']
    entry_spot = signal_row['spot']
    dte = signal_row.get('dte', 1)
    signal_type = signal_row['type']
    opt_type = extract_opt_type(signal_type)
    is_sell = is_sell_signal(signal_type)

    # Estimate IV from signal data
    iv = estimate_iv_from_signal(signal_row)

    # Find the 5-min bar at or just after signal time
    sig_date = sig_time.date() if hasattr(sig_time, 'date') else pd.Timestamp(sig_time).date()

    # Get bars from signal time onward (same day + next days up to MAX_HOLD_DAYS)
    end_date = sig_date + timedelta(days=MAX_HOLD_DAYS + 2)  # buffer for weekends
    mask = (spot_df['DateTime'] >= sig_time) & (spot_df['DateTime'].dt.date <= end_date)
    future_bars = spot_df[mask]

    if len(future_bars) < 2:
        return None  # Not enough data to replay

    # Track time elapsed for theta decay
    entry_bar_time = future_bars.iloc[0]['DateTime']
    current_day = sig_date
    bars_processed = 0

    for idx, bar in future_bars.iterrows():
        bars_processed += 1
        bar_time = bar['DateTime']
        bar_date = bar_time.date()
        current_spot = bar['Close']

        # Compute time elapsed in years for BS
        minutes_elapsed = (bar_time - entry_bar_time).total_seconds() / 60
        days_elapsed = minutes_elapsed / (60 * 24)
        T_remaining = max((dte - days_elapsed) / 365, 1e-6)

        # Compute current premium via BS
        current_premium = bs_price(current_spot, strike, T_remaining,
                                   RISK_FREE_RATE, iv, opt_type)

        # Apply bid-ask spread (2% for realistic exit pricing)
        if is_sell:
            # Selling: we buy back at slightly higher price
            exit_price = current_premium * 1.02
        else:
            # Buying: we sell at slightly lower price
            exit_price = current_premium * 0.98

        # Check exit conditions
        exit_reason = None

        if is_sell:
            # SELL signal: target = premium drops (we profit), SL = premium rises (we lose)
            if exit_price <= target:
                exit_reason = 'TARGET_HIT'
            elif exit_price >= sl:
                exit_reason = 'SL_HIT'
        else:
            # BUY signal: target = premium rises (we profit), SL = premium drops (we lose)
            if exit_price >= target:
                exit_reason = 'TARGET_HIT'
            elif exit_price <= sl:
                exit_reason = 'SL_HIT'

        # End of day forced exit
        if (exit_reason is None and
                bar_time.hour == EOD_EXIT_HOUR and bar_time.minute >= EOD_EXIT_MIN):
            # Check if we should carry to next day
            remaining_dte = dte - days_elapsed
            if remaining_dte > 0.5 and bar_date == sig_date and MAX_HOLD_DAYS > 0:
                # Allow carry to next trading day - don't exit yet
                pass
            else:
                exit_reason = 'EOD_EXIT'

        # Force exit at end of next day if carrying
        if (exit_reason is None and bar_date > sig_date and
                bar_time.hour == EOD_EXIT_HOUR and bar_time.minute >= EOD_EXIT_MIN):
            exit_reason = 'NEXT_DAY_EXIT'

        if exit_reason:
            # Compute P&L
            if is_sell:
                raw_pnl = (entry_premium - exit_price) * lot_size
            else:
                raw_pnl = (exit_price - entry_premium) * lot_size

            # Deduct costs
            costs = (BROKERAGE_PER_ORDER * 2) + (SLIPPAGE_POINTS * lot_size)
            pnl = raw_pnl - costs

            return ReplayTrade(
                signal_time=str(sig_time),
                strategy=signal_row['strategy'],
                symbol=signal_row['symbol'],
                signal_type=signal_type,
                strike=strike,
                entry_premium=round(entry_premium, 2),
                exit_premium=round(exit_price, 2),
                target=round(target, 2),
                sl=round(sl, 2),
                entry_spot=round(entry_spot, 2),
                exit_spot=round(current_spot, 2),
                exit_reason=exit_reason,
                exit_time=str(bar_time),
                pnl=round(pnl, 2),
                lot_size=lot_size,
                hold_bars=bars_processed,
                hold_minutes=int(minutes_elapsed),
                iv=round(iv, 4),
                dte=dte,
            )

    # If we exhausted all bars without exit -> force exit at last bar
    if bars_processed > 0:
        last_bar = future_bars.iloc[-1]
        minutes_elapsed = (last_bar['DateTime'] - entry_bar_time).total_seconds() / 60
        days_elapsed = minutes_elapsed / (60 * 24)
        T_remaining = max((dte - days_elapsed) / 365, 1e-6)
        exit_prem = bs_price(last_bar['Close'], strike, T_remaining,
                             RISK_FREE_RATE, iv, opt_type)
        exit_prem *= (1.02 if is_sell else 0.98)

        if is_sell:
            raw_pnl = (entry_premium - exit_prem) * lot_size
        else:
            raw_pnl = (exit_prem - entry_premium) * lot_size
        costs = (BROKERAGE_PER_ORDER * 2) + (SLIPPAGE_POINTS * lot_size)

        return ReplayTrade(
            signal_time=str(sig_time),
            strategy=signal_row['strategy'],
            symbol=signal_row['symbol'],
            signal_type=signal_type,
            strike=strike,
            entry_premium=round(entry_premium, 2),
            exit_premium=round(exit_prem, 2),
            target=round(target, 2),
            sl=round(sl, 2),
            entry_spot=round(entry_spot, 2),
            exit_spot=round(last_bar['Close'], 2),
            exit_reason='DATA_END',
            exit_time=str(last_bar['DateTime']),
            pnl=round(raw_pnl - costs, 2),
            lot_size=lot_size,
            hold_bars=bars_processed,
            hold_minutes=int(minutes_elapsed),
            iv=round(iv, 4),
            dte=dte,
        )

    return None


def deduplicate_signals(signals_df):
    """Remove duplicate signals at the same timestamp for same strategy+symbol.

    The paper trader generates the same signal every scan cycle (every 5 min)
    until a position is taken. We only want the FIRST signal per
    strategy+symbol+type per day to avoid counting duplicates.
    """
    # Group by date + strategy + symbol + type, take first signal only
    signals_df['date'] = signals_df['timestamp'].dt.date
    deduped = signals_df.groupby(['date', 'strategy', 'symbol', 'type']).first().reset_index()
    deduped = deduped.sort_values('timestamp').reset_index(drop=True)
    print(f"  Deduplicated: {len(signals_df)} -> {len(deduped)} unique signals")
    return deduped


def run_replay(signals, spot_data, strategy_filter=None, symbol_filter=None):
    """Run signal replay across all signals.

    Args:
        signals: DataFrame of signals.
        spot_data: Dict of {symbol: 5-min DataFrame}.
        strategy_filter: Optional strategy name to filter.
        symbol_filter: Optional symbol name to filter.

    Returns:
        List of ReplayTrade objects.
    """
    # Deduplicate signals
    signals = deduplicate_signals(signals)

    # Apply filters
    if strategy_filter:
        signals = signals[signals['strategy'] == strategy_filter]
    if symbol_filter:
        signals = signals[signals['symbol'] == symbol_filter]

    # Only keep signals where we have 5-min data
    valid_dates = set()
    for sym, df in spot_data.items():
        for d in df['DateTime'].dt.date.unique():
            valid_dates.add((sym, d))

    valid_mask = signals.apply(
        lambda r: (r['symbol'], r['timestamp'].date()) in valid_dates, axis=1
    )
    signals = signals[valid_mask]
    print(f"  Signals with matching 5-min data: {len(signals)}")

    trades = []
    skipped = 0

    for idx, row in signals.iterrows():
        symbol = row['symbol']
        lot_size = LOT_SIZES.get(symbol, 75)
        spot_df = spot_data.get(symbol)

        if spot_df is None:
            skipped += 1
            continue

        trade = replay_signal(row, spot_df, lot_size)
        if trade:
            trades.append(trade)
        else:
            skipped += 1

    print(f"  Replayed: {len(trades)} trades, Skipped: {skipped}")
    return trades


def compute_strategy_results(trades, strategy_name):
    """Compute performance metrics for a strategy's trades."""
    if not trades:
        return None

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float('inf')
    rr = avg_win / abs(avg_loss) if avg_loss != 0 else float('inf')

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    # Hold time stats
    hold_mins = [t.hold_minutes for t in trades]

    return {
        'strategy': strategy_name,
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': wr,
        'total_pnl': total_pnl,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': pf,
        'risk_reward': rr,
        'largest_win': max(wins) if wins else 0,
        'largest_loss': min(losses) if losses else 0,
        'exit_reasons': exit_reasons,
        'avg_hold_min': np.mean(hold_mins),
        'median_hold_min': np.median(hold_mins),
        'ev_per_trade': total_pnl / len(trades) if trades else 0,
    }


def print_results(results_list):
    """Pretty-print results for all strategies."""
    print(f"\n{'='*75}")
    print(f"  SIGNAL REPLAY BACKTEST RESULTS")
    print(f"  Real signals from live paper trading, replayed on 5-min spot data")
    print(f"{'='*75}")

    for res in results_list:
        if res is None:
            continue

        pnl_sign = '+' if res['total_pnl'] >= 0 else ''
        print(f"\n  --- {res['strategy']} ---")
        print(f"  Trades: {res['total_trades']} (W:{res['wins']} L:{res['losses']})")
        print(f"  Win Rate: {res['win_rate']:.1f}%")
        print(f"  Total PnL: Rs {pnl_sign}{res['total_pnl']:,.2f}")
        print(f"  Profit Factor: {res['profit_factor']:.2f}")
        print(f"  Risk:Reward: {res['risk_reward']:.2f}:1")
        print(f"  EV per trade: Rs {res['ev_per_trade']:,.2f}")
        print(f"  Avg Win: Rs {res['avg_win']:,.2f}  |  Avg Loss: Rs {res['avg_loss']:,.2f}")
        print(f"  Largest Win: Rs {res['largest_win']:,.2f}  |  Largest Loss: Rs {res['largest_loss']:,.2f}")
        print(f"  Avg Hold: {res['avg_hold_min']:.0f} min  |  Median Hold: {res['median_hold_min']:.0f} min")
        print(f"  Exit Reasons: {res['exit_reasons']}")

    # Summary table
    print(f"\n{'='*75}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*75}")
    print(f"  {'Strategy':<18s} {'Trades':>6s} {'WR%':>6s} {'PF':>6s} {'R:R':>6s} {'PnL':>14s} {'EV/trade':>10s}")
    print(f"  {'-'*66}")
    for res in results_list:
        if res is None:
            continue
        pnl_sign = '+' if res['total_pnl'] >= 0 else ''
        print(f"  {res['strategy']:<18s} {res['total_trades']:>6d} {res['win_rate']:>5.1f}% "
              f"{res['profit_factor']:>6.2f} {res['risk_reward']:>5.2f} "
              f"Rs {pnl_sign}{res['total_pnl']:>11,.0f} Rs {res['ev_per_trade']:>8,.0f}")
    print(f"{'='*75}\n")


def save_trades_csv(trades, strategy_name):
    """Save replay trades to CSV."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe_name = strategy_name.replace(' ', '_').replace('+', '_')
    path = os.path.join(RESULTS_DIR, f'replay_trades_{safe_name}.csv')

    rows = []
    for t in trades:
        rows.append({
            'signal_time': t.signal_time,
            'exit_time': t.exit_time,
            'strategy': t.strategy,
            'symbol': t.symbol,
            'signal_type': t.signal_type,
            'strike': t.strike,
            'entry_premium': t.entry_premium,
            'exit_premium': t.exit_premium,
            'target': t.target,
            'sl': t.sl,
            'entry_spot': t.entry_spot,
            'exit_spot': t.exit_spot,
            'exit_reason': t.exit_reason,
            'pnl': t.pnl,
            'lot_size': t.lot_size,
            'hold_bars': t.hold_bars,
            'hold_minutes': t.hold_minutes,
            'iv': t.iv,
            'dte': t.dte,
        })

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"  Saved {len(rows)} trades to {path}")


def main():
    parser = argparse.ArgumentParser(description='Replay live signals on 5-min spot data')
    parser.add_argument('--strategy', type=str, default=None,
                        help='Filter by strategy name (CPR, Ghost Zone, etc.)')
    parser.add_argument('--symbol', type=str, default=None,
                        help='Filter by symbol (NIFTY, BANKNIFTY, SENSEX)')
    args = parser.parse_args()

    print("\n" + "=" * 75)
    print("  SIGNAL REPLAY BACKTEST ENGINE")
    print("  Replaying real live signals against 5-min spot data")
    print("=" * 75)

    # 1. Load signals
    print("\n--- Loading Signals ---")
    signals = load_signals()
    if signals is None or len(signals) == 0:
        print("  No signals to replay. Exiting.")
        return

    # 2. Load 5-min spot data
    print("\n--- Loading 5-Min Spot Data ---")
    spot_data = {}
    for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        df = load_spot_5min(sym)
        if df is not None:
            spot_data[sym] = df

    if not spot_data:
        print("  No 5-min spot data found. Exiting.")
        return

    # 3. Run replay
    print("\n--- Running Signal Replay ---")
    all_trades = run_replay(signals, spot_data, args.strategy, args.symbol)

    if not all_trades:
        print("  No trades generated from replay. Exiting.")
        return

    # 4. Compute per-strategy results
    print("\n--- Computing Results ---")
    strategies = sorted(set(t.strategy for t in all_trades))
    results = []

    for strat in strategies:
        strat_trades = [t for t in all_trades if t.strategy == strat]
        res = compute_strategy_results(strat_trades, strat)
        results.append(res)

        # Save per-strategy CSV
        save_trades_csv(strat_trades, strat)

    # Also compute overall
    overall = compute_strategy_results(all_trades, 'ALL COMBINED')
    results.append(overall)

    # 5. Print results
    print_results(results)

    # 6. Per-symbol breakdown
    print(f"\n{'='*75}")
    print(f"  PER-SYMBOL BREAKDOWN")
    print(f"{'='*75}")
    for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        sym_trades = [t for t in all_trades if t.symbol == sym]
        if not sym_trades:
            continue
        pnls = [t.pnl for t in sym_trades]
        wins = [p for p in pnls if p > 0]
        wr = len(wins) / len(pnls) * 100
        pf = sum(wins) / abs(sum([p for p in pnls if p <= 0])) if any(p <= 0 for p in pnls) else float('inf')
        print(f"\n  {sym}: {len(sym_trades)} trades, WR={wr:.1f}%, PF={pf:.2f}, "
              f"PnL=Rs {'+'if sum(pnls)>=0 else ''}{sum(pnls):,.0f}")

    print(f"\n{'='*75}\n")


if __name__ == '__main__':
    main()
