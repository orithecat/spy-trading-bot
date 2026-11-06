"""Independent, fail-closed account polling for Strategy 1."""

from datetime import datetime, timezone
import logging
import os
import threading
import time
from pathlib import Path

from account_data import (
    DEFAULT_MAX_RESPONSE_AGE_SECONDS,
    fetch_margin_account_data,
)
from account_risk_monitor import evaluate_account_risk


ACCOUNT_POLL_SECONDS = 30
DEFAULT_LOCAL_RESPONSE_TTL_SECONDS = 60


class _SerializedOrderApi:
    def __init__(self, order_api, request_lock):
        self._order_api = order_api
        self._request_lock = request_lock

    def place_order(self, *args, **kwargs):
        with self._request_lock:
            return self._order_api.place_order(*args, **kwargs)

    def get_order_detail(self, *args, **kwargs):
        with self._request_lock:
            return self._order_api.get_order_detail(*args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        with self._request_lock:
            return self._order_api.cancel_order(*args, **kwargs)

    def list_order_open(self, *args, **kwargs):
        with self._request_lock:
            return self._order_api.list_order_open(*args, **kwargs)


class _WebullAccountOrderApi:
    """Share one authenticated ApiClient between account reads and orders."""

    def __init__(self, client):
        from webull.trade.trade.v2.account_info_v2 import AccountV2
        from webull.trade.trade.v3.order_operation_v3 import OrderOperationV3

        self._request_lock = threading.RLock()
        self._account_api = AccountV2(client)
        self.order_v3 = _SerializedOrderApi(OrderOperationV3(client), self._request_lock)

    def get_account_list(self):
        with self._request_lock:
            return self._account_api.get_account_list()

    def get_account_balance(self, account_id):
        with self._request_lock:
            return self._account_api.get_account_balance(account_id)

    def get_account_position(self, account_id):
        with self._request_lock:
            return self._account_api.get_account_position(account_id)


def create_webull_account_api():
    """Create the project's authenticated, read-only Webull account client."""
    logging.getLogger("webull").setLevel(logging.CRITICAL)

    from dotenv import load_dotenv
    from webull.core.client import ApiClient
    from webull.core.http.initializer.token.token_manager import TokenManager

    project_dir = Path(__file__).resolve().parent
    load_dotenv(project_dir / ".env")
    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError("Webull account credentials are unavailable.")

    client = ApiClient(app_key, app_secret, "us")
    TokenManager().init_token(client)
    return _WebullAccountOrderApi(client)


class AccountRiskPollingService:
    """Poll account data on a dedicated thread and keep its state isolated."""

    def __init__(
        self,
        api_factory=create_webull_account_api,
        *,
        poll_seconds=ACCOUNT_POLL_SECONDS,
        max_data_age_seconds=DEFAULT_LOCAL_RESPONSE_TTL_SECONDS,
        risk_state_path=None,
        clock=None,
        monotonic=None,
    ):
        if poll_seconds <= 0 or max_data_age_seconds <= 0:
            raise ValueError("Account monitor intervals must be positive.")
        self.api_factory = api_factory
        self.poll_seconds = poll_seconds
        self.max_data_age_seconds = max_data_age_seconds
        self.risk_state_path = risk_state_path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic

        self._condition = threading.Condition()
        self._requested = False
        self._thread = None
        self._api = None
        self._snapshot_lock = threading.Lock()
        self._snapshot = {
            "account_data": None,
            "risk_result": {
                "allow_new_entries": False,
                "risk_status": "BLOCKED",
                "risk_reason": "Account data has not been verified.",
                "account_data_updated_at": None,
            },
            "polled_at_monotonic": None,
            "last_error": None,
        }

    def start(self):
        """Start at most one polling thread; repeated starts reuse it."""
        with self._condition:
            self._requested = True
            if self._thread is not None and self._thread.is_alive():
                self._condition.notify_all()
                return False
            self._thread = threading.Thread(
                target=self._run,
                name="strategy1-account-risk-monitor",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self):
        with self._condition:
            self._requested = False
            self._condition.notify_all()

    def _run(self):
        try:
            try:
                account_api = self.api_factory()
                with self._snapshot_lock:
                    self._api = account_api
            except Exception as exc:
                self._record_failure("Account client initialization failed.", exc)
                return

            while True:
                with self._condition:
                    if not self._requested:
                        self._thread = None
                        return

                self.poll_once(account_api)

                with self._condition:
                    if not self._requested:
                        self._thread = None
                        return
                    self._condition.wait(timeout=self.poll_seconds)
        finally:
            with self._condition:
                if not self._requested and self._thread is threading.current_thread():
                    self._thread = None

    def poll_once(self, account_api):
        """Perform one account read and risk evaluation; errors fail closed."""
        try:
            current_time = self.clock()
            account_data = fetch_margin_account_data(
                account_api,
                now=current_time,
                max_response_age_seconds=max(
                    DEFAULT_MAX_RESPONSE_AGE_SECONDS,
                    self.max_data_age_seconds,
                ),
            )
            response_completed_monotonic = self.monotonic()
            # Webull does not provide a snapshot timestamp. This monitor uses
            # the local monotonic age of a complete successful response instead;
            # it does not claim to verify upstream snapshot freshness.
            risk_result = evaluate_account_risk(
                account_data,
                account_data_updated_at=None,
                now=self.clock(),
                risk_state_path=self.risk_state_path,
                max_response_age_seconds=max(
                    DEFAULT_MAX_RESPONSE_AGE_SECONDS,
                    self.max_data_age_seconds,
                ),
                require_snapshot_timestamp=False,
            )
            snapshot = {
                "account_data": account_data,
                "risk_result": risk_result,
                "polled_at_monotonic": response_completed_monotonic,
                "last_error": None,
            }
        except Exception as exc:
            self._record_failure("Account data poll failed.", exc)
            return self.get_snapshot()

        with self._snapshot_lock:
            self._snapshot = snapshot
        return self.get_snapshot()

    def _record_failure(self, reason, exc):
        # Store only the exception class; SDK exceptions may contain request data.
        error_type = type(exc).__name__
        snapshot = {
            "account_data": None,
            "risk_result": {
                "allow_new_entries": False,
                "risk_status": "BLOCKED",
                "risk_reason": reason,
                "account_data_updated_at": None,
            },
            "polled_at_monotonic": self.monotonic(),
            "last_error": error_type,
        }
        with self._snapshot_lock:
            self._snapshot = snapshot

    def get_snapshot(self):
        with self._snapshot_lock:
            snapshot = dict(self._snapshot)
            snapshot["risk_result"] = dict(self._snapshot["risk_result"])
            return snapshot

    def get_order_api(self):
        """Return the already authenticated SDK v3 order API, if initialized."""
        with self._snapshot_lock:
            return getattr(self._api, "order_v3", None)

    def snapshot_is_fresh(self):
        """Check local receipt age without claiming upstream snapshot freshness."""
        snapshot = self.get_snapshot()
        polled_at = snapshot.get("polled_at_monotonic")
        if polled_at is None or snapshot.get("account_data") is None:
            return False
        age = self.monotonic() - polled_at
        return 0 <= age <= self.max_data_age_seconds

    def allows_new_entries(self):
        snapshot = self.get_snapshot()
        polled_at = snapshot["polled_at_monotonic"]
        if polled_at is None:
            return False
        age = self.monotonic() - polled_at
        if age < 0 or age > self.max_data_age_seconds:
            return False
        risk_result = snapshot["risk_result"]
        return (
            snapshot["account_data"] is not None
            and risk_result.get("allow_new_entries") is True
        )
