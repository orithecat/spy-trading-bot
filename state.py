import json
import threading
from config import BASE_DIR, DEFAULT_SETTINGS, STRATEGIES

SETTINGS_FILE = BASE_DIR / "data" / "settings.json"
state_lock = threading.RLock()
state = {
    "active_strategy": None,
    "today_total_pnl": None,
    "risk_latched": False,
    "risk_message": "Account equity is not connected; global risk rules are not monitoring live funds yet.",
    "strategies": {
        key: {
            "running": False,
            "emergency_stop": False,
            "message": "Strategy stopped. Press START only when you intend to allow real orders.",
            "signal": "Waiting for live market data",
            "spy_price": None,
            "ema9": None,
            "ema21": None,
            "ema50": None,
            "ema_gap": None,
            "five_min_trend": "Not connected",
            "candidate_option": None,
            "tracking_low": None,
            "trigger_price": None,
            "order_status": "No live order activity",
            "entry_price": None,
            "quantity": None,
            "unrealized_pnl": None,
        }
        for key in STRATEGIES
    },
}


def _read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings():
    saved = _read_json(SETTINGS_FILE)
    old_s1 = saved.get("strategy1", {}) if isinstance(saved.get("strategy1"), dict) else {}
    global_saved = saved.get("global", {}) if isinstance(saved.get("global"), dict) else {}
    # Migrate the old daily limits to the new shared global settings once.
    # The new profit cap is explicitly 30%; do not carry forward the old +21% target.
    if "daily_profit_cap_pct" not in global_saved:
        global_saved["daily_profit_cap_pct"] = DEFAULT_SETTINGS["global"]["daily_profit_cap_pct"]
    if "peak_drawdown_limit_pct" not in global_saved:
        if "daily_loss_limit_pct" in global_saved:
            global_saved["peak_drawdown_limit_pct"] = global_saved["daily_loss_limit_pct"]
        elif "daily_loss_limit_pct" in old_s1:
            global_saved["peak_drawdown_limit_pct"] = old_s1["daily_loss_limit_pct"]
        else:
            global_saved["peak_drawdown_limit_pct"] = DEFAULT_SETTINGS["global"]["peak_drawdown_limit_pct"]
    saved["global"] = global_saved

    result = {}
    for key, defaults in DEFAULT_SETTINGS.items():
        result[key] = dict(defaults)
        current = saved.get(key)
        if isinstance(current, dict):
            result[key].update({k: v for k, v in current.items() if k in defaults})
            # Preserve newly added filter defaults when loading older settings files.
            if key == "strategy1":
                saved_filters = current.get("entry_filters", {})
                if isinstance(saved_filters, dict):
                    merged_filters = dict(DEFAULT_SETTINGS["strategy1"]["entry_filters"])
                    merged_filters.update({k: v for k, v in saved_filters.items()
                                           if k in merged_filters and isinstance(v, bool)})
                    result[key]["entry_filters"] = merged_filters
    return result


settings = load_settings()


def save_settings():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    tmp.replace(SETTINGS_FILE)


def snapshot():
    with state_lock:
        return {
            "active_strategies": [
                key for key, item in state["strategies"].items()
                if item["running"]
            ],
            "active_strategy": (
                next(
                    (key for key, item in state["strategies"].items()
                     if item["running"]),
                    None,
                )
                if sum(
                    1 for item in state["strategies"].values()
                    if item["running"]
                ) == 1
                else None
            ),
            "today_total_pnl": state["today_total_pnl"],
            "risk_latched": state["risk_latched"],
            "risk_message": state["risk_message"],
            "strategies": {k: dict(v) for k, v in state["strategies"].items()},
            "parameters": {k: dict(v) for k, v in settings.items()},
        }
