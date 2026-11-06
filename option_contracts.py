"""Read-only SPY option contract discovery and OTM contract selection."""

from datetime import date, datetime
import math

from webull.data.quotes.instrument import Instrument
from webull.data.common.category import Category


class OptionContractFinder:
    def __init__(self, client):
        self.instrument = Instrument(client)

    @staticmethod
    def _validate_expiry(expiry):
        if isinstance(expiry, (date, datetime)):
            return expiry.strftime("%Y-%m-%d")

        try:
            parsed = date.fromisoformat(str(expiry))
        except ValueError as exc:
            raise ValueError("expiry must be YYYY-MM-DD") from exc

        return parsed.isoformat()

    @staticmethod
    def _validate_option_type(option_type):
        value = str(option_type).upper()

        if value not in ("CALL", "PUT"):
            raise ValueError("option_type must be CALL or PUT")

        return value

    @staticmethod
    def _to_positive_float(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(number) or number <= 0:
            return None

        return number

    def get_contracts(self, expiry, option_type):
        """Fetch all LISTING SPY contracts for one exact expiry and type."""

        expiry = self._validate_expiry(expiry)
        option_type = self._validate_option_type(option_type)

        contracts = []
        pagination_key = None
        seen_keys = set()

        while True:
            response = self.instrument.list_option_contracts(
                category=Category.US_OPTION.name,
                underlying_symbols="SPY",
                status="LISTING",
                start_date=expiry,
                end_date=expiry,
                option_type=option_type,
                pagination_key=pagination_key,
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"Option contract request failed: HTTP {response.status_code}"
                )

            payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("Unexpected option contract response")

            page = payload.get("data")

            if not isinstance(page, list):
                raise ValueError("Option contract response has no valid data list")

            for contract in page:
                if not isinstance(contract, dict):
                    continue

                if contract.get("underlying_symbol") != "SPY":
                    continue

                if contract.get("expiration_date") != expiry:
                    continue

                if contract.get("option_type") != option_type:
                    continue

                if contract.get("status") != "LISTING":
                    continue

                strike = self._to_positive_float(
                    contract.get("strike_price")
                )

                if strike is None:
                    continue

                item = dict(contract)
                item["strike_price_numeric"] = strike
                contracts.append(item)

            next_key = payload.get("pagination_key")

            if not next_key:
                break

            if next_key in seen_keys:
                raise RuntimeError("Repeated pagination key from contract API")

            seen_keys.add(next_key)
            pagination_key = next_key

        if not contracts:
            raise ValueError(
                f"No LISTING SPY {option_type} contracts found for {expiry}"
            )

        return contracts

    @staticmethod
    def select_otm_contract(contracts, option_type, underlying_price):
        """Select the nearest strictly OTM strike."""

        option_type = OptionContractFinder._validate_option_type(option_type)
        underlying_price = OptionContractFinder._to_positive_float(
            underlying_price
        )

        if underlying_price is None:
            raise ValueError("underlying_price must be positive")

        candidates = []

        for contract in contracts:
            strike = OptionContractFinder._to_positive_float(
                contract.get("strike_price_numeric",
                             contract.get("strike_price"))
            )

            if strike is None:
                continue

            if option_type == "CALL" and strike > underlying_price:
                candidates.append((strike, contract))

            elif option_type == "PUT" and strike < underlying_price:
                candidates.append((strike, contract))

        if not candidates:
            raise ValueError(
                f"No OTM {option_type} contract found at SPY price "
                f"{underlying_price}"
            )

        if option_type == "CALL":
            selected = min(candidates, key=lambda item: item[0])
        else:
            selected = max(candidates, key=lambda item: item[0])

        return dict(selected[1])

    def find_nearest_otm(self, expiry, option_type, underlying_price):
        """Fetch contracts and return the nearest strictly OTM contract."""

        contracts = self.get_contracts(expiry, option_type)

        return self.select_otm_contract(
            contracts,
            option_type,
            underlying_price,
        )
