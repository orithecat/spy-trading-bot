"""Offline tests for Strategy 1 live-order orchestration."""

from types import SimpleNamespace
from decimal import Decimal
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from strategy1_runner import Strategy1Runner


class FakeCoordinator:
    def __init__(self, *_args, **_kwargs):
        self._orders = []
        self.submit_calls = []
        self.cancel_calls = []
        self.exit_calls = []
        self.broker_open_orders = []
        self.confirmed_orders = {}
        self.emergency_baselines = {}
        self.cancel_external_calls = []
        self.external_entry_states = []
        self.reconciliation_states = {}
        self.external_cancel_results = {}

    def orders(self):
        return [dict(order) for order in self._orders]

    def active_orders(self, role=None):
        terminal = {"FILLED", "CANCELLED", "REJECTED"}
        return [dict(order) for order in self._orders
                if order["status"] not in terminal and (role is None or order["role"] == role)]

    def refresh_active(self):
        return []

    def has_unknown_order(self):
        return any(order["status"] == "UNKNOWN" for order in self._orders)

    def submit_entry(self, *, signal_key, contract, direction, price,
                     max_trade_amount, allocated_amount, option_buying_power,
                     take_profit_pct, stop_loss_pct):
        quantity = min(
            int((float(max_trade_amount) - float(allocated_amount)) // (float(price) * 100)),
            int(float(option_buying_power) // (float(price) * 100)),
        )
        if quantity < 1:
            raise ValueError("budget insufficient")
        self.submit_calls.append((signal_key, contract, direction, price))
        order = {
            "client_order_id": f"entry-{len(self.submit_calls)}",
            "order_id": f"order-{len(self.submit_calls)}",
            "signal_key": signal_key,
            "position_key": f"entry-{len(self.submit_calls)}",
            "role": "ENTRY",
            "direction": direction,
            "contract": {
                "symbol": contract["symbol"],
                "strike": str(contract.get("strike_price", contract.get("strike"))),
                "expiry": contract.get("expiration_date", contract.get("expiry")),
                "option_type": direction,
            },
            "quantity": quantity,
            "filled_quantity": quantity,
            "filled_price": str(float(price) - 0.02),
            "limit_price": str(price),
            "status": "FILLED",
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "created_at": "2026-11-05T18:00:00+00:00",
        }
        self._orders.append(order)
        return dict(order)

    def list_open_orders(self):
        return [dict(order) for order in self.broker_open_orders]

    def confirm_order(self, client_order_id):
        return dict(self.confirmed_orders[client_order_id])

    def begin_emergency_scan(self, emergency_id):
        self.emergency_scans = getattr(self, "emergency_scans", {})
        self.emergency_scans[emergency_id] = {
            "status": "UNRESOLVED",
            "phase": "OPEN_ORDER_SCAN_REQUESTED",
        }

    def finish_emergency_scan(self, emergency_id, *, status, phase="RECONCILED"):
        scans = getattr(self, "emergency_scans", {})
        if emergency_id not in scans:
            raise RuntimeError("Emergency scan journal is unavailable.")
        scans[emergency_id].update({"status": status, "phase": phase})

    def journal_external_order(self, open_order, *, emergency_id):
        record = dict(open_order)
        side = str(record.get("side", "")).upper()
        intent = str(record.get("position_intent", "")).upper()
        record["role"] = (
            "ENTRY" if (side, intent) == ("BUY", "BUY_TO_OPEN")
            else "EXIT" if (side, intent) == ("SELL", "SELL_TO_CLOSE")
            else "EXTERNAL_UNKNOWN"
        )
        record["source"] = "emergency_external"
        record["emergency_id"] = emergency_id
        record.setdefault("filled_quantity", 0)
        record.setdefault("quantity", record.get("total_quantity"))
        return record

    def refresh_emergency_orders(self, emergency_id):
        return [
            dict(order) for order in self._orders
            if order.get("source") == "emergency_external"
            and order.get("emergency_id") == emergency_id
        ]

    def confirm_emergency_order(self, open_order, *, emergency_id):
        record = self.journal_external_order(
            open_order, emergency_id=emergency_id
        )
        client_id = record.get("client_order_id")
        detail = self.confirmed_orders.get(client_id)
        if detail is not None:
            record.update(detail)
        return record

    def begin_emergency_scan(self, emergency_id):
        self.emergency_scans = getattr(self, "emergency_scans", {})
        self.emergency_scans[emergency_id] = {
            "status": "UNRESOLVED",
            "phase": "OPEN_ORDER_SCAN_REQUESTED",
        }

    def finish_emergency_scan(self, emergency_id, *, status, phase="RECONCILED"):
        scans = getattr(self, "emergency_scans", {})
        if emergency_id not in scans:
            raise RuntimeError("Emergency scan journal is unavailable.")
        scans[emergency_id].update({"status": status, "phase": phase})

    def journal_external_order(self, open_order, *, emergency_id):
        client_id = open_order.get("client_order_id")
        if not isinstance(client_id, str) or not client_id:
            raise RuntimeError("External order ID is missing.")
        side = str(open_order.get("side", "")).upper()
        intent = str(open_order.get("position_intent", "")).upper()
        role = (
            "ENTRY" if (side, intent) == ("BUY", "BUY_TO_OPEN")
            else "EXIT" if (side, intent) == ("SELL", "SELL_TO_CLOSE")
            else "EXTERNAL_UNKNOWN"
        )
        legs = open_order.get("legs")
        contract = {}
        if isinstance(legs, list) and len(legs) == 1:
            leg = legs[0]
            contract = {
                "symbol": leg.get("symbol"),
                "underlying_symbol": str(open_order.get("symbol") or leg.get("symbol") or "").upper(),
                "strike": str(leg.get("strike_price", "")),
                "expiry": str(leg.get("option_expire_date", "")),
                "option_type": str(leg.get("option_type", "")).upper(),
            }
        else:
            role = "EXTERNAL_UNKNOWN"
        if role == "EXTERNAL_UNKNOWN":
            raise RuntimeError("External order contract is ambiguous.")
        quantity = int(open_order.get("total_quantity", open_order.get("quantity", 0)))
        filled = int(open_order.get("filled_quantity", open_order.get("filled_qty", 0)) or 0)
        if quantity < 1 or filled < 0 or filled > quantity:
            raise RuntimeError("External order quantities are invalid.")
        existing = next(
            (item for item in self._orders if item.get("client_order_id") == client_id),
            None,
        )
        record = {
            "client_order_id": client_id,
            "order_id": open_order.get("order_id"),
            "source": "emergency_external",
            "emergency_id": emergency_id,
            "role": role,
            "side": side,
            "position_intent": intent,
            "direction": contract["option_type"],
            "position_key": "external:" + client_id,
            "contract": contract,
            "quantity": quantity,
            "filled_quantity": filled,
            "status": str(open_order.get("status", "UNKNOWN")).upper(),
        }
        if existing is not None:
            if existing.get("contract") != contract or existing.get("role") != role:
                raise RuntimeError("External order identity changed.")
            record["filled_quantity"] = max(
                int(existing.get("filled_quantity", 0)),
                filled,
            )
            existing.update(record)
        else:
            self._orders.append(record)
        return dict(existing or record)

    def refresh_emergency_orders(self, emergency_id):
        return [
            dict(order) for order in self._orders
            if order.get("source") == "emergency_external"
            and order.get("emergency_id") == emergency_id
        ]

    def confirm_emergency_order(self, open_order, *, emergency_id):
        record = self.journal_external_order(
            open_order, emergency_id=emergency_id
        )
        detail = self.confirmed_orders.get(record["client_order_id"], {})
        existing = next(
            item for item in self._orders
            if item.get("client_order_id") == record["client_order_id"]
        )
        if detail:
            filled = int(detail.get("filled_quantity", existing["filled_quantity"]) or 0)
            existing["filled_quantity"] = max(existing["filled_quantity"], filled)
            existing["status"] = str(detail.get("status", existing["status"])).upper()
            existing["order_id"] = detail.get("order_id") or existing.get("order_id")
        return dict(existing)

    def get_or_create_emergency_baseline(self, contract_key, quantity, *, emergency_id):
        key = emergency_id + "|" + "|".join(str(value) for value in contract_key)
        entry_filled = 0
        exit_filled = 0
        for order in self._orders:
            contract = order.get("contract", {})
            identity = (
                str(contract.get("underlying_symbol", "SPY")).upper(),
                Decimal(str(contract.get("strike"))).normalize(),
                str(contract.get("expiry")),
                str(contract.get("option_type", "")).upper(),
            )
            expected = (
                str(contract_key[0]).upper(), Decimal(str(contract_key[1])).normalize(),
                str(contract_key[2]), str(contract_key[3]).upper(),
            )
            if identity == expected:
                if order.get("role") == "ENTRY":
                    entry_filled += int(order.get("filled_quantity", 0))
                elif (order.get("role") == "EXIT"
                      and order.get("source") != "emergency_external"):
                    exit_filled += int(order.get("filled_quantity", 0))
        return self.emergency_baselines.setdefault(
            key, {"quantity": quantity, "entry_filled": entry_filled,
                  "exit_filled": exit_filled,
                  "ledger_open_quantity": entry_filled - exit_filled,
                  "external_exit_fills": {}}
        )

    def record_emergency_external_entry(self, open_order, detail):
        record = dict(open_order)
        record.update(detail)
        self.external_entry_states.append(record)
        if record.get("legs"):
            leg = record["legs"][0]
            client_id = record["client_order_id"]
            existing = next(
                (item for item in self._orders
                 if item.get("client_order_id") == client_id),
                None,
            )
            order = {
                "client_order_id": client_id,
                "source": "emergency_external",
                "role": "ENTRY",
                "direction": leg["option_type"],
                "contract": {
                    "underlying_symbol": record.get("symbol", leg.get("symbol", "SPY")),
                    "strike": leg["strike_price"],
                    "expiry": leg["option_expire_date"],
                    "option_type": leg["option_type"],
                },
                "quantity": int(record.get("total_quantity", record.get("quantity", 1))),
                "filled_quantity": int(record.get("filled_quantity", 0)),
                "filled_price": record.get("filled_price"),
                "status": str(record["status"]).upper(),
                "cancelled_quantity": (
                    int(record.get("total_quantity", record.get("quantity", 1)))
                    - int(record.get("filled_quantity", 0))
                    if str(record["status"]).upper() == "CANCELLED" else 0
                ),
                "unresolved_quantity": 0,
            }
            if existing is not None:
                order["filled_quantity"] = max(
                    int(existing.get("filled_quantity", 0)),
                    order["filled_quantity"],
                )
                existing.update(order)
            else:
                self._orders.append(order)
        return record

    def record_emergency_reconciliation(self, contract_key, values, *, emergency_id):
        key = emergency_id + "|" + "|".join(str(value) for value in contract_key)
        self.reconciliation_states[key] = dict(values)

    def record_emergency_external_fill(self, contract_key, client_order_id,
                                       filled_quantity, *, emergency_id):
        key = emergency_id + "|" + "|".join(str(value) for value in contract_key)
        baseline = self.emergency_baselines[key]
        fills = baseline.setdefault("external_exit_fills", {})
        fills[client_order_id] = max(fills.get(client_order_id, 0), filled_quantity)

    def cancel_external(self, client_order_id):
        self.cancel_external_calls.append(client_order_id)
        open_order = next(
            (item for item in self.broker_open_orders
             if item.get("client_order_id") == client_order_id),
            None,
        )
        if open_order is None:
            raise RuntimeError("External order is not open.")
        detail = dict(self.external_cancel_results.get(
            client_order_id,
            {
                "client_order_id": client_order_id,
                "status": "CANCELLED",
                "total_quantity": open_order.get("total_quantity", open_order.get("quantity")),
                "filled_quantity": open_order.get("filled_quantity", 0),
            },
        ))
        self.record_emergency_external_entry(open_order, detail)
        self.broker_open_orders = [
            item for item in self.broker_open_orders
            if item.get("client_order_id") != client_order_id
        ]
        return {**open_order, **detail}

    def submit_exit(self, *, position_key, contract, direction, quantity, price=None,
                    underlying_symbol=None, source="strategy1",
                    emergency_baseline_quantity=None, broker_covered_quantity=0):
        self.exit_calls.append({
            "position_key": position_key, "contract": contract,
            "direction": direction, "quantity": quantity, "price": price,
            "underlying_symbol": underlying_symbol, "source": source,
            "request": {
                "instrument_type": "OPTION", "option_strategy": "SINGLE",
                "side": "SELL", "position_intent": "SELL_TO_CLOSE",
                "order_type": "MARKET", "time_in_force": "DAY",
                "entrust_type": "QTY", "support_trading_session": "CORE",
                "quantity": str(quantity),
            },
        })
        order = {
            "client_order_id": f"exit-{len(self._orders)}",
            "order_id": f"exit-order-{len(self._orders)}",
            "position_key": position_key,
            "role": "EXIT",
            "direction": direction,
            "quantity": quantity,
            "filled_quantity": quantity,
            "filled_price": "1.25",
            "limit_price": None,
            "order_type": "MARKET",
            "request": dict(self.exit_calls[-1]["request"]),
            "source": source,
            "emergency_baseline_quantity": emergency_baseline_quantity,
            "contract": {**contract, "underlying_symbol": underlying_symbol or "SPY"},
            "status": "FILLED",
        }
        self._orders.append(order)
        return dict(order)

    def cancel(self, client_order_id):
        self.cancel_calls.append(client_order_id)
        for order in self._orders:
            if order["client_order_id"] == client_order_id:
                order["status"] = "CANCELLED"
                return dict(order)


class FakeAccountMonitor:
    def __init__(self, allowed=True, *, positions=()):
        self.allowed = allowed
        self.start_calls = 0
        self.stop_calls = 0
        self.snapshot_calls = 0
        self.account_api_calls = 0
        self.order_api = object()
        self.account_data = SimpleNamespace(
            account_id="OFFLINE-MARGIN",
            account_type="MARGIN",
            positions_complete=True,
            option_buying_power=10000,
            positions=positions,
        )
        self.polled_at_monotonic = 100
        self.monotonic = lambda: 101

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def allows_new_entries(self):
        return self.allowed

    def get_snapshot(self):
        self.snapshot_calls += 1
        return {
            "account_data": self.account_data,
            "polled_at_monotonic": self.polled_at_monotonic,
        }

    def get_order_api(self):
        return self.order_api

    def snapshot_is_fresh(self):
        return True


class TestStrategy1Runner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="strategy1-runner-")
        self.addCleanup(self.temp.cleanup)
        self.log_patch = patch("strategy1_runner.log_event")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.account_monitor = FakeAccountMonitor()
        self.coordinator = FakeCoordinator()
        self.runner = Strategy1Runner(
            engine_factory=Mock,
            poll_seconds=0.01,
            account_monitor=self.account_monitor,
            order_state_path=Path(self.temp.name) / "orders.json",
            order_coordinator_factory=lambda *_args, **_kwargs: self.coordinator,
            real_trading_authorized=True,
        )
        self.runner._accept_entries = True
        self.runner._emergency_id = f"test-emergency-{self.id()}"
        self.runner._emergency_id = f"test-emergency-{self.id()}"
        self.params = {"max_trade_amount": 500, "take_profit_pct": 5, "stop_loss_pct": 5}
        self.contract = {
            "symbol": "SPY261105C00761000", "strike": 761,
            "expiry": "2026-11-05", "option_type": "CALL",
        }

    @staticmethod
    def account_option_position(quantity=1, direction="CALL", strike="761"):
        leg = SimpleNamespace(
            symbol="SPY", quantity=Decimal(quantity), option_type=direction,
            expiration_date="2026-11-05", exercise_price=Decimal(strike),
        )
        return SimpleNamespace(
            symbol="SPY", instrument_type="OPTION", currency="USD",
            quantity=Decimal(quantity), option_strategy="SINGLE", legs=(leg,),
        )

    def add_reconciliation_entry(self, *, direction="CALL", filled=1):
        contract = {
            "symbol": "SPY261105C00761000" if direction == "CALL" else "SPY261105P00761000",
            "underlying_symbol": "SPY", "strike": "761",
            "expiry": "2026-11-05", "option_type": direction,
        }
        self.coordinator._orders.append({
            "client_order_id": "reconcile-entry", "position_key": "reconcile-entry",
            "role": "ENTRY", "direction": direction, "contract": contract,
            "quantity": filled, "filled_quantity": filled,
            "filled_price": "1.002", "status": "FILLED",
        })

    def test_amount_differences_do_not_fail_matching_position_reconciliation(self):
        self.runner._order_coordinator = self.coordinator
        self.add_reconciliation_entry()
        position = self.account_option_position(1)
        position.cost_price = Decimal("1.002")
        position.last_price = Decimal("1.01")
        position.unrealized_profit_loss = Decimal("0.80")
        self.account_monitor.account_data.positions = (position,)
        self.account_monitor.account_data.equity = Decimal("100.10")
        self.account_monitor.account_data.option_buying_power = Decimal("25.00")
        # The fill ledger implies $100.20 notional; monetary differences are
        # deliberately outside the position identity/quantity reconciliation.

        self.assertTrue(
            self.runner._reconcile_account_positions(self.account_monitor.account_data)
        )

    def test_position_quantity_mismatch_fails_reconciliation(self):
        self.runner._order_coordinator = self.coordinator
        self.add_reconciliation_entry()
        self.account_monitor.account_data.positions = (self.account_option_position(2),)

        self.assertFalse(
            self.runner._reconcile_account_positions(self.account_monitor.account_data)
        )

    def test_option_contract_mismatches_fail_reconciliation(self):
        self.runner._order_coordinator = self.coordinator
        self.add_reconciliation_entry()
        for direction, expiry, strike in (
            ("PUT", "2026-11-05", "761"),
            ("CALL", "2026-11-06", "761"),
            ("CALL", "2026-11-05", "762"),
        ):
            with self.subTest(direction=direction, expiry=expiry, strike=strike):
                position = self.account_option_position(1)
                position.legs = (SimpleNamespace(
                    symbol="SPY", quantity=Decimal("1"),
                    option_type=direction, expiration_date=expiry,
                    exercise_price=Decimal(strike),
                ),)
                self.account_monitor.account_data.positions = (position,)
                self.assertFalse(
                    self.runner._reconcile_account_positions(self.account_monitor.account_data)
                )

    def test_unparseable_spy_position_or_overclosed_ledger_fails_reconciliation(self):
        self.runner._order_coordinator = self.coordinator
        malformed = self.account_option_position(1)
        malformed.legs = (SimpleNamespace(
            symbol="SPY", quantity=Decimal("1"), option_type="CALL",
            expiration_date="2026-11-05", exercise_price=None,
        ),)
        self.account_monitor.account_data.positions = (malformed,)
        self.assertFalse(
            self.runner._reconcile_account_positions(self.account_monitor.account_data)
        )

        self.account_monitor.account_data.positions = ()
        self.add_reconciliation_entry()
        self.coordinator._orders.append({
            "client_order_id": "reconcile-exit", "position_key": "reconcile-entry",
            "role": "EXIT", "quantity": 2, "filled_quantity": 2,
            "status": "FILLED",
        })
        self.assertFalse(
            self.runner._reconcile_account_positions(self.account_monitor.account_data)
        )

    def test_default_runner_start_does_not_authorize_real_buys(self):
        runner = Strategy1Runner(
            engine_factory=Mock, account_monitor=self.account_monitor,
            order_state_path=Path(self.temp.name) / "locked-orders.json",
            order_coordinator_factory=lambda *_args, **_kwargs: self.coordinator,
        )
        with patch.object(runner, "_worker"):
            runner.start()
        self.assertFalse(runner.real_trading_authorized)
        self.assertFalse(runner._accept_entries)
        runner._submit_live_entry(
            "CALL", self.contract, {"ask": 2.1}, self.params,
            {"eligible": True}, {"account_data": self.account_monitor.account_data},
            self.coordinator,
        )
        self.assertEqual(self.coordinator.submit_calls, [])

    def test_runner_start_does_not_reset_an_incomplete_emergency_stop(self):
        self.runner._emergency = True
        self.runner._emergency_account_flat = False
        self.runner._emergency_unresolved = ["order unresolved"]
        self.runner._accept_entries = False
        with patch.object(self.runner, "_worker") as worker:
            started = self.runner.start()
        self.assertFalse(started)
        self.assertTrue(self.runner._emergency)
        self.assertFalse(self.runner._accept_entries)
        worker.assert_not_called()

    def test_buy_authorization_off_does_not_disable_existing_position_exit(self):
        self.runner.real_trading_authorized = False
        self.runner._account_positions_reconciled = True
        self.runner._order_coordinator = self.coordinator
        self.coordinator._orders.append({
            "client_order_id": "entry-call", "position_key": "entry-call",
            "role": "ENTRY", "direction": "CALL",
            "contract": {"symbol": self.contract["symbol"], "strike": "761",
                         "expiry": "2026-11-05", "option_type": "CALL"},
            "quantity": 1, "filled_quantity": 1, "filled_price": "2.00",
            "limit_price": "2.10", "status": "FILLED",
            "take_profit_pct": 5, "stop_loss_pct": 5,
        })
        self.runner._sync_live_positions(self.coordinator)
        self.runner._engine = Mock()
        self.runner._engine.get_option_snapshot.return_value = {
            "bid": 2.10, "ask": 2.15, "quote_valid": True,
        }

        self.runner._manage_position("CALL", self.runner.positions["CALL"])

        self.assertEqual(self.coordinator.orders()[-1]["role"], "EXIT")
        self.assertEqual(self.coordinator.exit_calls[-1]["price"], None)
        self.assertIsNone(self.runner.positions["CALL"])

    def test_emergency_stop_market_closes_actual_account_option_not_only_ledger(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(2),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(),
            self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(len(self.coordinator.exit_calls), 1)
        call = self.coordinator.exit_calls[0]
        self.assertEqual(call["quantity"], 2)
        self.assertEqual(call["source"], "emergency_stop")
        self.assertEqual(call["underlying_symbol"], "SPY")
        self.assertEqual(self.coordinator.orders()[0]["order_type"], "MARKET")
        self.assertFalse(self.runner._emergency_account_flat)

    def test_emergency_stop_does_not_duplicate_covered_open_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(1),)
        self.coordinator.broker_open_orders = [{
            "client_order_id": "existing-sell", "symbol": "SPY",
            "side": "SELL", "position_intent": "SELL_TO_CLOSE",
            "quantity": "1", "filled_quantity": "0", "status": "SUBMITTED",
            "legs": [{
                "symbol": "SPY", "strike_price": "761",
                "option_expire_date": "2026-11-05", "option_type": "CALL",
            }],
        }]
        self.coordinator.confirmed_orders["existing-sell"] = {
            "client_order_id": "existing-sell", "status": "SUBMITTED",
            "total_quantity": "1", "filled_quantity": "0",
        }

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(),
            self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertFalse(self.runner._emergency_account_flat)

    def test_emergency_stop_reports_flat_only_after_empty_fresh_account_and_orders(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = ()

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertTrue(self.runner._emergency_account_flat)
        self.assertEqual(self.runner._emergency_unresolved, [])

    def test_emergency_stop_reports_unsupported_equity_and_does_not_claim_flat(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (
            SimpleNamespace(instrument_type="EQUITY", symbol="XYZ"),
        )

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(),
            self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertFalse(self.runner._emergency_account_flat)
        self.assertIn("unsupported non-option position", self.runner._emergency_unresolved)
        self.assertEqual(self.coordinator.exit_calls, [])

    def test_emergency_stop_waits_for_new_account_response_and_never_repeats_filled_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(2),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(len(self.coordinator.exit_calls), 1)

        # The same pre-order account response cannot authorize another sell.
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(len(self.coordinator.exit_calls), 1)

        # A later response that still conflicts with the confirmed fill also
        # blocks a duplicate market sell.
        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(len(self.coordinator.exit_calls), 1)
        self.assertTrue(self.runner._emergency_unresolved)

    def test_emergency_stop_cancels_account_opening_order_then_waits_to_refresh(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(1),)
        self.coordinator.broker_open_orders = [{
            "client_order_id": "open-buy", "symbol": "SPY",
            "side": "BUY", "position_intent": "BUY_TO_OPEN",
            "quantity": "1", "filled_quantity": "0", "status": "SUBMITTED",
            "legs": [{
                "symbol": "SPY", "side": "BUY", "quantity": "1",
                "strike_price": "761", "option_expire_date": "2026-11-05",
                "option_type": "CALL", "instrument_type": "OPTION",
            }],
        }]

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.cancel_external_calls, ["open-buy"])
        self.assertEqual(self.coordinator.exit_calls, [])

        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(len(self.coordinator.exit_calls), 1)

    def test_partial_buy_cancel_fill_is_persisted_and_liquidated_from_the_ledger(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "cancel-race"
        open_buy = {
            "client_order_id": "external-buy-partial", "symbol": "SPY",
            "side": "BUY", "position_intent": "BUY_TO_OPEN",
            "quantity": "2", "status": "SUBMITTED",
            "legs": [{
                "symbol": "SPY", "side": "BUY", "quantity": "2",
                "strike_price": "761", "option_expire_date": "2026-11-05",
                "option_type": "CALL", "instrument_type": "OPTION",
            }],
        }
        self.coordinator.broker_open_orders = [open_buy]
        self.coordinator.external_cancel_results["external-buy-partial"] = {
            "client_order_id": "external-buy-partial", "status": "CANCELLED",
            "total_quantity": "2", "filled_quantity": "1", "filled_price": "2.00",
        }

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.cancel_external_calls, ["external-buy-partial"])
        self.assertEqual(self.coordinator.external_entry_states[0]["filled_quantity"], "1")
        self.assertEqual(self.coordinator.exit_calls, [])

        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(len(self.coordinator.exit_calls), 1)
        self.assertEqual(self.coordinator.exit_calls[0]["quantity"], 1)
        self.assertEqual(self.coordinator.exit_calls[0]["request"]["order_type"], "MARKET")
        self.assertEqual(self.coordinator.exit_calls[0]["request"]["position_intent"], "SELL_TO_CLOSE")
        self.assertNotIn("limit_price", self.coordinator.exit_calls[0]["request"])
        self.assertFalse(self.runner._emergency_account_flat)

    def test_buy_cancel_that_races_to_filled_state_is_counted_before_any_flat_result(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "buy-fill-race"
        self.coordinator.broker_open_orders = [{
            "client_order_id": "external-buy-filled", "symbol": "SPY",
            "side": "BUY", "position_intent": "BUY_TO_OPEN", "quantity": "2",
            "status": "SUBMITTED",
            "legs": [{
                "symbol": "SPY", "side": "BUY", "quantity": "2",
                "strike_price": "761", "option_expire_date": "2026-11-05",
                "option_type": "CALL", "instrument_type": "OPTION",
            }],
        }]
        self.coordinator.external_cancel_results["external-buy-filled"] = {
            "client_order_id": "external-buy-filled", "status": "FILLED",
            "total_quantity": "2", "filled_quantity": "2", "filled_price": "2.00",
        }

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.external_entry_states[0]["status"], "FILLED")
        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.exit_calls[0]["quantity"], 2)
        self.assertFalse(self.runner._emergency_account_flat)

    def test_confirmed_buy_with_empty_account_snapshot_is_not_missed(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "buy-hidden-in-account-snapshot"
        self.coordinator._orders.append({
            "client_order_id": "confirmed-buy", "role": "ENTRY",
            "direction": "CALL", "contract": {
                "underlying_symbol": "SPY", "strike": "761",
                "expiry": "2026-11-05", "option_type": "CALL",
            },
            "quantity": 2, "filled_quantity": 2, "filled_price": "2.00",
            "cancelled_quantity": 0, "unresolved_quantity": 0,
            "status": "FILLED",
        })
        self.account_monitor.account_data.positions = ()

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.exit_calls[0]["quantity"], 2)
        self.assertFalse(self.runner._emergency_account_flat)

        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertTrue(self.runner._emergency_account_flat)

    def test_stale_account_quantity_cannot_override_confirmed_buy_quantity(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "old-account-quantity"
        self.coordinator._orders.append({
            "client_order_id": "confirmed-buy-2", "role": "ENTRY",
            "direction": "CALL", "contract": {
                "underlying_symbol": "SPY", "strike": "761",
                "expiry": "2026-11-05", "option_type": "CALL",
            },
            "quantity": 2, "filled_quantity": 2, "filled_price": "2.00",
            "cancelled_quantity": 0, "unresolved_quantity": 0,
            "status": "FILLED",
        })
        self.account_monitor.account_data.positions = (self.account_option_position(1),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.exit_calls[0]["quantity"], 2)
        self.assertFalse(self.runner._emergency_account_flat)

    def test_stale_position_snapshot_after_partial_sell_does_not_submit_another_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "partial-sell-stale-position"
        contract = {
            "underlying_symbol": "SPY", "strike": "761",
            "expiry": "2026-11-05", "option_type": "CALL",
        }
        self.coordinator._orders.extend([
            {"client_order_id": "buy-2", "role": "ENTRY", "direction": "CALL",
             "contract": contract, "quantity": 2, "filled_quantity": 2,
             "filled_price": "2.00", "status": "FILLED"},
            {"client_order_id": "sell-partial", "role": "EXIT", "direction": "CALL",
             "contract": contract, "quantity": 2, "filled_quantity": 1,
             "filled_price": "1.90", "status": "PARTIALLY_FILLED"},
        ])
        self.account_monitor.account_data.positions = (self.account_option_position(2),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertFalse(self.runner._emergency_account_flat)
        self.assertTrue(self.runner._emergency_unresolved)

    def test_unknown_sell_status_blocks_duplicate_emergency_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.coordinator._orders.extend([
            {"client_order_id": "buy-known", "role": "ENTRY", "direction": "CALL",
             "contract": {"underlying_symbol": "SPY", "strike": "761",
                          "expiry": "2026-11-05", "option_type": "CALL"},
             "quantity": 2, "filled_quantity": 2, "filled_price": "2.00", "status": "FILLED"},
            {"client_order_id": "sell-unknown", "role": "EXIT", "direction": "CALL",
             "contract": {"underlying_symbol": "SPY", "strike": "761",
                          "expiry": "2026-11-05", "option_type": "CALL"},
             "quantity": 2, "filled_quantity": 0, "status": "UNKNOWN"},
        ])
        self.account_monitor.account_data.positions = (self.account_option_position(2),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertFalse(self.runner._emergency_account_flat)
        self.assertTrue(self.runner._emergency_unresolved)

    def test_unreconciled_confirmed_sell_fill_cannot_be_reported_flat(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "position-order-mismatch"
        contract = {
            "underlying_symbol": "SPY", "strike": "761",
            "expiry": "2026-11-05", "option_type": "CALL",
        }
        self.coordinator._orders.extend([
            {"client_order_id": "buy-one", "role": "ENTRY", "direction": "CALL",
             "contract": contract, "quantity": 1, "filled_quantity": 1,
             "filled_price": "2.00", "status": "FILLED"},
            {"client_order_id": "sell-one", "role": "EXIT", "direction": "CALL",
             "contract": contract, "quantity": 1, "filled_quantity": 1,
             "filled_price": "1.90", "status": "FILLED"},
        ])
        self.coordinator.get_or_create_emergency_baseline(
            ("SPY", "761", "2026-11-05", "CALL"), 0,
            emergency_id=self.runner._emergency_id,
        )
        self.account_monitor.account_data.positions = (self.account_option_position(1),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertFalse(self.runner._emergency_account_flat)
        self.assertTrue(self.runner._emergency_unresolved)
        self.assertEqual(self.coordinator.exit_calls, [])

    def test_old_account_quantity_after_fully_filled_sell_cannot_trigger_another_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.runner._emergency_id = "fully-sold-stale-position"
        contract = {
            "underlying_symbol": "SPY", "strike": "761",
            "expiry": "2026-11-05", "option_type": "CALL",
        }
        self.coordinator._orders.extend([
            {"client_order_id": "buy-full", "role": "ENTRY", "direction": "CALL",
             "contract": contract, "quantity": 1, "filled_quantity": 1,
             "filled_price": "2.00", "status": "FILLED"},
            {"client_order_id": "sell-full", "role": "EXIT", "direction": "CALL",
             "contract": contract, "quantity": 1, "filled_quantity": 1,
             "filled_price": "1.90", "status": "FILLED"},
        ])
        # The account response still reflects the pre-sale position.
        self.account_monitor.account_data.positions = (self.account_option_position(1),)

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertFalse(self.runner._emergency_account_flat)
        self.assertTrue(self.runner._emergency_unresolved)

    def test_concurrent_emergency_passes_cannot_use_the_same_position_twice(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCoordinator(FakeCoordinator):
            def list_open_orders(inner_self):
                entered.set()
                release.wait(timeout=2)
                return super(BlockingCoordinator, inner_self).list_open_orders()

        coordinator = BlockingCoordinator()
        self.runner._order_coordinator = coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(1),)
        first = threading.Thread(target=self.runner._emergency_close_account_positions,
                                 args=(self.account_monitor.get_snapshot(),
                                       self.account_monitor.account_data, coordinator))
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            coordinator,
        )
        release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(coordinator.exit_calls), 1)

    def test_emergency_stop_fails_closed_on_stale_account_snapshot(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(1),)
        self.account_monitor.snapshot_is_fresh = lambda: False

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )

        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertFalse(self.runner._emergency_account_flat)

    def test_emergency_stop_does_not_repeat_an_external_filled_sell(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._emergency = True
        self.account_monitor.account_data.positions = (self.account_option_position(1),)
        self.coordinator.broker_open_orders = [{
            "client_order_id": "external-sell", "symbol": "SPY",
            "side": "SELL", "position_intent": "SELL_TO_CLOSE",
            "quantity": "1", "filled_quantity": "1", "status": "FILLED",
            "legs": [{
                "symbol": "SPY", "strike_price": "761",
                "option_expire_date": "2026-11-05", "option_type": "CALL",
            }],
        }]
        self.coordinator.confirmed_orders["external-sell"] = {
            "client_order_id": "external-sell", "status": "FILLED",
            "total_quantity": "1", "filled_quantity": "1",
        }

        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertEqual(
            self.coordinator.emergency_baselines[
                f"{self.runner._emergency_id}|SPY|761|2026-11-05|CALL"
            ]
            ["external_exit_fills"]["external-sell"],
            1,
        )

        self.account_monitor.polled_at_monotonic = 102
        self.runner._emergency_close_account_positions(
            self.account_monitor.get_snapshot(), self.account_monitor.account_data,
            self.coordinator,
        )
        self.assertEqual(self.coordinator.exit_calls, [])
        self.assertTrue(self.runner._emergency_unresolved)

    @patch("strategy1_runner.log_event")
    def test_filled_buy_creates_live_position_from_actual_fill(self, log_event):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._account_positions_reconciled = True
        self.runner.positions = {"CALL": None, "PUT": None}
        self.runner._submit_live_entry(
            "CALL", self.contract, {"ask": 2.10, "bid": 2.05}, self.params,
            {"eligible": True, "trigger_price": 2.0},
            {"account_data": self.account_monitor.account_data}, self.coordinator,
        )
        self.runner._sync_live_positions(self.coordinator)
        position = self.runner.positions["CALL"]
        self.assertEqual(position["status"], "OPEN — LIVE")
        self.assertEqual(position["entry_price"], 2.08)
        self.assertEqual(position["quantity"], 2)
        self.assertEqual(position["entry_client_order_id"], "entry-1")
        self.assertLessEqual(position["entry_price"] * 100 * position["quantity"], 500)
        self.assertEqual(self.account_monitor.account_api_calls, 0)
        self.assertTrue(log_event.called)

    @patch("strategy1_runner.log_event")
    def test_take_profit_submits_real_sell_to_close_and_waits_for_fill(self, _log_event):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._account_positions_reconciled = True
        self.coordinator._orders.append({
            "client_order_id": "entry-call", "order_id": "entry-order-call",
            "signal_key": "signal-call", "position_key": "entry-call",
            "role": "ENTRY", "direction": "CALL",
            "contract": {"symbol": self.contract["symbol"], "strike": "761",
                         "expiry": "2026-11-05", "option_type": "CALL"},
            "quantity": 1, "filled_quantity": 1, "filled_price": "2.00",
            "limit_price": "2.10", "status": "FILLED",
            "take_profit_pct": 5, "stop_loss_pct": 5,
        })
        self.runner._sync_live_positions(self.coordinator)
        self.runner._engine = Mock()
        self.runner._engine.get_option_snapshot.return_value = {
            "bid": 2.10, "ask": 2.15, "quote_valid": True,
        }

        self.runner._manage_position("CALL", self.runner.positions["CALL"])

        exit_order = self.coordinator.orders()[-1]
        self.assertEqual(exit_order["role"], "EXIT")
        self.assertEqual(exit_order["filled_quantity"], 1)
        self.assertEqual(self.runner.positions["CALL"], None)

    def test_rejected_exit_does_not_strand_confirmed_position(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._account_positions_reconciled = True
        self.coordinator._orders.extend([
            {
                "client_order_id": "entry-call", "position_key": "entry-call",
                "role": "ENTRY", "direction": "CALL",
                "contract": {"symbol": self.contract["symbol"], "strike": "761",
                             "expiry": "2026-11-05", "option_type": "CALL"},
                "quantity": 1, "filled_quantity": 1, "filled_price": "2.00",
                "limit_price": "2.10", "status": "FILLED",
                "take_profit_pct": 5, "stop_loss_pct": 5,
            },
            {
                "client_order_id": "rejected-exit", "position_key": "entry-call",
                "role": "EXIT", "direction": "CALL", "quantity": 1,
                "filled_quantity": 0, "status": "REJECTED",
            },
        ])
        self.runner._sync_live_positions(self.coordinator)
        self.assertIsNotNone(self.runner.positions["CALL"])
        self.assertEqual(self.runner.positions["CALL"]["status"], "EXIT REJECTED — RETRYING")

        self.runner._engine = Mock()
        self.runner._engine.get_option_snapshot.return_value = {
            "bid": 1.80, "ask": 1.85, "quote_valid": True,
        }
        self.runner._emergency = True
        self.runner._manage_position("CALL", self.runner.positions["CALL"])

        self.assertEqual(self.coordinator.orders()[-1]["role"], "EXIT")
        self.assertEqual(self.coordinator.orders()[-1]["status"], "FILLED")
        self.assertIsNone(self.runner.positions["CALL"])

    def test_partial_sell_keeps_only_the_unclosed_filled_quantity(self):
        self.runner._order_coordinator = self.coordinator
        self.coordinator._orders.extend([
            {
                "client_order_id": "entry-call", "position_key": "entry-call",
                "role": "ENTRY", "direction": "CALL",
                "contract": {"symbol": self.contract["symbol"], "strike": "761",
                             "expiry": "2026-11-05", "option_type": "CALL"},
                "quantity": 2, "filled_quantity": 2, "filled_price": "2.00",
                "limit_price": "2.10", "status": "FILLED",
                "take_profit_pct": 5, "stop_loss_pct": 5,
            },
            {
                "client_order_id": "partial-exit", "position_key": "entry-call",
                "role": "EXIT", "direction": "CALL", "quantity": 2,
                "filled_quantity": 1, "filled_price": "2.10",
                "status": "PARTIALLY_FILLED",
            },
        ])

        self.runner._sync_live_positions(self.coordinator)

        self.assertEqual(self.runner.positions["CALL"]["quantity"], 1)
        self.assertEqual(
            self.runner.positions["CALL"]["status"],
            "EXIT PARTIALLY_FILLED — LIVE",
        )

    @patch("strategy1_runner.evaluate_entry", return_value={"eligible": True, "trigger_price": 2.0})
    @patch("strategy1_runner.log_event")
    def test_buy_signal_uses_cached_account_snapshot_and_never_reads_account_api(self, _log_event, _evaluate):
        engine = Mock()
        engine.get_market_snapshot.return_value = {
            "entry_data_valid": True, "spy_price": 101.5,
            "ema9": 101.2, "ema21": 101, "ema50": 100,
            "ema_gap": 1.2, "ema_gap_5m_ago": 1.0,
            "five_min_trend": "UP",
        }
        engine.get_nearest_otm_option_snapshot.return_value = {
            "contract": self.contract,
            "quote": {"ask": 2.1, "bid": 2.05, "quote_age_seconds": 1},
        }
        engine.option_history.get_recent_low.return_value = {"history_valid": True, "recent_low": 2.0}
        self.runner._engine = engine
        self.runner._account_positions_reconciled = True
        from state import state, state_lock
        with state_lock:
            state["risk_latched"] = False
        self.runner.run_once()
        self.assertEqual(len(self.coordinator.submit_calls), 1)
        self.assertGreaterEqual(self.account_monitor.snapshot_calls, 1)
        self.assertEqual(self.account_monitor.account_api_calls, 0)
        engine.get_market_snapshot.assert_called_once()

    def test_low_budget_and_low_option_power_reject_new_entry(self):
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.runner._account_positions_reconciled = True
        with self.assertRaises(ValueError):
            self.coordinator.submit_entry(
                signal_key="small", contract=self.contract, direction="CALL", price=6,
                max_trade_amount=500, allocated_amount=0, option_buying_power=10000,
                take_profit_pct=5, stop_loss_pct=5,
            )
        with self.assertRaises(ValueError):
            self.coordinator.submit_entry(
                signal_key="bp-small", contract=self.contract, direction="PUT", price=2,
                max_trade_amount=500, allocated_amount=0, option_buying_power=50,
                take_profit_pct=5, stop_loss_pct=5,
            )
        self.assertEqual(self.coordinator.submit_calls, [])

    def test_account_monitor_failure_does_not_stop_existing_live_exit_management(self):
        self.account_monitor.allowed = False
        self.account_monitor.account_data.positions = (SimpleNamespace(symbol="SPY", instrument_type="OPTION", currency="USD", quantity=Decimal("1"), option_strategy="SINGLE", legs=(SimpleNamespace(symbol="SPY", quantity=Decimal("1"), option_type="PUT", expiration_date="2026-11-05", exercise_price=Decimal("759")),)),)
        self.runner._order_coordinator = self.coordinator
        self.runner._order_account_id = "OFFLINE-MARGIN"
        self.coordinator._orders.append({
            "client_order_id": "entry-put", "order_id": "entry-order-put",
            "signal_key": "signal-put", "position_key": "entry-put",
            "role": "ENTRY", "direction": "PUT",
            "contract": {"symbol": "SPY261105P00759000", "strike": "759",
                         "expiry": "2026-11-05", "option_type": "PUT"},
            "quantity": 1, "filled_quantity": 1, "filled_price": "2.00",
            "limit_price": "2.10", "status": "FILLED",
            "take_profit_pct": 5, "stop_loss_pct": 5,
        })
        self.account_monitor.account_data.positions = (
            self.account_option_position(1, direction="PUT", strike="759"),
        )
        self.runner._sync_live_positions(self.coordinator)
        self.runner._engine = Mock()
        self.runner._engine.get_market_snapshot.return_value = {"entry_data_valid": True}
        self.runner._engine.get_option_snapshot.return_value = {
            "bid": 1.90, "ask": 1.95, "quote_valid": True,
        }
        self.runner._accept_entries = True

        self.runner.run_once()

        self.assertEqual(self.coordinator.orders()[-1]["role"], "EXIT")
        self.assertIsNone(self.runner.positions["PUT"])
        self.runner._engine.get_nearest_otm_option_snapshot.assert_not_called()

    def test_ordinary_stop_keeps_managing_open_live_position(self):
        self.runner.positions["CALL"] = {"symbol": self.contract["symbol"]}
        self.runner._worker_active = True
        self.runner._accept_entries = True
        self.runner.stop_new_entries()
        self.assertFalse(self.runner._accept_entries)
        self.assertTrue(self.runner._worker_active)


if __name__ == "__main__":
    unittest.main()
