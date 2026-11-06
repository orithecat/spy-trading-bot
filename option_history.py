"""Read-only option historical bar retrieval and completed-bar analysis."""

from datetime import datetime, timedelta, timezone
import math

from webull.data.quotes.option_market_data import OptionMarketData
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan


class OptionHistory:
    def __init__(self, client):
        self.market_data = OptionMarketData(client)

    @staticmethod
    def _parse_time(value):
        if not isinstance(value, str):
            return None

        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _positive_float(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(number) or number <= 0:
            return None

        return number

    def get_recent_low(
        self,
        option_symbol,
        lookback_minutes=30,
        max_bar_age_seconds=120,
        now=None,
    ):
        if not option_symbol:
            raise ValueError("Option symbol is required")

        if lookback_minutes <= 0:
            raise ValueError("lookback_minutes must be positive")

        if max_bar_age_seconds <= 0:
            raise ValueError("max_bar_age_seconds must be positive")

        now = now or datetime.now(timezone.utc)

        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        now = now.astimezone(timezone.utc)

        response = self.market_data.get_option_history_bars(
            symbols=[option_symbol],
            category=Category.US_OPTION.name,
            timespan=Timespan.M1.name,
            count="1200",
            real_time_required=False,
        )

        if hasattr(response, "status_code"):
            if response.status_code != 200:
                raise RuntimeError(
                    f"Option history request failed: HTTP {response.status_code}"
                )
            payload = response.json()
        else:
            payload = response

        if not isinstance(payload, list):
            raise ValueError("Unexpected option history response")

        matching = [
            item for item in payload
            if isinstance(item, dict)
            and item.get("symbol") == option_symbol
        ]

        if len(matching) != 1:
            raise ValueError("Missing or ambiguous option history symbol")

        item = matching[0]

        if item.get("delay_minutes") != 0:
            raise ValueError("Delayed option history is not allowed")

        bars = item.get("result")

        if not isinstance(bars, list):
            raise ValueError("Option history bars are missing")

        cutoff = now - timedelta(minutes=lookback_minutes)
        completed = []

        for bar in bars:
            if not isinstance(bar, dict):
                continue

            bar_time = self._parse_time(bar.get("time"))
            low = self._positive_float(bar.get("low"))

            try:
                volume = float(bar.get("volume"))
            except (TypeError, ValueError):
                continue

            if bar_time is None or low is None:
                continue

            if not math.isfinite(volume) or volume <= 0:
                continue

            # A one-minute bar is complete only after its minute has ended.
            if bar_time + timedelta(minutes=1) > now:
                continue

            if bar_time < cutoff:
                continue

            completed.append({
                "time": bar_time,
                "low": low,
                "volume": volume,
            })

        if not completed:
            raise ValueError("No completed option bars in lookback window")

        latest_bar_time = max(bar["time"] for bar in completed)
        age_seconds = (
            now - (latest_bar_time + timedelta(minutes=1))
        ).total_seconds()

        if age_seconds < 0:
            raise ValueError("Option history bar timestamp is in the future")

        if age_seconds > max_bar_age_seconds:
            raise ValueError(
                f"Option history is stale: {age_seconds:.1f}s"
            )

        recent_low = min(bar["low"] for bar in completed)

        return {
            "symbol": option_symbol,
            "recent_low": recent_low,
            "lookback_minutes": lookback_minutes,
            "bar_count": len(completed),
            "latest_bar_time": latest_bar_time,
            "bar_age_seconds": age_seconds,
            "history_valid": True,
        }
