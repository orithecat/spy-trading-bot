"""Offline integration coverage for Dashboard controls and Strategy 1 runner."""

import copy
import io
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import app
import trade_logger
from state import settings, state
from strategy1_runner import runner
from state import state_lock
from tests.test_strategy1_runner import FakeAccountMonitor, FakeCoordinator


class FakeOptionHistory:
    def get_recent_low(self, **kwargs):
        return {
            "symbol": kwargs["option_symbol"],
            "recent_low": 2.0,
            "history_valid": True,
        }


class FakeReadOnlyMarketEngine:
    """Deterministic feed with tripwires for all order-changing operations."""

    def __init__(self):
        self.market = {
            "spy_price": 101.5,
            "ema9": 101.2,
            "ema21": 101.0,
            "ema50": 100.0,
            "ema_gap": 1.2,
            "ema_gap_5m_ago": 1.0,
            "five_min_trend": "UP",
            "entry_data_valid": True,
            "signal_status": "Market data valid",
        }
        self.entry_quote = {
            "bid": 2.09,
            "ask": 2.10,
            "quote_age_seconds": 1,
            "quote_valid": True,
        }
        self.management_quote = {
            "bid": 2.20,
            "ask": 2.22,
            "quote_valid": True,
        }
        self.option_history = FakeOptionHistory()
        self.market_calls = 0
        self.contract_calls = []
        self.order_methods = {
            name: Mock(side_effect=AssertionError("real order path reached"))
            for name in (
                "place_order",
                "submit_order",
                "modify_order",
                "replace_order",
                "cancel_order",
            )
        }

    def get_market_snapshot(self):
        self.market_calls += 1
        return dict(self.market)

    def get_nearest_otm_option_snapshot(self, *, expiry, option_type, spy_price):
        self.contract_calls.append(option_type)
        return {
            "contract": {
                "symbol": f"SPY-TEST-{option_type}",
                "strike": 102 if option_type == "CALL" else 100,
                "expiry": expiry,
            },
            "quote": dict(self.entry_quote),
        }

    def get_option_snapshot(self, symbol):
        return dict(self.management_quote)


class DashboardRequestHarness:
    """Invoke the real Handler method without opening a network socket."""

    def __init__(self, action):
        payload = json.dumps({"strategy": "strategy1", "action": action})
        self.path = "/api/control"
        self.headers = {"Content-Length": str(len(payload.encode("utf-8")))}
        self.rfile = io.BytesIO(payload.encode("utf-8"))
        self.status = None
        self.result = None

    def send_json(self, data, status=200):
        self.status = status
        self.result = data


class DashboardRunnerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_state = copy.deepcopy(state)
        cls.original_settings = copy.deepcopy(settings)

    @classmethod
    def tearDownClass(cls):
        with state_lock:
            state.clear()
            state.update(cls.original_state)
            settings.clear()
            settings.update(cls.original_settings)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="spy-dashboard-tests-")
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)

        self.old_runner = {
            "accept_entries": runner._accept_entries,
            "worker_active": runner._worker_active,
            "emergency": runner._emergency,
            "emergency_id": runner._emergency_id,
            "thread": runner._thread,
            "engine": runner._engine,
            "account_monitor": runner.account_monitor,
            "order_coordinator": runner._order_coordinator,
            "order_account_id": runner._order_account_id,
            "positions_reconciled": runner._account_positions_reconciled,
            "order_state_path": runner.order_state_path,
            "order_coordinator_factory": runner.order_coordinator_factory,
            "positions": copy.deepcopy(runner.positions),
            "last_error": runner.last_error,
            "real_trading_authorized": runner.real_trading_authorized,
        }
        self.addCleanup(self._restore_runner)

        with state_lock:
            state["active_strategy"] = None
            state["today_total_pnl"] = None
            state["risk_latched"] = False
            state["risk_message"] = "Test risk state"
            for strategy in state["strategies"].values():
                strategy.update({
                    "running": False,
                    "emergency_stop": False,
                    "message": "Test state",
                })

        self.engine = FakeReadOnlyMarketEngine()
        self.account_monitor = FakeAccountMonitor()
        self.coordinator = FakeCoordinator()
        with runner._lock:
            runner._accept_entries = False
            runner.real_trading_authorized = True
            runner._worker_active = False
            runner._emergency = False
            runner._emergency_id = None
            runner._thread = None
            runner._engine = self.engine
            runner.account_monitor = self.account_monitor
            runner._order_coordinator = self.coordinator
            runner._order_account_id = "OFFLINE-MARGIN"
            runner._account_positions_reconciled = True
            runner.order_state_path = self.temp_path / "orders.json"
            runner.order_coordinator_factory = lambda *_args, **_kwargs: self.coordinator
            runner.positions = {"CALL": None, "PUT": None}
            runner.last_error = None

        self.log_dir = self.temp_path / "logs"
        self.old_log_dir = trade_logger.LOG_DIR
        trade_logger.LOG_DIR = self.log_dir
        self.addCleanup(self._restore_log_dir)

        self.old_emergency_file = app.EMERGENCY_STATE_FILE
        app.EMERGENCY_STATE_FILE = self.temp_path / "emergency_state.json"
        self.addCleanup(self._restore_emergency_file)

        self.worker_patch = patch.object(runner, "_worker", Mock())
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.addCleanup(self._stop_runner_thread)

        # Entry time rules are covered by the dedicated trading-hours unit tests.
        self.entry_time_patch = patch("strategies.strategy1.get_entry_block_reason", return_value=None)
        self.entry_time_patch.start()
        self.addCleanup(self.entry_time_patch.stop)

    def _restore_runner(self):
        old = self.old_runner
        with runner._lock:
            runner._accept_entries = old["accept_entries"]
            runner._worker_active = old["worker_active"]
            runner._emergency = old["emergency"]
            runner._emergency_id = old["emergency_id"]
            runner._thread = old["thread"]
            runner._engine = old["engine"]
            runner.account_monitor = old["account_monitor"]
            runner._order_coordinator = old["order_coordinator"]
            runner._order_account_id = old["order_account_id"]
            runner._account_positions_reconciled = old["positions_reconciled"]
            runner.order_state_path = old["order_state_path"]
            runner.order_coordinator_factory = old["order_coordinator_factory"]
            runner.positions = old["positions"]
            runner.last_error = old["last_error"]
            runner.real_trading_authorized = old["real_trading_authorized"]

    def _restore_log_dir(self):
        trade_logger.LOG_DIR = self.old_log_dir

    def _restore_emergency_file(self):
        app.EMERGENCY_STATE_FILE = self.old_emergency_file

    def _stop_runner_thread(self):
        thread = runner._thread
        if thread:
            thread.join(timeout=2)

    def post_control(self, action):
        request = DashboardRequestHarness(action)
        app.Handler.do_POST(request)
        return request.status, request.result

    def test_persisted_emergency_stop_resumes_after_restart_only_when_latched(self):
        app.EMERGENCY_STATE_FILE.write_text(json.dumps({
            "latched": True, "emergency_id": "offline-emergency-cycle",
        }))
        with patch.object(runner, "emergency_stop") as emergency:
            self.assertTrue(app.resume_persisted_emergency_stop())
            emergency.assert_called_once_with(
                emergency_id="offline-emergency-cycle"
            )

        app.EMERGENCY_STATE_FILE.unlink()
        with patch.object(runner, "emergency_stop") as emergency:
            self.assertFalse(app.resume_persisted_emergency_stop())
            emergency.assert_not_called()

    def seed_live_position(self, direction="CALL"):
        strike = 102 if direction == "CALL" else 100
        contract = {
            "symbol": f"SPY-TEST-{direction}", "strike": str(strike),
            "expiry": "2026-11-05", "option_type": direction,
        }
        self.coordinator._orders.append({
            "client_order_id": f"entry-{direction}", "order_id": f"order-{direction}",
            "signal_key": f"signal-{direction}", "position_key": f"entry-{direction}",
            "role": "ENTRY", "direction": direction, "contract": contract,
            "quantity": 1, "filled_quantity": 1, "filled_price": "2.00",
            "limit_price": "2.10", "status": "FILLED",
            "take_profit_pct": 4, "stop_loss_pct": 5,
        })
        leg = SimpleNamespace(
            symbol="SPY", option_type=direction,
            exercise_price=strike, expiration_date="2026-11-05", quantity=1,
        )
        self.account_monitor.account_data.positions = (
            SimpleNamespace(instrument_type="OPTION", symbol="SPY", quantity=1,
                             option_strategy="SINGLE", legs=(leg,)),
        )
        runner._sync_live_positions(self.coordinator)

    def assert_no_order_operations(self):
        for method in self.engine.order_methods.values():
            method.assert_not_called()

    def test_start_submits_real_order_and_builds_position_from_mock_fill(self):
        status, result = self.post_control("start")
        self.assertEqual(status, 200, result)
        self.assertTrue(runner._accept_entries)

        runner.run_once()

        self.assertEqual(runner.positions["CALL"]["status"], "OPEN — LIVE")
        self.assertEqual(self.engine.contract_calls, ["CALL"])
        self.assertEqual(len(self.coordinator.submit_calls), 1)
        self.assert_no_order_operations()

    def test_start_when_real_authorization_is_off_only_runs_position_management(self):
        runner.real_trading_authorized = False
        status, result = self.post_control("start")
        self.assertEqual(status, 200, result)
        self.assertFalse(runner._accept_entries)

        runner.run_once()

        self.assertEqual(self.coordinator.submit_calls, [])
        self.assertIn("not authorized", state["strategies"]["strategy1"]["message"].lower())
        self.assert_no_order_operations()

    def test_account_risk_denial_blocks_new_entries(self):
        self.account_monitor.allowed = False
        status, result = self.post_control("start")
        self.assertEqual(status, 200, result)

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.engine.contract_calls, [])
        self.assert_no_order_operations()

    def test_ordinary_stop_blocks_entries_but_manages_and_exits_existing_position(self):
        self.seed_live_position()
        runner._accept_entries = True
        runner._worker_active = True

        status, result = self.post_control("stop")
        self.assertEqual(status, 200, result)
        self.assertFalse(runner._accept_entries)
        self.assertTrue(runner._worker_active)

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.coordinator.orders()[-1]["role"], "EXIT")
        self.assertEqual(self.engine.contract_calls, [])
        self.assert_no_order_operations()

    def test_emergency_stop_blocks_entries_account_wide_and_keeps_exit_management(self):
        self.seed_live_position()

        status, result = self.post_control("emergency_stop")
        self.assertEqual(status, 200, result)
        self.assertFalse(runner._accept_entries)
        self.assertTrue(runner._emergency)
        self.assertTrue(all(item["emergency_stop"] for item in state["strategies"].values()))

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.coordinator.orders()[-1]["role"], "EXIT")
        self.assertEqual(self.engine.contract_calls, [])
        self.assert_no_order_operations()

    def test_start_cannot_clear_an_emergency_stop_while_liquidation_is_unresolved(self):
        self.post_control("emergency_stop")

        status, result = self.post_control("start")
        self.assertEqual(status, 409, result)
        self.assertFalse(runner._accept_entries)
        self.assertTrue(runner._emergency)
        self.assertTrue(app.load_emergency_latch())
        self.assertTrue(all(item["emergency_stop"] for item in state["strategies"].values()))
        self.assert_no_order_operations()

    def test_start_resumes_only_after_emergency_liquidation_is_confirmed(self):
        self.post_control("emergency_stop")
        runner._emergency_account_flat = True
        runner._emergency_unresolved = []

        status, result = self.post_control("start")

        self.assertEqual(status, 200, result)
        self.assertTrue(runner._accept_entries)
        self.assertFalse(runner._emergency)
        self.assertFalse(app.load_emergency_latch())
        self.assertTrue(all(not item["emergency_stop"] for item in state["strategies"].values()))
        self.assert_no_order_operations()

    def test_invalid_market_data_and_stale_candidate_cannot_authorize_entry(self):
        state["strategies"]["strategy1"]["candidates"] = {
            "CALL": {"eligible": True, "status": "eligible", "symbol": "OLD"},
            "PUT": None,
        }
        self.engine.market.update({
            "entry_data_valid": False,
            "signal_status": "Blocked: stale one-minute data",
        })
        self.post_control("start")

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.engine.contract_calls, [])
        self.assertEqual(
            state["strategies"]["strategy1"]["candidates"]["CALL"]["symbol"],
            "OLD",
        )
        self.assert_no_order_operations()

    def test_stale_quote_and_previous_eligible_candidate_cannot_authorize_entry(self):
        state["strategies"]["strategy1"]["candidates"] = {
            "CALL": {"eligible": True, "status": "eligible", "symbol": "OLD"},
            "PUT": None,
        }
        self.engine.entry_quote["quote_age_seconds"] = 30
        self.post_control("start")

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.engine.contract_calls, ["CALL", "PUT"])
        self.assert_no_order_operations()

    def test_latched_daily_risk_state_blocks_new_entries(self):
        state["risk_latched"] = True
        self.post_control("start")

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.engine.contract_calls, [])
        self.assert_no_order_operations()

    def test_invalid_or_missing_risk_latch_fails_closed(self):
        self.post_control("start")

        for invalid_state in (None, "invalid", "missing"):
            with self.subTest(risk_latched=invalid_state):
                if invalid_state == "missing":
                    state.pop("risk_latched", None)
                else:
                    state["risk_latched"] = invalid_state

                runner.run_once()

                self.assertIsNone(runner.positions["CALL"])
                self.assertEqual(self.engine.contract_calls, [])
                self.assert_no_order_operations()

    def test_invalid_risk_latch_still_manages_existing_position(self):
        state["risk_latched"] = None
        self.seed_live_position()
        runner._accept_entries = True
        runner._worker_active = True

        runner.run_once()

        self.assertIsNone(runner.positions["CALL"])
        self.assertEqual(self.engine.contract_calls, [])
        self.assert_no_order_operations()


if __name__ == "__main__":
    unittest.main()
