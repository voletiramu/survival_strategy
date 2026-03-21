"""
Angel SmartAPI WebSocket Real-Time Feed
========================================
Provides real-time LTP (Last Traded Price) via WebSocket streaming.
Replaces REST API polling for spot prices — instant updates, no rate limits.

Usage:
    feed = WebSocketFeed()
    feed.start(auth_token, api_key, client_code, feed_token)

    # Subscribe index + commodity tokens
    feed.subscribe_indices()         # NIFTY, BANKNIFTY, SENSEX
    feed.subscribe_mcx(mcx_tokens)   # GOLDM, SILVERM, CRUDEOILM futures tokens

    # Read cached LTP (thread-safe, instant)
    ltp = feed.get_ltp('99926000')   # NIFTY spot

    # Shutdown
    feed.stop()
"""

import threading
import time
import logging
import os

logger = logging.getLogger(__name__)

# Index token mapping (same as paper_trader.py _index_tokens)
INDEX_TOKENS = {
    'NIFTY':     {'exchange': 1, 'token': '99926000'},   # NSE_CM
    'BANKNIFTY': {'exchange': 1, 'token': '99926009'},   # NSE_CM
    'SENSEX':    {'exchange': 3, 'token': '99919000'},   # BSE_CM
}

# Exchange type codes (from SmartWebSocketV2)
NSE_CM = 1
NSE_FO = 2
BSE_CM = 3
MCX_FO = 5


class WebSocketFeed:
    """Thread-safe WebSocket price feed using Angel SmartAPI SmartWebSocketV2.

    Maintains a shared price cache that equity/commodity traders read from.
    Falls back gracefully if WebSocket disconnects — traders revert to REST API.
    """

    def __init__(self):
        self._prices = {}          # {token_str: ltp_float}
        self._lock = threading.Lock()
        self._connected = False
        self._started = False
        self._thread = None
        self.sws = None
        self._mcx_tokens = {}      # {commodity: token_str} — set after connect
        self._subscribed_tokens = []  # Track what we subscribed for logging
        self._tick_count = 0
        self._last_tick_time = 0
        # v15: Tick callback hooks for event-driven exit checking
        self._tick_callbacks = []   # List of callable(token, ltp) — called on every tick

    def start(self, auth_token, api_key, client_code, feed_token):
        """Start WebSocket connection in a daemon thread.

        Args:
            auth_token: JWT auth token from SmartConnect.generateSession()
            api_key: Angel API key
            client_code: Angel client code (e.g., 'V251861')
            feed_token: Feed token from SmartConnect.getfeedToken()
        """
        if self._started:
            logger.warning("[WS_FEED] Already started, skipping duplicate start()")
            return

        self._auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token

        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        except ImportError:
            logger.error("[WS_FEED] SmartApi package not installed. pip install smartapi-python")
            return

        self.sws = SmartWebSocketV2(
            auth_token=auth_token,
            api_key=api_key,
            client_code=client_code,
            feed_token=feed_token,
            max_retry_attempt=10,      # Retry up to 10 times on disconnect
            retry_strategy=1,          # Exponential backoff
            retry_delay=5,             # Start with 5s delay
            retry_multiplier=2,        # Double each retry
            retry_duration=480,        # Keep trying for 8 hours (full trading day)
        )

        # Set callbacks
        self.sws.on_open = self._on_open
        self.sws.on_data = self._on_data
        self.sws.on_error = self._on_error
        self.sws.on_close = self._on_close

        # Run WebSocket in daemon thread (connect() is blocking via run_forever)
        self._thread = threading.Thread(
            target=self._run_ws,
            name="WebSocketFeed",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        logger.info("[WS_FEED] WebSocket thread started")

        # Wait briefly for connection to establish
        time.sleep(3)

    def _run_ws(self):
        """WebSocket thread entry point."""
        try:
            logger.info("[WS_FEED] Connecting to Angel WebSocket...")
            self.sws.connect()  # Blocking — runs until close_connection()
        except Exception as e:
            logger.error(f"[WS_FEED] WebSocket thread error: {e}")
            self._connected = False

    def _on_open(self, wsapp):
        """Called when WebSocket connection is established — subscribe to tokens."""
        self._connected = True
        logger.info("[WS_FEED] WebSocket CONNECTED")

        # Subscribe to index tokens (NSE + BSE)
        self._subscribe_indices()

        # Subscribe to MCX tokens if available
        if self._mcx_tokens:
            self._subscribe_mcx()

    def _subscribe_indices(self):
        """Subscribe to NIFTY, BANKNIFTY (NSE) and SENSEX (BSE) LTP."""
        try:
            nse_tokens = [INDEX_TOKENS['NIFTY']['token'], INDEX_TOKENS['BANKNIFTY']['token']]
            bse_tokens = [INDEX_TOKENS['SENSEX']['token']]

            token_list = [
                {"exchangeType": NSE_CM, "tokens": nse_tokens},
                {"exchangeType": BSE_CM, "tokens": bse_tokens},
            ]

            self.sws.subscribe("idx_stream", 1, token_list)  # mode=1 (LTP)
            self._subscribed_tokens.extend(nse_tokens + bse_tokens)
            logger.info(f"[WS_FEED] Subscribed to indices: NIFTY, BANKNIFTY, SENSEX ({len(nse_tokens)+len(bse_tokens)} tokens)")
        except Exception as e:
            logger.error(f"[WS_FEED] Index subscription error: {e}")

    def _subscribe_mcx(self):
        """Subscribe to MCX commodity futures tokens."""
        try:
            mcx_token_list = list(self._mcx_tokens.values())
            if not mcx_token_list:
                return

            token_list = [
                {"exchangeType": MCX_FO, "tokens": mcx_token_list},
            ]

            self.sws.subscribe("mcx_stream", 1, token_list)  # mode=1 (LTP)
            self._subscribed_tokens.extend(mcx_token_list)
            names = list(self._mcx_tokens.keys())
            logger.info(f"[WS_FEED] Subscribed to MCX: {', '.join(names)} ({len(mcx_token_list)} tokens)")
        except Exception as e:
            logger.error(f"[WS_FEED] MCX subscription error: {e}")

    def set_mcx_tokens(self, mcx_tokens_dict):
        """Set MCX futures tokens (called after commodity trader loads instruments).

        Args:
            mcx_tokens_dict: dict like {'GOLDM': '234230', 'SILVERM': '234220', ...}
        """
        self._mcx_tokens = mcx_tokens_dict
        logger.info(f"[WS_FEED] MCX tokens set: {mcx_tokens_dict}")

        # If already connected, subscribe immediately
        if self._connected and self.sws:
            self._subscribe_mcx()

    def subscribe_mcx_options(self, option_tokens):
        """v10.5: Subscribe to MCX option tokens for real-time LTP.

        Called dynamically when a commodity option trade is entered, so we get
        real-time exit price updates via WebSocket instead of REST API polling.

        Args:
            option_tokens: list of token strings (e.g., ['234567', '234568'])
        """
        if not option_tokens:
            return
        if not self.sws or not self._connected:
            logger.warning(f"[WS_FEED] Cannot subscribe MCX options — not connected")
            return
        try:
            # Filter out tokens already subscribed
            new_tokens = [t for t in option_tokens if str(t) not in self._subscribed_tokens]
            if not new_tokens:
                return

            token_list = [{"exchangeType": MCX_FO, "tokens": [str(t) for t in new_tokens]}]
            self.sws.subscribe("mcx_opt_stream", 1, token_list)  # mode=1 (LTP)
            self._subscribed_tokens.extend([str(t) for t in new_tokens])
            logger.info(f"[WS_FEED] MCX options subscribed: {len(new_tokens)} tokens ({new_tokens})")
        except Exception as e:
            logger.error(f"[WS_FEED] MCX option subscribe failed: {e}")

    def subscribe_equity_options(self, option_tokens):
        """v15: Subscribe to equity option tokens (NSE_FO) for real-time premium ticks.

        Called by PaperTrader when a position is opened, so we get
        real-time exit price updates via WebSocket instead of REST API polling.

        Args:
            option_tokens: list of token strings (e.g., ['45678', '45679'])
        """
        if not option_tokens:
            return
        if not self.sws or not self._connected:
            logger.warning(f"[WS_FEED] Cannot subscribe equity options — not connected")
            return
        try:
            new_tokens = [str(t) for t in option_tokens if str(t) not in self._subscribed_tokens]
            if not new_tokens:
                return

            token_list = [{"exchangeType": NSE_FO, "tokens": new_tokens}]
            self.sws.subscribe("nfo_opt_stream", 1, token_list)  # mode=1 (LTP)
            self._subscribed_tokens.extend(new_tokens)
            logger.info(f"[WS_FEED] Equity options subscribed: {len(new_tokens)} tokens ({new_tokens[:5]}...)")
        except Exception as e:
            logger.error(f"[WS_FEED] Equity option subscribe failed: {e}")

    def subscribe_bse_options(self, option_tokens):
        """v15: Subscribe to BSE option tokens (SENSEX options) for real-time ticks."""
        if not option_tokens:
            return
        if not self.sws or not self._connected:
            return
        try:
            new_tokens = [str(t) for t in option_tokens if str(t) not in self._subscribed_tokens]
            if not new_tokens:
                return
            token_list = [{"exchangeType": BSE_CM, "tokens": new_tokens}]
            self.sws.subscribe("bfo_opt_stream", 1, token_list)
            self._subscribed_tokens.extend(new_tokens)
            logger.info(f"[WS_FEED] BSE options subscribed: {len(new_tokens)} tokens")
        except Exception as e:
            logger.error(f"[WS_FEED] BSE option subscribe failed: {e}")

    def register_tick_callback(self, callback_fn):
        """v15: Register a callback to be called on every price tick.

        The callback receives (token: str, ltp: float) and should be fast
        (no heavy computation or I/O). Used for event-driven exit checking.

        Args:
            callback_fn: callable(token, ltp)
        """
        self._tick_callbacks.append(callback_fn)
        logger.info(f"[WS_FEED] Tick callback registered (total: {len(self._tick_callbacks)})")

    def _on_data(self, wsapp, data):
        """Called on each price tick — update shared cache + fire callbacks.

        Angel WebSocket sends prices in paisa (1/100 rupees) for NSE/BSE.
        MCX prices are also in paisa in the binary feed.
        v15: Added tick callback invocation for event-driven exits.
        """
        try:
            token = str(data.get('token', '')).strip()
            if not token:
                return

            exchange_type = data.get('exchange_type', 0)
            raw_ltp = data.get('last_traded_price', 0)

            # Angel sends LTP in paisa for NSE/BSE (divide by 100)
            # MCX prices are also in paisa in the binary feed
            ltp = raw_ltp / 100.0

            if ltp <= 0:
                return

            with self._lock:
                self._prices[token] = ltp

            self._tick_count += 1

            # v15: Fire tick callbacks for event-driven exit checking
            for cb in self._tick_callbacks:
                try:
                    cb(token, ltp)
                except Exception as cb_err:
                    logger.debug(f"[WS_FEED] Tick callback error: {cb_err}")

            # Log every 500th tick (was 100 — reduced for 1s scan loop)
            if self._tick_count % 500 == 1:
                logger.info(f"[WS_FEED] Tick #{self._tick_count}: token={token} LTP={ltp:.2f} (exchange={exchange_type})")

        except Exception as e:
            logger.error(f"[WS_FEED] on_data error: {e}")

    def _on_error(self, wsapp, error):
        """Called on WebSocket error."""
        self._connected = False
        logger.error(f"[WS_FEED] WebSocket ERROR: {error}")
        try:
            from trade_notifier import notify_websocket_issue
            notify_websocket_issue('error', str(error)[:200])
        except Exception:
            pass

    def _on_close(self, wsapp, *args):
        """Called when WebSocket disconnects.
        v7.7: Accept *args to handle SmartAPI library signature change
        (passes close_status_code, close_msg as extra args).
        """
        self._connected = False
        close_code = args[0] if args else None
        logger.warning(f"[WS_FEED] WebSocket DISCONNECTED (code={close_code})")
        try:
            from trade_notifier import notify_websocket_issue
            notify_websocket_issue('disconnected', f"code={close_code}")
        except Exception:
            pass

    def get_ltp(self, token):
        """Get cached LTP for a token. Thread-safe, instant (no API call).

        Args:
            token: Token string (e.g., '99926000' for NIFTY)

        Returns:
            float: LTP in rupees, or None if not available
        """
        with self._lock:
            return self._prices.get(str(token))

    @property
    def is_connected(self):
        """Check if WebSocket is currently connected."""
        return self._connected

    @property
    def cached_count(self):
        """Number of tokens with cached prices."""
        with self._lock:
            return len(self._prices)

    def get_status(self):
        """Get feed status for logging/diagnostics."""
        with self._lock:
            prices_copy = dict(self._prices)
        return {
            'connected': self._connected,
            'started': self._started,
            'ticks_received': self._tick_count,
            'tokens_cached': len(prices_copy),
            'subscribed_tokens': len(self._subscribed_tokens),
            'prices': prices_copy,
        }

    def stop(self):
        """Gracefully shut down WebSocket connection."""
        logger.info("[WS_FEED] Shutting down WebSocket feed...")
        self._started = False
        if self.sws:
            try:
                self.sws.close_connection()
            except Exception as e:
                logger.warning(f"[WS_FEED] Error closing WebSocket: {e}")
        self._connected = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[WS_FEED] WebSocket feed stopped")


def create_ws_feed_from_creds(cred_file=None):
    """Helper: Create and start a WebSocket feed using Angel credentials.

    Returns (feed, smart_connect_obj) — the SmartConnect obj can be reused for REST calls.
    Returns (None, None) if connection fails.
    """
    try:
        from SmartApi import SmartConnect
        import pyotp
    except ImportError:
        logger.error("[WS_FEED] Install: pip install smartapi-python pyotp")
        return None, None

    if cred_file is None:
        cred_file = os.environ.get(
            'ANGEL_CRED_FILE',
            os.path.join(os.path.dirname(__file__), '..', 'Angel',
                         'ANGEL_API_KEY=your_api_key.txt')
        )

    # Parse credentials (same logic as AngelConnection.load_credentials)
    creds = {}
    current_app = {}
    try:
        with open(cred_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    raw_val = val.strip()
                    if key == 'ANGEL_TOTP_KEY' and '#' in raw_val:
                        parts = raw_val.split('#')
                        if 'your_' in parts[0].strip().lower():
                            val = parts[-1].strip()
                        else:
                            val = parts[0].strip()
                    else:
                        val = raw_val.split('#')[0].strip()
                    current_app[key] = val
                    if key == 'BROKER_NAME':
                        app_type = current_app.get('ANGEL_APP_TYPE', 'Unknown')
                        creds[app_type] = current_app.copy()
                        current_app = {}
    except Exception as e:
        logger.error(f"[WS_FEED] Failed to read credentials: {e}")
        return None, None

    # Use 'Historical' app type (or first available)
    app = creds.get('Historical', creds.get('Market', {}))
    if not app:
        app = list(creds.values())[0] if creds else {}

    api_key = app.get('ANGEL_API_KEY', '')
    client = app.get('ANGEL_CLIENT_CODE', '')
    pin = app.get('ANGEL_PIN', '')
    totp_secret = app.get('ANGEL_TOTP_KEY', '')

    if not all([api_key, client, pin, totp_secret]):
        logger.error("[WS_FEED] Missing Angel credentials")
        return None, None

    # Login to get auth + feed tokens
    try:
        obj = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        session = obj.generateSession(client, pin, totp)

        if not session or not session.get('status'):
            logger.error(f"[WS_FEED] Angel login failed: {session}")
            return None, None

        auth_token = session['data']['jwtToken']
        feed_token = obj.getfeedToken()

        logger.info(f"[WS_FEED] Angel authenticated: {client}")

        # Create and start feed
        feed = WebSocketFeed()
        feed.start(auth_token, api_key, client, feed_token)

        return feed, obj

    except Exception as e:
        logger.error(f"[WS_FEED] Authentication error: {e}")
        import traceback
        traceback.print_exc()
        return None, None
