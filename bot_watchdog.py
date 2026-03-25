"""
Bot Watchdog — External Monitoring Service
===========================================
Runs as a separate systemd service (algo-watchdog.service) to catch
issues that bots cannot detect about themselves: zombie state, stale LTP,
resource exhaustion, failed cron jobs, and approaching loss limits.

Monitoring Threads:
    1. Heartbeat Monitor    — every 10s: detect zombie bots → restart
    2. LTP Staleness        — every 15s: detect stale prices → alert/reconnect
    3. Resource Monitor     — every 5m: disk, memory, CPU → alert/cleanup
    4. Service Verifier     — daily 9:05 AM: ensure bots are running
    5. Log Monitor          — every 5s: watch for FATAL/OOM/CIRCUIT_BREAKER
    6. Loss Escalation      — every 60s: warn at 5%, 7%, 9% daily loss

Usage:
    python bot_watchdog.py              # Run watchdog
    systemctl start algo-watchdog       # As systemd service

Deployment:
    1. Copy to /root/algo_trading/bot_watchdog.py
    2. Create /etc/systemd/system/algo-watchdog.service
    3. systemctl daemon-reload && systemctl enable algo-watchdog && systemctl start algo-watchdog
"""

import os
import sys
import json
import time
import shutil
import logging
import threading
import subprocess
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

# ── Setup ─────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_DIR = os.path.join(BASE_DIR, 'heartbeats')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(HEARTBEAT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

today_str = datetime.now().strftime('%Y%m%d')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [Watchdog] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'watchdog_{today_str}.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('BotWatchdog')

# ── Telegram ─────────────────────────────────────────────────────
import requests as req_lib
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', "8426384062:AAGJKO8y7ijbSMdbaFnxkfOeLFlnQZWc8oI")
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '1722559857') or '1722559857')
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Thread-to-service mapping ────────────────────────────────────
THREAD_SERVICE_MAP = {
    'equity':    'algo-trading',
    'commodity': 'algo-trading',
    'stock':     'algo-stock-trading',
}

# ── Market hours (IST) ──────────────────────────────────────────
EQUITY_OPEN = dtime(9, 15)
EQUITY_CLOSE = dtime(15, 30)
MCX_CLOSE = dtime(23, 30)

# ── Config ───────────────────────────────────────────────────────
HEARTBEAT_STALE_SECONDS = 30       # Bot zombie if heartbeat > 30s old
HEARTBEAT_CHECK_INTERVAL = 10      # Check every 10s
HEARTBEAT_GRACE_PERIOD = 90        # 90s grace after restart before checking

LTP_WARN_AGE = 30                  # Warn if LTP > 30s stale
LTP_CRITICAL_AGE = 60              # Signal reconnect if > 60s stale
LTP_CHECK_INTERVAL = 15

RESOURCE_CHECK_INTERVAL = 300      # 5 minutes
DISK_WARN_PCT = 90
DISK_CLEAN_PCT = 95
MEMORY_WARN_MB = 1000              # 1 GB
MEMORY_RESTART_MB = 1200           # 1.2 GB

LOG_CHECK_INTERVAL = 5
LOSS_CHECK_INTERVAL = 60

# Anti-flap: max restarts per service in a window
MAX_RESTARTS = 3
RESTART_WINDOW = 900               # 15 minutes

# Capital for loss % calculations
EQUITY_CAPITAL = 300000
COMMODITY_CAPITAL = 300000
STOCK_CAPITAL = 200000

# Loss escalation thresholds (% of capital)
LOSS_WARN_THRESHOLDS = [5, 7, 9]

# Portfolio state files
PORTFOLIO_FILES = {
    'equity': os.path.join(BASE_DIR, 'paper_trades', 'portfolio_state.json'),
    'commodity': os.path.join(BASE_DIR, 'paper_trades_commodity', 'commodity_portfolio_state.json'),
    'stock': os.path.join(BASE_DIR, 'stock_paper_trades', 'stock_portfolio_state.json'),
}

CAPITAL_MAP = {
    'equity': EQUITY_CAPITAL,
    'commodity': COMMODITY_CAPITAL,
    'stock': STOCK_CAPITAL,
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _send_telegram(msg):
    """Send message to Telegram (no cooldown — watchdog controls its own throttle)."""
    try:
        url = f"{TELEGRAM_API}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
        req_lib.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


def _is_market_hours():
    """Check if we're in any market's trading hours (equity or MCX)."""
    now = datetime.now()
    t = now.time()
    weekday = now.weekday() <= 4
    return weekday and (EQUITY_OPEN <= t <= MCX_CLOSE)


def _is_equity_hours():
    now = datetime.now()
    return now.weekday() <= 4 and EQUITY_OPEN <= now.time() <= EQUITY_CLOSE


def _restart_service(service_name, reason):
    """Restart a systemd service with anti-flap protection."""
    try:
        result = subprocess.run(
            ['systemctl', 'restart', service_name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info(f"Service {service_name} restarted: {reason}")
            _send_telegram(
                f"<b>🔄 WATCHDOG RESTART</b>\n"
                f"<b>Service:</b> {service_name}\n"
                f"<b>Reason:</b> {reason}\n"
                f"<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            logger.error(f"Restart failed for {service_name}: {result.stderr}")
            _send_telegram(
                f"<b>🔴 RESTART FAILED</b>\n"
                f"<b>Service:</b> {service_name}\n"
                f"<b>Error:</b> {result.stderr[:200]}"
            )
    except Exception as e:
        logger.error(f"Restart exception for {service_name}: {e}")


def _read_heartbeat(thread_name):
    """Read a heartbeat file. Returns dict or None."""
    filepath = os.path.join(HEARTBEAT_DIR, f'{thread_name}.json')
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _get_process_memory_mb(pid):
    """Get RSS memory of a process in MB (Linux only)."""
    try:
        with open(f'/proc/{pid}/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024  # KB → MB
    except Exception:
        pass
    return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 1: HEARTBEAT (Zombie Detection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def heartbeat_monitor(stop_event):
    """Monitor heartbeat files to detect zombie bots."""
    logger.info("Heartbeat monitor started")
    restart_history = {}  # {service: [timestamp, ...]}
    service_start_times = {}  # {service: timestamp} — grace period tracking

    while not stop_event.is_set():
        if not _is_market_hours():
            stop_event.wait(60)
            continue

        for thread_name, service in THREAD_SERVICE_MAP.items():
            # Skip stock during non-equity hours (stock only runs 9:15-15:30)
            if thread_name == 'stock' and not _is_equity_hours():
                continue

            hb = _read_heartbeat(thread_name)

            if hb is None:
                # No heartbeat file — check if service is even running
                try:
                    result = subprocess.run(
                        ['systemctl', 'is-active', service],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.stdout.strip() == 'active':
                        # Service running but no heartbeat — check grace period
                        start_time = service_start_times.get(service, time.time())
                        if time.time() - start_time > HEARTBEAT_GRACE_PERIOD:
                            logger.warning(f"{thread_name}: No heartbeat file but service is active — zombie?")
                except Exception:
                    pass
                continue

            # Check staleness
            age = time.time() - hb.get('epoch', 0)

            if age > HEARTBEAT_STALE_SECONDS:
                # Grace period check
                if service in service_start_times:
                    if time.time() - service_start_times[service] < HEARTBEAT_GRACE_PERIOD:
                        continue

                # Anti-flap: check restart history
                now = time.time()
                history = restart_history.get(service, [])
                recent = [t for t in history if now - t < RESTART_WINDOW]
                restart_history[service] = recent

                if len(recent) >= MAX_RESTARTS:
                    logger.error(
                        f"{thread_name}: Heartbeat stale ({age:.0f}s) but {MAX_RESTARTS} "
                        f"restarts in {RESTART_WINDOW}s — ANTI-FLAP, manual intervention needed"
                    )
                    _send_telegram(
                        f"<b>🔴 ANTI-FLAP LIMIT</b>\n"
                        f"<b>Thread:</b> {thread_name}\n"
                        f"<b>Service:</b> {service}\n"
                        f"<b>Heartbeat age:</b> {age:.0f}s\n"
                        f"<b>Restarts in {RESTART_WINDOW//60}min:</b> {len(recent)}/{MAX_RESTARTS}\n"
                        f"Manual restart needed!"
                    )
                    continue

                logger.warning(f"{thread_name}: Heartbeat stale ({age:.0f}s) — restarting {service}")
                _restart_service(service, f"{thread_name} heartbeat stale ({age:.0f}s)")
                restart_history.setdefault(service, []).append(now)
                service_start_times[service] = now

        stop_event.wait(HEARTBEAT_CHECK_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 2: LTP STALENESS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ltp_alert_sent = {}  # {thread: timestamp}

def ltp_staleness_monitor(stop_event):
    """Monitor LTP age from heartbeat files."""
    logger.info("LTP staleness monitor started")

    while not stop_event.is_set():
        if not _is_market_hours():
            stop_event.wait(60)
            continue

        for thread_name in THREAD_SERVICE_MAP:
            if thread_name == 'stock' and not _is_equity_hours():
                continue

            hb = _read_heartbeat(thread_name)
            if not hb:
                continue

            ltp_age = hb.get('ltp_age_seconds', 0)
            ws_connected = hb.get('ws_connected', True)

            if not ws_connected:
                continue  # WS not connected, REST fallback is expected

            if ltp_age > LTP_CRITICAL_AGE:
                # Only alert once per 5 minutes per thread
                last_alert = _ltp_alert_sent.get(thread_name, 0)
                if time.time() - last_alert > 300:
                    logger.warning(f"{thread_name}: LTP critically stale ({ltp_age:.0f}s)")
                    _send_telegram(
                        f"<b>🟠 LTP STALE — CRITICAL</b>\n"
                        f"<b>Thread:</b> {thread_name}\n"
                        f"<b>LTP age:</b> {ltp_age:.0f}s (limit: {LTP_CRITICAL_AGE}s)\n"
                        f"<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}\n"
                        f"WebSocket may need reconnection."
                    )
                    _ltp_alert_sent[thread_name] = time.time()

            elif ltp_age > LTP_WARN_AGE:
                last_alert = _ltp_alert_sent.get(thread_name, 0)
                if time.time() - last_alert > 300:
                    logger.warning(f"{thread_name}: LTP stale ({ltp_age:.0f}s)")
                    _ltp_alert_sent[thread_name] = time.time()

        stop_event.wait(LTP_CHECK_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 3: RESOURCE MONITOR (Disk, Memory)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_resource_alert_sent = {}

def resource_monitor(stop_event):
    """Monitor disk space and memory usage."""
    logger.info("Resource monitor started")

    while not stop_event.is_set():
        # ── Disk check ──
        try:
            usage = shutil.disk_usage('/')
            pct = (usage.used / usage.total) * 100
            free_gb = usage.free / (1024**3)

            if pct >= DISK_CLEAN_PCT:
                logger.warning(f"Disk {pct:.1f}% full — auto-cleaning old logs")
                _cleanup_old_logs(days=7)
                _send_telegram(
                    f"<b>🔴 DISK CRITICAL ({pct:.1f}%)</b>\n"
                    f"Free: {free_gb:.1f} GB\n"
                    f"Auto-cleaning logs older than 7 days."
                )
            elif pct >= DISK_WARN_PCT:
                if 'disk' not in _resource_alert_sent or time.time() - _resource_alert_sent['disk'] > 3600:
                    _send_telegram(
                        f"<b>🟡 DISK WARNING ({pct:.1f}%)</b>\n"
                        f"Free: {free_gb:.1f} GB\n"
                        f"Consider cleaning up logs."
                    )
                    _resource_alert_sent['disk'] = time.time()
        except Exception as e:
            logger.error(f"Disk check error: {e}")

        # ── Memory check (per bot PID from heartbeats) ──
        for thread_name in THREAD_SERVICE_MAP:
            hb = _read_heartbeat(thread_name)
            if not hb:
                continue

            pid = hb.get('pid', 0)
            if pid <= 0:
                continue

            mem_mb = _get_process_memory_mb(pid)
            if mem_mb <= 0:
                continue

            if mem_mb >= MEMORY_RESTART_MB:
                service = THREAD_SERVICE_MAP[thread_name]
                logger.error(f"{thread_name} (PID {pid}): Memory {mem_mb:.0f}MB > {MEMORY_RESTART_MB}MB — restarting")
                _restart_service(service, f"Memory leak: {mem_mb:.0f}MB")
            elif mem_mb >= MEMORY_WARN_MB:
                alert_key = f"mem_{thread_name}"
                if alert_key not in _resource_alert_sent or time.time() - _resource_alert_sent[alert_key] > 3600:
                    _send_telegram(
                        f"<b>🟡 MEMORY WARNING</b>\n"
                        f"<b>Thread:</b> {thread_name} (PID {pid})\n"
                        f"<b>RSS:</b> {mem_mb:.0f} MB (limit: {MEMORY_RESTART_MB}MB)"
                    )
                    _resource_alert_sent[alert_key] = time.time()

        stop_event.wait(RESOURCE_CHECK_INTERVAL)


def _cleanup_old_logs(days=7):
    """Delete log files older than N days."""
    cutoff = time.time() - (days * 86400)
    count = 0
    for f in Path(LOG_DIR).glob('*.log'):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        except Exception:
            pass
    if count > 0:
        logger.info(f"Cleaned up {count} log files older than {days} days")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 4: SERVICE VERIFIER (Daily morning check)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def service_verifier(stop_event):
    """Daily check at 9:05 AM that all services are running."""
    logger.info("Service verifier started")
    last_check_date = None

    while not stop_event.is_set():
        now = datetime.now()

        # Run once per day at 9:05 AM on weekdays
        if (now.weekday() <= 4
            and now.time() >= dtime(9, 5)
            and now.time() <= dtime(9, 10)
            and last_check_date != now.date()):

            last_check_date = now.date()
            logger.info("Running daily service verification")

            services_status = {}
            for service in set(THREAD_SERVICE_MAP.values()):
                try:
                    result = subprocess.run(
                        ['systemctl', 'is-active', service],
                        capture_output=True, text=True, timeout=5
                    )
                    status = result.stdout.strip()
                    services_status[service] = status
                    if status != 'active':
                        logger.warning(f"Service {service} is {status} — starting...")
                        _restart_service(service, f"Morning check: was {status}")
                except Exception as e:
                    services_status[service] = f'error: {e}'

            # Send daily morning status
            lines = [f"<b>🌅 DAILY BOT STATUS ({now.strftime('%Y-%m-%d')})</b>"]
            for svc, status in services_status.items():
                emoji = '✅' if status == 'active' else '❌'
                lines.append(f"  {emoji} {svc}: {status}")
            _send_telegram('\n'.join(lines))

        stop_event.wait(30)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 5: LOG MONITOR (Pattern detection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Critical patterns to watch for in logs
LOG_CRITICAL_PATTERNS = [
    ('FATAL', '🔴 FATAL ERROR detected in log'),
    ('MemoryError', '🔴 MemoryError detected'),
    ('CIRCUIT_BREAKER', '🟠 CIRCUIT BREAKER triggered'),
    ('AUTO-RECOVERY', '🟠 Auto-recovery triggered (10-strike hit)'),
    ('OOM', '🔴 Out of Memory detected'),
    ('Segmentation fault', '🔴 Segfault detected'),
    ('killed', '🔴 Process killed (likely OOM killer)'),
]

def log_monitor(stop_event):
    """Monitor log files for critical error patterns."""
    logger.info("Log monitor started")

    # Track file positions
    file_positions = {}
    _log_alert_sent = {}

    while not stop_event.is_set():
        today = datetime.now().strftime('%Y%m%d')
        log_files = [
            os.path.join(LOG_DIR, f'unified_paper_{today}.log'),
            os.path.join(LOG_DIR, f'stock_paper_{today}.log'),
            os.path.join(LOG_DIR, f'commodity_paper_{today}.log'),
        ]

        for log_file in log_files:
            if not os.path.exists(log_file):
                continue

            # Get current position
            pos = file_positions.get(log_file, 0)

            try:
                file_size = os.path.getsize(log_file)

                # File was rotated/truncated
                if file_size < pos:
                    pos = 0

                if file_size <= pos:
                    continue

                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(pos)
                    new_lines = f.read(65536)  # Read up to 64KB at a time
                    file_positions[log_file] = f.tell()

                # Check each line for critical patterns
                for line in new_lines.split('\n'):
                    for pattern, alert_msg in LOG_CRITICAL_PATTERNS:
                        if pattern in line:
                            alert_key = f"log_{pattern}_{os.path.basename(log_file)}"
                            last = _log_alert_sent.get(alert_key, 0)
                            if time.time() - last > 300:  # 5 min cooldown per pattern per file
                                _send_telegram(
                                    f"<b>{alert_msg}</b>\n"
                                    f"<b>Log:</b> {os.path.basename(log_file)}\n"
                                    f"<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}\n"
                                    f"<code>{line.strip()[:300]}</code>"
                                )
                                _log_alert_sent[alert_key] = time.time()
                            break  # One alert per line

            except Exception as e:
                logger.error(f"Log monitor error for {log_file}: {e}")

        stop_event.wait(LOG_CHECK_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MONITOR 6: DAILY LOSS ESCALATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_loss_alerts_sent = {}  # {market_threshold: True}

def loss_escalation_monitor(stop_event):
    """Monitor portfolio state for approaching daily loss limits."""
    logger.info("Loss escalation monitor started")

    while not stop_event.is_set():
        if not _is_market_hours():
            # Reset alerts at start of new trading day
            _loss_alerts_sent.clear()
            stop_event.wait(60)
            continue

        today = datetime.now().strftime('%Y-%m-%d')

        for market, filepath in PORTFOLIO_FILES.items():
            # Skip stock outside equity hours
            if market == 'stock' and not _is_equity_hours():
                continue

            try:
                if not os.path.exists(filepath):
                    continue

                with open(filepath, 'r') as f:
                    portfolio = json.load(f)

                daily_pnl = portfolio.get('daily_pnl', {}).get(today, 0)
                capital = CAPITAL_MAP.get(market, 300000)

                if daily_pnl >= 0:
                    continue

                loss_pct = abs(daily_pnl) / capital * 100

                for threshold in LOSS_WARN_THRESHOLDS:
                    alert_key = f"{market}_{threshold}"
                    if loss_pct >= threshold and alert_key not in _loss_alerts_sent:
                        _loss_alerts_sent[alert_key] = True
                        emoji = '🟡' if threshold < 7 else ('🟠' if threshold < 9 else '🔴')
                        _send_telegram(
                            f"<b>{emoji} LOSS WARNING — {market.upper()}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"<b>Daily Loss:</b> Rs {daily_pnl:,.0f} ({loss_pct:.1f}%)\n"
                            f"<b>Threshold:</b> {threshold}% of Rs {capital:,.0f}\n"
                            f"<b>Circuit Breaker:</b> 10% (Rs {capital * 0.1:,.0f})\n"
                            f"<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}\n"
                            f"{'⚠️ Approaching circuit breaker!' if threshold >= 9 else ''}"
                        )
                        logger.warning(f"{market}: Daily loss {loss_pct:.1f}% hit {threshold}% threshold")

            except Exception as e:
                logger.debug(f"Loss check error for {market}: {e}")

        stop_event.wait(LOSS_CHECK_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN — START ALL MONITORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    """Start all watchdog monitoring threads."""
    logger.info("=" * 60)
    logger.info("BOT WATCHDOG STARTING")
    logger.info(f"  Heartbeat dir: {HEARTBEAT_DIR}")
    logger.info(f"  Log dir: {LOG_DIR}")
    logger.info(f"  Anti-flap: max {MAX_RESTARTS} restarts per {RESTART_WINDOW}s")
    logger.info("=" * 60)

    # Send startup notification
    _send_telegram(
        f"<b>🛡️ WATCHDOG STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"<b>Monitors:</b>\n"
        f"  1. Heartbeat (zombie detect) — every {HEARTBEAT_CHECK_INTERVAL}s\n"
        f"  2. LTP staleness — every {LTP_CHECK_INTERVAL}s\n"
        f"  3. Resource (disk/memory) — every {RESOURCE_CHECK_INTERVAL//60}min\n"
        f"  4. Service verify — daily 9:05 AM\n"
        f"  5. Log patterns — every {LOG_CHECK_INTERVAL}s\n"
        f"  6. Loss escalation — every {LOSS_CHECK_INTERVAL}s\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    stop_event = threading.Event()

    monitors = [
        ('Heartbeat', heartbeat_monitor),
        ('LTP', ltp_staleness_monitor),
        ('Resource', resource_monitor),
        ('ServiceVerifier', service_verifier),
        ('LogMonitor', log_monitor),
        ('LossEscalation', loss_escalation_monitor),
    ]

    threads = []
    for name, func in monitors:
        t = threading.Thread(target=func, args=(stop_event,), name=name, daemon=True)
        t.start()
        threads.append(t)
        logger.info(f"  Started monitor: {name}")

    # Write watchdog's own heartbeat
    try:
        # systemd watchdog notify (if running under Type=notify)
        try:
            import sdnotify
            notifier = sdnotify.SystemdNotifier()
            notifier.notify("READY=1")
        except ImportError:
            pass  # sdnotify not installed, that's fine

        while True:
            # Write watchdog heartbeat
            wd_hb = {
                'thread': 'watchdog',
                'timestamp': datetime.now().isoformat(),
                'epoch': time.time(),
                'pid': os.getpid(),
                'monitors_alive': sum(1 for t in threads if t.is_alive()),
                'monitors_total': len(threads),
            }
            hb_path = os.path.join(HEARTBEAT_DIR, 'watchdog.json')
            try:
                tmp = hb_path + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(wd_hb, f)
                os.replace(tmp, hb_path)
            except Exception:
                pass

            # Notify systemd watchdog
            try:
                notifier.notify("WATCHDOG=1")
            except Exception:
                pass

            # Check if any monitor died and restart it
            for i, (name, func) in enumerate(monitors):
                if not threads[i].is_alive():
                    logger.warning(f"Monitor {name} died — restarting")
                    t = threading.Thread(target=func, args=(stop_event,), name=name, daemon=True)
                    t.start()
                    threads[i] = t

            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("Watchdog shutting down...")
        stop_event.set()
        _send_telegram("<b>🛡️ WATCHDOG STOPPED</b>\nManual shutdown.")
        for t in threads:
            t.join(timeout=5)


if __name__ == '__main__':
    main()
