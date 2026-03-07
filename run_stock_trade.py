"""
STOCK OPTIONS PAPER TRADING RUNNER (Standalone)
=================================================
Fully independent runner for the stock options CPR bot.
Runs in its own process with its own Angel login, WebSocket, PID lock.

Does NOT interfere with the existing run_paper_trade.py (equity + commodity).

Usage:
    python run_stock_trade.py                    # Run continuous
    python run_stock_trade.py --once             # Single scan cycle
    python run_stock_trade.py --scan-only        # Run CPR scanner only (for testing)
    python run_stock_trade.py --status           # Show portfolio status
    python run_stock_trade.py --reset            # Reset stock portfolio
    python run_stock_trade.py --interval 30      # Custom scan interval (seconds)
    python run_stock_trade.py --force            # Override PID lock

Deployment:
    systemctl start algo-stock-trading.service    # As systemd service on VPS
"""

import sys
import os
import time
import signal
import logging
import argparse
import json
from datetime import datetime, time as dtime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
LOCK_DIR = os.path.join(BASE_DIR, 'locks')
STOCK_PAPER_DIR = os.path.join(BASE_DIR, 'stock_paper_trades')

for d in [LOG_DIR, LOCK_DIR, STOCK_PAPER_DIR]:
    os.makedirs(d, exist_ok=True)

# PID lock file — separate from equity/commodity
LOCK_FILE = os.path.join(LOCK_DIR, 'stock_trading.pid')

# Market hours (IST)
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 30)
SCANNER_TIME = dtime(8, 30)     # Morning CPR scan

# Default scan interval
DEFAULT_INTERVAL = 30  # seconds

# Logging
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'stock_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('StockRunner')


# ====================================================================
# PID LOCK — Prevent duplicate instances
# ====================================================================
def acquire_lock(force=False):
    """Acquire PID lock file. Exits if another instance is running.

    Args:
        force: If True, override existing lock regardless.
    """
    if force and os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.warning("Lock forcibly removed (--force flag)")

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                data = json.load(f)
            old_pid = data.get('pid')
            try:
                os.kill(old_pid, 0)  # Check if process exists
                logger.error("=" * 70)
                logger.error("STOCK TRADER ALREADY RUNNING!")
                logger.error(f"  PID: {old_pid}")
                logger.error(f"  Started: {data.get('started', 'unknown')}")
                logger.error("  Use --force to override.")
                logger.error("=" * 70)
                sys.exit(1)
            except (OSError, ProcessLookupError):
                logger.warning(f"Stale lock found (PID {old_pid} dead). Overriding.")
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt lock file. Overriding.")

    # Write new lock
    lock_data = {
        'pid': os.getpid(),
        'type': 'stock_options',
        'started': datetime.now().isoformat(),
    }
    with open(LOCK_FILE, 'w') as f:
        json.dump(lock_data, f, indent=2)
    logger.info(f"Lock acquired: PID={os.getpid()}")


def release_lock():
    """Remove PID lock file on shutdown."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Lock released.")
    except Exception as e:
        logger.warning(f"Failed to release lock: {e}")


# ====================================================================
# BANNER
# ====================================================================
def print_banner():
    print("\n" + "=" * 70)
    print("  STOCK OPTIONS CPR BOT — PAPER TRADING SYSTEM v1.0")
    print("  " + "-" * 64)
    print("  MARKET:     NSE F&O Stocks (Monthly Expiry)")
    print("  SCANNER:    Auto narrow CPR scan (~180 stocks)")
    print("  STRATEGIES: CPR Breakout | Gamma Blast | PCR+VWAP")
    print("  EXIT:       3-Phase TSL | OI/IV SL-Tighten | Hold Score")
    print("  CAPITAL:    Rs 2,00,000 (Max 5 positions)")
    print("  " + "-" * 64)
    print(f"  SCAN:       Every {DEFAULT_INTERVAL}s | Hours: 9:15 AM - 3:30 PM IST")
    print("  SCANNER:    Pre-market at 8:30 AM")
    print("=" * 70 + "\n")


# ====================================================================
# MAIN TRADING LOOP
# ====================================================================
def stock_loop(interval_sec=30, stop_event=None):
    """Main stock options trading loop.

    Flow:
    1. Initialize StockPaperTrader
    2. Run morning CPR scan (if before 9:15)
    3. Every interval_sec: run_once() = check_exits + scan + execute
    4. At 3:20 PM: EOD force close
    5. At 3:30 PM: EOD summary and stop

    Args:
        interval_sec: Seconds between scan cycles.
        stop_event: threading.Event to signal shutdown.
    """
    sys.path.insert(0, BASE_DIR)
    from stock_paper_trader import StockPaperTrader

    logger.info(f"[StockLoop] Starting | Interval: {interval_sec}s")

    trader = StockPaperTrader()

    # Initialize (connect to Angel API, load instruments)
    if not trader.initialize():
        logger.error("[StockLoop] Initialization failed!")
        return

    # Morning scan if pre-market
    now = datetime.now().time()
    if now < EQUITY_OPEN:
        logger.info("[StockLoop] Pre-market: Running morning CPR scan...")
        trader.morning_scan()
    elif not trader._todays_watchlist:
        # Market already open but no scan yet — run now
        logger.info("[StockLoop] No watchlist found, running CPR scan now...")
        trader.morning_scan()

    scan_count = 0
    nse_holiday_logged = None

    while not (stop_event and stop_event.is_set()):
        now = datetime.now()
        current_time = now.time()
        is_weekday = now.weekday() <= 4

        # Check NSE holiday
        try:
            from market_holidays import is_nse_holiday
            nse_holiday, holiday_name = is_nse_holiday(now.date())
            if nse_holiday and is_weekday:
                if nse_holiday_logged != now.date():
                    logger.info(f"[StockLoop] NSE HOLIDAY: {holiday_name} — skipping stock trading")
                    nse_holiday_logged = now.date()
                time.sleep(300)
                continue
        except ImportError:
            pass

        # Market hours check
        market_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and is_weekday

        if market_open:
            scan_count += 1
            try:
                trader.run_once()
            except Exception as e:
                logger.error(f"[StockLoop] Scan error: {e}", exc_info=True)

            # Check if market is about to close
            if current_time > EQUITY_CLOSE:
                logger.info("[StockLoop] Market closed. Running EOD summary...")
                trader.eod_summary()
                break

            if stop_event:
                stop_event.wait(interval_sec)
            else:
                time.sleep(interval_sec)

        elif current_time > EQUITY_CLOSE:
            # After market
            if scan_count > 0:
                logger.info("[StockLoop] Market closed after trading. EOD summary...")
                trader.eod_summary()
            break

        elif current_time < EQUITY_OPEN:
            # Pre-market: check if it's time for morning scan
            if current_time >= SCANNER_TIME and not trader._todays_watchlist:
                logger.info("[StockLoop] Scanner time: Running morning CPR scan...")
                trader.morning_scan()

            # Wait until market opens
            wait = 60
            logger.info(f"[StockLoop] Pre-market. Waiting for {EQUITY_OPEN}...")
            if stop_event:
                stop_event.wait(wait)
            else:
                time.sleep(wait)

    logger.info(f"[StockLoop] Trading loop ended. Total scans: {scan_count}")


# ====================================================================
# CLI ENTRY POINT
# ====================================================================
def main():
    parser = argparse.ArgumentParser(description='Stock Options CPR Paper Trading Bot')
    parser.add_argument('--once', action='store_true', help='Run single scan cycle')
    parser.add_argument('--scan-only', action='store_true', help='Run CPR scanner only')
    parser.add_argument('--status', action='store_true', help='Show portfolio status')
    parser.add_argument('--reset', action='store_true', help='Reset stock portfolio')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                        help=f'Scan interval in seconds (default: {DEFAULT_INTERVAL})')
    parser.add_argument('--force', action='store_true', help='Override PID lock')
    args = parser.parse_args()

    print_banner()

    # Status / Reset — no lock needed
    if args.status:
        sys.path.insert(0, BASE_DIR)
        from stock_paper_trader import StockPaperTrader
        trader = StockPaperTrader()
        trader.get_status()
        return

    if args.reset:
        state_file = os.path.join(STOCK_PAPER_DIR, 'stock_portfolio_state.json')
        if os.path.exists(state_file):
            os.remove(state_file)
            logger.info("Stock portfolio state reset.")
        else:
            logger.info("No state file to reset.")
        return

    # Acquire PID lock
    acquire_lock(force=args.force)

    # Graceful shutdown handler
    import threading
    stop_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("Shutdown signal received. Stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.scan_only:
            sys.path.insert(0, BASE_DIR)
            from stock_paper_trader import StockPaperTrader
            trader = StockPaperTrader()
            if trader.initialize():
                watchlist = trader.morning_scan()
                logger.info(f"\nScanner found {len(watchlist)} narrow CPR stocks.")
            else:
                logger.error("Initialization failed.")
            return

        if args.once:
            sys.path.insert(0, BASE_DIR)
            from stock_paper_trader import StockPaperTrader
            trader = StockPaperTrader()
            if trader.initialize():
                if not trader._todays_watchlist:
                    trader.morning_scan()
                trader.run_once()
            else:
                logger.error("Initialization failed.")
            return

        # Continuous mode
        stock_loop(interval_sec=args.interval, stop_event=stop_event)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt. Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        release_lock()
        logger.info("Stock trader stopped.")


if __name__ == '__main__':
    main()
