#!/usr/bin/env python3
"""
SL Hunt Multi-Day Backtest with Full Risk Metrics
===================================================
Runs SL hunt detection across 6 days of live 1-min data.
Computes: PF, Max DD, Sharpe, Sortino, Calmar, win streaks, etc.
Exports comprehensive Excel report.
"""
import pandas as pd
import numpy as np
from datetime import datetime
import os, sys

DATA_DIR = 'D:/AlgoTrading/data'
DATES = ['20260311', '20260312', '20260313', '20260316', '20260317', '20260318']
SYMBOLS = ['NIFTY', 'BANKNIFTY', 'SENSEX']
LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20}
CAPITAL = 300000


def load_spot(symbol, date):
    path = os.path.join(DATA_DIR, f'live_{symbol.lower()}_{date}.csv')
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    df['close'] = df['ltp']
    df['open'] = df['close'].shift(1).fillna(df['close'])
    df['high'] = df[['open', 'close']].max(axis=1)
    df['low'] = df[['open', 'close']].min(axis=1)
    return df


def detect_hunts(df, symbol, spike_sigma=2.0, reversal_pct=0.4):
    prices = df['close'].values
    times = df['timestamp'].values
    n = len(prices)
    if n < 30:
        return []

    velocity = np.diff(prices, prepend=prices[0])
    accel = np.diff(velocity, prepend=velocity[0])
    vel_std = pd.Series(velocity).rolling(20, min_periods=5).std().values
    vel_pct = velocity / np.maximum(prices, 1) * 100

    hunts = []
    for i in range(25, n - 15):
        s = vel_std[i]
        if s == 0 or np.isnan(s):
            continue
        z = abs(velocity[i]) / s
        if z < spike_sigma:
            continue

        spike_dir = 'UP' if velocity[i] > 0 else 'DOWN'
        cum = 0
        bars = 0
        for j in range(3):
            if i + j >= n:
                break
            m = velocity[i + j]
            if (spike_dir == 'UP' and m > 0) or (spike_dir == 'DOWN' and m < 0):
                cum += m
                bars += 1
            else:
                break
        if bars == 0:
            bars = 1
            cum = velocity[i]

        spike_size = abs(cum)
        spike_pct = spike_size / prices[i] * 100
        if spike_pct < 0.08:
            continue

        peak_idx = i + bars - 1
        if peak_idx >= n:
            continue
        peak = prices[peak_idx]

        best_rev = 0
        rev_idx = None
        rev_time = None
        for k in range(1, min(16, n - peak_idx)):
            ci = peak_idx + k
            if ci >= n:
                break
            if spike_dir == 'UP':
                ra = peak - prices[ci]
            else:
                ra = prices[ci] - peak
            if ra > best_rev:
                best_rev = ra
                rev_idx = ci
                rev_time = k

        rev_ratio = best_rev / spike_size if spike_size > 0 else 0
        if rev_ratio < reversal_pct:
            continue

        accel_during = accel[i]
        accel_after = accel[min(peak_idx + 2, n - 1)]
        accel_flip = (accel_during > 0 and accel_after < 0) or (accel_during < 0 and accel_after > 0)

        pre_range = np.ptp(prices[max(0, i - 10):i])
        pre_range_pct = pre_range / prices[i] * 100

        entry_idx = peak_idx + 2
        if entry_idx >= n:
            continue
        entry_price = prices[entry_idx]
        trade_dir = 'PE' if spike_dir == 'UP' else 'CE'

        # Realistic exit: trail from peak reversal, or SL at spike peak
        # SL = spike peak (if price goes past spike, we were wrong)
        sl_price = peak + (spike_size * 0.3 if spike_dir == 'UP' else -spike_size * 0.3)

        exit_idx = None
        exit_price = None
        exit_reason = 'TIMEOUT'

        for k in range(1, min(31, n - entry_idx)):
            ci = entry_idx + k
            if ci >= n:
                break
            p = prices[ci]

            # Check SL hit
            if spike_dir == 'UP' and p > sl_price:
                exit_idx = ci
                exit_price = sl_price  # SL fill at SL level
                exit_reason = 'SL_HIT'
                break
            elif spike_dir == 'DOWN' and p < sl_price:
                exit_idx = ci
                exit_price = sl_price
                exit_reason = 'SL_HIT'
                break

            # Check target: 80% of spike retraced
            target_price = peak - (spike_size * 0.8) if spike_dir == 'UP' else peak + (spike_size * 0.8)
            if spike_dir == 'UP' and p <= target_price:
                exit_idx = ci
                exit_price = p
                exit_reason = 'TARGET'
                break
            elif spike_dir == 'DOWN' and p >= target_price:
                exit_idx = ci
                exit_price = p
                exit_reason = 'TARGET'
                break

        if exit_idx is None:
            exit_idx = min(entry_idx + 30, n - 1)
            exit_price = prices[exit_idx]

        if trade_dir == 'CE':
            spot_pnl = exit_price - entry_price
        else:
            spot_pnl = entry_price - exit_price

        # Apply slippage (0.5 pts for index)
        spot_pnl -= 0.5

        # Quality score
        q = 0
        if z >= 4.0: q += 25
        elif z >= 3.0: q += 15
        elif z >= 2.5: q += 10
        if rev_ratio >= 0.8: q += 25
        elif rev_ratio >= 0.6: q += 15
        elif rev_ratio >= 0.5: q += 10
        if accel_flip: q += 20
        if pre_range_pct < 0.15: q += 15
        if rev_time and rev_time <= 5: q += 15
        elif rev_time and rev_time <= 10: q += 8
        q = min(q, 100)

        # Option PnL estimate
        lot = LOT_SIZES.get(symbol, 50)
        delta = 0.50
        option_pnl = delta * spot_pnl * lot

        # Estimate premium for % calc
        est_prem = entry_price * 0.004
        prem_pnl_pct = (delta * spot_pnl) / max(est_prem, 1) * 100

        hunts.append({
            'date': pd.Timestamp(times[i]).strftime('%Y-%m-%d'),
            'timestamp': pd.Timestamp(times[i]),
            'symbol': symbol,
            'spike_dir': spike_dir,
            'spike_pct': round(spike_pct, 4),
            'spike_bars': bars,
            'reversal_ratio': round(rev_ratio, 2),
            'reversal_bars': rev_time,
            'accel_flip': accel_flip,
            'z_score': round(z, 1),
            'pre_range_pct': round(pre_range_pct, 3),
            'trade_dir': trade_dir,
            'entry_price': round(entry_price, 2),
            'exit_price': round(exit_price, 2),
            'spot_pnl': round(spot_pnl, 2),
            'quality': q,
            'lot_size': lot,
            'option_pnl': round(option_pnl, 2),
            'prem_pnl_pct': round(prem_pnl_pct, 1),
        })

    # De-dup within 5 bars
    if hunts:
        filtered = [hunts[0]]
        for h in hunts[1:]:
            td = (h['timestamp'] - filtered[-1]['timestamp']).total_seconds() / 60
            if td >= 5:
                filtered.append(h)
        hunts = filtered

    return hunts


def compute_risk_metrics(trades_df, capital=CAPITAL):
    """Compute full risk metrics from trade list."""
    if trades_df.empty:
        return {}

    pnls = trades_df['option_pnl'].values
    n = len(pnls)

    # Basic
    gross_win = pnls[pnls > 0].sum() if (pnls > 0).any() else 0
    gross_loss = abs(pnls[pnls <= 0].sum()) if (pnls <= 0).any() else 0.01
    winners = (pnls > 0).sum()
    losers = (pnls <= 0).sum()
    win_rate = winners / n * 100
    avg_win = pnls[pnls > 0].mean() if winners > 0 else 0
    avg_loss = abs(pnls[pnls <= 0].mean()) if losers > 0 else 0.01
    pf = gross_win / max(gross_loss, 0.01)

    # Cumulative equity curve
    cum_pnl = np.cumsum(pnls)
    total_pnl = cum_pnl[-1]

    # Max Drawdown
    peak = np.maximum.accumulate(cum_pnl)
    dd = peak - cum_pnl
    max_dd = dd.max()
    max_dd_pct = max_dd / max(capital, 1) * 100

    # DD duration (trades in drawdown)
    in_dd = dd > 0
    dd_streaks = []
    streak = 0
    for d in in_dd:
        if d:
            streak += 1
        else:
            if streak > 0:
                dd_streaks.append(streak)
            streak = 0
    if streak > 0:
        dd_streaks.append(streak)
    max_dd_duration = max(dd_streaks) if dd_streaks else 0

    # Daily PnL for Sharpe/Sortino
    trades_df = trades_df.copy()
    trades_df['trade_date'] = trades_df['timestamp'].dt.date
    daily_pnl = trades_df.groupby('trade_date')['option_pnl'].sum()
    daily_returns = daily_pnl / capital

    # Annualize (252 trading days)
    if len(daily_returns) > 1:
        mean_daily = daily_returns.mean()
        std_daily = daily_returns.std()
        sharpe = (mean_daily / std_daily) * np.sqrt(252) if std_daily > 0 else 0

        # Sortino (downside deviation only)
        neg_returns = daily_returns[daily_returns < 0]
        downside_std = neg_returns.std() if len(neg_returns) > 1 else std_daily
        sortino = (mean_daily / downside_std) * np.sqrt(252) if downside_std > 0 else 0

        # Calmar = annualized return / max DD
        ann_return = mean_daily * 252
        calmar = ann_return / (max_dd / capital) if max_dd > 0 else 0
    else:
        sharpe = sortino = calmar = 0

    # Expectancy
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)

    # Win/Loss streaks
    results = pnls > 0
    max_win_streak = max_loss_streak = current = 0
    is_winning = None
    for r in results:
        if r == is_winning:
            current += 1
        else:
            if is_winning is True:
                max_win_streak = max(max_win_streak, current)
            elif is_winning is False:
                max_loss_streak = max(max_loss_streak, current)
            current = 1
            is_winning = r
    if is_winning is True:
        max_win_streak = max(max_win_streak, current)
    elif is_winning is False:
        max_loss_streak = max(max_loss_streak, current)

    # Reward-to-risk
    rr = avg_win / avg_loss if avg_loss > 0 else 0

    # Recovery factor
    recovery = total_pnl / max_dd if max_dd > 0 else 0

    return {
        'total_trades': n,
        'winners': int(winners),
        'losers': int(losers),
        'win_rate': round(win_rate, 1),
        'gross_win': round(gross_win, 0),
        'gross_loss': round(gross_loss, 0),
        'net_pnl': round(total_pnl, 0),
        'profit_factor': round(pf, 2),
        'avg_win': round(avg_win, 0),
        'avg_loss': round(avg_loss, 0),
        'reward_risk': round(rr, 2),
        'expectancy': round(expectancy, 0),
        'max_drawdown': round(max_dd, 0),
        'max_dd_pct': round(max_dd_pct, 2),
        'max_dd_duration': max_dd_duration,
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'calmar_ratio': round(calmar, 2),
        'recovery_factor': round(recovery, 2),
        'max_win_streak': max_win_streak,
        'max_loss_streak': max_loss_streak,
        'capital': capital,
        'return_pct': round(total_pnl / capital * 100, 2),
        'daily_pnl_series': daily_pnl,
        'equity_curve': cum_pnl,
    }


def main():
    all_hunts = []

    for date_str in DATES:
        for sym in SYMBOLS:
            df = load_spot(sym, date_str)
            if df is None or len(df) < 30:
                continue
            hunts = detect_hunts(df, sym)
            all_hunts.extend(hunts)

    if not all_hunts:
        print("No hunts found across all dates")
        return

    hunts_df = pd.DataFrame(all_hunts)
    print(f"Total hunts across {len(DATES)} days: {len(hunts_df)}")
    print(f"Days: {', '.join(DATES)}")
    print()

    # ---- Run analysis at different quality thresholds ----
    thresholds = [0, 30, 40, 50, 60, 70]
    best_score = 0
    best_thresh = 0
    threshold_results = []

    for q_min in thresholds:
        subset = hunts_df[hunts_df['quality'] >= q_min].copy()
        if len(subset) < 3:
            continue

        m = compute_risk_metrics(subset)

        # Combined score: PF * WR * net / (DD+1)
        score = m['profit_factor'] * m['win_rate'] / 100 * m['net_pnl'] / max(m['max_drawdown'] + 1, 1)

        threshold_results.append({
            'threshold': q_min,
            'trades': m['total_trades'],
            'winners': m['winners'],
            'losers': m['losers'],
            'win_rate': m['win_rate'],
            'net_pnl': m['net_pnl'],
            'profit_factor': m['profit_factor'],
            'max_dd': m['max_drawdown'],
            'max_dd_pct': m['max_dd_pct'],
            'sharpe': m['sharpe_ratio'],
            'sortino': m['sortino_ratio'],
            'calmar': m['calmar_ratio'],
            'recovery': m['recovery_factor'],
            'avg_win': m['avg_win'],
            'avg_loss': m['avg_loss'],
            'rr': m['reward_risk'],
            'expectancy': m['expectancy'],
            'max_win_streak': m['max_win_streak'],
            'max_loss_streak': m['max_loss_streak'],
            'return_pct': m['return_pct'],
            'score': round(score, 1),
        })

        if score > best_score:
            best_score = score
            best_thresh = q_min

        print(f"Q >= {q_min:>2}: {m['total_trades']:>3} trades | "
              f"WR={m['win_rate']:>5.1f}% | PF={m['profit_factor']:>5.2f} | "
              f"Net=Rs {m['net_pnl']:>8,.0f} | DD=Rs {m['max_drawdown']:>6,.0f} "
              f"({m['max_dd_pct']:.2f}%) | Sharpe={m['sharpe_ratio']:>5.2f} | "
              f"Sortino={m['sortino_ratio']:>5.2f} | Score={score:.0f}")

    print(f"\nBest threshold: Q >= {best_thresh} (score={best_score:.0f})")

    # ---- Full metrics for best threshold ----
    best_df = hunts_df[hunts_df['quality'] >= best_thresh].copy()
    m = compute_risk_metrics(best_df)

    print(f"\n{'='*70}")
    print(f"  FINAL METRICS — SL HUNT STRATEGY (Q >= {best_thresh})")
    print(f"{'='*70}")
    print(f"  Period: {len(DATES)} trading days ({DATES[0]} to {DATES[-1]})")
    print(f"  Capital: Rs {CAPITAL:,}")
    print()
    print(f"  PERFORMANCE")
    print(f"    Total Trades:     {m['total_trades']}")
    print(f"    Winners/Losers:   {m['winners']}W / {m['losers']}L")
    print(f"    Win Rate:         {m['win_rate']:.1f}%")
    print(f"    Net PnL:          Rs {m['net_pnl']:,.0f}")
    print(f"    Return:           {m['return_pct']:.2f}%")
    print(f"    Profit Factor:    {m['profit_factor']:.2f}")
    print(f"    Avg Win:          Rs {m['avg_win']:,.0f}")
    print(f"    Avg Loss:         Rs {m['avg_loss']:,.0f}")
    print(f"    Reward:Risk:      {m['reward_risk']:.2f}")
    print(f"    Expectancy:       Rs {m['expectancy']:,.0f}/trade")
    print()
    print(f"  RISK")
    print(f"    Max Drawdown:     Rs {m['max_drawdown']:,.0f} ({m['max_dd_pct']:.2f}%)")
    print(f"    DD Duration:      {m['max_dd_duration']} trades")
    print(f"    Recovery Factor:  {m['recovery_factor']:.2f}")
    print()
    print(f"  RATIOS")
    print(f"    Sharpe Ratio:     {m['sharpe_ratio']:.2f}")
    print(f"    Sortino Ratio:    {m['sortino_ratio']:.2f}")
    print(f"    Calmar Ratio:     {m['calmar_ratio']:.2f}")
    print()
    print(f"  STREAKS")
    print(f"    Max Win Streak:   {m['max_win_streak']}")
    print(f"    Max Loss Streak:  {m['max_loss_streak']}")

    # ---- Daily P&L breakdown ----
    print(f"\n  DAILY P&L")
    daily = m['daily_pnl_series']
    for dt, pnl in daily.items():
        tag = '+' if pnl >= 0 else ''
        print(f"    {dt}: {tag}Rs {pnl:,.0f}")
    print(f"    {'---':>10}")
    print(f"    Total:  Rs {daily.sum():,.0f}")

    # ---- Per-symbol breakdown ----
    print(f"\n  PER-SYMBOL BREAKDOWN")
    for sym in SYMBOLS:
        s = best_df[best_df['symbol'] == sym]
        if len(s) == 0:
            continue
        sw = len(s[s['option_pnl'] > 0])
        sp = s['option_pnl'].sum()
        print(f"    {sym:>10}: {len(s):>3} trades, {sw}W/{len(s)-sw}L, "
              f"WR={sw/len(s)*100:.0f}%, PnL=Rs {sp:,.0f}")

    # ---- Save Excel ----
    output = 'D:/AlgoTrading/data/sl_hunt_backtest_6day.xlsx'
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        # Sheet 1: All trades
        best_df_out = best_df.drop(columns=['timestamp'], errors='ignore')
        best_df_out.to_excel(writer, sheet_name='Trades', index=False)

        # Sheet 2: Threshold comparison
        pd.DataFrame(threshold_results).to_excel(writer, sheet_name='Threshold_Analysis', index=False)

        # Sheet 3: Risk metrics
        metrics_data = []
        for key in ['total_trades', 'winners', 'losers', 'win_rate', 'net_pnl',
                     'profit_factor', 'avg_win', 'avg_loss', 'reward_risk',
                     'expectancy', 'max_drawdown', 'max_dd_pct', 'max_dd_duration',
                     'sharpe_ratio', 'sortino_ratio', 'calmar_ratio', 'recovery_factor',
                     'max_win_streak', 'max_loss_streak', 'return_pct', 'capital']:
            val = m[key]
            if key in ('net_pnl', 'avg_win', 'avg_loss', 'expectancy', 'max_drawdown', 'capital'):
                val_str = f'Rs {val:,.0f}'
            elif key in ('win_rate', 'max_dd_pct', 'return_pct'):
                val_str = f'{val:.2f}%'
            elif key in ('profit_factor', 'reward_risk', 'sharpe_ratio', 'sortino_ratio',
                         'calmar_ratio', 'recovery_factor'):
                val_str = f'{val:.2f}'
            else:
                val_str = str(int(val))
            metrics_data.append({'Metric': key.replace('_', ' ').title(), 'Value': val_str})
        pd.DataFrame(metrics_data).to_excel(writer, sheet_name='Risk_Metrics', index=False)

        # Sheet 4: Daily PnL
        daily_df = pd.DataFrame({'Date': daily.index, 'PnL': daily.values})
        daily_df['Cumulative'] = daily_df['PnL'].cumsum()
        daily_df.to_excel(writer, sheet_name='Daily_PnL', index=False)

        # Sheet 5: Per-symbol
        sym_data = []
        for sym in SYMBOLS:
            s = best_df[best_df['symbol'] == sym]
            if len(s) == 0:
                continue
            sw = len(s[s['option_pnl'] > 0])
            sym_data.append({
                'Symbol': sym, 'Trades': len(s),
                'Winners': sw, 'Losers': len(s) - sw,
                'Win Rate': f'{sw/len(s)*100:.0f}%',
                'Net PnL': round(s['option_pnl'].sum(), 0),
                'Avg PnL': round(s['option_pnl'].mean(), 0),
                'Max DD': round(np.maximum.accumulate(np.cumsum(s['option_pnl'].values)).max() -
                                np.cumsum(s['option_pnl'].values).min(), 0) if len(s) > 0 else 0,
            })
        pd.DataFrame(sym_data).to_excel(writer, sheet_name='Per_Symbol', index=False)

    print(f"\n  Excel: {output}")


if __name__ == '__main__':
    main()
