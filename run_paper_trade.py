"""
UNIFIED PAPER TRADING LAUNCHER (Threaded)
==========================================
Runs equity, commodity, and crypto paper trading in SEPARATE THREADS
with independent scan intervals.

Equity:    NIFTY, BANKNIFTY, SENSEX          every 45 seconds (9:15-15:30)
Commodity: Gold Mini, Silver Mini, Crude Oil  every 30 seconds (9:00-23:30)
Crypto:    BTC, ETH, SOL                      every 5 minutes  (24/7)

Usage:
    python run_paper_trade.py                   # Run all systems (threaded)
    python run_paper_trade.py --once            # Single scan both
    python run_paper_trade.py --offline         # Offline test both
    python run_paper_trade.py --status          # Show both portfolios
    python run_paper_trade.py --reset           # Reset both portfolios
    python run_paper_trade.py --equity-interval 0.75 --commodity-interval 0.5
"""

import sys
import os
import time
import signal
import logging
import argparse
import threading
import json
from datetime import datetime, time as dtime

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
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
COMMODITY_TRADE_START = dtime(9, 15)  # Commodity trades from 9:15 AM (no time barrier)

# Holiday calendar
from market_holidays import is_nse_holiday, is_mcx_holiday

# Default scan intervals (seconds)
# With WebSocket real-time LTP, we can scan faster (no REST API call for spot prices)
DEFAULT_EQUITY_INTERVAL = 15    # 15 seconds — was 45s before WebSocket
DEFAULT_COMMODITY_INTERVAL = 10  # 10 seconds — was 30s before WebSocket
DEFAULT_CRYPTO_INTERVAL = 300    # 5 minutes for BTC, ETH, SOL (Binance, not Angel)

# Instance identification & PID lock
LOCK_DIR = os.path.join(BASE_DIR, 'locks')
LOCK_FILE = os.path.join(LOCK_DIR, 'trading.pid')


def acquire_lock(instance_id, force=False):
    """Acquire PID lock file. Exits if another instance is running on this machine.

    Args:
        instance_id: 'local' or 'vultr' (auto-detected from TRADING_INSTANCE env)
        force: If True, override existing lock regardless
    """
    os.makedirs(LOCK_DIR, exist_ok=True)

    if force and os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.warning("Lock forcibly removed (--force flag)")

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                data = json.load(f)
            old_pid = data.get('pid')
            old_instance = data.get('instance', 'unknown')
            # Check if the old process is still alive
            try:
                os.kill(old_pid, 0)  # Signal 0 = check existence, doesn't kill
                # Process is alive — another instance is running on THIS machine
                logger.error("=" * 70)
                logger.error("ANOTHER INSTANCE ALREADY RUNNING on this machine!")
                logger.error(f"  Instance: {old_instance}, PID: {old_pid}")
                logger.error(f"  Started: {data.get('started', 'unknown')}")
                logger.error(f"  Lock file: {LOCK_FILE}")
                logger.error("  Use --force to override, or stop the other instance first.")
                logger.error("=" * 70)
                sys.exit(1)
            except (OSError, ProcessLookupError):
                # Process is dead — stale lock, safe to override
                logger.warning(f"Stale lock found (PID {old_pid} dead). Overriding.")
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt lock file. Overriding.")

    # Write new lock
    lock_data = {
        'pid': os.getpid(),
        'instance': instance_id,
        'started': datetime.now().isoformat(),
    }
    with open(LOCK_FILE, 'w') as f:
        json.dump(lock_data, f, indent=2)
    logger.info(f"Lock acquired: instance={instance_id}, PID={os.getpid()}")


def release_lock():
    """Remove PID lock file on shutdown."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Lock released.")
    except Exception as e:
        logger.warning(f"Failed to release lock: {e}")


def get_instance_label(instance_id):
    """Get human-readable label for instance ID."""
    if instance_id == 'vultr':
        return '☁️ Vultr VPS'
    else:
        return '🖥️ Local Machine'


def print_banner():
    print("\n" + "=" * 70)
    print("  UNIFIED ALGO TRADING - PAPER TRADING SYSTEM (Threaded)")
    print("  " + "-" * 64)
    print("  EQUITY:     NIFTY | BANKNIFTY | SENSEX")
    print("              Strategies: CPR, Gamma Blast, Ghost Zone (v7), PCR+VWAP")
    print("              [Survivor HALTED — needs more capital]")
    print(f"              Scan: Every {DEFAULT_EQUITY_INTERVAL}s (WebSocket LTP) | Hours: 9:15 AM - 3:30 PM IST")
    print("  " + "-" * 64)
    print("  COMMODITY:  Gold Mini | Silver Mini | Crude Oil Mini")
    print("              Strategies: CPR, Gamma Blast, Ghost Zone")
    print(f"              Scan: Every {DEFAULT_COMMODITY_INTERVAL}s (WebSocket LTP) | Hours: 9:00 AM - 11:30 PM IST")
    print("  " + "-" * 64)
    print("  CRYPTO:     BTC | ETH | SOL (24/7)")
    print("  " + "-" * 64)
    print(f"  Capital: Rs 6,00,000 (Rs 3L equity + Rs 3L commodity)")
    print("=" * 70 + "\n")


def run_equity_scan(offline=False):
    """Run equity paper trading scan (creates new trader each call - for --once mode)."""
    try:
        sys.path.insert(0, BASE_DIR)
        from paper_trader import PaperTrader

        logger.info("=" * 50 + " EQUITY INDEX SCAN " + "=" * 50)

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
    """Run commodity paper trading scan (creates new trader each call - for --once mode)."""
    try:
        sys.path.insert(0, BASE_DIR)
        from commodity_paper_trader import CommodityPaperTrader, PAPER_TRADE_COMMODITIES

        logger.info("=" * 50 + " COMMODITY MCX SCAN " + "=" * 50)

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


# ====================================================================
# THREADED LOOP FUNCTIONS
# ====================================================================

# Shared dict for passing trader references from threads to main (for EOD report)
_trader_refs = {}

def equity_loop(interval_sec=15, offline=False, stop_event=None, ws_feed=None, market_pipeline=None):
    """Equity scanning thread: NIFTY, BANKNIFTY, SENSEX every 15 seconds.
    Creates ONE PaperTrader instance and reuses it across scans.
    Uses WebSocket feed for real-time LTP (falls back to REST if WS unavailable).
    v9: Uses MarketDataPipeline for real PCR, OI sentiment, VIX from NSE/BSE direct.
    """
    logger.info(f"[EquityThread] Starting | Interval: {interval_sec}s | Hours: {EQUITY_OPEN}-{EQUITY_CLOSE}")

    try:
        sys.path.insert(0, BASE_DIR)
        from paper_trader import PaperTrader

        trader = PaperTrader(ws_feed=ws_feed, market_pipeline=market_pipeline)
        _trader_refs['equity'] = trader  # Share for EOD report
        for symbol in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            trader.engine.load_historical(symbol)

        if not offline:
            try:
                if not trader.connect():
                    logger.warning("[EquityThread] Angel API connect returned False, continuing with offline data")
            except Exception as e:
                logger.warning(f"[EquityThread] Angel connection error: {e}. Continuing with offline data.")

        scan_count = 0
        _last_signal_time = {}  # v7.7: Track last signal time per symbol for gap alerts
        _SIGNAL_GAP_MINUTES = 60  # Alert if no signals for 60 min during market hours

        _nse_holiday_logged = None  # Prevent log spam on holidays

        while not stop_event.is_set():
            now = datetime.now()
            current_time = now.time()
            is_weekday = now.weekday() <= 4

            # Check NSE holiday calendar
            nse_holiday, holiday_name = is_nse_holiday(now.date())
            if nse_holiday and is_weekday:
                if _nse_holiday_logged != now.date():
                    logger.info(f"[EquityThread] NSE HOLIDAY: {holiday_name} — skipping equity trading")
                    _nse_holiday_logged = now.date()
                stop_event.wait(300)  # Sleep 5 min on holidays
                continue

            equity_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and is_weekday

            if equity_open:
                scan_count += 1
                logger.info(f"[EquityThread] Scan #{scan_count} | {now.strftime('%H:%M:%S')}")
                try:
                    trader.run_once()
                    # v7.7: Track signal times and detect gaps
                    for sym in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
                        if any(s.get('symbol') == sym for s in getattr(trader, 'daily_signals_all', [])):
                            _last_signal_time[sym] = now
                        elif sym in _last_signal_time:
                            gap_mins = (now - _last_signal_time[sym]).total_seconds() / 60
                            if gap_mins >= _SIGNAL_GAP_MINUTES:
                                try:
                                    from trade_notifier import notify_signal_gap
                                    notify_signal_gap('EQUITY', sym, gap_mins,
                                                     _last_signal_time[sym].strftime('%H:%M'))
                                except Exception:
                                    pass
                        elif scan_count > 30 and sym not in _last_signal_time:
                            # 30+ scans with zero signals ever for this symbol
                            _last_signal_time[sym] = now  # Set to now to trigger gap after next period
                            try:
                                from trade_notifier import notify_data_issue
                                notify_data_issue('EQUITY', 'NO_SIGNALS',
                                                 f"{sym}: Zero signals after {scan_count} scans")
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"[EquityThread] Scan error: {e}")
                    import traceback
                    traceback.print_exc()
                    # v7.2: Send Telegram notification for scan failure
                    try:
                        from trade_notifier import notify_scan_failure
                        notify_scan_failure('EQUITY', str(e), 'EquityThread')
                    except Exception:
                        pass
            elif current_time > EQUITY_CLOSE and is_weekday:
                logger.info("[EquityThread] Equity market closed for today.")
                # v7.2: Send equity EOD summary at market close (3:30 PM)
                try:
                    eq_trader = _trader_refs.get('equity')
                    if eq_trader:
                        eq_summary = eq_trader.get_eod_summary()
                        eq_wins = sum(1 for t in eq_trader.portfolio.closed_trades if t.get('pnl', 0) > 0)
                        eq_closed = eq_summary.get('closed_trades', 0)
                        eq_wr = (eq_wins / eq_closed * 100) if eq_closed > 0 else 0
                        from trade_notifier import send_message
                        msg = (
                            f"<b>📊 EQUITY SESSION CLOSED (3:30 PM)</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"<b>Capital:</b> Rs {eq_trader.portfolio.capital:,.0f}\n"
                            f"<b>PnL:</b> Rs {eq_summary.get('actual_pnl', 0):,.0f}\n"
                            f"<b>Open:</b> {eq_summary.get('open_positions', 0)} | "
                            f"<b>Closed:</b> {eq_closed}\n"
                            f"<b>Win Rate:</b> {eq_wr:.0f}%\n"
                            f"<b>Signals:</b> {eq_summary.get('total_signals', 0)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"Commodity session continues until 11:30 PM"
                        )
                        send_message(msg)
                except Exception as eod_err:
                    logger.error(f"[EquityThread] EOD summary error: {eod_err}")
                break
            elif not is_weekday:
                logger.info("[EquityThread] Weekend - equity market closed. Waiting...")

            stop_event.wait(interval_sec)

    except Exception as e:
        logger.error(f"[EquityThread] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        # v7.2: Notify fatal error
        try:
            from trade_notifier import notify_scan_failure
            notify_scan_failure('EQUITY', f'FATAL: {str(e)}', 'EquityThread')
        except Exception:
            pass

    logger.info(f"[EquityThread] Exited after {scan_count if 'scan_count' in dir() else 0} scans")


def commodity_loop(interval_sec=10, offline=False, stop_event=None, ws_feed=None):
    """Commodity scanning thread: GOLDM, SILVERM, CRUDEOILM every 10 seconds.
    Creates ONE CommodityPaperTrader instance and reuses it across scans.
    Uses WebSocket feed for real-time LTP (falls back to REST if WS unavailable).
    """
    logger.info(f"[CommodityThread] Starting | Interval: {interval_sec}s | Hours: {MCX_OPEN}-{MCX_CLOSE}")

    try:
        sys.path.insert(0, BASE_DIR)
        from commodity_paper_trader import CommodityPaperTrader, PAPER_TRADE_COMMODITIES

        trader = CommodityPaperTrader(ws_feed=ws_feed)
        _trader_refs['commodity'] = trader  # Share for EOD report
        for comm in PAPER_TRADE_COMMODITIES:
            trader.engine.load_historical(comm)

        if not offline:
            try:
                if not trader.connect():
                    logger.warning("[CommodityThread] Angel API connect returned False, continuing with offline data")
            except Exception as e:
                logger.warning(f"[CommodityThread] Angel connection error: {e}. Continuing with offline data.")

        scan_count = 0
        _last_comm_signal_time = {}  # v7.7: Track last signal time per commodity
        _COMM_SIGNAL_GAP_MINUTES = 90  # MCX has longer gaps, alert after 90 min

        _mcx_holiday_logged = None  # Prevent log spam on holidays

        while not stop_event.is_set():
            now = datetime.now()
            current_time = now.time()
            is_weekday = now.weekday() <= 4

            # Check MCX holiday calendar (supports partial holidays)
            mcx_closed, mcx_reason = is_mcx_holiday(now.date(), current_time)
            if mcx_closed and is_weekday:
                if _mcx_holiday_logged != (now.date(), current_time.hour):
                    logger.info(f"[CommodityThread] MCX CLOSED: {mcx_reason}")
                    _mcx_holiday_logged = (now.date(), current_time.hour)
                stop_event.wait(300)  # Sleep 5 min on holidays
                continue

            mcx_open = MCX_OPEN <= current_time <= MCX_CLOSE and is_weekday
            commodity_trade_ok = current_time >= COMMODITY_TRADE_START

            if mcx_open and commodity_trade_ok:
                scan_count += 1
                logger.info(f"[CommodityThread] Scan #{scan_count} | {now.strftime('%H:%M:%S')}")
                try:
                    trader.run_once()
                    # v7.7: Track commodity signal gaps
                    for comm in PAPER_TRADE_COMMODITIES:
                        if any(s.get('commodity') == comm for s in getattr(trader, 'daily_signals_all', [])):
                            _last_comm_signal_time[comm] = now
                        elif comm in _last_comm_signal_time:
                            gap_mins = (now - _last_comm_signal_time[comm]).total_seconds() / 60
                            if gap_mins >= _COMM_SIGNAL_GAP_MINUTES:
                                try:
                                    from trade_notifier import notify_signal_gap
                                    notify_signal_gap('COMMODITY', comm, gap_mins,
                                                     _last_comm_signal_time[comm].strftime('%H:%M'))
                                except Exception:
                                    pass
                except Exception as e:
                    logger.error(f"[CommodityThread] Scan error: {e}")
                    import traceback
                    traceback.print_exc()
                    # v7.2: Send Telegram notification for scan failure
                    try:
                        from trade_notifier import notify_scan_failure
                        notify_scan_failure('COMMODITY', str(e), 'CommodityThread')
                    except Exception:
                        pass
            elif mcx_open and not commodity_trade_ok:
                logger.info(f"[CommodityThread] MCX open but trades blocked until {COMMODITY_TRADE_START}")
            elif current_time > MCX_CLOSE and is_weekday:
                logger.info("[CommodityThread] MCX closed for today.")
                break
            elif not is_weekday:
                logger.info("[CommodityThread] Weekend - MCX closed. Waiting...")

            stop_event.wait(interval_sec)

    except Exception as e:
        logger.error(f"[CommodityThread] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        # v7.2: Notify fatal error
        try:
            from trade_notifier import notify_scan_failure
            notify_scan_failure('COMMODITY', f'FATAL: {str(e)}', 'CommodityThread')
        except Exception:
            pass

    logger.info(f"[CommodityThread] Exited after {scan_count if 'scan_count' in dir() else 0} scans")


def crypto_loop(interval_sec=300, stop_event=None):
    """Crypto scanning thread: DISABLED v2.5.1 — user requested halt."""
    logger.info(f"[CryptoThread] DISABLED by user request (v2.5.1). Exiting immediately.")
    return  # v2.5.1: Crypto halted


def _send_heartbeat(instance_id='local'):
    """Log heartbeat locally (no Telegram spam — user wants fewer messages)."""
    logger.info(f"  [Heartbeat] System alive at {datetime.now().strftime('%H:%M')}")

    # v9.2: Proactive token health check — re-authenticate before expiry
    try:
        eq_trader = _trader_refs.get('equity')
        if eq_trader and hasattr(eq_trader, 'angel') and eq_trader.angel:
            eq_trader.angel.check_token_health()
    except Exception as e:
        logger.warning(f"  [Heartbeat] Equity token health check failed: {e}")

    try:
        comm_trader = _trader_refs.get('commodity')
        if comm_trader and hasattr(comm_trader, 'angel') and comm_trader.angel:
            comm_trader.angel.check_token_health()
    except Exception as e:
        logger.warning(f"  [Heartbeat] Commodity token health check failed: {e}")


def show_combined_status():
    """Show both equity and commodity portfolio status."""
    print_banner()

    eq_port = None
    comm_port = None

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
        total_initial = 600000  # Rs 6L total (3L equity + 3L commodity)
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
    """Reset both portfolios to Rs 3L each (Rs 6L total)."""
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


# ====================================================================
# MAIN CONTINUOUS RUNNER (Threaded)
# ====================================================================

def run_continuous(equity_interval=15, commodity_interval=10, crypto_interval=300,
                   offline=False, instance_id='local', force=False):
    """Run all systems in SEPARATE THREADS with independent scan intervals.

    Args:
        equity_interval: Seconds between equity scans (default 15 — fast with WebSocket LTP)
        commodity_interval: Seconds between commodity scans (default 10 — fast with WebSocket LTP)
        crypto_interval: Seconds between crypto scans (default 300)
        offline: If True, don't connect to Angel API
        instance_id: 'local' or 'vultr' (identifies this instance)
        force: If True, override existing PID lock
    """
    # Acquire PID lock — prevents duplicate instances on same machine
    acquire_lock(instance_id, force=force)

    stop_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("\n" + "=" * 70)
        logger.info("SHUTDOWN SIGNAL RECEIVED - stopping all threads...")
        logger.info("=" * 70)
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ---- Start WebSocket real-time feed (shared by equity + commodity) ----
    ws_feed = None
    if not offline:
        try:
            from ws_feed import create_ws_feed_from_creds
            ws_feed, _ws_obj = create_ws_feed_from_creds()
            if ws_feed and ws_feed.is_connected:
                logger.info("[MAIN] WebSocket real-time feed ACTIVE — LTP from WebSocket")
            else:
                logger.warning("[MAIN] WebSocket feed failed to start — using REST API fallback")
                ws_feed = None
        except Exception as e:
            logger.warning(f"[MAIN] WebSocket setup failed: {e} — using REST API fallback")
            ws_feed = None

    # ---- v9: Start MarketDataPipeline (NSE/BSE direct option chain feed) ----
    market_pipeline = None
    if not offline:
        try:
            from market_data_pipeline import MarketDataPipeline
            market_pipeline = MarketDataPipeline()
            market_pipeline.start()
            logger.info("[MAIN] MarketDataPipeline ACTIVE — Real PCR from NSE/BSE every 3 min")
        except Exception as e:
            logger.warning(f"[MAIN] MarketDataPipeline failed to start: {e} — using Angel API fallback")
            market_pipeline = None

    # v7.2: Detect auto-restart (if lock file existed, this is a restart, not first start)
    instance_label = get_instance_label(instance_id)
    is_restart = os.path.exists(os.path.join(LOCK_DIR, 'last_shutdown.flag'))
    try:
        if is_restart:
            from trade_notifier import notify_service_restart
            notify_service_restart(instance_id=instance_id, reason='Auto-restart by systemd')
            logger.info("[MAIN] Detected auto-restart — Telegram notified")
            # Clean up flag
            os.remove(os.path.join(LOCK_DIR, 'last_shutdown.flag'))
        else:
            from trade_notifier import notify_scanner_start
            notify_scanner_start(instance_id=instance_id)
    except Exception as e:
        logger.warning(f"Telegram startup notify failed: {e}")

    pipeline_status = "NSE/BSE Pipeline" if market_pipeline else "Angel API only"
    ws_status = "WebSocket" if ws_feed else "REST API polling"
    logger.info("=" * 70)
    logger.info(f"THREADED PAPER TRADING SYSTEM STARTING — {instance_label}")
    logger.info("=" * 70)
    logger.info(f"  Instance:           {instance_id} ({instance_label})")
    logger.info(f"  LTP feed:           {ws_status}")
    logger.info(f"  Option chain:       {pipeline_status}")
    logger.info(f"  Equity interval:    {equity_interval}s ({equity_interval/60:.1f} min)")
    logger.info(f"  Commodity interval: {commodity_interval}s ({commodity_interval/60:.1f} min)")
    logger.info(f"  Crypto interval:    {crypto_interval}s ({crypto_interval/60:.1f} min)")
    logger.info(f"  Equity hours:       {EQUITY_OPEN} - {EQUITY_CLOSE}")
    logger.info(f"  Commodity hours:    {MCX_OPEN} - {MCX_CLOSE}")
    logger.info(f"  Crypto hours:       24/7")
    logger.info(f"  Capital:            Rs 3,00,000 (equity) + Rs 3,00,000 (commodity)")
    logger.info(f"  PID:                {os.getpid()}")
    logger.info(f"  Press Ctrl+C to stop")
    logger.info("=" * 70 + "\n")

    # Create threads (pass ws_feed + market_pipeline to equity, ws_feed to commodity)
    t_equity = threading.Thread(
        target=equity_loop,
        args=(equity_interval, offline, stop_event, ws_feed, market_pipeline),
        name="EquityThread",
        daemon=True
    )

    t_commodity = threading.Thread(
        target=commodity_loop,
        args=(commodity_interval, offline, stop_event, ws_feed),
        name="CommodityThread",
        daemon=True
    )

    t_crypto = threading.Thread(
        target=crypto_loop,
        args=(crypto_interval, stop_event),
        name="CryptoThread",
        daemon=True
    )

    threads = [t_equity, t_commodity, t_crypto]

    # Start threads with stagger to avoid Angel API rate limiting
    for i, t in enumerate(threads):
        t.start()
        logger.info(f"  Started thread: {t.name}")
        if i < len(threads) - 1:
            time.sleep(5)  # 5s gap between thread starts to avoid API rate limit

    # Main thread: heartbeat + monitor
    heartbeat_interval = 1800  # 30 minutes
    while not stop_event.is_set():
        stop_event.wait(heartbeat_interval)
        if not stop_event.is_set():
            _send_heartbeat(instance_id)

            # Check if both market threads have exited (market closed)
            if not t_equity.is_alive() and not t_commodity.is_alive():
                logger.info("All market threads finished for today. Shutting down.")

                # Save end-of-day report with actual + dummy PnL
                try:
                    now = datetime.now()
                    eq_trader = _trader_refs.get('equity')
                    comm_trader = _trader_refs.get('commodity')

                    eq_summary = eq_trader.get_eod_summary() if eq_trader else {}
                    comm_summary = comm_trader.get_eod_summary() if comm_trader else {}

                    # Calculate win rates
                    eq_wins = sum(1 for t in (eq_trader.portfolio.closed_trades if eq_trader else []) if t.get('pnl', 0) > 0)
                    eq_closed = eq_summary.get('closed_trades', 0)
                    eq_win_rate = (eq_wins / eq_closed * 100) if eq_closed > 0 else 0

                    comm_wins = sum(1 for t in (comm_trader.portfolio.closed_trades if comm_trader else []) if t.get('pnl', 0) > 0)
                    comm_closed = comm_summary.get('closed_trades', 0)
                    comm_win_rate = (comm_wins / comm_closed * 100) if comm_closed > 0 else 0

                    # Send Telegram EOD summary
                    from trade_notifier import notify_daily_summary
                    notify_daily_summary(
                        equity_capital=300000,
                        equity_pnl=eq_summary.get('actual_pnl', 0),
                        equity_positions=eq_summary.get('open_positions', 0),
                        equity_closed=eq_closed,
                        commodity_capital=300000,
                        commodity_pnl=comm_summary.get('actual_pnl', 0),
                        commodity_positions=comm_summary.get('open_positions', 0),
                        commodity_closed=comm_closed,
                        equity_win_rate=eq_win_rate,
                        commodity_win_rate=comm_win_rate,
                        equity_signals=eq_summary.get('total_signals', 0),
                        equity_dummy_pnl=eq_summary.get('dummy_pnl', 0),
                        commodity_signals=comm_summary.get('total_signals', 0),
                        commodity_dummy_pnl=comm_summary.get('dummy_pnl', 0),
                        equity_capital_used=eq_summary.get('capital_used', 0),
                        commodity_capital_used=comm_summary.get('capital_used', 0),
                    )

                    # Also save JSON report
                    report = {
                        'date': now.strftime('%Y-%m-%d'),
                        'timestamp': now.isoformat(),
                        'equity': eq_summary,
                        'commodity': comm_summary,
                    }
                    report_file = os.path.join(LOG_DIR, f'eod_report_{today_str}.json')
                    with open(report_file, 'w') as f:
                        json.dump(report, f, indent=2)
                    logger.info(f"EOD report saved: {report_file}")
                except Exception as e:
                    logger.error(f"EOD report error: {e}")
                    import traceback
                    traceback.print_exc()

                stop_event.set()

    # Wait for threads to finish
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            logger.warning(f"  Thread {t.name} did not exit cleanly")
        else:
            logger.info(f"  Thread {t.name} exited")

    # Shutdown WebSocket feed and MarketDataPipeline
    if ws_feed:
        ws_feed.stop()
    if market_pipeline:
        market_pipeline.stop()
        logger.info("[MAIN] MarketDataPipeline stopped")

    # Release PID lock and set restart detection flag
    release_lock()
    # v7.2: Create flag so next startup knows it's a restart
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        with open(os.path.join(LOCK_DIR, 'last_shutdown.flag'), 'w') as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass

    show_combined_status()
    logger.info("Paper trading system shut down cleanly.")


def main():
    print_banner()

    parser = argparse.ArgumentParser(description='Unified Paper Trading System (Threaded)')
    parser.add_argument('--status', action='store_true', help='Show both portfolio status')
    parser.add_argument('--once', action='store_true', help='Single scan both markets')
    parser.add_argument('--interval', type=float, default=None,
                        help='Legacy: set ALL intervals (minutes). Overridden by per-market args.')
    parser.add_argument('--equity-interval', type=float, default=0.25,
                        help='Equity scan interval in minutes (default: 0.25 = 15s with WebSocket)')
    parser.add_argument('--commodity-interval', type=float, default=0.167,
                        help='Commodity scan interval in minutes (default: 0.167 = 10s with WebSocket)')
    parser.add_argument('--crypto-interval', type=float, default=5,
                        help='Crypto scan interval in minutes (default: 5)')
    parser.add_argument('--offline', action='store_true', help='Run offline (no API)')
    parser.add_argument('--reset', action='store_true', help='Reset both portfolios')
    parser.add_argument('--equity-only', action='store_true', help='Only run equity')
    parser.add_argument('--commodity-only', action='store_true', help='Only run commodity')
    parser.add_argument('--instance', type=str, default=None,
                        help='Instance ID (auto-detected: local/vultr via TRADING_INSTANCE env)')
    parser.add_argument('--force', action='store_true',
                        help='Override existing PID lock (use if previous instance crashed)')
    args = parser.parse_args()

    # Auto-detect instance ID: CLI arg > env var > default 'local'
    instance_id = args.instance or os.environ.get('TRADING_INSTANCE', 'local')
    instance_label = get_instance_label(instance_id)

    if args.reset:
        reset_all()
        return

    if args.status:
        show_combined_status()
        return

    if args.once:
        now = datetime.now()
        current_time = now.time()
        is_weekend = now.weekday() > 4

        if not args.commodity_only:
            equity_open = EQUITY_OPEN <= current_time <= EQUITY_CLOSE and not is_weekend
            if equity_open:
                run_equity_scan(args.offline)
            else:
                logger.warning(f"EQUITY market CLOSED ({current_time.strftime('%H:%M')}). "
                             f"Hours: {EQUITY_OPEN}-{EQUITY_CLOSE}, Mon-Fri. Skipping equity scan.")

        if not args.equity_only:
            mcx_open = MCX_OPEN <= current_time <= MCX_CLOSE and not is_weekend
            commodity_trade_ok = current_time >= COMMODITY_TRADE_START
            if mcx_open and commodity_trade_ok:
                run_commodity_scan(args.offline)
            elif mcx_open and not commodity_trade_ok:
                logger.warning(f"MCX OPEN but commodity trades blocked until {COMMODITY_TRADE_START}. "
                             f"Current: {current_time.strftime('%H:%M')}")
            else:
                logger.warning(f"MCX market CLOSED ({current_time.strftime('%H:%M')}). "
                             f"Hours: {MCX_OPEN}-{MCX_CLOSE}, Mon-Fri. Skipping commodity scan.")

        show_combined_status()
        return

    # Continuous mode — threaded
    # If legacy --interval is provided, use it for all
    if args.interval is not None:
        eq_sec = int(args.interval * 60)
        comm_sec = int(args.interval * 60)
        crypto_sec = int(args.interval * 60)
    else:
        eq_sec = int(args.equity_interval * 60)
        comm_sec = int(args.commodity_interval * 60)
        crypto_sec = int(args.crypto_interval * 60)

    run_continuous(eq_sec, comm_sec, crypto_sec, args.offline,
                   instance_id=instance_id, force=args.force)


if __name__ == '__main__':
    main()
