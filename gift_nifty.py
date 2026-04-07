"""
GIFT Nifty Pre-Market Data Fetcher
Fetches GIFT Nifty (formerly SGX Nifty) price before NSE opens.
Available 8:00 AM IST onwards from Google Finance.
"""
import json
import os
import logging
from datetime import datetime, time as dtime

logger = logging.getLogger("GiftNifty")

GIFT_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades", "gift_nifty_today.json")

def fetch_gift_nifty():
    """Fetch GIFT Nifty price from multiple sources."""
    gift_price = None
    source = None

    # Method 1: TrueData (if connected)
    try:
        from truedata_ws.websocket.TD import TD
        td = TD("Trial138", "rambabu138", live_port=8086)
        data = td.get_historic_data("NIFTY 50", duration="1 D", bar_size="1 min")
        if data is not None and len(data) > 0:
            gift_price = float(data["close"].iloc[-1])
            source = "TRUEDATA"
            td.disconnect()
            logger.info(f"[GiftNifty] TrueData NIFTY last: {gift_price}")
    except Exception as e:
        logger.debug(f"[GiftNifty] TrueData failed: {e}")

    # Method 2: Use previous day close + Zerodha pre-market if available
    if gift_price is None:
        try:
            import requests
            # Google Finance GIFT Nifty
            url = "https://www.google.com/finance/quote/NIFTY_50:INDEXNSE"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                text = resp.text
                # Extract price from meta tag
                import re
                match = re.search(r'"price":"([\d,.]+)"', text)
                if match:
                    gift_price = float(match.group(1).replace(",", ""))
                    source = "GOOGLE"
                    logger.info(f"[GiftNifty] Google Finance NIFTY: {gift_price}")
        except Exception as e:
            logger.debug(f"[GiftNifty] Google Finance failed: {e}")

    # Method 3: NSE pre-open session
    if gift_price is None:
        try:
            import requests
            url = "https://www.nseindia.com/api/marketStatus"
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            }
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            resp = session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for mkt in data.get("marketState", []):
                    if "NIFTY" in mkt.get("index", ""):
                        gift_price = float(mkt.get("last", 0))
                        source = "NSE_PREOPEN"
                        break
        except Exception as e:
            logger.debug(f"[GiftNifty] NSE pre-open failed: {e}")

    return gift_price, source


def get_gap_analysis(gift_price, prev_close):
    """Analyze gap direction and magnitude."""
    if not gift_price or not prev_close or prev_close == 0:
        return None

    gap_pts = gift_price - prev_close
    gap_pct = gap_pts / prev_close * 100

    if gap_pct > 1.5:
        direction = "BIG_GAP_UP"
        bias = "CAUTION"  # May fill
        lot_multiplier = 0.5  # Half size
    elif gap_pct > 0.3:
        direction = "GAP_UP"
        bias = "CE_WEAK"  # Gap up days are choppy for us
        lot_multiplier = 0.7
    elif gap_pct < -1.5:
        direction = "BIG_GAP_DOWN"
        bias = "PE_STRONG"  # Strong PE but may bounce
        lot_multiplier = 1.0
    elif gap_pct < -0.3:
        direction = "GAP_DOWN"
        bias = "PE_STRONG"  # Our best setup
        lot_multiplier = 1.0
    else:
        direction = "FLAT"
        bias = "NEUTRAL"
        lot_multiplier = 1.0

    analysis = {
        "gift_price": round(gift_price, 2),
        "prev_close": round(prev_close, 2),
        "gap_pts": round(gap_pts, 1),
        "gap_pct": round(gap_pct, 2),
        "direction": direction,
        "bias": bias,
        "lot_multiplier": lot_multiplier,
        "timestamp": datetime.now().isoformat(),
    }

    # Save to file for bot to read
    try:
        os.makedirs(os.path.dirname(GIFT_CACHE_FILE), exist_ok=True)
        with open(GIFT_CACHE_FILE, "w") as f:
            json.dump(analysis, f, indent=2)
        logger.info(f"[GiftNifty] Gap: {direction} {gap_pct:+.2f}% ({gap_pts:+.0f} pts) | Bias: {bias} | Lots: {lot_multiplier}x")
    except Exception as e:
        logger.error(f"[GiftNifty] Save failed: {e}")

    return analysis


def load_gap_analysis():
    """Load today's gap analysis (called by bot at startup)."""
    try:
        if os.path.exists(GIFT_CACHE_FILE):
            with open(GIFT_CACHE_FILE) as f:
                data = json.load(f)
            # Check if it's today's data
            ts = data.get("timestamp", "")
            if ts[:10] == datetime.now().strftime("%Y-%m-%d"):
                return data
    except Exception:
        pass
    return None


def run_premarket():
    """Run pre-market analysis. Called by cron at 8:45 AM."""
    logger.info("[GiftNifty] Running pre-market analysis...")

    # Get GIFT Nifty price
    gift_price, source = fetch_gift_nifty()
    if not gift_price:
        logger.warning("[GiftNifty] Could not fetch GIFT Nifty price")
        return None

    # Get previous close from historical data
    prev_close = None
    try:
        import pandas as pd
        hist_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "equity_1min", "NIFTY_1min_60d.csv")
        if os.path.exists(hist_file):
            df = pd.read_csv(hist_file)
            df["DateTime"] = pd.to_datetime(df["DateTime"], utc=True)
            # Get last trading day's close
            last_day = df["DateTime"].dt.date.max()
            last_data = df[df["DateTime"].dt.date == last_day]
            if len(last_data) > 0:
                prev_close = float(last_data["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"[GiftNifty] Failed to get prev close: {e}")

    if not prev_close:
        logger.warning("[GiftNifty] No previous close available")
        return None

    analysis = get_gap_analysis(gift_price, prev_close)
    if analysis:
        analysis["source"] = source
        logger.info(f"[GiftNifty] Pre-market complete: {analysis['direction']} {analysis['gap_pct']:+.2f}%")

    return analysis


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_premarket()
    if result:
        print(f"GIFT Nifty: {result['gift_price']}")
        print(f"Gap: {result['direction']} {result['gap_pct']:+.2f}% ({result['gap_pts']:+.0f} pts)")
        print(f"Bias: {result['bias']} | Lot multiplier: {result['lot_multiplier']}x")
    else:
        print("Could not fetch GIFT Nifty data")
