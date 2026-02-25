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
                       spot, lot_size, multiplier, delta, target, sl, capital_used, reason,
                       capital_available=None, instance_id=None, total_invested=None):
    """Send notification when a new trade is entered."""
    direction = "BUY" if "BUY" in signal_type else "SELL"
    opt_type = "CE" if "CE" in signal_type else "PE"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    capital_lines = ""
    if total_invested is not None:
        capital_lines += f"<b>📊 Total Invested:</b> ₹{total_invested:,.2f}\n"
    if capital_available is not None:
        capital_lines += f"<b>💰 Capital Available:</b> ₹{capital_available:,.2f}\n"

    instance_line = ""
    if instance_id:
        label = '☁️ GCloud VM' if instance_id == 'gcloud' else '🖥️ Local Machine'
        instance_line = f"<b>📍 Instance:</b> {label}\n"

    msg = (
        f"<b>{'🟢' if 'CE' in signal_type else '🔴'} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{instance_line}"
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
        f"{capital_lines}"
        f"<b>Entry Time:</b> {now}\n"
        f"<b>Reason:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return send_message(msg)


def notify_trade_exit(market, strategy, symbol, signal_type, strike,
                      entry_price, exit_price, entry_time, pnl,
                      capital_used, exit_reason, capital_available=None,
                      instance_id=None, total_invested=None):
    """Send notification when a trade is exited."""
    opt_type = "CE" if "CE" in signal_type else "PE"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pnl_emoji = "💰" if pnl > 0 else "📉"
    pnl_pct = (pnl / capital_used * 100) if capital_used > 0 else 0
    final_amount = capital_used + pnl

    capital_lines = ""
    if total_invested is not None:
        capital_lines += f"<b>📊 Total Invested:</b> ₹{total_invested:,.2f}\n"
    if capital_available is not None:
        capital_lines += f"<b>💰 Capital Available:</b> ₹{capital_available:,.2f}\n"

    instance_line = ""
    if instance_id:
        label = '☁️ GCloud VM' if instance_id == 'gcloud' else '🖥️ Local Machine'
        instance_line = f"<b>📍 Instance:</b> {label}\n"

    msg = (
        f"<b>{pnl_emoji} TRADE EXIT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{instance_line}"
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
        f"{capital_lines}"
        f"<b>Exit Reason:</b> {exit_reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return send_message(msg)


def notify_daily_summary(equity_capital, equity_pnl, equity_positions, equity_closed,
                         commodity_capital, commodity_pnl, commodity_positions, commodity_closed,
                         equity_win_rate=0, commodity_win_rate=0,
                         equity_signals=0, equity_dummy_pnl=0,
                         commodity_signals=0, commodity_dummy_pnl=0,
                         equity_capital_used=0, commodity_capital_used=0):
    """Send end-of-day portfolio summary with actual + dummy PnL."""
    total_capital = equity_capital + commodity_capital
    total_pnl = equity_pnl + commodity_pnl
    total_pct = (total_pnl / total_capital * 100) if total_capital > 0 else 0
    total_dummy = equity_dummy_pnl + commodity_dummy_pnl
    total_signals = equity_signals + commodity_signals
    total_trades = equity_positions + equity_closed + commodity_positions + commodity_closed
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = (
        f"<b>📊 DAILY PORTFOLIO SUMMARY</b>\n"
        f"<b>Date:</b> {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"\n<b>EQUITY (NIFTY/BN/SENSEX)</b>\n"
        f"  Capital: ₹{equity_capital:,.0f} | Used: ₹{equity_capital_used:,.0f}\n"
        f"  PnL: ₹{equity_pnl:,.2f}\n"
        f"  Open: {equity_positions} | Closed: {equity_closed}\n"
        f"  Win Rate: {equity_win_rate:.0f}%\n"
        f"\n<b>COMMODITY (GOLDM/SILVERM/CRUDEOILM)</b>\n"
        f"  Capital: ₹{commodity_capital:,.0f} | Used: ₹{commodity_capital_used:,.0f}\n"
        f"  PnL: ₹{commodity_pnl:,.2f}\n"
        f"  Open: {commodity_positions} | Closed: {commodity_closed}\n"
        f"  Win Rate: {commodity_win_rate:.0f}%\n"
        f"\n<b>SIGNAL ANALYSIS</b>\n"
        f"  Equity Signals: {equity_signals} | Trades Taken: {equity_positions + equity_closed}\n"
        f"  Commodity Signals: {commodity_signals} | Trades Taken: {commodity_positions + commodity_closed}\n"
        f"  Total Signals: {total_signals} | Total Trades: {total_trades}\n"
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ACTUAL PnL (trades taken): ₹{total_pnl:,.2f} ({total_pct:+.1f}%)</b>\n"
        f"<b>DUMMY PnL (all signals): ₹{total_dummy:,.2f}</b>\n"
        f"<b>TOTAL CAPITAL: ₹{total_capital:,.0f}</b>"
    )
    return send_message(msg)


def notify_scanner_start(instance_id=None):
    """Send notification when scanner starts."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if instance_id:
        label = '☁️ GCloud VM' if instance_id == 'gcloud' else '🖥️ Local Machine'
        instance_line = f"<b>📍 Running on:</b> {label}\n"
        warning_line = f"⚠️ Ensure the other instance is STOPPED to avoid duplicate trades!\n"
    else:
        instance_line = ""
        warning_line = ""

    msg = (
        f"<b>🚀 ALGO TRADING BOT STARTED (Threaded)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Time:</b> {now}\n"
        f"{instance_line}"
        f"{warning_line}"
        f"<b>Equity:</b> NIFTY, BANKNIFTY, SENSEX (9:15-15:30) | Every 45s\n"
        f"<b>Commodity:</b> GOLDM, SILVERM, CRUDEOILM (9:15-23:30) | Every 30s\n"
        f"<b>Crypto:</b> BTC, ETH, SOL (24/7) | Every 5min\n"
        f"<b>Strategies:</b> CPR, Gamma Blast, Ghost Zone, PCR+VWAP, Survivor\n"
        f"<b>Capital:</b> ₹2,00,000 Equity + ₹1,00,000 Commodity = ₹3,00,000\n"
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


def notify_active_trades():
    """Push all active (open) trades from equity + commodity portfolios to Telegram."""
    import json, os
    base = os.path.dirname(os.path.abspath(__file__))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Load equity portfolio
    eq_file = os.path.join(base, 'paper_trades', 'portfolio_state.json')
    eq_positions, eq_closed, eq_capital = [], [], 300000
    try:
        with open(eq_file) as f:
            eq = json.load(f)
        eq_positions = eq.get('positions', [])
        eq_closed = eq.get('closed_trades', [])
        eq_capital = eq.get('capital', 300000)
    except Exception:
        pass

    # Load commodity portfolio
    comm_file = os.path.join(base, 'paper_trades_commodity', 'commodity_portfolio_state.json')
    comm_positions, comm_closed, comm_capital = [], [], 300000
    try:
        with open(comm_file) as f:
            comm = json.load(f)
        comm_positions = comm.get('positions', [])
        comm_closed = comm.get('closed_trades', [])
        comm_capital = comm.get('capital', 300000)
    except Exception:
        pass

    # Build message — EQUITY OPEN POSITIONS
    total_unrealized = 0
    msg = f"<b>📋 ACTIVE TRADES REPORT</b>\n<b>Time:</b> {now}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"

    if eq_positions:
        msg += f"\n<b>EQUITY — {len(eq_positions)} Open Positions</b>\n"
        for p in eq_positions:
            pnl = p.get('unrealized_pnl', 0)
            total_unrealized += pnl
            emoji = "🟢" if pnl >= 0 else "🔴"
            opt = "CE" if "CE" in p.get('signal_type', '') else "PE"
            msg += (
                f"{emoji} {p['symbol']} {p.get('strike',0):.0f}{opt} "
                f"| {p['strategy']} | Entry:₹{p['entry_premium']:.1f} "
                f"Cur:₹{p.get('current_premium', p['entry_premium']):.1f} "
                f"| PnL:₹{pnl:+,.0f} | Δ:{p.get('delta',0):.3f}\n"
            )

    if comm_positions:
        msg += f"\n<b>COMMODITY — {len(comm_positions)} Open Positions</b>\n"
        for p in comm_positions:
            pnl = p.get('unrealized_pnl', 0)
            total_unrealized += pnl
            emoji = "🟢" if pnl >= 0 else "🔴"
            opt = "CE" if "CE" in p.get('signal_type', '') else "PE"
            msg += (
                f"{emoji} {p.get('commodity', p.get('symbol','?'))} {p.get('strike',0):.0f}{opt} "
                f"| {p['strategy']} | Entry:₹{p['entry_premium']:.1f} "
                f"Cur:₹{p.get('current_premium', p['entry_premium']):.1f} "
                f"| PnL:₹{pnl:+,.0f} | Δ:{p.get('delta',0):.3f}\n"
            )

    if not eq_positions and not comm_positions:
        msg += "\nNo open positions.\n"

    # Closed trades summary
    eq_closed_pnl = sum(t.get('pnl', 0) for t in eq_closed)
    comm_closed_pnl = sum(t.get('pnl', 0) for t in comm_closed)
    eq_wins = sum(1 for t in eq_closed if t.get('pnl', 0) > 0)
    comm_wins = sum(1 for t in comm_closed if t.get('pnl', 0) > 0)

    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"<b>TODAY'S SUMMARY</b>\n"
    msg += f"Open Positions: {len(eq_positions) + len(comm_positions)}\n"
    msg += f"Unrealized PnL: ₹{total_unrealized:+,.2f}\n"
    msg += f"Closed Trades: {len(eq_closed) + len(comm_closed)}\n"
    msg += f"Realized PnL: ₹{eq_closed_pnl + comm_closed_pnl:+,.2f}\n"
    if eq_closed:
        wr = eq_wins / len(eq_closed) * 100
        msg += f"Equity Win Rate: {wr:.0f}% ({eq_wins}/{len(eq_closed)})\n"
    if comm_closed:
        wr = comm_wins / len(comm_closed) * 100
        msg += f"Commodity Win Rate: {wr:.0f}% ({comm_wins}/{len(comm_closed)})\n"
    msg += f"Equity Capital: ₹{eq_capital:,.0f}\n"
    msg += f"Commodity Capital: ₹{comm_capital:,.0f}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━"

    # Telegram has 4096 char limit — split if needed
    if len(msg) > 4000:
        # Send in two parts
        mid = msg.find('\n<b>COMMODITY')
        if mid == -1:
            mid = len(msg) // 2
        send_message(msg[:mid])
        return send_message(msg[mid:])
    else:
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
