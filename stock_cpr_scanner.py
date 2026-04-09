"""Stock CPR Scanner -- Daily narrow CPR stock discovery.

Scans all ~180 F&O stocks at 8:30 AM before market opens, computes CPR width
for each stock using previous day's OHLC, identifies breakout candidates with
narrow CPR, scores them, and selects the best stocks for trading within
available capital.

Usage:
    # As module (called from paper_trader or scheduler):
    from stock_cpr_scanner import StockCPRScanner
    scanner = StockCPRScanner(angel_connection=angel)
    watchlist = scanner.get_todays_watchlist()

    # Standalone:
    python stock_cpr_scanner.py              # Run scan now
    python stock_cpr_scanner.py --history 5  # Show recurring CPR over last 5 days
"""

import os
import sys
import json
import logging
import time
from datetime import datetime, timedelta
from collections import Counter

import pandas as pd

# Import from our shared strategy library
from strategies.indicators import compute_cpr, compute_camarilla
from stock_fno_config import StockFnOConfig, get_config

logger = logging.getLogger('StockCPRScanner')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_DIR = os.path.join(BASE_DIR, 'paper_trades_stocks')

# ====================================================================
# SCANNER CONFIGURATION
# ====================================================================
CPR_WIDTH_THRESHOLD = 0.50       # Max CPR width (%) to qualify as narrow
MAX_STOCKS_PER_DAY = 5           # Max stocks to select for trading
MAX_PER_SECTOR = 2               # Max stocks from the same sector
DEFAULT_CAPITAL = 200000         # Rs 2L default capital budget
MIN_SCORE_THRESHOLD = 40         # Minimum score to include in results
RATE_LIMIT_SLEEP = 0.35          # Seconds between API calls
RECURRING_BONUS_DAYS = 5         # Lookback window for recurring CPR bonus
API_RETRY_MAX = 2                # Max retries for a single symbol's OHLC fetch
API_RETRY_DELAY = 2.0            # Seconds to wait before retrying a failed fetch


# ====================================================================
# HELPER: Previous trading day calculation
# ====================================================================
def _get_previous_trading_day(ref_date=None):
    """Return the previous trading day (skip weekends).

    Does NOT account for NSE holidays -- a holiday calendar can be added
    later.  For now, the API simply returns no data on holidays and
    the scanner gracefully skips those symbols.

    Args:
        ref_date: Reference date (default: today).

    Returns:
        datetime.date of the previous trading day.
    """
    if ref_date is None:
        ref_date = datetime.now().date()
    elif isinstance(ref_date, datetime):
        ref_date = ref_date.date()

    day = ref_date - timedelta(days=1)
    # Skip weekends: Saturday=5, Sunday=6
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _format_angel_date(d, time_str='09:15'):
    """Format a date for Angel API's fromdate/todate parameters.

    Args:
        d: date or datetime object.
        time_str: Time component (HH:MM).

    Returns:
        String like '2026-03-06 09:15'.
    """
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.strftime('%Y-%m-%d')} {time_str}"


# ====================================================================
# MAIN CLASS
# ====================================================================
class StockCPRScanner:
    """Scans all F&O stocks for narrow CPR and selects trading candidates.

    Workflow:
        1. Fetch previous day OHLC for every F&O stock via Angel One API.
        2. Compute CPR (Pivot, TC, BC, Width) and Camarilla (R3/R4/S3/S4).
        3. Filter stocks with CPR width < 0.5%.
        4. Score each stock using StockFnOConfig.compute_stock_score().
        5. Sort by score descending.
        6. Select top N stocks that fit within available capital.
        7. Save results to JSON and send Telegram notification.
    """

    def __init__(self, angel_connection=None):
        """Initialize scanner.

        Args:
            angel_connection: An AngelConnection instance (from paper_trader.py).
                              If None, scanner will attempt to create its own.
        """
        self.angel = angel_connection
        self.fno_config = get_config()
        self.scan_results = {}       # {date_str: scan_data_dict}
        self._ohlc_cache = {}        # {symbol: {date_str: ohlc_dict}}
        self._today_watchlist = None  # Cached watchlist for today

        self.scan_dir = SCAN_DIR
        os.makedirs(self.scan_dir, exist_ok=True)

    # ================================================================
    # OHLC FETCHING
    # ================================================================

    def fetch_previous_day_ohlc(self, symbol, token=None, exchange='NSE'):
        """Fetch previous trading day's OHLC from Angel One API.

        Uses ONE_DAY candle for the last 5 trading days and takes the most
        recent valid candle.  This guards against holidays where no data
        would be returned for a single day request.

        Args:
            symbol: Stock symbol (e.g., 'SBIN').
            token: Angel One symbol token.  If None, looked up from fno_config.
            exchange: Exchange segment (default 'NSE').

        Returns:
            dict with keys {open, high, low, close, volume, date} or None on error.
        """
        if self.angel is None:
            logger.warning("No Angel connection -- cannot fetch OHLC")
            return None

        # Resolve token and exchange from config if not provided
        if token is None:
            token = self.fno_config.get_spot_token(symbol)
        if token is None:
            token = self.fno_config.get_token(symbol)
        if token is None:
            logger.debug(f"No token found for {symbol}, skipping")
            return None

        exchange = self.fno_config.get_spot_exchange(symbol) or exchange

        # Check cache first
        prev_day = _get_previous_trading_day()
        prev_day_str = prev_day.strftime('%Y-%m-%d')
        if symbol in self._ohlc_cache and prev_day_str in self._ohlc_cache[symbol]:
            return self._ohlc_cache[symbol][prev_day_str]

        # Fetch last 5 days of daily candles
        to_date = _format_angel_date(prev_day, '15:30')
        from_date = _format_angel_date(prev_day - timedelta(days=7), '09:15')

        candles = None
        for attempt in range(API_RETRY_MAX):
            try:
                candles = self.angel.get_historical(
                    exchange, token, 'ONE_DAY', from_date, to_date
                )
                if candles:
                    break
            except Exception as e:
                logger.debug(f"OHLC fetch error for {symbol} (attempt {attempt+1}): {e}")

            if attempt < API_RETRY_MAX - 1:
                time.sleep(API_RETRY_DELAY)

        if not candles or len(candles) == 0:
            logger.debug(f"No OHLC data returned for {symbol}")
            return None

        # Angel API candle format: [timestamp, open, high, low, close, volume]
        # Take the most recent candle (last element)
        last_candle = candles[-1]
        try:
            ohlc = {
                'date': last_candle[0] if isinstance(last_candle[0], str) else str(last_candle[0]),
                'open': float(last_candle[1]),
                'high': float(last_candle[2]),
                'low': float(last_candle[3]),
                'close': float(last_candle[4]),
                'volume': int(last_candle[5]) if len(last_candle) > 5 else 0,
            }
        except (IndexError, ValueError, TypeError) as e:
            logger.warning(f"Bad candle data for {symbol}: {last_candle} -- {e}")
            return None

        # Validate: prices must be positive
        if ohlc['high'] <= 0 or ohlc['low'] <= 0 or ohlc['close'] <= 0:
            logger.warning(f"Invalid OHLC prices for {symbol}: {ohlc}")
            return None

        # Cache it
        self._ohlc_cache.setdefault(symbol, {})[prev_day_str] = ohlc
        return ohlc

    # ================================================================
    # CPR COMPUTATION FOR A SINGLE STOCK
    # ================================================================

    def _compute_stock_cpr(self, symbol, ohlc, recurring_days=1,
                           existing_sectors=None, available_capital=DEFAULT_CAPITAL):
        """Compute CPR, Camarilla, and suitability score for one stock.

        Args:
            symbol: Stock symbol.
            ohlc: dict with {open, high, low, close, volume, date}.
            recurring_days: Number of consecutive days with narrow CPR.
            existing_sectors: Set of sectors already selected (for diversity).
            available_capital: Remaining capital budget.

        Returns:
            dict with all computed data, or None if data is invalid.
        """
        prev_high = ohlc['high']
        prev_low = ohlc['low']
        prev_close = ohlc['close']

        # --- CPR ---
        cpr = compute_cpr(prev_high, prev_low, prev_close)
        pivot = round(cpr['pivot'], 2)
        tc = round(cpr['tc'], 2)
        bc = round(cpr['bc'], 2)
        cpr_width = round(cpr['cpr_width'], 4)

        # --- Camarilla ---
        cam = compute_camarilla(prev_high, prev_low, prev_close)
        cam_r3 = round(cam['cam_r3'], 2)
        cam_r4 = round(cam['cam_r4'], 2)
        cam_s3 = round(cam['cam_s3'], 2)
        cam_s4 = round(cam['cam_s4'], 2)

        # --- Stock metadata ---
        info = self.fno_config.get_stock_info(symbol)
        lot_size = info['lot_size'] if info else 1
        strike_interval = info['strike_interval'] if info else 1.0
        liquidity = self.fno_config.get_liquidity_tier(symbol)
        sector = self.fno_config.get_sector(symbol)
        tier = self.fno_config.get_tier(symbol)

        # --- Estimate ATM premium cost per lot ---
        # Rough: ATM premium ~ 1.5% of spot * lot_size
        est_premium = round(prev_close * 0.015 * lot_size, 0)

        # --- Score using fno_config ---
        score_result = self.fno_config.compute_stock_score(
            symbol=symbol,
            cpr_width=cpr_width,
            recurring_days=recurring_days,
            existing_sectors=existing_sectors,
            available_capital=available_capital,
        )
        total_score = score_result.get('total_score', 0)

        # Build score breakdown from components
        components = score_result.get('components', {})
        score_breakdown = {
            'cpr_narrowness': components.get('cpr_narrowness', {}).get('score', 0),
            'option_liquidity': components.get('option_liquidity', {}).get('score', 0),
            'capital_fit': components.get('capital_fit', {}).get('score', 0),
            'recurring_cpr': components.get('recurring_cpr', {}).get('score', 0),
            'sector_diversity': components.get('sector_diversity', {}).get('score', 0),
        }

        return {
            'symbol': symbol,
            'score': total_score,
            'cpr_width': cpr_width,
            'pivot': pivot,
            'tc': tc,
            'bc': bc,
            'cam_r3': cam_r3,
            'cam_r4': cam_r4,
            'cam_s3': cam_s3,
            'cam_s4': cam_s4,
            'prev_open': round(ohlc['open'], 2),
            'prev_high': round(prev_high, 2),
            'prev_low': round(prev_low, 2),
            'prev_close': round(prev_close, 2),
            'prev_volume': ohlc.get('volume', 0),
            'lot_size': lot_size,
            'strike_interval': strike_interval,
            'liquidity': liquidity,
            'sector': sector,
            'tier': tier,
            'recurring_days': recurring_days,
            'est_premium': est_premium,
            'selected': False,  # Will be set later by select_tradeable_stocks
            'score_breakdown': score_breakdown,
            'tradeable': score_result.get('tradeable', False),
        }

    # ================================================================
    # MAIN SCANNER
    # ================================================================

    def scan_all_fno_stocks(self, available_capital=DEFAULT_CAPITAL):
        """Scan all F&O stocks for narrow CPR.

        Steps:
            1. Get all tradeable symbols from fno_config.
            2. Load recurring CPR history for bonus scoring.
            3. Fetch OHLC for each symbol (with rate limiting).
            4. Compute CPR, Camarilla, and score.
            5. Filter: CPR width < threshold.
            6. Sort by score descending.
            7. Save results to JSON.
            8. Return the full scan result dict.

        Args:
            available_capital: Capital budget for stock selection (default Rs 2L).

        Returns:
            dict with scan metadata and list of narrow CPR stocks.
        """
        scan_start = datetime.now()
        today_str = scan_start.strftime('%Y-%m-%d')

        logger.info(f"=== Stock CPR Scanner starting at {scan_start.strftime('%H:%M:%S')} ===")
        logger.info(f"F&O config: {len(self.fno_config)} total stocks, "
                     f"{len(self.fno_config.get_tradeable_symbols())} tradeable")

        # Get all symbols to scan (use get_all_fno_symbols to scan everything,
        # including lower-tier stocks that might have narrow CPR)
        all_symbols = self.fno_config.get_all_fno_symbols()
        logger.info(f"Scanning {len(all_symbols)} F&O stocks for narrow CPR...")

        # Load recurring CPR data for bonus scoring
        recurring_map = self._build_recurring_map(days=RECURRING_BONUS_DAYS)

        # Scan each stock
        all_computed = []
        errors = []
        skipped = 0

        for i, symbol in enumerate(all_symbols):
            # Log progress every 25 stocks
            if (i + 1) % 25 == 0:
                logger.info(f"  Progress: {i+1}/{len(all_symbols)} scanned, "
                            f"{len(all_computed)} computed, {len(errors)} errors")

            try:
                ohlc = self.fetch_previous_day_ohlc(symbol)
                if ohlc is None:
                    skipped += 1
                    continue

                recurring_days = recurring_map.get(symbol, 1)

                result = self._compute_stock_cpr(
                    symbol=symbol,
                    ohlc=ohlc,
                    recurring_days=recurring_days,
                    available_capital=available_capital,
                )
                if result is not None:
                    all_computed.append(result)

            except Exception as e:
                errors.append({'symbol': symbol, 'error': str(e)})
                logger.warning(f"Error scanning {symbol}: {e}")

            # Rate limiting between API calls
            time.sleep(RATE_LIMIT_SLEEP)

        # --- Filter: narrow CPR only ---
        narrow_cpr = [
            s for s in all_computed
            if s['cpr_width'] < CPR_WIDTH_THRESHOLD
        ]

        # --- Sort by score descending ---
        narrow_cpr.sort(key=lambda x: (-x['score'], x.get('tier', 4)))

        scan_end = datetime.now()
        elapsed = (scan_end - scan_start).total_seconds()

        # Build scan result
        scan_data = {
            'date': today_str,
            'scan_time': scan_start.strftime('%H:%M:%S'),
            'scan_elapsed_seconds': round(elapsed, 1),
            'total_scanned': len(all_computed),
            'total_symbols': len(all_symbols),
            'skipped': skipped,
            'errors': len(errors),
            'narrow_cpr_count': len(narrow_cpr),
            'cpr_width_threshold': CPR_WIDTH_THRESHOLD,
            'selected_for_trading': 0,  # Updated after selection
            'stocks': narrow_cpr,
            'error_details': errors[:10],  # Keep first 10 errors for debugging
        }

        # Cache the result
        self.scan_results[today_str] = scan_data

        logger.info(f"=== Scan complete in {elapsed:.1f}s ===")
        logger.info(f"  Scanned: {len(all_computed)} | "
                     f"Narrow CPR (<{CPR_WIDTH_THRESHOLD}%): {len(narrow_cpr)} | "
                     f"Errors: {len(errors)} | Skipped: {skipped}")

        if narrow_cpr:
            logger.info(f"  Top 5 narrow CPR stocks:")
            for s in narrow_cpr[:5]:
                logger.info(f"    {s['symbol']:12s} CPR={s['cpr_width']:.3f}%  "
                            f"Score={s['score']}  Sector={s['sector']}")

        # Save to disk
        self.save_scan_results(scan_data)

        return scan_data

    # ================================================================
    # STOCK SELECTION (Capital-Aware)
    # ================================================================

    def select_tradeable_stocks(self, available_capital=DEFAULT_CAPITAL, scan_data=None):
        """From today's scan, pick stocks that fit within capital budget.

        Algorithm:
            1. Start with scan results sorted by score descending.
            2. Iterate through stocks, checking:
               a. Estimated premium cost fits remaining capital.
               b. Max 2 stocks from the same sector.
               c. Stock must be tradeable per fno_config.
            3. Stop after MAX_STOCKS_PER_DAY or capital is exhausted.

        Args:
            available_capital: Total capital budget (default Rs 2L).
            scan_data: Scan result dict. If None, uses today's cached scan.

        Returns:
            list of selected stock dicts (subset of scan_data['stocks']).
        """
        if scan_data is None:
            today_str = datetime.now().strftime('%Y-%m-%d')
            scan_data = self.scan_results.get(today_str)
            if scan_data is None:
                scan_data = self._load_scan(today_str)
            if scan_data is None:
                logger.warning("No scan data available for selection")
                return []

        stocks = scan_data.get('stocks', [])
        if not stocks:
            logger.info("No narrow CPR stocks found for selection")
            return []

        selected = []
        remaining_capital = available_capital
        sector_count = Counter()

        for stock in stocks:
            # Already have enough
            if len(selected) >= MAX_STOCKS_PER_DAY:
                break

            # Must be tradeable
            if not stock.get('tradeable', True):
                continue

            # Sector diversity: max 2 per sector
            sector = stock.get('sector', 'UNKNOWN')
            if sector_count[sector] >= MAX_PER_SECTOR:
                logger.debug(f"Skipping {stock['symbol']}: sector {sector} already has "
                             f"{sector_count[sector]} stocks")
                continue

            # Capital check: estimated premium must fit
            est_premium = stock.get('est_premium', 0)
            if est_premium <= 0:
                # Fallback: rough estimate from lot_size * strike_interval
                lot_size = stock.get('lot_size', 1)
                strike_interval = stock.get('strike_interval', 1)
                est_premium = strike_interval * 100 * 0.015 * lot_size

            if est_premium > remaining_capital:
                logger.debug(f"Skipping {stock['symbol']}: est_premium Rs {est_premium:.0f} "
                             f"> remaining Rs {remaining_capital:.0f}")
                continue

            # Minimum score gate
            if stock.get('score', 0) < MIN_SCORE_THRESHOLD:
                continue

            # Select this stock
            stock['selected'] = True
            selected.append(stock)
            remaining_capital -= est_premium
            sector_count[sector] += 1

            logger.info(f"  Selected: {stock['symbol']:12s} Score={stock['score']}  "
                        f"CPR={stock['cpr_width']:.3f}%  Sector={sector}  "
                        f"EstPremium=Rs {est_premium:.0f}  "
                        f"Remaining=Rs {remaining_capital:.0f}")

        # Update scan_data
        scan_data['selected_for_trading'] = len(selected)

        # Re-save with selection info
        self.save_scan_results(scan_data)

        logger.info(f"Selected {len(selected)} stocks for trading "
                    f"(capital used: Rs {available_capital - remaining_capital:.0f} "
                    f"of Rs {available_capital:.0f})")

        return selected

    # ================================================================
    # WATCHLIST (HIGH-LEVEL API)
    # ================================================================

    def get_todays_watchlist(self, force_rescan=False, available_capital=DEFAULT_CAPITAL):
        """Return today's watchlist of selected stocks.

        If a scan has already been run today and cached, uses it unless
        force_rescan=True.  Otherwise runs a fresh scan.

        Args:
            force_rescan: Force a new scan even if today's results exist.
            available_capital: Capital budget for selection.

        Returns:
            list of selected stock dicts.
        """
        today_str = datetime.now().strftime('%Y-%m-%d')

        # Return cached if available
        if not force_rescan and self._today_watchlist is not None:
            return self._today_watchlist

        # Check if today's scan already exists on disk
        if not force_rescan:
            scan_data = self._load_scan(today_str)
            if scan_data is not None:
                logger.info(f"Loaded existing scan for {today_str} from disk")
                self.scan_results[today_str] = scan_data
                selected = [s for s in scan_data.get('stocks', []) if s.get('selected')]
                if selected:
                    self._today_watchlist = selected
                    return selected
                # If no stocks were selected yet, run selection
                selected = self.select_tradeable_stocks(available_capital, scan_data)
                self._today_watchlist = selected
                return selected

        # Run fresh scan
        scan_data = self.scan_all_fno_stocks(available_capital)
        selected = self.select_tradeable_stocks(available_capital, scan_data)
        self._today_watchlist = selected

        return selected

    # ================================================================
    # RECURRING CPR ANALYSIS
    # ================================================================

    def get_recurring_narrow_cpr(self, days=5):
        """Find stocks that had narrow CPR on multiple recent trading days.

        Loads last N days of scan results from disk, counts how many days
        each symbol appeared with narrow CPR.

        Args:
            days: Number of past trading days to look back.

        Returns:
            dict mapping symbol -> number of days with narrow CPR,
            sorted by count descending.
        """
        symbol_days = Counter()
        symbol_details = {}  # symbol -> list of {date, cpr_width}
        ref_date = datetime.now().date()

        loaded = 0
        checked = 0
        d = ref_date - timedelta(days=1)

        while loaded < days and checked < days + 10:
            # Skip weekends
            if d.weekday() < 5:
                date_str = d.strftime('%Y-%m-%d')
                scan = self._load_scan(date_str)
                if scan is not None:
                    for stock in scan.get('stocks', []):
                        sym = stock['symbol']
                        symbol_days[sym] += 1
                        symbol_details.setdefault(sym, []).append({
                            'date': date_str,
                            'cpr_width': stock.get('cpr_width', 0),
                            'score': stock.get('score', 0),
                        })
                    loaded += 1
                    logger.debug(f"Loaded scan for {date_str}: "
                                 f"{len(scan.get('stocks', []))} narrow CPR stocks")
            checked += 1
            d -= timedelta(days=1)

        logger.info(f"Recurring CPR analysis: loaded {loaded} days, "
                    f"{len(symbol_days)} unique symbols appeared")

        # Sort by frequency
        recurring = dict(symbol_days.most_common())

        # Log top recurring
        for sym, count in list(recurring.items())[:10]:
            if count >= 2:
                logger.info(f"  Recurring: {sym:12s} appeared {count}/{loaded} days")

        return recurring

    def _build_recurring_map(self, days=5):
        """Build a symbol -> recurring_days map from historical scans.

        Used internally during scan_all_fno_stocks to award recurring
        CPR bonus points.

        Args:
            days: Number of past trading days to look back.

        Returns:
            dict {symbol: int} with count of days symbol had narrow CPR.
        """
        recurring = self.get_recurring_narrow_cpr(days=days)
        # Every symbol gets at least 1 (today counts as 1)
        return {sym: count + 1 for sym, count in recurring.items()}

    # ================================================================
    # FILE I/O
    # ================================================================

    def _load_scan(self, date_str):
        """Load scan results from JSON file on disk.

        Args:
            date_str: Date string in YYYY-MM-DD format.

        Returns:
            Scan data dict, or None if file does not exist.
        """
        # Check in-memory cache first
        if date_str in self.scan_results:
            return self.scan_results[date_str]

        filepath = os.path.join(self.scan_dir, f'cpr_scan_{date_str}.json')
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.scan_results[date_str] = data
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load scan {filepath}: {e}")
            return None

    def save_scan_results(self, results):
        """Save scan results to paper_trades_stocks/cpr_scan_{date}.json.

        Args:
            results: Scan data dict (must contain 'date' key).
        """
        date_str = results.get('date', datetime.now().strftime('%Y-%m-%d'))
        filepath = os.path.join(self.scan_dir, f'cpr_scan_{date_str}.json')

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"Scan results saved to {filepath}")
        except IOError as e:
            logger.error(f"Failed to save scan results: {e}")

    # ================================================================
    # TELEGRAM NOTIFICATION
    # ================================================================

    def send_watchlist_notification(self, watchlist):
        """Send Telegram alert with today's CPR watchlist.

        Formats a concise message with the selected stocks, their
        CPR widths, scores, and key levels.

        Args:
            watchlist: list of selected stock dicts.

        Returns:
            True if message sent successfully, False otherwise.
        """
        try:
            from trade_notifier import send_message
        except ImportError:
            logger.warning("trade_notifier not available -- skipping Telegram alert")
            return False

        if not watchlist:
            # Still send a notification that scan found nothing
            today_str = datetime.now().strftime('%Y-%m-%d')
            scan_data = self.scan_results.get(today_str, {})
            msg = (
                f"<b>CPR STOCK SCANNER</b>\n"
                f"<b>Date:</b> {today_str}\n"
                f"No stocks with narrow CPR (<{CPR_WIDTH_THRESHOLD}%) found today.\n"
                f"Scanned: {scan_data.get('total_scanned', 0)} stocks"
            )
            return send_message(msg)

        today_str = datetime.now().strftime('%Y-%m-%d')
        scan_data = self.scan_results.get(today_str, {})

        # Header
        lines = [
            f"<b>CPR STOCK SCANNER</b>",
            f"<b>Date:</b> {today_str}",
            f"<b>Scanned:</b> {scan_data.get('total_scanned', 0)} | "
            f"<b>Narrow CPR:</b> {scan_data.get('narrow_cpr_count', 0)} | "
            f"<b>Selected:</b> {len(watchlist)}",
            f"",
        ]

        # Stock details
        for i, stock in enumerate(watchlist, 1):
            recurring_tag = ""
            if stock.get('recurring_days', 1) >= 2:
                recurring_tag = f" [{stock['recurring_days']}d]"

            lines.append(
                f"<b>{i}. {stock['symbol']}</b> "
                f"(Score: {stock['score']}, CPR: {stock['cpr_width']:.2f}%){recurring_tag}"
            )
            lines.append(
                f"   P={stock['pivot']:.1f} | TC={stock['tc']:.1f} | BC={stock['bc']:.1f}"
            )
            lines.append(
                f"   R3={stock['cam_r3']:.1f} R4={stock['cam_r4']:.1f} | "
                f"S3={stock['cam_s3']:.1f} S4={stock['cam_s4']:.1f}"
            )
            lines.append(
                f"   Close={stock['prev_close']:.1f} | "
                f"Lot={stock['lot_size']} | "
                f"Est=Rs {stock['est_premium']:.0f} | "
                f"{stock['sector']}"
            )
            lines.append("")

        # Footer
        total_est = sum(s.get('est_premium', 0) for s in watchlist)
        lines.append(f"<b>Total Est. Capital:</b> Rs {total_est:,.0f}")

        msg = "\n".join(lines)

        # Telegram has 4096 char limit -- split if needed
        if len(msg) > 4000:
            # Send header + first few stocks, then remainder
            mid_idx = len(watchlist) // 2
            part1_stocks = watchlist[:mid_idx]
            part2_stocks = watchlist[mid_idx:]

            # Build part 1
            p1_lines = lines[:4]
            for i, stock in enumerate(part1_stocks, 1):
                p1_lines.append(f"<b>{i}. {stock['symbol']}</b> "
                                f"Score={stock['score']} CPR={stock['cpr_width']:.2f}%")
                p1_lines.append(f"   P={stock['pivot']:.1f} TC={stock['tc']:.1f} "
                                f"BC={stock['bc']:.1f} | {stock['sector']}")
                p1_lines.append("")
            send_message("\n".join(p1_lines))

            # Build part 2
            p2_lines = []
            for i, stock in enumerate(part2_stocks, mid_idx + 1):
                p2_lines.append(f"<b>{i}. {stock['symbol']}</b> "
                                f"Score={stock['score']} CPR={stock['cpr_width']:.2f}%")
                p2_lines.append(f"   P={stock['pivot']:.1f} TC={stock['tc']:.1f} "
                                f"BC={stock['bc']:.1f} | {stock['sector']}")
                p2_lines.append("")
            p2_lines.append(f"<b>Total Est. Capital:</b> Rs {total_est:,.0f}")
            return send_message("\n".join(p2_lines))
        else:
            return send_message(msg)

    # ================================================================
    # SUMMARY / DISPLAY
    # ================================================================

    def get_scan_summary(self):
        """Return a human-readable summary string of today's scan.

        Returns:
            Multi-line string with scan statistics and top stocks.
        """
        today_str = datetime.now().strftime('%Y-%m-%d')
        scan_data = self.scan_results.get(today_str)

        if scan_data is None:
            scan_data = self._load_scan(today_str)

        if scan_data is None:
            return f"No scan data available for {today_str}."

        stocks = scan_data.get('stocks', [])
        selected = [s for s in stocks if s.get('selected')]

        lines = [
            f"=== CPR Scan Summary for {today_str} ===",
            f"Scan time:       {scan_data.get('scan_time', 'N/A')}",
            f"Duration:        {scan_data.get('scan_elapsed_seconds', 0):.1f}s",
            f"Total scanned:   {scan_data.get('total_scanned', 0)} / "
            f"{scan_data.get('total_symbols', 0)} symbols",
            f"Skipped:         {scan_data.get('skipped', 0)}",
            f"Errors:          {scan_data.get('errors', 0)}",
            f"Narrow CPR:      {scan_data.get('narrow_cpr_count', 0)} "
            f"(< {scan_data.get('cpr_width_threshold', CPR_WIDTH_THRESHOLD)}%)",
            f"Selected:        {len(selected)}",
            f"",
        ]

        if stocks:
            lines.append(f"--- All Narrow CPR Stocks (top 20) ---")
            lines.append(f"{'Symbol':12s} {'CPR%':>7s} {'Score':>5s} {'Sector':12s} "
                         f"{'Liquidity':10s} {'Tier':>4s} {'Recur':>5s} {'Sel':>3s}")
            lines.append("-" * 72)
            for s in stocks[:20]:
                sel_tag = "*" if s.get('selected') else ""
                lines.append(
                    f"{s['symbol']:12s} {s['cpr_width']:>7.3f} {s['score']:>5d} "
                    f"{s.get('sector', '?'):12s} {s.get('liquidity', '?'):10s} "
                    f"{s.get('tier', '?'):>4} {s.get('recurring_days', 1):>5d} "
                    f"{sel_tag:>3s}"
                )

        if selected:
            lines.append("")
            lines.append(f"--- Selected for Trading ---")
            total_est = 0
            for i, s in enumerate(selected, 1):
                est = s.get('est_premium', 0)
                total_est += est
                lines.append(
                    f"  {i}. {s['symbol']:12s} Score={s['score']}  "
                    f"CPR={s['cpr_width']:.3f}%  "
                    f"Pivot={s['pivot']:.1f}  "
                    f"EstPremium=Rs {est:.0f}  "
                    f"Sector={s.get('sector', '?')}"
                )
            lines.append(f"  Total estimated capital: Rs {total_est:,.0f}")

        return "\n".join(lines)


# ====================================================================
# STANDALONE ENTRY POINT
# ====================================================================
def _create_angel_connection():
    """Create an AngelConnection for standalone usage.

    Imports the AngelConnection class from paper_trader.py and initializes it.

    Returns:
        AngelConnection instance (connected) or None on failure.
    """
    try:
        from paper_trader import AngelConnection
        angel = AngelConnection()
        if angel.connect(app_type='Historical'):
            logger.info("Angel One connected for stock scanner")
            return angel
        else:
            logger.error("Failed to connect to Angel One API")
            return None
    except Exception as e:
        logger.error(f"Could not create Angel connection: {e}")
        return None


def main():
    """Standalone entry point for the CPR scanner."""
    global CPR_WIDTH_THRESHOLD

    import argparse

    parser = argparse.ArgumentParser(description='Stock CPR Scanner -- Narrow CPR Discovery')
    parser.add_argument('--history', type=int, default=0,
                        help='Show recurring CPR analysis over N days')
    parser.add_argument('--capital', type=int, default=DEFAULT_CAPITAL,
                        help=f'Available capital in Rs (default: {DEFAULT_CAPITAL})')
    parser.add_argument('--rescan', action='store_true',
                        help='Force rescan even if today\'s results exist')
    parser.add_argument('--notify', action='store_true',
                        help='Send Telegram notification after scan')
    parser.add_argument('--summary', action='store_true',
                        help='Show summary of today\'s existing scan (no new scan)')
    parser.add_argument('--threshold', type=float, default=CPR_WIDTH_THRESHOLD,
                        help=f'CPR width threshold %% (default: {CPR_WIDTH_THRESHOLD})')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Override threshold if specified
    if args.threshold != CPR_WIDTH_THRESHOLD:
        CPR_WIDTH_THRESHOLD = args.threshold
        logger.info(f"CPR width threshold set to {CPR_WIDTH_THRESHOLD}%")

    # Summary mode: just print existing results
    if args.summary:
        scanner = StockCPRScanner()
        print(scanner.get_scan_summary())
        return

    # History mode: show recurring CPR
    if args.history > 0:
        scanner = StockCPRScanner()
        recurring = scanner.get_recurring_narrow_cpr(days=args.history)
        if not recurring:
            print(f"No scan history found for the last {args.history} days.")
            return

        print(f"\n=== Recurring Narrow CPR (last {args.history} trading days) ===")
        print(f"{'Symbol':12s} {'Days':>5s} {'Sector':12s} {'Liquidity':10s} {'Tier':>4s}")
        print("-" * 50)
        for sym, count in recurring.items():
            if count >= 2:
                sector = scanner.fno_config.get_sector(sym)
                liquidity = scanner.fno_config.get_liquidity_tier(sym)
                tier = scanner.fno_config.get_tier(sym)
                print(f"{sym:12s} {count:>5d} {sector:12s} {liquidity:10s} {tier:>4d}")
        return

    # Normal scan mode
    angel = _create_angel_connection()
    if angel is None:
        logger.error("Cannot run scanner without Angel One API connection")
        sys.exit(1)

    scanner = StockCPRScanner(angel_connection=angel)

    # Run scan
    watchlist = scanner.get_todays_watchlist(
        force_rescan=args.rescan,
        available_capital=args.capital,
    )

    # Print summary
    print(scanner.get_scan_summary())

    # Telegram notification
    if args.notify and watchlist:
        logger.info("Sending Telegram notification...")
        scanner.send_watchlist_notification(watchlist)
        logger.info("Telegram notification sent")
    elif args.notify:
        scanner.send_watchlist_notification([])

    # Return exit code based on results
    if watchlist:
        print(f"\n{len(watchlist)} stocks selected for trading today.")
    else:
        print("\nNo stocks selected for trading today.")


if __name__ == '__main__':
    main()
