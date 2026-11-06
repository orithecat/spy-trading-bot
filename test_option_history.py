from datetime import datetime, timezone

from market_data import MarketDataEngine
from webull.data.quotes.option_market_data import OptionMarketData
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan

engine = MarketDataEngine()
options = OptionMarketData(engine.client)

symbol = "SPY261105C00761000"

response = options.get_option_history_bars(
    symbols=[symbol],
    category=Category.US_OPTION.name,
    timespan=Timespan.M1.name,
    count="30",
    real_time_required=False,
)

print("Response type:", type(response).__name__)

if hasattr(response, "status_code"):
    print("HTTP:", response.status_code)
    data = response.json()
else:
    data = response

print("Data type:", type(data).__name__)

if isinstance(data, dict):
    print("Top-level keys:", list(data.keys()))
    print("Response preview:", str(data)[:12000])
elif isinstance(data, list):
    print("Count:", len(data))
    print("Response preview:", str(data[:3])[:12000])
else:
    print("Response:", str(data)[:12000])

print("Local UTC time:", datetime.now(timezone.utc).isoformat())
