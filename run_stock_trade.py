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

_stk_err_count = 0  # v13.7: Auto-recovery counter (legacy — replaced by error_reactor v16)

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
DEFAULT_INTERVAL = 30  # seconds (was 1 — caused 1s scan loop, 26MB log spam)

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
def stock_loop(interval_sec=1, stop_event=None):
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

    # v16: Error reactor for immediate error classification + action
    try:
        from error_reactor import classify_error, ErrorTracker, HeartbeatWriter, send_watchdog_alert, write_crash_dump, try_reconnect
        _stk_tracker = ErrorTracker()
        _stk_heartbeat = HeartbeatWriter()
        logger.info("[StockLoop] Error reactor + heartbeat initialized")
    except ImportError:
        _stk_tracker = None
        _stk_heartbeat = None
        try_reconnect = None
        logger.warning("[StockLoop] error_reactor not available — using legacy error handling")

    logger.info(f"[StockLoop] Starting | Interval: {interval_sec}s")

    trader = StockPaperTrader()

    # v10.5: Start market data pipeline for NSE stock option chain fallback
    try:
        from market_data_pipeline import MarketDataPipeline
        pipeline = MarketDataPipeline()
        pipeline.start()
        trader.set_market_pipeline(pipeline)
        logger.info("[StockLoop] MarketDataPipeline ACTIVE — NSE stock option chain fallback enabled")
    except Exception as e:
        logger.warning(f"[StockLoop] MarketDataPipeline failed: {e} — NSE fallback disabled")

    # v10.2e: Live data logger for backtesting
    try:
        from live_data_logger import LiveDataLogger
        trader.data_logger = LiveDataLogger()
        logger.info("[StockLoop] LiveDataLogger ACTIVE — saving stock data to data/live/")
    except Exception as e:
        logger.warning(f"[StockLoop] LiveDataLogger failed: {e} — data logging disabled")

    # Initialize (connect to Angel API, load instruments)
    if not trader.initialize():
        logger.error("[StockLoop] Initialization failed!")
        return

    # v9.3: Morning scan — only if before market close (prevents restart-loop spam)
    now = datetime.now().time()
    if now < EQUITY_OPEN:
        if now >= SCANNER_TIME:
            logger.info("[StockLoop] Pre-market: Running morning CPR scan...")
            trader.morning_scan()
        else:
            logger.info(f"[StockLoop] Too early for scan. Waiting for {SCANNER_TIME}...")
    elif now <= EQUITY_CLOSE:
        # Market open but no scan yet — run now
        if not trader._todays_watchlist:
            logger.info("[StockLoop] No watchlist found, running CPR scan now...")
            trader.morning_scan()
    else:
        # v9.3: After market hours — DO NOT run morning scan (prevents Telegram spam on restart)
        logger.info("[StockLoop] After market hours. Sleeping until next trading day...")

    scan_count = 0
    eod_done = False
    nse_holiday_logged = None
    last_health_check = datetime.now()  # v9.2: Token health check timer

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
                if stop_event:
                    stop_event.wait(300)
                else:
                    time.sleep(300)
                continue
        except ImportError:
            pass

        # Market hours check
        market_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and is_weekday

        if market_open:
            scan_count += 1
            eod_done = False  # Reset for today
            # v9.2: Proactive token health check every 30 min
            if (datetime.now() - last_health_check).total_seconds() > 1800:
                try:
                    if hasattr(trader, 'angel') and trader.angel:
                        trader.angel.check_token_health()
                except Exception as e:
                    logger.warning(f"[StockLoop] Token health check failed: {e}")
                last_health_check = datetime.now()
            try:
                trader.run_once()
                # v16: Reset error counter + write heartbeat on success
                if _stk_tracker:
                    _stk_tracker.reset('stock')
                if _stk_heartbeat:
                    _stk_heartbeat.write('stock', scan_count)
            except Exception as e:
                # v16: EMERGENCY — try to protect open trades even though run_once() failed
                try:
                    if hasattr(trader, 'check_exits') and trader.portfolio.positions:
                        logger.warning(f"[StockLoop] EMERGENCY EXIT CHECK — {len(trader.portfolio.positions)} open positions")
                        trader.check_exits()
                except Exception as exit_err:
                    logger.error(f"[StockLoop] Emergency exit check also failed: {exit_err}")

                # v16: Error reactor — reconnect first, restart only as last resort
                if _stk_tracker:
                    severity, code, _ = classify_error(e)
                    logger.error(f"[StockLoop] {severity} {code}: {e}", exc_info=True)
                    action = _stk_tracker.record('stock', severity, code)
                    send_watchdog_alert(severity, code, str(e), action)
                    if severity == 'FATAL':
                        write_crash_dump('stock', e)

                    if action == 'reconnect':
                        if try_reconnect(trader, None, 'stock'):
                            _stk_tracker.reconnect_succeeded('stock')
                            logger.info("[StockLoop] Reconnected — continuing trading")
                        else:
                            final = _stk_tracker.reconnect_failed('stock')
                            if final == 'restart_service':
                                logger.error("[StockLoop] 3 reconnect failures → RESTART algo-stock-trading (last resort)")
                                import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-stock-trading'])
                                break
                    elif action == 'restart_service':
                        logger.error(f"[StockLoop] Error reactor → RESTART algo-stock-trading")
                        import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-stock-trading'])
                        break
                else:
                    # Legacy fallback
                    logger.error(f"[StockLoop] Scan error: {e}", exc_info=True)
                    global _stk_err_count
                    _stk_err_count += 1
                    if _stk_err_count >= 10:
                        logger.error(f"[StockLoop] AUTO-RECOVERY: {_stk_err_count} consecutive errors — restarting")
                        import subprocess; subprocess.Popen(['systemctl', 'restart', 'algo-stock-trading'])
                        break

            if stop_event:
                stop_event.wait(interval_sec)
            else:
                time.sleep(interval_sec)

        elif current_time > EQUITY_CLOSE:
            # v9.3: After market — run EOD summary once, then sleep (don't break/exit)
            if not eod_done:
                if scan_count > 0:
                    logger.info("[StockLoop] Market closed. Running EOD summary...")
                    trader.eod_summary()
                else:
                    logger.info("[StockLoop] Market closed (no scans today).")
                eod_done = True

            # Sleep 5 min, stay alive to prevent systemd restart → Telegram spam
            if stop_event:
                stop_event.wait(300)
            else:
                time.sleep(300)

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
