import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path

from account_data import (
    AccountDataError,
    AccountPosition,
    MarginAccountData,
    fetch_margin_account_data,
)
from account_risk_monitor import evaluate_account_risk
import state as project_state


NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


def make_position(*, unrealized="250"):
    return AccountPosition(
        symbol="MOCK-EQUITY",
        instrument_type="EQUITY",
        currency="USD",
        quantity=Decimal("2"),
        last_price=Decimal("125"),
        cost_price=Decimal("100"),
        unrealized_profit_loss=Decimal(unrealized),
        option_strategy=None,
        legs=(),
    )


def make_account_data(
    *,
    equity="10000",
    positions=(),
    account_type="MARGIN",
    currency="USD",
    as_of=NOW,
    buying_power=None,
    option_buying_power="4000",
    day_buying_power="3000",
    overnight_buying_power="2000",
    night_trading_buying_power="1000",
):
    return MarginAccountData(
        account_type=account_type,
        currency=currency,
        equity=(
            Decimal(equity)
            if isinstance(equity, str) and equity != "not-a-number"
            else equity
        ),
        buying_power=(Decimal(buying_power) if buying_power is not None else None),
        option_buying_power=Decimal(option_buying_power),
        day_buying_power=(Decimal(day_buying_power) if day_buying_power is not None else None),
        overnight_buying_power=(Decimal(overnight_buying_power) if overnight_buying_power is not None else None),
        positions=tuple(positions),
        as_of=as_of,
        night_trading_buying_power=(Decimal(night_trading_buying_power) if night_trading_buying_power is not None else None),
    )


def valid_risk_settings(profit="25", drawdown="10"):
    return {
        "daily_profit_cap_pct": Decimal(profit),
        "peak_drawdown_limit_pct": Decimal(drawdown),
    }


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {"Date": format_datetime(NOW, usegmt=True)}
        self.payload = payload

    def json(self):
        return self.payload


class DuplicateMarginAccountApi:
    def get_account_list(self):
        return FakeResponse([
            {"account_type": "MARGIN", "account_id": "MOCK_ONE"},
            {"account_type": "MARGIN", "account_id": "MOCK_TWO"},
        ])

    def get_account_balance(self, account_id):
        raise AssertionError("ambiguous account must not be queried")

    def get_account_position(self, account_id):
        raise AssertionError("ambiguous account must not be queried")


class AccountRiskMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="account-risk-monitor-")
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "risk_state.json"

    def evaluate(self, account_data, *, at=NOW, updated_at=NOW, risk=None):
        return evaluate_account_risk(
            account_data,
            account_data_updated_at=updated_at,
            now=at,
            risk_settings=risk,
            risk_state_path=self.state_path,
        )

    def test_normal_margin_account_returns_structured_allow_result(self):
        data = make_account_data(positions=(make_position(),))
        result = self.evaluate(data)

        self.assertTrue(result["allow_new_entries"])
        self.assertEqual(result["risk_status"], "OK")
        self.assertEqual(result["risk_reason"], "Risk limits are not reached.")
        self.assertTrue(result["margin_account_verified"])
        self.assertEqual(result["current_equity"], Decimal("10000"))
        self.assertEqual(result["starting_equity"], Decimal("10000"))
        self.assertEqual(result["peak_equity"], Decimal("10000"))
        self.assertEqual(result["daily_profit_pct"], Decimal("0"))
        self.assertEqual(result["drawdown_from_peak_pct"], Decimal("0"))

    def test_cash_missing_unverified_and_unclear_accounts_are_blocked(self):
        inputs = (
            make_account_data(account_type="CASH"),
            None,
            {"account_type": "MARGIN", "equity": "10000"},
            make_account_data(account_type=None),
        )
        for data in inputs:
            with self.subTest(data_type=type(data).__name__, account_type=getattr(data, "account_type", None)):
                result = self.evaluate(data)
                self.assertFalse(result["allow_new_entries"])
                self.assertFalse(result["margin_account_verified"])
                self.assertEqual(result["risk_status"], "BLOCKED")

    def test_duplicate_margin_accounts_are_rejected_by_account_validation(self):
        with self.assertRaises(AccountDataError):
            fetch_margin_account_data(DuplicateMarginAccountApi(), now=NOW)

    def test_wrong_currency_is_blocked(self):
        result = self.evaluate(make_account_data(currency="CAD"))
        self.assertFalse(result["allow_new_entries"])
        self.assertIn("USD", result["risk_reason"])

    def test_missing_non_numeric_and_negative_equity_are_blocked(self):
        for equity in (None, "not-a-number", "-0.01", "0"):
            with self.subTest(equity=equity):
                data = make_account_data(equity=equity)
                result = self.evaluate(data)
                self.assertFalse(result["allow_new_entries"])
                self.assertEqual(result["risk_status"], "BLOCKED")

    def test_buying_power_fields_remain_separate(self):
        data = make_account_data(
            buying_power="91.11",
            option_buying_power="82.22",
            day_buying_power="73.33",
            overnight_buying_power="64.44",
        )
        result = self.evaluate(data)
        self.assertEqual(result["buying_power"], Decimal("91.11"))
        self.assertEqual(result["option_buying_power"], Decimal("82.22"))
        self.assertEqual(result["day_buying_power"], Decimal("73.33"))
        self.assertEqual(result["overnight_buying_power"], Decimal("64.44"))
        self.assertEqual(result["night_trading_buying_power"], Decimal("1000"))

    def test_non_option_buying_power_fields_do_not_block_risk_evaluation(self):
        data = make_account_data(
            buying_power=Decimal("NaN"),
            day_buying_power=Decimal("-1"),
            overnight_buying_power=None,
            night_trading_buying_power=None,
        )

        result = self.evaluate(data)

        self.assertTrue(result["allow_new_entries"])
        self.assertEqual(result["option_buying_power"], Decimal("4000"))

    def test_fresh_http_date_does_not_substitute_for_snapshot_update_time(self):
        data = make_account_data(as_of=NOW)
        result = self.evaluate(data, updated_at=None)
        self.assertFalse(result["allow_new_entries"])
        self.assertIn("HTTP response time is not proof", result["risk_reason"])

    def test_explicit_local_response_policy_does_not_claim_snapshot_timestamp(self):
        result = evaluate_account_risk(
            make_account_data(),
            account_data_updated_at=None,
            now=NOW,
            risk_settings=valid_risk_settings(),
            risk_state_path=self.state_path,
            require_snapshot_timestamp=False,
        )
        self.assertTrue(result["allow_new_entries"])
        self.assertIsNone(result["account_data_updated_at"])
        self.assertEqual(result["api_response_time"], NOW)

    def test_stale_response_and_stale_snapshot_are_blocked_independently(self):
        stale_response = make_account_data(as_of=NOW - timedelta(seconds=31))
        result = self.evaluate(stale_response)
        self.assertFalse(result["allow_new_entries"])
        self.assertIn("API response is stale", result["risk_reason"])

        fresh_response = make_account_data(as_of=NOW)
        result = self.evaluate(fresh_response, updated_at=NOW - timedelta(seconds=31))
        self.assertFalse(result["allow_new_entries"])
        self.assertIn("snapshot data is stale", result["risk_reason"])

    def test_starting_equity_is_set_once_for_the_current_market_date(self):
        first = self.evaluate(make_account_data(equity="10000"), at=NOW)
        second_time = NOW + timedelta(hours=1)
        second = self.evaluate(
            make_account_data(equity="11000", as_of=second_time),
            at=second_time,
            updated_at=second_time,
        )
        self.assertEqual(first["starting_equity"], Decimal("10000"))
        self.assertEqual(second["starting_equity"], Decimal("10000"))
        self.assertEqual(second["daily_profit_pct"], Decimal("10"))

    def test_persisted_project_global_limits_are_used_by_default(self):
        data = make_account_data(equity="10000")
        initial = self.evaluate(data, risk=None)
        cap = Decimal(str(project_state.settings["global"]["daily_profit_cap_pct"]))
        drawdown = Decimal(str(project_state.settings["global"]["peak_drawdown_limit_pct"]))
        self.assertEqual(initial["starting_equity"], Decimal("10000"))

        at_cap = Decimal("10000") * (Decimal("1") + cap / Decimal("100"))
        triggered = self.evaluate(
            make_account_data(equity=str(at_cap), as_of=NOW + timedelta(minutes=1)),
            at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
            risk=None,
        )
        self.assertFalse(triggered["allow_new_entries"])
        self.assertEqual(triggered["risk_status"], "LOCKED")
        self.assertEqual(triggered["daily_profit_pct"], cap)
        self.assertGreater(drawdown, Decimal("0"))

    def test_daily_profit_limit_latches_and_stays_latched_if_settings_change(self):
        self.evaluate(make_account_data(equity="10000"))
        reached_at = NOW + timedelta(minutes=1)
        reached = self.evaluate(
            make_account_data(equity="12500", as_of=reached_at),
            at=reached_at,
            updated_at=reached_at,
        )
        self.assertFalse(reached["allow_new_entries"])
        self.assertEqual(reached["daily_profit_pct"], Decimal("25"))
        self.assertEqual(reached["risk_reason"], "Daily profit cap reached.")

        later = NOW + timedelta(minutes=2)
        still_locked = self.evaluate(
            make_account_data(equity="11000", as_of=later),
            at=later,
            updated_at=later,
            risk=valid_risk_settings(profit="1000", drawdown="100"),
        )
        self.assertFalse(still_locked["allow_new_entries"])
        self.assertEqual(still_locked["risk_status"], "LOCKED")

    def test_peak_updates_and_drawdown_uses_highest_equity(self):
        first = self.evaluate(make_account_data(equity="10000"))
        peak_time = NOW + timedelta(minutes=1)
        peak = self.evaluate(
            make_account_data(equity="12000", as_of=peak_time),
            at=peak_time,
            updated_at=peak_time,
        )
        self.assertEqual(first["peak_equity"], Decimal("10000"))
        self.assertEqual(peak["peak_equity"], Decimal("12000"))
        self.assertEqual(peak["drawdown_from_peak_pct"], Decimal("0"))

        dip_time = NOW + timedelta(minutes=2)
        dip = self.evaluate(
            make_account_data(equity="10800", as_of=dip_time),
            at=dip_time,
            updated_at=dip_time,
        )
        self.assertEqual(dip["starting_equity"], Decimal("10000"))
        self.assertEqual(dip["peak_equity"], Decimal("12000"))
        self.assertEqual(dip["drawdown_from_peak_pct"], Decimal("10"))
        self.assertFalse(dip["allow_new_entries"])
        self.assertEqual(dip["risk_reason"], "Peak drawdown limit reached.")

    def test_peak_can_recover_but_does_not_reset_starting_equity(self):
        self.evaluate(make_account_data(equity="10000"))
        update_time = NOW + timedelta(minutes=1)
        higher = self.evaluate(
            make_account_data(equity="11000", as_of=update_time),
            at=update_time,
            updated_at=update_time,
        )
        self.assertEqual(higher["starting_equity"], Decimal("10000"))
        self.assertEqual(higher["peak_equity"], Decimal("11000"))

    def test_unrealized_pnl_is_reported_but_not_added_to_equity_again(self):
        data = make_account_data(equity="10000", positions=(make_position(unrealized="250"),))
        result = self.evaluate(data)
        self.assertEqual(result["current_equity"], Decimal("10000"))
        self.assertEqual(result["unrealized_pnl"], Decimal("250"))
        self.assertTrue(result["equity_includes_unrealized_pnl"])
        self.assertEqual(result["starting_equity"], Decimal("10000"))

    def test_malformed_persisted_state_fails_closed_without_replacing_it(self):
        original = '{not-json'
        self.state_path.write_text(original, encoding="utf-8")
        result = self.evaluate(make_account_data())
        self.assertFalse(result["allow_new_entries"])
        self.assertEqual(result["risk_status"], "BLOCKED")
        self.assertIn("Persisted risk state", result["risk_reason"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original)

    def test_risk_state_persists_values_without_account_identifiers(self):
        self.evaluate(make_account_data(equity="10000"))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["start_equity"], "10000")
        self.assertEqual(saved["peak_equity"], "10000")
        self.assertTrue(saved["block_new_entries"] is False)
        self.assertNotIn("account_id", saved)

    def test_next_new_york_date_starts_a_new_daily_baseline(self):
        first = self.evaluate(make_account_data(equity="10000"))
        next_day = NOW + timedelta(days=1)
        second = self.evaluate(
            make_account_data(equity="10500", as_of=next_day),
            at=next_day,
            updated_at=next_day,
        )
        self.assertEqual(first["starting_equity"], Decimal("10000"))
        self.assertEqual(second["starting_equity"], Decimal("10500"))
        self.assertEqual(second["peak_equity"], Decimal("10500"))
        self.assertEqual(second["daily_profit_pct"], Decimal("0"))

    def test_positions_and_option_buying_power_are_required_for_trust(self):
        for data in (
            replace(make_account_data(), option_buying_power=None),
            replace(make_account_data(), option_buying_power=Decimal("-1")),
            replace(make_account_data(), positions=(object(),)),
        ):
            with self.subTest(data=data):
                result = self.evaluate(data)
                self.assertFalse(result["allow_new_entries"])


if __name__ == "__main__":
    unittest.main()
