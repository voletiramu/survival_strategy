"""
Telegram Trade Notification System
Sends instant alerts for trade entries, exits, and daily summaries.
"""

import requests
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Telegram Bot Config
BOT_TOKEN = "8426384062:AAGJKO8y7ijbSMdbaFnxkfOeLFlnQZWc8oI"
CHAT_ID = 1722559857  # Ram's Telegram chat ID

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _get_chat_id():
    """Auto-discover chat_id from bot updates."""
    global CHAT_ID
    if CHAT_ID:
        return CHAT_ID
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", timeout=5)
        data = r.json()
        if data.get('result'):
            CHAT_ID = data['result'][-1]['message']['chat']['id']
            logger.info(f"Telegram chat_id discovered: {CHAT_ID}")
            return CHAT_ID
    except Exception as e:
        logger.warning(f"Could not get chat_id: {e}")
    return None


def send_message(text, parse_mode="HTML"):
    """Send a message via Telegram Bot API."""
    chat_id = _get_chat_id()
    if not chat_id:
        logger.warning("Telegram chat_id not available. Send a message to @Ramalgotradebot first.")
        return False
    try:
        url = f"{TELEGRAM_API}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200 and r.json().get('ok'):
            return True
        else:
            logger.warning(f"Telegram send failed: {r.text}")
            return False
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return False


def notify_trade_entry(market, strategy, symbol, signal_type, strike, entry_price,
                       spot, lot_size, multiplier, delta, target, sl, capital_used, reason):
    """Send notification when a new trade is entered."""
    direction = "BUY" if "BUY" in signal_type else "SELL"
    opt_type = "CE" if "CE" in signal_type else "PE"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"<b>{'🟢' if 'CE' in signal_type else '🔴'} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Strategy:</b> {strategy}\n"
        f"<b>Symbol:</b> {symbol} {strike} {opt_type}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Market:</b> {market}\n"
        f"<b>Entry Price:</b> ₹{entry_price:,.2f}\n"
        f"<b>Spot:</b> ₹{spot:,.2f}\n"
        f"<b>Lot Size:</b> {lot_size}x{multiplier}\n"
        f"<b>Delta:</b> {delta:.4f}\n"
        f"<b>Target:</b> ₹{target:,.2f}\n"
        f"<b>Stop Loss:</b> ₹{sl:,.2f}\n"
        f"<b>Capital Used:</b> ₹{capital_used:,.2f}\n"
        f"<b>Entry Time:</b> {now}\n"
        f"<b>Reason:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return send_message(msg)


def notify_trade_exit(market, strategy, symbol, signal_type, strike,
                      entry_price, exit_price, entry_time, pnl,
                      capital_used, exit_reason):
    """Send notification when a trade is exited."""
    opt_type = "CE" if "CE" in signal_type else "PE"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pnl_emoji = "💰" if pnl > 0 else "📉"
    pnl_pct = (pnl / capital_used * 100) if capital_used > 0 else 0
    final_amount = capital_used + pnl

    msg = (
        f"<b>{pnl_emoji} TRADE EXIT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Strategy:</b> {strategy}\n"
        f"<b>Symbol:</b> {symbol} {strike} {opt_type}\n"
        f"<b>Market:</b> {market}\n"
        f"<b>Entry Price:</b> ₹{entry_price:,.2f}\n"
        f"<b>Exit Price:</b> ₹{exit_price:,.2f}\n"
        f"<b>Entry Time:</b> {entry_time}\n"
        f"<b>Exit Time:</b> {now}\n"
        f"<b>PnL:</b> ₹{pnl:,.2f} ({pnl_pct:+.1f}%)\n"
        f"<b>Initial Amount:</b> ₹{capital_used:,.2f}\n"
        f"<b>Final Amount:</b> ₹{final_amount:,.2f}\n"
        f"<b>Exit Reason:</b> {exit_reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return send_message(msg)


def notify_daily_summary(equity_capital, equity_pnl, equity_positions, equity_closed,
                         commodity_capital, commodity_pnl, commodity_positions, commodity_closed,
                         equity_win_rate=0, commodity_win_rate=0):
    """Send end-of-day portfolio summary."""
    total_capital = equity_capital + commodity_capital
    total_pnl = equity_pnl + commodity_pnl
    total_pct = (total_pnl / total_capital * 100) if total_capital > 0 else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = (
        f"<b>📊 DAILY PORTFOLIO SUMMARY</b>\n"
        f"<b>Date:</b> {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"\n<b>EQUITY (NIFTY/BN/SENSEX)</b>\n"
        f"  Capital: ₹{equity_capital:,.0f}\n"
        f"  PnL: ₹{equity_pnl:,.2f}\n"
        f"  Open: {equity_positions} | Closed: {equity_closed}\n"
        f"  Win Rate: {equity_win_rate:.0f}%\n"
        f"\n<b>COMMODITY (GOLDM/SILVERM/CRUDEOILM)</b>\n"
        f"  Capital: ₹{commodity_capital:,.0f}\n"
        f"  PnL: ₹{commodity_pnl:,.2f}\n"
        f"  Open: {commodity_positions} | Closed: {commodity_closed}\n"
        f"  Win Rate: {commodity_win_rate:.0f}%\n"
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TOTAL PnL: ₹{total_pnl:,.2f} ({total_pct:+.1f}%)</b>\n"
        f"<b>TOTAL CAPITAL: ₹{total_capital:,.0f}</b>"
    )
    return send_message(msg)


def notify_scanner_start():
    """Send notification when scanner starts."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"<b>🚀 ALGO TRADING BOT STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Time:</b> {now}\n"
        f"<b>Equity:</b> NIFTY, BANKNIFTY, SENSEX (9:15-15:30)\n"
        f"<b>Commodity:</b> GOLDM, SILVERM, CRUDEOILM (15:30-23:30)\n"
        f"<b>Strategies:</b> CPR, Gamma Blast, Ghost Zone, PCR+VWAP, Survivor\n"
        f"<b>Capital:</b> ₹3,00,000 Equity + ₹3,00,000 Commodity\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return send_message(msg)


def notify_error(error_msg):
    """Send notification for critical errors."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"<b>⚠️ ALGO BOT ERROR</b>\n"
        f"<b>Time:</b> {now}\n"
        f"<b>Error:</b> {error_msg}"
    )
    return send_message(msg)


if __name__ == "__main__":
    # Test: send a test message
    print("Testing Telegram Bot connection...")
    chat_id = _get_chat_id()
    if chat_id:
        print(f"Chat ID: {chat_id}")
        ok = send_message(
            "<b>🤖 Algo Trade Bot Connected!</b>\n"
            "Trade notifications are now active.\n"
            "You will receive alerts for every entry and exit."
        )
        print(f"Message sent: {ok}")
    else:
        print("No chat_id found. Please send a message to @Ramalgotradebot first.")
