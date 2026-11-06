"""Date-organized local JSONL event log for strategy decisions and trade lifecycle.

The live Webull engine is not connected in this build. When it is added, call
log_event for each order/replace/cancel/fill/exit/reconciliation event.
"""
import json
import math
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import BASE_DIR

MARKET_TZ = ZoneInfo("America/New_York")
LOG_DIR = BASE_DIR / "logs"
_LOCK = threading.Lock()
_SECRET_KEYS = {"app_key", "app_secret", "token", "access_token", "refresh_token", "password", "authorization"}


def _safe(value, key=""):
    if key.lower() in _SECRET_KEYS or any(word in key.lower() for word in ("secret", "password", "access_token", "refresh_token")):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return str(value)


def log_event(event_type, **fields):
    """Append one timestamped event to logs/YYYY-MM-DD.jsonl (New York date)."""
    now = datetime.now(MARKET_TZ)
    record = {
        "timestamp": now.isoformat(timespec="milliseconds"),
        "date": now.date().isoformat(),
        "timezone": "America/New_York",
        "event": str(event_type),
        **_safe(fields),
    }
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{now.date().isoformat()}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as exc:
        # Logging failure is visible in stderr but should not silently crash a strategy.
        import sys
        print(f"[trade-log-error] {exc}", file=sys.stderr)
