"""Isolated, read-only retrieval of one verified US Webull margin account.

This module accepts an already constructed ``TradeClient.account_v2``-compatible
object. It never creates a Webull client, reads credentials, logs responses, or
imports the trading runner.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import math


DEFAULT_MAX_RESPONSE_AGE_SECONDS = 30
_ALLOWED_FUTURE_SKEW_SECONDS = 5


class AccountDataError(RuntimeError):
    """Raised when account data is unavailable or cannot be trusted."""


@dataclass(frozen=True)
class OptionLeg:
    symbol: str
    quantity: Decimal
    option_type: str
    expiration_date: str
    exercise_price: Decimal
    contract_multiplier: Decimal
    contract_deliverable: Decimal | None


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    instrument_type: str
    currency: str
    quantity: Decimal
    last_price: Decimal
    cost_price: Decimal
    unrealized_profit_loss: Decimal
    option_strategy: str | None
    legs: tuple[OptionLeg, ...]


@dataclass(frozen=True)
class MarginAccountData:
    """Normalized margin-account data; account identifiers are not returned."""

    account_type: str
    currency: str
    equity: Decimal
    buying_power: Decimal | None
    option_buying_power: Decimal
    day_buying_power: Decimal | None
    overnight_buying_power: Decimal | None
    positions: tuple[AccountPosition, ...]
    as_of: datetime
    night_trading_buying_power: Decimal | None
    account_id: str = field(default="", repr=False)
    # True means every row in the successful top-level positions response was
    # parsed. It does not assert when Webull generated its upstream snapshot.
    positions_complete: bool = False


def _utc_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise AccountDataError("A timezone-aware current time is required.")
    return now.astimezone(timezone.utc)


def _response_timestamp(response, now, max_age_seconds):
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        raise AccountDataError("Account response has no freshness metadata.")
    date_value = headers.get("Date")
    if not isinstance(date_value, str) or not date_value.strip():
        raise AccountDataError("Account response has no freshness metadata.")
    try:
        timestamp = parsedate_to_datetime(date_value)
    except (TypeError, ValueError, OverflowError):
        raise AccountDataError("Account response freshness metadata is invalid.") from None
    if timestamp.tzinfo is None:
        raise AccountDataError("Account response freshness metadata is invalid.")
    timestamp = timestamp.astimezone(timezone.utc)
    age_seconds = (now - timestamp).total_seconds()
    if age_seconds > max_age_seconds or age_seconds < -_ALLOWED_FUTURE_SKEW_SECONDS:
        raise AccountDataError("Account response is stale or has an invalid timestamp.")
    return timestamp


def _read_response(response, *, now, max_age_seconds):
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        raise AccountDataError("Webull account data permission was denied.")
    if status != 200:
        raise AccountDataError("Webull account data request failed.")

    timestamp = _response_timestamp(response, now, max_age_seconds)
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        raise AccountDataError("Webull account response is malformed.")
    try:
        payload = json_method()
    except Exception:
        raise AccountDataError("Webull account response is malformed.") from None
    return payload, timestamp


def _call(method, *args):
    try:
        return method(*args)
    except Exception:
        # SDK exceptions can contain request metadata; do not pass them through.
        raise AccountDataError("Webull account data is unavailable.") from None


def _required_string(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AccountDataError("Webull account response is malformed.")
    return value


def _decimal_string(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AccountDataError("Webull account response is malformed.")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise AccountDataError("Webull account response contains an invalid amount.") from None
    if not result.is_finite():
        raise AccountDataError("Webull account response contains an invalid amount.")
    return result


def _optional_decimal_string(data, key):
    if key not in data or data[key] is None:
        return None
    return _decimal_string(data, key)


def _select_margin_account(accounts):
    if not isinstance(accounts, list) or not accounts:
        raise AccountDataError("Webull account list is missing or malformed.")

    margin_accounts = []
    for account in accounts:
        if not isinstance(account, dict):
            raise AccountDataError("Webull account list is malformed.")
        account_type = account.get("account_type")
        account_id = account.get("account_id")
        if account_type not in ("MARGIN", "CASH"):
            raise AccountDataError("Webull account type is missing or unsupported.")
        if not isinstance(account_id, str) or not account_id.strip():
            raise AccountDataError("Webull account list is malformed.")
        if account_type == "MARGIN":
            margin_accounts.append(account_id)

    if len(margin_accounts) != 1:
        raise AccountDataError("Exactly one MARGIN account is required.")
    return margin_accounts[0]


def _parse_option_leg(leg):
    if not isinstance(leg, dict):
        raise AccountDataError("Option position data is malformed.")
    symbol = _required_string(leg, "symbol")
    quantity = _decimal_string(leg, "quantity")
    option_type = _required_string(leg, "option_type")
    if option_type not in ("CALL", "PUT"):
        raise AccountDataError("Option position data is malformed.")
    expiration_date = _required_string(leg, "option_expire_date")
    try:
        if datetime.strptime(expiration_date, "%Y-%m-%d").date().isoformat() != expiration_date:
            raise ValueError
    except ValueError:
        raise AccountDataError("Option position data is malformed.") from None
    exercise_price = _decimal_string(leg, "option_exercise_price")
    multiplier = _decimal_string(leg, "option_contract_multiplier")
    if multiplier <= 0:
        raise AccountDataError("Option position data is malformed.")
    deliverable_value = leg.get("option_contract_deliverable")
    if deliverable_value is None:
        deliverable = None
    else:
        deliverable = _decimal_string(leg, "option_contract_deliverable")
        if deliverable <= 0:
            raise AccountDataError("Option position data is malformed.")
    return OptionLeg(
        symbol=symbol,
        quantity=quantity,
        option_type=option_type,
        expiration_date=expiration_date,
        exercise_price=exercise_price,
        contract_multiplier=multiplier,
        contract_deliverable=deliverable,
    )


def _parse_position(position):
    if not isinstance(position, dict):
        raise AccountDataError("Webull position response is malformed.")
    instrument_type = _required_string(position, "instrument_type")
    if instrument_type not in ("EQUITY", "OPTION"):
        raise AccountDataError("Webull returned an unsupported position type.")
    currency = _required_string(position, "currency")
    if currency != "USD":
        raise AccountDataError("Webull position currency is not USD.")

    symbol = _required_string(position, "symbol")
    quantity = _decimal_string(position, "quantity")
    last_price = _decimal_string(position, "last_price")
    cost_price = _decimal_string(position, "cost_price")
    unrealized = _decimal_string(position, "unrealized_profit_loss")

    legs = position.get("legs", [])
    if instrument_type == "OPTION":
        if not isinstance(legs, list) or not legs:
            raise AccountDataError("Option position legs are missing or malformed.")
        parsed_legs = tuple(_parse_option_leg(leg) for leg in legs)
    else:
        if legs not in (None, []):
            raise AccountDataError("Equity position response is malformed.")
        parsed_legs = ()

    option_strategy = position.get("option_strategy")
    if option_strategy is not None and not isinstance(option_strategy, str):
        raise AccountDataError("Webull position response is malformed.")

    return AccountPosition(
        symbol=symbol,
        instrument_type=instrument_type,
        currency=currency,
        quantity=quantity,
        last_price=last_price,
        cost_price=cost_price,
        unrealized_profit_loss=unrealized,
        option_strategy=option_strategy,
        legs=parsed_legs,
    )


def fetch_margin_account_data(
    account_api,
    *,
    now=None,
    max_response_age_seconds=DEFAULT_MAX_RESPONSE_AGE_SECONDS,
):
    """Fetch and validate account, balance, and positions using ``account_v2``.

    ``account_api`` must provide only the read methods used here. Pass a
    ``TradeClient.account_v2`` in a separately approved runtime integration, or
    a fake object in offline tests. Each response must have a fresh HTTP Date
    header; the SDK/API payloads do not expose a documented data snapshot time.
    """
    if (
        isinstance(max_response_age_seconds, bool)
        or not isinstance(max_response_age_seconds, (int, float))
        or not math.isfinite(max_response_age_seconds)
        or max_response_age_seconds <= 0
    ):
        raise AccountDataError("Maximum account response age must be positive.")
    current_time = _utc_now(now)

    account_payload, account_time = _read_response(
        _call(account_api.get_account_list),
        now=current_time,
        max_age_seconds=max_response_age_seconds,
    )
    account_id = _select_margin_account(account_payload)

    balance_payload, balance_time = _read_response(
        _call(account_api.get_account_balance, account_id),
        now=current_time,
        max_age_seconds=max_response_age_seconds,
    )
    positions_payload, positions_time = _read_response(
        _call(account_api.get_account_position, account_id),
        now=current_time,
        max_age_seconds=max_response_age_seconds,
    )

    if not isinstance(balance_payload, dict):
        raise AccountDataError("Webull balance response is malformed.")
    currency = _required_string(balance_payload, "total_asset_currency")
    if currency != "USD":
        raise AccountDataError("Webull account currency is not USD.")
    equity = _decimal_string(balance_payload, "total_net_liquidation_value")

    currency_assets = balance_payload.get("account_currency_assets")
    if not isinstance(currency_assets, list) or not currency_assets:
        raise AccountDataError("Webull buying-power data is missing or malformed.")
    for item in currency_assets:
        if not isinstance(item, dict) or not isinstance(item.get("currency"), str):
            raise AccountDataError("Webull buying-power data is malformed.")
    usd_assets = [
        item for item in currency_assets
        if isinstance(item, dict) and item.get("currency") == "USD"
    ]
    if len(usd_assets) != 1:
        raise AccountDataError("Exactly one USD buying-power record is required.")
    usd_asset = usd_assets[0]
    buying_power = _optional_decimal_string(usd_asset, "buying_power")
    option_buying_power = _decimal_string(usd_asset, "option_buying_power")
    day_buying_power = _optional_decimal_string(usd_asset, "day_buying_power")
    overnight_buying_power = _optional_decimal_string(usd_asset, "overnight_buying_power")
    night_trading_buying_power = _optional_decimal_string(
        usd_asset, "night_trading_buying_power"
    )

    if not isinstance(positions_payload, list):
        raise AccountDataError("Webull position response is malformed.")
    positions = tuple(_parse_position(item) for item in positions_payload)

    return MarginAccountData(
        account_type="MARGIN",
        currency="USD",
        equity=equity,
        buying_power=buying_power,
        option_buying_power=option_buying_power,
        day_buying_power=day_buying_power,
        overnight_buying_power=overnight_buying_power,
        positions=positions,
        as_of=min(account_time, balance_time, positions_time),
        night_trading_buying_power=night_trading_buying_power,
        account_id=account_id,
        positions_complete=True,
    )
