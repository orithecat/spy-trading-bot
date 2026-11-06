from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import mimetypes
import uuid

from config import BASE_DIR, DEFAULT_SETTINGS, HOST, PORT, STRATEGIES
from state import save_settings, settings, snapshot, state, state_lock
from trade_logger import log_event
from strategies.strategy1 import validate_settings
from strategy1_runner import runner

RISK_STATE_FILE = BASE_DIR / "data" / "risk_state.json"


EMERGENCY_STATE_FILE = BASE_DIR / "data" / "emergency_state.json"


def load_emergency_latch():
    if not EMERGENCY_STATE_FILE.exists():
        return False
    try:
        data = json.loads(EMERGENCY_STATE_FILE.read_text(encoding="utf-8"))
        return data.get("latched") is True
    except (OSError, ValueError, TypeError):
        return True


def persist_emergency_latch(active, emergency_id=None):
    EMERGENCY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if active:
        temp_file = EMERGENCY_STATE_FILE.with_suffix(".json.tmp")
        temp_file.write_text(
            json.dumps({
                "latched": True,
                "emergency_id": emergency_id or uuid.uuid4().hex,
            }),
            encoding="utf-8",
        )
        temp_file.replace(EMERGENCY_STATE_FILE)
    else:
        EMERGENCY_STATE_FILE.unlink(missing_ok=True)


def resume_persisted_emergency_stop():
    """Resume a previously requested account liquidation after restart."""
    if not load_emergency_latch():
        return False
    emergency_id = None
    try:
        saved = json.loads(EMERGENCY_STATE_FILE.read_text(encoding="utf-8"))
        candidate = saved.get("emergency_id")
        if isinstance(candidate, str) and candidate:
            emergency_id = candidate
    except (OSError, ValueError, TypeError):
        pass
    if emergency_id is None:
        emergency_id = uuid.uuid4().hex
        persist_emergency_latch(True, emergency_id=emergency_id)
    runner.emergency_stop(emergency_id=emergency_id)
    return True


if load_emergency_latch():
    with state_lock:
        for strategy in state["strategies"].values():
            strategy["running"] = False
            strategy["emergency_stop"] = True
        state["active_strategy"] = None



def load_risk_latch():
    try:
        saved = json.loads(RISK_STATE_FILE.read_text())
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if saved.get("date") != today:
            return False, None
        return bool(saved.get("latched")), saved.get("reason")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False, None


_latched, _reason = load_risk_latch()
state["risk_latched"] = _latched
if _latched:
    state["risk_message"] = f"Daily risk lock is active: {_reason or 'limit reached'}. It will not clear when settings are edited."


def render_page(name, script=""):
    base = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    body = (BASE_DIR / "templates" / name).read_text(encoding="utf-8")
    return base.replace("{{CONTENT}}", body).replace("{{SCRIPTS}}", script)


class Handler(BaseHTTPRequestHandler):
    def send_data(self, data, content_type="application/json; charset=utf-8", status=200):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_data(json.dumps(data), status=status)

    def do_GET(self):
        path = urlparse(self.path).path
        pages = {
            "/": ("index.html", "<script src=\"/static/js/app.js\" defer></script>"),
            "/strategy1": ("strategies/strategy1.html", "<script src=\"/static/js/strategy1.js\" defer></script>"),
            "/strategy2": ("strategies/placeholder.html", ""),
            "/strategy3": ("strategies/placeholder.html", ""),
        }
        if path in pages:
            name, script = pages[path]
            return self.send_data(render_page(name, script), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            static_root = (BASE_DIR / "static").resolve()
            target = (BASE_DIR / path.lstrip("/")).resolve()
            if not target.is_relative_to(static_root) or not target.is_file():
                return self.send_json({"error": "Not found"}, 404)
            return self.send_data(target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        if path == "/api/status":
            return self.send_json(snapshot())
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        allowed = {"/api/control", "/api/strategy1/settings", "/api/global/settings"}
        if path not in allowed:
            return self.send_json({"error": "Not found"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 8192:
                return self.send_json({"error": "Invalid request size"}, 400)
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"error": "Invalid JSON"}, 400)

        if path == "/api/global/settings":
            try:
                new = {}
                ranges = {
                    "daily_profit_cap_pct": (0.1, 1000),
                    "peak_drawdown_limit_pct": (0.1, 100),
                }
                for key, (low, high) in ranges.items():
                    value = float(payload[key])
                    if not low <= value <= high:
                        raise ValueError(f"{key} must be between {low} and {high}.")
                    new[key] = int(value) if value.is_integer() else value
            except (KeyError, TypeError, ValueError) as exc:
                return self.send_json({"error": str(exc) or "Global risk settings must be valid numbers."}, 400)
            with state_lock:
                settings["global"].update(new)
                save_settings()
                log_event("global_settings_saved", settings=new)
                # Intentionally do not clear state['risk_latched'] here.
                result = snapshot()
            return self.send_json(result)

        if path == "/api/strategy1/settings":
            try:
                new = validate_settings(payload)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            with state_lock:
                old_settings = {k: (dict(v) if isinstance(v, dict) else v)
                                for k, v in settings["strategy1"].items()}
                old_filters = dict(old_settings.get("entry_filters", {}))
                new_filters = dict(new.get("entry_filters", old_filters))
                changed_filters = {
                    key: {"old": old_filters.get(key, True), "new": new_filters[key]}
                    for key in new_filters
                    if old_filters.get(key, True) != new_filters[key]
                }
                settings["strategy1"].update(new)
                save_settings()
                log_event(
                    "strategy_settings_saved", strategy="strategy1",
                    old_settings=old_settings, new_settings=settings["strategy1"],
                    changed_entry_filters=changed_filters,
                    active_entry_filters=settings["strategy1"].get("entry_filters", {}),
                )
                if changed_filters:
                    log_event(
                        "entry_filters_updated", strategy="strategy1",
                        changed_filters=changed_filters,
                        old_entry_filters=old_filters,
                        new_entry_filters=new_filters,
                        active_entry_filters=new_filters,
                    )
                result = snapshot()
            return self.send_json(result)

        key, action = payload.get("strategy"), payload.get("action")
        if key not in STRATEGIES:
            return self.send_json({"error": "Invalid strategy"}, 400)
        if key != "strategy1":
            return self.send_json({"error": "Only Strategy 1 is enabled for development in this version."}, 409)

        with state_lock:
            item = state["strategies"][key]

            if action == "start":
                if state["risk_latched"]:
                    return self.send_json({
                        "error": "Global daily risk lock is active; new entries are blocked."
                    }, 409)

                was_emergency_stopped = any(
                    strategy["emergency_stop"]
                    for strategy in state["strategies"].values()
                )
                emergency_pending = (
                    load_emergency_latch()
                    or was_emergency_stopped
                    or getattr(runner, "_emergency", False)
                )
                if emergency_pending and not runner.emergency_stop_complete():
                    return self.send_json({
                        "error": (
                            "Emergency Stop is still unresolved. START cannot clear its latch "
                            "until account positions and orders are confirmed reconciled."
                        )
                    }, 409)

                try:
                    persist_emergency_latch(False)
                except OSError as exc:
                    return self.send_json({
                        "error": f"Could not clear emergency stop: {exc}"
                    }, 500)

                for strategy in state["strategies"].values():
                    strategy["emergency_stop"] = False

                item["running"] = True
                state["active_strategy"] = key

                if was_emergency_stopped:
                    log_event("emergency_stop_cleared", strategy=key, mode="live")

                try:
                    runner.start()
                except Exception as exc:
                    item["running"] = False
                    state["active_strategy"] = None
                    item["message"] = f"Could not start Strategy 1 runner: {exc}"
                    return self.send_json({"error": item["message"]}, 500)

                log_event("strategy_started", strategy=key, mode="live")
                if getattr(runner, "real_trading_authorized", False) is True:
                    item["message"] = (
                        "Strategy 1 is active. Qualifying signals can submit real Webull option orders."
                    )
                else:
                    item["message"] = (
                        "Strategy 1 is managing existing positions only. Real BUY orders are not authorized."
                    )

            elif action == "stop":
                item["running"] = False
                if state["active_strategy"] == key:
                    state["active_strategy"] = None

                runner.stop_new_entries()
                item["message"] = (
                    "Strategy 1 stopped accepting new entries. "
                    "Pending entry remainders will be cancelled; existing live positions continue to be managed."
                )
                log_event("strategy_stopped", strategy=key, mode="live")

            elif action == "emergency_stop":
                emergency_id = uuid.uuid4().hex
                try:
                    persist_emergency_latch(True, emergency_id=emergency_id)
                except OSError as exc:
                    return self.send_json({
                        "error": f"Could not persist emergency stop: {exc}"
                    }, 500)

                for strategy in state["strategies"].values():
                    strategy["running"] = False
                    strategy["emergency_stop"] = True

                state["active_strategy"] = None

                runner.emergency_stop(emergency_id=emergency_id)
                log_event(
                    "emergency_stop_requested",
                    strategy=key,
                    scope="account_wide",
                    mode="live",
                )
                item["message"] = (
                    "Account-wide emergency stop latched. New entries are disabled; "
                    "the runner will cancel pending entries and submit real closing orders for Strategy 1 positions. "
                    "Click START to clear the latch."
                )

            else:
                return self.send_json({"error": "Unknown action"}, 400)

            result = snapshot()
        return self.send_json(result)

    def log_message(self, fmt, *args):
        print("[Dashboard]", fmt % args)


if __name__ == "__main__":
    # A previously requested Emergency Stop remains active across restarts.
    # Ordinary application startup still does not start trading.
    resume_persisted_emergency_stop()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Dashboard: http://{HOST}:{PORT}")
    print("Strategy 1 uses real Webull orders after START and when all risk gates pass.")
    print(f"Daily JSONL logs: {BASE_DIR / 'logs'} (one file per New York date)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
