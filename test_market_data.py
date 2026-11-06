import logging
import os
from dotenv import load_dotenv

from webull.core.client import ApiClient
from webull.data.quotes.market_data import MarketData
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan

logging.disable(logging.CRITICAL)
load_dotenv()

app_key = os.getenv("WEBULL_APP_KEY")
app_secret = os.getenv("WEBULL_APP_SECRET")

if not app_key or not app_secret:
    print("FAILED: .env credentials are missing")
    raise SystemExit(1)

try:
    print("Initializing Webull client...")

    client = ApiClient(
        app_key,
        app_secret,
        "us",
        connect_timeout=5,
        timeout=10,
        token_check_duration_seconds=20,
        token_check_interval_seconds=2,
    )

    stocks = MarketData(client)

    print("Token initialized successfully.")

    print("\n===== SPY 5-minute K-line test =====")

    response = stocks.get_batch_history_bar(
        symbols=["SPY"],
        category=Category.US_STOCK.value[0],
        timespan=Timespan.M5.value[0],
        count="100",
    )

    print("HTTP status:", response.status_code)
    print("Response:")
    print(response.text)

except Exception as exc:
    print("Request failed:", type(exc).__name__, str(exc))
