"""
Exit engine: Trailing Stop Loss (TSL) phases, target checks, max loss, and time exits.
All functions are instrument-agnostic -- they operate on position dicts and config.
"""

from datetime import datetime


# ====================================================================
# DEFAULT TSL CONFIGURATION
# ====================================================================
DEFAULT_TSL_CONFIG = {
    'breakeven_gain_pct': 15,   # Phase 1: Lock breakeven when premium gains 15%
    'trail_gain_pct': 25,       # Phase 2: Start trailing when premium gains 25%
    'trail_distance_pct': 30,   # Phase 2 trail: 30% below peak profit
    'tight_gain_pct': 40,       # Phase 3: Tight trail when premium gains 40%+
    'tight_distance_pct': 20,   # Phase 3 trail: 20% below peak profit
}


# ====================================================================
# TRAILING STOP LOSS
# ====================================================================
def compute_tsl(pos, current_premium, config=None):
    """3-phase Trailing Stop Loss computation.

    Phase 1 (Breakeven): Lock SL at entry + 3% when peak gain >= breakeven_gain_pct.
    Phase 2 (Trail):     Trail at trail_distance_pct below peak when gain >= trail_gain_pct.
    Phase 3 (Tight):     Tighten to tight_distance_pct below peak when gain >= tight_gain_pct.

    Works for both BUY and SELL positions:
        BUY:  premium going UP = profit, TSL is below current.
        SELL: premium going DOWN = profit, TSL is above current.

    Args:
        pos: Position dict with keys:
            entry_premium (float),
            is_sell (bool),
            peak_premium (float, optional for BUY),
            trough_premium (float, optional for SELL),
            trailing_sl (float, optional),
            breakeven_locked (bool, optional).
        current_premium: Current option premium.
        config: TSL config dict (uses DEFAULT_TSL_CONFIG if None).
            Keys: breakeven_gain_pct, trail_gain_pct, trail_distance_pct,
                  tight_gain_pct, tight_distance_pct.

    Returns:
        dict with keys:
            trailing_sl: New TSL value (float or None if not triggered).
            breakeven_locked: Whether breakeven has been locked (bool).
            peak_premium: Updated peak (BUY) or trough (SELL) premium.
            phase: Current TSL phase ('none', 'breakeven', 'trail', 'tight').
    """
    cfg = config if config else DEFAULT_TSL_CONFIG
    entry_prem = pos.get('entry_premium', 0)
    is_sell = pos.get('is_sell', False)
    current_tsl = pos.get('trailing_sl', None)
    breakeven_locked = pos.get('breakeven_locked', False)

    if entry_prem <= 0:
        return {
            'trailing_sl': current_tsl,
            'breakeven_locked': breakeven_locked,
            'peak_premium': current_premium,
            'phase': 'none',
        }

    phase = 'none'

    if not is_sell:
        # ---- BUY: premium going UP is profit ----
        peak = max(current_premium, pos.get('peak_premium', entry_prem))
        peak_gain_pct = (peak - entry_prem) / entry_prem * 100
        profit_from_entry = peak - entry_prem

        # Phase 1: Lock breakeven
        if peak_gain_pct >= cfg['breakeven_gain_pct'] and not breakeven_locked:
            breakeven_locked = True
            new_tsl = round(entry_prem * 1.03, 2)  # entry + 3% buffer
            current_tsl = new_tsl
            phase = 'breakeven'

        # Phase 3: Tight trail (check first -- higher priority)
        if peak_gain_pct >= cfg['tight_gain_pct']:
            new_tsl = round(peak - (profit_from_entry * cfg['tight_distance_pct'] / 100), 2)
            new_tsl = max(new_tsl, current_tsl or 0)
            if new_tsl > (current_tsl or 0):
                current_tsl = new_tsl
            phase = 'tight'

        # Phase 2: Trail (only if not already in phase 3)
        elif peak_gain_pct >= cfg['trail_gain_pct']:
            new_tsl = round(peak - (profit_from_entry * cfg['trail_distance_pct'] / 100), 2)
            new_tsl = max(new_tsl, current_tsl or 0)
            if new_tsl > (current_tsl or 0):
                current_tsl = new_tsl
            phase = 'trail'

        return {
            'trailing_sl': current_tsl,
            'breakeven_locked': breakeven_locked,
            'peak_premium': peak,
            'phase': phase,
        }

    else:
        # ---- SELL: premium going DOWN is profit ----
        trough = min(current_premium, pos.get('trough_premium', entry_prem))
        trough_gain_pct = (entry_prem - trough) / entry_prem * 100
        profit_from_entry = entry_prem - trough

        # Phase 1: Lock breakeven
        if trough_gain_pct >= cfg['breakeven_gain_pct'] and not breakeven_locked:
            breakeven_locked = True
            new_tsl = round(entry_prem * 0.97, 2)  # entry - 3% buffer
            current_tsl = new_tsl
            phase = 'breakeven'

        # Phase 3: Tight trail
        if trough_gain_pct >= cfg['tight_gain_pct']:
            new_tsl = round(trough + (profit_from_entry * cfg['tight_distance_pct'] / 100), 2)
            if current_tsl is None or new_tsl < current_tsl:
                current_tsl = new_tsl
            phase = 'tight'

        # Phase 2: Trail
        elif trough_gain_pct >= cfg['trail_gain_pct']:
            new_tsl = round(trough + (profit_from_entry * cfg['trail_distance_pct'] / 100), 2)
            if current_tsl is None or new_tsl < current_tsl:
                current_tsl = new_tsl
            phase = 'trail'

        return {
            'trailing_sl': current_tsl,
            'breakeven_locked': breakeven_locked,
            'peak_premium': trough,  # For SELL, "peak" is actually the trough
            'phase': phase,
        }


def check_tsl_hit(pos, current_premium):
    """Check if trailing stop loss has been hit.

    Args:
        pos: Position dict with trailing_sl and is_sell.
        current_premium: Current option premium.

    Returns:
        (hit, exit_premium): Tuple of (bool, float).
            hit = True if TSL was breached.
            exit_premium = current_premium if hit, else 0.
    """
    tsl = pos.get('trailing_sl')
    if tsl is None:
        return False, 0

    if pos.get('is_sell', False):
        # SELL: premium RISING above TSL = loss
        if current_premium >= tsl:
            return True, current_premium
    else:
        # BUY: premium DROPPING below TSL = loss
        if current_premium <= tsl:
            return True, current_premium

    return False, 0


# ====================================================================
# TARGET AND STOP LOSS CHECKS
# ====================================================================
def check_target_hit(pos, current_premium):
    """Check if target premium has been reached.

    Args:
        pos: Position dict with:
            target (float): Target premium.
            is_sell (bool): True for sell positions.
        current_premium: Current option premium.

    Returns:
        (hit, exit_premium): Tuple.
            hit = True if target reached.
            exit_premium = current_premium if hit, else 0.
    """
    target = pos.get('target', 0)
    if target <= 0:
        return False, 0

    if pos.get('is_sell', False):
        # SELL: target is LOWER premium (decay)
        if current_premium <= target:
            return True, current_premium
    else:
        # BUY: target is HIGHER premium (appreciation)
        if current_premium >= target:
            return True, current_premium

    return False, 0


def check_max_loss(pos, current_premium, max_loss_pct=60):
    """Check if maximum loss has been breached.

    Args:
        pos: Position dict with entry_premium and is_sell.
        current_premium: Current option premium.
        max_loss_pct: Maximum acceptable loss as % of entry premium (default 60%).

    Returns:
        (breached, exit_premium): Tuple.
            breached = True if loss exceeds max_loss_pct.
            exit_premium = current_premium if breached, else 0.
    """
    entry_prem = pos.get('entry_premium', 0)
    if entry_prem <= 0:
        return False, 0

    if pos.get('is_sell', False):
        # SELL: premium going UP = loss
        loss_pct = (current_premium - entry_prem) / entry_prem * 100
    else:
        # BUY: premium going DOWN = loss
        loss_pct = (entry_prem - current_premium) / entry_prem * 100

    if loss_pct >= max_loss_pct:
        return True, current_premium

    return False, 0


def check_sl_hit(pos, current_premium):
    """Check if static stop loss has been hit.

    Args:
        pos: Position dict with:
            sl (float): Stop-loss premium.
            is_sell (bool): True for sell positions.
        current_premium: Current option premium.

    Returns:
        (hit, exit_premium): Tuple.
    """
    sl = pos.get('sl', 0)
    if sl <= 0:
        return False, 0

    if pos.get('is_sell', False):
        # SELL: SL is above entry (premium rising = loss)
        if current_premium >= sl:
            return True, current_premium
    else:
        # BUY: SL is below entry (premium falling = loss)
        if current_premium <= sl:
            return True, current_premium

    return False, 0


# ====================================================================
# TIME-BASED EXIT
# ====================================================================
def check_time_exit(pos, current_time=None, max_hold_hours=4,
                    min_profit_pct=15):
    """Check if time-based exit conditions are met.

    Exits positions that have been held too long without meaningful progress.

    Args:
        pos: Position dict with:
            timestamp (str, ISO format): Entry time.
            unrealized_pnl (float): Current P&L.
            max_risk (float, optional): Max risk amount for % calculation.
            entry_premium (float): Entry premium.
            lot_size (int): Lot size.
        current_time: Current datetime (defaults to now()).
        max_hold_hours: Maximum hold time before checking (default 4).
        min_profit_pct: Minimum profit % to justify holding (default 15).

    Returns:
        (should_exit, reason): Tuple of (bool, str).
    """
    if current_time is None:
        current_time = datetime.now()

    entry_time = datetime.fromisoformat(pos.get('timestamp', current_time.isoformat()))
    hours_held = (current_time - entry_time).total_seconds() / 3600

    if hours_held <= max_hold_hours:
        return False, ''

    # Calculate profit as percentage of max risk
    max_risk = pos.get('max_risk',
                       pos.get('entry_premium', 1) * pos.get('lot_size', 1))
    if max_risk <= 0:
        max_risk = 1

    pnl = pos.get('unrealized_pnl', 0)
    profit_pct = (pnl / max_risk) * 100

    if abs(profit_pct) < min_profit_pct:
        reason = (f"TIME_EXIT: held {hours_held:.1f}h with only "
                  f"{profit_pct:.1f}% profit (threshold {min_profit_pct}%)")
        return True, reason

    return False, ''


def check_signal_weak_exit(pos, hold_score, current_time=None,
                           weak_threshold=40, min_hold_mins=30,
                           min_hold_hours_for_exit=1):
    """Check if position should exit due to weak hold score + loss.

    Args:
        pos: Position dict with unrealized_pnl, timestamp.
        hold_score: Current hold score (0-100).
        current_time: Current datetime (defaults to now()).
        weak_threshold: Hold score below this = weak (default 40).
        min_hold_mins: Minimum minutes before computing hold score (default 30).
        min_hold_hours_for_exit: Minimum hours held before allowing weak exit (default 1).

    Returns:
        (should_exit, reason): Tuple of (bool, str).
    """
    if current_time is None:
        current_time = datetime.now()

    entry_time = datetime.fromisoformat(pos.get('timestamp', current_time.isoformat()))
    hold_mins = (current_time - entry_time).total_seconds() / 60
    hours_held = hold_mins / 60

    if hold_mins < min_hold_mins:
        return False, ''

    if (hold_score < weak_threshold
            and pos.get('unrealized_pnl', 0) < 0
            and hours_held > min_hold_hours_for_exit):
        reason = (f"SIGNAL_WEAK_EXIT: hold_score={hold_score} "
                  f"PnL Rs {pos.get('unrealized_pnl', 0):.0f} -- signals reversed")
        return True, reason

    return False, ''
