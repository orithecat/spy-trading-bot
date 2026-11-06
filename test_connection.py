"""Read-only Webull US account connection and data validation."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from account_data import fetch_margin_account_data


def main():
    logging.disable(logging.CRITICAL)

    project_dir = Path(__file__).resolve().parent
    load_dotenv(project_dir / ".env")

    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")

    if not app_key or not app_secret:
        print("FAILED: Webull credentials are unavailable.")
        return 1

    try:
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient

        client = ApiClient(
            app_key,
            app_secret,
            "us",
            connect_timeout=5,
            timeout=10,
            token_check_duration_seconds=20,
            token_check_interval_seconds=2,
        )
        trade = TradeClient(client)

        print("Webull client initialized.")
        print("Reading and validating account data (read-only)...")

        account = fetch_margin_account_data(trade.account_v2)

        print("SUCCESS: exactly one MARGIN account verified.")
        print("Account type:", account.account_type)
        print("Currency:", account.currency)
        print("Equity:", account.equity)
        print("Option buying power:", account.option_buying_power)
        print("Positions parsed:", len(account.positions))
        print("Positions complete:", account.positions_complete)
        print("No orders were submitted.")
        return 0

    except Exception as exc:
        print("FAILED:", type(exc).__name__)
        print("Account data could not be verified. Details suppressed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
