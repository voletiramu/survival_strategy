"""
OI PAPER TRADING RUNNER (Standalone)
======================================
Fully independent runner for the OI-based strategies bot.
Runs in its own process with own MarketDataPipeline, Angel login, PID lock.

Does NOT interfere with run_paper_trade.py or run_stock_trade.py.

Strategies:
    - OI Wall Bouncer: BUY at OI wall support/resistance
    - Max Pain Magnet: BUY toward max pain convergence
    - VWAP Bounce: BUY on VWAP mean-reversion with OI confirmation

Usage:
    python run_oi_trade.py               # Run continuous
    python run_oi_trade.py --once        # Single scan cycle
    python run_oi_trade.py --status      # Show portfolio status
    python run_oi_trade.py --reset       # Reset OI portfolio
    python run_oi_trade.py --interval 180 # Custom scan interval (seconds)
    python run_oi_trade.py --force       # Override PID lock

Deployment:
    systemctl start algo-oi-trading.service
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
OI_PAPER_DIR = os.path.join(BASE_DIR, 'paper_trades_oi')

for d in [LOG_DIR, LOCK_DIR, OI_PAPER_DIR]:
    os.makedirs(d, exist_ok=True)

# PID lock file — separate from equity/commodity and stock bots
LOCK_FILE = os.path.join(LOCK_DIR, 'oi_trading.pid')

# Market hours (IST)
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 30)
PIPELINE_START = dtime(9, 10)  # Start pipeline 5 min before market

# Default scan interval: 3 minutes (matches pipeline fetch cycle)
DEFAULT_INTERVAL = 180

# Logging
today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'oi_paper_{today_str}.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('OIRunner')


# ====================================================================
# PID LOCK — Prevent duplicate instances
# ====================================================================
def acquire_lock(force=False):
    """Acquire PID lock file. Exits if another instance is running."""
    if force and os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.warning("Lock forcibly removed (--force flag)")

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                data = json.load(f)
            old_pid = data.get('pid')
            try:
                os.kill(old_pid, 0)
                logger.error("=" * 70)
                logger.error("OI TRADER ALREADY RUNNING!")
                logger.error(f"  PID: {old_pid}")
                logger.error(f"  Started: {data.get('started', 'unknown')}")
                logger.error("  Use --force to override.")
                logger.error("=" * 70)
                sys.exit(1)
            except (OSError, ProcessLookupError):
                logger.warning(f"Stale lock found (PID {old_pid} dead). Overriding.")
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt lock file. Overriding.")

    lock_data = {
        'pid': os.getpid(),
        'type': 'oi_trading',
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
    print("  OI PAPER TRADING BOT v1.0")
    print("  " + "-" * 64)
    print("  MARKET:     NSE/BSE Indices (NIFTY, BANKNIFTY, SENSEX)")
    print("  STRATEGIES: OI Wall Bouncer | Max Pain Magnet | VWAP Bounce")
    print("  EXIT:       3-Phase TSL | Hold Score | Time Exit | EOD Close")
    print("  CAPITAL:    Rs 3,00,000 (Max 4 positions)")
    print("  " + "-" * 64)
    print(f"  SCAN:       Every {DEFAULT_INTERVAL}s | Hours: 9:15 AM - 3:30 PM IST")
    print("  DATA:       Real per-strike OI from NSE/BSE option chains")
    print("=" * 70 + "\n")


# ====================================================================
# MAIN TRADING LOOP
# ====================================================================
def oi_loop(interval_sec=180, stop_event=None):
    """Main OI trading loop.

    Flow:
    1. Initialize MarketDataPipeline + OIPaperTrader
    2. Every interval_sec: run_once() = scan + check_exits + execute
    3. At 3:20 PM: EOD force close
    4. At 3:30 PM: EOD summary and stop
    """
    sys.path.insert(0, BASE_DIR)
    from market_data_pipeline import MarketDataPipeline
    from oi_paper_trader import OIPaperTrader

    logger.info(f"[OILoop] Starting | Interval: {interval_sec}s")

    # Initialize MarketDataPipeline
    logger.info("[OILoop] Initializing MarketDataPipeline...")
    pipeline = MarketDataPipeline()

    # Initialize trader
    trader = OIPaperTrader(market_pipeline=pipeline)
    if not trader.initialize():
        logger.error("[OILoop] Trader initialization failed!")
        return

    scan_count = 0
    pipeline_started = False
    nse_holiday_logged = None
    last_heartbeat = datetime.now()

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
                    logger.info(f"[OILoop] NSE HOLIDAY: {holiday_name}")
                    nse_holiday_logged = now.date()
                time.sleep(300)
                continue
        except ImportError:
            pass

        # Start pipeline near market open
        if not pipeline_started and current_time >= PIPELINE_START and is_weekday:
            logger.info("[OILoop] Starting MarketDataPipeline background fetch...")
            try:
                pipeline.start()
                pipeline_started = True
                # Wait for first data fetch
                time.sleep(15)
                logger.info("[OILoop] Pipeline started, waiting for initial data...")
            except Exception as e:
                logger.error(f"[OILoop] Pipeline start failed: {e}")

        # Market hours check
        market_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and is_weekday

        if market_open:
            scan_count += 1
            try:
                trader.run_once()
            except Exception as e:
                logger.error(f"[OILoop] Scan error: {e}", exc_info=True)

            # Heartbeat every 30 min
            if (datetime.now() - last_heartbeat).total_seconds() > 1800:
                logger.info(f"[OILoop] Heartbeat: scan #{scan_count} | "
                            f"signals={trader.daily_signal_count} | "
                            f"trades={trader.daily_trade_count} | "
                            f"positions={len(trader.portfolio.positions)}")
                last_heartbeat = datetime.now()
                # v9.2: Proactive token health check
                try:
                    if hasattr(trader, 'angel') and trader.angel:
                        trader.angel.check_token_health()
                except Exception as e:
                    logger.warning(f"[OILoop] Token health check failed: {e}")

            # Check if market closing
            if current_time > EQUITY_CLOSE:
                logger.info("[OILoop] Market closed. Running EOD summary...")
                trader.eod_summary()
                break

            if stop_event:
                stop_event.wait(interval_sec)
            else:
                time.sleep(interval_sec)

        elif current_time > EQUITY_CLOSE:
            # After market
            if scan_count > 0:
                logger.info("[OILoop] Market closed after trading. EOD summary...")
                trader.eod_summary()
            break

        else:
            # Pre-market
            logger.info(f"[OILoop] Pre-market. Waiting for {EQUITY_OPEN}...")
            if stop_event:
                stop_event.wait(60)
            else:
                time.sleep(60)

    # Cleanup
    if pipeline_started:
        try:
            pipeline.stop()
            logger.info("[OILoop] Pipeline stopped.")
        except Exception:
            pass

    logger.info(f"[OILoop] Trading loop ended. Total scans: {scan_count}")


# ====================================================================
# CLI ENTRY POINT
# ====================================================================
def main():
    parser = argparse.ArgumentParser(
        description='OI-Based Paper Trading Bot (OI Wall, Max Pain, VWAP Bounce)')
    parser.add_argument('--once', action='store_true', help='Run single scan cycle')
    parser.add_argument('--status', action='store_true', help='Show portfolio status')
    parser.add_argument('--reset', action='store_true', help='Reset OI portfolio')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                        help=f'Scan interval in seconds (default: {DEFAULT_INTERVAL})')
    parser.add_argument('--force', action='store_true', help='Override PID lock')
    args = parser.parse_args()

    print_banner()

    # Status / Reset — no lock needed
    if args.status:
        sys.path.insert(0, BASE_DIR)
        from oi_paper_trader import OIPaperTrader
        trader = OIPaperTrader()
        trader.get_status()
        return

    if args.reset:
        state_file = os.path.join(OI_PAPER_DIR, 'portfolio_state.json')
        if os.path.exists(state_file):
            os.remove(state_file)
            logger.info("OI portfolio state reset.")
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
        if args.once:
            sys.path.insert(0, BASE_DIR)
            from market_data_pipeline import MarketDataPipeline
            from oi_paper_trader import OIPaperTrader

            logger.info("[Once] Initializing pipeline + trader...")
            pipeline = MarketDataPipeline()

            # Fetch data once
            logger.info("[Once] Fetching option chain data...")
            pipeline.fetch_once()
            time.sleep(2)

            trader = OIPaperTrader(market_pipeline=pipeline)
            trader.initialize()
            trader.run_once()
            logger.info("[Once] Single scan complete.")
            return

        # Continuous mode
        oi_loop(interval_sec=args.interval, stop_event=stop_event)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt. Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        release_lock()
        logger.info("OI trader stopped.")


if __name__ == '__main__':
    main()
