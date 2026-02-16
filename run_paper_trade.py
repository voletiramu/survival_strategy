"""
UNIFIED PAPER TRADING LAUNCHER
================================
Runs BOTH equity index AND commodity options paper trading simultaneously.

Equity:   NIFTY, BANKNIFTY, SENSEX     (9:15 AM - 3:30 PM)
Commodity: Gold Mini, Silver Mini, Crude Oil Mini  (9:00 AM - 11:30 PM)

Usage:
    python run_paper_trade.py              # Run both systems live
    python run_paper_trade.py --once       # Single scan both
    python run_paper_trade.py --offline    # Offline test both
    python run_paper_trade.py --status     # Show both portfolios
    python run_paper_trade.py --reset      # Reset both portfolios
"""

import sys
import os
import time
import signal
import logging
import argparse
import threading
from datetime import datetime, time as dtime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'unified_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('UnifiedPaperTrader')

# Market hours
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 30)
MCX_OPEN = dtime(9, 0)
MCX_CLOSE = dtime(23, 30)


def print_banner():
    print("\n" + "=" * 70)
    print("  UNIFIED ALGO TRADING - PAPER TRADING SYSTEM")
    print("  " + "-" * 64)
    print("  EQUITY:     NIFTY | BANKNIFTY | SENSEX")
    print("              Strategies: CPR, Gamma Blast, Ghost Zone, PCR+VWAP, Survivor")
    print("              Hours: 9:15 AM - 3:30 PM IST")
    print("  " + "-" * 64)
    print("  COMMODITY:  Gold Mini | Silver Mini | Crude Oil Mini")
    print("              Strategies: CPR, Gamma Blast, Ghost Zone")
    print("              Hours: 9:00 AM - 11:30 PM IST")
    print("  " + "-" * 64)
    print(f"  Capital: Rs 3,00,000 each | Angel One SmartAPI")
    print("=" * 70 + "\n")


def run_equity_scan(offline=False):
    """Run equity paper trading scan."""
    try:
        # Import here to avoid circular imports
        sys.path.insert(0, BASE_DIR)
        from paper_trader import PaperTrader

        logger.info("\n" + "=" * 70)
        logger.info("EQUITY INDEX SCAN")
        logger.info("=" * 70)

        trader = PaperTrader()
        for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            trader.engine.load_historical(symbol)

        if not offline:
            if not trader.connect():
                logger.warning("Equity: Angel API failed, using offline data")

        trader.run_once()
        return trader

    except Exception as e:
        logger.error(f"Equity scan error: {e}")
        import traceback
        traceback.print_exc()
        return None


def run_commodity_scan(offline=False):
    """Run commodity paper trading scan."""
    try:
        sys.path.insert(0, BASE_DIR)
        from commodity_paper_trader import CommodityPaperTrader, PAPER_TRADE_COMMODITIES

        logger.info("\n" + "=" * 70)
        logger.info("COMMODITY MCX SCAN")
        logger.info("=" * 70)

        trader = CommodityPaperTrader()
        for comm in PAPER_TRADE_COMMODITIES:
            trader.engine.load_historical(comm)

        if not offline:
            if not trader.connect():
                logger.warning("Commodity: Angel API failed, using offline data")

        trader.run_once()
        return trader

    except Exception as e:
        logger.error(f"Commodity scan error: {e}")
        import traceback
        traceback.print_exc()
        return None


def show_combined_status():
    """Show both equity and commodity portfolio status."""
    print_banner()

    # Equity status
    try:
        from paper_trader import PaperPortfolio
        eq_port = PaperPortfolio()
        eq_port.print_status()
    except Exception as e:
        print(f"  Equity portfolio: {e}")

    # Commodity status
    try:
        from commodity_paper_trader import CommodityPortfolio
        comm_port = CommodityPortfolio()
        comm_port.print_status()
    except Exception as e:
        print(f"  Commodity portfolio: {e}")

    # Combined summary
    try:
        eq_capital = eq_port.capital if eq_port else 300000
        comm_capital = comm_port.capital if comm_port else 300000
        total_capital = eq_capital + comm_capital
        total_initial = 600000  # 3L each
        total_return = (total_capital - total_initial) / total_initial * 100

        eq_trades = len(eq_port.closed_trades) if eq_port else 0
        comm_trades = len(comm_port.closed_trades) if comm_port else 0

        eq_wins = len([t for t in eq_port.closed_trades if t['pnl'] > 0]) if eq_port else 0
        comm_wins = len([t for t in comm_port.closed_trades if t['pnl'] > 0]) if comm_port else 0
        total_trades = eq_trades + comm_trades
        total_wins = eq_wins + comm_wins

        print("\n" + "=" * 70)
        print("COMBINED PORTFOLIO SUMMARY")
        print("=" * 70)
        print(f"  Equity Capital:     Rs {eq_capital:,.0f}")
        print(f"  Commodity Capital:  Rs {comm_capital:,.0f}")
        print(f"  Total Capital:      Rs {total_capital:,.0f}")
        print(f"  Total Initial:      Rs {total_initial:,.0f}")
        print(f"  Combined Return:    {total_return:.2f}%")
        print(f"  Total Trades:       {total_trades} (Eq: {eq_trades} + Comm: {comm_trades})")
        print(f"  Combined Win Rate:  {total_wins/total_trades*100:.1f}%" if total_trades > 0 else "  Combined Win Rate:  N/A")
        print(f"  Equity Positions:   {len(eq_port.positions) if eq_port else 0}")
        print(f"  Commodity Positions:{len(comm_port.positions) if comm_port else 0}")
        print("=" * 70)
    except Exception:
        pass


def reset_all():
    """Reset both portfolios."""
    try:
        from paper_trader import PaperPortfolio
        eq = PaperPortfolio()
        eq.capital = 300000; eq.positions = []; eq.closed_trades = []; eq.daily_pnl = {}
        eq.save_state()
        print("  Equity portfolio reset to Rs 3,00,000")
    except Exception as e:
        print(f"  Equity reset error: {e}")

    try:
        from commodity_paper_trader import CommodityPortfolio
        comm = CommodityPortfolio()
        comm.capital = 300000; comm.positions = []; comm.closed_trades = []; comm.daily_pnl = {}
        comm.save_state()
        print("  Commodity portfolio reset to Rs 3,00,000")
    except Exception as e:
        print(f"  Commodity reset error: {e}")

    print("  Total starting capital: Rs 6,00,000 (Rs 3L equity + Rs 3L commodity)")


def run_continuous(interval=5, offline=False):
    """Run both systems continuously with proper market hour handling."""
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("\nShutting down paper trading...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)

    # Track API connections
    equity_trader = None
    commodity_trader = None
    equity_connected = False
    commodity_connected = False

    logger.info(f"Scanning every {interval} minutes")
    logger.info(f"Equity hours:    9:15 AM - 3:30 PM")
    logger.info(f"Commodity hours: 9:00 AM - 11:30 PM")
    logger.info(f"Press Ctrl+C to stop\n")

    scan_count = 0

    while running:
        now = datetime.now()
        current_time = now.time()
        scan_count += 1

        logger.info(f"\n{'#'*70}")
        logger.info(f"SCAN #{scan_count} | {now.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'#'*70}")

        # Check if equity market is open
        equity_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE
        # Check if MCX is open
        mcx_open = MCX_OPEN <= current_time <= MCX_CLOSE

        if now.weekday() > 4:  # Saturday/Sunday
            equity_open = False
            mcx_open = False
            logger.info("Weekend - markets closed")

        # Run equity scan if market open
        if equity_open:
            logger.info(f"  Equity market: OPEN")
            run_equity_scan(offline)
        else:
            logger.info(f"  Equity market: CLOSED ({EQUITY_OPEN}-{EQUITY_CLOSE})")

        # Run commodity scan if MCX open
        if mcx_open:
            logger.info(f"  MCX market: OPEN")
            run_commodity_scan(offline)
        else:
            logger.info(f"  MCX market: CLOSED ({MCX_OPEN}-{MCX_CLOSE})")

        # If both markets closed and it's past MCX close, stop
        if not equity_open and not mcx_open and current_time > MCX_CLOSE:
            logger.info("\nAll markets closed for today.")
            show_combined_status()

            # Save end-of-day report
            try:
                import json
                report = {
                    'date': now.strftime('%Y-%m-%d'),
                    'scans': scan_count,
                    'timestamp': now.isoformat(),
                }
                report_file = os.path.join(LOG_DIR, f'eod_report_{today_str}.json')
                with open(report_file, 'w') as f:
                    json.dump(report, f, indent=2)
                logger.info(f"EOD report saved: {report_file}")
            except Exception:
                pass
            break

        # If no market is open yet, wait
        if not equity_open and not mcx_open:
            # Find next market open
            if current_time < MCX_OPEN:
                wait_secs = (datetime.combine(now.date(), MCX_OPEN) - now).total_seconds()
                logger.info(f"  Waiting {wait_secs/60:.0f} min for MCX to open...")
                time.sleep(min(wait_secs, interval * 60))
                continue

        # Normal interval wait
        logger.info(f"\n  Next scan in {interval} minutes...")
        time.sleep(interval * 60)


def main():
    print_banner()

    parser = argparse.ArgumentParser(description='Unified Paper Trading System')
    parser.add_argument('--status', action='store_true', help='Show both portfolio status')
    parser.add_argument('--once', action='store_true', help='Single scan both markets')
    parser.add_argument('--interval', type=int, default=5, help='Scan interval (minutes)')
    parser.add_argument('--offline', action='store_true', help='Run offline (no API)')
    parser.add_argument('--reset', action='store_true', help='Reset both portfolios')
    parser.add_argument('--equity-only', action='store_true', help='Only run equity')
    parser.add_argument('--commodity-only', action='store_true', help='Only run commodity')
    args = parser.parse_args()

    if args.reset:
        reset_all()
        return

    if args.status:
        show_combined_status()
        return

    if args.once:
        if not args.commodity_only:
            run_equity_scan(args.offline)
        if not args.equity_only:
            run_commodity_scan(args.offline)
        show_combined_status()
        return

    # Continuous mode
    run_continuous(args.interval, args.offline)


if __name__ == '__main__':
    main()
