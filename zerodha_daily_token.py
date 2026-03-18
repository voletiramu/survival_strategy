#!/usr/bin/env python3
"""
Zerodha Daily Auto Token Generator + Bot Restarter.
Runs via cron at 8:45 AM IST every weekday.

Flow:
1. Skip weekends & NSE holidays
2. Generate fresh Zerodha access token using headless Chrome + TOTP
3. Restart all trading bots so they pick up the new token
4. Log everything to /root/algo_trading/logs/zerodha_token.log
"""
import os
import sys
import json
import subprocess
import logging
from datetime import datetime, date
from pathlib import Path

# Setup logging
LOG_DIR = Path('/root/algo_trading/logs')
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'zerodha_token.log'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# NSE holidays 2026 (add more as needed)
NSE_HOLIDAYS = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Maha Shivaratri
    date(2026, 3, 30),   # Id-Ul-Fitr (Eid)
    date(2026, 3, 31),   # Holi
    date(2026, 4, 2),    # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # May Day
    date(2026, 5, 25),   # Buddha Purnima
    date(2026, 6, 5),    # Eid-Ul-Adha
    date(2026, 7, 6),    # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 18),   # Janmashtami
    date(2026, 9, 4),    # Milad-un-Nabi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 9),   # Diwali (Laxmi Puja)
    date(2026, 11, 10),  # Diwali (Balipratipada)
    date(2026, 11, 30),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def is_trading_day():
    """Check if today is a trading day (weekday + not NSE holiday)."""
    today = date.today()
    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if today in NSE_HOLIDAYS:
        return False
    return True


def generate_token():
    """Run zerodha_token_gen.py and return success status."""
    script = '/root/algo_trading/zerodha_token_gen.py'
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=120,
            cwd='/root/algo_trading',
            input='y\n',  # Auto-confirm regeneration
        )
        logger.info(f"Token gen stdout: {result.stdout[-500:]}")
        if result.returncode != 0:
            logger.error(f"Token gen failed (rc={result.returncode}): {result.stderr[-300:]}")
            return False

        # Verify token file exists with today's date
        token_file = Path('/root/algo_trading/config/zerodha_token.json')
        if token_file.exists():
            data = json.loads(token_file.read_text())
            if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                logger.info(f"Token verified: generated at {data.get('generated_at')}")
                return True
            else:
                logger.error(f"Token date mismatch: {data.get('date')}")
                return False
        else:
            logger.error("Token file not found after generation")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Token generation timed out (120s)")
        return False
    except Exception as e:
        logger.error(f"Token generation error: {e}")
        return False


def restart_bots():
    """Restart all trading bot services."""
    services = ['algo-trading', 'algo-stock-trading']
    for svc in services:
        try:
            result = subprocess.run(
                ['systemctl', 'restart', svc],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info(f"Restarted {svc}")
            else:
                logger.error(f"Failed to restart {svc}: {result.stderr}")
        except Exception as e:
            logger.error(f"Error restarting {svc}: {e}")


def main():
    logger.info("=" * 60)
    logger.info("ZERODHA DAILY TOKEN GENERATOR")
    logger.info("=" * 60)

    if not is_trading_day():
        logger.info(f"Not a trading day ({date.today()}). Skipping.")
        return

    logger.info(f"Trading day confirmed: {date.today()}")

    # Attempt token generation (retry up to 3 times)
    success = False
    for attempt in range(1, 4):
        logger.info(f"Token generation attempt {attempt}/3...")
        if generate_token():
            success = True
            break
        else:
            logger.warning(f"Attempt {attempt} failed. {'Retrying...' if attempt < 3 else 'Giving up.'}")
            if attempt < 3:
                import time
                time.sleep(10)

    if success:
        logger.info("Token generated successfully. Restarting bots...")
        restart_bots()
        logger.info("All bots restarted with fresh Zerodha token.")
    else:
        logger.error("TOKEN GENERATION FAILED after 3 attempts!")
        logger.error("Bots will fall back to TrueData as Source 1.")


if __name__ == '__main__':
    main()
