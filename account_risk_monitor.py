"""Offline account-level risk evaluation with persistent daily equity state.

This module is deliberately not connected to the dashboard, runner, or broker.
It consumes a validated ``MarginAccountData`` value and a separately supplied
account-snapshot update timestamp. ``MarginAccountData.as_of`` is only the HTTP
response Date and is never treated as the account snapshot timestamp.
"""

import json
import math
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from account_data import AccountPosition, MarginAccountData
from config import BASE_DIR
import state as project_state


MARKET_TZ = ZoneInfo("America/New_York")
DEFAULT_RISK_STATE_PATH = BASE_DIR / "data" / "risk_state.json"
DEFAULT_MAX_RESPONSE_AGE_SECONDS = 30
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 30
FUTURE_SKEW_SECONDS = 5
HUNDRED = Decimal("100")


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float, str)):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError from None
    else:
        raise ValueError
    if not result.is_finite():
        raise ValueError
    return result


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError
    return value.astimezone(timezone.utc)


def _age_is_valid(timestamp, now, max_age_seconds):
    age = (now - timestamp).total_seconds()
    return -FUTURE_SKEW_SECONDS <= age <= max_age_seconds


def _risk_settings(provided):
    if provided is None:
        provided = project_state.settings.get("global")
    if not isinstance(provided, dict):
        raise ValueError
    profit_cap = _decimal(provided.get("daily_profit_cap_pct"))
    drawdown_limit = _decimal(provided.get("peak_drawdown_limit_pct"))
    # Match the ranges enforced by the existing Dashboard settings endpoint.
    if not Decimal("0.1") <= profit_cap <= Decimal("1000"):
        raise ValueError
    if not Decimal("0.1") <= drawdown_limit <= Decimal("100"):
        raise ValueError
    return profit_cap, drawdown_limit


def _validate_account_data(account_data):
    if not isinstance(account_data, MarginAccountData):
        raise ValueError("Account data is missing or unverified.")
    if account_data.account_type != "MARGIN":
        raise ValueError("Only a verified MARGIN account is accepted.")
    if account_data.currency != "USD":
        raise ValueError("Account currency must be USD.")

    equity = _decimal(account_data.equity)
    if equity <= 0:
        raise ValueError("Account equity must be a positive USD amount.")

    buying_power = account_data.buying_power
    option_buying_power = _decimal(account_data.option_buying_power)
    # OPTION buying power is the relevant capacity for option entries. Other
    # buying-power fields remain available as separate metadata and are not
    # substituted for or required in risk evaluation.
    if option_buying_power < 0:
        raise ValueError("Option buying-power data is invalid.")

    positions = account_data.positions
    if not isinstance(positions, tuple):
        raise ValueError("Position data is missing or malformed.")
    unrealized_total = Decimal("0")
    for position in positions:
        if not isinstance(position, AccountPosition):
            raise ValueError("Position data is missing or malformed.")
        if position.instrument_type not in ("EQUITY", "OPTION") or position.currency != "USD":
            raise ValueError("Position data is unsupported or has the wrong currency.")
        for amount in (
            position.quantity,
            position.last_price,
            position.cost_price,
            position.unrealized_profit_loss,
        ):
            _decimal(amount)
        unrealized_total += _decimal(position.unrealized_profit_loss)

    return {
        "current_equity": equity,
        "buying_power": buying_power,
        "option_buying_power": option_buying_power,
        "day_buying_power": account_data.day_buying_power,
        "overnight_buying_power": account_data.overnight_buying_power,
        "night_trading_buying_power": account_data.night_trading_buying_power,
        "positions": positions,
        "unrealized_pnl": unrealized_total,
    }


def _load_day_state(path, day_key, equity):
    if not path.exists():
        return {
            "date": day_key,
            "start_equity": equity,
            "peak_equity": equity,
            "latched": False,
            "reason": None,
        }

    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Persisted risk state is unreadable.") from None
    if not isinstance(saved, dict):
        raise ValueError("Persisted risk state is malformed.")
    saved_date = saved.get("date")
    if not isinstance(saved_date, str):
        raise ValueError("Persisted risk state has no valid date.")
    try:
        parsed_day = date.fromisoformat(saved_date)
    except ValueError:
        raise ValueError("Persisted risk state has no valid date.") from None
    if parsed_day.isoformat() != saved_date:
        raise ValueError("Persisted risk state has no valid date.")
    requested_day = date.fromisoformat(day_key)

    # A future state date indicates a clock or state-file problem. Do not reset it.
    if parsed_day > requested_day:
        raise ValueError("Persisted risk state is from a future date.")
    if parsed_day < requested_day:
        return {
            "date": day_key,
            "start_equity": equity,
            "peak_equity": equity,
            "latched": False,
            "reason": None,
        }

    try:
        start_equity = _decimal(saved.get("start_equity"))
        peak_equity = _decimal(saved.get("peak_equity"))
    except ValueError:
        raise ValueError("Persisted risk state has invalid equity values.") from None
    latched = saved.get("latched")
    reason = saved.get("reason")
    if (
        start_equity <= 0
        or peak_equity < start_equity
        or not isinstance(latched, bool)
        or (reason is not None and not isinstance(reason, str))
    ):
        raise ValueError("Persisted risk state is inconsistent.")
    if latched and reason is not None and not reason.strip():
        raise ValueError("Persisted risk state is inconsistent.")
    return {
        "date": day_key,
        "start_equity": start_equity,
        "peak_equity": peak_equity,
        "latched": latched,
        "reason": reason,
    }


def _persist_day_state(path, day_state):
    payload = {
        "date": day_state["date"],
        "start_equity": str(day_state["start_equity"]),
        "peak_equity": str(day_state["peak_equity"]),
        "latched": day_state["latched"],
        "reason": day_state["reason"],
        "block_new_entries": day_state["latched"],
        "close_all_positions_required": day_state["latched"],
    }
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("Persisted risk state could not be saved.") from None


def _result(
    *,
    allow=False,
    status="BLOCKED",
    reason="Risk state is uncertain.",
    margin_verified=False,
    current_equity=None,
    starting_equity=None,
    peak_equity=None,
    daily_profit_pct=None,
    drawdown_pct=None,
    account_values=None,
    response_time=None,
    snapshot_time=None,
):
    values = account_values or {}
    return {
        "allow_new_entries": allow,
        "risk_status": status,
        "risk_reason": reason,
        "margin_account_verified": margin_verified,
        "current_equity": current_equity,
        "starting_equity": starting_equity,
        "peak_equity": peak_equity,
        "daily_profit_pct": daily_profit_pct,
        "drawdown_from_peak_pct": drawdown_pct,
        "buying_power": values.get("buying_power"),
        "option_buying_power": values.get("option_buying_power"),
        "day_buying_power": values.get("day_buying_power"),
        "overnight_buying_power": values.get("overnight_buying_power"),
        "night_trading_buying_power": values.get("night_trading_buying_power"),
        "positions": values.get("positions"),
        "unrealized_pnl": values.get("unrealized_pnl"),
        "equity_includes_unrealized_pnl": bool(margin_verified),
        "api_response_time": response_time,
        "account_data_updated_at": snapshot_time,
    }


def evaluate_account_risk(
    account_data,
    *,
    account_data_updated_at=None,
    now=None,
    risk_settings=None,
    risk_state_path=None,
    max_snapshot_age_seconds=DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    max_response_age_seconds=DEFAULT_MAX_RESPONSE_AGE_SECONDS,
    require_snapshot_timestamp=True,
):
    """Evaluate daily risk from validated account data; never place or manage orders.

    ``account_data_updated_at`` must represent when the account snapshot itself
    was updated, obtained independently from the HTTP response Date header. The
    account SDK schema currently supplies no such documented timestamp; if the
    caller cannot provide one, this function blocks new entries by default.
    A caller may set ``require_snapshot_timestamp=False`` only when it applies
    its own explicit freshness policy to complete local responses. That policy
    does not establish freshness of Webull's upstream account snapshot.

    When ``risk_settings`` is omitted, the current persisted values loaded by
    ``state.py`` are used. Risk state is stored in the same file and compatible
    shape as ``risk.py``. Pass a temporary path in tests.
    """
    margin_verified = (
        isinstance(account_data, MarginAccountData)
        and account_data.account_type == "MARGIN"
    )
    values = None
    response_time = None
    snapshot_time = None
    current_equity = None
    starting_equity = None
    peak_equity = None
    daily_profit_pct = None
    drawdown_pct = None
    try:
        current_time = _timestamp(now) if now is not None else datetime.now(timezone.utc)
        max_ages = (max_snapshot_age_seconds, max_response_age_seconds)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in max_ages
        ):
            raise ValueError("Freshness limits are invalid.")

        values = _validate_account_data(account_data)
        response_time = _timestamp(account_data.as_of)
        if not _age_is_valid(response_time, current_time, max_response_age_seconds):
            raise ValueError("API response is stale.")
        if require_snapshot_timestamp:
            if account_data_updated_at is None:
                raise ValueError(
                    "Account snapshot update time is unknown; HTTP response time is not proof of snapshot freshness."
                )
            snapshot_time = _timestamp(account_data_updated_at)
            if not _age_is_valid(snapshot_time, current_time, max_snapshot_age_seconds):
                raise ValueError("Account snapshot data is stale or future-dated.")

        profit_cap, drawdown_limit = _risk_settings(risk_settings)
        day_key = current_time.astimezone(MARKET_TZ).date().isoformat()
        path = Path(risk_state_path) if risk_state_path is not None else DEFAULT_RISK_STATE_PATH
        day_state = _load_day_state(path, day_key, values["current_equity"])

        starting_equity = day_state["start_equity"]
        peak_equity = max(day_state["peak_equity"], values["current_equity"])
        current_equity = values["current_equity"]
        daily_profit_pct = (current_equity - starting_equity) / starting_equity * HUNDRED
        drawdown_pct = (peak_equity - current_equity) / peak_equity * HUNDRED

        reason = day_state["reason"]
        if not day_state["latched"]:
            if daily_profit_pct >= profit_cap:
                day_state["latched"] = True
                reason = "Daily profit cap reached."
            elif drawdown_pct >= drawdown_limit:
                day_state["latched"] = True
                reason = "Peak drawdown limit reached."
        day_state["peak_equity"] = peak_equity
        day_state["reason"] = reason
        _persist_day_state(path, day_state)

        if day_state["latched"]:
            reason = reason or "Daily risk lock is active."
            status = "LOCKED"
            allow = False
        else:
            reason = "Risk limits are not reached."
            status = "OK"
            allow = True
        return _result(
            allow=allow,
            status=status,
            reason=reason,
            margin_verified=True,
            current_equity=current_equity,
            starting_equity=starting_equity,
            peak_equity=peak_equity,
            daily_profit_pct=daily_profit_pct,
            drawdown_pct=drawdown_pct,
            account_values=values,
            response_time=response_time,
            snapshot_time=snapshot_time,
        )
    except (ValueError, OSError, TypeError, KeyError):
        # Do not leak IDs, raw data, SDK exceptions, or settings into caller logs.
        error = sys.exc_info()[1]
        return _result(
            reason=str(error) or "Account risk state is uncertain.",
            margin_verified=margin_verified,
            current_equity=current_equity,
            starting_equity=starting_equity,
            peak_equity=peak_equity,
            daily_profit_pct=daily_profit_pct,
            drawdown_pct=drawdown_pct,
            account_values=values,
            response_time=response_time,
            snapshot_time=snapshot_time,
        )
