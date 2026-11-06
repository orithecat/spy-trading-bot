import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
from unittest.mock import Mock

from webull.trade.trade.v2.account_info_v2 import AccountV2

from account_data import AccountDataError, fetch_margin_account_data


NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
MARGIN_ID = "MOCK_MARGIN_ACCOUNT"


class FakeResponse:
    def __init__(self, payload, *, status=200, response_time=NOW, bad_json=False, headers=None):
        self.status_code = status
        self.headers = headers if headers is not None else {
            "Date": format_datetime(response_time, usegmt=True),
        }
        self.payload = payload
        self.bad_json = bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("mock response parse failure")
        return self.payload


class FakeApiClient:
    """Fake SDK transport: records requests and cannot contact a network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.place_order = Mock(side_effect=AssertionError("order operation called"))
        self.submit_order = Mock(side_effect=AssertionError("order operation called"))
        self.modify_order = Mock(side_effect=AssertionError("order operation called"))
        self.replace_order = Mock(side_effect=AssertionError("order operation called"))
        self.cancel_order = Mock(side_effect=AssertionError("order operation called"))

    def get_response(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected SDK request")
        return self.responses.pop(0)


def account_list(*accounts):
    return list(accounts)


def margin_account(account_id=MARGIN_ID):
    return {"account_id": account_id, "account_type": "MARGIN"}


def cash_account(account_id="MOCK_CASH_ACCOUNT"):
    return {"account_id": account_id, "account_type": "CASH"}


def valid_balance():
    return {
        "total_asset_currency": "USD",
        "total_net_liquidation_value": "12345.67",
        "account_currency_assets": [{
            "currency": "USD",
            "option_buying_power": "802.34",
            "day_buying_power": "703.45",
            "overnight_buying_power": "604.56",
            "night_trading_buying_power": "505.67",
        }],
    }


def equity_position():
    return {
        "currency": "USD",
        "quantity": "4",
        "symbol": "MOCK",
        "instrument_type": "EQUITY",
        "last_price": "12.50",
        "cost_price": "11.25",
        "unrealized_profit_loss": "5.00",
    }


def option_position():
    return {
        "currency": "USD",
        "quantity": "2",
        "symbol": "MOCK-OPTION",
        "option_strategy": "SINGLE",
        "instrument_type": "OPTION",
        "last_price": "2.50",
        "cost_price": "2.00",
        "unrealized_profit_loss": "100.00",
        "legs": [{
            "leg_id": "MOCK_LEG_ID",
            "symbol": "MOCK-OPTION",
            "quantity": "2",
            "option_type": "CALL",
            "option_expire_date": "2026-12-18",
            "option_exercise_price": "100.00",
            "option_contract_multiplier": "100",
            "option_contract_deliverable": "100",
            "expiration_type": "PM",
        }],
    }


def response(payload, **kwargs):
    return FakeResponse(payload, **kwargs)


class AccountDataTests(unittest.TestCase):
    def make_api(self, accounts=None, balance=None, positions=None, **response_options):
        if accounts is None:
            accounts = account_list(margin_account())
        if balance is None:
            balance = valid_balance()
        if positions is None:
            positions = []
        options = response_options.get("per_response", [{} for _ in range(3)])
        responses = [
            response(accounts, **options[0]),
            response(balance, **options[1]),
            response(positions, **options[2]),
        ]
        api_client = FakeApiClient(responses)
        return AccountV2(api_client), api_client

    def fetch(self, account_api):
        return fetch_margin_account_data(account_api, now=NOW)

    def assert_no_order_calls(self, api_client):
        for method_name in (
            "place_order", "submit_order", "modify_order", "replace_order", "cancel_order"
        ):
            getattr(api_client, method_name).assert_not_called()

    def test_exact_sdk_calls_and_parameters_and_distinct_buying_power_fields(self):
        api, api_client = self.make_api(
            accounts=account_list(cash_account(), margin_account()),
        )

        result = self.fetch(api)

        self.assertEqual(result.account_type, "MARGIN")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.equity, Decimal("12345.67"))
        self.assertIsNone(result.buying_power)
        self.assertEqual(result.option_buying_power, Decimal("802.34"))
        self.assertEqual(result.day_buying_power, Decimal("703.45"))
        self.assertEqual(result.overnight_buying_power, Decimal("604.56"))
        self.assertEqual(result.night_trading_buying_power, Decimal("505.67"))
        self.assertEqual(
            [
                (request.get_action_name(), request.get_method(), request.get_version(), request.get_query_params())
                for request in api_client.requests
            ],
            [
                ("/trading/accounts/list", "GET", "v3", {}),
                ("/trading/assets/balances/get", "GET", "v3", {"account_id": MARGIN_ID}),
                ("/trading/assets/positions/list", "GET", "v3", {"account_id": MARGIN_ID}),
            ],
        )
        self.assertEqual(api_client.responses, [])
        self.assert_no_order_calls(api_client)

    def test_cash_only_account_is_rejected_without_balance_or_positions_calls(self):
        api, api_client = self.make_api(accounts=account_list(cash_account()))
        with self.assertRaises(AccountDataError):
            self.fetch(api)
        self.assertEqual(len(api_client.requests), 1)
        self.assert_no_order_calls(api_client)

    def test_missing_account_type_is_rejected(self):
        api, api_client = self.make_api(accounts=[{"account_id": MARGIN_ID}])
        with self.assertRaises(AccountDataError):
            self.fetch(api)
        self.assertEqual(len(api_client.requests), 1)

    def test_ambiguous_margin_accounts_are_rejected(self):
        api, api_client = self.make_api(accounts=account_list(
            margin_account("MOCK_MARGIN_ONE"), margin_account("MOCK_MARGIN_TWO")
        ))
        with self.assertRaises(AccountDataError):
            self.fetch(api)
        self.assertEqual(len(api_client.requests), 1)

    def test_malformed_account_lists_are_rejected(self):
        for payload in ({"data": [margin_account()]}, [], [None], [cash_account("")]):
            with self.subTest(payload_type=type(payload).__name__):
                api, api_client = self.make_api(accounts=payload)
                with self.assertRaises(AccountDataError):
                    self.fetch(api)
                self.assertEqual(len(api_client.requests), 1)

    def test_empty_positions_are_valid(self):
        api, _ = self.make_api(positions=[])
        result = self.fetch(api)
        self.assertEqual(result.positions, ())

    def test_equity_position_is_parsed_with_decimal_values(self):
        api, _ = self.make_api(positions=[equity_position()])
        result = self.fetch(api)
        position = result.positions[0]
        self.assertEqual(position.instrument_type, "EQUITY")
        self.assertEqual(position.quantity, Decimal("4"))
        self.assertEqual(position.last_price, Decimal("12.50"))
        self.assertEqual(position.cost_price, Decimal("11.25"))
        self.assertEqual(position.unrealized_profit_loss, Decimal("5.00"))
        self.assertEqual(position.legs, ())

    def test_option_position_and_leg_are_parsed(self):
        api, _ = self.make_api(positions=[option_position()])
        position = self.fetch(api).positions[0]
        self.assertEqual(position.instrument_type, "OPTION")
        self.assertEqual(position.quantity, Decimal("2"))
        self.assertEqual(position.option_strategy, "SINGLE")
        self.assertEqual(len(position.legs), 1)
        leg = position.legs[0]
        self.assertEqual(leg.option_type, "CALL")
        self.assertEqual(leg.expiration_date, "2026-12-18")
        self.assertEqual(leg.exercise_price, Decimal("100.00"))
        self.assertEqual(leg.contract_multiplier, Decimal("100"))
        self.assertEqual(leg.contract_deliverable, Decimal("100"))
        # IDs are used internally for the requests but never included in the result.
        self.assertFalse(hasattr(position, "position_id"))

    def test_balance_missing_fields_and_non_usd_currency_fail_closed(self):
        cases = [
            (lambda data: data.pop("total_net_liquidation_value"), "missing equity"),
            (lambda data: data.update(total_asset_currency="CAD"), "wrong total currency"),
            (lambda data: data.pop("account_currency_assets"), "missing currency assets"),
            (lambda data: data["account_currency_assets"][0].pop("option_buying_power"), "missing option BP"),
        ]
        for mutate, label in cases:
            with self.subTest(case=label):
                payload = valid_balance()
                mutate(payload)
                api, _ = self.make_api(balance=payload)
                with self.assertRaises(AccountDataError):
                    self.fetch(api)

    def test_non_option_buying_power_fields_are_optional_and_not_substituted(self):
        payload = valid_balance()
        asset = payload["account_currency_assets"][0]
        asset.pop("day_buying_power")
        asset.pop("overnight_buying_power")
        asset.pop("night_trading_buying_power")
        api, _ = self.make_api(balance=payload)

        result = self.fetch(api)

        self.assertIsNone(result.buying_power)
        self.assertEqual(result.option_buying_power, Decimal("802.34"))
        self.assertIsNone(result.day_buying_power)
        self.assertIsNone(result.overnight_buying_power)
        self.assertIsNone(result.night_trading_buying_power)

    def test_malformed_decimal_wrong_usd_asset_and_duplicate_usd_asset_fail(self):
        cases = []
        payload = valid_balance()
        payload["total_net_liquidation_value"] = "NaN"
        cases.append(payload)
        payload = valid_balance()
        payload["account_currency_assets"][0]["buying_power"] = 123
        cases.append(payload)
        payload = valid_balance()
        payload["account_currency_assets"][0]["currency"] = "CAD"
        cases.append(payload)
        payload = valid_balance()
        payload["account_currency_assets"].append(dict(payload["account_currency_assets"][0]))
        cases.append(payload)

        for bad_balance in cases:
            with self.subTest(balance=bad_balance):
                api, _ = self.make_api(balance=bad_balance)
                with self.assertRaises(AccountDataError):
                    self.fetch(api)

    def test_malformed_and_unsupported_positions_fail_closed(self):
        cases = [
            [{**equity_position(), "currency": "CAD"}],
            [{**equity_position(), "quantity": "Infinity"}],
            [{**equity_position(), "instrument_type": "FUTURES"}],
            [{**option_position(), "legs": []}],
            [{**option_position(), "legs": [{"option_type": "CALL"}]}],
            {"positions": [equity_position()]},
        ]
        for positions in cases:
            with self.subTest(positions_type=type(positions).__name__):
                api, _ = self.make_api(positions=positions)
                with self.assertRaises(AccountDataError):
                    self.fetch(api)

    def test_http_errors_and_permission_errors_fail_closed(self):
        for stage in range(3):
            for status in (401, 403, 500):
                with self.subTest(stage=stage, status=status):
                    responses = [
                        response(account_list(margin_account())),
                        response(valid_balance()),
                        response([]),
                    ]
                    responses[stage] = response({}, status=status)
                    api_client = FakeApiClient(responses)
                    with self.assertRaises(AccountDataError):
                        self.fetch(AccountV2(api_client))
                    self.assert_no_order_calls(api_client)

    def test_stale_missing_invalid_and_future_response_dates_fail_closed(self):
        options = [
            {"response_time": NOW - timedelta(seconds=31)},
            {"headers": {}},
            {"headers": {"Date": "invalid"}},
            {"response_time": NOW + timedelta(seconds=6)},
        ]
        for option in options:
            with self.subTest(option=option):
                api, _ = self.make_api(per_response=[option, {}, {}])
                with self.assertRaises(AccountDataError):
                    self.fetch(api)

    def test_json_parse_failure_and_sdk_transport_failure_are_sanitized(self):
        api_client = FakeApiClient([
            response(None, bad_json=True),
        ])
        with self.assertRaises(AccountDataError) as caught:
            self.fetch(AccountV2(api_client))
        self.assertNotIn(MARGIN_ID, str(caught.exception))

        class RaisingApi:
            def get_account_list(self):
                raise RuntimeError("mock contains account identifier and token")

        with self.assertRaises(AccountDataError) as caught:
            self.fetch(RaisingApi())
        self.assertNotIn("token", str(caught.exception).lower())
        self.assertNotIn("account identifier", str(caught.exception).lower())

    def test_missing_response_date_and_invalid_age_limit_fail_closed(self):
        api, _ = self.make_api(per_response=[{"headers": {}}, {}, {}])
        with self.assertRaises(AccountDataError):
            self.fetch(api)

        api, _ = self.make_api()
        with self.assertRaises(AccountDataError):
            fetch_margin_account_data(api, now=NOW, max_response_age_seconds=float("nan"))


if __name__ == "__main__":
    unittest.main()
