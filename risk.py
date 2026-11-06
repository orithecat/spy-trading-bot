"""Persistent daily equity guard helpers; no broker/account feed is connected yet."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def check_daily_equity(current_equity, settings, risk_state_path, now=None):
    """Update peak/start equity and latch daily protection when limits are hit.

    Call only with verified account equity from the broker. A latch is not cleared by
    editing settings or restarting the app. It resets on the next New York trading date.
    This helper does not send or confirm any orders.
    """
    now = now or datetime.now(MARKET_TZ)
    day_key = now.astimezone(MARKET_TZ).date().isoformat()
    path = Path(risk_state_path)
    try:
        old = json.loads(path.read_text())
        if not isinstance(old, dict): old = {}
    except (OSError, json.JSONDecodeError):
        old = {}
    if old.get("date") != day_key:
        old = {"date": day_key, "start_equity": float(current_equity),
               "peak_equity": float(current_equity), "latched": False, "reason": None}
    equity = float(current_equity)
    old["peak_equity"] = max(float(old.get("peak_equity", equity)), equity)
    if not old.get("latched"):
        start = float(old.get("start_equity", equity))
        peak = float(old["peak_equity"])
        profit_cap = float(settings["daily_profit_cap_pct"])
        drawdown_limit = float(settings["peak_drawdown_limit_pct"])
        if equity >= start * (1 + profit_cap / 100):
            old["latched"] = True
            old["reason"] = "Daily profit cap reached"
        elif equity <= peak * (1 - drawdown_limit / 100):
            old["latched"] = True
            old["reason"] = "Intraday peak drawdown limit reached"
    if old.get("latched") and not old.get("_logged_latch"):
        try:
            from trade_logger import log_event
            log_event("global_risk_latched", reason=old.get("reason"), current_equity=equity,
                      start_equity=old.get("start_equity"), peak_equity=old.get("peak_equity"),
                      settings=settings)
        finally:
            old["_logged_latch"] = True
    old["block_new_entries"] = bool(old.get("latched"))
    old["close_all_positions_required"] = bool(old.get("latched"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=2))
    tmp.replace(path)
    return old
