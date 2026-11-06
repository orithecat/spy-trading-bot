"""Strategy 1 validation and entry evaluation."""

from trading_hours import get_entry_block_reason, get_trading_status

ENTRY_FILTER_DEFAULTS = {
    "ema_gap_not_shrinking": True,
}

MANDATORY_FILTERS = {
    "ema_alignment": "1-minute EMA alignment",
    "five_min_trend_confirmation": "5-minute trend confirmation",
    "option_rebound": "Option rebound trigger",
    "quote_quality": "Quote quality",
}


def validate_settings(payload):
    ranges = {
        "max_trade_amount": (1, 1_000_000),
        "take_profit_pct": (0.1, 500),
        "stop_loss_pct": (0.1, 100),
        "rebound_trigger_pct": (0.1, 100),
    }

    expiry = payload.get("expiry", "0DTE")
    if expiry not in ("0DTE", "1DTE"):
        raise ValueError("Expiry must be 0DTE or 1DTE.")

    result = {"expiry": expiry}

    for key, (low, high) in ranges.items():
        try:
            value = float(payload[key])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None

        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")

        result[key] = int(value) if value.is_integer() else value

    supplied = payload.get("entry_filters", {})
    if not isinstance(supplied, dict):
        raise ValueError("entry_filters must be an object.")

    filters = dict(ENTRY_FILTER_DEFAULTS)
    if "ema_gap_not_shrinking" in supplied:
        value = supplied["ema_gap_not_shrinking"]
        if not isinstance(value, bool):
            raise ValueError(
                "Entry filter ema_gap_not_shrinking must be true or false."
            )
        filters["ema_gap_not_shrinking"] = value

    # Legacy mandatory-filter settings are deliberately ignored.
    result["entry_filters"] = filters
    return result


def _evaluate_entry(
    *,
    direction,
    expiry,
    ema9,
    ema21,
    ema50,
    ema_gap_now,
    ema_gap_5m_ago,
    spy_price,
    five_min_trend,
    option_low,
    option_ask,
    bid,
    ask,
    quote_age_seconds,
    now=None,
    max_spread_pct=3.0,
    rebound_trigger_pct=3.0,
    entry_filters=None,
):
    filters = dict(ENTRY_FILTER_DEFAULTS)

    if isinstance(entry_filters, dict):
        value = entry_filters.get("ema_gap_not_shrinking")
        if isinstance(value, bool):
            filters["ema_gap_not_shrinking"] = value

    if direction not in ("CALL", "PUT"):
        return {
            "eligible": False,
            "direction": direction,
            "filter_results": {},
            "reasons": ["Direction must be CALL or PUT."],
        }

    # Time gate applies only to new entries.
    # Existing positions must be managed by a separate position-management flow.
    trading_status = get_trading_status(now)
    block_reason = get_entry_block_reason(expiry, now)

    trading_hours_ok = block_reason is None

    trading_hours_result = {
        "enabled": True,
        "passed": trading_hours_ok,
        "applied": True,
        "status": "pass" if trading_hours_ok else "fail",
        "expiry": expiry,
        "local_time": trading_status["local_time"],
    }

    if not trading_hours_ok:
        return {
            "eligible": False,
            "direction": direction,
            "entry_filters": filters,
            "filter_results": {
                "trading_hours": trading_hours_result,
            },
            "reasons": [block_reason],
        }

    required = (
        ema9,
        ema21,
        ema50,
        spy_price,
        five_min_trend,
        option_low,
        option_ask,
        bid,
        ask,
        quote_age_seconds,
    )

    if any(value is None for value in required):
        return {
            "eligible": False,
            "direction": direction,
            "entry_filters": filters,
            "filter_results": {
                "trading_hours": trading_hours_result,
            },
            "reasons": ["Missing market data; do not enter."],
        }

    if filters["ema_gap_not_shrinking"] and (
        ema_gap_now is None or ema_gap_5m_ago is None
    ):
        return {
            "eligible": False,
            "direction": direction,
            "entry_filters": filters,
            "filter_results": {
                "trading_hours": trading_hours_result,
            },
            "reasons": ["Missing EMA gap data; do not enter."],
        }

    if ask <= 0 or bid < 0 or ask < bid or option_low <= 0 or quote_age_seconds < 0:
        return {
            "eligible": False,
            "direction": direction,
            "entry_filters": filters,
            "filter_results": {
                "trading_hours": trading_hours_result,
            },
            "reasons": ["Invalid option quote; do not enter."],
        }

    ema_alignment_ok = (
        ema9 >= ema21 > ema50 and spy_price > ema21
        if direction == "CALL"
        else ema50 > ema21 >= ema9 and spy_price < ema21
    )

    expected_5m = "UP" if direction == "CALL" else "DOWN"
    five_min_ok = five_min_trend == expected_5m

    trigger_price = option_low * (1 + rebound_trigger_pct / 100)
    rebound_ok = option_ask >= trigger_price

    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint * 100 if midpoint > 0 else float("inf")
    quote_quality_ok = quote_age_seconds <= 10 and spread_pct <= max_spread_pct

    filter_results = {
        "trading_hours": trading_hours_result,
        "ema_alignment": {
            "enabled": True,
            "passed": bool(ema_alignment_ok),
            "applied": True,
            "status": "pass" if ema_alignment_ok else "fail",
        },
        "five_min_trend_confirmation": {
            "enabled": True,
            "passed": bool(five_min_ok),
            "applied": True,
            "status": "pass" if five_min_ok else "fail",
        },
        "option_rebound": {
            "enabled": True,
            "passed": bool(rebound_ok),
            "applied": True,
            "status": "pass" if rebound_ok else "fail",
        },
        "quote_quality": {
            "enabled": True,
            "passed": bool(quote_quality_ok),
            "applied": True,
            "status": "pass" if quote_quality_ok else "fail",
        },
    }

    reasons = []

    mandatory_checks = (
        (ema_alignment_ok, "1-minute EMA/price trend does not confirm direction."),
        (five_min_ok, "5-minute EMA21/EMA50 trend does not confirm direction."),
        (rebound_ok, "Option ask has not reached the rebound trigger."),
        (
            quote_quality_ok,
            "Quote quality failed: quote older than 10 seconds and/or bid/ask spread too wide.",
        ),
    )

    for passed, reason in mandatory_checks:
        if not passed:
            reasons.append(reason)

    gap_now = abs(ema_gap_now) if ema_gap_now is not None else None
    gap_previous = abs(ema_gap_5m_ago) if ema_gap_5m_ago is not None else None

    gap_ok = (
        gap_now >= gap_previous
        if gap_now is not None and gap_previous is not None
        else False
    )

    filter_results["ema_gap_not_shrinking"] = {
        "enabled": filters["ema_gap_not_shrinking"],
        "passed": bool(gap_ok),
        "applied": filters["ema_gap_not_shrinking"],
        "status": (
            ("pass" if gap_ok else "fail")
            if filters["ema_gap_not_shrinking"]
            else "disabled"
        ),
    }

    if filters["ema_gap_not_shrinking"] and not gap_ok:
        reasons.append(
            "Absolute EMA21/EMA50 separation is shrinking versus 5 minutes ago."
        )

    return {
        "eligible": not reasons,
        "direction": direction,
        "trigger_price": round(trigger_price, 4),
        "spread_pct": round(spread_pct, 3),
        "entry_filters": filters,
        "filter_results": filter_results,
        "reasons": reasons
        or ["All mandatory and enabled optional entry checks passed."],
    }


def evaluate_entry(**kwargs):
    from trade_logger import log_event

    decision = _evaluate_entry(**kwargs)

    log_event(
        "entry_decision",
        strategy="strategy1",
        inputs=kwargs,
        active_entry_filters=decision.get("entry_filters", ENTRY_FILTER_DEFAULTS),
        filter_results=decision.get("filter_results", {}),
        decision=decision,
    )

    return decision
