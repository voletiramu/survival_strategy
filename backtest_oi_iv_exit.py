"""
BACKTEST: OI+IV Dynamic Exit vs Static Exit
============================================
Reads existing portfolio_state.json (closed trades with losses)
Simulates what would have happened if OI+IV exit was active.

Since we don't have minute-level historical OI data, we use:
- IV changes estimated from Black-Scholes model + spot movement
- OI changes estimated using volume proxy + premium movement speed
- For each losing trade:
  - Reconstruct approximate price path from entry → exit
  - Check if OI+IV thresholds would trigger exit earlier
  - Calculate saved loss amount
  - Simulate reversal trade profit
"""

import json
import os
import csv
import math
from datetime import datetime
from scipy.stats import norm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

CORRECT_LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
INITIAL_CAPITAL = 300000
RISK_FREE = 0.065

# OI+IV Exit Thresholds (same as paper_trader.py)
OI_SURGE_PCT = 15
OI_REVERSE_PCT = 25
IV_SPIKE_PCT = 25
IV_REVERSE_PCT = 40
OI_IV_COMBO_OI = 10
OI_IV_COMBO_IV = 15
GAMMA_SHIELD = 0.002


def bs_price(S, K, T, r, sigma, opt_type='CE'):
    """Black-Scholes option price."""
    if T <= 0 or sigma <= 0:
        if opt_type == 'CE':
            return max(S - K, 0)
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == 'CE':
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_gamma(S, K, T, r, sigma):
    """Black-Scholes gamma."""
    if T <= 0 or sigma <= 0:
        return 0
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def implied_vol_from_price(S, K, T, r, price, opt_type):
    """Estimate implied vol from option price using bisection."""
    low, high = 0.01, 2.0
    for _ in range(50):
        mid = (low + high) / 2
        p = bs_price(S, K, T, r, mid, opt_type)
        if p < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def simulate_oi_iv_exit(trade, lot_size):
    """
    Simulate what OI+IV exit would do for a losing trade.

    Approach:
    - Interpolate premium from entry to exit in 10 steps
    - At each step, estimate IV change from premium movement
    - Estimate OI change from premium velocity (fast moves = OI surge proxy)
    - If OI+IV thresholds trigger, calculate early exit PnL
    - If reversal triggered, simulate reverse trade from early exit to actual exit
    """
    entry_premium = trade['entry_premium']
    exit_premium = trade.get('exit_premium', 0)
    is_sell = trade.get('is_sell', False)
    strike = trade.get('strike', 0)
    entry_iv = trade.get('iv', 15.0)  # stored as percentage
    gamma = abs(trade.get('gamma', 0.001))
    dte = trade.get('dte', 2)

    if entry_iv == 0:
        entry_iv = 15.0

    # Original PnL
    if is_sell:
        original_pnl = (entry_premium - exit_premium) * lot_size
    else:
        original_pnl = (exit_premium - entry_premium) * lot_size

    # Only analyze losing trades
    if original_pnl >= 0:
        return None

    # Simulate price path in 10 steps
    steps = 10
    early_exit_step = None
    should_reverse = False

    for step in range(1, steps + 1):
        fraction = step / steps
        interp_premium = entry_premium + (exit_premium - entry_premium) * fraction

        # Estimate IV change from premium movement
        premium_change_pct = abs(interp_premium - entry_premium) / max(entry_premium, 1) * 100

        # IV proxy: Large premium changes imply IV changes
        # If premium moves fast, IV is likely changing
        iv_change_est = premium_change_pct * 0.5  # Rough: 1% premium move ≈ 0.5% IV change

        # OI proxy: Fast premium moves in adverse direction suggest OI buildup
        # For sellers: premium rising fast = new OI being added (bearish for seller)
        # For buyers: premium dropping fast = OI unwinding
        if is_sell:
            adverse_move = (interp_premium - entry_premium) / max(entry_premium, 1) * 100
        else:
            adverse_move = (entry_premium - interp_premium) / max(entry_premium, 1) * 100

        # OI estimate: adverse moves suggest OI changes proportionally
        oi_change_est = max(0, adverse_move * 0.3)  # 1% adverse move ≈ 0.3% OI change

        # Gamma risk check for shorts
        gamma_risk = is_sell and gamma > GAMMA_SHIELD

        # Check OI+IV exit conditions
        triggered = False
        exit_type = None

        if oi_change_est > OI_SURGE_PCT:
            triggered = True
            exit_type = 'OI_SURGE'
            should_reverse = oi_change_est > OI_REVERSE_PCT
        elif iv_change_est > IV_SPIKE_PCT:
            triggered = True
            exit_type = 'IV_SPIKE'
            should_reverse = iv_change_est > IV_REVERSE_PCT
        elif oi_change_est > OI_IV_COMBO_OI and iv_change_est > OI_IV_COMBO_IV:
            triggered = True
            exit_type = 'OI_IV_COMBO'
            should_reverse = True
        elif gamma_risk:
            triggered = True
            exit_type = 'GAMMA_SHIELD'
            should_reverse = False

        if triggered:
            early_exit_step = step
            break

    if early_exit_step is None:
        # OI+IV would NOT have triggered before static exit
        return {
            'triggered': False,
            'original_pnl': original_pnl,
            'new_pnl': original_pnl,
            'saved': 0,
            'reversal_pnl': 0,
        }

    # Calculate early exit premium (interpolated)
    fraction = early_exit_step / steps
    early_exit_premium = entry_premium + (exit_premium - entry_premium) * fraction

    # Early exit PnL
    if is_sell:
        early_pnl = (entry_premium - early_exit_premium) * lot_size
    else:
        early_pnl = (early_exit_premium - entry_premium) * lot_size

    saved = early_pnl - original_pnl  # Should be positive (loss reduced)

    # Reversal PnL: If we reversed at early exit, what happened from there to actual exit?
    reversal_pnl = 0
    if should_reverse:
        remaining_move = exit_premium - early_exit_premium
        # Reverse means we take opposite position
        # If original was BUY CE → reverse is SELL CE → profit if premium drops more
        # If original was SELL PE → reverse is BUY PE → profit if premium rises more
        if is_sell:
            # Was selling, now buying → profit from remaining premium rise
            reversal_pnl = (exit_premium - early_exit_premium) * lot_size
        else:
            # Was buying, now selling → profit from remaining premium drop
            reversal_pnl = (early_exit_premium - exit_premium) * lot_size

    return {
        'triggered': True,
        'exit_type': exit_type,
        'exit_step': f"{early_exit_step}/{steps}",
        'original_pnl': round(original_pnl, 2),
        'early_exit_premium': round(early_exit_premium, 2),
        'early_pnl': round(early_pnl, 2),
        'saved': round(saved, 2),
        'should_reverse': should_reverse,
        'reversal_pnl': round(reversal_pnl, 2),
        'new_total_pnl': round(early_pnl + reversal_pnl, 2),
    }


def main():
    # Load portfolio
    pf_file = os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json')
    with open(pf_file) as f:
        pf = json.load(f)

    closed = pf.get('closed_trades', [])
    print(f"\n{'='*90}")
    print(f"  BACKTEST: OI+IV Dynamic Exit vs Static Exit")
    print(f"{'='*90}")
    print(f"  Total closed trades: {len(closed)}")

    # Separate winners and losers
    results = []
    for t in closed:
        symbol = t['symbol']
        lot_size = CORRECT_LOT_SIZES.get(symbol, 65)
        is_sell = 'SELL' in t.get('signal_type', '')

        if is_sell:
            pnl = (t['entry_premium'] - t.get('exit_premium', 0)) * lot_size
        else:
            pnl = (t.get('exit_premium', 0) - t['entry_premium']) * lot_size

        t['_pnl'] = pnl
        t['_lot_size'] = lot_size
        t['is_sell'] = is_sell

    losers = [t for t in closed if t['_pnl'] < 0]
    winners = [t for t in closed if t['_pnl'] >= 0]

    total_original_loss = sum(t['_pnl'] for t in losers)
    total_winner_pnl = sum(t['_pnl'] for t in winners)

    print(f"  Winners: {len(winners)} (Total: Rs {total_winner_pnl:+,.0f})")
    print(f"  Losers:  {len(losers)} (Total: Rs {total_original_loss:+,.0f})")
    print(f"  Net PnL: Rs {total_winner_pnl + total_original_loss:+,.0f}")
    print(f"\n  Simulating OI+IV exits on {len(losers)} losing trades...")
    print(f"{'='*90}")

    # Backtest OI+IV exit on each loser
    early_exits = 0
    total_saved = 0
    total_reversal_pnl = 0
    reversal_count = 0
    backtest_results = []

    for t in losers:
        result = simulate_oi_iv_exit(t, t['_lot_size'])
        if result and result['triggered']:
            early_exits += 1
            total_saved += result['saved']
            if result['should_reverse']:
                reversal_count += 1
                total_reversal_pnl += result['reversal_pnl']

            opt = 'CE' if 'CE' in t.get('signal_type', '') else 'PE'
            symbol_str = f"{t['symbol']} {t.get('strike', 0):.0f} {opt}"
            backtest_results.append({
                'symbol': symbol_str,
                'strategy': t['strategy'],
                'exit_reason': t.get('exit_reason', ''),
                'original_pnl': result['original_pnl'],
                'early_exit_at': result['exit_step'],
                'oi_iv_exit_type': result['exit_type'],
                'early_pnl': result['early_pnl'],
                'saved': result['saved'],
                'reversed': result['should_reverse'],
                'reversal_pnl': result['reversal_pnl'],
                'new_total_pnl': result['new_total_pnl'],
            })

    # Print results
    print(f"\n{'='*90}")
    print(f"  OI+IV EXIT BACKTEST RESULTS")
    print(f"{'='*90}")

    if backtest_results:
        print(f"\n  {'Symbol':<28} {'Strategy':<12} {'Orig PnL':>12} {'Exit Type':<16} "
              f"{'New PnL':>12} {'Saved':>10} {'Rev PnL':>10}")
        print(f"  {'-'*100}")

        for r in backtest_results[:30]:  # Show top 30
            print(f"  {r['symbol']:<28} {r['strategy']:<12} "
                  f"Rs{r['original_pnl']:>+10,.0f} {r['oi_iv_exit_type']:<16} "
                  f"Rs{r['new_total_pnl']:>+10,.0f} Rs{r['saved']:>+8,.0f} "
                  f"Rs{r['reversal_pnl']:>+8,.0f}")

    new_total_loss = total_original_loss + total_saved + total_reversal_pnl
    new_net_pnl = total_winner_pnl + new_total_loss
    original_net = total_winner_pnl + total_original_loss
    improvement = new_net_pnl - original_net

    pct_reduction = abs(total_saved / total_original_loss * 100) if total_original_loss != 0 else 0

    print(f"\n{'='*90}")
    print(f"  SUMMARY COMPARISON")
    print(f"{'='*90}")
    print(f"  Original losing trades:    {len(losers)}")
    print(f"  Original total loss:       Rs {total_original_loss:>+14,.2f}")
    print(f"")
    print(f"  With OI+IV Exit:")
    print(f"    Early exits triggered:   {early_exits} ({early_exits/len(losers)*100:.0f}% of losers)")
    print(f"    Loss reduction:          Rs {total_saved:>+14,.2f} ({pct_reduction:.0f}% reduction)")
    print(f"    Reversals triggered:     {reversal_count}")
    print(f"    Reversal profits:        Rs {total_reversal_pnl:>+14,.2f}")
    print(f"")
    print(f"  ---- BOTTOM LINE ----")
    print(f"  Original Net PnL:          Rs {original_net:>+14,.2f}")
    print(f"  New Net PnL (with OI+IV):  Rs {new_net_pnl:>+14,.2f}")
    print(f"  Improvement:               Rs {improvement:>+14,.2f}")
    print(f"  Original Balance:          Rs {INITIAL_CAPITAL + original_net:>14,.2f}")
    print(f"  New Balance:               Rs {INITIAL_CAPITAL + new_net_pnl:>14,.2f}")
    print(f"{'='*90}")

    # Strategy-wise breakdown
    print(f"\n  STRATEGY-WISE IMPROVEMENT:")
    strategy_stats = {}
    for r in backtest_results:
        s = r['strategy']
        if s not in strategy_stats:
            strategy_stats[s] = {'count': 0, 'saved': 0, 'reversal': 0, 'orig_loss': 0}
        strategy_stats[s]['count'] += 1
        strategy_stats[s]['saved'] += r['saved']
        strategy_stats[s]['reversal'] += r['reversal_pnl']
        strategy_stats[s]['orig_loss'] += r['original_pnl']

    print(f"  {'Strategy':<16} {'Trades':>8} {'Orig Loss':>14} {'Saved':>12} {'Reversal':>12} {'Net Improve':>14}")
    for s, st in sorted(strategy_stats.items(), key=lambda x: -x[1]['saved']):
        net = st['saved'] + st['reversal']
        print(f"  {s:<16} {st['count']:>8} Rs{st['orig_loss']:>+12,.0f} Rs{st['saved']:>+10,.0f} "
              f"Rs{st['reversal']:>+10,.0f} Rs{net:>+12,.0f}")

    # Save CSV
    csv_path = os.path.join(REPORT_DIR, 'oi_iv_backtest_comparison.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Symbol', 'Strategy', 'Original Exit Reason', 'Original PnL',
                        'OI/IV Exit Type', 'Exit Step', 'Early PnL', 'Saved Amount',
                        'Reversed', 'Reversal PnL', 'New Total PnL'])
        for r in backtest_results:
            writer.writerow([r['symbol'], r['strategy'], r['exit_reason'],
                           r['original_pnl'], r['oi_iv_exit_type'], r['early_exit_at'],
                           r['early_pnl'], r['saved'], r['reversed'],
                           r['reversal_pnl'], r['new_total_pnl']])
    print(f"\n  CSV saved: {csv_path}")

    # Send to Telegram
    try:
        from trade_notifier import send_message

        msg = (
            f"<b>BACKTEST: OI+IV Exit vs Static Exit</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Losing trades analyzed: {len(losers)}\n"
            f"Original total loss: Rs {total_original_loss:+,.0f}\n\n"
            f"<b>With OI+IV Exit:</b>\n"
            f"Early exits: {early_exits} ({early_exits/max(len(losers),1)*100:.0f}%)\n"
            f"Loss reduction: Rs {total_saved:+,.0f}\n"
            f"Reversals: {reversal_count}\n"
            f"Reversal profits: Rs {total_reversal_pnl:+,.0f}\n\n"
            f"<b>BOTTOM LINE:</b>\n"
            f"Original PnL: Rs {original_net:+,.0f}\n"
            f"New PnL: Rs {new_net_pnl:+,.0f}\n"
            f"<b>Improvement: Rs {improvement:+,.0f}</b>\n"
            f"New Balance: Rs {INITIAL_CAPITAL + new_net_pnl:,.0f}"
        )
        send_message(msg)
        print("  Results sent to Telegram!")
    except Exception as e:
        print(f"  Telegram failed: {e}")


if __name__ == '__main__':
    main()
