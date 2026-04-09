"""
Dhan Access Token Auto-Refresh
===============================
Generates a fresh Dhan access token using TOTP every day.
Token validity: 24 hours from generation.

Usage:
    python dhan_token_refresh.py              # Generate token and update config
    python dhan_token_refresh.py --check      # Check current token validity

Cron (VPS):
    45 8 * * 1-5 cd /root/algo_trading && python dhan_token_refresh.py >> logs/dhan_token.log 2>&1
    (Runs at 8:45 AM Mon-Fri, before 9:00 AM MCX open)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [DhanToken] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Config paths
CONFIG_PATHS = [
    Path('D:/AlgoTrading/config/dhan_creds.json'),          # Local Windows
    Path('/root/algo_trading/config/dhan_creds.json'),       # VPS
    Path(os.path.dirname(os.path.abspath(__file__))) / 'config' / 'dhan_creds.json',  # Relative
]

AUTH_URL = 'https://auth.dhan.co/app/generateAccessToken'


def find_config():
    """Find the first existing config file."""
    for p in CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config(config_path):
    """Load credentials from config file."""
    with open(str(config_path), 'r') as f:
        return json.load(f)


def save_config(config_path, config):
    """Save updated credentials to config file."""
    with open(str(config_path), 'w') as f:
        json.dump(config, f, indent=4)
    logger.info(f"Config saved to {config_path}")


def generate_totp(secret):
    """Generate current TOTP code from secret."""
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        logger.error("pyotp not installed. Run: pip install pyotp")
        sys.exit(1)


def generate_access_token(client_id, pin, totp_code):
    """Call Dhan auth endpoint to generate a new access token."""
    import requests

    url = f"{AUTH_URL}?dhanClientId={client_id}&pin={pin}&totp={totp_code}"

    try:
        resp = requests.post(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get('accessToken')
            expiry = data.get('expiryTime', 'unknown')
            name = data.get('dhanClientName', '')
            if token:
                logger.info(f"Token generated for {name} (client: {client_id})")
                logger.info(f"Expires: {expiry}")
                return token, expiry
            else:
                logger.error(f"No accessToken in response: {data}")
                return None, None
        else:
            logger.error(f"Auth failed (HTTP {resp.status_code}): {resp.text[:200]}")
            return None, None
    except Exception as e:
        logger.error(f"Auth request failed: {e}")
        return None, None


def check_token_validity(config):
    """Check if current token is still valid by making a test API call."""
    try:
        from dhanhq import dhanhq
        dhan = dhanhq(config['client_id'], config['access_token'])
        result = dhan.expiry_list(13, 'IDX_I')  # NIFTY expiry test
        if result and result.get('status') == 'success':
            logger.info("Current token is VALID")
            return True
        else:
            logger.warning(f"Token check failed: {result}")
            return False
    except Exception as e:
        logger.warning(f"Token check error: {e}")
        return False


def refresh_token(config_path=None):
    """Main function: generate new token and update config."""
    if config_path is None:
        config_path = find_config()
    if config_path is None:
        logger.error("No dhan_creds.json found in any known path")
        return False

    config = load_config(config_path)

    # Validate required fields
    required = ['client_id', 'pin', 'totp_secret']
    missing = [f for f in required if not config.get(f)]
    if missing:
        logger.error(f"Missing fields in config: {missing}")
        return False

    # Generate TOTP
    totp_code = generate_totp(config['totp_secret'])
    logger.info(f"TOTP generated: {totp_code}")

    # Generate access token
    token, expiry = generate_access_token(
        config['client_id'],
        config['pin'],
        totp_code
    )

    if not token:
        # TOTP codes are time-sensitive (30s window). Retry once after a brief wait.
        logger.info("Retrying after 5 seconds (TOTP may have rotated)...")
        time.sleep(5)
        totp_code = generate_totp(config['totp_secret'])
        token, expiry = generate_access_token(
            config['client_id'],
            config['pin'],
            totp_code
        )

    if token:
        config['access_token'] = token
        config['token_expiry'] = expiry
        config['token_generated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_config(config_path, config)
        logger.info("ACCESS TOKEN REFRESHED SUCCESSFULLY")
        return True
    else:
        logger.error("FAILED to refresh access token after 2 attempts")
        return False


if __name__ == '__main__':
    if '--check' in sys.argv:
        config_path = find_config()
        if config_path:
            config = load_config(config_path)
            check_token_validity(config)
        else:
            logger.error("No config found")
    else:
        success = refresh_token()
        sys.exit(0 if success else 1)
