"""Offline tests for the persistent Webull order coordinator."""

import json
import tempfile
import unittest
from pathlib import Path

from order_execution import OrderCoordinator, OrderStateError


CONTRACT = {
    "symbol": "SPY261218C00640000",
    "strike_price": "640",
    "expiration_date": "2026-12-18",
    "option_type": "CALL",
}


class FakeOrderApi:
    def __init__(self):
        self.place_calls = []
        self.detail_calls = []
        self.cancel_calls = []
        self.place_result = {"order_id": "WEBULL-ORDER-1", "client_order_id": "client"}
        self.details = []
        self.open_pages = []
        self.open_calls = []
        self.place_error = None
        self.detail_error = None

    def place_order(self, account_id, new_orders):
        self.place_calls.append((account_id, new_orders))
        if self.place_error:
            raise self.place_error
        return self.place_result

    def get_order_detail(self, account_id, client_order_id):
        self.detail_calls.append((account_id, client_order_id))
        if self.detail_error:
            raise self.detail_error
        if self.details:
            return self.details.pop(0)
        return {"order_id": "WEBULL-ORDER-1", "status": "SUBMITTED",
                "total_quantity": "2", "filled_quantity": "0", "filled_price": None}

    def cancel_order(self, account_id, client_order_id):
        self.cancel_calls.append((account_id, client_order_id))
        return {"success": True}

    def list_order_open(self, account_id, pagination_key=None):
        self.open_calls.append((account_id, pagination_key))
        if self.open_pages:
            return self.open_pages.pop(0)
        return {"data": [], "pagination_key": None}


class OrderCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="order-coordinator-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.json"
        self.api = FakeOrderApi()
        self.events = []
        self.manager = OrderCoordinator(
            self.api, "OFFLINE-MARGIN", state_path=self.path,
            event_logger=lambda *args, **kwargs: self.events.append((args, kwargs)),
        )

    def buy(self, signal="signal-1"):
        return self.manager.submit_entry(
            signal_key=signal, contract=CONTRACT, direction="CALL", price="2.10",
            max_trade_amount="500", allocated_amount="0", option_buying_power="1000",
            take_profit_pct=4, stop_loss_pct=5,
        )

    def test_buy_order_payload_matches_documented_single_option_limit_shape(self):
        record = self.buy()
        account_id, new_orders = self.api.place_calls[0]
        self.assertEqual(account_id, "OFFLINE-MARGIN")
        order = new_orders[0]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["position_intent"], "BUY_TO_OPEN")
        self.assertEqual(order["order_type"], "LIMIT")
        self.assertEqual(order["limit_price"], "2.10")
        self.assertEqual(order["quantity"], "2")
        self.assertEqual(order["instrument_type"], "OPTION")
        self.assertEqual(order["symbol"], "SPY")
        self.assertEqual(order["legs"][0], {
            "side": "BUY", "quantity": "2", "symbol": "SPY",
            "strike_price": "640", "option_expire_date": "2026-12-18",
            "instrument_type": "OPTION", "option_type": "CALL", "market": "US",
        })
        self.assertEqual(record["order_id"], "WEBULL-ORDER-1")
        self.assertEqual(record["status"], "SUBMITTED")
        self.assertLessEqual(len(record["client_order_id"]), 32)

    def test_confirmed_fill_records_actual_quantity_and_average_price(self):
        self.api.place_result = {
            "order_id": "WEBULL-ORDER-1", "status": "FILLED",
            "total_quantity": "2", "filled_quantity": "2", "filled_price": "2.08",
        }
        record = self.buy()
        self.assertEqual(record["status"], "FILLED")
        self.assertEqual(record["filled_quantity"], 2)
        self.assertEqual(record["filled_price"], "2.08")

    def test_partial_fill_reserves_only_unfilled_remainder(self):
        self.api.place_result = {
            "order_id": "WEBULL-ORDER-1", "status": "PARTIAL_FILLED",
            "filled_quantity": "1", "filled_price": "2.08",
        }
        record = self.buy()
        self.assertEqual(record["status"], "PARTIALLY_FILLED")
        self.assertEqual(record["filled_quantity"], 1)
        self.assertEqual(self.manager.reserved_amount(), 210)

    def test_partial_cancel_persists_fills_cancelled_quantity_and_remaining_after_restart(self):
        stored = self.manager.record_emergency_external_entry(
            {
                "client_order_id": "external-buy",
                "order_id": "external-order",
                "side": "BUY",
                "position_intent": "BUY_TO_OPEN",
                "symbol": "SPY",
                "quantity": "2",
                "legs": [{
                    "symbol": "SPY", "side": "BUY", "quantity": "2",
                    "strike_price": "640", "option_expire_date": "2026-12-18",
                    "option_type": "CALL", "instrument_type": "OPTION",
                }],
            },
            {
                "client_order_id": "external-buy", "status": "CANCELLED",
                "total_quantity": "2", "filled_quantity": "1",
                "filled_price": "2.05",
            },
        )
        self.assertEqual(stored["status"], "CANCELLED")
        self.assertEqual(stored["filled_quantity"], 1)
        self.assertEqual(stored["remaining_quantity"], 1)
        self.assertEqual(stored["cancelled_quantity"], 1)
        self.assertEqual(stored["unresolved_quantity"], 0)

        replay = OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
        recovered = next(item for item in replay.orders()
                         if item["client_order_id"] == "external-buy")
        self.assertEqual(recovered["filled_quantity"], 1)
        self.assertEqual(recovered["cancelled_quantity"], 1)
        self.assertEqual(recovered["remaining_quantity"], 1)
        self.assertEqual(len(self.api.place_calls), 0)

    def test_combined_local_and_broker_sell_coverage_does_not_submit_another_sell(self):
        self.api.place_result = {
            "order_id": "SELL-1", "status": "SUBMITTED",
            "quantity": "1", "filled_quantity": "0",
        }
        first = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=1,
        )
        second = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL",
            quantity=2, broker_covered_quantity=1,
        )
        self.assertEqual(second["client_order_id"], first["client_order_id"])
        self.assertEqual(len(self.api.place_calls), 1)

    def test_rejected_order_releases_reservation(self):
        self.api.place_result = {
            "order_id": "WEBULL-ORDER-1", "status": "FAILED",
            "filled_quantity": "0", "filled_price": None,
        }
        record = self.buy()
        self.assertEqual(record["status"], "REJECTED")
        self.assertEqual(self.manager.reserved_amount(), 0)

    def test_cancel_is_confirmed_by_order_detail_and_releases_remaining_reservation(self):
        record = self.buy()
        self.api.details = [{
            "order_id": "WEBULL-ORDER-1", "status": "CANCELLED",
            "filled_quantity": "0", "filled_price": None,
        }]
        cancelled = self.manager.cancel(record["client_order_id"])
        self.assertEqual(self.api.cancel_calls, [("OFFLINE-MARGIN", record["client_order_id"])])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self.manager.reserved_amount(), 0)

    def test_timeout_queries_original_client_id_and_never_repeats_submit(self):
        self.api.place_error = TimeoutError("offline timeout")
        self.api.details = [{
            "order_id": "WEBULL-ORDER-1", "status": "SUBMITTED",
            "filled_quantity": "0", "filled_price": None,
        }]
        first = self.buy()
        second = self.buy()
        self.assertEqual(len(self.api.place_calls), 1)
        self.assertEqual(len(self.api.detail_calls), 1)
        self.assertEqual(self.api.detail_calls[0][1], first["client_order_id"])
        self.assertEqual(first["client_order_id"], second["client_order_id"])

    def test_unknown_status_blocks_other_entries(self):
        self.api.place_error = TimeoutError("offline timeout")
        self.api.detail_error = OSError("offline unavailable")
        first = self.buy()
        self.assertEqual(first["status"], "UNKNOWN")
        with self.assertRaises(OrderStateError):
            self.buy("different-signal")
        self.assertEqual(len(self.api.place_calls), 1)

    def test_duplicate_signal_cannot_submit_twice(self):
        first = self.buy()
        repeated = self.buy()
        self.assertEqual(first["client_order_id"], repeated["client_order_id"])
        self.assertEqual(len(self.api.place_calls), 1)

    def test_buying_power_and_configured_budget_are_both_caps(self):
        first = self.buy()
        self.assertEqual(first["quantity"], 2)
        with self.assertRaises(OrderStateError):
            self.manager.submit_entry(
                signal_key="another", contract=CONTRACT, direction="PUT", price="2.10",
                max_trade_amount="500", allocated_amount="400", option_buying_power="1000",
                take_profit_pct=4, stop_loss_pct=5,
            )
        self.assertEqual(len(self.api.place_calls), 1)

    def test_sell_to_close_uses_documented_option_sell_fields(self):
        result = self.manager.submit_exit(
            position_key="position-1", contract={**CONTRACT, "option_type": "PUT"},
            direction="PUT", quantity=1, price="1.75",
        )
        order = self.api.place_calls[0][1][0]
        self.assertEqual(order["side"], "SELL")
        self.assertEqual(order["position_intent"], "SELL_TO_CLOSE")
        self.assertEqual(order["order_type"], "MARKET")
        self.assertEqual(order["support_trading_session"], "CORE")
        self.assertNotIn("limit_price", order)
        self.assertEqual(order["legs"][0]["option_type"], "PUT")
        self.assertEqual(order["quantity"], "1")
        self.assertEqual(result["role"], "EXIT")

    def test_order_builder_rejects_limit_sell_to_close(self):
        with self.assertRaises(OrderStateError):
            OrderCoordinator._make_order(
                client_order_id="offline-exit", contract=CONTRACT,
                direction="CALL", quantity=1, price="1.00", side="SELL",
                intent="SELL_TO_CLOSE", order_type="LIMIT",
                underlying_symbol="SPY",
            )

    def test_partial_market_exit_tracks_fill_and_does_not_claim_flat(self):
        self.api.place_result = {
            "order_id": "SELL-1", "status": "PARTIAL_FILLED",
            "quantity": "2", "filled_quantity": "1", "filled_price": "1.20",
        }
        result = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=2,
        )
        self.assertEqual(result["order_type"], "MARKET")
        self.assertEqual(result["filled_quantity"], 1)
        self.assertEqual(result["status"], "PARTIALLY_FILLED")
        self.assertEqual(self.manager.active_orders(role="EXIT")[0]["quantity"], 2)
        repeated = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=1,
        )
        self.assertEqual(repeated["client_order_id"], result["client_order_id"])
        self.assertEqual(len(self.api.place_calls), 1)

    def test_partial_market_sell_remaining_quantity_survives_restart(self):
        self.api.place_result = {
            "order_id": "BUY-1", "status": "FILLED",
            "total_quantity": "2", "filled_quantity": "2", "filled_price": "2.08",
        }
        entry = self.buy("restart-partial-sell")
        self.api.place_result = {
            "order_id": "SELL-1", "status": "PARTIALLY_FILLED",
            "total_quantity": "2", "filled_quantity": "1", "filled_price": "1.80",
        }
        exit_order = self.manager.submit_exit(
            position_key=entry["client_order_id"], contract=CONTRACT,
            direction="CALL", quantity=2,
        )
        self.assertEqual(exit_order["remaining_quantity"], 1)
        self.assertEqual(exit_order["unresolved_quantity"], 1)

        recovered = OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
        restored = next(order for order in recovered.orders()
                        if order["client_order_id"] == exit_order["client_order_id"])
        self.assertEqual(restored["filled_quantity"], 1)
        self.assertEqual(restored["remaining_quantity"], 1)
        self.assertEqual(restored["unresolved_quantity"], 1)
        retry = recovered.submit_exit(
            position_key=entry["client_order_id"], contract=CONTRACT,
            direction="CALL", quantity=1,
        )
        self.assertEqual(retry["client_order_id"], exit_order["client_order_id"])
        self.assertEqual(len(self.api.place_calls), 2)

    def test_broker_covered_quantity_prevents_duplicate_market_exit(self):
        result = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL",
            quantity=1, broker_covered_quantity=1,
        )
        self.assertEqual(result["status"], "ALREADY_COVERED")
        self.assertEqual(self.api.place_calls, [])

    def test_rejected_market_exit_is_terminal_and_releases_retry_path(self):
        self.api.place_result = {
            "order_id": "SELL-REJECTED", "status": "FAILED",
            "filled_quantity": "0", "filled_price": None,
        }
        result = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=1,
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.manager.active_orders(role="EXIT"), [])

    def test_market_exit_timeout_queries_same_id_without_resubmitting(self):
        self.api.place_error = TimeoutError("offline timeout")
        self.api.details = [{
            "order_id": "SELL-1", "status": "SUBMITTED",
            "filled_quantity": "0", "filled_price": None,
        }]
        result = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=1,
        )
        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(len(self.api.place_calls), 1)
        self.assertEqual(self.api.detail_calls[0][1], result["client_order_id"])

    def test_market_exit_unknown_status_is_persisted_and_active(self):
        self.api.place_error = TimeoutError("offline timeout")
        self.api.detail_error = OSError("offline unavailable")
        result = self.manager.submit_exit(
            position_key="position-1", contract=CONTRACT, direction="CALL", quantity=1,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(self.manager.active_orders(role="EXIT")), 1)

    def test_open_orders_are_paginated_and_nested_order_rows_are_flattened(self):
        self.api.open_pages = [
            {"data": [{"symbol": "SPY", "orders": [{"client_order_id": "x", "side": "SELL"}]}],
             "pagination_key": "next"},
            {"data": [{"client_order_id": "y", "side": "BUY"}], "pagination_key": None},
        ]
        orders = self.manager.list_open_orders()
        self.assertEqual([order["client_order_id"] for order in orders], ["x", "y"])
        self.assertEqual(self.api.open_calls, [
            ("OFFLINE-MARGIN", None), ("OFFLINE-MARGIN", "next"),
        ])

    def test_malformed_open_order_page_fails_closed(self):
        self.api.open_pages = [{"data": None}]
        with self.assertRaises(OrderStateError):
            self.manager.list_open_orders()

    def test_restart_recovers_order_id_and_never_resubmits_intent(self):
        original = self.buy()
        loaded = OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
        self.assertEqual(loaded.active_orders()[0]["order_id"], original["order_id"])
        replay = loaded.submit_entry(
            signal_key="signal-1", contract=CONTRACT, direction="CALL", price="2.10",
            max_trade_amount="500", allocated_amount="0", option_buying_power="1000",
            take_profit_pct=4, stop_loss_pct=5,
        )
        self.assertEqual(replay["client_order_id"], original["client_order_id"])
        self.assertEqual(len(self.api.place_calls), 1)

    def test_crash_during_submit_recovers_as_unknown_and_blocks_new_signal(self):
        self.path.write_text(json.dumps({
            "account_id": "OFFLINE-MARGIN",
            "orders": [{"client_order_id": "crashed-id", "status": "SUBMITTING",
                         "role": "ENTRY", "quantity": 1, "limit_price": "2.00",
                         "filled_quantity": 0}],
            "signals": {"old": "crashed-id"},
        }))
        loaded = OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
        self.assertTrue(loaded.has_unknown_order())
        with self.assertRaises(OrderStateError):
            loaded.submit_entry(
                signal_key="new", contract=CONTRACT, direction="CALL", price="2.10",
                max_trade_amount="500", allocated_amount="0", option_buying_power="1000",
                take_profit_pct=4, stop_loss_pct=5,
            )
        self.assertEqual(self.api.place_calls, [])

    def test_persisted_orders_require_the_same_verified_account(self):
        for ledger in (
            {"account_id": "OTHER-MARGIN", "orders": [], "signals": {}},
            {"orders": [], "signals": {}},
        ):
            with self.subTest(account_id=ledger.get("account_id")):
                self.path.write_text(json.dumps(ledger))
                with self.assertRaises(OrderStateError):
                    OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
                self.assertEqual(self.api.place_calls, [])
                self.assertEqual(self.api.detail_calls, [])
                self.assertEqual(self.api.cancel_calls, [])

    def test_emergency_baseline_survives_restart_and_is_not_reset(self):
        key = ("SPY", "640", "2026-12-18", "CALL")
        first = self.manager.get_or_create_emergency_baseline(
            key, 2, emergency_id="cycle-one"
        )
        self.manager.record_emergency_external_fill(
            key, "external-1", 1, emergency_id="cycle-one"
        )
        loaded = OrderCoordinator(self.api, "OFFLINE-MARGIN", state_path=self.path)
        second = loaded.get_or_create_emergency_baseline(
            key, 1, emergency_id="cycle-one"
        )
        self.assertEqual(first["quantity"], 2)
        self.assertEqual(second["quantity"], 2)
        self.assertEqual(second["external_exit_fills"], {"external-1": 1})

    def test_new_emergency_cycle_gets_a_new_position_baseline(self):
        key = ("SPY", "640", "2026-12-18", "CALL")
        first = self.manager.get_or_create_emergency_baseline(
            key, 2, emergency_id="cycle-one"
        )
        second = self.manager.get_or_create_emergency_baseline(
            key, 1, emergency_id="cycle-two"
        )
        self.assertEqual(first["quantity"], 2)
        self.assertEqual(second["quantity"], 1)

    def test_event_log_records_order_id_and_fills_without_raw_response(self):
        self.api.place_result = {
            "order_id": "WEBULL-ORDER-1", "status": "FILLED",
            "filled_quantity": "2", "filled_price": "2.08",
        }
        self.buy()
        serialized = json.dumps(self.events)
        self.assertIn("WEBULL-ORDER-1", serialized)
        self.assertIn("filled_quantity", serialized)
        self.assertNotIn("OFFLINE-MARGIN", serialized)


if __name__ == "__main__":
    unittest.main()
