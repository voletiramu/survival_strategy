"""
Zerodha Kite Connect — Daily Access Token Generator.

This script automates the daily login process using TOTP (2FA).
Run this once before market opens (e.g., 8:45 AM via Task Scheduler).

Usage:
    python zerodha_token_gen.py

First-time setup:
    python zerodha_token_gen.py --setup

Requirements:
    pip install kiteconnect pyotp requests
"""
import os
import sys
import json
import time
import logging
import argparse
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Config paths
CONFIG_DIR = Path('D:/AlgoTrading/config')
if not CONFIG_DIR.exists():
    CONFIG_DIR = Path('/root/algo_trading/config')

CREDS_FILE = CONFIG_DIR / 'zerodha_creds.json'
TOKEN_FILE = CONFIG_DIR / 'zerodha_token.json'


def setup_credentials():
    """Interactive setup — store API key, secret, and TOTP seed."""
    print("\n" + "=" * 60)
    print("  Zerodha Kite Connect — First-Time Setup")
    print("=" * 60)
    print()
    print("  Steps to get your credentials:")
    print("  1. Go to https://developers.kite.trade")
    print("  2. Login with your Zerodha account")
    print("  3. Create a new app (or use existing)")
    print("  4. Note down: API Key, API Secret")
    print("  5. Set Redirect URL to: http://127.0.0.1:5555/callback")
    print()
    print("  For TOTP automation:")
    print("  - In Zerodha Console > My Profile > Security")
    print("  - When setting up TOTP, copy the 'secret key' (seed)")
    print("  - This is the base32 string shown during TOTP setup")
    print()

    api_key = input("  Enter API Key: ").strip()
    api_secret = input("  Enter API Secret: ").strip()
    totp_seed = input("  Enter TOTP Seed (base32, or leave blank for manual login): ").strip()
    user_id = input("  Enter Zerodha User ID (e.g., AB1234): ").strip()
    password = input("  Enter Zerodha Password: ").strip()

    creds = {
        'api_key': api_key,
        'api_secret': api_secret,
        'totp_seed': totp_seed,
        'user_id': user_id,
        'password': password,
        'redirect_url': 'http://127.0.0.1:5555/callback',
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(json.dumps(creds, indent=2))
    print(f"\n  ✅ Credentials saved to {CREDS_FILE}")
    print("  ⚠️  Keep this file secure — it contains your login credentials!")
    print()
    return creds


def generate_token_auto(creds):
    """Fully automated token generation using Selenium headless Chrome + TOTP.

    Flow:
    1. Navigate to Kite Connect login URL
    2. Enter user_id (id='userid') + password (id='password')
    3. Click Login button
    4. Enter TOTP (input type='number' — auto-submits after 6 digits)
    5. If authorize page shown, click Authorize; otherwise auto-redirects
    6. Capture request_token from callback redirect URL
    7. Exchange request_token for access_token via KiteConnect API
    """
    import pyotp
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options

    api_key = creds['api_key']
    api_secret = creds['api_secret']
    totp_seed = creds.get('totp_seed', '')
    user_id = creds.get('user_id', '')
    password = creds.get('password', '')

    if not totp_seed or not user_id or not password:
        logger.error("TOTP seed, user ID, or password missing. Use --setup to configure.")
        return None

    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1280,720')

    driver = None
    request_token = None

    try:
        driver = webdriver.Chrome(options=chrome_options)
        wait = WebDriverWait(driver, 20)

        # Step 1: Navigate to Kite Connect login
        login_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
        logger.info("Step 1: Opening Kite login page...")
        driver.get(login_url)
        time.sleep(2)

        # Step 2: Enter user_id and password using correct element IDs
        logger.info("Step 2: Entering credentials...")
        uid_field = wait.until(EC.presence_of_element_located((By.ID, 'userid')))
        uid_field.clear()
        uid_field.send_keys(user_id)

        pwd_field = driver.find_element(By.ID, 'password')
        pwd_field.clear()
        pwd_field.send_keys(password)

        # Click Login/Submit button
        driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        logger.info("Login submitted")
        time.sleep(3)

        # Step 3: Enter TOTP — field is input[type="number"] (auto-submits after 6 digits)
        logger.info("Step 3: Entering TOTP...")
        totp = pyotp.TOTP(totp_seed)
        totp_value = totp.now()

        totp_field = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, 'input[type="number"]')
        ))
        totp_field.send_keys(totp_value)
        logger.info(f"TOTP entered: {totp_value}")

        # TOTP auto-submits after 6 digits, but click submit if available
        time.sleep(2)
        try:
            submit_btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            if submit_btn.is_displayed():
                submit_btn.click()
                logger.info("TOTP submit clicked")
        except Exception:
            logger.info("TOTP auto-submitted")

        time.sleep(4)

        # Step 4: Handle authorize page or auto-redirect
        if 'request_token' in driver.current_url:
            logger.info("Auto-redirected with token!")
        else:
            # Authorize page — click Authorize button
            logger.info("Step 4: Looking for Authorize button...")
            buttons = driver.find_elements(By.TAG_NAME, 'button')
            authorize_clicked = False

            for btn in buttons:
                btn_text = btn.text.lower()
                if 'authorize' in btn_text or 'allow' in btn_text:
                    btn.click()
                    authorize_clicked = True
                    logger.info(f"Clicked Authorize button")
                    break

            if not authorize_clicked and buttons:
                # Click any visible submit button
                for btn in buttons:
                    if btn.is_displayed():
                        btn.click()
                        logger.info(f"Clicked button: {btn.text}")
                        break

            time.sleep(4)

        # Step 5: Extract request_token from redirect URL
        logger.info("Step 5: Extracting request token...")
        for _ in range(15):
            current_url = driver.current_url
            if 'request_token' in current_url:
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                request_token = params.get('request_token', [''])[0]
                if request_token:
                    logger.info(f"Got request token: {request_token[:8]}...")
                    break
            time.sleep(1)

        if not request_token:
            logger.error(f"Failed to get request_token. Final URL: {driver.current_url}")
            try:
                debug_path = str(CONFIG_DIR / 'kite_auth_debug.png')
                driver.save_screenshot(debug_path)
                logger.info(f"Debug screenshot saved: {debug_path}")
            except Exception:
                pass
            return None

    except Exception as e:
        logger.error(f"Selenium failed: {e}")
        if driver:
            try:
                driver.save_screenshot(str(CONFIG_DIR / 'kite_auth_error.png'))
            except Exception:
                pass
        return None
    finally:
        if driver:
            driver.quit()

    # Step 6: Exchange request_token for access_token
    logger.info("Step 6: Exchanging for access token...")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data['access_token']

    logger.info(f"Access token generated: {access_token[:8]}...")
    return access_token


def generate_token_manual(creds):
    """Manual token generation — opens browser for login."""
    from kiteconnect import KiteConnect

    api_key = creds['api_key']
    api_secret = creds['api_secret']

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print(f"\n  Opening browser for login...")
    print(f"  URL: {login_url}")
    print(f"\n  After login, you'll be redirected to a URL like:")
    print(f"  http://127.0.0.1:5555/callback?request_token=XXXX&action=login")
    print(f"\n  Copy the 'request_token' value from the URL.")
    print()

    webbrowser.open(login_url)

    request_token = input("  Paste request_token here: ").strip()
    if not request_token:
        logger.error("No request_token provided")
        return None

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data['access_token']

    logger.info(f"✅ Access token generated: {access_token[:8]}...")
    return access_token


def save_token(access_token):
    """Save access token with today's date."""
    token_data = {
        'access_token': access_token,
        'date': datetime.now().strftime('%Y-%m-%d'),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    logger.info(f"Token saved to {TOKEN_FILE}")


def main():
    parser = argparse.ArgumentParser(description='Zerodha Kite daily token generator')
    parser.add_argument('--setup', action='store_true', help='First-time credential setup')
    parser.add_argument('--manual', action='store_true', help='Manual browser-based login')
    args = parser.parse_args()

    # Setup mode
    if args.setup:
        setup_credentials()
        return

    # Load credentials
    if not CREDS_FILE.exists():
        print(f"  ❌ Credentials file not found: {CREDS_FILE}")
        print(f"  Run: python zerodha_token_gen.py --setup")
        return

    creds = json.loads(CREDS_FILE.read_text())

    # Check if today's token already exists
    if TOKEN_FILE.exists():
        existing = json.loads(TOKEN_FILE.read_text())
        if existing.get('date') == datetime.now().strftime('%Y-%m-%d'):
            logger.info(f"Today's token already exists (generated at {existing.get('generated_at')})")
            response = input("  Regenerate? (y/N): ").strip().lower()
            if response != 'y':
                return

    # Generate token
    if args.manual or not creds.get('totp_seed'):
        access_token = generate_token_manual(creds)
    else:
        try:
            access_token = generate_token_auto(creds)
        except Exception as e:
            logger.warning(f"Auto token gen failed: {e}. Falling back to manual...")
            access_token = generate_token_manual(creds)

    if access_token:
        save_token(access_token)
        print(f"\n  ✅ Done! Token valid for today ({datetime.now().strftime('%Y-%m-%d')})")
        print(f"  Your bots will use Zerodha as Source 1 automatically.")
    else:
        print(f"\n  ❌ Token generation failed. Check credentials and try again.")


if __name__ == '__main__':
    main()
