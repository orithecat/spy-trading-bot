import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from market_data import MarketDataEngine
from webull.data.quotes.instrument import Instrument
from webull.data.common.category import Category


def main():
    engine = MarketDataEngine()
    instrument = Instrument(engine.client)
    today = datetime.now(ZoneInfo("America/New_York")).date()

    for offset in (0, 1):
        expiry = (today + timedelta(days=offset)).isoformat()

        print(f"\n{'=' * 50}")
        print("TARGET EXPIRY:", expiry)

        response = instrument.list_option_contracts(
            category=Category.US_OPTION.name,
            underlying_symbols="SPY",
            status="LISTING",
            start_date=expiry,
            end_date=expiry,
        )

        print("HTTP:", response.status_code)
        print("REQUEST URL:", response.request.url)

        data = response.json()
        contracts = data.get("data", [])

        print("CONTRACT COUNT:", len(contracts))
        print("EXPIRATIONS:", dict(
            __import__("collections").Counter(
                item.get("expiration_date") for item in contracts
            )
        ))
        print("PAGINATION KEY:", bool(data.get("pagination_key")))

        for item in contracts[:5]:
            print({
                key: item.get(key)
                for key in (
                    "symbol", "option_type", "strike_price",
                    "expiration_date", "tradable_status"
                )
            })


if __name__ == "__main__":
    main()
