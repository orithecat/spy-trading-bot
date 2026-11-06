"""Persistent, idempotent order coordination for Strategy 1.

All broker access is injected. Importing this module never creates a Webull
client or sends a request; production code supplies the already authenticated
SDK v3 order API and tests supply offline mocks.
"""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid


ACTIVE_STATUSES = {
    "SUBMITTING", "PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"
}
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}


class OrderStateError(RuntimeError):
    """Order state cannot safely authorize a new order."""


def _money(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid order amount.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid order amount.") from None
    if not result.is_finite():
        raise ValueError("Invalid order amount.")
    return result


def _payload(response):
    if isinstance(response, dict):
        return response
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        raise OrderStateError("Order API returned an unsuccessful HTTP status.")
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            value = json_method()
        except Exception:
            raise OrderStateError("Order API response could not be decoded.") from None
        if isinstance(value, dict):
            return value
    raise OrderStateError("Order API response is malformed.")


def _order_item(payload):
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    orders = payload.get("orders")
    if isinstance(orders, list):
        return orders[0] if orders and isinstance(orders[0], dict) else payload
    return payload


def _normalized_status(value):
    status = str(value or "").strip().upper().replace(" ", "_")
    return {
        "PARTIAL_FILLED": "PARTIALLY_FILLED",
        "PARTIALLY_FILLED": "PARTIALLY_FILLED",
        "FAILED": "REJECTED",
        "REJECTED": "REJECTED",
        "CANCELED": "CANCELLED",
        "CANCELLED": "CANCELLED",
        "FILLED": "FILLED",
        "PENDING": "PENDING",
        "SUBMITTED": "SUBMITTED",
        "ACCEPTED": "ACCEPTED",
    }.get(status, "UNKNOWN")


class OrderCoordinator:
    """Persist client order IDs before sending and never blindly resubmit."""

    def __init__(self, order_api, account_id, *, state_path, event_logger=None):
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("A verified account ID is required for orders.")
        if order_api is None:
            raise ValueError("The authenticated order API is unavailable.")
        self.order_api = order_api
        self.account_id = account_id
        self.state_path = Path(state_path)
        self.event_logger = event_logger or (lambda *_args, **_kwargs: None)
        self._lock = threading.RLock()
        self._orders = {}
        self._signals = {}
        self._emergency_baselines = {}
        self._emergency_scans = {}
        self._load()

    def _load(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OrderStateError("Persistent order state is unreadable.") from None
        if not isinstance(data, dict) or not isinstance(data.get("orders", []), list):
            raise OrderStateError("Persistent order state is malformed.")
        if data.get("account_id") != self.account_id:
            raise OrderStateError("Persistent order state belongs to a different or unverified account.")
        for order in data.get("orders", []):
            if not isinstance(order, dict) or not isinstance(order.get("client_order_id"), str):
                raise OrderStateError("Persistent order state is malformed.")
            # A crash during submit is ambiguous. Query this client ID; never
            # generate another ID and repeat the side effect.
            if order.get("status") == "SUBMITTING":
                order["status"] = "UNKNOWN"
            self._update_quantity_tracking(order)
            self._orders[order["client_order_id"]] = order
        signals = data.get("signals", {})
        if not isinstance(signals, dict):
            raise OrderStateError("Persistent signal state is malformed.")
        self._signals = {str(key): str(value) for key, value in signals.items()}
        baselines = data.get("emergency_baselines", {})
        if not isinstance(baselines, dict):
            raise OrderStateError("Persistent emergency state is malformed.")
        self._emergency_baselines = baselines
        scans = data.get("emergency_scans", {})
        if not isinstance(scans, dict):
            raise OrderStateError("Persistent emergency journal is malformed.")
        self._emergency_scans = scans

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            temp.write_text(json.dumps({
                "account_id": self.account_id,
                "orders": list(self._orders.values()),
                "signals": self._signals,
                "emergency_baselines": self._emergency_baselines,
                "emergency_scans": self._emergency_scans,
            }, indent=2), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise OrderStateError("Persistent order state could not be saved.") from None

    @staticmethod
    def _make_order(*, client_order_id, contract, direction, quantity, price,
                    side, intent, order_type, underlying_symbol):
        try:
            strike = _money(contract.get("strike_price", contract.get("strike")))
            expiry = contract.get("option_expire_date", contract.get("expiration_date", contract.get("expiry")))
        except ValueError:
            raise OrderStateError("Selected option contract is incomplete.") from None
        option_type = str(contract.get("option_type", direction)).upper()
        if option_type not in ("CALL", "PUT") or not isinstance(expiry, str) or not expiry:
            raise OrderStateError("Selected option contract is invalid.")
        if strike <= 0 or int(quantity) < 1:
            raise OrderStateError("Order quantity or price is invalid.")
        if not isinstance(underlying_symbol, str) or not underlying_symbol.isalpha():
            raise OrderStateError("Option underlying symbol is invalid.")
        if intent == "SELL_TO_CLOSE" and (side != "SELL" or order_type != "MARKET"):
            raise OrderStateError("Position exits must be MARKET SELL_TO_CLOSE orders.")
        if intent == "BUY_TO_OPEN" and (side != "BUY" or order_type != "LIMIT"):
            raise OrderStateError("New positions must use the authorized LIMIT BUY_TO_OPEN order path.")
        if order_type == "LIMIT" and (price is None or _money(price) <= 0):
            raise OrderStateError("Order quantity or price is invalid.")
        q = str(int(quantity))
        order = {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "order_type": order_type,
            "quantity": q,
            "side": side,
            "position_intent": intent,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": underlying_symbol.upper(),
            "legs": [{
                "side": side,
                "quantity": q,
                "symbol": underlying_symbol.upper(),
                "strike_price": format(strike, "f"),
                "option_expire_date": expiry,
                "instrument_type": "OPTION",
                "option_type": option_type,
                "market": "US",
            }],
        }
        if order_type == "LIMIT":
            order["limit_price"] = format(_money(price), "f")
        if order_type == "MARKET":
            order["support_trading_session"] = "CORE"
        return order

    def active_orders(self, role=None):
        with self._lock:
            return [dict(order) for order in self._orders.values()
                    if order.get("status") in ACTIVE_STATUSES
                    and (role is None or order.get("role") == role)]

    def orders(self):
        with self._lock:
            return [dict(order) for order in self._orders.values()]

    def begin_emergency_scan(self, emergency_id):
        """Persist the account-order scan intent before listing broker orders."""
        if not isinstance(emergency_id, str) or not emergency_id:
            raise OrderStateError("Emergency cycle identity is invalid.")
        with self._lock:
            current = self._emergency_scans.get(emergency_id, {})
            self._emergency_scans[emergency_id] = {
                "account_id": self.account_id,
                "emergency_id": emergency_id,
                "phase": "OPEN_ORDER_SCAN_REQUESTED",
                "status": "UNRESOLVED",
                "started_at": current.get("started_at") or datetime.now(timezone.utc).isoformat(),
            }
            self._save()

    def finish_emergency_scan(self, emergency_id, *, status, phase="RECONCILED"):
        if not isinstance(emergency_id, str) or not emergency_id:
            raise OrderStateError("Emergency cycle identity is invalid.")
        with self._lock:
            current = self._emergency_scans.get(emergency_id)
            if not isinstance(current, dict) or current.get("account_id") != self.account_id:
                raise OrderStateError("Emergency scan journal is unavailable.")
            current["phase"] = str(phase)
            current["status"] = str(status)
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def journal_external_order(self, open_order, *, emergency_id):
        """Persist a discovered account order before querying or cancelling it."""
        if not isinstance(open_order, dict):
            raise OrderStateError("External order data is malformed.")
        client_order_id = open_order.get("client_order_id")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise OrderStateError("External order has no verifiable client order ID.")
        if not isinstance(emergency_id, str) or not emergency_id:
            raise OrderStateError("Emergency cycle identity is invalid.")
        side = str(open_order.get("side", "")).upper()
        intent = str(open_order.get("position_intent", "")).upper()
        role = (
            "ENTRY" if (side, intent) == ("BUY", "BUY_TO_OPEN") else
            "EXIT" if (side, intent) == ("SELL", "SELL_TO_CLOSE") else
            "EXTERNAL_UNKNOWN"
        )
        quantity = filled = None
        contract = None
        try:
            raw_quantity = open_order.get("total_quantity", open_order.get("quantity"))
            qty_decimal = _money(raw_quantity)
            fill_decimal = _money(open_order.get("filled_quantity", open_order.get("filled_qty", 0)))
            if (qty_decimal != qty_decimal.to_integral_value()
                    or fill_decimal != fill_decimal.to_integral_value()):
                raise ValueError
            quantity, filled = int(qty_decimal), int(fill_decimal)
            legs = open_order.get("legs")
            if (quantity < 1 or filled < 0 or filled > quantity
                    or not isinstance(legs, list) or len(legs) != 1
                    or not isinstance(legs[0], dict)):
                raise ValueError
            leg = legs[0]
            strike = _money(leg.get("strike_price"))
            expiry = leg.get("option_expire_date")
            option_type = str(leg.get("option_type", "")).upper()
            underlying = str(open_order.get("symbol") or leg.get("symbol") or "").upper()
            if (strike <= 0 or not isinstance(expiry, str) or not expiry
                    or option_type not in ("CALL", "PUT") or not underlying.isalpha()):
                raise ValueError
            contract = {
                "symbol": leg.get("symbol"), "strike": format(strike, "f"),
                "expiry": expiry, "option_type": option_type,
                "underlying_symbol": underlying,
            }
        except (TypeError, ValueError, ArithmeticError):
            role = "EXTERNAL_UNKNOWN"

        with self._lock:
            existing = self._orders.get(client_order_id)
            if existing is not None and existing.get("source") != "emergency_external":
                raise OrderStateError("External order conflicts with a locally journaled order.")
            if existing is not None:
                same_identity = (
                    existing.get("account_id") == self.account_id
                    and existing.get("emergency_id") == emergency_id
                    and existing.get("side") == side
                    and existing.get("position_intent") == intent
                    and existing.get("quantity") == quantity
                    and existing.get("contract") == contract
                )
                if not same_identity:
                    existing["status"] = "UNKNOWN"
                    existing["reconciliation_conflict"] = True
                    existing["emergency_phase"] = "IDENTITY_CONFLICT"
                    self._update_quantity_tracking(existing)
                    self._save()
                    raise OrderStateError("External order identity changed during reconciliation.")
                existing["emergency_phase"] = "DISCOVERED"
                existing["order_id"] = open_order.get("order_id") or existing.get("order_id")
                # The broker list may be stale. Preserve the highest confirmed
                # cumulative fill and let the detail query apply monotonic checks.
                existing["filled_quantity"] = max(
                    int(existing.get("filled_quantity", 0) or 0), int(filled or 0)
                )
                if existing.get("status") not in TERMINAL_STATUSES:
                    existing["status"] = _normalized_status(
                        open_order.get("status", open_order.get("order_status"))
                    )
                self._update_quantity_tracking(existing)
                record = existing
            else:
                position_key = "external:" + client_order_id
                if role == "EXIT" and contract is not None:
                    identity = (
                        contract["underlying_symbol"],
                        _money(contract["strike"]).normalize(),
                        contract["expiry"], contract["option_type"],
                    )
                    candidates = []
                    for entry in self._orders.values():
                        if entry.get("role") != "ENTRY":
                            continue
                        entry_contract = entry.get("contract", {})
                        try:
                            entry_identity = (
                                str(entry_contract.get("underlying_symbol", "SPY")).upper(),
                                _money(entry_contract.get("strike")).normalize(),
                                str(entry_contract.get("expiry", "")),
                                str(entry_contract.get("option_type", "")).upper(),
                            )
                        except ValueError:
                            continue
                        if entry_identity != identity:
                            continue
                        exits = sum(
                            int(item.get("filled_quantity", 0) or 0)
                            for item in self._orders.values()
                            if item.get("role") == "EXIT"
                            and item.get("position_key") == entry.get("client_order_id")
                        )
                        remaining = int(entry.get("filled_quantity", 0) or 0) - exits
                        if remaining > 0:
                            candidates.append(entry)
                    if len(candidates) == 1:
                        position_key = candidates[0].get("client_order_id")
                record = {
                    "client_order_id": client_order_id,
                    "order_id": open_order.get("order_id"),
                    "account_id": self.account_id,
                    "emergency_id": emergency_id,
                    "emergency_phase": "DISCOVERED",
                    "source": "emergency_external",
                    "signal_key": None,
                    "position_key": position_key,
                    "role": role,
                    "side": side,
                    "position_intent": intent,
                    "direction": contract.get("option_type") if contract else None,
                    "contract": contract or {},
                    "quantity": quantity,
                    "filled_quantity": filled or 0,
                    "filled_price": None,
                    "status": _normalized_status(
                        open_order.get("status", open_order.get("order_status"))
                    ) if role != "EXTERNAL_UNKNOWN" else "UNKNOWN",
                    "reconciliation_conflict": role == "EXTERNAL_UNKNOWN",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                if role == "EXTERNAL_UNKNOWN":
                    record["status"] = "UNKNOWN"
                self._update_quantity_tracking(record)
                self._orders[client_order_id] = record
            self._save()
            return dict(record)

    def _set_external_order_phase(self, client_order_id, emergency_id, phase):
        with self._lock:
            record = self._orders.get(client_order_id)
            if (record is None or record.get("source") != "emergency_external"
                    or record.get("account_id") != self.account_id
                    or record.get("emergency_id") != emergency_id):
                raise OrderStateError("External order journal is unavailable.")
            record["emergency_phase"] = phase
            self._save()

    def refresh_emergency_orders(self, emergency_id):
        """Re-query every journaled external order for the active batch."""
        with self._lock:
            ids = [
                order["client_order_id"] for order in self._orders.values()
                if order.get("source") == "emergency_external"
                and order.get("emergency_id") == emergency_id
            ]
        refreshed = []
        for client_order_id in ids:
            self._set_external_order_phase(
                client_order_id, emergency_id, "DETAIL_QUERY_REQUESTED"
            )
            with self._lock:
                record = self._orders[client_order_id]
            try:
                detail = _payload(self.order_api.get_order_detail(
                    self.account_id, client_order_id
                ))
                item = _order_item(detail)
                with self._lock:
                    record = self._orders[client_order_id]
                    if item.get("client_order_id") not in (None, client_order_id):
                        raise OrderStateError("Order detail belongs to a different order.")
                    self._apply_detail(record, item)
                    record["emergency_phase"] = (
                        "UNRESOLVED" if record.get("reconciliation_conflict")
                        or record.get("status") == "UNKNOWN" else "DETAIL_CONFIRMED"
                    )
                    self._save()
                    refreshed.append(dict(record))
            except Exception:
                with self._lock:
                    record = self._orders[client_order_id]
                    record["status"] = "UNKNOWN"
                    record["emergency_phase"] = "DETAIL_QUERY_UNRESOLVED"
                    self._update_quantity_tracking(record)
                    self._save()
                refreshed.append(dict(record))
        return refreshed

    @staticmethod
    def _emergency_key(contract_key, emergency_id):
        if not isinstance(emergency_id, str) or not emergency_id:
            raise OrderStateError("Emergency cycle identity is invalid.")
        return emergency_id + "|" + "|".join(str(value) for value in contract_key)

    def get_or_create_emergency_baseline(self, contract_key, quantity, *, emergency_id):
        """Persist account quantity and order fills at the first emergency read."""
        key = self._emergency_key(contract_key, emergency_id)
        with self._lock:
            if key in self._emergency_baselines:
                baseline = self._emergency_baselines[key]
                if (not isinstance(baseline, dict)
                        or int(baseline.get("quantity", -1)) < 0
                        or int(baseline.get("entry_filled", -1)) < 0
                        or int(baseline.get("exit_filled", -1)) < 0):
                    raise OrderStateError("Persisted emergency baseline is malformed.")
                return dict(baseline)
            identity = (
                str(contract_key[0]).upper(), _money(contract_key[1]).normalize(),
                str(contract_key[2]), str(contract_key[3]).upper(),
            )
            totals = {"ENTRY": 0, "EXIT": 0}
            for order in self._orders.values():
                contract = order.get("contract", {})
                try:
                    order_identity = (
                        str(contract.get("underlying_symbol", "SPY")).upper(),
                        _money(contract.get("strike")).normalize(),
                        str(contract.get("expiry", "")),
                        str(contract.get("option_type", "")).upper(),
                    )
                except ValueError:
                    continue
                if (order_identity == identity and order.get("role") in totals
                        and not (order.get("role") == "EXIT"
                                 and order.get("source") == "emergency_external")):
                    totals[order["role"]] += int(order.get("filled_quantity", 0))
            baseline = {
                "quantity": int(quantity),
                "entry_filled": totals["ENTRY"],
                "exit_filled": totals["EXIT"],
                "ledger_open_quantity": totals["ENTRY"] - totals["EXIT"],
                "external_exit_fills": {},
            }
            if baseline["quantity"] < 0:
                raise OrderStateError("Emergency position quantity is invalid.")
            self._emergency_baselines[key] = baseline
            self._save()
            return dict(baseline)

    def record_emergency_reconciliation(self, contract_key, values, *, emergency_id):
        """Persist auditable quantities used by the current emergency pass."""
        key = self._emergency_key(contract_key, emergency_id)
        if not isinstance(values, dict):
            raise OrderStateError("Emergency reconciliation state is malformed.")
        with self._lock:
            baseline = self._emergency_baselines.get(key)
            if not isinstance(baseline, dict):
                raise OrderStateError("Emergency baseline is unavailable.")
            allowed = {
                "confirmed_entry_filled", "confirmed_exit_filled",
                "confirmed_cancelled_entry_quantity", "actual_remaining_position_quantity",
                "pending_exit_quantity", "unresolved_order_quantity",
                "account_snapshot_quantity", "reconciliation_status",
            }
            if set(values) - allowed:
                raise OrderStateError("Emergency reconciliation fields are invalid.")
            baseline["reconciliation"] = dict(values)
            self._save()

    def record_emergency_external_fill(self, contract_key, client_order_id,
                                       filled_quantity, *, emergency_id):
        """Persist a matched external SELL_TO_CLOSE fill once by order ID."""
        key = self._emergency_key(contract_key, emergency_id)
        if not isinstance(client_order_id, str) or not client_order_id:
            raise OrderStateError("External closing order identity is invalid.")
        try:
            filled_quantity = int(filled_quantity)
        except (TypeError, ValueError):
            raise OrderStateError("External closing order fill is invalid.") from None
        if filled_quantity < 0:
            raise OrderStateError("External closing order fill is invalid.")
        with self._lock:
            baseline = self._emergency_baselines.get(key)
            if not isinstance(baseline, dict):
                raise OrderStateError("Emergency baseline is unavailable.")
            fills = baseline.setdefault("external_exit_fills", {})
            if not isinstance(fills, dict):
                raise OrderStateError("Emergency external fill state is malformed.")
            previous = fills.get(client_order_id, 0)
            if not isinstance(previous, int) or previous < 0:
                raise OrderStateError("Emergency external fill state is malformed.")
            fills[client_order_id] = max(previous, filled_quantity)
            self._save()

    def record_emergency_external_entry(self, open_order, detail):
        """Persist a confirmed external BUY_TO_OPEN cancellation/fill result."""
        merged = dict(open_order) if isinstance(open_order, dict) else {}
        if isinstance(detail, dict):
            merged.update(detail)
        client_order_id = merged.get("client_order_id")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise OrderStateError("External opening order identity is invalid.")
        if (str(merged.get("side", "")).upper() != "BUY"
                or str(merged.get("position_intent", "")).upper() != "BUY_TO_OPEN"):
            raise OrderStateError("External order is not a verified BUY_TO_OPEN.")
        legs = merged.get("legs")
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
            raise OrderStateError("External opening order contract is ambiguous.")
        leg = legs[0]
        try:
            strike = _money(leg.get("strike_price"))
            quantity = int(_money(merged.get("total_quantity", merged.get("quantity"))))
            expiry = leg.get("option_expire_date")
            option_type = str(leg.get("option_type", "")).upper()
            underlying = str(merged.get("symbol") or leg.get("symbol") or "").upper()
            filled = int(_money(merged.get("filled_quantity", merged.get("filled_qty", 0))))
        except (ValueError, TypeError):
            raise OrderStateError("External opening order quantities are malformed.") from None
        if (quantity < 1 or filled < 0 or filled > quantity or strike <= 0
                or option_type not in ("CALL", "PUT") or not isinstance(expiry, str)
                or not expiry or not underlying.isalpha()):
            raise OrderStateError("External opening order contract or quantities are invalid.")

        with self._lock:
            record = self._orders.get(client_order_id, {
                "client_order_id": client_order_id,
                "order_id": merged.get("order_id"),
                "signal_key": None,
                "position_key": client_order_id,
                "role": "ENTRY",
                "source": "emergency_external_order",
                "direction": option_type,
                "contract": {
                    "symbol": leg.get("symbol"), "strike": format(strike, "f"),
                    "expiry": expiry, "option_type": option_type,
                    "underlying_symbol": underlying,
                },
                "quantity": quantity,
                "filled_quantity": 0,
                "filled_price": None,
                "order_type": merged.get("order_type"),
                "status": "UNKNOWN",
                "take_profit_pct": None,
                "stop_loss_pct": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            if record.get("role") != "ENTRY":
                raise OrderStateError("External opening order conflicts with the order ledger.")
            record["quantity"] = quantity
            record["order_id"] = merged.get("order_id") or record.get("order_id")
            self._apply_detail(record, merged)
            self._orders[client_order_id] = record
            self._save()
            return dict(record)

    def has_unknown_order(self):
        with self._lock:
            return any(order.get("status") == "UNKNOWN" for order in self._orders.values())

    def reserved_amount(self):
        with self._lock:
            total = Decimal("0")
            for order in self._orders.values():
                if order.get("role") != "ENTRY" or order.get("status") not in ACTIVE_STATUSES:
                    continue
                remaining = max(0, int(order["quantity"]) - int(order.get("filled_quantity", 0)))
                total += _money(order["limit_price"]) * 100 * remaining
            return total

    def submit_entry(self, *, signal_key, contract, direction, price,
                     max_trade_amount, allocated_amount, option_buying_power,
                     take_profit_pct, stop_loss_pct):
        with self._lock:
            previous_id = self._signals.get(signal_key)
            if previous_id:
                return dict(self._orders[previous_id])
            if self.has_unknown_order():
                raise OrderStateError("An order has unknown status; new entries are locked.")
            for order in self._orders.values():
                if (order.get("role") == "ENTRY" and order.get("direction") == direction
                        and order.get("status") in ACTIVE_STATUSES):
                    raise OrderStateError("A Strategy 1 entry order is already active for this direction.")

            budget = _money(max_trade_amount)
            allocated = _money(allocated_amount)
            option_bp = _money(option_buying_power)
            unit_cost = _money(price) * 100
            available = min(budget - allocated - self.reserved_amount(), option_bp - self.reserved_amount())
            quantity = int(max(Decimal("0"), available / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
            if quantity < 1:
                raise OrderStateError("Configured budget or option buying power is insufficient.")
            record = self._submit(
                signal_key=signal_key, role="ENTRY", direction=direction,
                contract=contract, quantity=quantity, price=price,
                side="BUY", intent="BUY_TO_OPEN", position_key=signal_key,
                order_type="LIMIT", underlying_symbol="SPY", source="strategy1",
                take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
            )
            return dict(record)

    def submit_exit(self, *, position_key, contract, direction, quantity, price=None,
                    underlying_symbol=None, source="strategy1",
                    emergency_baseline_quantity=None, broker_covered_quantity=0):
        with self._lock:
            underlying_symbol = str(
                underlying_symbol or contract.get("underlying_symbol") or "SPY"
            ).upper()
            base_key = (
                underlying_symbol,
                _money(contract.get("strike_price", contract.get("strike"))).normalize(),
                str(contract.get("expiry", contract.get("option_expire_date", contract.get("expiration_date", "")))),
                str(contract.get("option_type", direction)).upper(),
            )
            covered = 0
            latest_active = None
            for order in self._orders.values():
                if order.get("role") != "EXIT" or order.get("status") not in ACTIVE_STATUSES:
                    continue
                order_contract = order.get("contract", {})
                try:
                    order_key = (
                        str(order_contract.get("underlying_symbol", "SPY")).upper(),
                        _money(order_contract.get("strike")).normalize(),
                        str(order_contract.get("expiry", "")),
                        str(order_contract.get("option_type", "")).upper(),
                    )
                except ValueError:
                    continue
                if order_key == base_key:
                    covered += max(0, int(order.get("quantity", 0)) - int(order.get("filled_quantity", 0)))
                    latest_active = order
            broker_covered_quantity = int(broker_covered_quantity)
            if broker_covered_quantity < 0:
                raise OrderStateError("Covered closing quantity is invalid.")
            covered += broker_covered_quantity
            quantity_to_submit = max(0, int(quantity) - covered)
            if quantity_to_submit == 0:
                return dict(latest_active) if latest_active else {
                    "status": "ALREADY_COVERED", "role": "EXIT",
                    "position_key": position_key, "filled_quantity": 0,
                }
            return dict(self._submit(
                signal_key=None, role="EXIT", direction=direction,
                contract=contract, quantity=quantity_to_submit, price=None,
                side="SELL", intent="SELL_TO_CLOSE", position_key=position_key,
                order_type="MARKET", underlying_symbol=underlying_symbol,
                source=source, emergency_baseline_quantity=emergency_baseline_quantity,
                take_profit_pct=None, stop_loss_pct=None,
            ))

    def _submit(self, *, signal_key, role, direction, contract, quantity,
                price, side, intent, position_key, order_type, underlying_symbol,
                source, take_profit_pct, stop_loss_pct,
                emergency_baseline_quantity=None):
        client_order_id = uuid.uuid4().hex
        order_body = self._make_order(
            client_order_id=client_order_id, contract=contract,
            direction=direction, quantity=quantity, price=price,
            side=side, intent=intent, order_type=order_type,
            underlying_symbol=underlying_symbol,
        )
        record = {
            "client_order_id": client_order_id,
            "order_id": None,
            "signal_key": signal_key,
            "position_key": client_order_id if role == "ENTRY" else position_key,
            "role": role,
            "source": source,
            "direction": direction,
            "contract": {
                "symbol": contract.get("symbol"), "strike": str(contract.get("strike_price", contract.get("strike"))),
                "expiry": order_body["legs"][0]["option_expire_date"], "option_type": order_body["legs"][0]["option_type"],
                "underlying_symbol": underlying_symbol.upper(),
            },
            "quantity": int(quantity),
            "limit_price": order_body.get("limit_price"),
            "order_type": order_type,
            "filled_quantity": 0,
            "remaining_quantity": int(quantity),
            "cancelled_quantity": 0,
            "unresolved_quantity": int(quantity),
            "filled_price": None,
            "status": "SUBMITTING",
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "emergency_baseline_quantity": emergency_baseline_quantity,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._orders[client_order_id] = record
        if signal_key is not None:
            self._signals[signal_key] = client_order_id
        self._save()
        self.event_logger("order_request", client_order_id=client_order_id,
                          direction=direction, role=role, order=order_body)
        try:
            response = self.order_api.place_order(self.account_id, [order_body])
            payload = _payload(response)
            item = _order_item(payload)
            record["order_id"] = item.get("order_id")
            self._apply_detail(record, item, default_status="SUBMITTED")
        except Exception:
            # A transport error can happen after acceptance. Query exactly the
            # original client ID; never retry POST /place.
            try:
                detail = _payload(self.order_api.get_order_detail(self.account_id, client_order_id))
                self._apply_detail(record, _order_item(detail))
            except Exception:
                record["status"] = "UNKNOWN"
        self._save()
        self._log_order(record)
        return record

    @staticmethod
    def _apply_detail(record, item, default_status=None):
        if item.get("order_id"):
            record["order_id"] = str(item["order_id"])
        old_status = _normalized_status(record.get("status"))
        new_status_value = item.get("status", item.get("order_status", default_status))
        new_status = _normalized_status(new_status_value)
        try:
            old_filled = int(_money(record.get("filled_quantity", 0)))
            new_filled_value = _money(item.get(
                "filled_quantity", item.get("filled_qty", old_filled)
            ))
            total_value = _money(record["quantity"])
            if (new_filled_value != new_filled_value.to_integral_value()
                    or total_value != total_value.to_integral_value()):
                raise ValueError
            new_filled = int(new_filled_value)
            total = int(total_value)
            reported_total = item.get("total_quantity", item.get("quantity"))
            if reported_total is not None:
                reported_total_value = _money(reported_total)
                if (reported_total_value != reported_total_value.to_integral_value()
                        or int(reported_total_value) != total):
                    raise ValueError
            if (new_filled < 0 or new_filled > total
                    or new_filled < old_filled
                    or (new_status == "FILLED" and new_filled != total)):
                raise ValueError
            if (old_status in TERMINAL_STATUSES and new_status != old_status):
                raise ValueError
            if record.get("reconciliation_conflict"):
                record["filled_quantity"] = max(old_filled, new_filled)
                record["status"] = "UNKNOWN"
                OrderCoordinator._update_quantity_tracking(record)
                return False
            record["filled_quantity"] = new_filled
            record["status"] = new_status
            price = item.get("filled_price")
            if new_filled and price not in (None, "", "0", 0):
                record["filled_price"] = format(_money(price), "f")
            elif new_filled and record.get("filled_price") is None:
                # Position creation requires a confirmed average fill price.
                record["status"] = "UNKNOWN"
                record["reconciliation_conflict"] = True
        except (ValueError, TypeError):
            try:
                previous = int(record.get("filled_quantity", 0) or 0)
                reported = int(_money(item.get(
                    "filled_quantity", item.get("filled_qty", previous)
                )))
                record["filled_quantity"] = max(previous, reported)
            except (ValueError, TypeError, ArithmeticError):
                pass
            record["status"] = "UNKNOWN"
            record["reconciliation_conflict"] = True
        OrderCoordinator._update_quantity_tracking(record)
        return not record.get("reconciliation_conflict", False)

    @staticmethod
    def _update_quantity_tracking(record):
        try:
            quantity_value = _money(record.get("quantity"))
            filled_value = _money(record.get("filled_quantity", 0))
            if (quantity_value != quantity_value.to_integral_value()
                    or filled_value != filled_value.to_integral_value()):
                raise ValueError
            quantity = int(quantity_value)
            filled = int(filled_value)
            if quantity < 0 or filled < 0 or filled > quantity:
                raise ValueError
        except (ValueError, TypeError):
            record["status"] = "UNKNOWN"
            record["remaining_quantity"] = None
            record["cancelled_quantity"] = None
            record["unresolved_quantity"] = None
            return
        remaining = quantity - filled
        status = record.get("status")
        record["remaining_quantity"] = remaining
        record["cancelled_quantity"] = (
            remaining if status in ("CANCELLED", "CANCELED") else 0
        )
        record["unresolved_quantity"] = remaining if status in ACTIVE_STATUSES else 0

    def refresh_active(self):
        with self._lock:
            changed = []
            for record in self._orders.values():
                if record.get("status") not in ACTIVE_STATUSES:
                    continue
                try:
                    detail = _payload(self.order_api.get_order_detail(
                        self.account_id, record["client_order_id"]))
                    self._apply_detail(record, _order_item(detail))
                except Exception:
                    record["status"] = "UNKNOWN"
                self._log_order(record)
                changed.append(dict(record))
            if changed:
                self._save()
            return changed

    def list_open_orders(self):
        """Read every cursor page; ambiguous responses must not authorize sells."""
        with self._lock:
            pagination_key = None
            seen_keys = set()
            result = []
            for _ in range(1000):
                payload = _payload(self.order_api.list_order_open(
                    self.account_id, pagination_key
                ))
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise OrderStateError("Open-order response is malformed.")
                for row in rows:
                    if not isinstance(row, dict):
                        raise OrderStateError("Open-order response is malformed.")
                    nested = row.get("orders")
                    if nested is None:
                        result.append(dict(row))
                        continue
                    if not isinstance(nested, list):
                        raise OrderStateError("Open-order response is malformed.")
                    for item in nested:
                        if not isinstance(item, dict):
                            raise OrderStateError("Open-order response is malformed.")
                        result.append({**row, **item})
                next_key = payload.get("pagination_key")
                if next_key in (None, ""):
                    return result
                if not isinstance(next_key, str) or next_key in seen_keys:
                    raise OrderStateError("Open-order pagination is malformed.")
                seen_keys.add(next_key)
                pagination_key = next_key
            raise OrderStateError("Open-order pagination exceeded the safety limit.")

    def confirm_order(self, client_order_id):
        """Query a broker order detail without treating list data as current."""
        payload = _payload(self.order_api.get_order_detail(
            self.account_id, client_order_id
        ))
        item = _order_item(payload)
        if item.get("client_order_id") not in (None, client_order_id):
            raise OrderStateError("Order detail belongs to a different order.")
        return dict(item)

    def cancel_external(self, client_order_id):
        """Cancel a previously journaled external order and query its outcome."""
        with self._lock:
            record = self._orders.get(client_order_id)
            if (record is None or record.get("source") != "emergency_external"
                    or record.get("account_id") != self.account_id):
                raise OrderStateError("External order must be journaled before cancellation.")
            emergency_id = record.get("emergency_id")
        self._set_external_order_phase(client_order_id, emergency_id, "CANCEL_REQUESTED")
        try:
            self.order_api.cancel_order(self.account_id, client_order_id)
            phase = "CANCEL_ACKNOWLEDGED"
        except Exception:
            phase = "CANCEL_REQUEST_UNKNOWN"
        self._set_external_order_phase(client_order_id, emergency_id, phase)
        self._set_external_order_phase(client_order_id, emergency_id, "DETAIL_QUERY_REQUESTED")
        with self._lock:
            try:
                payload = _payload(self.order_api.get_order_detail(
                    self.account_id, client_order_id
                ))
                item = _order_item(payload)
                if item.get("client_order_id") not in (None, client_order_id):
                    raise OrderStateError("Order detail belongs to a different order.")
                record = self._orders[client_order_id]
                self._apply_detail(record, item)
                record["emergency_phase"] = (
                    "UNRESOLVED" if record.get("reconciliation_conflict")
                    or record.get("status") == "UNKNOWN" else "DETAIL_CONFIRMED"
                )
                self._save()
                if record.get("status") == "UNKNOWN":
                    raise OrderStateError("External order cancellation is unresolved.")
                return dict(record)
            except OrderStateError:
                raise
            except Exception:
                record = self._orders[client_order_id]
                record["status"] = "UNKNOWN"
                record["emergency_phase"] = "DETAIL_QUERY_UNRESOLVED"
                self._update_quantity_tracking(record)
                self._save()
                raise OrderStateError("External order cancellation is unresolved.") from None

    def confirm_emergency_order(self, open_order, *, emergency_id):
        """Journal an external order before its order-detail request."""
        record = self.journal_external_order(open_order, emergency_id=emergency_id)
        if record.get("role") == "EXTERNAL_UNKNOWN":
            raise OrderStateError("External order cannot be safely identified.")
        self._set_external_order_phase(
            record["client_order_id"], emergency_id, "DETAIL_QUERY_REQUESTED"
        )
        with self._lock:
            try:
                payload = _payload(self.order_api.get_order_detail(
                    self.account_id, record["client_order_id"]
                ))
                item = _order_item(payload)
                if item.get("client_order_id") not in (None, record["client_order_id"]):
                    raise OrderStateError("Order detail belongs to a different order.")
                current = self._orders[record["client_order_id"]]
                self._apply_detail(current, item)
                current["emergency_phase"] = (
                    "UNRESOLVED" if current.get("reconciliation_conflict")
                    or current.get("status") == "UNKNOWN" else "DETAIL_CONFIRMED"
                )
                self._save()
                if current.get("status") == "UNKNOWN":
                    raise OrderStateError("External order detail is unresolved.")
                return dict(current)
            except OrderStateError:
                raise
            except Exception:
                current = self._orders[record["client_order_id"]]
                current["status"] = "UNKNOWN"
                current["emergency_phase"] = "DETAIL_QUERY_UNRESOLVED"
                self._update_quantity_tracking(current)
                self._save()
                raise OrderStateError("External order detail is unresolved.") from None

    def cancel(self, client_order_id):
        with self._lock:
            record = self._orders.get(client_order_id)
            if not record or record.get("status") not in ACTIVE_STATUSES:
                return dict(record) if record else None
            try:
                self.order_api.cancel_order(self.account_id, client_order_id)
            except Exception:
                pass
            # A cancel acknowledgment does not prove the order is cancelled;
            # confirm state and fills from order detail.
            try:
                detail = _payload(self.order_api.get_order_detail(self.account_id, client_order_id))
                self._apply_detail(record, _order_item(detail))
            except Exception:
                record["status"] = "UNKNOWN"
            self._save()
            self._log_order(record)
            return dict(record)

    def _log_order(self, record):
        self.event_logger(
            "order_state", client_order_id=record.get("client_order_id"),
            order_id=record.get("order_id"), role=record.get("role"),
            status=record.get("status"), filled_quantity=record.get("filled_quantity"),
            filled_price=record.get("filled_price"), quantity=record.get("quantity"),
        )

