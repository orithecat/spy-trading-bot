"""Offline tests for the Strategy 1 account polling service."""

import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
from unittest.mock import patch

from account_monitor import AccountRiskPollingService


NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, *, at=NOW, status=200):
        self.payload = payload
        self.status_code = status
        self.headers = {"Date": format_datetime(at, usegmt=True)}

    def json(self):
        return self.payload


class FakeAccountApi:
    def __init__(self, *, balance=None, positions=None, at=NOW, fail=None):
        self.fail = fail
        self.calls = []
        self.responses = {
            "list": FakeResponse([{"account_id": "OFFLINE", "account_type": "MARGIN"}], at=at),
            "balance": FakeResponse(balance or {
                "total_asset_currency": "USD",
                "total_net_liquidation_value": "12500.50",
                "account_currency_assets": [{
                    "currency": "USD",
                    "option_buying_power": "2500.25",
                    "day_buying_power": "3000.25",
                    "overnight_buying_power": "2000.25",
                    "night_trading_buying_power": "1500.25",
                }],
            }, at=at),
            "positions": FakeResponse([] if positions is None else positions, at=at),
        }

    def _get(self, name, *args):
        self.calls.append((name, args))
        if self.fail == name:
            raise RuntimeError("sensitive details suppressed")
        return self.responses[name]

    def get_account_list(self):
        return self._get("list")

    def get_account_balance(self, account_id):
        return self._get("balance", account_id)

    def get_account_position(self, account_id):
        return self._get("positions", account_id)


def allowed_risk_result(*, updated_at=None, allowed=True):
    return {
        "allow_new_entries": allowed,
        "risk_status": "OK" if allowed else "LOCKED",
        "risk_reason": "mocked offline result",
        "account_data_updated_at": updated_at,
    }


class AccountRiskPollingServiceTests(unittest.TestCase):
    def service(self, **kwargs):
        return AccountRiskPollingService(
            poll_seconds=0.01,
            clock=lambda: NOW,
            **kwargs,
        )

    def test_success_updates_separate_account_data_and_uses_local_response_policy(self):
        api = FakeAccountApi()
        service = self.service(monotonic=lambda: 100.0)
        with patch("account_monitor.evaluate_account_risk", return_value=allowed_risk_result()):
            snapshot = service.poll_once(api)

        self.assertEqual(snapshot["account_data"].equity, Decimal("12500.50"))
        self.assertEqual(snapshot["account_data"].option_buying_power, Decimal("2500.25"))
        self.assertEqual(snapshot["account_data"].positions, ())
        self.assertTrue(service.allows_new_entries())
        self.assertEqual([call[0] for call in api.calls], ["list", "balance", "positions"])

    def test_api_failure_clears_stale_data_and_fails_closed(self):
        service = self.service(monotonic=lambda: 100.0)
        snapshot = service.poll_once(FakeAccountApi(fail="balance"))

        self.assertIsNone(snapshot["account_data"])
        self.assertFalse(snapshot["risk_result"]["allow_new_entries"])
        self.assertEqual(snapshot["last_error"], "AccountDataError")
        self.assertFalse(service.allows_new_entries())

    def test_stale_http_response_fails_closed(self):
        api = FakeAccountApi(at=NOW - timedelta(seconds=61))
        service = self.service(monotonic=lambda: 100.0)

        snapshot = service.poll_once(api)

        self.assertIsNone(snapshot["account_data"])
        self.assertFalse(snapshot["risk_result"]["allow_new_entries"])
        self.assertEqual(snapshot["last_error"], "AccountDataError")

    def test_missing_option_buying_power_fails_closed(self):
        balance = {
            "total_asset_currency": "USD",
            "total_net_liquidation_value": "12500.50",
            "account_currency_assets": [{"currency": "USD"}],
        }
        service = self.service(monotonic=lambda: 100.0)

        snapshot = service.poll_once(FakeAccountApi(balance=balance))

        self.assertIsNone(snapshot["account_data"])
        self.assertFalse(snapshot["risk_result"]["allow_new_entries"])

    def test_risk_lock_blocks_entries_but_keeps_valid_account_snapshot(self):
        api = FakeAccountApi()
        service = self.service(monotonic=lambda: 100.0)
        locked = allowed_risk_result(allowed=False)
        with patch("account_monitor.evaluate_account_risk", return_value=locked):
            snapshot = service.poll_once(api)

        self.assertIsNotNone(snapshot["account_data"])
        self.assertEqual(snapshot["risk_result"]["risk_status"], "LOCKED")
        self.assertFalse(service.allows_new_entries())

    def test_local_response_policy_allows_risk_grant_without_claiming_snapshot_time(self):
        api = FakeAccountApi()
        service = self.service(monotonic=lambda: 100.0)
        with patch(
            "account_monitor.evaluate_account_risk",
            return_value=allowed_risk_result(updated_at=None, allowed=True),
        ) as evaluate:
            snapshot = service.poll_once(api)

        self.assertIsNotNone(snapshot["account_data"])
        self.assertTrue(service.allows_new_entries())
        self.assertIsNone(evaluate.call_args.kwargs["account_data_updated_at"])
        self.assertFalse(evaluate.call_args.kwargs["require_snapshot_timestamp"])

    def test_cached_account_snapshot_expires(self):
        api = FakeAccountApi()
        current = [100.0]
        service = self.service(monotonic=lambda: current[0])
        with patch(
            "account_monitor.evaluate_account_risk",
            return_value=allowed_risk_result(updated_at=NOW, allowed=True),
        ):
            service.poll_once(api)
        current[0] = 160.0
        self.assertTrue(service.allows_new_entries())
        current[0] = 161.0

        self.assertFalse(service.allows_new_entries())

    def test_account_poll_interval_remains_thirty_seconds_and_local_ttl_is_sixty(self):
        service = AccountRiskPollingService(api_factory=lambda: None)
        self.assertEqual(service.poll_seconds, 30)
        self.assertEqual(service.max_data_age_seconds, 60)

    def test_start_is_idempotent_and_uses_one_monitor_thread(self):
        ready = threading.Event()
        release = threading.Event()
        api = FakeAccountApi()

        def factory():
            ready.set()
            release.wait(timeout=1)
            return api

        service = self.service(api_factory=factory)
        self.assertTrue(service.start())
        self.assertTrue(ready.wait(timeout=1))
        first_thread = service._thread
        self.assertFalse(service.start())
        self.assertIs(service._thread, first_thread)
        service.stop()
        release.set()
        first_thread.join(timeout=1)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
