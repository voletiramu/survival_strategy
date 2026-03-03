"""
Master backtest runner - Unified Equity + Commodity Backtesting
Runs 6 equity strategies on Nifty50, BankNifty, Sensex (Rs 3L capital)
Runs 3 commodity strategies on 7 MCX commodities (Rs 3L capital)
Generates comparison report with charts and capital allocation.

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

from data_fetcher import load_or_fetch, LOT_SIZES, DATA_SOURCE
from backtest_engine import BacktestEngine, BacktestResult
from strategies.survivor_strategy import SurvivorStrategy
from strategies.wave_strategy import WaveStrategy
from strategies.pcr_vwap_strategy import PCRVWAPStrategy
from strategies.ghost_zone_strategy import GhostZoneStrategy
from strategies.cpr_strategy import CPRStrategy
from strategies.gamma_blast_strategy import GammaBlastStrategy

# Commodity imports
from commodity_backtest import (
    backtest_cpr_commodity, backtest_gamma_blast_commodity,
    backtest_ghost_zone_commodity, load_commodity_data,
    compute_stats, COMMODITIES, INITIAL_CAPITAL as COMMODITY_INITIAL_CAPITAL
)

# Configuration
EQUITY_CAPITAL = 300000     # Rs 3 Lakhs TOTAL for all equity strategies
COMMODITY_CAPITAL = 300000  # Rs 3 Lakhs TOTAL for all commodity strategies

# Equity: 6 strategies share Rs 3L → Rs 50K per strategy
NUM_EQUITY_STRATEGIES = 6
EQUITY_PER_STRATEGY = EQUITY_CAPITAL // NUM_EQUITY_STRATEGIES  # Rs 50,000

# Commodity: 3 strategies share Rs 3L → Rs 1L per strategy
NUM_COMMODITY_STRATEGIES = 3
COMMODITY_PER_STRATEGY = COMMODITY_CAPITAL // NUM_COMMODITY_STRATEGIES  # Rs 1,00,000

INITIAL_CAPITAL = EQUITY_PER_STRATEGY  # Used by equity BacktestEngine
SYMBOLS = ["NIFTY50", "BANKNIFTY", "SENSEX"]
COMMODITY_SYMBOLS = ['GOLD', 'GOLDM', 'SILVER', 'SILVERM', 'CRUDEOIL', 'NATURALGAS', 'COPPER']
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def run_all_backtests(data_source=None):
    """Run all strategies on all symbols.

    Args:
        data_source: 'angel' for Angel One data, 'csv' for legacy Yahoo CSV
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_results = []
    source = data_source or DATA_SOURCE

    for symbol in SYMBOLS:
        print(f"\n{'='*60}")
        print(f"BACKTESTING ON {symbol} [Data: {source.upper()}]")
        print(f"{'='*60}")

        try:
            df = load_or_fetch(symbol, period="5y", source=source)
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
            GammaBlastStrategy(),
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


def run_commodity_backtests():
    """Run commodity strategies on all MCX commodities."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_commodity_stats = []

    strategy_fns = {
        'CPR': backtest_cpr_commodity,
        'Gamma Blast': backtest_gamma_blast_commodity,
        'Ghost Zone': backtest_ghost_zone_commodity,
    }

    for commodity in COMMODITY_SYMBOLS:
        spec = COMMODITIES.get(commodity)
        if spec is None:
            print(f"  Unknown commodity: {commodity}")
            continue

        print(f"\n{'='*60}")
        print(f"BACKTESTING ON {commodity} - {spec['description']}")
        print(f"Lots: {spec['lot_size']} | Contract Mult: {spec['multiplier']} | Margin: Rs {spec['margin']:,}")
        print(f"{'='*60}")

        df = load_commodity_data(commodity)
        if df is None:
            print(f"  No data for {commodity}")
            continue

        print(f"  Data: {len(df)} days ({df.index[0].date()} to {df.index[-1].date()})")

        for strat_name, strat_fn in strategy_fns.items():
            label = f"{strat_name} ({commodity})"
            try:
                trades_list, curve, final = strat_fn(df, COMMODITY_PER_STRATEGY, commodity)
                stats = compute_stats(trades_list, curve, COMMODITY_PER_STRATEGY, label)
                all_commodity_stats.append(stats)
            except Exception as e:
                print(f"    ERROR in {label}: {e}")
                import traceback
                traceback.print_exc()

    return all_commodity_stats


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
    """Aggregate results per strategy — AVERAGE across symbols (not sum).

    Each strategy gets Rs 50K capital. It runs on each symbol independently
    for comparison. The summary shows AVERAGE performance across symbols
    since in practice you'd trade one symbol at a time from your Rs 50K pool.
    """
    strategy_agg = {}
    for r in results:
        name = r.strategy_name
        if name not in strategy_agg:
            strategy_agg[name] = {
                'pnls': [], 'trade_counts': [], 'win_counts': [],
                'max_dd_pcts': [], 'sharpes': [], 'sortinos': [],
                'profit_factors': [], 'annual_returns': [], 'calmars': [],
                'return_pcts': []
            }
        agg = strategy_agg[name]
        agg['pnls'].append(r.total_pnl)
        agg['trade_counts'].append(r.total_trades)
        agg['win_counts'].append(r.winning_trades)
        agg['max_dd_pcts'].append(r.max_drawdown_pct)
        agg['sharpes'].append(r.sharpe_ratio)
        agg['sortinos'].append(r.sortino_ratio)
        agg['profit_factors'].append(r.profit_factor)
        agg['annual_returns'].append(r.annualized_return_pct)
        agg['calmars'].append(r.calmar_ratio)
        agg['return_pcts'].append(r.total_return_pct)

    rows = []
    for name, agg in strategy_agg.items():
        total_wins = sum(agg['win_counts'])
        total_trades = sum(agg['trade_counts'])
        win_rate = total_wins / max(total_trades, 1) * 100
        avg_pnl = np.mean(agg['pnls'])
        rows.append({
            'Strategy': name,
            'Capital (Rs)': f"{EQUITY_PER_STRATEGY:,.0f}",
            'Avg PnL/Symbol': f"{avg_pnl:,.0f}",
            'Avg Trades': f"{np.mean(agg['trade_counts']):.0f}",
            'Win Rate %': f"{win_rate:.1f}",
            'Avg Return %': f"{np.mean(agg['return_pcts']):.1f}",
            'Avg Annual Return %': f"{np.mean(agg['annual_returns']):.1f}",
            'Avg Max DD %': f"{np.mean(agg['max_dd_pcts']):.1f}",
            'Avg Sharpe': f"{np.mean(agg['sharpes']):.2f}",
            'Avg Sortino': f"{np.mean(agg['sortinos']):.2f}",
            'Avg Profit Factor': f"{np.mean(agg['profit_factors']):.2f}",
            'Avg Calmar': f"{np.mean(agg['calmars']):.2f}",
        })

    # Sort by avg PnL
    rows.sort(key=lambda x: float(x['Avg PnL/Symbol'].replace(',', '')), reverse=True)
    return pd.DataFrame(rows)


def plot_equity_curves(results: list):
    """Plot equity curves for all strategies."""
    # Group by symbol
    symbols = set(r.symbol for r in results)

    for symbol in symbols:
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [3, 1]})
        fig.suptitle(f'Strategy Comparison - {symbol}\nPer-Strategy Capital: ₹{EQUITY_PER_STRATEGY:,.0f} (Total: ₹{EQUITY_CAPITAL:,.0f} / {NUM_EQUITY_STRATEGIES} strategies)',
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
    """Compute capital allocation based on risk-adjusted returns.

    v3.1: Rebalanced scoring to properly weight absolute PnL and annual return.
    - Caps Calmar/Sortino to prevent distortion from tiny-drawdown strategies
    - Adds absolute PnL contribution to scoring
    - Raises max allocation to 50% for dominant strategies like Survivor
    """
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
        avg_pnl = float(str(row['Avg PnL/Symbol']).replace(',', ''))

        # v3.1: Cap extreme ratios to prevent distortion
        # Gamma Blast Calmar=113 vs Survivor=17 was dominating allocation unfairly
        sharpe_capped = min(sharpe, 5.0)
        sortino_capped = min(sortino, 10.0)
        calmar_capped = min(calmar, 25.0)

        # v3.1: Composite score — rebalanced weights
        # Sharpe 20%, Annual Return 25%, PnL 20%, Sortino 10%, Calmar 10%, PF 10%, DD 5%
        score = (
            sharpe_capped * 0.20 +
            (annual_ret / 100) * 0.25 +         # Annual return as direct percentage
            (avg_pnl / 1_000_000) * 0.20 +       # Absolute PnL in millions
            sortino_capped * 0.10 +
            calmar_capped * 0.10 +
            min(pf, 5) * 0.10 +
            max(0, (25 - dd) / 25) * 0.05        # Lower DD = higher score, gentler
        )

        strategies.append({
            'name': row['Strategy'],
            'score': max(score, 0),
            'annual_ret': annual_ret,
            'avg_pnl': avg_pnl,
            'max_dd': dd,
            'sharpe': sharpe,
        })

    # Normalize scores
    total_score = sum(s['score'] for s in strategies)
    if total_score <= 0:
        for s in strategies:
            s['allocation_pct'] = 100 / len(strategies)
    else:
        for s in strategies:
            s['allocation_pct'] = (s['score'] / total_score) * 100

    # v3.1: Apply constraints: min 5%, max 50% (raised from 40% for dominant strategies)
    for s in strategies:
        s['allocation_pct'] = max(5, min(50, s['allocation_pct']))

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
            'Avg PnL (₹)': f"{s.get('avg_pnl', 0):,.0f}",
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
    import argparse
    parser = argparse.ArgumentParser(description='Run backtests')
    parser.add_argument('--source', choices=['angel', 'csv'], default=None,
                        help='Data source: angel (Angel One) or csv (legacy Yahoo). Default: angel')
    args = parser.parse_args()

    data_source = args.source or DATA_SOURCE
    source_label = "Angel One SmartAPI" if data_source == 'angel' else "Legacy Yahoo CSV"

    print("="*60)
    print("UNIFIED ALGORITHMIC TRADING STRATEGY BACKTEST")
    print(f"Data Source: {source_label}")
    print(f"Equity Capital: Rs {EQUITY_CAPITAL:,.0f} | Commodity Capital: Rs {COMMODITY_CAPITAL:,.0f}")
    print(f"Total Capital: Rs {EQUITY_CAPITAL + COMMODITY_CAPITAL:,.0f}")
    print("Equity: Nifty50, BankNifty, Sensex | Commodities: 7 MCX instruments")
    print("="*60)
    print("\nDISCLAIMER: This is for EDUCATIONAL purposes only.")
    print("Past performance does NOT guarantee future results.")
    print("Options trading involves substantial risk of loss.\n")

    # ============================================================
    # PART 1: EQUITY BACKTESTS
    # ============================================================
    print("\n" + "="*60)
    print("PART 1: EQUITY INDEX STRATEGIES")
    print(f"Data Source: {source_label}")
    print(f"Total Equity Pool: Rs {EQUITY_CAPITAL:,.0f} | {NUM_EQUITY_STRATEGIES} strategies | Rs {EQUITY_PER_STRATEGY:,.0f} per strategy")
    print(f"Indices: NIFTY50, BANKNIFTY, SENSEX")
    print("="*60)

    results = run_all_backtests(data_source=data_source)

    if results:
        print("\n" + "="*60)
        print("EQUITY DETAILED RESULTS - ALL STRATEGIES x ALL SYMBOLS")
        print("="*60)
        comparison = generate_comparison_table(results)
        print(tabulate(comparison, headers='keys', tablefmt='grid', showindex=False))
        comparison.to_csv(os.path.join(RESULTS_DIR, 'equity_detailed_results.csv'), index=False)

        print("\n" + "="*60)
        print("EQUITY STRATEGY SUMMARY (AGGREGATED ACROSS ALL SYMBOLS)")
        print("="*60)
        equity_summary = generate_strategy_summary(results)
        print(tabulate(equity_summary, headers='keys', tablefmt='grid', showindex=False))
        equity_summary.to_csv(os.path.join(RESULTS_DIR, 'equity_strategy_summary.csv'), index=False)

        print("\nGenerating equity charts...")
        plot_equity_curves(results)
        plot_monthly_heatmap(results)

        print("\n" + "="*60)
        print(f"EQUITY CAPITAL ALLOCATION (Rs {EQUITY_CAPITAL:,.0f})")
        print("="*60)
        equity_allocation = compute_allocation(equity_summary, EQUITY_CAPITAL)
        pd.DataFrame(equity_allocation).to_csv(
            os.path.join(RESULTS_DIR, 'equity_allocation.csv'), index=False)
    else:
        print("No equity results generated. Check data availability.")
        equity_summary = None
        equity_allocation = []

    # ============================================================
    # PART 2: COMMODITY BACKTESTS
    # ============================================================
    print("\n\n" + "="*60)
    print("PART 2: COMMODITY STRATEGIES (MCX)")
    print(f"Total Commodity Pool: Rs {COMMODITY_CAPITAL:,.0f} | {NUM_COMMODITY_STRATEGIES} strategies | Rs {COMMODITY_PER_STRATEGY:,.0f} per strategy")
    print(f"Black-76 Model | Real MCX Costs")
    print("="*60)

    commodity_stats = run_commodity_backtests()

    if commodity_stats:
        print("\n" + "="*60)
        print("COMMODITY DETAILED RESULTS")
        print("="*60)
        commodity_df = pd.DataFrame(commodity_stats)
        print(tabulate(commodity_df, headers='keys', tablefmt='grid', showindex=False))
        commodity_df.to_csv(os.path.join(RESULTS_DIR, 'commodity_detailed_results.csv'), index=False)

        # Commodity allocation by Sharpe
        commodity_df['Sharpe_num'] = pd.to_numeric(commodity_df['Sharpe'], errors='coerce')
        commodity_df['Trades_num'] = pd.to_numeric(commodity_df['Trades'], errors='coerce')
        valid_comm = commodity_df[commodity_df['Trades_num'] >= 10].copy()

        if len(valid_comm) > 0:
            valid_comm = valid_comm.sort_values('Sharpe_num', ascending=False)
            print("\n" + "="*60)
            print(f"COMMODITY CAPITAL ALLOCATION (Rs {COMMODITY_CAPITAL:,.0f})")
            print("Ranked by Sharpe (min 10 trades)")
            print("="*60)

            profitable = valid_comm[valid_comm['Sharpe_num'] > 0]
            if len(profitable) > 0:
                total_sharpe = profitable['Sharpe_num'].sum()
                comm_alloc = []
                for _, row in profitable.iterrows():
                    w = row['Sharpe_num'] / total_sharpe if total_sharpe > 0 else 1/len(profitable)
                    w = max(0.05, min(0.40, w))  # Constrain 5-40%
                    comm_alloc.append({
                        'Strategy': row['Strategy'],
                        'Score (Sharpe)': f"{row['Sharpe_num']:.2f}",
                        'Weight': f"{w*100:.1f}%",
                        'Amount (Rs)': f"{COMMODITY_CAPITAL * w:,.0f}",
                        'Win Rate': row['Win Rate'],
                        'Ann Return': row['Ann Return'],
                        'Max DD': row['Max DD'],
                    })
                # Renormalize weights
                total_w = sum(float(c['Weight'].replace('%',''))/100 for c in comm_alloc)
                for c in comm_alloc:
                    orig_w = float(c['Weight'].replace('%',''))/100
                    norm_w = orig_w / total_w
                    c['Weight'] = f"{norm_w*100:.1f}%"
                    c['Amount (Rs)'] = f"{COMMODITY_CAPITAL * norm_w:,.0f}"

                comm_alloc_df = pd.DataFrame(comm_alloc)
                print(tabulate(comm_alloc_df, headers='keys', tablefmt='grid', showindex=False))
                comm_alloc_df.to_csv(os.path.join(RESULTS_DIR, 'commodity_allocation.csv'), index=False)
            else:
                print("No profitable commodity strategies found.")
    else:
        print("No commodity results generated. Check data availability.")

    # ============================================================
    # PART 3: COMBINED SUMMARY
    # ============================================================
    print("\n\n" + "="*60)
    print("COMBINED PORTFOLIO SUMMARY")
    print(f"Total Capital: Rs {EQUITY_CAPITAL + COMMODITY_CAPITAL:,.0f}")
    print(f"  Equity Pool:    Rs {EQUITY_CAPITAL:,.0f} ({NUM_EQUITY_STRATEGIES} strategies x Rs {EQUITY_PER_STRATEGY:,.0f} each)")
    print(f"  Commodity Pool: Rs {COMMODITY_CAPITAL:,.0f} ({NUM_COMMODITY_STRATEGIES} strategies x Rs {COMMODITY_PER_STRATEGY:,.0f} each)")
    print("="*60)

    # Save combined results
    if results:
        all_equity_rows = []
        for r in results:
            all_equity_rows.append({
                'Market': 'Equity',
                'Strategy': r.strategy_name,
                'Symbol': r.symbol,
                'Trades': r.total_trades,
                'Win Rate %': f"{r.win_rate:.1f}",
                'Total PnL': f"{r.total_pnl:,.0f}",
                'Sharpe': f"{r.sharpe_ratio:.2f}",
            })

        if commodity_stats:
            for cs in commodity_stats:
                all_equity_rows.append({
                    'Market': 'Commodity',
                    'Strategy': cs['Strategy'],
                    'Symbol': '-',
                    'Trades': cs['Trades'],
                    'Win Rate %': cs['Win Rate'],
                    'Total PnL': cs['Total PnL'],
                    'Sharpe': cs['Sharpe'],
                })

        combined_df = pd.DataFrame(all_equity_rows)
        combined_df.to_csv(os.path.join(RESULTS_DIR, 'combined_results.csv'), index=False)

    print(f"\nAll results saved to: {RESULTS_DIR}")
    print("\nFinal Notes:")
    print("- Equity strategies simulate option P&L from spot price movements (delta=0.5)")
    print("- Commodity strategies use Black-76 model with real MCX costs")
    print("- Real options have Greeks (gamma, theta, vega) that affect P&L")
    print("- Slippage and liquidity in real markets may differ")
    print("- Paper trade for 1-2 weeks before deploying real capital")
    print("- Weekly or monthly rebalancing of allocation is recommended")


if __name__ == "__main__":
    main()
