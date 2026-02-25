"""
Master backtest runner - Runs all 5 strategies on Nifty50, BankNifty, Sensex
Generates comparison report with charts.

DISCLAIMER: This is for educational and research purposes only.
Past performance does not guarantee future results.
Options trading involves substantial risk of loss.
"""

import sys
import os
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
from tabulate import tabulate
from datetime import datetime

from data_fetcher import load_or_fetch, LOT_SIZES
from backtest_engine import BacktestEngine, BacktestResult
from strategies.survivor_strategy import SurvivorStrategy
from strategies.wave_strategy import WaveStrategy
from strategies.pcr_vwap_strategy import PCRVWAPStrategy
from strategies.ghost_zone_strategy import GhostZoneStrategy
from strategies.cpr_strategy import CPRStrategy

# Configuration
INITIAL_CAPITAL = 300000  # 3 Lakhs
SYMBOLS = ["NIFTY50", "BANKNIFTY", "SENSEX"]
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def run_all_backtests():
    """Run all strategies on all symbols."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_results = []

    for symbol in SYMBOLS:
        print(f"\n{'='*60}")
        print(f"BACKTESTING ON {symbol}")
        print(f"{'='*60}")

        try:
            df = load_or_fetch(symbol, period="5y")
        except Exception as e:
            print(f"Error loading {symbol}: {e}")
            continue

        if len(df) < 100:
            print(f"Insufficient data for {symbol}: {len(df)} rows")
            continue

        lot_size = LOT_SIZES.get(symbol, 25)

        strategies = [
            SurvivorStrategy(pe_gap=30, ce_gap=30, reset_gap=90, max_positions=3),
            WaveStrategy(base_gap=25, max_trades_per_day=6),
            PCRVWAPStrategy(pcr_lookback=5, vwap_tolerance_pct=0.3),
            GhostZoneStrategy(zone_lookback=20, volume_threshold=1.2, min_impulse_atr_mult=0.8, target_rr=2.0),
            CPRStrategy(risk_per_trade_pct=1.0, max_trades_per_day=2),
        ]

        for strat in strategies:
            print(f"\n  Running: {strat.NAME} on {symbol}...")
            engine = BacktestEngine(
                initial_capital=INITIAL_CAPITAL,
                lot_size=lot_size,
                max_risk_per_trade_pct=2.0,
                brokerage_per_order=20.0,
                slippage_points=2.0
            )

            try:
                result = strat.backtest(df, engine, symbol)
                all_results.append(result)
                print(f"    Trades: {result.total_trades}, PnL: {result.total_pnl:,.0f}, "
                      f"Win%: {result.win_rate:.1f}%, MaxDD: {result.max_drawdown_pct:.1f}%, "
                      f"Sharpe: {result.sharpe_ratio:.2f}")
            except Exception as e:
                print(f"    ERROR: {e}")
                import traceback
                traceback.print_exc()

    return all_results


def generate_comparison_table(results: list):
    """Generate comparison table across all strategies and symbols."""
    rows = []
    for r in results:
        rows.append({
            'Strategy': r.strategy_name,
            'Symbol': r.symbol,
            'Trades': r.total_trades,
            'Win Rate %': f"{r.win_rate:.1f}",
            'Total PnL': f"{r.total_pnl:,.0f}",
            'Return %': f"{r.total_return_pct:.1f}",
            'Annual Return %': f"{r.annualized_return_pct:.1f}",
            'Max DD %': f"{r.max_drawdown_pct:.1f}",
            'Sharpe': f"{r.sharpe_ratio:.2f}",
            'Sortino': f"{r.sortino_ratio:.2f}",
            'Profit Factor': f"{r.profit_factor:.2f}",
            'Calmar': f"{r.calmar_ratio:.2f}",
            'Avg Win': f"{r.avg_win:,.0f}",
            'Avg Loss': f"{r.avg_loss:,.0f}",
        })

    df = pd.DataFrame(rows)
    return df


def generate_strategy_summary(results: list):
    """Aggregate results per strategy across all symbols."""
    strategy_agg = {}
    for r in results:
        name = r.strategy_name
        if name not in strategy_agg:
            strategy_agg[name] = {
                'total_pnl': 0, 'total_trades': 0, 'wins': 0,
                'max_dd_pcts': [], 'sharpes': [], 'sortinos': [],
                'profit_factors': [], 'annual_returns': [], 'calmars': []
            }
        agg = strategy_agg[name]
        agg['total_pnl'] += r.total_pnl
        agg['total_trades'] += r.total_trades
        agg['wins'] += r.winning_trades
        agg['max_dd_pcts'].append(r.max_drawdown_pct)
        agg['sharpes'].append(r.sharpe_ratio)
        agg['sortinos'].append(r.sortino_ratio)
        agg['profit_factors'].append(r.profit_factor)
        agg['annual_returns'].append(r.annualized_return_pct)
        agg['calmars'].append(r.calmar_ratio)

    rows = []
    for name, agg in strategy_agg.items():
        win_rate = agg['wins'] / max(agg['total_trades'], 1) * 100
        rows.append({
            'Strategy': name,
            'Total PnL (All)': f"{agg['total_pnl']:,.0f}",
            'Trades': agg['total_trades'],
            'Win Rate %': f"{win_rate:.1f}",
            'Avg Annual Return %': f"{np.mean(agg['annual_returns']):.1f}",
            'Avg Max DD %': f"{np.mean(agg['max_dd_pcts']):.1f}",
            'Avg Sharpe': f"{np.mean(agg['sharpes']):.2f}",
            'Avg Sortino': f"{np.mean(agg['sortinos']):.2f}",
            'Avg Profit Factor': f"{np.mean(agg['profit_factors']):.2f}",
            'Avg Calmar': f"{np.mean(agg['calmars']):.2f}",
        })

    # Sort by total PnL
    rows.sort(key=lambda x: float(x['Total PnL (All)'].replace(',', '')), reverse=True)
    return pd.DataFrame(rows)


def plot_equity_curves(results: list):
    """Plot equity curves for all strategies."""
    # Group by symbol
    symbols = set(r.symbol for r in results)

    for symbol in symbols:
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [3, 1]})
        fig.suptitle(f'Strategy Comparison - {symbol}\nInitial Capital: ₹3,00,000 | 5-Year Backtest',
                     fontsize=14, fontweight='bold')

        symbol_results = [r for r in results if r.symbol == symbol]

        # Equity curves
        ax1 = axes[0]
        for r in symbol_results:
            if len(r.equity_curve) > 0:
                ax1.plot(r.equity_curve.index, r.equity_curve.values,
                        label=f"{r.strategy_name} (PnL: ₹{r.total_pnl:,.0f})", linewidth=1.5)

        ax1.axhline(y=INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5, label='Initial Capital')
        ax1.set_ylabel('Portfolio Value (₹)')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)

        # Drawdown
        ax2 = axes[1]
        for r in symbol_results:
            if len(r.equity_curve) > 0:
                eq = r.equity_curve
                peak = eq.expanding().max()
                dd_pct = (eq - peak) / peak * 100
                ax2.fill_between(dd_pct.index, dd_pct.values, 0,
                               alpha=0.3, label=r.strategy_name)

        ax2.set_ylabel('Drawdown %')
        ax2.set_xlabel('Date')
        ax2.legend(loc='lower left', fontsize=8)
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45)

        plt.tight_layout()
        filepath = os.path.join(RESULTS_DIR, f'equity_curves_{symbol}.png')
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"Saved: {filepath}")


def plot_monthly_heatmap(results: list):
    """Plot monthly returns heatmap for top strategies."""
    for r in results:
        if len(r.monthly_returns) < 12:
            continue

        monthly = r.monthly_returns * 100
        # Create year-month matrix
        years = monthly.index.year
        months = monthly.index.month
        pivot_data = pd.DataFrame({'Year': years, 'Month': months, 'Return': monthly.values})
        try:
            pivot_table = pivot_data.pivot_table(values='Return', index='Year', columns='Month',
                                                  aggfunc='sum')
        except Exception:
            continue

        if pivot_table.empty:
            continue

        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        pivot_table.columns = [month_names[m-1] for m in pivot_table.columns]

        fig, ax = plt.subplots(figsize=(12, 4))
        sns.heatmap(pivot_table, annot=True, fmt='.1f', cmap='RdYlGn',
                   center=0, ax=ax, linewidths=0.5)
        ax.set_title(f'{r.strategy_name} - {r.symbol} Monthly Returns %')
        plt.tight_layout()
        safe_name = r.strategy_name.replace(' ', '_').replace('(', '').replace(')', '').replace('+', '_')
        filepath = os.path.join(RESULTS_DIR, f'monthly_{safe_name}_{r.symbol}.png')
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()


def compute_allocation(summary_df: pd.DataFrame, total_capital: float = 300000):
    """Compute capital allocation based on risk-adjusted returns."""
    print("\n" + "="*60)
    print("CAPITAL ALLOCATION RECOMMENDATION")
    print(f"Total Capital: ₹{total_capital:,.0f}")
    print("="*60)

    # Score each strategy
    strategies = []
    for _, row in summary_df.iterrows():
        sharpe = float(row['Avg Sharpe'])
        sortino = float(row['Avg Sortino'])
        calmar = float(row['Avg Calmar'])
        pf = float(row['Avg Profit Factor'])
        dd = abs(float(row['Avg Max DD %']))
        annual_ret = float(row['Avg Annual Return %'])

        # Composite score (higher is better)
        # Weighted: Sharpe 25%, Sortino 20%, Calmar 15%, PF 15%, Return 15%, Low DD 10%
        score = (
            sharpe * 0.25 +
            sortino * 0.20 +
            calmar * 0.15 +
            min(pf, 5) * 0.15 +  # Cap PF at 5 to avoid inf distortion
            (annual_ret / 10) * 0.15 +
            max(0, (20 - dd) / 20) * 0.10  # Lower DD = higher score
        )

        strategies.append({
            'name': row['Strategy'],
            'score': max(score, 0),
            'annual_ret': annual_ret,
            'max_dd': dd,
            'sharpe': sharpe,
        })

    # Normalize scores
    total_score = sum(s['score'] for s in strategies)
    if total_score <= 0:
        # Equal allocation if all scores negative
        for s in strategies:
            s['allocation_pct'] = 100 / len(strategies)
    else:
        for s in strategies:
            s['allocation_pct'] = (s['score'] / total_score) * 100

    # Apply constraints: min 5%, max 40%
    for s in strategies:
        s['allocation_pct'] = max(5, min(40, s['allocation_pct']))

    # Renormalize
    total_alloc = sum(s['allocation_pct'] for s in strategies)
    for s in strategies:
        s['allocation_pct'] = s['allocation_pct'] / total_alloc * 100
        s['allocation_amount'] = total_capital * s['allocation_pct'] / 100

    # Sort by score
    strategies.sort(key=lambda x: x['score'], reverse=True)

    alloc_rows = []
    for i, s in enumerate(strategies):
        rank = i + 1
        alloc_rows.append({
            'Rank': rank,
            'Strategy': s['name'],
            'Score': f"{s['score']:.2f}",
            'Allocation %': f"{s['allocation_pct']:.1f}%",
            'Amount (₹)': f"{s['allocation_amount']:,.0f}",
            'Annual Return %': f"{s['annual_ret']:.1f}%",
            'Max DD %': f"{s['max_dd']:.1f}%",
            'Sharpe': f"{s['sharpe']:.2f}",
        })

    alloc_df = pd.DataFrame(alloc_rows)
    print(tabulate(alloc_df, headers='keys', tablefmt='grid', showindex=False))

    # Winner
    winner = strategies[0]
    print(f"\n🏆 WINNER: {winner['name']}")
    print(f"   Score: {winner['score']:.2f} | Allocation: ₹{winner['allocation_amount']:,.0f} "
          f"({winner['allocation_pct']:.1f}%)")

    return strategies


def main():
    print("="*60)
    print("ALGORITHMIC TRADING STRATEGY BACKTEST")
    print("Capital: ₹3,00,000 | Period: 5 Years | Indices: Nifty50, BankNifty, Sensex")
    print("="*60)
    print("\nDISCLAIMER: This is for EDUCATIONAL purposes only.")
    print("Past performance does NOT guarantee future results.")
    print("Options trading involves substantial risk of loss.\n")

    # Run backtests
    results = run_all_backtests()

    if not results:
        print("No results generated. Check data availability.")
        return

    # Generate comparison table
    print("\n" + "="*60)
    print("DETAILED RESULTS - ALL STRATEGIES x ALL SYMBOLS")
    print("="*60)
    comparison = generate_comparison_table(results)
    print(tabulate(comparison, headers='keys', tablefmt='grid', showindex=False))
    comparison.to_csv(os.path.join(RESULTS_DIR, 'detailed_results.csv'), index=False)

    # Strategy summary
    print("\n" + "="*60)
    print("STRATEGY SUMMARY (AGGREGATED ACROSS ALL SYMBOLS)")
    print("="*60)
    summary = generate_strategy_summary(results)
    print(tabulate(summary, headers='keys', tablefmt='grid', showindex=False))
    summary.to_csv(os.path.join(RESULTS_DIR, 'strategy_summary.csv'), index=False)

    # Plot charts
    print("\nGenerating charts...")
    plot_equity_curves(results)
    plot_monthly_heatmap(results)

    # Capital allocation
    allocation = compute_allocation(summary, INITIAL_CAPITAL)

    # Save allocation
    alloc_df = pd.DataFrame(allocation)
    alloc_df.to_csv(os.path.join(RESULTS_DIR, 'allocation.csv'), index=False)

    print(f"\nAll results saved to: {RESULTS_DIR}")
    print("\nFinal Notes:")
    print("- These backtests simulate option P&L from spot price movements")
    print("- Real options have Greeks (gamma, theta, vega) that affect P&L")
    print("- Slippage and liquidity in real markets may differ")
    print("- Consider paper trading before deploying real capital")
    print("- Weekly or monthly rebalancing of allocation is recommended")


if __name__ == "__main__":
    main()
