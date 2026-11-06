"""Pacific-time trading session and new-entry restrictions."""

from datetime import datetime, time
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")

MARKET_OPEN = time(6, 30)
ZERO_DTE_CUTOFF = time(11, 0)
MARKET_CLOSE = time(13, 0)


def pacific_now():
    """Return the current time in the US Pacific timezone."""
    return datetime.now(PACIFIC)


def get_trading_status(now=None):
    """
    Return time-based permissions.

    These permissions apply only to NEW positions.
    Existing positions must continue to be managed independently.
    """
    current = now.astimezone(PACIFIC) if now is not None else pacific_now()
    current_time = current.time().replace(tzinfo=None)

    before_open = current_time < MARKET_OPEN
    after_close = current_time >= MARKET_CLOSE
    after_zero_dte_cutoff = current_time >= ZERO_DTE_CUTOFF

    return {
        "timezone": "America/Los_Angeles",
        "local_time": current.isoformat(),
        "before_market_open": before_open,
        "market_closed": after_close,
        "allow_new_0dte": (
            not before_open
            and not after_zero_dte_cutoff
            and not after_close
        ),
        "allow_new_1dte": not before_open and not after_close,
        "allow_new_positions": not before_open and not after_close,
        "manage_existing_positions": True,
    }


def can_open_new_position(expiry, now=None):
    """Check whether a new position with the requested expiry is allowed."""
    status = get_trading_status(now)

    if expiry == "0DTE":
        return status["allow_new_0dte"]

    if expiry == "1DTE":
        return status["allow_new_1dte"]

    return False


def get_entry_block_reason(expiry, now=None):
    """Return a human-readable reason when a new entry is blocked."""
    status = get_trading_status(now)

    if status["before_market_open"]:
        return "New entries are blocked before 6:30 AM Pacific."

    if status["market_closed"]:
        return "New entries are blocked at or after 1:00 PM Pacific."

    if expiry == "0DTE" and not status["allow_new_0dte"]:
        return "New 0DTE entries are blocked at or after 11:00 AM Pacific."

    if expiry not in ("0DTE", "1DTE"):
        return "Unsupported option expiry."

    return None
