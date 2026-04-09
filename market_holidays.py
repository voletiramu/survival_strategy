"""
INDIAN MARKET HOLIDAY CALENDAR
===============================
NSE/BSE and MCX holiday calendars for preventing trades on market holidays.

NSE: Full closure on all listed dates (no equity/derivative trading)
MCX: Full closure on some dates, partial closure on others
     (morning session 9:00-17:00 closed, evening 17:00-23:30 open)

Sources:
  - NSE Official: https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf
  - MCX Official: https://www.mcxindia.com/market-operations/trading-survelliance/trading-holidays
  - Groww/ClearTax aggregated lists

Usage:
    from market_holidays import is_nse_holiday, is_mcx_holiday, is_mcx_morning_closed

Updated: March 2026
"""

from datetime import date, time as dtime, datetime

# ====================================================================
# NSE/BSE HOLIDAYS 2025
# ====================================================================
NSE_HOLIDAYS_2025 = {
    date(2025, 2, 26): "Maha Shivaratri",
    date(2025, 3, 14): "Holi",
    date(2025, 3, 31): "Id-Ul-Fitr (Ramadan)",
    date(2025, 4, 10): "Shri Ram Navami",
    date(2025, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2025, 4, 18): "Good Friday",
    date(2025, 5, 1): "Maharashtra Day",
    date(2025, 8, 15): "Independence Day",
    date(2025, 8, 16): "Janmashtami",
    date(2025, 8, 27): "Ganesh Chaturthi",
    date(2025, 10, 2): "Mahatma Gandhi Jayanti / Dussehra",
    date(2025, 10, 21): "Diwali Laxmi Pujan",
    date(2025, 10, 22): "Diwali Balipratipada",
    date(2025, 11, 5): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2025, 12, 25): "Christmas",
}

# ====================================================================
# NSE/BSE HOLIDAYS 2026 (Official NSE Circular CMTR71775)
# ====================================================================
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 15): "Municipal Corporation Election Day (Maharashtra)",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id (Eid al-Adha)",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# Combined NSE holidays (all years)
NSE_HOLIDAYS = {}
NSE_HOLIDAYS.update(NSE_HOLIDAYS_2025)
NSE_HOLIDAYS.update(NSE_HOLIDAYS_2026)

# ====================================================================
# MCX HOLIDAYS 2026
# ====================================================================
# Full closures: Both morning (9:00-17:00) and evening (17:00-23:30) sessions closed
MCX_FULL_CLOSURES_2026 = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 12, 25): "Christmas",
}

# Partial closures: Morning session (9:00-17:00) CLOSED, Evening (17:00-23:30) OPEN
MCX_MORNING_CLOSURES_2026 = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali Balipratipada",
    date(2026, 11, 24): "Guru Nanak Jayanti",
}

# Combined MCX holidays
MCX_ALL_HOLIDAYS = {}
MCX_ALL_HOLIDAYS.update(MCX_FULL_CLOSURES_2026)
MCX_ALL_HOLIDAYS.update(MCX_MORNING_CLOSURES_2026)

# MCX session boundary
MCX_EVENING_SESSION_START = dtime(17, 0)  # 5:00 PM


# ====================================================================
# PUBLIC API
# ====================================================================

def is_nse_holiday(check_date=None):
    """Check if a date is an NSE/BSE trading holiday.

    Args:
        check_date: date object or None (defaults to today)

    Returns:
        tuple: (is_holiday: bool, holiday_name: str or None)
    """
    if check_date is None:
        check_date = date.today()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    # Weekend check
    if check_date.weekday() > 4:
        day_name = "Saturday" if check_date.weekday() == 5 else "Sunday"
        return True, f"Weekend ({day_name})"

    # Holiday calendar check
    holiday_name = NSE_HOLIDAYS.get(check_date)
    if holiday_name:
        return True, holiday_name

    return False, None


def is_mcx_holiday(check_date=None, check_time=None):
    """Check if MCX is closed for trading at a given date/time.

    MCX has two sessions:
      Morning: 9:00 AM - 5:00 PM
      Evening: 5:00 PM - 11:30 PM

    On partial holidays, morning is closed but evening is open.

    Args:
        check_date: date object or None (defaults to today)
        check_time: time object or None (defaults to current time)

    Returns:
        tuple: (is_closed: bool, reason: str or None)
    """
    if check_date is None:
        check_date = date.today()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    if check_time is None:
        check_time = datetime.now().time()

    # Weekend check
    if check_date.weekday() > 4:
        day_name = "Saturday" if check_date.weekday() == 5 else "Sunday"
        return True, f"Weekend ({day_name})"

    # Full closure check
    full_holiday = MCX_FULL_CLOSURES_2026.get(check_date)
    if full_holiday:
        return True, f"{full_holiday} (MCX full closure)"

    # Partial closure check (morning closed, evening open)
    partial_holiday = MCX_MORNING_CLOSURES_2026.get(check_date)
    if partial_holiday:
        if check_time < MCX_EVENING_SESSION_START:
            return True, f"{partial_holiday} (MCX morning session closed)"
        else:
            return False, None  # Evening session is open

    return False, None


def is_mcx_morning_closed(check_date=None):
    """Check if MCX morning session (9:00-17:00) is closed on a date.

    Returns:
        tuple: (is_closed: bool, holiday_name: str or None)
    """
    if check_date is None:
        check_date = date.today()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    # Full closures = morning also closed
    full = MCX_FULL_CLOSURES_2026.get(check_date)
    if full:
        return True, full

    # Partial closures = morning closed
    partial = MCX_MORNING_CLOSURES_2026.get(check_date)
    if partial:
        return True, partial

    return False, None


def get_next_trading_day(exchange='NSE', from_date=None):
    """Get the next trading day for NSE or MCX.

    Args:
        exchange: 'NSE' or 'MCX'
        from_date: Start date (defaults to today)

    Returns:
        date: Next trading day
    """
    if from_date is None:
        from_date = date.today()
    elif isinstance(from_date, datetime):
        from_date = from_date.date()

    from datetime import timedelta
    check = from_date + timedelta(days=1)
    for _ in range(30):  # Max 30 days lookahead
        if exchange.upper() == 'NSE':
            is_holiday, _ = is_nse_holiday(check)
        else:
            is_holiday, _ = is_mcx_holiday(check, dtime(10, 0))  # Check morning session
        if not is_holiday:
            return check
        check += timedelta(days=1)
    return check  # Fallback


def get_holiday_name(check_date=None, exchange='NSE'):
    """Get the holiday name for a date, if it's a holiday."""
    if exchange.upper() == 'NSE':
        _, name = is_nse_holiday(check_date)
    else:
        _, name = is_mcx_holiday(check_date)
    return name


# ====================================================================
# QUICK TEST
# ====================================================================
if __name__ == '__main__':
    from datetime import timedelta

    print("=" * 60)
    print("NSE HOLIDAYS 2026")
    print("=" * 60)
    for d in sorted(NSE_HOLIDAYS_2026.keys()):
        print(f"  {d.strftime('%Y-%m-%d %A'):30s} {NSE_HOLIDAYS_2026[d]}")

    print(f"\n  Total: {len(NSE_HOLIDAYS_2026)} holidays")

    print("\n" + "=" * 60)
    print("MCX HOLIDAYS 2026")
    print("=" * 60)
    print("\n  FULL CLOSURES (both sessions):")
    for d in sorted(MCX_FULL_CLOSURES_2026.keys()):
        print(f"    {d.strftime('%Y-%m-%d %A'):30s} {MCX_FULL_CLOSURES_2026[d]}")
    print(f"\n  PARTIAL CLOSURES (morning closed, evening open):")
    for d in sorted(MCX_MORNING_CLOSURES_2026.keys()):
        print(f"    {d.strftime('%Y-%m-%d %A'):30s} {MCX_MORNING_CLOSURES_2026[d]}")

    # Test today
    print(f"\n\nToday ({date.today()}):")
    is_h, name = is_nse_holiday()
    print(f"  NSE holiday: {is_h} ({name})")
    is_h, name = is_mcx_holiday()
    print(f"  MCX closed:  {is_h} ({name})")

    # Test Holi 2026
    holi = date(2026, 3, 3)
    print(f"\nHoli ({holi}):")
    is_h, name = is_nse_holiday(holi)
    print(f"  NSE holiday: {is_h} ({name})")
    is_h, name = is_mcx_holiday(holi, dtime(10, 0))
    print(f"  MCX 10:00 AM: closed={is_h} ({name})")
    is_h, name = is_mcx_holiday(holi, dtime(18, 0))
    print(f"  MCX 06:00 PM: closed={is_h} ({name})")
