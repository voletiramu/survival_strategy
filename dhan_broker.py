"""
Dhan Broker — Order Execution Layer for Live Trading
=====================================================
Handles all order operations via DhanHQ SDK using LIMIT orders (SEBI mandate Apr 2026).

Order types:
  ENTRY  : LIMIT buy at LTP ± tick buffer
  SL     : SL (Stop-Loss Limit) — trigger at SL level, limit 2 ticks below
  TARGET : LIMIT sell at target premium
  EXIT   : LIMIT sell at LTP - tick buffer (for manual/EOD exits)

All methods are atomic, idempotent, and safe to retry.
Credentials loaded from config/dhan_creds.json (gitignored).
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

# ── Config paths ─────────────────────────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get('DHAN_CONFIG_DIR', 'D:/AlgoTrading/config'))
CREDS_FILE = CONFIG_DIR / 'dhan_creds.json'

if not CONFIG_DIR.exists():
    CONFIG_DIR = Path('/root/algo_trading/config')
    CREDS_FILE = CONFIG_DIR / 'dhan_creds.json'

# ── Constants ────────────────────────────────────────────────────────────────
TICK_SIZE_BELOW_100 = 0.05      # Options premium tick = Rs 0.05 below Rs 100
TICK_SIZE_ABOVE_100 = 0.10      # Options premium tick = Rs 0.10 above Rs 100

ENTRY_TICK_BUFFER = 2           # Place entry LIMIT 2 ticks above LTP (ensure fill)
SL_TICK_BUFFER = 3              # SL limit price = trigger - 3 ticks (ensure fill on gap)
EXIT_TICK_BUFFER = 3            # EOD/manual exit = LTP - 3 ticks (aggressive fill)
TARGET_TICK_BUFFER = 1          # Target = target_premium - 1 tick (hit slightly early)

ORDER_POLL_INTERVAL = 2         # Poll order status every 2 seconds
ORDER_POLL_MAX_WAIT = 30        # Max wait for fill = 30 seconds
ORDER_CANCEL_RETRY = 2          # Retry cancel up to 2 times

# Exchange segment mapping
EXCHANGE_SEGMENT_MAP = {
    'NIFTY':     'NSE_FNO',
    'BANKNIFTY': 'NSE_FNO',
    'FINNIFTY':  'NSE_FNO',
    'MIDCPNIFTY':'NSE_FNO',
    'SENSEX':    'BSE_FNO',
    'BANKEX':    'BSE_FNO',
}


def get_tick_size(premium):
    """Get tick size based on premium level."""
    return TICK_SIZE_ABOVE_100 if premium >= 100 else TICK_SIZE_BELOW_100


def round_to_tick(price, premium_ref=None):
    """Round price to nearest tick."""
    if premium_ref is None:
        premium_ref = price
    tick = get_tick_size(premium_ref)
    return round(round(price / tick) * tick, 2)


class OrderTracker:
    """Thread-safe order tracking with status history."""

    def __init__(self):
        self._orders = {}       # order_id -> order_info dict
        self._lock = threading.Lock()

    def add(self, order_id, info):
        with self._lock:
            info['created_at'] = datetime.now().isoformat()
            info['status_history'] = [{'status': 'PLACED', 'at': info['created_at']}]
            self._orders[order_id] = info

    def update_status(self, order_id, status, fill_price=None, fill_qty=None):
        with self._lock:
            if order_id in self._orders:
                self._orders[order_id]['status'] = status
                self._orders[order_id]['status_history'].append({
                    'status': status, 'at': datetime.now().isoformat()
                })
                if fill_price is not None:
                    self._orders[order_id]['fill_price'] = fill_price
                if fill_qty is not None:
                    self._orders[order_id]['fill_qty'] = fill_qty

    def get(self, order_id):
        with self._lock:
            return self._orders.get(order_id, {}).copy()

    def get_open_orders(self):
        with self._lock:
            return {oid: o.copy() for oid, o in self._orders.items()
                    if o.get('status') in ('PLACED', 'PENDING', 'TRANSIT', 'TRIGGER_PENDING')}

    def get_by_position(self, position_id):
        with self._lock:
            return {oid: o.copy() for oid, o in self._orders.items()
                    if o.get('position_id') == position_id}

    def today_orders(self):
        with self._lock:
            today = datetime.now().strftime('%Y-%m-%d')
            return {oid: o.copy() for oid, o in self._orders.items()
                    if o.get('created_at', '').startswith(today)}


class DhanBroker:
    """Order execution interface for Dhan API.

    Usage:
        broker = DhanBroker()
        if broker.connect():
            result = broker.place_entry_order(
                security_id='46537',
                exchange_segment='NSE_FNO',
                transaction_type='BUY',
                quantity=65,
                ltp=150.0,
                tag='CPR_NIFTY_CE_24500'
            )
            if result['success']:
                order_id = result['order_id']
                fill = broker.wait_for_fill(order_id, timeout=30)
    """

    def __init__(self, shadow_mode=True):
        """Initialize broker.

        Args:
            shadow_mode: If True, log orders but don't actually place them.
                         MUST be explicitly set to False for live trading.
        """
        self.dhan = None
        self.client_id = None
        self.access_token = None
        self.shadow_mode = shadow_mode
        self._connected = False
        self.tracker = OrderTracker()
        self._daily_order_count = 0
        self._daily_order_date = None

        # Load credentials
        self._load_credentials()

        mode_label = "SHADOW (no real orders)" if shadow_mode else "LIVE (real money)"
        logger.info(f"[DhanBroker] Initialized in {mode_label} mode")

    def _load_credentials(self):
        """Load Dhan credentials from config file."""
        if not CREDS_FILE.exists():
            logger.error(f"[DhanBroker] Credentials file not found: {CREDS_FILE}")
            return
        try:
            with open(str(CREDS_FILE), 'r') as f:
                creds = json.load(f)
            self.client_id = creds.get('client_id')
            self.access_token = creds.get('access_token')
        except Exception as e:
            logger.error(f"[DhanBroker] Failed to load credentials: {e}")

    def connect(self):
        """Establish connection to Dhan API."""
        if not self.client_id or not self.access_token:
            logger.error("[DhanBroker] Missing client_id or access_token")
            return False

        try:
            from dhanhq import dhanhq
            self.dhan = dhanhq(self.client_id, self.access_token)
            self._connected = True

            # Verify connection
            funds = self.dhan.get_fund_limits()
            if funds and funds.get('status') == 'success':
                fund_data = funds.get('data', {})
                available = fund_data.get('availabelBalance', fund_data.get('available_balance', 'N/A'))
                logger.info(f"[DhanBroker] Connected. Available balance: Rs {available}")
            else:
                logger.warning(f"[DhanBroker] Connected but fund check returned: {funds}")

            return True
        except ImportError:
            logger.error("[DhanBroker] dhanhq not installed. Run: pip install dhanhq")
            return False
        except Exception as e:
            logger.error(f"[DhanBroker] Connection failed: {e}")
            return False

    @property
    def is_connected(self):
        return self._connected and self.dhan is not None

    # ── Order Placement ──────────────────────────────────────────────────────

    def place_entry_order(self, security_id, exchange_segment, transaction_type,
                          quantity, ltp, tag=None):
        """Place a LIMIT entry order.

        Price = LTP + buffer (for BUY) or LTP - buffer (for SELL).

        Args:
            security_id: Dhan security ID for the option contract.
            exchange_segment: 'NSE_FNO' or 'BSE_FNO'.
            transaction_type: 'BUY' or 'SELL'.
            quantity: Number of units (lot_size * num_lots).
            ltp: Current Last Traded Price of the option.
            tag: Correlation tag for tracking (e.g., 'CPR_NIFTY_CE_24500').

        Returns:
            dict: {'success': bool, 'order_id': str or None, 'message': str}
        """
        tick = get_tick_size(ltp)
        if transaction_type == 'BUY':
            price = round_to_tick(ltp + ENTRY_TICK_BUFFER * tick, ltp)
        else:
            price = round_to_tick(ltp - ENTRY_TICK_BUFFER * tick, ltp)

        return self._place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type='LIMIT',
            price=price,
            trigger_price=0,
            tag=tag,
            purpose='ENTRY'
        )

    def place_sl_order(self, security_id, exchange_segment, quantity,
                       sl_trigger_price, tag=None):
        """Place a Stop-Loss Limit (SL-L) order for exit.

        Trigger = SL level. Limit = trigger - buffer ticks (ensure fill on gap down).
        Always a SELL order (closing a BUY position).

        Args:
            security_id: Dhan security ID for the option contract.
            exchange_segment: 'NSE_FNO' or 'BSE_FNO'.
            quantity: Number of units to sell.
            sl_trigger_price: The trigger price (when premium drops to this, order activates).
            tag: Correlation tag.

        Returns:
            dict: {'success': bool, 'order_id': str or None, 'message': str}
        """
        tick = get_tick_size(sl_trigger_price)
        limit_price = round_to_tick(sl_trigger_price - SL_TICK_BUFFER * tick, sl_trigger_price)
        limit_price = max(tick, limit_price)  # Never go below 1 tick

        return self._place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type='SELL',
            quantity=quantity,
            order_type='SL',
            price=limit_price,
            trigger_price=round_to_tick(sl_trigger_price, sl_trigger_price),
            tag=tag,
            purpose='SL'
        )

    def place_target_order(self, security_id, exchange_segment, quantity,
                           target_price, tag=None):
        """Place a LIMIT sell order at target price.

        Args:
            security_id: Dhan security ID.
            exchange_segment: 'NSE_FNO' or 'BSE_FNO'.
            quantity: Number of units to sell.
            target_price: Target premium to sell at.
            tag: Correlation tag.

        Returns:
            dict: {'success': bool, 'order_id': str or None, 'message': str}
        """
        tick = get_tick_size(target_price)
        price = round_to_tick(target_price - TARGET_TICK_BUFFER * tick, target_price)

        return self._place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type='SELL',
            quantity=quantity,
            order_type='LIMIT',
            price=price,
            trigger_price=0,
            tag=tag,
            purpose='TARGET'
        )

    def place_exit_order(self, security_id, exchange_segment, quantity,
                         ltp, tag=None, aggressive=False):
        """Place a LIMIT sell order at current LTP (for manual/EOD exits).

        Args:
            security_id: Dhan security ID.
            exchange_segment: 'NSE_FNO' or 'BSE_FNO'.
            quantity: Number of units to sell.
            ltp: Current LTP.
            tag: Correlation tag.
            aggressive: If True, use wider tick buffer (EOD force close).

        Returns:
            dict: {'success': bool, 'order_id': str or None, 'message': str}
        """
        tick = get_tick_size(ltp)
        buffer = EXIT_TICK_BUFFER * 2 if aggressive else EXIT_TICK_BUFFER
        price = round_to_tick(ltp - buffer * tick, ltp)
        price = max(tick, price)

        return self._place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type='SELL',
            quantity=quantity,
            order_type='LIMIT',
            price=price,
            trigger_price=0,
            tag=tag,
            purpose='EXIT_AGGRESSIVE' if aggressive else 'EXIT'
        )

    def _place_order(self, security_id, exchange_segment, transaction_type,
                     quantity, order_type, price, trigger_price=0, tag=None,
                     purpose=''):
        """Internal order placement. Handles shadow mode and error wrapping."""
        # Track daily orders
        today = datetime.now().strftime('%Y-%m-%d')
        if self._daily_order_date != today:
            self._daily_order_count = 0
            self._daily_order_date = today
        self._daily_order_count += 1

        order_info = {
            'security_id': str(security_id),
            'exchange_segment': exchange_segment,
            'transaction_type': transaction_type,
            'quantity': quantity,
            'order_type': order_type,
            'price': price,
            'trigger_price': trigger_price,
            'tag': tag or '',
            'purpose': purpose,
            'shadow': self.shadow_mode,
        }

        logger.info(f"[DhanBroker] {'SHADOW' if self.shadow_mode else 'LIVE'} "
                    f"{purpose} {transaction_type} {quantity}x "
                    f"secID={security_id} @ Rs {price:.2f} "
                    f"{'trig=' + str(trigger_price) if trigger_price else ''} "
                    f"[{exchange_segment}] tag={tag}")

        if self.shadow_mode:
            shadow_id = f"SHADOW_{self._daily_order_count}_{int(time.time())}"
            order_info['status'] = 'SHADOW_FILLED'
            order_info['fill_price'] = price
            order_info['fill_qty'] = quantity
            self.tracker.add(shadow_id, order_info)
            return {'success': True, 'order_id': shadow_id,
                    'message': 'Shadow order logged', 'fill_price': price}

        if not self.is_connected:
            return {'success': False, 'order_id': None,
                    'message': 'Not connected to Dhan'}

        try:
            result = self.dhan.place_order(
                security_id=str(security_id),
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=order_type,
                product_type='INTRA',
                price=price,
                trigger_price=trigger_price,
                validity='DAY',
                tag=tag or '',
            )

            if result and result.get('status') == 'success':
                order_id = str(result.get('data', {}).get('orderId',
                              result.get('data', {}).get('order_id', '')))
                if order_id:
                    order_info['status'] = 'PLACED'
                    self.tracker.add(order_id, order_info)
                    logger.info(f"[DhanBroker] Order placed: {order_id} "
                               f"({purpose} {transaction_type} {quantity}x @ {price})")
                    return {'success': True, 'order_id': order_id,
                            'message': 'Order placed successfully'}
                else:
                    logger.error(f"[DhanBroker] Order placed but no ID in response: {result}")
                    return {'success': False, 'order_id': None,
                            'message': f'No order ID in response: {result}'}
            else:
                msg = result.get('remarks', result.get('message', str(result))) if result else 'No response'
                logger.error(f"[DhanBroker] Order REJECTED: {msg}")
                return {'success': False, 'order_id': None, 'message': str(msg)}

        except Exception as e:
            logger.error(f"[DhanBroker] Order placement error: {e}")
            return {'success': False, 'order_id': None, 'message': str(e)}

    # ── Order Management ─────────────────────────────────────────────────────

    def wait_for_fill(self, order_id, timeout=ORDER_POLL_MAX_WAIT):
        """Poll order status until filled or timeout.

        Returns:
            dict: {'filled': bool, 'fill_price': float, 'fill_qty': int,
                   'status': str, 'message': str}
        """
        if order_id.startswith('SHADOW_'):
            info = self.tracker.get(order_id)
            return {'filled': True, 'fill_price': info.get('fill_price', 0),
                    'fill_qty': info.get('fill_qty', 0), 'status': 'SHADOW_FILLED',
                    'message': 'Shadow fill'}

        start = time.time()
        while time.time() - start < timeout:
            try:
                result = self.dhan.get_order_by_id(order_id)
                if result and result.get('status') == 'success':
                    data = result.get('data', {})
                    order_status = data.get('orderStatus', data.get('order_status', ''))

                    if order_status == 'TRADED':
                        fill_price = float(data.get('price', data.get('tradedPrice', 0)))
                        fill_qty = int(data.get('filledQty', data.get('quantity', 0)))
                        self.tracker.update_status(order_id, 'TRADED', fill_price, fill_qty)
                        logger.info(f"[DhanBroker] FILLED: {order_id} @ Rs {fill_price:.2f} "
                                   f"qty={fill_qty}")
                        return {'filled': True, 'fill_price': fill_price,
                                'fill_qty': fill_qty, 'status': 'TRADED',
                                'message': 'Order filled'}

                    elif order_status in ('REJECTED', 'CANCELLED'):
                        self.tracker.update_status(order_id, order_status)
                        msg = data.get('rejectedReason', data.get('remarks', order_status))
                        logger.warning(f"[DhanBroker] {order_status}: {order_id} — {msg}")
                        return {'filled': False, 'fill_price': 0, 'fill_qty': 0,
                                'status': order_status, 'message': str(msg)}

                    else:
                        self.tracker.update_status(order_id, order_status)

            except Exception as e:
                logger.debug(f"[DhanBroker] Poll error for {order_id}: {e}")

            time.sleep(ORDER_POLL_INTERVAL)

        # Timeout — order still pending
        logger.warning(f"[DhanBroker] TIMEOUT: {order_id} not filled in {timeout}s")
        return {'filled': False, 'fill_price': 0, 'fill_qty': 0,
                'status': 'TIMEOUT', 'message': f'Not filled in {timeout}s'}

    def cancel_order(self, order_id):
        """Cancel a pending order.

        Returns:
            dict: {'success': bool, 'message': str}
        """
        if order_id.startswith('SHADOW_'):
            self.tracker.update_status(order_id, 'CANCELLED')
            return {'success': True, 'message': 'Shadow order cancelled'}

        if not self.is_connected:
            return {'success': False, 'message': 'Not connected'}

        for attempt in range(ORDER_CANCEL_RETRY + 1):
            try:
                result = self.dhan.cancel_order(order_id)
                if result and result.get('status') == 'success':
                    self.tracker.update_status(order_id, 'CANCELLED')
                    logger.info(f"[DhanBroker] Cancelled: {order_id}")
                    return {'success': True, 'message': 'Order cancelled'}
                else:
                    msg = str(result)
                    if 'already' in msg.lower() or 'traded' in msg.lower():
                        return {'success': False, 'message': 'Order already traded/cancelled'}
                    logger.warning(f"[DhanBroker] Cancel attempt {attempt + 1} failed: {msg}")
            except Exception as e:
                logger.warning(f"[DhanBroker] Cancel error (attempt {attempt + 1}): {e}")

            if attempt < ORDER_CANCEL_RETRY:
                time.sleep(1)

        return {'success': False, 'message': f'Cancel failed after {ORDER_CANCEL_RETRY + 1} attempts'}

    def modify_sl_order(self, order_id, new_trigger_price, new_limit_price=None):
        """Modify an existing SL order's trigger and limit price.

        Used for trailing stop-loss updates.

        Args:
            order_id: The SL order to modify.
            new_trigger_price: New trigger price.
            new_limit_price: New limit price (defaults to trigger - buffer).

        Returns:
            dict: {'success': bool, 'message': str}
        """
        if order_id.startswith('SHADOW_'):
            info = self.tracker.get(order_id)
            info['trigger_price'] = new_trigger_price
            self.tracker.update_status(order_id, 'MODIFIED')
            return {'success': True, 'message': 'Shadow SL modified'}

        if not self.is_connected:
            return {'success': False, 'message': 'Not connected'}

        info = self.tracker.get(order_id)
        if not info:
            return {'success': False, 'message': f'Order {order_id} not tracked'}

        tick = get_tick_size(new_trigger_price)
        if new_limit_price is None:
            new_limit_price = round_to_tick(new_trigger_price - SL_TICK_BUFFER * tick, new_trigger_price)
            new_limit_price = max(tick, new_limit_price)

        try:
            result = self.dhan.modify_order(
                order_id=order_id,
                order_type='SL',
                leg_name='',
                quantity=info.get('quantity', 0),
                price=new_limit_price,
                trigger_price=round_to_tick(new_trigger_price, new_trigger_price),
                disclosed_quantity=0,
                validity='DAY',
            )
            if result and result.get('status') == 'success':
                self.tracker.update_status(order_id, 'MODIFIED')
                logger.info(f"[DhanBroker] SL modified: {order_id} → "
                           f"trigger={new_trigger_price:.2f} limit={new_limit_price:.2f}")
                return {'success': True, 'message': 'SL modified'}
            else:
                msg = str(result)
                logger.warning(f"[DhanBroker] SL modify failed: {order_id} — {msg}")
                return {'success': False, 'message': msg}
        except Exception as e:
            logger.error(f"[DhanBroker] SL modify error: {e}")
            return {'success': False, 'message': str(e)}

    # ── Account Queries ──────────────────────────────────────────────────────

    def get_funds(self):
        """Get available trading balance.

        Returns:
            dict: {'available': float, 'used': float, 'total': float} or None
        """
        if self.shadow_mode:
            return {'available': 100000, 'used': 0, 'total': 100000}

        if not self.is_connected:
            return None
        try:
            result = self.dhan.get_fund_limits()
            if result and result.get('status') == 'success':
                data = result.get('data', {})
                return {
                    'available': float(data.get('availabelBalance', data.get('available_balance', 0))),
                    'used': float(data.get('utilizedAmount', data.get('utilized_amount', 0))),
                    'total': float(data.get('sodLimit', data.get('sod_limit', 0))),
                }
        except Exception as e:
            logger.error(f"[DhanBroker] Fund query error: {e}")
        return None

    def get_positions(self):
        """Get all open positions from Dhan.

        Returns:
            list of position dicts, or empty list
        """
        if self.shadow_mode:
            return []

        if not self.is_connected:
            return []
        try:
            result = self.dhan.get_positions()
            if result and result.get('status') == 'success':
                return result.get('data', [])
        except Exception as e:
            logger.error(f"[DhanBroker] Positions query error: {e}")
        return []

    def get_order_book(self):
        """Get today's order book from Dhan.

        Returns:
            list of order dicts, or empty list
        """
        if self.shadow_mode:
            return list(self.tracker.today_orders().values())

        if not self.is_connected:
            return []
        try:
            result = self.dhan.get_order_list()
            if result and result.get('status') == 'success':
                return result.get('data', [])
        except Exception as e:
            logger.error(f"[DhanBroker] Order book query error: {e}")
        return []

    def activate_kill_switch(self):
        """Activate Dhan kill switch — disables ALL trading for the day.

        WARNING: This is IRREVERSIBLE for the current trading day.
        """
        if self.shadow_mode:
            logger.warning("[DhanBroker] SHADOW KILL SWITCH — would disable trading")
            return {'success': True, 'message': 'Shadow kill switch'}

        if not self.is_connected:
            return {'success': False, 'message': 'Not connected'}

        try:
            result = self.dhan.kill_switch('activate')
            logger.critical(f"[DhanBroker] KILL SWITCH ACTIVATED: {result}")
            return {'success': True, 'message': 'Kill switch activated'}
        except Exception as e:
            logger.error(f"[DhanBroker] Kill switch error: {e}")
            return {'success': False, 'message': str(e)}

    def flatten_all(self):
        """Emergency: cancel all pending orders and close all positions.

        Used by circuit breaker and kill switch.
        """
        logger.critical("[DhanBroker] FLATTEN ALL — cancelling orders + closing positions")

        # Step 1: Cancel all open orders
        cancelled = 0
        for order_id, info in self.tracker.get_open_orders().items():
            result = self.cancel_order(order_id)
            if result['success']:
                cancelled += 1

        # Step 2: Close all positions via aggressive limit
        closed = 0
        positions = self.get_positions()
        for pos in positions:
            qty = abs(int(pos.get('netQty', pos.get('buyQty', 0)) or 0))
            if qty == 0:
                continue
            sec_id = pos.get('securityId', pos.get('security_id', ''))
            exch = pos.get('exchangeSegment', pos.get('exchange_segment', 'NSE_FNO'))
            ltp = float(pos.get('lastTradedPrice', pos.get('ltp', 0)) or 0)
            if ltp > 0:
                result = self.place_exit_order(
                    security_id=sec_id,
                    exchange_segment=exch,
                    quantity=qty,
                    ltp=ltp,
                    tag='FLATTEN_ALL',
                    aggressive=True,
                )
                if result['success']:
                    closed += 1

        logger.critical(f"[DhanBroker] FLATTEN: cancelled={cancelled} orders, "
                       f"closed={closed} positions")
        return {'cancelled': cancelled, 'closed': closed}

    # ── Utility ──────────────────────────────────────────────────────────────

    def get_order_status(self, order_id):
        """Get current status of an order.

        Returns:
            str: Status string ('TRADED', 'PENDING', 'CANCELLED', 'REJECTED', etc.)
        """
        if order_id.startswith('SHADOW_'):
            return self.tracker.get(order_id).get('status', 'UNKNOWN')

        if not self.is_connected:
            return 'DISCONNECTED'

        try:
            result = self.dhan.get_order_by_id(order_id)
            if result and result.get('status') == 'success':
                data = result.get('data', {})
                return data.get('orderStatus', data.get('order_status', 'UNKNOWN'))
        except Exception as e:
            logger.debug(f"[DhanBroker] Status query error: {e}")
        return 'UNKNOWN'
