"""Strategy 1 real-order runner with persisted fill-based position management."""

import math
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from account_monitor import AccountRiskPollingService
from config import REAL_TRADING_AUTHORIZED
from market_data import MarketDataEngine
from order_execution import ACTIVE_STATUSES, OrderCoordinator, OrderStateError
from state import settings, state, state_lock
from strategies.strategy1 import evaluate_entry
from trade_logger import log_event


NY = ZoneInfo("America/New_York")
POLL_SECONDS = 30
DIRECTIONS = ("CALL", "PUT")


class Strategy1Runner:
    """Background S1 scanner, real order manager, and position manager."""

    def __init__(
        self,
        engine_factory=MarketDataEngine,
        poll_seconds=POLL_SECONDS,
        account_monitor=None,
        order_state_path=None,
        order_coordinator_factory=OrderCoordinator,
        real_trading_authorized=REAL_TRADING_AUTHORIZED,
    ):
        self.engine_factory = engine_factory
        self.poll_seconds = poll_seconds
        self.account_monitor = account_monitor or AccountRiskPollingService()
        self.order_state_path = (
            Path(order_state_path) if order_state_path is not None
            else Path(__file__).resolve().parent / "data" / "strategy1_orders.json"
        )
        self.order_coordinator_factory = order_coordinator_factory
        self.real_trading_authorized = real_trading_authorized is True
        self._order_coordinator = None
        self._order_account_id = None
        self._account_positions_reconciled = False
        self._lock = threading.RLock()
        self._emergency_liquidation_lock = threading.Lock()
        self._thread = None
        self._engine = None
        self._accept_entries = False
        self._worker_active = False
        self._emergency = False
        self._emergency_id = None
        self._emergency_unresolved = []
        self._emergency_strategy1_flat = False
        self._emergency_account_flat = False
        self._emergency_refresh_after = None
        self.positions = {"CALL": None, "PUT": None}
        self.last_error = None

    @staticmethod
    def _expiry_date(expiry):
        today = datetime.now(NY).date()
        if expiry == "0DTE":
            return today
        day = today + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day

    @staticmethod
    def _number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    def start(self):
        self.account_monitor.start()
        with self._lock:
            if self._emergency and not self._emergency_stop_complete_locked():
                self._set_message(
                    "Emergency Stop remains active; START is blocked until account liquidation is confirmed."
                )
                return False
            self._accept_entries = self.real_trading_authorized
            self._emergency = False
            self._emergency_id = None
            self._emergency_account_flat = False
            self._emergency_unresolved = []
            self._emergency_refresh_after = None
            self._worker_active = True
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._worker,
                name="strategy1-live-runner",
                daemon=True,
            )
            self._thread.start()
        return True

    def _emergency_stop_complete_locked(self):
        coordinator = self._order_coordinator
        return (
            self._emergency_account_flat is True
            and not self._emergency_unresolved
            and not (coordinator and coordinator.active_orders())
        )

    def emergency_stop_complete(self):
        """Return true only after liquidation and order reconciliation finish."""
        with self._lock:
            return self._emergency_stop_complete_locked()

    def stop_new_entries(self):
        """Ordinary STOP: stop entries but keep managing open positions."""
        with self._lock:
            self._accept_entries = False
            pending_entries = bool(
                self._order_coordinator
                and self._order_coordinator.active_orders(role="ENTRY")
            )
            pending_recovery = self._order_coordinator is None
            if not any(self.positions.values()) and not pending_entries and not pending_recovery:
                self._worker_active = False
            else:
                self._worker_active = True

    def emergency_stop(self, emergency_id=None):
        """Latch account-wide stop and submit closing orders for live fills."""
        self.account_monitor.start()
        with self._lock:
            unfinished_batch = (
                self._emergency and not self._emergency_stop_complete_locked()
            )
            self._accept_entries = False
            self._emergency = True
            if unfinished_batch:
                # A repeated trigger resumes the same durable batch and keeps
                # its reconciliation barrier and order journal intact.
                emergency_id = self._emergency_id
            else:
                self._emergency_id = (
                    emergency_id if isinstance(emergency_id, str) and emergency_id
                    else uuid.uuid4().hex
                )
                self._emergency_strategy1_flat = False
                self._emergency_account_flat = False
                self._emergency_unresolved = []
                self._emergency_refresh_after = None
            self._worker_active = True
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker,
                    name="strategy1-live-runner",
                    daemon=True,
                )
                self._thread.start()

    def _worker(self):
        try:
            try:
                self._engine = self.engine_factory()
            except Exception:
                with self._lock:
                    emergency = self._emergency
                if not emergency:
                    raise
                # Emergency liquidation uses the verified account snapshot and
                # market orders; it must not depend on market-data startup.
                self._engine = None
            while True:
                with self._lock:
                    active = self._worker_active
                    accepting = self._accept_entries
                    emergency = self._emergency
                    has_positions = any(self.positions.values())
                    has_orders = bool(
                        self._order_coordinator
                        and self._order_coordinator.active_orders()
                    )

                recovering_orders = (
                    self._order_coordinator is None
                    or not self._account_positions_reconciled
                )
                if not active or (not accepting and not has_positions and not has_orders and not recovering_orders and not emergency):
                    break

                try:
                    self.run_once()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                    log_event(
                        "runner_error",
                        strategy="strategy1",
                        error=str(exc),
                        mode="live",
                    )
                    self._set_message(f"Market scan error: {exc}")

                with self._lock:
                    has_positions = any(self.positions.values())
                    has_orders = bool(
                        self._order_coordinator
                        and self._order_coordinator.active_orders()
                    )
                    if self._emergency:
                        self._worker_active = not self._emergency_account_flat
                    elif not self._accept_entries and not has_positions and not has_orders:
                        self._worker_active = (
                            self._order_coordinator is None
                            or not self._account_positions_reconciled
                        )
                    should_continue = self._worker_active

                if not should_continue:
                    break
                time.sleep(self.poll_seconds)
        except Exception as exc:
            self.last_error = str(exc)
            self._set_message(f"Runner initialization failed: {exc}")
            log_event(
                "runner_initialization_error",
                strategy="strategy1",
                error=str(exc),
                mode="live",
            )
        finally:
            self.account_monitor.stop()
            with self._lock:
                self._worker_active = False
                self._engine = None

    def run_once(self):
        """One deterministic scan cycle; useful for mocked offline tests."""
        with self._lock:
            emergency_mode = self._emergency
        if self._engine is None and not emergency_mode:
            raise RuntimeError("Market data engine is not initialized")

        if self._engine is not None:
            market = self._engine.get_market_snapshot()
            self._publish_market(market)
        else:
            market = {"entry_data_valid": False}

        account_snapshot = self.account_monitor.get_snapshot()
        coordinator = self._ensure_order_coordinator(account_snapshot)
        if coordinator is not None:
            try:
                coordinator.refresh_active()
                self._sync_live_positions(coordinator)
                if account_snapshot.get("account_data") is not None:
                    self._account_positions_reconciled = self._reconcile_account_positions(
                        account_snapshot["account_data"]
                    )
            except Exception as exc:
                self.last_error = type(exc).__name__
                self._account_positions_reconciled = False
        elif self._order_coordinator is not None:
            # Keep managing known fills during an account monitor outage. New
            # entries remain blocked by the monitor's local snapshot gate.
            try:
                self._order_coordinator.refresh_active()
                self._sync_live_positions(self._order_coordinator)
            except Exception:
                self._account_positions_reconciled = False

        with self._lock:
            accepting_entries = self._accept_entries
            emergency_stop = self._emergency
        if coordinator is not None and (not accepting_entries or emergency_stop):
            # STOP and EMERGENCY STOP also cancel unfilled entry remainders so
            # they cannot unexpectedly open new risk after the control action.
            for pending in coordinator.active_orders(role="ENTRY"):
                coordinator.cancel(pending["client_order_id"])
            self._sync_live_positions(coordinator)

        if emergency_mode:
            account_data = account_snapshot.get("account_data")
            if coordinator is None or account_data is None:
                self._set_emergency_block("Verified MARGIN account data or order API is unavailable.")
            else:
                self._emergency_close_account_positions(
                    account_snapshot, account_data, coordinator
                )

        with self._lock:
            accepting = self._accept_entries
            emergency = self._emergency
            positions = dict(self.positions)

        # Manage existing positions first, regardless of entry time gates.
        if not emergency:
            for direction, position in positions.items():
                if position is not None:
                    self._manage_position(direction, position)

        with self._lock:
            accepting = self._accept_entries
            emergency = self._emergency

        if (not accepting or emergency or not self.real_trading_authorized
                or not market.get("entry_data_valid")):
            return
        if not self.account_monitor.allows_new_entries():
            return
        if coordinator is None or not self._account_positions_reconciled:
            return
        if coordinator.has_unknown_order():
            self._set_message("Order status is unknown; new entries are locked until reconciled.")
            return

        with state_lock:
            if state.get("risk_latched") is not False:
                return
            strategy_settings = dict(settings["strategy1"])
            strategy_settings["entry_filters"] = dict(
                settings["strategy1"].get("entry_filters", {})
            )

        expiry = self._expiry_date(strategy_settings["expiry"]).isoformat()

        for direction in DIRECTIONS:
            with self._lock:
                if self.positions[direction] is not None:
                    continue
                if not self._accept_entries or self._emergency:
                    break
            if not self.account_monitor.allows_new_entries() or not self._account_positions_reconciled:
                break

            try:
                option_data = self._engine.get_nearest_otm_option_snapshot(
                    expiry=expiry,
                    option_type=direction,
                    spy_price=market["spy_price"],
                )
                contract = option_data["contract"]
                quote = option_data["quote"]
                history = self._engine.option_history.get_recent_low(
                    option_symbol=contract["symbol"],
                    lookback_minutes=30,
                    max_bar_age_seconds=120,
                )
                if not history.get("history_valid"):
                    continue

                decision = evaluate_entry(
                    direction=direction,
                    expiry=strategy_settings["expiry"],
                    ema9=market.get("ema9"),
                    ema21=market.get("ema21"),
                    ema50=market.get("ema50"),
                    ema_gap_now=market.get("ema_gap"),
                    ema_gap_5m_ago=market.get("ema_gap_5m_ago"),
                    spy_price=market.get("spy_price"),
                    five_min_trend=market.get("five_min_trend"),
                    option_low=history.get("recent_low"),
                    option_ask=quote.get("ask"),
                    bid=quote.get("bid"),
                    ask=quote.get("ask"),
                    quote_age_seconds=quote.get("quote_age_seconds"),
                    rebound_trigger_pct=strategy_settings["rebound_trigger_pct"],
                    entry_filters=strategy_settings.get("entry_filters"),
                )
                self._publish_candidate(direction, contract, history, decision)

                if decision.get("eligible"):
                    self._submit_live_entry(
                        direction,
                        contract,
                        quote,
                        strategy_settings,
                        decision,
                        account_snapshot,
                        coordinator,
                    )
                    self._sync_live_positions(coordinator)
                    if self.positions[direction] is not None:
                        self._account_positions_reconciled = False
            except Exception as exc:
                log_event(
                    "entry_scan_error",
                    strategy="strategy1",
                    direction=direction,
                    error=str(exc),
                    mode="live",
                )

    def _ensure_order_coordinator(self, account_snapshot):
        account_data = account_snapshot.get("account_data") if isinstance(account_snapshot, dict) else None
        if getattr(account_data, "account_type", None) != "MARGIN":
            self._account_positions_reconciled = False
            return None
        account_id = getattr(account_data, "account_id", None)
        if not isinstance(account_id, str) or not account_id.strip():
            self._account_positions_reconciled = False
            return None
        if self._order_coordinator is not None:
            if self._order_account_id != account_id:
                self._account_positions_reconciled = False
                return None
            self._account_positions_reconciled = self._reconcile_account_positions(account_data)
            return self._order_coordinator
        order_api_getter = getattr(self.account_monitor, "get_order_api", None)
        order_api = order_api_getter() if callable(order_api_getter) else None
        if order_api is None:
            self._account_positions_reconciled = False
            return None
        try:
            self._order_coordinator = self.order_coordinator_factory(
                order_api, account_id, state_path=self.order_state_path,
                event_logger=log_event,
            )
        except Exception as exc:
            self.last_error = type(exc).__name__
            self._account_positions_reconciled = False
            return None
        self._order_account_id = account_id
        self._account_positions_reconciled = self._reconcile_account_positions(account_data)
        return self._order_coordinator

    def _reconcile_account_positions(self, account_data):
        """Compare option contract identity and quantity, never account amounts."""
        positions = getattr(account_data, "positions", None)
        if (getattr(account_data, "positions_complete", False) is not True
                or not isinstance(positions, (tuple, list))):
            return False

        entry_totals = {}
        exit_totals = {}
        coordinator = self._order_coordinator
        orders = coordinator.orders() if coordinator is not None else []
        for order in orders:
            role = order.get("role")
            if role not in ("ENTRY", "EXIT"):
                continue
            contract = order.get("contract", {})
            key = self._contract_key(contract)
            try:
                quantity = int(order.get("filled_quantity", 0) or 0)
            except (TypeError, ValueError, ArithmeticError):
                return False
            if quantity < 0:
                return False
            if key is None:
                if quantity:
                    return False
                continue
            underlying = contract.get("underlying_symbol")
            symbol = str(contract.get("symbol", "")).upper()
            if (underlying is not None and str(underlying).upper() != "SPY"):
                if quantity:
                    return False
                continue
            if underlying is None and not symbol.startswith("SPY"):
                if quantity:
                    return False
                continue
            target = entry_totals if role == "ENTRY" else exit_totals
            target[key] = target.get(key, 0) + quantity

        expected = {}
        for key in set(entry_totals) | set(exit_totals):
            bought = entry_totals.get(key, 0)
            sold = exit_totals.get(key, 0)
            if sold > bought:
                return False
            if bought > sold:
                expected[key] = bought - sold

        actual = {}
        for position in positions:
            instrument_type = getattr(position, "instrument_type", None)
            if instrument_type not in ("EQUITY", "OPTION"):
                return False
            if instrument_type != "OPTION":
                continue
            legs = getattr(position, "legs", ())
            if not isinstance(legs, (tuple, list)):
                return False
            position_symbol = str(getattr(position, "symbol", "")).upper()
            leg_symbols = [str(getattr(leg, "symbol", "")).upper() for leg in legs]
            is_spy_position = position_symbol.startswith("SPY") or any(
                symbol.startswith("SPY") for symbol in leg_symbols
            )
            if not is_spy_position:
                continue

            contract, reason = self._emergency_position_contract(position)
            if reason or contract["underlying_symbol"] != "SPY":
                return False
            key = self._contract_key({
                "option_type": contract["option_type"],
                "strike": contract["strike"],
                "expiry": contract["expiry"],
            })
            if key is None:
                return False
            quantity = contract["quantity"]
            actual[key] = actual.get(key, 0) + quantity
        # Exact quantities reject unjournaled/manual SPY option holdings and
        # ensure holdings agree with fill-derived quantities. Equity, buying
        # power, prices, cost basis, and P/L are intentionally not compared.
        return actual == expected

    @staticmethod
    def _emergency_position_contract(position):
        """Return a contract only for an unambiguous, long, single-leg option."""
        if getattr(position, "instrument_type", None) != "OPTION":
            return None, "unsupported non-option position"
        legs = getattr(position, "legs", ())
        if len(legs) != 1:
            return None, "unsupported multi-leg or missing option leg"
        if getattr(position, "option_strategy", None) not in (None, "SINGLE"):
            return None, "unsupported option strategy"
        leg = legs[0]
        try:
            root_quantity = Decimal(str(position.quantity))
            leg_quantity = Decimal(str(leg.quantity))
            if (not root_quantity.is_finite() or not leg_quantity.is_finite()
                    or root_quantity <= 0 or leg_quantity <= 0
                    or root_quantity != leg_quantity
                    or root_quantity != root_quantity.to_integral_value()):
                return None, "unsupported short, fractional, or inconsistent option quantity"
            strike = Decimal(str(leg.exercise_price))
            if not strike.is_finite() or strike <= 0:
                return None, "invalid option strike"
            option_type = str(leg.option_type).upper()
            if option_type not in ("CALL", "PUT"):
                return None, "unknown option type"
            expiry = str(leg.expiration_date)
            symbols = [str(getattr(position, "symbol", "")).upper(),
                       str(getattr(leg, "symbol", "")).upper()]
            underlyings = [symbol for symbol in symbols
                           if re.fullmatch(r"[A-Z]{1,6}", symbol)]
            if not underlyings or len(set(underlyings)) > 1:
                return None, "option underlying is ambiguous"
            underlying = underlyings[0]
            if not expiry:
                return None, "option expiration is missing"
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            return None, "option position fields are malformed"
        return {
            "underlying_symbol": underlying,
            "symbol": getattr(position, "symbol", ""),
            "strike": format(strike, "f"),
            "expiry": expiry,
            "option_type": option_type,
            "quantity": int(root_quantity),
        }, None

    @staticmethod
    def _open_order_contract(order):
        legs = order.get("legs")
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
            return None
        leg = legs[0]
        try:
            return (
                str(order.get("symbol") or leg.get("symbol") or "").upper(),
                Decimal(str(leg.get("strike_price"))).normalize(),
                str(leg.get("option_expire_date", "")),
                str(leg.get("option_type", "")).upper(),
            )
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _contract_identity(contract):
        try:
            return (
                str(contract.get("underlying_symbol", "SPY")).upper(),
                Decimal(str(contract.get("strike"))).normalize(),
                str(contract["expiry"]),
                str(contract["option_type"]).upper(),
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return None

    def _set_emergency_block(self, reason):
        with self._lock:
            self._emergency_strategy1_flat = False
            self._emergency_account_flat = False
            self._emergency_unresolved = [reason]
        self._set_message(f"Emergency Stop is active; account liquidation is unresolved: {reason}")
        log_event("emergency_liquidation_blocked", strategy="strategy1", reason=reason)

    def _emergency_close_account_positions(self, account_snapshot, account_data, coordinator):
        """Serialize emergency passes so one account snapshot cannot fund two sells."""
        if not self._emergency_liquidation_lock.acquire(blocking=False):
            self._set_emergency_block("Another emergency liquidation pass is still reconciling.")
            return
        try:
            self._emergency_close_account_positions_locked(
                account_snapshot, account_data, coordinator
            )
        finally:
            self._emergency_liquidation_lock.release()

    def _emergency_close_account_positions_locked(
        self, account_snapshot, account_data, coordinator
    ):
        if getattr(account_data, "account_type", None) != "MARGIN":
            self._set_emergency_block("The unique MARGIN account could not be verified.")
            return
        if getattr(account_data, "positions_complete", False) is not True:
            self._set_emergency_block("The account positions response is not confirmed complete.")
            return
        snapshot_fresh = getattr(self.account_monitor, "snapshot_is_fresh", None)
        if not callable(snapshot_fresh) or snapshot_fresh() is not True:
            self._set_emergency_block("The local account response is stale or unavailable.")
            return
        if getattr(account_data, "account_id", None) != self._order_account_id:
            self._set_emergency_block("The account does not match the verified order account.")
            return
        if not isinstance(self._emergency_id, str) or not self._emergency_id:
            self._set_emergency_block("The persisted emergency batch identity is unavailable.")
            return

        # A later local response is required to continue after an order action,
        # but receipt time alone never establishes that the broker snapshot
        # includes a fill. Quantities below must independently reconcile.
        snapshot_received = account_snapshot.get("polled_at_monotonic")
        if (self._emergency_refresh_after is not None
                and (snapshot_received is None or snapshot_received <= self._emergency_refresh_after)):
            self._set_emergency_block("Waiting for a new account response after the last order action.")
            return
        self._emergency_refresh_after = None

        try:
            coordinator.begin_emergency_scan(self._emergency_id)
            recovered_external = coordinator.refresh_emergency_orders(self._emergency_id)
            if any(order.get("status") == "UNKNOWN"
                   or order.get("reconciliation_conflict")
                   for order in recovered_external):
                self._set_emergency_block("A previously journaled external order is unresolved.")
                return
            open_orders = coordinator.list_open_orders()
        except Exception:
            self._set_emergency_block("Open orders could not be fully checked.")
            return

        local_by_id = {
            order.get("client_order_id"): order for order in coordinator.orders()
        }
        cancel_sent = False
        unresolved = []
        for order in open_orders:
            side = str(order.get("side", "")).upper()
            intent = str(order.get("position_intent", "")).upper()
            client_order_id = order.get("client_order_id")
            if not isinstance(client_order_id, str) or not client_order_id:
                unresolved.append("An open order has no verifiable client order ID.")
                continue
            local_order = local_by_id.get(client_order_id)
            if local_order is not None:
                if (local_order.get("status") == "UNKNOWN"
                        or local_order.get("reconciliation_conflict")):
                    unresolved.append("A journaled order has conflicting or unknown state.")
                elif (local_order.get("role") == "ENTRY"
                      and local_order.get("status") in ACTIVE_STATUSES):
                    # Local order IDs were journaled before their original submission.
                    try:
                        result = coordinator.cancel(client_order_id)
                        if not result or result.get("status") not in (
                            "CANCELLED", "FILLED", "REJECTED"
                        ):
                            unresolved.append("A local opening order has unresolved cancellation.")
                        else:
                            cancel_sent = True
                    except Exception:
                        unresolved.append("A local opening order has unresolved cancellation.")
                continue
            try:
                # Persist the discovered order before any detail query or cancel.
                record = coordinator.journal_external_order(
                    order, emergency_id=self._emergency_id
                )
                if record.get("role") == "EXTERNAL_UNKNOWN":
                    unresolved.append("An external account order cannot be safely identified.")
                elif side == "BUY" and intent == "BUY_TO_OPEN":
                    detail = coordinator.cancel_external(client_order_id)
                    if detail.get("status") not in ("CANCELLED", "FILLED", "REJECTED"):
                        unresolved.append("An account opening order has unresolved cancellation.")
                    else:
                        cancel_sent = True
                elif side == "SELL" and intent == "SELL_TO_CLOSE":
                    detail = coordinator.confirm_emergency_order(
                        order, emergency_id=self._emergency_id
                    )
                    if detail.get("status") == "UNKNOWN":
                        unresolved.append("An external closing order has unresolved state.")
                else:
                    unresolved.append("A non-supported external order needs manual reconciliation.")
            except Exception:
                unresolved.append("An external account order could not be durably reconciled.")
        try:
            coordinator.finish_emergency_scan(
                self._emergency_id,
                status="UNRESOLVED" if unresolved or cancel_sent else "ORDERS_CONFIRMED",
                phase="ORDER_SCAN_COMPLETE",
            )
        except Exception:
            unresolved.append("Emergency order-scan state could not be persisted.")
        if unresolved:
            self._set_emergency_block(" ".join(sorted(set(unresolved))))
            return
        if cancel_sent:
            monitor_clock = getattr(self.account_monitor, "monotonic", time.monotonic)
            self._emergency_refresh_after = monitor_clock()
            self._set_emergency_block("Opening orders were reconciled; checking confirmed fills and positions.")
            return
        if any(order.get("role") == "ENTRY"
               for order in coordinator.active_orders(role="ENTRY")):
            self._set_emergency_block("An entry order remains active or has unknown status.")
            return

        account_positions = getattr(account_data, "positions", None)
        if not isinstance(account_positions, tuple):
            self._set_emergency_block("The account positions response is malformed.")
            return

        # Build the contract set from both the account and durable order fills.
        # This retains a confirmed BUY even when a delayed account snapshot is empty.
        contracts = {}
        account_quantities = {}
        for position in account_positions:
            contract, reason = self._emergency_position_contract(position)
            if reason:
                unresolved.append(reason)
                continue
            key = self._contract_identity(contract)
            if key is None:
                unresolved.append("An account position has an invalid option identity.")
                continue
            if key in contracts:
                contracts[key]["quantity"] += contract["quantity"]
            else:
                contracts[key] = dict(contract)
            account_quantities[key] = account_quantities.get(key, 0) + contract["quantity"]

        local_orders = coordinator.orders()
        local_orders_by_key = {}
        for order in local_orders:
            if order.get("role") not in ("ENTRY", "EXIT"):
                if (int(order.get("filled_quantity", 0) or 0) > 0
                        or order.get("status") in ("SUBMITTING", "UNKNOWN", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")):
                    unresolved.append("A filled or active order cannot be matched to a supported position.")
                continue
            key = self._contract_identity(order.get("contract", {}))
            if key is None:
                if (int(order.get("filled_quantity", 0) or 0) > 0
                        or order.get("status") in ("SUBMITTING", "UNKNOWN", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")):
                    unresolved.append("A filled or active order has an ambiguous contract.")
                continue
            local_orders_by_key.setdefault(key, []).append(order)
            contracts.setdefault(key, {
                "underlying_symbol": key[0], "symbol": "", "strike": str(key[1]),
                "expiry": key[2], "option_type": key[3], "quantity": 0,
            })
            if order.get("status") == "UNKNOWN":
                unresolved.append(f"An order status is unknown for {key[0]} option.")

        if unresolved:
            self._set_emergency_block(" ".join(sorted(set(unresolved))))
            return

        exposure_remaining = {}
        for key, contract in contracts.items():
            orders = local_orders_by_key.get(key, [])
            entry_filled = sum(
                int(order.get("filled_quantity", 0) or 0)
                for order in orders if order.get("role") == "ENTRY"
            )
            exit_filled = sum(
                int(order.get("filled_quantity", 0) or 0)
                for order in orders if order.get("role") == "EXIT"
            )
            local_net = entry_filled - exit_filled
            if local_net < 0:
                unresolved.append(f"Confirmed order fills conflict for {key[0]} option.")
                continue
            account_quantity = account_quantities.get(key, 0)
            baseline_seed = max(account_quantity, local_net)
            try:
                baseline = coordinator.get_or_create_emergency_baseline(
                    key, baseline_seed, emergency_id=self._emergency_id
                )
                if (int(baseline.get("exit_filled", 0)) > 0
                        and int(baseline["quantity"])
                        != int(baseline.get("ledger_open_quantity", 0))):
                    coordinator.record_emergency_reconciliation(key, {
                        "confirmed_entry_filled": entry_filled,
                        "confirmed_exit_filled": exit_filled,
                        "confirmed_cancelled_entry_quantity": sum(
                            int(order.get("cancelled_quantity", 0) or 0)
                            for order in orders if order.get("role") == "ENTRY"
                        ),
                        "actual_remaining_position_quantity": None,
                        "pending_exit_quantity": sum(
                            max(0, int(order.get("quantity", 0)) - int(order.get("filled_quantity", 0)))
                            for order in orders if order.get("role") == "EXIT"
                            and order.get("status") in (
                                "SUBMITTING", "PENDING", "SUBMITTED", "ACCEPTED",
                                "PARTIALLY_FILLED", "UNKNOWN",
                            )
                        ),
                        "unresolved_order_quantity": sum(
                            int(order.get("unresolved_quantity", 0) or 0)
                            for order in orders
                        ),
                        "account_snapshot_quantity": account_quantity,
                        "reconciliation_status": "CONFLICT",
                    }, emergency_id=self._emergency_id)
                    unresolved.append(
                        f"The initial account quantity conflicts with prior exits for {key[0]} option."
                    )
                    continue
                baseline = coordinator.get_or_create_emergency_baseline(
                    key, baseline_seed, emergency_id=self._emergency_id
                )
                external_fills = sum(
                    int(value) for value in baseline.get("external_exit_fills", {}).values()
                )
                expected_remaining = (
                    int(baseline["quantity"])
                    + entry_filled - int(baseline["entry_filled"])
                    - exit_filled + int(baseline["exit_filled"])
                    - external_fills
                )
                if expected_remaining < 0:
                    raise ValueError
            except (OrderStateError, KeyError, TypeError, ValueError, ArithmeticError):
                unresolved.append(f"Emergency order quantities are malformed for {key[0]} option.")
                continue

            local_exit_coverage = 0
            for order in orders:
                if order.get("role") != "EXIT" or order.get("status") not in (
                    "SUBMITTING", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"
                ):
                    continue
                try:
                    quantity = int(order.get("quantity", 0))
                    filled = int(order.get("filled_quantity", 0))
                    if quantity < 0 or filled < 0 or filled > quantity:
                        raise ValueError
                    local_exit_coverage += quantity - filled
                except (TypeError, ValueError):
                    unresolved.append(f"A local closing order has invalid quantities for {key[0]} option.")

            total_coverage = local_exit_coverage
            cancelled_entry_quantity = sum(
                int(order.get("cancelled_quantity", 0) or 0)
                for order in orders if order.get("role") == "ENTRY"
            )
            unresolved_order_quantity = sum(
                int(order.get("unresolved_quantity", 0) or 0)
                for order in orders
            )
            quantity_conflict = (
                account_quantity != expected_remaining
                or total_coverage > expected_remaining
            )
            try:
                coordinator.record_emergency_reconciliation(key, {
                    "confirmed_entry_filled": entry_filled,
                    "confirmed_exit_filled": exit_filled + external_fills,
                    "confirmed_cancelled_entry_quantity": cancelled_entry_quantity,
                    "actual_remaining_position_quantity": (
                        account_quantity if account_quantity == expected_remaining else None
                    ),
                    "pending_exit_quantity": total_coverage,
                    "unresolved_order_quantity": unresolved_order_quantity,
                    "account_snapshot_quantity": account_quantity,
                    "reconciliation_status": (
                        "CONFLICT" if quantity_conflict
                        else "MATCHED" if account_quantity == expected_remaining
                        else "WAITING_FOR_ACCOUNT_RECONCILIATION"
                    ),
                }, emergency_id=self._emergency_id)
            except OrderStateError:
                unresolved.append(f"Emergency reconciliation state could not be saved for {key[0]} option.")
                continue

            # A lower or higher account quantity cannot authorize a sell by
            # itself. The order-ID journal must explain the complete difference.
            if account_quantity != expected_remaining:
                unresolved.append(f"Account quantity conflicts with confirmed fills for {key[0]} option.")
                continue
            if total_coverage > expected_remaining:
                unresolved.append(f"Closing orders exceed confirmed remaining quantity for {key[0]} option.")
                continue

            exposure_remaining[key] = expected_remaining
            if expected_remaining == 0 or total_coverage == expected_remaining:
                continue
            matching_entries = [
                order for order in orders
                if order.get("role") == "ENTRY"
                and self._contract_identity(order.get("contract", {})) == key
            ]
            if len(matching_entries) > 1:
                unresolved.append(f"Multiple local entries overlap {key[0]} option.")
                continue
            position_key = (
                matching_entries[0].get("client_order_id")
                if matching_entries else "emergency:" + "|".join(map(str, key))
            )
            try:
                result = coordinator.submit_exit(
                    position_key=position_key,
                    contract={**contract, "quantity": expected_remaining},
                    direction=contract["option_type"],
                    quantity=expected_remaining,
                    underlying_symbol=contract["underlying_symbol"],
                    source="emergency_stop",
                    emergency_baseline_quantity=baseline["quantity"],
                    broker_covered_quantity=0,
                )
                if result.get("status") in ("REJECTED", "UNKNOWN"):
                    unresolved.append(
                        f"Emergency closing order is {result['status']} for {key[0]} option."
                    )
                else:
                    self._sync_live_positions(coordinator)
                    monitor_clock = getattr(self.account_monitor, "monotonic", time.monotonic)
                    self._emergency_refresh_after = monitor_clock()
                    self._set_emergency_block(
                        f"A closing order was submitted for {key[0]}; fill and position reconciliation is pending."
                    )
                    return
            except Exception:
                unresolved.append(f"Emergency closing order could not be confirmed for {key[0]} option.")

        if unresolved:
            self._set_emergency_block(" ".join(sorted(set(unresolved))))
            return

        current_orders = coordinator.orders()
        active_orders = coordinator.active_orders()
        current_known_exposure = False
        for order in current_orders:
            if order.get("role") not in ("ENTRY", "EXIT"):
                if (int(order.get("filled_quantity", 0) or 0) > 0
                        or order.get("status") in ("SUBMITTING", "UNKNOWN", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")):
                    current_known_exposure = True
                continue
            key = self._contract_identity(order.get("contract", {}))
            if key is None:
                if int(order.get("filled_quantity", 0) or 0) > 0:
                    current_known_exposure = True
                continue
            if key not in exposure_remaining:
                entry_total = sum(
                    int(item.get("filled_quantity", 0) or 0) for item in current_orders
                    if item.get("role") == "ENTRY" and self._contract_identity(item.get("contract", {})) == key
                )
                exit_total = sum(
                    int(item.get("filled_quantity", 0) or 0) for item in current_orders
                    if item.get("role") == "EXIT" and self._contract_identity(item.get("contract", {})) == key
                )
                if entry_total > exit_total:
                    current_known_exposure = True
        all_flat = (
            getattr(account_data, "positions_complete", False) is True
            and not account_positions
            and not current_known_exposure
            and not open_orders
            and not active_orders
            and not unresolved
            and all(quantity == 0 for quantity in exposure_remaining.values())
        )
        with self._lock:
            self._emergency_unresolved = []
            self._emergency_strategy1_flat = not current_known_exposure
            self._emergency_account_flat = all_flat
        if all_flat:
            self._set_message("Emergency Stop confirmed flat from reconciled positions and order fills.")
        elif account_positions or open_orders or active_orders or current_known_exposure:
            self._set_message("Emergency Stop is active; confirmed positions and orders are being reconciled.")
        else:
            self._set_emergency_block("Position and order quantities are not sufficiently reconciled.")

    @staticmethod
    def _contract_key(contract):
        try:
            option_type = str(contract.get("option_type", "")).upper()
            strike = Decimal(str(contract.get("strike", contract.get("strike_price"))))
            expiry = str(contract.get("expiry", contract.get("expiration_date", contract.get("option_expire_date", ""))))
            if option_type not in ("CALL", "PUT") or not strike.is_finite() or not expiry:
                return None
            return option_type, strike.normalize(), expiry
        except Exception:
            return None

    def _sync_live_positions(self, coordinator):
        orders = coordinator.orders()
        entries = [order for order in orders if order.get("role") == "ENTRY"]
        rebuilt = {"CALL": None, "PUT": None}
        for entry in entries:
            entry_filled = int(entry.get("filled_quantity", 0))
            if entry_filled <= 0 or not entry.get("filled_price"):
                continue
            exits = [order for order in orders if order.get("role") == "EXIT"
                     and order.get("position_key") == entry.get("client_order_id")]
            sold = sum(int(order.get("filled_quantity", 0)) for order in exits)
            remaining = max(0, entry_filled - sold)
            if remaining <= 0:
                continue
            direction = entry.get("direction")
            if direction not in rebuilt:
                continue
            contract = entry.get("contract", {})
            active_exit = next((order for order in reversed(exits)
                                if order.get("status") in ("SUBMITTING", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN")), None)
            position = {
                "direction": direction,
                "symbol": contract.get("symbol"),
                "strike": contract.get("strike"),
                "expiry": contract.get("expiry"),
                "option_type": contract.get("option_type"),
                "entry_price": float(Decimal(entry["filled_price"])),
                "quantity": remaining,
                "opened_at": entry.get("created_at"),
                "highest_bid": float(Decimal(entry["filled_price"])),
                "current_bid": float(Decimal(entry["filled_price"])),
                "take_profit_pct": float(entry.get("take_profit_pct") or 4),
                "stop_loss_pct": float(entry.get("stop_loss_pct") or 5),
                "unrealized_pnl": 0.0,
                "status": (
                    f"EXIT {active_exit.get('status')} — LIVE"
                    if active_exit is not None else "OPEN — LIVE"
                ),
                "entry_client_order_id": entry.get("client_order_id"),
                "exit_client_order_id": active_exit.get("client_order_id") if active_exit else None,
                "exit_rejected": any(order.get("status") == "REJECTED" for order in exits),
            }
            if position["exit_rejected"]:
                position["status"] = "EXIT REJECTED — RETRYING"
            previous = rebuilt[direction]
            if previous is not None:
                # Multiple live positions in one direction cannot be represented
                # safely by this strategy's one-slot-per-direction model.
                self._account_positions_reconciled = False
                continue
            rebuilt[direction] = position
        with self._lock:
            self.positions = rebuilt
        self._publish_positions()

    def _submit_live_entry(self, direction, contract, quote, params, decision,
                           account_snapshot, coordinator):
        if not self.real_trading_authorized:
            self._set_message("Real entry authorization is disabled; no BUY order was submitted.")
            return
        if not self.account_monitor.allows_new_entries() or not self._account_positions_reconciled:
            return
        ask = self._number(quote.get("ask"))
        if ask is None or ask <= 0:
            return
        account_data = account_snapshot.get("account_data")
        if account_data is None or account_data.account_type != "MARGIN":
            return
        with state_lock:
            if state.get("risk_latched") is not False:
                return
        with self._lock:
            if self.positions[direction] is not None or not self._accept_entries or self._emergency:
                return
            allocated_budget = sum(
                position["entry_price"] * 100 * position["quantity"]
                for position in self.positions.values()
                if position is not None
            )
            try:
                record = coordinator.submit_entry(
                    signal_key=f"{direction}:{contract.get('symbol')}:{decision.get('trigger_price')}:{contract.get('expiry', contract.get('expiration_date'))}",
                    contract=contract,
                    direction=direction,
                    price=ask,
                    max_trade_amount=params.get("max_trade_amount"),
                    allocated_amount=allocated_budget,
                    option_buying_power=account_data.option_buying_power,
                    take_profit_pct=params["take_profit_pct"],
                    stop_loss_pct=params["stop_loss_pct"],
                )
            except (OrderStateError, ValueError) as exc:
                self._set_message(f"{direction}: order blocked ({str(exc)}).")
                return
        log_event(
            "live_entry_order",
            strategy="strategy1",
            order_id=record.get("order_id"),
            client_order_id=record.get("client_order_id"),
            direction=direction,
            status=record.get("status"),
            filled_quantity=record.get("filled_quantity"),
            filled_price=record.get("filled_price"),
            decision=decision,
            mode="live",
        )

    def _manage_position(self, direction, position):
        with self._lock:
            emergency_now = self._emergency
        if emergency_now:
            self._submit_live_exit(direction, None, "EMERGENCY STOP")
            return
        try:
            quote = self._engine.get_option_snapshot(position["symbol"])
        except Exception as exc:
            log_event(
                "position_quote_error",
                strategy="strategy1",
                direction=direction,
                symbol=position["symbol"],
                error=str(exc),
                mode="live",
            )
            return

        bid = self._number(quote.get("bid"))
        if bid is None or bid <= 0 or not quote.get("quote_valid"):
            return

        with self._lock:
            current = self.positions[direction]
            if current is None or current["symbol"] != position["symbol"]:
                return
            current["current_bid"] = bid
            current["highest_bid"] = max(current["highest_bid"], bid)
            current["unrealized_pnl"] = round(
                (bid - current["entry_price"]) * 100 * current["quantity"], 2
            )
            emergency = self._emergency
            take_profit = current["entry_price"] * (
                1 + current["take_profit_pct"] / 100
            )
            trailing_stop = current["highest_bid"] * (
                1 - current["stop_loss_pct"] / 100
            )

        if emergency:
            self._submit_live_exit(direction, bid, "EMERGENCY STOP")
        elif bid >= take_profit:
            self._submit_live_exit(direction, bid, "TAKE PROFIT")
        elif bid <= trailing_stop:
            self._submit_live_exit(direction, bid, "TRAILING STOP LOSS")
        else:
            self._publish_positions()

    def _submit_live_exit(self, direction, bid, reason):
        with self._lock:
            position = self.positions[direction]
            # A terminal rejection must not strand a confirmed open position.
            # submit_exit deduplicates active orders, so a later scan can make
            # a fresh attempt for the remaining quantity after rejection.
            if position is None:
                return
            if not self._account_positions_reconciled:
                self._set_message(
                    f"{direction}: automatic exit is paused until account position quantity is reconciled."
                )
                return
            coordinator = self._order_coordinator
            if coordinator is None:
                return
            position_key = position.get("entry_client_order_id")
            contract = {
                "symbol": position.get("symbol"),
                "strike": position.get("strike"),
                "expiry": position.get("expiry"),
                "option_type": position.get("option_type", direction),
            }
            quantity = int(position["quantity"])
        try:
            record = coordinator.submit_exit(
                position_key=position_key,
                contract=contract,
                direction=direction,
                quantity=quantity,
            )
        except (OrderStateError, ValueError) as exc:
            self._set_message(f"{direction}: real exit is unresolved ({type(exc).__name__}).")
            return
        with self._lock:
            current = self.positions.get(direction)
            if current and current.get("entry_client_order_id") == position_key:
                current["exit_client_order_id"] = record.get("client_order_id")
                current["status"] = f"EXIT {record.get('status', 'UNKNOWN')} — LIVE"
        log_event(
            "live_exit_order",
            strategy="strategy1",
            order_id=record.get("order_id"),
            client_order_id=record.get("client_order_id"),
            direction=direction,
            reason=reason,
            status=record.get("status"),
            filled_quantity=record.get("filled_quantity"),
            filled_price=record.get("filled_price"),
            mode="live",
        )
        self._sync_live_positions(coordinator)

    def _publish_market(self, market):
        with state_lock:
            item = state["strategies"]["strategy1"]
            item["spy_price"] = market.get("spy_price")
            item["ema9"] = market.get("ema9")
            item["ema21"] = market.get("ema21")
            item["ema50"] = market.get("ema50")
            item["ema_gap"] = market.get("ema_gap")
            item["five_min_trend"] = market.get("five_min_trend")
            item["signal"] = market.get("signal_status", "Scanning")
            item["message"] = (
                "Strategy 1 is managing positions only; real BUY orders are not authorized."
                if not self.real_trading_authorized
                else "Strategy 1 is active. Real Webull orders are submitted when all entry gates pass."
            )

    def _publish_candidate(self, direction, contract, history, decision):
        candidate = {
            "symbol": contract.get("symbol"),
            "strike": contract.get("strike"),
            "expiry": contract.get("expiry"),
            "tracking_low": history.get("recent_low"),
            "trigger_price": decision.get("trigger_price"),
            "eligible": bool(decision.get("eligible")),
            "status": (
                "eligible"
                if decision.get("eligible")
                else "waiting"
            ),
        }

        with state_lock:
            item = state["strategies"]["strategy1"]
            candidates = item.setdefault(
                "candidates",
                {"CALL": None, "PUT": None},
            )
            candidates[direction] = candidate

            # Preserve legacy fields for existing dashboard consumers.
            item["candidate_option"] = contract.get("symbol")
            item["tracking_low"] = history.get("recent_low")
            item["trigger_price"] = decision.get("trigger_price")
            item["signal"] = f"{direction}: {candidate['status']}"

    def _publish_positions(self):
        with self._lock:
            positions = {
                direction: dict(position) if position else None
                for direction, position in self.positions.items()
            }

        with state_lock:
            item = state["strategies"]["strategy1"]
            active = [p for p in positions.values() if p]
            item["positions"] = positions
            item["order_status"] = (
                " | ".join(
                    f"{p['direction']} {p['status']}" for p in active
                )
                if active else "No open live positions"
            )
            if active:
                # Legacy single-position fields show the first open position.
                p = active[0]
                item["entry_price"] = p["entry_price"]
                item["quantity"] = p["quantity"]
                item["unrealized_pnl"] = p["unrealized_pnl"]
            else:
                item["entry_price"] = None
                item["quantity"] = None
                item["unrealized_pnl"] = None

    def _set_message(self, message):
        with state_lock:
            state["strategies"]["strategy1"]["message"] = str(message)


runner = Strategy1Runner()
