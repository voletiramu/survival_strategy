"""
Error Reactor — Real-Time Error Classification & Immediate Action
=================================================================
Classifies errors by severity, tracks consecutive failures per thread,
writes heartbeat files for external watchdog, and gates trades on LTP freshness.

Severity Levels:
    FATAL    — Process must restart immediately (1-strike)
    CRITICAL — Corrective action after 3 consecutive failures
    WARNING  — Logged + alert, restart after 3 consecutive
    INFO     — Expected condition, skip and continue

Integration:
    from error_reactor import classify_error, ErrorTracker, HeartbeatWriter

    tracker = ErrorTracker()
    heartbeat = HeartbeatWriter()

    # In scan loop:
    try:
        trader.run_once()
        tracker.reset('equity')
        heartbeat.write('equity', scan_count, ws_feed)
    except Exception as e:
        severity, code, _ = classify_error(e)
        action = tracker.record('equity', severity, code)
        send_watchdog_alert(severity, code, str(e), action)
        if action == 'restart_service':
            subprocess.Popen(['systemctl', 'restart', 'algo-trading'])
            break
"""

import os
import json
import time
import logging
import tempfile
import traceback
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Severity constants ────────────────────────────────────────────
FATAL = 'FATAL'
CRITICAL = 'CRITICAL'
WARNING = 'WARNING'
INFO = 'INFO'

# ── Thresholds ────────────────────────────────────────────────────
FATAL_THRESHOLD = 1       # Restart immediately
CRITICAL_THRESHOLD = 3    # 3 consecutive → restart
WARNING_THRESHOLD = 3     # 3 consecutive → restart (was 10)

# ── Heartbeat directory ──────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_DIR = os.path.join(BASE_DIR, 'heartbeats')
os.makedirs(HEARTBEAT_DIR, exist_ok=True)

# ── Telegram (reuse trade_notifier config) ───────────────────────
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', "8426384062:AAGJKO8y7ijbSMdbaFnxkfOeLFlnQZWc8oI")
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '1722559857') or '1722559857')
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Alert cooldowns (only for WARNING, FATAL/CRITICAL bypass) ────
_alert_cooldowns = {}
WARNING_COOLDOWN = 300  # 5 min for warnings


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ERROR CLASSIFICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_error(exc):
    """Classify an exception into (severity, error_code, recommended_action).

    Args:
        exc: The caught Exception object

    Returns:
        tuple: (severity, error_code, recommended_action)
    """
    exc_type = type(exc).__name__
    exc_msg = str(exc).lower()

    # ── FATAL: Must restart immediately ──
    if isinstance(exc, MemoryError):
        return FATAL, 'OOM', 'restart_service'
    if isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return FATAL, 'SHUTDOWN', 'shutdown'
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return FATAL, 'IMPORT_FAIL', 'restart_service'

    # ── INFO: Expected / harmless ──
    if 'ab9019' in exc_msg or 'no data' in exc_msg:
        return INFO, 'NO_DATA', 'continue'
    if 'market closed' in exc_msg or 'holiday' in exc_msg:
        return INFO, 'MARKET_CLOSED', 'continue'

    # ── WARNING: Rate limit (handled by existing backoff) ──
    if 'ab1004' in exc_msg or 'rate limit' in exc_msg:
        return WARNING, 'RATE_LIMIT', 'backoff'

    # ── CRITICAL: Auth failures → reconnect first, not restart ──
    if any(code in exc_msg for code in ['ag8001', 'ag8002', 'ab8050', 'ab4006']):
        return CRITICAL, 'AUTH_DEAD', 'reconnect'
    if 'token expired' in exc_msg or 'invalid token' in exc_msg:
        return CRITICAL, 'AUTH_DEAD', 'reconnect'

    # ── CRITICAL: Connection/network failures → reconnect first ──
    if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionAbortedError)):
        return CRITICAL, 'NETWORK_DOWN', 'reconnect'
    if 'timeout' in exc_msg or 'timed out' in exc_msg:
        return WARNING, 'TIMEOUT', 'reconnect'

    # ── WARNING: Everything else ──
    return WARNING, 'SCAN_ERROR', 'continue'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ERROR TRACKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ErrorTracker:
    """Tracks consecutive errors per thread and decides action.

    Flow: error → reconnect in-place → if reconnect fails repeatedly → restart service
    Trading NEVER stops unless absolutely necessary.

    FATAL = 1-strike restart (OOM, import fail — can't recover in-place)
    CRITICAL = reconnect immediately, restart only after 3 failed reconnects
    WARNING = reconnect on 1st hit, restart only after 3 failed reconnects
    INFO = never restart
    """

    def __init__(self):
        self._counts = {}       # {thread_name: consecutive_error_count}
        self._first_error = {}  # {thread_name: timestamp of first error in streak}
        self._reconnect_fails = {}  # {thread_name: consecutive reconnect failure count}

    def record(self, thread_name, severity, error_code):
        """Record an error and return required action.

        Args:
            thread_name: 'equity', 'commodity', 'stock'
            severity: FATAL, CRITICAL, WARNING, INFO
            error_code: Short error identifier

        Returns:
            str: 'continue', 'reconnect', 'restart_service', or 'shutdown'
        """
        if severity == INFO:
            return 'continue'

        if severity == FATAL:
            logger.error(f"[ErrorReactor] {thread_name} FATAL ({error_code}) — immediate restart")
            return 'restart_service'

        # Track consecutive errors
        count = self._counts.get(thread_name, 0) + 1
        self._counts[thread_name] = count

        if thread_name not in self._first_error:
            self._first_error[thread_name] = time.time()

        threshold = CRITICAL_THRESHOLD if severity == CRITICAL else WARNING_THRESHOLD

        if count >= threshold:
            elapsed = time.time() - self._first_error.get(thread_name, time.time())
            # Even at threshold, try reconnect first — only restart if reconnect keeps failing
            recon_fails = self._reconnect_fails.get(thread_name, 0)
            if recon_fails >= 3:
                logger.error(
                    f"[ErrorReactor] {thread_name} {severity} ({error_code}) — "
                    f"{count} errors + {recon_fails} reconnect failures in {elapsed:.0f}s — RESTART SERVICE (last resort)"
                )
                self.reset(thread_name)
                return 'restart_service'
            else:
                logger.warning(
                    f"[ErrorReactor] {thread_name} {severity} ({error_code}) — "
                    f"{count} errors in {elapsed:.0f}s — trying RECONNECT ({recon_fails} prior fails)"
                )
                return 'reconnect'

        logger.warning(
            f"[ErrorReactor] {thread_name} {severity} ({error_code}) — "
            f"error {count}/{threshold} — trying RECONNECT"
        )
        return 'reconnect'

    def reconnect_failed(self, thread_name):
        """Record that a reconnect attempt failed."""
        count = self._reconnect_fails.get(thread_name, 0) + 1
        self._reconnect_fails[thread_name] = count
        logger.warning(f"[ErrorReactor] {thread_name} reconnect failed ({count}/3)")
        if count >= 3:
            return 'restart_service'
        return 'reconnect'

    def reconnect_succeeded(self, thread_name):
        """Record that reconnect worked — reset all counters, keep trading."""
        self._reconnect_fails[thread_name] = 0
        logger.info(f"[ErrorReactor] {thread_name} reconnected successfully — counters reset")
        # Don't full-reset error count yet — that happens on successful run_once()

    def reset(self, thread_name):
        """Reset error counter after a successful scan."""
        if thread_name in self._counts:
            if self._counts[thread_name] > 0:
                logger.info(f"[ErrorReactor] {thread_name} error counter reset (was {self._counts[thread_name]})")
            del self._counts[thread_name]
        self._first_error.pop(thread_name, None)

    def get_count(self, thread_name):
        """Get current consecutive error count for a thread."""
        return self._counts.get(thread_name, 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IN-PLACE RECONNECTION (no service restart needed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def try_reconnect(trader, ws_feed=None, thread_name='unknown'):
    """Attempt to re-establish ALL data connections WITHOUT restarting the service.

    Data source priority (same as paper_trader.py):
        1. Zerodha Kite (primary) — daily token based
        2. NSE/BSE Pipeline (fallback)
        3. Angel API (last resort fallback)
        4. WebSocket (real-time LTP — uses Angel auth tokens)

    Tries each source that's available. Trading continues as long as ANY source works.

    Args:
        trader: PaperTrader/CommodityPaperTrader/StockPaperTrader instance
        ws_feed: WebSocketFeed instance (optional)
        thread_name: For logging

    Returns:
        bool: True if at least one data source reconnected, False if all failed
    """
    any_success = False
    sources_tried = []

    # 1. Try Zerodha reconnect (PRIMARY source)
    zerodha = getattr(trader, 'zerodha', None)
    if zerodha:
        try:
            if not getattr(zerodha, 'is_connected', False):
                logger.info(f"[Reconnect] {thread_name}: Zerodha down, reconnecting...")
                if zerodha.connect():
                    logger.info(f"[Reconnect] {thread_name}: Zerodha reconnected OK")
                    any_success = True
                    sources_tried.append('Zerodha:OK')
                else:
                    logger.warning(f"[Reconnect] {thread_name}: Zerodha reconnect failed")
                    sources_tried.append('Zerodha:FAIL')
            else:
                sources_tried.append('Zerodha:already_connected')
                any_success = True
        except Exception as e:
            logger.error(f"[Reconnect] {thread_name}: Zerodha error: {e}")
            sources_tried.append(f'Zerodha:ERR')

    # 2. Try NSE/BSE Market Pipeline reconnect
    pipeline = getattr(trader, 'market_pipeline', None)
    if pipeline:
        try:
            if hasattr(pipeline, 'is_running') and not pipeline.is_running:
                logger.info(f"[Reconnect] {thread_name}: Market pipeline down, restarting...")
                pipeline.start()
                any_success = True
                sources_tried.append('Pipeline:OK')
            elif pipeline:
                sources_tried.append('Pipeline:already_running')
                any_success = True
        except Exception as e:
            logger.warning(f"[Reconnect] {thread_name}: Pipeline restart failed: {e}")
            sources_tried.append('Pipeline:FAIL')

    # 3. Try Angel API reconnect (fallback source)
    angel = getattr(trader, 'angel', None)
    if angel and hasattr(angel, 'reconnect'):
        try:
            if not getattr(angel, '_connected', False):
                logger.info(f"[Reconnect] {thread_name}: Angel API reconnecting...")
                if angel.reconnect():
                    logger.info(f"[Reconnect] {thread_name}: Angel API reconnected OK")
                    any_success = True
                    sources_tried.append('Angel:OK')
                else:
                    sources_tried.append('Angel:FAIL')
            else:
                sources_tried.append('Angel:already_connected')
                any_success = True
        except Exception as e:
            logger.warning(f"[Reconnect] {thread_name}: Angel reconnect failed: {e}")
            sources_tried.append('Angel:FAIL')
    elif hasattr(trader, 'initialize'):
        # StockPaperTrader uses initialize() for full setup
        try:
            logger.info(f"[Reconnect] {thread_name}: Re-initializing trader...")
            if trader.initialize():
                any_success = True
                sources_tried.append('Trader:OK')
            else:
                sources_tried.append('Trader:FAIL')
        except Exception as e:
            logger.warning(f"[Reconnect] {thread_name}: Re-initialize failed: {e}")
            sources_tried.append('Trader:FAIL')

    # 4. Try WebSocket reconnect if it's down
    if ws_feed and not ws_feed.is_connected:
        try:
            logger.info(f"[Reconnect] {thread_name}: WebSocket down, restarting...")
            ws_feed.stop()
            time.sleep(2)
            if hasattr(ws_feed, '_auth_token') and ws_feed._auth_token:
                ws_feed._started = False  # Allow restart
                ws_feed.start(
                    ws_feed._auth_token,
                    ws_feed._api_key,
                    ws_feed._client_code,
                    ws_feed._feed_token,
                )
                time.sleep(3)  # WS connects async
                if ws_feed.is_connected:
                    logger.info(f"[Reconnect] {thread_name}: WebSocket reconnected OK")
                    any_success = True
                    sources_tried.append('WS:OK')
                else:
                    sources_tried.append('WS:PENDING')
            else:
                sources_tried.append('WS:no_auth_token')
        except Exception as e:
            logger.error(f"[Reconnect] {thread_name}: WebSocket reconnect failed: {e}")
            sources_tried.append('WS:FAIL')

    summary = ', '.join(sources_tried)
    if any_success:
        logger.info(f"[Reconnect] {thread_name}: SUCCESS — {summary}")
        send_watchdog_alert(WARNING, 'RECONNECTED',
                           f"{thread_name} reconnected: {summary}", 'reconnect_ok')
    else:
        logger.error(f"[Reconnect] {thread_name}: ALL SOURCES FAILED — {summary}")
        send_watchdog_alert(CRITICAL, 'RECONNECT_FAILED',
                           f"{thread_name} all sources failed: {summary}", 'reconnect_failed')

    return any_success


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEARTBEAT WRITER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HeartbeatWriter:
    """Writes atomic heartbeat files for external watchdog to monitor.

    After each successful scan, writes JSON to heartbeats/{thread}.json.
    The external watchdog reads these to detect zombie bots (stale timestamps).
    """

    def __init__(self, heartbeat_dir=None):
        self._dir = heartbeat_dir or HEARTBEAT_DIR
        os.makedirs(self._dir, exist_ok=True)

    def write(self, thread_name, scan_count=0, ws_feed=None):
        """Write heartbeat file atomically.

        Args:
            thread_name: 'equity', 'commodity', 'stock'
            scan_count: Current scan iteration number
            ws_feed: WebSocketFeed instance (for LTP age tracking)
        """
        # Calculate max LTP age from ws_feed
        ltp_age = 0.0
        if ws_feed and hasattr(ws_feed, 'get_max_ltp_age'):
            ltp_age = ws_feed.get_max_ltp_age()

        data = {
            'thread': thread_name,
            'timestamp': datetime.now().isoformat(),
            'epoch': time.time(),
            'scan_count': scan_count,
            'pid': os.getpid(),
            'ltp_age_seconds': round(ltp_age, 1),
            'ws_connected': ws_feed.is_connected if ws_feed else False,
        }

        filepath = os.path.join(self._dir, f'{thread_name}.json')
        tmp_path = filepath + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f)
            # Atomic rename (works on Linux; on Windows, may need replace)
            if os.name == 'nt':
                if os.path.exists(filepath):
                    os.replace(tmp_path, filepath)
                else:
                    os.rename(tmp_path, filepath)
            else:
                os.rename(tmp_path, filepath)
        except Exception as e:
            logger.debug(f"[Heartbeat] Write error for {thread_name}: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LTP FRESHNESS GATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_ltp_freshness(ws_feed, max_age_seconds=15):
    """Check if LTP data is fresh enough for trade execution.

    Args:
        ws_feed: WebSocketFeed instance with get_max_ltp_age()
        max_age_seconds: Maximum acceptable LTP age

    Returns:
        tuple: (is_fresh: bool, age_seconds: float)
    """
    if ws_feed is None or not hasattr(ws_feed, 'get_max_ltp_age'):
        return True, 0.0  # No WS feed, allow REST fallback

    if not ws_feed.is_connected:
        return True, 0.0  # WS not connected, REST fallback is fine

    age = ws_feed.get_max_ltp_age()
    if age > max_age_seconds:
        logger.warning(f"[LTP_STALE] Max LTP age: {age:.1f}s > {max_age_seconds}s threshold — skipping trade")
        return False, age

    return True, age


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WATCHDOG TELEGRAM ALERTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_watchdog_alert(severity, error_code, detail, action_taken):
    """Send Telegram alert for watchdog events.

    FATAL/CRITICAL: Always send immediately (no cooldown)
    WARNING: 5-minute cooldown per error_code
    INFO: No alert
    """
    if severity == INFO:
        return

    # WARNING has cooldown
    if severity == WARNING:
        key = f"wd_{error_code}"
        now = time.time()
        last = _alert_cooldowns.get(key, 0)
        if now - last < WARNING_COOLDOWN:
            return
        _alert_cooldowns[key] = now

    emoji = {'FATAL': '🔴', 'CRITICAL': '🟠', 'WARNING': '🟡'}.get(severity, '⚪')
    action_emoji = '🔄' if action_taken == 'restart_service' else '▶️'

    now_str = datetime.now().strftime('%H:%M:%S')
    msg = (
        f"<b>{emoji} WATCHDOG {severity}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Error:</b> {error_code}\n"
        f"<b>Time:</b> {now_str}\n"
        f"<b>Detail:</b> <code>{str(detail)[:400]}</code>\n"
        f"<b>{action_emoji} Action:</b> {action_taken}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        url = f"{TELEGRAM_API}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"[WatchdogAlert] Telegram send failed: {e}")


def send_watchdog_status(message):
    """Send a general watchdog status message to Telegram."""
    try:
        url = f"{TELEGRAM_API}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"[WatchdogStatus] Telegram send failed: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CRASH DUMP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def write_crash_dump(thread_name, exc):
    """Write crash details to a log file for post-mortem analysis."""
    crash_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(crash_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filepath = os.path.join(crash_dir, f'crash_{thread_name}_{ts}.log')
    try:
        with open(filepath, 'w') as f:
            f.write(f"Thread: {thread_name}\n")
            f.write(f"Time: {datetime.now().isoformat()}\n")
            f.write(f"PID: {os.getpid()}\n")
            f.write(f"Error: {type(exc).__name__}: {exc}\n")
            f.write(f"\nTraceback:\n{traceback.format_exc()}\n")
        logger.info(f"[CrashDump] Written to {filepath}")
    except Exception as e:
        logger.error(f"[CrashDump] Failed to write: {e}")
