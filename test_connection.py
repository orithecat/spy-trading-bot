import logging
import os
from dotenv import load_dotenv

logging.disable(logging.CRITICAL)
load_dotenv()

app_key = os.getenv("WEBULL_APP_KEY")
app_secret = os.getenv("WEBULL_APP_SECRET")

if not app_key or not app_secret:
    print("FAILED: .env credentials are missing")
    raise SystemExit(1)

try:
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

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
    trade = TradeClient(client)

    print("Requesting account list (read-only)...")
    response = trade.account_v2.get_account_list()

    print("\n========== ACCOUNT LIST RAW RESPONSE ==========")
    print(response.text)
    print("HTTP status:", response.status_code)

    if response.status_code != 200:
        raise RuntimeError("Account query failed")

    accounts = response.json()
    print("Account entries:", len(accounts))

    margin_account = next(
        (a for a in accounts if a.get("account_type") == "MARGIN"), None
    )

    if not margin_account:
        raise RuntimeError("MARGIN account not found")

    margin_id = margin_account["account_id"]

    print("\n========== MARGIN ACCOUNT BALANCE ==========")
    balance = trade.account_v2.get_account_balance(margin_id)
    print(balance.text)

    print("\n========== MARGIN ACCOUNT POSITIONS ==========")
    positions = trade.account_v2.get_account_position(margin_id)
    print(positions.text)

    print("HTTP status:", response.status_code)

    if response.status_code == 200:
        try:
            data = response.json()
            print("JSON response: valid")
            if isinstance(data, list):
                print("Account entries:", len(data))
            elif isinstance(data, dict):
                print("Response structure: object")
            else:
                print("Response structure:", type(data).__name__)
        except Exception:
            print("Response received, but JSON parsing failed")
    else:
        print("Account query did not succeed; check API permissions.")

except Exception as exc:
    print("Connection test failed:", type(exc).__name__)
    print("Details suppressed to protect credentials and account data.")
